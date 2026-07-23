"""Unit test for the own-the-prefix contract enforced by
``DidipMicroservice._assert_routes_prefixed`` (see the single-origin gateway design).

The base class is loaded standalone from source with a stub ``ddp_util.config_ms`` (so no fargv /
suite is needed), then exercised with a hand-built Flask app -- no server is started.
"""
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("flask", reason="flask not installed")
pytest.importorskip("requests", reason="requests not installed")
from flask import Flask

ROOT = Path(__file__).resolve().parents[2]
MS_PATH = ROOT / "src" / "ddp_microservices" / "microservice.py"


def _load_microservice_module():
    # microservice.py does `from ddp_util.config_ms import DdpMsConfigs` at import time; the
    # assertion under test never touches DdpMsConfigs, so a stub is enough (avoids fargv).
    pkg = sys.modules.setdefault("ddp_util", types.ModuleType("ddp_util"))
    cfg = types.ModuleType("ddp_util.config_ms")
    cfg.DdpMsConfigs = object
    pkg.config_ms = cfg
    sys.modules["ddp_util.config_ms"] = cfg
    spec = importlib.util.spec_from_file_location("ddp_microservice_under_test", MS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ms = _load_microservice_module()


def _service(prefix="st", launch="ddpa_x", glob_prefix=None, glob_launch=None):
    """A DidipMicroservice instance built WITHOUT __init__ (no config/load/monitor)."""
    svc = object.__new__(ms.DidipMicroservice)
    svc.cfg = types.SimpleNamespace(route_prefix=prefix, launch_cmd=launch, url="http://x")
    svc.GLOBAL_ROUTE_PREFIX = prefix if glob_prefix is None else glob_prefix
    svc.LAUNCH_CMD = launch if glob_launch is None else glob_launch
    svc.started_at = time.time()
    svc._request_counts = {}
    svc._counts_lock = threading.Lock()
    svc.registry = {}
    svc.siblings = []
    svc.app = Flask("t", static_folder=None)   # no default /static route to trip the assertion
    return svc


def test_all_prefixed_passes():
    svc = _service()
    svc.app.add_url_rule("/st/charter/<md5>", "charter", lambda md5: "x")
    svc.app.add_url_rule("/st/health", "health", lambda: "OK")
    svc.app.add_url_rule("/st/", "home", lambda: "home")   # bare prefix home is allowed
    svc._assert_routes_prefixed()  # no raise


def test_unprefixed_route_raises():
    svc = _service()
    svc.app.add_url_rule("/charter/<md5>", "charter", lambda md5: "x")  # missing /st
    with pytest.raises(AssertionError) as exc:
        svc._assert_routes_prefixed()
    # the error names the offending route and points at where it is defined
    assert "/charter/<md5>" in str(exc.value)
    assert "test_route_prefix.py" in str(exc.value)


def test_wrong_prefix_route_raises():
    svc = _service(prefix="st")
    svc.app.add_url_rule("/sl/charter/<md5>", "charter", lambda md5: "x")  # someone else's prefix
    with pytest.raises(AssertionError):
        svc._assert_routes_prefixed()


def test_prefix_drift_raises():
    svc = _service(prefix="st", glob_prefix="sl")   # class attr disagrees with config
    with pytest.raises(AssertionError):
        svc._assert_routes_prefixed()


def test_launch_cmd_drift_raises():
    svc = _service(launch="ddpa_x", glob_launch="ddpa_y")
    with pytest.raises(AssertionError):
        svc._assert_routes_prefixed()


def test_second_level_key_merges_families():
    assert ms._second_level_key("/st/iiif/abc/full/1000,") == "/st/iiif"
    assert ms._second_level_key("/st/static/ddp_style.css") == "/st/static"
    assert ms._second_level_key("/st/charter/abc") == "/st/charter"
    assert ms._second_level_key("/st/") == "/st/"
    assert ms._second_level_key("/st") == "/st"


def test_health_report_shape():
    svc = _service()
    svc._request_counts = {"/st/charter/<md5>": 3, "/st/health": 1}
    svc.siblings = [{"prefix": "sl"}]
    rep = svc.health_report()
    assert rep["service"] == "DidipMicroservice"
    assert rep["route_prefix"] == "st"
    assert rep["total_requests"] == 4
    assert rep["request_counts"]["/st/charter/<md5>"] == 3
    assert rep["siblings"] == ["sl"]
    assert isinstance(rep["uptime_s"], float)


# ---- decentralized discovery (registry + registration) --------------------------------

def _disc_service(prefix="st"):
    """A service with the extra attrs the discovery code reads (VIEWS, icon, url)."""
    svc = _service(prefix=prefix)
    svc.VIEWS = ("charter", "root")
    svc.cfg = types.SimpleNamespace(route_prefix=prefix, launch_cmd="ddpa_x",
                                    url="http://host:5000", icon="static/icon.svg")
    return svc


def test_descriptor_shape():
    svc = _disc_service("st")
    d = svc.descriptor()
    assert d["prefix"] == "st" and d["base_url"] == "http://host:5000"
    assert d["views"] == ["charter", "root"] and d["icon"] == "static/icon.svg"


def test_valid_descriptor():
    svc = _disc_service()
    assert svc._valid_descriptor({"prefix": "sl", "base_url": "http://h:5005"})
    assert not svc._valid_descriptor({"prefix": "Sl", "base_url": "http://h"})   # uppercase
    assert not svc._valid_descriptor({"prefix": "sl"})                          # no base_url
    assert not svc._valid_descriptor({"prefix": "1x", "base_url": "http://h"})  # starts with digit
    assert not svc._valid_descriptor("nope")


def test_register_allowed():
    svc = _disc_service()
    assert svc._register_allowed("127.0.0.1")
    assert svc._register_allowed("::1")
    assert not svc._register_allowed("10.1.2.3")
    assert not svc._register_allowed("garbage")
    svc.cfg.register_trusted_cidr = "10.0.0.0/8"
    assert svc._register_allowed("10.1.2.3")
    assert not svc._register_allowed("192.168.1.1")


def test_receive_registration_and_reciprocity():
    svc = _disc_service("st")
    calls, ev = [], threading.Event()
    svc._register_with = lambda base_url, prefix: (calls.append((base_url, prefix)), ev.set())
    # first registration of a new peer -> stored + reciprocated
    assert svc._receive_registration({"prefix": "sl", "base_url": "http://h:5005", "views": ["charter"]})
    assert ev.wait(1.0)
    assert calls == [("http://h:5005", "sl")]
    assert svc.registry["sl"]["views"] == ["charter"]
    # second registration of the SAME peer -> no re-reciprocation (idempotent)
    ev.clear(); calls.clear()
    assert svc._receive_registration({"prefix": "sl", "base_url": "http://h:5005"})
    assert not ev.wait(0.2)
    # our own prefix and invalid descriptors are ignored
    assert not svc._receive_registration({"prefix": "st", "base_url": "http://self"})
    assert not svc._receive_registration({"prefix": "BAD"})


def test_poll_registry_self_heals_partial(monkeypatch):
    """A roster-seeded PARTIAL entry (no views) is upgraded by pulling the peer's /info once it is
    up -- so a raced/failed registration push still converges (the topnav gets views/icon)."""
    svc = _disc_service("ly")
    svc.registry = {"st": {"prefix": "st", "base_url": "http://h:5001"}}   # partial: no views/icon
    svc.siblings = []

    class R:
        def __init__(self, code, payload=None):
            self.status_code, self._p = code, payload
        def json(self):
            return self._p

    def fake_get(url, timeout=None):
        if url.endswith("/st/health"):
            return R(200)
        if url.endswith("/st/info"):
            return R(200, {"prefix": "st", "base_url": "http://localhost:5001", "name": "Static",
                           "icon": "static/icon_static_1.svg", "launch_cmd": "ddpa_static_fsdb_serve",
                           "views": ["charter", "fond", "archive", "root"]})
        return R(404)
    monkeypatch.setattr(ms.requests, "get", fake_get)

    svc._poll_registry()
    st = svc.registry["st"]
    assert st["views"] == ["charter", "fond", "archive", "root"]   # healed
    assert st["icon"] == "static/icon_static_1.svg" and st["name"] == "Static"
    assert st["base_url"] == "http://h:5001"                       # kept our reachable base_url
    assert any(d["prefix"] == "st" and "views" in d for d in svc.siblings)


def test_flasgger_routes_exempt():
    svc = _service()
    svc.app.add_url_rule("/st/health", "health", lambda: "OK")
    svc.app.add_url_rule("/apispec_1.json", "flasgger.apispec_1", lambda: "{}")     # by endpoint
    svc.app.add_url_rule("/flasgger_static/x", "flasgger.static", lambda: "x")      # by endpoint
    svc.app.add_url_rule("/oauth2-redirect.html", "flasgger.oauth_redirect", lambda: "x")  # root path
    svc._assert_routes_prefixed()  # no raise
