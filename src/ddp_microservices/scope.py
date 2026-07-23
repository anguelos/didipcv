"""Generic, request-scoped basket **scope** — uniform context consumption for DiDip services.

``scope`` is a lazy, ``request``-like proxy: it parses the current request's basket scope ONCE
(from ``?scope=`` for small baskets, or the POST/PUT body ``scope`` for large ones), resolves it
against the service's :class:`fsdb.shared_index.FSDBSharedIndex` into a numpy bool mask over the
sorted charter universe, and caches it on ``flask.g``. A route that never touches ``scope`` never
parses it, so single-item / meta routes pay nothing.

**Resolve is universal (here); apply is per-route.** Only a route knows its candidate set, so the
base cannot auto-clamp -- the per-route one-liner is ``scope.apply(candidate_mask)``.

Wired by :class:`ddp_microservices.microservice.DidipMicroservice`, which registers itself at
``app.extensions["ddp_ms"]`` so this proxy can reach ``self.index``. A service **without** a shared
index (a plain ``DidipMicroservice``) has no charter universe: a *null* scope (no ``?scope=``) is
still fine (``apply`` returns the candidate unchanged), but any real scope use raises -- see
:meth:`Scope._require_index`.
"""
from __future__ import annotations

import json

import numpy as np
from flask import abort, current_app, g, request
from werkzeug.local import LocalProxy

from fsdb.shared_index import IndexMismatch  # noqa: F401  (receive_basket raises it -> base 409)


class ScopeResult:
    """Outcome of intersecting a route's candidate charter set with the active scope."""

    __slots__ = ("mask", "in_scope", "total", "active")

    def __init__(self, mask, in_scope, total, active):
        self.mask = mask            # bool[N]: candidate ∩ active-scope
        self.in_scope = int(in_scope)
        self.total = int(total)     # candidate size before scoping
        self.active = bool(active)

    @property
    def note(self) -> str:
        if self.active:
            return f"{self.in_scope} of {self.total} charters in scope"
        return f"{self.total} charter{'' if self.total == 1 else 's'}"


class Scope:
    """Resolved basket scope for one request: a charter-aligned bool mask + helpers.

    Charter-level only on the wire (the compact basket has no image encoding); :attr:`images` is a
    projection through the index's image->charter map and needs an image-aware index. ``index`` may
    be ``None`` (a non-index service): a null scope is tolerated, any real use raises.
    """

    def __init__(self, index):
        self._index = index         # may be None (non-index service)
        self._charters = None       # bool[N] once resolved; stays None when no scope is present
        self._resolved = False
        self._active = False

    def _require_index(self):
        if self._index is None:
            raise RuntimeError(
                "scope requires a shared index (use a SharedIndexMicroservice); this service has "
                "no charter universe to scope over")

    def _resolve(self):
        if self._resolved:
            return
        self._resolved = True
        # ``request.values`` = query string + form body, so ONE lookup covers both the
        # bookmarkable ``?scope=`` (GET, curl/power users) and the form field posted by the UI
        # navigation (``ddp_scope.js`` submits a hidden ``scope`` input, because a basket can be
        # ~90 kB packed and would 414 in a URL). A JSON body is still accepted for API clients.
        raw = request.values.get("scope")
        if raw is None and request.method in ("POST", "PUT"):
            body = request.get_json(silent=True)
            raw = body.get("scope") if isinstance(body, dict) else None
        if not raw:
            return                   # null scope: fine even without an index
        self._require_index()        # a real scope needs an index universe
        try:
            basket = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            abort(400, "malformed 'scope' parameter (expected a JSON basket)")
        self._charters = self._index.receive_basket(basket)   # bool[N]; IndexMismatch -> base 409
        self._active = True

    @property
    def active(self) -> bool:
        self._resolve()
        return self._active

    @property
    def index_hash(self) -> str:
        self._require_index()
        return self._index.index_hash

    @property
    def charters(self) -> np.ndarray:
        """bool[N_charter] over the sorted charter universe (all-True when no scope is active).
        Allocates a full array when inactive -- prefer :meth:`apply` (or check :attr:`active`) on
        the hot path so a 2M-charter no-op scope costs nothing."""
        self._resolve()
        self._require_index()        # all-True needs the universe size
        if self._charters is None:
            return np.ones(len(self._index), dtype=bool)
        return self._charters

    @property
    def images(self) -> np.ndarray:
        """bool[N_image] derived from the charter mask via a single vectorised gather over
        ``image_to_charter_idx`` (needs an FSDBSharedImageIndex)."""
        self._require_index()
        idx = self._index
        if not hasattr(idx, "image_to_charter_idx"):
            raise TypeError("scope.images requires an image-aware index (FSDBSharedImageIndex)")
        self._resolve()
        if self._charters is None:
            return np.ones(idx.n_images, dtype=bool)
        return self._charters[idx.image_to_charter_idx]   # image row -> its charter's scope bit

    def apply(self, candidate) -> ScopeResult:
        """Intersect a candidate charter bool mask with the active scope (the per-route one-liner).
        Works even on a non-index service when no scope is active (returns the candidate unchanged)."""
        candidate = np.asarray(candidate, dtype=bool)
        total = int(candidate.sum())
        self._resolve()
        if self._charters is None:
            return ScopeResult(candidate, total, total, active=False)
        scoped = candidate & self._charters
        return ScopeResult(scoped, int(scoped.sum()), total, active=True)


def request_has_real_scope() -> bool:
    """True when the CURRENT request carries a scope that would actually narrow the answer.

    Used by the base class to reject a scope sent to a route that does not consume one (silently
    ignoring it would return whole-DB results that the caller believes are scoped). A missing
    scope, and an explicit ``all_charters`` basket, are both "the whole DB" -- honest no-ops on an
    unscoped route -- and are tolerated.

    Deliberately answers False for anything that is not a **wire basket** (unparsable, or not a
    dict with basket keys). The name ``scope`` is not ours alone: the Slicer's
    ``POST /sl/download_basket`` already carries ``scope: "fsdb_noimg"`` meaning its EXPORT scope,
    and rejecting that would break a working service. Only a route that actually consumes the
    basket reports a malformed value, as a 400 from :meth:`Scope._resolve`.
    """
    raw = request.values.get("scope")
    if raw is None and request.method in ("POST", "PUT"):
        body = request.get_json(silent=True)
        raw = body.get("scope") if isinstance(body, dict) else None
    if not raw:
        return False
    try:
        basket = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return False                 # not a wire basket at all -- see the note below
    if not isinstance(basket, dict):
        return False
    if basket.get("all_charters"):
        return False                 # whole DB == the unscoped answer
    return bool(basket.get("charter_ids") or basket.get("fond_ids")
                or basket.get("archive_ids") or basket.get("bit_vector"))


def _current_scope() -> Scope:
    s = getattr(g, "_ddp_scope", None)
    if s is None:
        svc = current_app.extensions.get("ddp_ms")
        index = getattr(svc, "index", None) if svc is not None else None
        s = Scope(index)
        g._ddp_scope = s
    return s


#: request-like proxy -- ``from ddp_microservices import scope; scope.apply(candidate_mask)``.
scope = LocalProxy(_current_scope)
