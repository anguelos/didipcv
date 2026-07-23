"""Basket-codec tests across the Python/JS boundary.

The JS `SharedIndex.sendBasket`/`receiveBasket` (static/fsdb_sharedindex.js) must agree with
`FSDBSharedIndex.send_basket`/`receive_basket` byte-for-byte: a bit vector packed by one side
decodes to the same charter set on the other. These tests run the Node driver
`codec_basket_test.mjs` and then decode its emitted wire baskets in Python. They skip cleanly
when `node` is absent. The pure-Python behaviour is covered in `test_shared_index.py`.
"""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
PY_MODULE_PATH = ROOT / "src" / "fsdb" / "shared_index.py"
JS_MODULE_PATH = ROOT / "src" / "ddp_microservices" / "static" / "fsdb_sharedindex.js"
DRIVER = Path(__file__).parent / "codec_basket_test.mjs"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def _load_py_module():
    spec = importlib.util.spec_from_file_location("shared_index", PY_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


si = _load_py_module()


def _py_index(n=4096):
    """The Python twin of the universe the Node driver builds (must match exactly)."""
    charter_id = np.array([f"{i:032x}" for i in range(n)], dtype="S32")
    fond_id = np.array(["fond_0", "fond_1", "fond_2"], dtype="S32")
    archive_id = np.array(["AR-A", "AR-B"], dtype="S32")
    c2f = (np.arange(n) % 3).astype("<i4")
    f2a = np.array([0, 1, 1], dtype="<i4")
    return si.FSDBSharedIndex(archive_id, fond_id, charter_id, c2f, f2a)


def _run_driver(tmp_path, out=None):
    mjs = tmp_path / "fsdb_sharedindex.mjs"
    mjs.write_text(JS_MODULE_PATH.read_text())
    (tmp_path / "package.json").write_text('{"type":"module"}')
    driver = tmp_path / "codec_basket_test.mjs"
    driver.write_text(DRIVER.read_text())
    cmd = [NODE, str(driver), str(mjs)] + ([str(out)] if out else [])
    return subprocess.run(cmd, capture_output=True, text=True, cwd=tmp_path)


@needs_node
def test_js_basket_codec_selfcheck(tmp_path):
    r = _run_driver(tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("ok")


@needs_node
def test_js_wire_baskets_decode_in_python(tmp_path):
    out = tmp_path / "wire.json"
    r = _run_driver(tmp_path, out)
    assert r.returncode == 0, r.stderr
    wire = json.loads(out.read_text())
    idx = _py_index(wire["N"])

    def positions(basket):
        return list(np.nonzero(idx.receive_basket(basket))[0])

    # Python decodes the JS-produced baskets to the SAME positions the JS side reported.
    assert positions(wire["small"]) == wire["small_positions"]
    assert positions(wire["fond0"]) == wire["fond0_positions"]
    # ...and the JS bit vector equals Python's own fond_0 selection (true byte-level parity).
    direct = list(np.nonzero(idx.receive_basket({"fond_ids": ["fond_0"]}))[0])
    assert positions(wire["fond0"]) == direct
