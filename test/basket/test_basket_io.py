"""Pytest wrapper that runs the Node basket export/restore driver (``basket_io_test.mjs``) against
the browser modules ``static/fsdb_basket_io.js`` + ``static/fsdb_basket.js``, then re-reads what the
JS produced with Python's own ``gzip``/``json`` -- so the exported file is proven to be a normal
gzip member, not merely something our own reader accepts. Skips cleanly when ``node`` is absent.

Same throwaway ``type: module`` dir trick as ``test_basket.py``: the modules import each other by
relative path, so all of them are copied together and Node treats ``.js`` there as ESM.
"""
import base64
import gzip
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "ddp_microservices" / "static"
DRIVER = Path(__file__).parent / "basket_io_test.mjs"
NODE = shutil.which("node")

MODULES = ("fsdb_sharedindex.js", "fsdb_sharedindex_store.js", "ddp_views.js", "ddp_busy.js",
           "fsdb_basket_io.js", "fsdb_basket.js")


def _module_dir(tmp_path):
    (tmp_path / "package.json").write_text('{"type":"module"}')
    for name in MODULES:
        shutil.copy(STATIC / name, tmp_path / name)
    return tmp_path


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_basket_io_node(tmp_path):
    d = _module_dir(tmp_path)
    r = subprocess.run([NODE, str(DRIVER), str(d / "fsdb_basket_io.js"), str(d / "fsdb_basket.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node driver failed:\n{r.stdout}\n{r.stderr}"
    assert "ok" in r.stdout


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_export_is_readable_gzip_json(tmp_path):
    """The JS export must be a plain gzip file that anything -- here Python -- can open."""
    d = _module_dir(tmp_path)
    script = """
import { exportBytes } from './fsdb_basket_io.js';
import { BasketStore } from './fsdb_basket.js';
const files = new Map();
const store = { async getFileHandle(n, o) {
  if (!files.has(n)) { if (o && o.create) files.set(n, new Uint8Array(0));
                       else { const e = new Error('nf'); e.name = 'NotFoundError'; throw e; } }
  return { async createWritable() { return { async write(x) { files.set(n, new Uint8Array(x.buffer || x)); },
                                             async close() {} }; },
           async getFile() { const u = files.get(n);
             return { size: u.length, async arrayBuffer() { return u.buffer; },
                      async text() { return new TextDecoder().decode(u); } }; } };
} };
const s = await BasketStore.open({ store });
await s.create('field trip');
await s.add('charters', 'a'.repeat(32));
await s.setMemo('unicode \\u2713');
process.stdout.write(Buffer.from(await exportBytes(s, { indexHash: 'abc123' })).toString('base64'));
"""
    (d / "emit.mjs").write_text(script)
    r = subprocess.run([NODE, str(d / "emit.mjs")], capture_output=True, text=True, cwd=d)
    assert r.returncode == 0, r.stderr

    doc = json.loads(gzip.decompress(base64.b64decode(r.stdout)))
    assert doc["format"] == "ddp-baskets" and doc["version"] == 1
    assert doc["index_hash"] == "abc123"
    assert "All" not in doc["baskets"], "the reserved basket must never be exported"
    assert doc["baskets"]["field trip"]["charters"] == ["a" * 32]
    assert doc["baskets"]["field trip"]["memo"] == "unicode ✓"
