"""Single-origin dev-gateway for the DiDip microservices (a thin forwarding reverse proxy).

Each service OWNS its path prefix (routes are literally ``/st/...``, ``/sl/...``), so the gateway
is a pure PASS-THROUGH: it holds an explicit **roster** (``prefix -> base_url``) of the services it
fronts, picks the backend by the request's leading path segment, and forwards the request
**unchanged** (same path, no strip) to that backend's ``host:port``. The services are expected to
already be running on their own ports (``ddpa_static_fsdb_serve`` etc.).

The roster is the gateway's deployment config (``--roster roster.json``: a list of
``{prefix, base_url, name?, launch_cmd?}``) -- this is what makes decentralized, out-of-suite apps
(e.g. ``ddpa_layout``) routable without editing the core. With no roster file it falls back to
enumerating the ``DdpMsConfigs`` suite (dev convenience; misses out-of-suite apps). Booting services
poll ``GET /roster`` to learn their peers, then register peer-to-peer.

Two hard rules: the gateway **never forwards PUT** (so ``PUT /<prefix>/register`` is unreachable
through it -- registration is loopback/trusted-subnet only), and it knows nothing about a backend's
internal routes beyond its prefix.

This forwards in Python (via ``requests``, streamed), which is fine for local dev but not the best
data plane for large slice/IIIF downloads -- for Proxmox / multi-host, run a real proxy in front.
Because the services own their prefixes, that proxy is a dumb pass-through. Equivalent configs:

nginx (TLS terminates here; upstreams are plain HTTP on loopback; NOTE: no trailing URI on
``proxy_pass`` so the ``/st`` prefix is preserved -- a trailing ``/`` would strip it)::

    server {
        listen 443 ssl;
        server_name vre.example;
        # ... ssl_certificate / ssl_certificate_key ...
        location /st/ { proxy_pass http://127.0.0.1:5001; }   # Static  (ms_id 1 -> 5001)
        location /sl/ { proxy_pass http://127.0.0.1:5005; }   # Slicer  (ms_id 5 -> 5005)
        location = /  { return 302 /st/; }                    # or serve a manifest page
    }

Caddy (``reverse_proxy`` preserves the path by default -- no strip)::

    vre.example {
        handle /st/* { reverse_proxy 127.0.0.1:5001 }
        handle /sl/* { reverse_proxy 127.0.0.1:5005 }
        handle /     { respond "DiDip VRE" 200 }
    }

A real proxy must also replicate the two rules this gateway enforces: (1) refuse to forward
``PUT`` (e.g. nginx ``limit_except GET POST { deny all; }`` per location, or Caddy match on
``method PUT`` -> ``respond 405``) so ``/<prefix>/register`` stays internal-only, and (2) serve the
roster booting services poll -- either a static ``location /roster { return 200 '<json>'; }`` or by
pointing ``/roster`` at one service.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import re
import sys
import threading
import time

from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, redirect, request

from ddp_util.config_ms import DdpMsConfigs

#: response headers we must not copy verbatim (hop-by-hop, or invalid once we re-chunk).
_HOP_BY_HOP = {"content-encoding", "content-length", "transfer-encoding", "connection",
               "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "upgrade"}
#: methods the gateway will forward. PUT is DELIBERATELY absent: it is never proxied, so a
#: backend's loopback-only ``PUT /<prefix>/register`` cannot be reached through the gateway.
_FORWARD_METHODS = ["GET", "POST", "PATCH", "DELETE", "HEAD", "OPTIONS"]


#: reachable WITHOUT the password, and only from loopback / private addresses. `/roster` MUST stay
#: open: every microservice BLOCKS in a poll loop on ``GET {proxy}/roster`` until it answers, so
#: gating it deadlocks the whole mesh at boot. `/health` is the liveness probe of the same loop.
_OPEN_PATHS = frozenset({"/health", "/roster"})
#: where the password form lives. No backend can ever shadow it: a service prefix must match
#: ``^[a-z][a-z0-9_]*$``, so nothing owned by a backend can begin with an underscore.
_AUTH_PATH = "/_ddp_auth"

_warned_forwarded = False


def _parse_cidrs(spec: str) -> tuple:
    """Parse ``trusted_proxy_cidr``: comma/space separated CIDRs (or bare addresses). Invalid input
    raises HERE, at startup, rather than silently trusting nothing at request time."""
    nets = []
    for token in re.split(r"[,\s]+", (spec or "").strip()):
        if token:
            nets.append(ipaddress.ip_network(token, strict=False))   # ValueError on junk
    return tuple(nets)


def _in_nets(ip: str, nets) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def _client_ip(trusted_nets=()) -> str:
    """The client's address, used for the password whitelist.

    Two modes, and which one applies is decided by ``trusted_proxy_cidr`` alone:

    **Nothing configured (default).** The gateway is assumed to be the OUTERMOST hop, so the answer
    is ``remote_addr`` and the forwarding headers are IGNORED. That is not paranoia: honouring them
    unconditionally would let anybody whitelist themselves by sending one header, which is worse
    than having no gate at all. If something IS in front, every client arrives wearing that hop's
    address and one password admits everyone -- an ``X-Forwarded-For`` is the tell, so warn once.

    **``trusted_proxy_cidr`` set** (e.g. the docker network a ``cloudflared`` sidecar sits on).
    When the peer is inside it, and ONLY then, the real client is read from the headers:

    * ``CF-Connecting-IP`` first -- Cloudflare OVERWRITES it at its edge (a client-supplied value is
      discarded), so unlike XFF it is a single unambiguous address rather than an appendable list;
    * else the LAST ``X-Forwarded-For`` entry, i.e. the one our trusted neighbour appended, never
      the first -- the leftmost entry is whatever the client claimed. This assumes ONE trusted hop;
      a longer chain needs a hop count, not this.

    A peer outside the CIDR is still read from ``remote_addr``, so forging the header from the
    outside changes nothing. The value is validated as an IP before use: a trusted-but-broken proxy
    must not be able to inject junk into the whitelist.
    """
    peer = request.remote_addr or ""
    if trusted_nets and _in_nets(peer, trusted_nets):
        for header in ("CF-Connecting-IP", "X-Forwarded-For"):
            value = (request.headers.get(header) or "").strip()
            if not value:
                continue
            candidate = value.split(",")[-1].strip()   # last hop = the one our neighbour set
            if _is_ip(candidate):
                return candidate
        return peer          # trusted hop that forwarded nothing usable
    global _warned_forwarded
    if request.headers.get("X-Forwarded-For") and not _warned_forwarded:
        _warned_forwarded = True
        print("[ddp_gateway] WARNING: a request carried X-Forwarded-For, so this gateway is NOT the "
              "outermost hop. The password gate gives every client the SAME address "
              f"({peer}) -- one correct password admits all of them. Set --trusted_proxy_cidr to "
              "the network that hop sits on (see doc/docker_images.md), or put the gate in the "
              "front proxy instead.",
              file=sys.stderr, flush=True)
    return peer


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


#: the networks the backends can live on: RFC1918 + link-local (+ the v6 equivalents). Docker's
#: default bridges (172.17-172.31) fall inside 172.16/12, so containers qualify.
_LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "fc00::/7", "fe80::/10",
))


def _is_local(ip: str) -> bool:
    """Loopback or a private/link-local address -- the host and its own network.

    Spelled out rather than using ``ipaddress.is_private``, which is far broader than RFC1918: it
    also matches the IANA special-purpose ranges, including **CGNAT 100.64.0.0/10**. A client behind
    carrier-grade NAT is emphatically not on our LAN, and would otherwise be handed ``/roster`` --
    which discloses every backend's internal url -- without the password.
    """
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_loopback or any(a in net for net in _LOCAL_NETS)


def _safe_next(target: str) -> str:
    """Sanitise a ``?next=`` redirect target to a path on THIS origin.

    Without this the login form is an open redirector: ``/_ddp_auth?next=https://evil`` would bounce
    an authenticated user off-site. Only a single-slash-rooted path is accepted; anything else
    (absolute url, scheme-relative ``//host``, backslash tricks) falls back to the manifest.
    """
    if not target or not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return "/"
    return target


class IpGate:
    """Password -> client-IP whitelist for the gateway.

    One shared manual password; the first correct answer from an address adds that address to an
    **in-RAM** whitelist, so a restart re-asks everybody (deliberate: there is no state to age out
    or leak to disk). Empty password = disabled, which is what keeps every existing deployment,
    test and dev run working untouched.

    Thread-safe: the gateway runs ``threaded=True``, so the whitelist and the failure counters are
    touched concurrently.
    """

    def __init__(self, password: str = "", *, max_attempts: int = 10, lockout_s: int = 60,
                 now=time.time):
        self.password = password or ""
        self.max_attempts = max_attempts
        self.lockout_s = lockout_s
        self.now = now
        self.whitelist: set[str] = set()
        self._fails: dict[str, list] = {}      # ip -> [count, locked_until]
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def is_allowed(self, ip: str) -> bool:
        if not self.enabled:
            return True
        with self._lock:
            return ip in self.whitelist

    def locked_for(self, ip: str) -> int:
        """Seconds of lockout left for ``ip`` (0 = may try). Cheap brute-force brake: the password
        is one short human-typed string, so unlimited guessing over HTTP would fall quickly."""
        with self._lock:
            entry = self._fails.get(ip)
            if not entry:
                return 0
            return max(0, int(entry[1] - self.now()))

    def check(self, ip: str, supplied: str) -> bool:
        """Verify ``supplied`` for ``ip``; on success whitelist it and clear its failures."""
        if not self.enabled:
            return True
        if self.locked_for(ip):
            return False
        # constant-time even though the secret is plaintext: comparison time must not leak a prefix
        ok = hmac.compare_digest(self.password.encode(), (supplied or "").encode())
        with self._lock:
            if ok:
                self.whitelist.add(ip)
                self._fails.pop(ip, None)
            else:
                entry = self._fails.setdefault(ip, [0, 0.0])
                entry[0] += 1
                if entry[0] >= self.max_attempts:
                    entry[1] = self.now() + self.lockout_s
                    entry[0] = 0
        return ok


def _auth_page(next_path: str, message: str = "") -> str:
    """The password form. Self-contained (no static assets): it is served before any access is
    granted, so it must not depend on anything behind the gate."""
    note = f'<p class="err">{message}</p>' if message else ""
    return (
        "<!doctype html><meta charset='utf-8'><title>DiDip VRE</title>"
        "<style>body{font-family:system-ui,sans-serif;background:#d1eba3;color:#3e2723;"
        "display:flex;min-height:90vh;align-items:center;justify-content:center}"
        "form{background:#fffbe6;padding:1.5rem 2rem;border-radius:.5rem;box-shadow:0 2px 12px rgba(0,0,0,.18)}"
        "input{font:inherit;padding:.3rem .5rem;border:1px solid #8d6e63;border-radius:.3rem}"
        "button{font:inherit;padding:.3rem 1rem;margin-left:.4rem;border:1px solid #8d6e63;"
        "border-radius:.3rem;background:#8d6e63;color:#fffbe6;cursor:pointer}"
        ".err{color:#b3261e;margin:0 0 .6rem}small{opacity:.7;display:block;margin-top:.8rem}</style>"
        f"<form method='post' action='{_AUTH_PATH}'>"
        f"<h1>DiDip VRE</h1>{note}"
        f"<input type='hidden' name='next' value='{next_path}'>"
        "<input type='password' name='password' autofocus placeholder='password' "
        "autocomplete='current-password'>"
        "<button type='submit'>Enter</button>"
        "<small>Asked once per address; this machine is remembered until the gateway restarts.</small>"
        "</form>")


def _norm_entry(e: dict) -> dict:
    """Normalise one roster entry (accepts ``url`` or ``base_url``)."""
    prefix = e.get("prefix")
    return {"name": e.get("name") or prefix or "?", "prefix": prefix,
            "url": e.get("url") or e.get("base_url"), "launch_cmd": e.get("launch_cmd", "")}


def load_roster(roster_path: str = "") -> list[dict]:
    """The gateway's authoritative service roster as ``{name, prefix, url, launch_cmd}``.

    From an explicit deployment config when ``roster_path`` is given (a JSON list of
    ``{prefix, base_url|url, name?, launch_cmd?}``) -- this is how out-of-suite apps (layout, …)
    become routable without touching the core. With no path it falls back to enumerating the
    ``DdpMsConfigs`` suite (dev convenience only; will not include decentralized apps)."""
    if roster_path:
        with open(roster_path) as f:
            data = json.load(f)
        return [_norm_entry(e) for e in data if e.get("prefix") and (e.get("url") or e.get("base_url"))]
    routes = []
    for name, klass in vars(DdpMsConfigs).items():
        if name.startswith("Ms") and isinstance(klass, type):
            inst = klass()
            routes.append({"name": name, "prefix": inst.route_prefix,
                           "url": inst.url, "launch_cmd": inst.launch_cmd})
    return routes


def _probe_health(route) -> bool:
    """True iff the service answers 200 on ``/<prefix>/health``."""
    try:
        return requests.get(f"{route['url']}/{route['prefix']}/health", timeout=1.5).status_code == 200
    except requests.RequestException:
        return False


def service_report(route) -> dict:
    """Liveness comes from ``/<prefix>/health`` (always present); metrics from the OPTIONAL
    ``/<prefix>/health_report``. Returns ``up`` plus either the report fields, ``no_report``
    (alive but that route is absent -- e.g. an older build), or just the down stub."""
    base = {"service": route["name"], "route_prefix": route["prefix"], "url": route["url"],
            "launch_cmd": route["launch_cmd"]}
    if not _probe_health(route):
        return {**base, "up": False}
    try:
        rep = requests.get(f"{route['url']}/{route['prefix']}/health_report",
                           params={"format": "json"}, timeout=2).json()
        return {**rep, "up": True}
    except (requests.RequestException, ValueError):
        return {**base, "up": True, "no_report": True}   # alive, but no /health_report route


class HealthMonitor:
    """Optional background poller of every backend. Enabled only when ``freq > 0`` (poll + PRINT
    every ``freq`` seconds); otherwise dormant and the manifest probes each backend on demand.
    Liveness is from ``/health``, so a live service without ``/health_report`` still shows up."""

    def __init__(self, routes, freq: int):
        self.routes = routes
        self.freq = freq
        self.status = {r["prefix"]: None for r in routes}   # prefix -> True/False/None(unknown)

    def poll_and_print(self):
        print(f"[gateway health @ {time.strftime('%H:%M:%S')}]", file=sys.stderr, flush=True)
        for r in self.routes:
            rep = service_report(r)
            self.status[r["prefix"]] = rep["up"]
            if not rep["up"]:
                print(f"  {r['name']:<26} DOWN  launch: {r['launch_cmd']}", file=sys.stderr, flush=True)
            elif rep.get("no_report"):
                print(f"  {r['name']:<26} up    (no /health_report route — old build?)",
                      file=sys.stderr, flush=True)
            else:
                print(f"  {r['name']:<26} up    uptime={rep.get('uptime_s')}s  "
                      f"requests={rep.get('total_requests')}", file=sys.stderr, flush=True)

    def start(self):
        if self.freq <= 0:
            return
        def _loop():
            while True:
                self.poll_and_print()
                time.sleep(self.freq)
        threading.Thread(target=_loop, daemon=True).start()

    def status_of(self, route):
        """Cached status when the monitor is active, else a live on-demand probe. May be None
        (unknown) if the background monitor hasn't completed its first pass yet."""
        return self.status.get(route["prefix"]) if self.freq > 0 else _probe_health(route)


def build_gateway_app(routes=None, ms_health_freq: int = 0, password: str = "",
                      max_attempts: int = 10, lockout_s: int = 60,
                      trusted_proxy_cidr: str = "") -> Flask:
    """A Flask app that forwards ``/<prefix>/...`` to the owning backend and serves a manifest at
    ``/``. Backends must already be running; the gateway never constructs them. When
    ``ms_health_freq > 0`` a background thread probes every backend's health that often.

    ``password`` (empty = off) puts a one-off manual gate in front of everything except the two
    boot endpoints: an unknown address is sent to a form, and answering correctly whitelists that
    address for as long as the gateway runs. See :class:`IpGate`.

    ``trusted_proxy_cidr`` (empty = off) names the network a reverse proxy or tunnel sidecar sits
    on. Requests from inside it have their real client address read from ``CF-Connecting-IP`` /
    ``X-Forwarded-For``; everything else keeps using ``remote_addr``. See :func:`_client_ip` --
    without it, a fronted gateway sees every visitor as the same address."""
    routes = routes if routes is not None else load_roster()
    by_prefix = {r["prefix"]: r for r in routes}
    monitor = HealthMonitor(routes, ms_health_freq)
    monitor.start()
    started_at = time.time()
    trusted_nets = _parse_cidrs(trusted_proxy_cidr)   # raises on a malformed CIDR, at startup
    app = Flask("ddp_gateway", static_folder=None)
    app.health_monitor = monitor  # exposed for tests / introspection
    gate = IpGate(password, max_attempts=max_attempts, lockout_s=lockout_s)
    app.ip_gate = gate            # exposed for tests / introspection
    app.trusted_nets = trusted_nets

    @app.before_request
    def _require_password():
        """Gate every request. Order matters: an already-whitelisted address is the hot path and is
        checked first; the boot endpoints are exempt only from LOCAL addresses, so opening them up
        never widens the gate to the internet.

        Note the exemption is tested against the RESOLVED client address. Behind a tunnel sidecar
        that sits on the docker bridge, the peer address is itself 'local', so without
        ``trusted_proxy_cidr`` the public internet inherits the /health + /roster exemption."""
        if not gate.enabled or request.path == _AUTH_PATH:
            return None
        ip = _client_ip(trusted_nets)
        if gate.is_allowed(ip):
            return None
        if request.path in _OPEN_PATHS and _is_local(ip):
            return None
        if request.method in ("GET", "HEAD"):
            nxt = request.full_path.rstrip("?") or "/"
            return redirect(f"{_AUTH_PATH}?next={quote(nxt, safe='/?=&')}", code=303)
        # non-navigational (XHR, a service POSTing): no point redirecting to an html form
        return ("Unauthorized: open this gateway in a browser and enter the password first.", 401)

    @app.route(_AUTH_PATH, methods=["GET", "POST"])
    def _auth():
        """The password form, and the only route that can whitelist an address."""
        ip = _client_ip(trusted_nets)
        nxt = _safe_next(request.values.get("next", "/"))
        if not gate.enabled or gate.is_allowed(ip):
            return redirect(nxt, code=303)
        wait = gate.locked_for(ip)
        if wait:
            return _auth_page(nxt, f"Too many attempts — try again in {wait}s."), 429
        if request.method == "POST":
            if gate.check(ip, request.form.get("password", "")):
                print(f"[ddp_gateway] whitelisted {ip} ({len(gate.whitelist)} address(es) admitted)",
                      file=sys.stderr, flush=True)
                return redirect(nxt, code=303)
            return _auth_page(nxt, "Wrong password."), 401
        return _auth_page(nxt)

    @app.route("/health")
    def health():
        return "OK", 200

    @app.route("/roster")
    def roster():
        """The service roster a booting microservice polls to learn its peers (prefix + base_url).
        This is the authoritative, deployment-configured set of services behind the gateway."""
        return jsonify([{"prefix": r["prefix"], "base_url": r["url"]} for r in routes])

    @app.route("/")
    def manifest():
        items = []
        for r in routes:
            up = monitor.status_of(r)
            if up is True:
                status = "up"
            elif up is False:
                status = f'down — launch: <code>{r["launch_cmd"]}</code>'
            else:
                status = "unknown (awaiting first health probe)"
            items.append(f'<li><a href="/{r["prefix"]}/">{r["name"]}</a> '
                         f'({r["url"]}) — {status}</li>')
        return f"<h1>DiDip VRE</h1><ul>{''.join(items)}</ul>"

    @app.route("/health_report")
    def health_report():
        """The gateway's own uptime + each microservice's /health_report (aggregated).
        ``?format=json`` for JSON, else a simple HTML page."""
        reports = [service_report(r) for r in routes]
        payload = {"gateway_uptime_s": round(time.time() - started_at, 1), "services": reports}
        if request.args.get("format") == "json":
            return jsonify(payload)
        blocks = []
        for rep in reports:
            if not rep.get("up"):
                blocks.append(f'<h2>{rep["service"]}</h2><p>{rep["url"]} — DOWN, '
                              f'launch: <code>{rep["launch_cmd"]}</code></p>')
            elif rep.get("no_report"):
                blocks.append(f'<h2>{rep["service"]}</h2><p>{rep["url"]} — up, but no '
                              f'/health_report route (old build?)</p>')
            else:
                rows = "".join(f"<tr><td>{route}</td><td style='text-align:right'>{n}</td></tr>"
                               for route, n in sorted(rep.get("request_counts", {}).items()))
                blocks.append(
                    f'<h2>{rep["service"]}</h2>'
                    f'<p>{rep.get("url", "")} · uptime: {rep.get("uptime_s")} s · '
                    f'requests: {rep.get("total_requests")} · index: {rep.get("index_hash", "—")}</p>'
                    f"<table border='1' cellpadding='4'><tr><th>route</th><th>requests</th></tr>{rows}</table>")
        return (f"<h1>DiDip VRE gateway</h1><p>gateway uptime: "
                f"{payload['gateway_uptime_s']} s</p>{''.join(blocks)}")

    @app.route("/<prefix>/", defaults={"rest": ""}, methods=_FORWARD_METHODS)
    @app.route("/<prefix>/<path:rest>", methods=_FORWARD_METHODS)
    def forward(prefix, rest):
        # PUT is not in _FORWARD_METHODS, so Flask 405s it here -> /<prefix>/register (PUT) is
        # never reachable through the gateway (registration stays loopback/trusted-subnet only).
        r = by_prefix.get(prefix)
        if r is None:
            return f"Unknown service prefix {prefix!r}", 404
        # backend owns the prefix -> forward the path UNCHANGED (no strip).
        target = f"{r['url']}{request.path}"
        fwd_headers = {k: v for k, v in request.headers if k.lower() != "host"}
        fwd_headers["X-Forwarded-Host"] = request.host
        fwd_headers["X-Forwarded-Proto"] = request.scheme
        try:
            upstream = requests.request(
                request.method, target, params=request.args,
                data=request.get_data(), headers=fwd_headers,
                stream=True, allow_redirects=False, timeout=(5, None))
        except requests.RequestException as e:
            return (f"Bad gateway: {r['name']} unreachable at {r['url']} "
                    f"(launch: {r['launch_cmd']}) — {e}", 502)
        headers = [(k, v) for k, v in upstream.raw.headers.items()
                   if k.lower() not in _HOP_BY_HOP]
        return Response(upstream.iter_content(chunk_size=65536),
                        status=upstream.status_code, headers=headers)

    return app


def main():
    """``ddp_gateway`` entry point: forward one origin to the already-running services."""
    import fargv
    from werkzeug.serving import run_simple
    p, _ = fargv.parse({
        "bind": "0.0.0.0",
        "gateway_port": 8080,
        "roster": ("", "path to a JSON roster [{prefix, base_url, name?, launch_cmd?}]; "
                       "empty -> the DdpMsConfigs suite (dev only, misses out-of-suite apps)"),
        "ms_health_freq": (0, "probe every microservice's /health this often (seconds); <=0 disables"),
        "password": ("", "manual password gating this gateway; the first correct answer whitelists "
                         "the client's IP until restart. Empty -> no gate. Visible in `ps`, so "
                         "prefer the env var DDP_GATEWAY_PASSWORD"),
        "auth_max_attempts": (10, "wrong passwords from one address before it is locked out"),
        "auth_lockout_s": (60, "how long a locked-out address must wait (seconds)"),
        "trusted_proxy_cidr": ("", "network(s) a front proxy / tunnel sidecar sits on, comma "
                                   "separated (e.g. 172.16.0.0/12). Requests from inside it get "
                                   "their real client address from CF-Connecting-IP or the last "
                                   "X-Forwarded-For entry. Empty -> those headers are ignored and "
                                   "remote_addr is used. Set this ONLY for a hop you control"),
    })
    routes = load_roster(p.roster)
    app = build_gateway_app(routes=routes, ms_health_freq=p.ms_health_freq, password=p.password,
                            max_attempts=p.auth_max_attempts, lockout_s=p.auth_lockout_s,
                            trusted_proxy_cidr=p.trusted_proxy_cidr)
    mon = "off" if p.ms_health_freq <= 0 else f"every {p.ms_health_freq}s"
    src = f"roster {p.roster!r}" if p.roster else "DdpMsConfigs suite (dev fallback)"
    gate = ("password gate ON (/health + /roster stay open to local addresses)" if p.password
            else "password gate OFF (--password '…' to enable)")
    gate += (f"; trusting client headers from {p.trusted_proxy_cidr}" if p.trusted_proxy_cidr
             else "; no trusted proxy (CF-Connecting-IP / X-Forwarded-For ignored)")
    print(f"\n\nDiDip VRE gateway on http://{p.bind}:{p.gateway_port}/  "
          f"(services from {src}: {', '.join(r['prefix'] for r in routes)}; health monitor: {mon}; "
          f"{gate})\n",
          file=sys.stderr)
    run_simple(p.bind, p.gateway_port, app, threaded=True)


if __name__ == "__main__":
    main()
