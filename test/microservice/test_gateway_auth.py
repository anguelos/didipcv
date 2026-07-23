"""The gateway's manual password gate (``IpGate`` + ``/_ddp_auth``).

Beyond the happy path, these pin the properties that make the gate worth having: the two boot
endpoints stay reachable (or no microservice can ever start), ``X-Forwarded-For`` cannot forge a
whitelisted address, ``?next=`` cannot bounce an authenticated user off-site, and an empty password
leaves the gateway exactly as it was.
"""
import pytest

from ddp_microservices.gateway import IpGate, build_gateway_app

ROUTES = [{"prefix": "st", "url": "http://127.0.0.1:65001", "name": "Static", "launch_cmd": "x"}]
PW = "hunter2"


def client(password=PW, **kw):
    app = build_gateway_app(routes=list(ROUTES), ms_health_freq=0, password=password, **kw)
    app.config["TESTING"] = True
    return app.test_client()


def get(c, path, ip="203.0.113.7", **kw):
    """A request from a NON-local address, so nothing is exempted by being on the LAN."""
    return c.get(path, environ_overrides={"REMOTE_ADDR": ip}, **kw)


# ---- the gate itself ---------------------------------------------------------------------

def test_disabled_when_no_password():
    c = client(password="")
    assert get(c, "/").status_code == 200          # manifest served straight away
    assert get(c, "/roster").status_code == 200


def test_unknown_ip_is_redirected_to_the_form():
    c = client()
    r = get(c, "/")
    assert r.status_code == 303
    assert r.headers["Location"].startswith("/_ddp_auth?next=")


def test_correct_password_whitelists_the_ip_once():
    c = client()
    r = c.post("/_ddp_auth", data={"password": PW, "next": "/"},
               environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
    assert r.status_code == 303 and r.headers["Location"] == "/"
    # ...and is not asked again from that address
    assert get(c, "/").status_code == 200
    # a DIFFERENT address is still asked
    assert get(c, "/", ip="203.0.113.9").status_code == 303


def test_wrong_password_does_not_whitelist():
    c = client()
    r = c.post("/_ddp_auth", data={"password": "nope", "next": "/"},
               environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
    assert r.status_code == 401
    assert get(c, "/").status_code == 303


def test_forwarding_route_is_gated():
    """The whole point: a backend must not be reachable through the gateway before the password."""
    c = client()
    assert get(c, "/st/charter/abc").status_code == 303


def test_non_navigational_request_gets_401_not_a_redirect():
    c = client()
    r = c.post("/st/search", environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
    assert r.status_code == 401


# ---- the things that must NOT break ------------------------------------------------------

def test_boot_endpoints_stay_open_to_local_addresses():
    """A booting microservice BLOCKS on GET {proxy}/roster; gating it deadlocks the whole mesh."""
    c = client()
    for ip in ("127.0.0.1", "192.168.1.20", "10.0.0.5", "172.17.0.3"):   # incl. a docker bridge
        assert get(c, "/roster", ip=ip).status_code == 200, ip
        assert get(c, "/health", ip=ip).status_code == 200, ip


def test_boot_endpoints_are_not_open_to_the_world():
    c = client()
    assert get(c, "/roster").status_code == 303      # 203.0.113.7 is public
    assert get(c, "/health").status_code == 303


def test_local_addresses_still_need_the_password_for_everything_else():
    c = client()
    assert get(c, "/", ip="127.0.0.1").status_code == 303
    assert get(c, "/st/x", ip="192.168.1.20").status_code == 303


# ---- bypass attempts ---------------------------------------------------------------------

def test_x_forwarded_for_cannot_forge_a_whitelisted_address():
    c = client()
    c.post("/_ddp_auth", data={"password": PW, "next": "/"},
           environ_overrides={"REMOTE_ADDR": "127.0.0.1"})          # loopback is whitelisted
    # an outsider claiming to BE loopback must still be stopped
    r = get(c, "/", ip="203.0.113.7", headers={"X-Forwarded-For": "127.0.0.1"})
    assert r.status_code == 303


def test_next_cannot_be_an_open_redirect():
    c = client()
    for evil in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
        r = c.post("/_ddp_auth", data={"password": PW, "next": evil},
                   environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
        assert r.headers["Location"] == "/", evil
        c = client()      # fresh gateway: that address is now whitelisted


def test_auth_path_cannot_be_shadowed_by_a_service_prefix():
    """Prefixes match ^[a-z][a-z0-9_]*$, so no backend can own a path starting with '_'."""
    c = client()
    r = get(c, "/_ddp_auth")
    assert r.status_code == 200 and b"password" in r.data


# ---- trusted_proxy_cidr: reading the real client through a tunnel/proxy -------------------
#
# The deployment in mind is a `cloudflared` sidecar on the docker bridge: it dials out to
# Cloudflare, so every visitor reaches the gateway FROM the sidecar's address. Without
# trusted_proxy_cidr they all look like one client (and, worse, like a LOCAL one -- see the
# /roster test below). With it, the real address comes from CF-Connecting-IP.

SIDECAR = "172.18.0.5"          # the cloudflared container, on the compose bridge
VISITOR = "203.0.113.99"        # the actual person on the internet
CIDR = "172.16.0.0/12"


def tclient(**kw):
    return client(trusted_proxy_cidr=CIDR, **kw)


def via_proxy(c, path, visitor=VISITOR, header="CF-Connecting-IP", peer=SIDECAR, **kw):
    return c.get(path, environ_overrides={"REMOTE_ADDR": peer}, headers={header: visitor}, **kw)


def test_cf_connecting_ip_identifies_the_real_client():
    c = tclient()
    # the visitor authenticates once...
    r = c.post("/_ddp_auth", data={"password": PW, "next": "/"},
               environ_overrides={"REMOTE_ADDR": SIDECAR}, headers={"CF-Connecting-IP": VISITOR})
    assert r.status_code == 303
    assert VISITOR in c.application.ip_gate.whitelist        # the VISITOR, not the sidecar
    assert SIDECAR not in c.application.ip_gate.whitelist
    # ...and is remembered
    assert via_proxy(c, "/").status_code == 200
    # a DIFFERENT visitor through the SAME sidecar is still asked -- the whole point
    assert via_proxy(c, "/", visitor="198.51.100.4").status_code == 303


def test_forged_header_from_an_untrusted_peer_is_ignored():
    """The attack the plain remote_addr rule exists to stop: someone reaching the gateway directly
    and claiming, via a header, to be an address that is already whitelisted."""
    c = tclient()
    c.post("/_ddp_auth", data={"password": PW, "next": "/"},
           environ_overrides={"REMOTE_ADDR": SIDECAR}, headers={"CF-Connecting-IP": VISITOR})
    assert via_proxy(c, "/").status_code == 200               # genuine: through the sidecar
    # same claim, but arriving directly from the internet -> header ignored, still gated
    r = c.get("/", environ_overrides={"REMOTE_ADDR": "198.51.100.7"},
              headers={"CF-Connecting-IP": VISITOR, "X-Forwarded-For": VISITOR})
    assert r.status_code == 303


def test_boot_endpoints_are_not_exposed_through_a_tunnel():
    """Regression: /health + /roster are exempt for LOCAL addresses so services can boot. A tunnel
    sidecar is itself on a local bridge, so without resolving the real client the whole internet
    inherited that exemption -- and /roster discloses every backend's internal url."""
    leaky = client()                       # no trusted_proxy_cidr: sees only the sidecar's address
    assert leaky.get("/roster", environ_overrides={"REMOTE_ADDR": SIDECAR}).status_code == 200
    fixed = tclient()
    assert via_proxy(fixed, "/roster").status_code == 303
    assert via_proxy(fixed, "/health").status_code == 303
    # and a real container on the bridge (no forwarding headers) can still boot
    assert fixed.get("/roster", environ_overrides={"REMOTE_ADDR": "172.18.0.9"}).status_code == 200


def test_last_xff_entry_is_used_when_there_is_no_cf_header():
    """A non-Cloudflare proxy: the last entry is what our trusted neighbour appended; the first is
    whatever the client claimed, so it must never be the one we take."""
    c = tclient()
    r = c.post("/_ddp_auth", data={"password": PW, "next": "/"},
               environ_overrides={"REMOTE_ADDR": SIDECAR},
               headers={"X-Forwarded-For": f"10.9.9.9, {VISITOR}"})
    assert r.status_code == 303
    assert VISITOR in c.application.ip_gate.whitelist
    assert "10.9.9.9" not in c.application.ip_gate.whitelist   # the forgeable leftmost entry


def test_cf_header_wins_over_xff():
    c = tclient()
    c.post("/_ddp_auth", data={"password": PW, "next": "/"},
           environ_overrides={"REMOTE_ADDR": SIDECAR},
           headers={"CF-Connecting-IP": VISITOR, "X-Forwarded-For": "10.9.9.9"})
    assert VISITOR in c.application.ip_gate.whitelist


def test_junk_header_from_a_trusted_peer_falls_back_to_the_peer():
    """A trusted-but-broken proxy must not be able to put garbage into the whitelist."""
    c = tclient()
    c.post("/_ddp_auth", data={"password": PW, "next": "/"},
           environ_overrides={"REMOTE_ADDR": SIDECAR}, headers={"CF-Connecting-IP": "not-an-ip"})
    assert c.application.ip_gate.whitelist == {SIDECAR}


def test_untrusted_deployment_is_unchanged_by_default():
    """No trusted_proxy_cidr -> the headers are ignored exactly as before."""
    c = client()
    c.post("/_ddp_auth", data={"password": PW, "next": "/"},
           environ_overrides={"REMOTE_ADDR": SIDECAR}, headers={"CF-Connecting-IP": VISITOR})
    assert c.application.ip_gate.whitelist == {SIDECAR}


def test_malformed_cidr_fails_loudly_at_startup():
    with pytest.raises(ValueError):
        build_gateway_app(routes=list(ROUTES), password=PW, trusted_proxy_cidr="not-a-cidr")


def test_multiple_cidrs_and_bare_addresses_are_accepted():
    app = build_gateway_app(routes=list(ROUTES), password=PW,
                            trusted_proxy_cidr="172.16.0.0/12, 10.0.0.0/8 192.0.2.7")
    app.config["TESTING"] = True
    c = app.test_client()
    for peer in ("172.18.0.5", "10.1.2.3", "192.0.2.7"):
        c2 = app.test_client()
        r = c2.post("/_ddp_auth", data={"password": PW, "next": "/"},
                    environ_overrides={"REMOTE_ADDR": peer}, headers={"CF-Connecting-IP": VISITOR})
        assert r.status_code == 303, peer
    assert VISITOR in app.ip_gate.whitelist


# ---- brute-force brake -------------------------------------------------------------------

def test_lockout_after_repeated_failures():
    t = [1000.0]
    gate = IpGate(PW, max_attempts=3, lockout_s=60, now=lambda: t[0])
    for _ in range(3):
        assert gate.check("1.2.3.4", "wrong") is False
    assert gate.locked_for("1.2.3.4") == 60
    assert gate.check("1.2.3.4", PW) is False        # correct password refused while locked
    t[0] += 61
    assert gate.locked_for("1.2.3.4") == 0
    assert gate.check("1.2.3.4", PW) is True         # and works again afterwards
    assert gate.is_allowed("1.2.3.4")


def test_lockout_is_per_address():
    gate = IpGate(PW, max_attempts=2, lockout_s=60)
    gate.check("1.2.3.4", "wrong")
    gate.check("1.2.3.4", "wrong")
    assert gate.locked_for("1.2.3.4") > 0
    assert gate.locked_for("5.6.7.8") == 0


def test_success_clears_earlier_failures():
    gate = IpGate(PW, max_attempts=3, lockout_s=60)
    gate.check("1.2.3.4", "wrong")
    assert gate.check("1.2.3.4", PW) is True
    assert gate._fails.get("1.2.3.4") is None
