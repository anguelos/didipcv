"""Pytest wrapper that runs the Node basket driver (``basket_test.mjs``) against the browser
module ``static/fsdb_basket.js``. Skips cleanly when ``node`` is absent.

``fsdb_basket.js`` statically imports ``./fsdb_sharedindex.js`` and ``./fsdb_sharedindex_store.js``,
so all three are copied into a throwaway ``type: module`` dir (Node treats ``.js`` there as
ESM) before the driver runs.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "ddp_microservices" / "static"
DRIVER = Path(__file__).parent / "basket_test.mjs"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_basket_model_node(tmp_path):
    (tmp_path / "package.json").write_text('{"type":"module"}')
    for name in ("fsdb_sharedindex.js", "fsdb_sharedindex_store.js", "ddp_views.js", "fsdb_basket.js"):
        shutil.copy(STATIC / name, tmp_path / name)
    r = subprocess.run([NODE, str(DRIVER), str(tmp_path / "fsdb_basket.js"),
                        str(tmp_path / "fsdb_sharedindex.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node driver failed:\n{r.stdout}\n{r.stderr}"
    assert "ok" in r.stdout
