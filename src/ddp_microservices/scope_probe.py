"""``ddp_scope_probe`` -- exercise a scoped DiDip microservice's scope contract from the CLI.

A scoped service answers the SAME question three ways -- unscoped, scoped by ``?scope=`` (GET), and
scoped by a ``scope`` form field (POST) -- and the three must agree with each other and with the
contract (400 on a malformed basket, 409 on a stale index). This walks all of that and exits
non-zero when something disagrees, so it works as a smoke test in a Makefile or CI as well as by
hand.

It deliberately builds **id-list** wire baskets only::

    {"charter_ids": [...], "fond_ids": [...], "archive_ids": [...]}

Those are *literal*: :meth:`fsdb.shared_index.FSDBSharedIndex.receive_basket` enforces
``bit_vector_hash`` only for universe-relative baskets (``all_charters`` / ``bit_vector``), so a
probe never has to sync ``/basket/db`` first. That is what makes this a one-line CLI rather than a
client that has to mirror the browser's OPFS cache.

Examples::

    ddp_scope_probe --prefix=cei --route=/ --charters=02402a6d…,03ad66b6…
    ddp_scope_probe --prefix=sl --url=http://127.0.0.1:8895 --archives=IT-BSNSP --route=/download_basket
    ddp_scope_probe --prefix=cei --route=/filter --fonds=fff44d78… --extra='format=json&skip=0'
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass

import fargv
import requests

_SPLIT = ",; \t\n"


@dataclass
class ScopeProbeConfig:
    """Probe a scoped microservice route with an id-list basket."""

    url: str = "http://127.0.0.1:8895"
    "Base URL of the service, WITHOUT the route prefix."
    prefix: str = "cei"
    "The service's owned route prefix (cei, sl, sly, ly...)."
    route: str = "/"
    "Route under the prefix, e.g. / or /filter or /archive/IT-BSNSP."
    charters: str = ""
    "Charter md5s (comma/space separated)."
    fonds: str = ""
    "Fond md5s (comma/space separated)."
    archives: str = ""
    "Archive names (comma/space separated), e.g. IT-BSNSP."
    ids_file: str = ""
    "Optional file of identifiers, one per line; merged with the flags above (kind auto-split by shape)."
    extra: str = "format=json"
    "Extra query parameters, urlencoded, applied to every request."
    checks: bool = True
    "Also assert the error contract: malformed scope -> 400, stale index -> 409."
    timeout: float = 60.0
    "Per-request timeout in seconds."
    verbosity: int = fargv.FargvInt(0, short_name="v", is_count_switch=True)
    "-v: show the wire basket  -vv: show response bodies."


def _split(text: str) -> list[str]:
    for ch in _SPLIT[1:]:
        text = text.replace(ch, ",")
    return [t for t in text.split(",") if t]


def _load_ids_file(path: str) -> tuple[list[str], list[str]]:
    """Read an identifier file into (md5s, names). 32-hex tokens are md5s, everything else a name.

    Charter and fond md5s are indistinguishable by shape, so md5s are returned as one bucket for the
    caller to place -- only the server's index can classify them, and this probe does not hold it.
    """
    md5s, names = [], []
    for tok in _split(open(path).read()):
        (md5s if len(tok) == 32 and all(c in "0123456789abcdefABCDEF" for c in tok) else names).append(tok)
    return md5s, names


def _digest(resp) -> str:
    return hashlib.sha256(resp.content).hexdigest()[:12]


def _scope_note(resp):
    """The service's own scope report, when the route answers JSON with a ``scope`` block."""
    try:
        block = resp.json().get("scope")
    except ValueError:
        return None
    if not isinstance(block, dict):
        return None
    return f"active={block.get('active')} in_scope={block.get('in_scope')} total={block.get('total')}"


def main():
    """``ddp_scope_probe`` entry point: probe one route, print a table, exit non-zero on a mismatch."""
    cfg, _ = fargv.parse(ScopeProbeConfig)

    charters, fonds, archives = _split(cfg.charters), _split(cfg.fonds), _split(cfg.archives)
    if cfg.ids_file:
        md5s, names = _load_ids_file(cfg.ids_file)
        charters += md5s          # ambiguous md5s default to charters; use --fonds for fond ids
        archives += names
    if not (charters or fonds or archives):
        print("error: give at least one of --charters / --fonds / --archives / --ids_file", file=sys.stderr)
        return 2

    basket = {"charter_ids": charters, "fond_ids": fonds, "archive_ids": archives}
    wire = json.dumps(basket)
    base = f"{cfg.url.rstrip('/')}/{cfg.prefix.strip('/')}{cfg.route if cfg.route.startswith('/') else '/' + cfg.route}"
    extra = dict(p.split("=", 1) for p in cfg.extra.split("&") if "=" in p)

    if cfg.verbosity >= 1:
        print(f"target : {base}\nbasket : {wire}\n", file=sys.stderr)

    def get(params):
        return requests.get(base, params={**extra, **params}, timeout=cfg.timeout)

    results, failures = [], []
    try:
        unscoped = get({})
        scoped_get = get({"scope": wire})
        scoped_post = requests.post(base, params=extra, data={"scope": wire}, timeout=cfg.timeout)
    except requests.RequestException as e:
        print(f"error: cannot reach {base}: {e}", file=sys.stderr)
        return 2

    for name, resp in (("unscoped", unscoped), ("scoped GET", scoped_get), ("scoped POST", scoped_post)):
        results.append((name, resp))
        if cfg.verbosity >= 2:
            print(f"--- {name}\n{resp.text[:800]}\n", file=sys.stderr)

    print(f"{'request':<14} {'status':>6} {'bytes':>9}  {'sha256':<12} scope")
    for name, resp in results:
        note = _scope_note(resp) or ""
        print(f"{name:<14} {resp.status_code:>6} {len(resp.content):>9}  {_digest(resp):<12} {note}")

    # --- the contract ----------------------------------------------------------------------
    if scoped_get.status_code != 200:
        failures.append(f"scoped GET returned {scoped_get.status_code}, expected 200")
    if scoped_post.status_code != 200:
        failures.append(f"scoped POST returned {scoped_post.status_code}, expected 200")
    if scoped_get.status_code == scoped_post.status_code == 200 and _digest(scoped_get) != _digest(scoped_post):
        failures.append("GET ?scope= and POST form scope returned DIFFERENT bodies (they must agree)")
    if unscoped.status_code == 200 and _digest(unscoped) == _digest(scoped_get):
        failures.append("scoped answer is byte-identical to the unscoped one -- the route may be ignoring scope "
                        "(expected when the basket covers the whole slice)")

    if cfg.checks:
        malformed = get({"scope": "}{ not json"})
        stale = get({"scope": json.dumps({"bit_vector": [255], "bit_vector_hash": "deadbeef" * 8})})
        print(f"{'malformed':<14} {malformed.status_code:>6} {len(malformed.content):>9}  "
              f"{_digest(malformed):<12} expect 400")
        print(f"{'stale index':<14} {stale.status_code:>6} {len(stale.content):>9}  "
              f"{_digest(stale):<12} expect 409")
        if malformed.status_code != 400:
            failures.append(f"malformed scope returned {malformed.status_code}, expected 400")
        if stale.status_code != 409:
            failures.append(f"stale bit_vector_hash returned {stale.status_code}, expected 409")

    print()
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print("OK  scope contract satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
