"""Tests for the /basket serving routes and the client-side OPFS store.

The route *logic* (manifest_dict / db_payload) is pure and tested directly with numpy --
no Flask needed. The Flask blueprint is exercised only when Flask is installed. The client
store is driven through Node (fsdb_sharedindex_store.js) with a mocked OPFS + fetch.
"""
import gzip
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STORE_JS = ROOT / "src" / "ddp_microservices" / "static" / "fsdb_sharedindex_store.js"
CODEC_JS = ROOT / "src" / "ddp_microservices" / "static" / "fsdb_sharedindex.js"
STORE_TEST = Path(__file__).parent / "store_test.mjs"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


si = _load("shared_index", ROOT / "src" / "fsdb" / "shared_index.py")
routes = _load("sharedindex_routes", ROOT / "src" / "ddp_microservices" / "sharedindex_routes.py")
NODE = shutil.which("node")


def _make_index(tmp_path):
    import hashlib
    h = lambda s: hashlib.md5(s.encode()).hexdigest()
    layout = {"AT-DOZA": {h("f1"): [h("c1"), h("c2")]}, "IT-BSNSP": {h("f2"): [h("c3")]}}
    for archive, fonds in layout.items():
        for fond, charters in fonds.items():
            for charter in charters:
                (tmp_path / archive / fond / charter).mkdir(parents=True)
    return si.FSDBSharedIndex.from_fsdb_root(tmp_path)


# --- pure route logic (no Flask) -------------------------------------------------------

def test_manifest_dict(tmp_path):
    idx = _make_index(tmp_path)
    m = routes.manifest_dict(idx, uptime_s=1.5)
    assert m["index_hash"] == idx.index_hash
    assert m["counts"] == {"archives": 2, "fonds": 2, "charters": 3}
    assert m["fsdb_root"] == str(tmp_path)
    assert m["uptime_s"] == 1.5


def test_db_payload_roundtrip(tmp_path):
    idx = _make_index(tmp_path)
    status, headers, body = routes.db_payload(idx)
    assert status == 200
    assert headers["ETag"] == f'"{idx.index_hash}"'
    assert headers["Content-Type"] == "application/octet-stream"
    assert si.FSDBSharedIndex.from_db_bytes(body) == idx


def test_db_payload_if_none_match_304(tmp_path):
    idx = _make_index(tmp_path)
    status, headers, body = routes.db_payload(idx, if_none_match=f'"{idx.index_hash}"')
    assert status == 304
    assert body == b""
    # a non-matching tag still returns the body
    status2, _, body2 = routes.db_payload(idx, if_none_match='"stale"')
    assert status2 == 200 and body2


def test_db_payload_gzip(tmp_path):
    idx = _make_index(tmp_path)
    status, headers, body = routes.db_payload(idx, accept_encoding="gzip, deflate")
    assert status == 200
    assert headers["Content-Encoding"] == "gzip"
    assert si.FSDBSharedIndex.from_db_bytes(gzip.decompress(body)) == idx


# --- Flask blueprint (only if Flask is installed) --------------------------------------

def test_blueprint(tmp_path):
    flask = pytest.importorskip("flask")
    idx = _make_index(tmp_path)
    app = flask.Flask(__name__)
    app.register_blueprint(routes.make_sharedindex_blueprint(idx))
    client = app.test_client()

    r = client.get("/basket")
    assert r.status_code == 200
    assert r.get_json()["index_hash"] == idx.index_hash

    r = client.get("/basket/db")
    assert r.status_code == 200
    assert si.FSDBSharedIndex.from_db_bytes(r.data) == idx
    assert r.headers["ETag"] == f'"{idx.index_hash}"'

    r = client.get("/basket/db", headers={"If-None-Match": f'"{idx.index_hash}"'})
    assert r.status_code == 304


# --- client store (driven through Node) ------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_client_store_sync(tmp_path):
    store_mjs = tmp_path / "store.mjs"
    codec_mjs = tmp_path / "codec.mjs"
    shutil.copyfile(STORE_JS, store_mjs)
    shutil.copyfile(CODEC_JS, codec_mjs)
    r = subprocess.run([NODE, str(STORE_TEST), str(store_mjs), str(codec_mjs)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout
