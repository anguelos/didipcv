"""Object-oriented base classes for DiDip online microservices.

:class:`DidipMicroservice` is the base. It owns the ``fargv`` config (parsed with the shared
``DdpMsConfigs`` base as ``suite_root``, but a service's own ``config_class`` may live in its
own package), the Flask app under its owned prefix, the contract routes
(``/<prefix>/health`` ``/info`` ``/register`` ``/health_report``), Swagger (optional),
**decentralized discovery**, and :meth:`run`. Subclasses set ``config_class`` /
``GLOBAL_ROUTE_PREFIX`` / ``LAUNCH_CMD`` (and ``VIEWS``) and override the hooks :meth:`load`
(the load-time reduction into RAM) and :meth:`register_routes` (service-specific routes).

Discovery is a runtime **registry** (``self.registry``: prefix -> peer descriptor), NOT a
central config enumeration. A service learns peers from the proxy's ``/roster`` at startup
(:meth:`_bootstrap`) and/or by peers pushing their descriptor to ``PUT /<prefix>/register``
(:meth:`_receive_registration`, which reciprocates once); a periodic ``/health`` poll
(:meth:`_poll_registry`) keeps the LIVE subset ``self.siblings``. See the ``ddp_online`` skill.

:class:`SharedIndexMicroservice` adds an :class:`fsdb.shared_index.FSDBSharedIndex` built
at load and the ``/basket`` + ``/basket/db`` routes. See ``static_fsdb.py`` for a concrete
service (:class:`StaticFsdbMicroservice`).

Flask and requests are core dependencies, imported at module top. Only genuinely optional
deps are imported lazily: flasgger (the Swagger UI) is skipped if it is not installed.
"""
from __future__ import annotations

import inspect
import ipaddress
import re
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import jinja2
import numpy as np
import requests
from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from ddp_util.config_ms import DdpMsConfigs
from .scope import scope

#: a proxy_url: a full http(s)://host[:port][/path] URL (a scheme is required). The authority is
#: any non-space, non-slash run, so hostnames, IPv4, and bracketed IPv6 all match.
_PROXY_URL_RE = re.compile(r"^https?://[^\s/]+(/.*)?$")


def _validate_proxy_url(value: str) -> str:
    """Validate a ``proxy_url``: ``''`` (proxyless) or a full ``http(s)://host[:port][/path]`` URL.
    Returns it with any trailing ``/`` stripped; **raises ValueError** on a non-empty malformed
    value -- notably a scheme-less ``host:port`` -- so a bad ``--proxy_url`` fails LOUDLY at
    construction instead of a silent, forever-looping bootstrap."""
    value = (value or "").strip()
    if not value:
        return ""
    if not _PROXY_URL_RE.match(value):
        raise ValueError(
            f"invalid proxy_url {value!r}: expected empty, or a full http(s)://host[:port] URL "
            f"with a scheme (e.g. http://127.0.0.1:8080)")
    return value.rstrip("/")


def _second_level_key(path: str) -> str:
    """Collapse a request path to its first two segments so route FAMILIES merge in the
    request-count monitor, e.g. ``/st/iiif/<md5>/full/1000,`` -> ``/st/iiif`` and every
    ``/st/static/...`` -> ``/st/static``. ``/st/`` stays ``/st/``."""
    return "/".join(path.split("/")[:3]) or "/"


def scoped_ms(cls):
    """Class decorator: mark a microservice as **scope-aware** (``HAS_SCOPE = True``).

    Scope is fundamental, so opting in is explicit -- an undecorated service keeps the base
    ``HAS_SCOPE = False`` and REJECTS any real basket scope (400) instead of silently returning
    whole-DB results the caller believes are scoped.

    Decorating also switches on the route-declaration rule: inside a scoped service every route
    registered by :meth:`register_routes` must be declared with ``@self.scoped_route`` (consumes
    the scope; GET+POST) or ``@self.unscoped_route`` (ignores it; a real scope there -> 400). A
    plain ``@app.route`` raises at startup, so "does this view respect the basket?" is answered
    once, explicitly, per route.
    """
    cls.HAS_SCOPE = True
    return cls


class DidipMicroservice:
    #: a ``DdpMsConfigs.Ms*`` class; MUST be set by subclasses.
    config_class = None
    #: does this service consume a basket scope? Set to True ONLY by the ``@scoped_ms`` decorator;
    #: False here so an undecorated service rejects a real scope rather than ignoring it.
    HAS_SCOPE = False
    #: does this service expose the shared index (``/<prefix>/basket`` + ``/basket/db``)? True on
    #: :class:`SharedIndexMicroservice`. Surfaced to templates as ``has_shared_index``: base.html
    #: mounts the basket UI in the context rail only when it is, since the widget syncs against
    #: those routes and would sit permanently unsynced without them.
    HAS_SHARED_INDEX = False
    #: the path segment this service OWNS (routes are ``/<GLOBAL_ROUTE_PREFIX>/...``). MUST be set
    #: by subclasses and MUST equal ``config_class.route_prefix`` (asserted). See the single-origin
    #: gateway design: every service lives under its prefix so any proxy is a pass-through.
    GLOBAL_ROUTE_PREFIX = None
    #: the CLI to (re)start this service, shown when a sibling is found down. MUST equal
    #: ``config_class.launch_cmd`` (asserted).
    LAUNCH_CMD = None
    #: the hand-off view types this service ACCEPTS (a datum a sibling can send it and it will
    #: display), e.g. ``("charter", "root")``. Advertised in the descriptor; drives which sibling
    #: links appear for a page's context view. Extensible; empty by default.
    VIEWS: tuple = ()

    #: a valid owned prefix / registry key: short, lowercase, starts with a letter.
    _PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]*$")

    #: url_map paths allowed to sit outside the prefix (flasgger's own routes, if installed).
    #: NOTE: flasgger also registers /oauth2-redirect.html at the ROOT; the primary guard is the
    #: endpoint-name check (any 'flasgger.*' endpoint), this is a belt-and-suspenders fallback.
    _PREFIX_EXEMPT = ("/apispec", "/flasgger_static", "/documentation", "/oauth2-redirect.html")

    def __init__(self, cfg=None, *, argv=None):
        if self.config_class is None:
            raise TypeError(f"{type(self).__name__}.config_class must be set")
        self.cfg = cfg if cfg is not None else self.parse_config(argv=argv)
        self.started_at = time.time()
        self._proxy_url = _validate_proxy_url(getattr(self.cfg, "proxy_url", "") or "")  # raises on a bad value
        self.registry: dict[str, dict] = {}      # prefix -> peer descriptor (all known siblings)
        self.siblings: list[dict] = []           # LIVE subset of the registry, refreshed by the monitor
        self._request_counts = defaultdict(int)  # route rule -> served count (health_report)
        self._counts_lock = threading.Lock()     # threaded server: guard the counter
        # scope bookkeeping: which endpoints consume / ignore a basket scope, and which belong to
        # the base itself (exempt from the @scoped_ms declaration rule and the rejection guard).
        self._scoped_endpoints: set[str] = set()
        self._unscoped_endpoints: set[str] = set()
        self._core_endpoints: set[str] = set()
        self.app = self._create_app()
        self.load()
        self._register_core_routes()
        self._core_endpoints = set(self.app.view_functions)   # everything so far is the base's
        self.register_routes()
        self._assert_routes_prefixed()
        self._assert_scope_declarations()
        self._register_scope_guard()

    # ---- config + siblings (pure; no Flask) ----------------------------------------
    @classmethod
    def parse_config(cls, argv=None):
        """Parse this service's config from the shared ``DdpMsConfigs`` suite."""
        import fargv
        cfg, _ = fargv.parse(cls.config_class, suite_root=DdpMsConfigs, given_parameters=argv)
        return cfg

    @property
    def route_prefix(self) -> str:
        """This service's owned path prefix (no slashes), e.g. ``st``."""
        return self.cfg.route_prefix

    @property
    def proxy_url(self) -> str:
        """The validated rendezvous proxy URL (``''`` = proxyless). Seeded from config at
        construction (which raises on a malformed value); runtime-settable via the setter, which
        also validates. Used by :meth:`_bootstrap` and to derive :attr:`absolute_links`."""
        return self._proxy_url

    @proxy_url.setter
    def proxy_url(self, value):
        self._proxy_url = _validate_proxy_url(value)

    @property
    def absolute_links(self) -> bool:
        """Whether the topnav renders sibling hand-off links ABSOLUTE (to each sibling's own
        ``base_url``) rather than root-relative (``/<prefix>/…``). Derived from ``proxy_url``:
        **empty** ``proxy_url`` (proxyless, each service its own origin) -> absolute; a **set**
        ``proxy_url`` (single-origin front proxy / gateway) -> root-relative. See doc/proxyless_ms.md."""
        return not self._proxy_url

    # ---- decentralized discovery: descriptor + registration ------------------------
    def descriptor(self) -> dict:
        """This service's stable self-description: what ``/info`` returns and ``/register``
        pushes to peers. ``prefix`` is the primary key; ``base_url`` its reachable address."""
        icon = getattr(self.cfg, "icon", None)
        return {
            "prefix": self.route_prefix,
            "base_url": self.cfg.url,
            "name": type(self).__name__,
            "icon": icon,
            "launch_cmd": self.LAUNCH_CMD,
            "views": list(self.VIEWS),
        }

    def _valid_descriptor(self, desc) -> bool:
        return (isinstance(desc, dict)
                and isinstance(desc.get("prefix"), str) and bool(self._PREFIX_RE.match(desc["prefix"]))
                and isinstance(desc.get("base_url"), str) and bool(desc["base_url"]))

    def _receive_registration(self, desc) -> bool:
        """Store a peer's pushed descriptor (keyed by prefix); the FIRST time we learn a peer,
        reciprocate so both sides hold full descriptors (idempotent -- a peer that already knows
        us will not reciprocate back, so it terminates)."""
        if not self._valid_descriptor(desc) or desc["prefix"] == self.route_prefix:
            return False
        prefix = desc["prefix"]
        first_time = prefix not in self.registry
        self.registry[prefix] = desc
        if first_time:
            threading.Thread(target=self._register_with, args=(desc["base_url"], prefix), daemon=True).start()
        return True

    def _register_with(self, base_url: str, prefix: str) -> None:
        """Best-effort push of our descriptor to a peer's ``/<prefix>/register`` (PUT)."""
        try:
            requests.put(f"{base_url}/{prefix}/register", json=self.descriptor(), timeout=2)
        except requests.RequestException:
            pass

    def _register_allowed(self, remote_addr) -> bool:
        """Trust gate for ``/register``: loopback, or the configured trusted subnet. The gateway
        must ALSO refuse to forward PUT, or a proxied caller would appear as loopback."""
        try:
            ip = ipaddress.ip_address(remote_addr)
        except (ValueError, TypeError):
            return False
        if ip.is_loopback:
            return True
        cidr = getattr(self.cfg, "register_trusted_cidr", "") or ""
        if cidr:
            try:
                return ip in ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                return False
        return False

    # ---- Flask app -----------------------------------------------------------------
    def _create_app(self):
        base = Path(__file__).resolve().parent
        # static is served under the prefix too (``/<prefix>/static/...``), so no route escapes it.
        # The static folder stays the shared ``ddp_microservices/static`` (base.html chrome, the
        # basket widget, ...); an out-of-tree app reuses it by depending on this package.
        app = Flask(type(self).__name__, template_folder=str(base / "templates"),
                    static_folder=str(base / "static"),
                    static_url_path=f"/{self.route_prefix}/static")
        app.extensions["ddp_ms"] = self   # so the request-scoped `scope` proxy can reach self.index
        self._add_app_templates(app, base)
        self._init_swagger(app)
        return app

    def _add_app_templates(self, app, base: Path):
        """Let a subclass defined in ANOTHER package ship its own templates next to the shared
        ``base.html``. If ``<subclass module dir>/templates`` exists and differs from the shared
        one, it is appended to the Jinja loader (shared chrome keeps priority; the app only ADDS
        page templates). This is what lets a decentralized app (e.g. ddpa_layout) extend
        ``base.html`` without vendoring it. No-op for in-tree services."""
        try:
            sub = Path(inspect.getfile(type(self))).resolve().parent / "templates"
        except (TypeError, OSError):
            return
        if not sub.is_dir() or sub == (base / "templates"):
            return
        app.jinja_loader = jinja2.ChoiceLoader([app.jinja_loader,
                                                jinja2.FileSystemLoader(str(sub))])

    # ---- scope-aware route declaration (only meaningful on a @scoped_ms service) --------------
    def scoped_route(self, rule, **options):
        """Declare a route that **consumes** the active basket scope.

        Same call shape as ``@app.route``, but it defaults to ``methods=["GET", "POST"]`` so a
        large basket can be POSTed (a packed bit-vector over ~1M charters is ~90 kB and would 414
        as a URL) while ``?scope=`` still works for curl/API clients. The endpoint is recorded so
        the base knows this view is allowed to receive a scope.

        Inside the view, read the scope through the request proxy::

            from ddp_microservices import scope
            mask = scope.charters          # bool[N_charter]; all-True when no basket is active
            hits = scope.apply(candidate)  # or intersect your own candidate mask
        """
        options.setdefault("methods", ["GET", "POST"])

        def decorator(fn):
            self._scoped_endpoints.add(fn.__name__)
            return self.app.route(rule, **options)(fn)
        return decorator

    def unscoped_route(self, rule, **options):
        """Declare a route that **ignores** the basket scope (images, health, static assets...).

        Sending a real scope here is a caller error -- the answer would be whole-DB while the
        caller believes it is scoped -- so the base rejects it with 400. Declaring the route this
        way is what makes that rejection precise instead of service-wide.
        """
        def decorator(fn):
            self._unscoped_endpoints.add(fn.__name__)
            return self.app.route(rule, **options)(fn)
        return decorator

    def _assert_scope_declarations(self):
        """On a ``@scoped_ms`` service, every app route must be declared via ``scoped_route`` or
        ``unscoped_route``; a plain ``@app.route`` raises here (at construction, like the
        own-the-prefix check). Core routes registered by the base itself are exempt."""
        if not self.HAS_SCOPE:
            return
        declared = self._scoped_endpoints | self._unscoped_endpoints | self._core_endpoints
        offenders = []
        for rule in self.app.url_map.iter_rules():
            ep = rule.endpoint
            if ep in declared or ep.startswith("flasgger") or ep == "static":
                continue
            if not rule.rule.startswith(f"/{self.route_prefix}/") and rule.rule != f"/{self.route_prefix}":
                continue                      # not ours (already caught by the prefix check)
            offenders.append((rule.rule, ep, self._route_definition(ep)))
        if offenders:
            listing = "\n".join(f"  - {r}  (endpoint {ep!r}){loc}" for r, ep, loc in offenders)
            raise AssertionError(
                f"{type(self).__name__} is @scoped_ms, so every route it declares must say whether "
                f"it consumes the basket scope. {len(offenders)} route(s) used a plain @app.route; "
                f"use @self.scoped_route (consumes the scope) or @self.unscoped_route (ignores "
                f"it):\n{listing}")

    def _register_scope_guard(self):
        """Reject a basket scope sent to a route that does not consume one (400).

        Without this, an ignored scope returns whole-DB results that look scoped -- a silent wrong
        answer. Applies to every service: on an undecorated one NO route consumes scope, so any
        real scope is rejected; on a ``@scoped_ms`` one only the ``unscoped_route`` views reject.
        A missing scope and an ``all_charters`` basket are honest no-ops and always pass.
        """
        from .scope import request_has_real_scope

        @self.app.before_request
        def _guard_scope():
            ep = request.endpoint
            if ep is None or ep in self._scoped_endpoints:
                return None                          # this view consumes the scope
            if ep.startswith("flasgger") or ep == "static" or ep in self._core_endpoints:
                return None                          # framework / base routes: not app answers
            if not request_has_real_scope():
                return None
            who = "does not consume a basket scope" if self.HAS_SCOPE else \
                  "is not scope-aware (not decorated with @scoped_ms)"
            return jsonify({"error": "scope_not_supported",
                            "detail": f"route {request.path!r} {who}; the result would be the "
                                      f"whole database, not the basket."}), 400

    def _assert_routes_prefixed(self):
        """Enforce the own-the-prefix contract: every registered rule lives under
        ``/<route_prefix>/`` (except flasgger's own routes). Also guard the class attrs against
        drifting from the config. Raises at construction so a mis-prefixed route can't ship."""
        if self.GLOBAL_ROUTE_PREFIX != self.route_prefix:
            raise AssertionError(f"{type(self).__name__}.GLOBAL_ROUTE_PREFIX="
                                 f"{self.GLOBAL_ROUTE_PREFIX!r} != config route_prefix {self.route_prefix!r}")
        if self.LAUNCH_CMD != getattr(self.cfg, "launch_cmd", self.LAUNCH_CMD):
            raise AssertionError(f"{type(self).__name__}.LAUNCH_CMD={self.LAUNCH_CMD!r} "
                                 f"!= config launch_cmd {getattr(self.cfg, 'launch_cmd', None)!r}")
        base = f"/{self.route_prefix}/"
        offenders = []
        for rule in self.app.url_map.iter_rules():
            r = rule.rule
            if r == base.rstrip("/") or r.startswith(base):
                continue
            # flasgger's own routes are exempt: by endpoint (robust across versions) or path.
            if rule.endpoint.startswith("flasgger") or r.startswith(self._PREFIX_EXEMPT):
                continue
            offenders.append((r, rule.endpoint, self._route_definition(rule.endpoint)))
        if offenders:
            listing = "\n".join(f"  - {r}  (endpoint {ep!r}){loc}" for r, ep, loc in offenders)
            raise AssertionError(
                f"{type(self).__name__}: {len(offenders)} route(s) are not under the "
                f"'/{self.route_prefix}/' prefix (own-the-prefix contract). Either prefix them or, "
                f"for a third-party route, add its path to _PREFIX_EXEMPT:\n{listing}")

    def _route_definition(self, endpoint: str) -> str:
        """`` — defined at <file>:<line>`` for a route's view function (best-effort; empty if the
        source can't be located, e.g. a C/built-in or lambda handler)."""
        fn = self.app.view_functions.get(endpoint)
        if fn is None:
            return ""
        try:
            return f" — defined at {inspect.getsourcefile(fn)}:{inspect.getsourcelines(fn)[1]}"
        except (TypeError, OSError):
            return ""

    def _init_swagger(self, app):
        try:
            from flasgger import Swagger
        except Exception:
            return  # Swagger is optional; skip if flasgger is not installed
        Swagger(app, config={"specs_route": "/documentation/", "swagger_ui": True,
                             "specs": [{"endpoint": "apispec_1", "route": "/apispec_1.json",
                                        "rule_filter": lambda r: True, "model_filter": lambda t: True}],
                             "headers": [], "static_url_path": "/flasgger_static"})

    def _register_core_routes(self):
        app = self.app
        p = f"/{self.route_prefix}"  # core routes live under the prefix too (gateway owns "/")

        # A wire basket that references a different index universe -> 409, so the client re-syncs
        # from /basket/db. Raised by index.receive_basket (reached via the `scope` proxy or directly).
        from fsdb.shared_index import IndexMismatch

        @app.errorhandler(IndexMismatch)
        def _index_mismatch(e):
            return jsonify({"error": "index_mismatch", "index_hash": getattr(e, "got", None)}), 409

        level = getattr(self.cfg, "monitor_level", 1)
        if level > 0:                            # 0 -> no per-request hook at all (zero overhead)
            @app.after_request
            def _count_request(resp):
                rule = request.url_rule
                if rule is not None:             # matched a real route (skip 404s -> bounded keys)
                    key = rule.rule if level >= 2 else _second_level_key(request.path)
                    with self._counts_lock:
                        self._request_counts[key] += 1
                return resp

        @app.route(f"{p}/health")
        def health():
            """Liveness.
            ---
            responses:
              200: {description: service is up}
            """
            return "OK", 200

        @app.route(f"{p}/info")
        def info():
            """Service info: the descriptor + uptime + live siblings.
            ---
            responses:
              200: {description: info dict}
            """
            return jsonify(self.info_dict())

        @app.route(f"{p}/register", methods=["GET", "PUT"])
        def register():
            """Peer self-registration: a sibling PUTs its descriptor (JSON) to announce itself.
            **PUT-only** (plus a GET debug form that itself PUTs), so the gateway blocking forwarded
            PUT fully seals it off; loopback / trusted-subnet only.
            ---
            responses:
              200: {description: registered; returns the known-prefix list}
              400: {description: invalid descriptor}
              403: {description: caller not on loopback / trusted subnet}
            """
            if request.method == "GET":
                return self._register_form()
            if not self._register_allowed(request.remote_addr):
                abort(403, "registration is allowed from loopback / trusted subnet only")
            desc = request.get_json(silent=True)
            if not self._receive_registration(desc):
                abort(400, "invalid descriptor (need at least a valid prefix and base_url)")
            return jsonify({"registered": desc["prefix"], "known": sorted(self.registry)}), 200

        @app.route(f"{p}/health_report")
        def health_report():
            """Uptime + per-route served-request counts. ``?format=json`` for JSON, else HTML.
            ---
            responses:
              200: {description: health report}
            """
            if request.args.get("format") == "json":
                return jsonify(self.health_report())
            return self._health_report_html(self.health_report())

        @app.route(f"{p}/icon.ico")
        def icon():
            """This service's own icon, at a canonical route. A sibling renders it via
            ``/<prefix>/icon.ico`` (through the gateway) without needing the file locally; it also
            serves as the page favicon. Served from the configured ``cfg.icon`` file.
            ---
            responses:
              200: {description: the icon}
              404: {description: no icon configured}
            """
            rel = getattr(self.cfg, "icon", None)
            if not rel or not app.static_folder:
                abort(404)
            fname = rel.split("static/", 1)[-1] if "static/" in rel else rel
            return send_from_directory(app.static_folder, fname)

    def health_report(self) -> dict:
        """This service's uptime and per-route served-request counts (subclasses may add keys,
        e.g. the shared-index hash). Aggregated by the gateway's ``/health_report``."""
        with self._counts_lock:
            counts = dict(self._request_counts)
        return {
            "service": type(self).__name__,
            "route_prefix": self.route_prefix,
            "url": self.cfg.url,
            "uptime_s": round(time.time() - self.started_at, 1),
            "monitor_level": getattr(self.cfg, "monitor_level", 1),
            "total_requests": sum(counts.values()),
            "request_counts": counts,
            "siblings": [d.get("prefix") for d in self.siblings],   # live known siblings
        }

    @staticmethod
    def _health_report_html(rep: dict) -> str:
        """A simple tabular HTML rendering of one service's health_report dict."""
        rows = "".join(f"<tr><td>{route}</td><td style='text-align:right'>{n}</td></tr>"
                       for route, n in sorted(rep.get("request_counts", {}).items()))
        extra = "".join(f" · {k}: {v}" for k, v in rep.items()
                        if k not in ("service", "url", "uptime_s", "total_requests", "request_counts"))
        return (f"<h2>{rep['service']}</h2>"
                f"<p>{rep.get('url', '')} · uptime: {rep['uptime_s']} s · "
                f"total requests: {rep['total_requests']}{extra}</p>"
                f"<table border='1' cellpadding='4'><tr><th>route</th><th>requests</th></tr>"
                f"{rows}</table>")

    def info_dict(self) -> dict:
        """The descriptor plus runtime info (uptime + live sibling descriptors)."""
        return {
            **self.descriptor(),
            "fsdb_root": getattr(self.cfg, "fsdb_root", None),
            "uptime_s": round(time.time() - self.started_at, 1),
            "siblings": self.siblings,
        }

    def _register_form(self) -> str:
        """A minimal HTML page to PUT a descriptor by hand (debugging). No dependencies."""
        prefix = self.route_prefix
        placeholder = ('{"prefix":"","base_url":"http://host:port","name":"",'
                       '"icon":null,"launch_cmd":"","views":[]}')
        return (
            f"<h2>{type(self).__name__} — manual peer registration</h2>"
            f"<p>PUT a descriptor JSON to <code>/{prefix}/register</code> "
            f"(loopback / trusted-subnet only). Currently known: "
            f"<code>{', '.join(sorted(self.registry)) or '(none)'}</code>.</p>"
            f"<textarea id='d' rows='6' cols='72'>{placeholder}</textarea><br>"
            f"<button onclick=\"fetch('/{prefix}/register',{{method:'PUT',"
            f"headers:{{'Content-Type':'application/json'}},"
            f"body:document.getElementById('d').value}})"
            f".then(r=>r.text()).then(t=>{{document.getElementById('o').textContent=t;}})\">"
            f"Register</button><pre id='o'></pre>")

    # ---- template context ----------------------------------------------------------
    def render_context(self) -> dict:
        """Common variables every template's base needs. Siblings are **all known** peers (the
        whole registry), sorted by prefix for a stable topnav, each flagged ``alive`` (in the
        live health-poll set) so the box can grey out the unreachable ones. Icons are the
        basename only -- the topnav serves them from THIS service's own (shared) static, so a
        down sibling still shows its icon rather than a broken image."""
        icon = getattr(self.cfg, "icon", None)
        live = {d.get("prefix") for d in self.siblings}
        # the box shows THIS service too (flagged is_self, always alive), sorted in with the peers.
        # Icons are NOT carried here: each service serves its own at /<prefix>/icon.ico, so the
        # topnav references a sibling's icon by route, never by a local file.
        known = dict(self.registry)
        known[self.route_prefix] = {"prefix": self.route_prefix, "base_url": self.cfg.url,
                                    "name": type(self).__name__,
                                    "views": list(self.VIEWS), "launch_cmd": self.LAUNCH_CMD}
        siblings = []
        for prefix in sorted(known):                             # stable order: by route prefix
            d = known[prefix]
            siblings.append({
                "name": d.get("name") or prefix,
                "prefix": prefix,
                "url": d.get("base_url"),
                "views": d.get("views") or [],
                "launch_cmd": d.get("launch_cmd"),
                "alive": prefix == self.route_prefix or prefix in live,
                "is_self": prefix == self.route_prefix,
            })
        return {
            "service_name": type(self).__name__,
            "service_icon": icon.rsplit("/", 1)[-1] if icon else None,  # basename for url_for('static')
            "service_url": self.cfg.url,
            "route_prefix": f"/{self.route_prefix}",  # base path templates/client prepend to links
            "views": list(self.VIEWS),                # this service's own accepted view types
            "has_shared_index": self.HAS_SHARED_INDEX,  # -> base.html mounts the basket in ctx_nav
            "absolute_links": self.absolute_links,    # true (proxyless) -> absolute sibling links; false -> root-relative
            "siblings": siblings,
        }

    def render(self, template: str, **ctx):
        """render_template with the common context merged in (call-site ctx wins)."""
        return render_template(template, **{**self.render_context(), **ctx})

    # ---- hooks (subclasses override) -----------------------------------------------
    def load(self):
        """Load-time reduction of the FSDB into RAM. No-op in the base."""

    def register_routes(self):
        """Register service-specific routes on ``self.app``. No-op in the base."""

    # ---- discovery bootstrap + liveness monitor + run ------------------------------
    def _bootstrap(self):
        """Find the proxy (poll ``cfg.proxy_url`` until it answers), fetch its roster of
        ``{prefix, base_url}``, and register our descriptor with each peer. No proxy configured
        -> no-op (a manual ``curl`` script seeds the mesh instead). Registering a roster peer
        also pre-seeds it into our registry, so its reciprocal push just fills in the full
        descriptor without triggering a re-reciprocation."""
        proxy = self._proxy_url            # validated at construction (full URL or ''); no trailing /
        if not proxy:
            return
        interval = getattr(self.cfg, "proxy_poll_seconds", 1.0)
        verbose = getattr(self.cfg, "verbosity", 0)
        roster, attempts = None, 0
        while roster is None:
            try:
                resp = requests.get(f"{proxy}/roster", timeout=2)
                if resp.status_code == 200:
                    roster = resp.json()
                elif verbose and attempts % 10 == 0:
                    print(f"[{type(self).__name__}] proxy {proxy}/roster -> HTTP {resp.status_code}, retrying…",
                          file=sys.stderr, flush=True)
            except requests.RequestException as e:
                if verbose and attempts % 10 == 0:  # every ~10 tries, so a stuck bootstrap is visible
                    print(f"[{type(self).__name__}] waiting for proxy roster at {proxy}/roster … "
                          f"({type(e).__name__}: {e})", file=sys.stderr, flush=True)
            attempts += 1
            if roster is None:
                time.sleep(interval)
        if verbose:
            print(f"[{type(self).__name__}] roster from {proxy}: {[e.get('prefix') for e in roster]}; "
                  f"registering with peers", file=sys.stderr, flush=True)
        for entry in roster:
            prefix, base_url = entry.get("prefix"), entry.get("base_url")
            if not prefix or not base_url or prefix == self.route_prefix:
                continue
            self.registry.setdefault(prefix, {"prefix": prefix, "base_url": base_url})
            self._register_with(base_url, prefix)

    def _poll_registry(self):
        """Refresh the LIVE sibling list by polling each known peer's ``/<prefix>/health``.
        Discovery adds to ``self.registry``; this poll decides liveness (a peer down without
        deregistering simply drops out of ``self.siblings``). It also **self-heals** a peer we
        only know partially -- a roster seed ``{prefix, base_url}`` whose registration push raced
        our boot -- by pulling its ``/info`` once it is up, so the topnav gets its ``views``/icon
        even when the push/reciprocation never landed."""
        live = []
        for prefix, desc in list(self.registry.items()):
            base_url = desc.get("base_url")
            if not base_url:
                continue
            try:
                if requests.get(f"{base_url}/{prefix}/health", timeout=2).status_code != 200:
                    continue
            except requests.RequestException:
                continue
            if "views" not in desc:                          # partial -> pull the full descriptor
                desc = self._refresh_descriptor(prefix, base_url) or desc
            live.append(desc)
        self.siblings = live

    def _refresh_descriptor(self, prefix: str, base_url: str):
        """Pull a live peer's ``/info`` and merge its descriptor fields (name/icon/launch_cmd/views)
        into the registry, keeping our known-reachable ``base_url``. Returns the merged entry or
        ``None`` on failure. Idempotent: once ``views`` is present the poll stops re-fetching."""
        try:
            info = requests.get(f"{base_url}/{prefix}/info", timeout=2).json()
        except (requests.RequestException, ValueError):
            return None
        fields = {k: info[k] for k in ("name", "icon", "launch_cmd", "views") if k in info}
        if not fields:
            return None
        self.registry[prefix] = {**self.registry.get(prefix, {}), **fields}
        return self.registry[prefix]

    def _monitor_loop(self):
        freq = getattr(self.cfg, "monitor_frequency", 15)
        while True:
            self._poll_registry()
            time.sleep(freq)

    def run(self):
        threading.Thread(target=self._bootstrap, daemon=True).start()
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        self.app.run(host=self.cfg.bind, port=self.cfg.port, debug=getattr(self.cfg, "debug", False))


class SharedIndexMicroservice(DidipMicroservice):
    """A microservice that reduces the FSDB into an :class:`FSDBSharedIndex` at load and
    exposes it at ``/basket`` + ``/basket/db``. Subclasses build further indexes on top."""

    #: the basket UI syncs against those two routes, so every page of such a service gets it.
    HAS_SHARED_INDEX = True

    #: optional app-output filename overlay for the index's presence mask.
    filepattern = None
    #: which FSDBSharedIndex class to build; ``None`` -> the charter-only base. A service that
    #: also needs the per-image universe sets this to ``FSDBSharedImageIndex``.
    index_class = None

    def load(self):
        super().load()
        from fsdb.shared_index import FSDBSharedIndex
        index_class = self.index_class or FSDBSharedIndex
        # `workers` drives the process-parallel per-charter scan (see fsdb.iter_charter_scan);
        # it is a config field so every service exposes it as --workers.
        self.index = index_class.from_fsdb_root(self.cfg.fsdb_root, filepattern=self.filepattern,
                                                verbose=getattr(self.cfg, "verbosity", 0),
                                                workers=getattr(self.cfg, "workers", None))

    def register_routes(self):
        super().register_routes()
        from .sharedindex_routes import make_sharedindex_blueprint
        # mount /basket + /basket/db under this service's prefix (-> /<prefix>/basket)
        before = set(self.app.view_functions)
        self.app.register_blueprint(make_sharedindex_blueprint(self.index, started_at=self.started_at),
                                    url_prefix=f"/{self.route_prefix}")
        # these are the BASE's routes (registered here, after __init__ snapshotted the core set),
        # so exempt them from the @scoped_ms declaration rule and the scope-rejection guard.
        self._core_endpoints |= set(self.app.view_functions) - before

    def charter_path(self, key):
        """Absolute charter directory for a charter md5/position (via the index)."""
        return self.index.charter_path(key)

    # ---- scoped / global reductions ----------------------------------------------------
    def scoped_reduce(self, mask):
        """Summarise the slice restricted to ``mask`` (bool over the sorted charter universe).

        Overridable hook, **not abstract**: the default returns the generic counts that any shared
        index can produce (charters, and images when the index has them), so a service gets a
        useful answer before writing a line. Apps override it and call ``super()`` to keep these::

            def scoped_reduce(self, mask):
                out = super().scoped_reduce(mask)
                out["total_objects"] = ...      # app payload, computed from the SAME mask
                return out

        Rules that keep this fast enough to run per request (see the plan):
        - reduce over **compact numpy** (row arrays / masks); never materialise per-item records
          here -- ``global_reduce`` is this same call with an all-True mask, i.e. the whole DB;
        - materialise only the page a view actually renders.
        """
        m = np.asarray(mask, dtype=bool)
        out = {"n_charters_in_scope": int(m.sum()), "n_charters_total": int(len(self.index))}
        if hasattr(self.index, "image_to_charter_idx"):
            out["n_images_in_scope"] = int(m[self.index.image_to_charter_idx].sum())
            out["n_images_total"] = int(self.index.n_images)
        return out

    def global_reduce(self):
        """The whole-DB reduction: :meth:`scoped_reduce` with an all-True mask, **memoised**.

        Concrete on purpose -- deriving it from ``scoped_reduce`` means the scoped and unscoped
        numbers can never disagree, and memoising it keeps an unscoped landing page free (the
        answer is constant until the index is rebuilt).
        """
        cached = getattr(self, "_global_reduce_cache", None)
        if cached is None:
            cached = self.scoped_reduce(np.ones(len(self.index), dtype=bool))
            self._global_reduce_cache = cached
        return cached

    def reduce_for_request(self):
        """The reduction for the CURRENT request: scoped when a basket is active, else the
        memoised whole-DB one. The one call a scoped view needs."""
        return self.scoped_reduce(scope.charters) if scope.active else self.global_reduce()

    def health_report(self) -> dict:
        rep = super().health_report()
        rep["index_hash"] = self.index.index_hash
        rep["n_charters"] = len(self.index)
        return rep
