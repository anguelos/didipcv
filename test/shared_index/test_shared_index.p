"""Cross-language tests for the shared-index binary container.

More than a unit test: beyond the pure-Python round trips, the ``*_cross_*`` tests drive
the JavaScript implementation (``static/fsdb_sharedindex.js``) through Node and assert the
two agree, including the full Python -> JS -> Python and JS -> Python -> JS round trips and
byte-for-byte format identity. The Node-dependent tests skip cleanly when ``node`` is absent.
"""
import importlib.util
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
PY_MODULE_PATH = ROOT / "src" / "fsdb" / "shared_index.py"
JS_MODULE_PATH = ROOT / "src" / "ddp_microservices" / "static" / "fsdb_sharedindex.js"
BRIDGE = Path(__file__).parent / "js_bridge.mjs"


def _load_py_module():
    spec = importlib.util.spec_from_file_location("shared_index", PY_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


si = _load_py_module()
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


# --- the neutral dataset both languages build blocks from ------------------------------

def sample_dataset():
    m = "0123456789abcdef"
    fond_ids = [(m[i % 16] * 32)[:32] for i in range(3)]
    charter_ids = [((m[(i * 7) % 16] + m[(i * 3) % 16]) * 16)[:32] for i in range(5)]
    return {
        "index_hash": "a" * 64,
        "meta": {"fsdb_root_basename": "mariapia", "counts": {"Na": 2, "Nf": 3, "Nc": 5}},
        "blocks": [
            {"name": "archive_id", "kind": "S", "width": 16, "values": ["AT-DOZA", "IT-BSNSP"]},
            {"name": "fond_id", "kind": "S", "width": 32, "values": fond_ids},
            {"name": "charter_id", "kind": "S", "width": 32, "values": charter_ids},
            {"name": "charter_to_fond_idx", "kind": "i4", "values": [0, 0, 1, 2, 2]},
            {"name": "fond_to_archive_idx", "kind": "i4", "values": [0, 1, 1]},
            {"name": "charter_to_archive_idx", "kind": "i4", "values": [0, 0, 1, 1, 1]},
        ],
    }


def dataset_to_blocks(ds):
    blocks = {}
    for b in ds["blocks"]:
        if b["kind"] == "S":
            blocks[b["name"]] = np.array(b["values"], dtype=f"S{b['width']}")
        else:
            blocks[b["name"]] = np.array(b["values"], dtype="<i4")
    return blocks


def blocks_to_dataset(info, blocks):
    out = []
    for name, arr in blocks.items():
        if arr.dtype.kind == "S":
            out.append({"name": name, "kind": "S", "width": int(arr.dtype.itemsize),
                        "values": [x.decode("ascii") for x in arr]})
        else:
            out.append({"name": name, "kind": "i4", "values": [int(x) for x in arr]})
    return {"index_hash": info["index_hash"], "meta": info["meta"], "blocks": out}


def run_node(module_mjs, cmd, in_path, out_path):
    subprocess.run([NODE, str(BRIDGE), str(module_mjs), cmd, str(in_path), str(out_path)],
                   check=True, capture_output=True, text=True)


@pytest.fixture()
def js_module(tmp_path):
    """A copy of the browser JS module with a .mjs extension so Node loads it as ESM."""
    dst = tmp_path / "fsdb_sharedindex.mjs"
    shutil.copyfile(JS_MODULE_PATH, dst)
    return dst


# --- pure-Python -----------------------------------------------------------------------

def test_python_roundtrip():
    ds = sample_dataset()
    blocks = dataset_to_blocks(ds)
    buf = si.SharedIndexContainer.serialize(blocks, index_hash=ds["index_hash"], meta=ds["meta"])
    info, back = si.SharedIndexContainer.deserialize(buf)
    assert blocks_to_dataset(info, back) == ds


def test_serialize_is_deterministic():
    blocks = dataset_to_blocks(sample_dataset())
    assert si.SharedIndexContainer.serialize(blocks) == si.SharedIndexContainer.serialize(blocks)


def test_int32_blocks_are_4_aligned():
    ds = sample_dataset()
    buf = si.SharedIndexContainer.serialize(dataset_to_blocks(ds), index_hash=ds["index_hash"], meta=ds["meta"])
    header_len = struct.unpack_from("<I", buf, 16)[0]
    header = json.loads(buf[20:20 + header_len])
    data_start = (20 + header_len + 3) & ~3
    for blk in header["blocks"]:
        if blk["kind"] == "i4":
            assert (data_start + blk["offset"]) % 4 == 0


def test_variable_width_nul_padding():
    # archive_id "AT-DOZA" (7 bytes) stored in S16 -> NUL padded, must decode back cleanly
    blocks = {"archive_id": np.array(["AT-DOZA", "IT-BSNSP"], dtype="S16")}
    _info, back = si.SharedIndexContainer.deserialize(si.SharedIndexContainer.serialize(blocks))
    assert [x.decode() for x in back["archive_id"]] == ["AT-DOZA", "IT-BSNSP"]


def test_bad_magic_rejected():
    with pytest.raises(ValueError):
        si.SharedIndexContainer.deserialize(b"NOTMAGIC" + b"\x00" * 32)


def test_sentinel_mismatch_rejected():
    buf = bytearray(si.SharedIndexContainer.serialize(dataset_to_blocks(sample_dataset())))
    struct.pack_into("<I", buf, 12, 0xDEADBEEF)  # corrupt the byte-order sentinel
    with pytest.raises(ValueError):
        si.SharedIndexContainer.deserialize(bytes(buf))


def test_larger_random_roundtrip():
    rng = np.random.default_rng(0)
    n = 4000
    charter_id = np.array([f"{rng.integers(16**8):08x}{i:024x}" for i in range(n)], dtype="S32")
    charter_to_fond_idx = rng.integers(0, 100, size=n).astype("<i4")
    blocks = {"charter_id": charter_id, "charter_to_fond_idx": charter_to_fond_idx}
    _info, back = si.SharedIndexContainer.deserialize(si.SharedIndexContainer.serialize(blocks))
    assert np.array_equal(back["charter_id"], charter_id)
    assert np.array_equal(back["charter_to_fond_idx"], charter_to_fond_idx)


# --- cross-language (Node) -------------------------------------------------------------

@needs_node
def test_cross_byte_identical(tmp_path, js_module):
    """Python and JS serialise the same dataset to byte-identical containers."""
    ds = sample_dataset()
    ds_json = tmp_path / "ds.json"
    ds_json.write_text(json.dumps(ds))
    js_bin = tmp_path / "js.bin"
    run_node(js_module, "serialize", ds_json, js_bin)
    py_bytes = si.SharedIndexContainer.serialize(dataset_to_blocks(ds), index_hash=ds["index_hash"], meta=ds["meta"])
    assert py_bytes == js_bin.read_bytes()


@needs_node
def test_cross_python_to_js_to_python(tmp_path, js_module):
    ds = sample_dataset()
    a = tmp_path / "a.bin"
    a.write_bytes(si.SharedIndexContainer.serialize(dataset_to_blocks(ds), index_hash=ds["index_hash"], meta=ds["meta"]))
    b = tmp_path / "b.bin"
    run_node(js_module, "reserialize", a, b)  # JS deserialize + re-serialize
    info, back = si.SharedIndexContainer.deserialize(b.read_bytes())
    assert blocks_to_dataset(info, back) == ds


@needs_node
def test_cross_js_to_python_to_js(tmp_path, js_module):
    ds = sample_dataset()
    ds_json = tmp_path / "ds.json"
    ds_json.write_text(json.dumps(ds))
    c = tmp_path / "c.bin"
    run_node(js_module, "serialize", ds_json, c)          # JS serialise
    d = tmp_path / "d.bin"
    info, back = si.SharedIndexContainer.deserialize(c.read_bytes())          # Python deserialise ...
    d.write_bytes(si.SharedIndexContainer.serialize(back, index_hash=info["index_hash"], meta=info["meta"]))  # ... + re-serialise
    out_json = tmp_path / "out.json"
    run_node(js_module, "todataset", d, out_json)          # JS deserialise back to a dataset
    assert json.loads(out_json.read_text()) == ds


# --- FSDBSharedIndex (canonical constructor + derived state) ----------------------------

def sample_index():
    """A tiny but consistent index: 2 archives, 3 fonds, 5 charters."""
    m = "0123456789abcdef"
    archive_id = np.array(["AT-DOZA", "IT-BSNSP"], dtype="S32")
    fond_id = np.array(sorted((m[i] * 32)[:32] for i in range(3)), dtype="S32")
    charter_id = np.array(sorted(((m[(i * 7) % 16] + m[(i * 3) % 16]) * 16)[:32] for i in range(5)), dtype="S32")
    charter_to_fond_idx = np.array([0, 0, 1, 2, 2], dtype="<i4")
    fond_to_archive_idx = np.array([0, 1, 1], dtype="<i4")
    return si.FSDBSharedIndex(archive_id, fond_id, charter_id, charter_to_fond_idx, fond_to_archive_idx)


def test_index_derives_reverse_and_hash():
    idx = sample_index()
    assert list(idx.charter_to_archive_idx) == [0, 0, 1, 1, 1]  # fond_to_archive[charter_to_fond]
    assert len(idx.index_hash) == 64
    assert idx.index_hash == sample_index().index_hash          # reproducible


def test_index_arrays_are_readonly():
    idx = sample_index()
    for a in (idx.archive_id, idx.fond_id, idx.charter_id, idx.charter_to_fond_idx,
              idx.fond_to_archive_idx, idx.charter_to_archive_idx):
        assert a.flags.writeable is False
    assert idx.archive_to_charter_idx["AT-DOZA"].flags.writeable is False


def test_index_forward_maps():
    idx = sample_index()
    assert list(idx.archive_to_charter_idx["AT-DOZA"]) == [0, 1]
    assert list(idx.archive_to_charter_idx["IT-BSNSP"]) == [2, 3, 4]
    assert list(idx.archive_to_fond_idx["IT-BSNSP"]) == [1, 2]


def test_index_lookups():
    idx = sample_index()
    assert len(idx) == 5
    md5 = idx.id_of(3)
    assert idx.position_of(md5) == 3
    assert md5 in idx
    assert idx.position_of("f" * 32) == -1


def test_index_db_roundtrip():
    idx = sample_index()
    rebuilt = si.FSDBSharedIndex.from_db_bytes(idx.to_db_bytes())
    assert rebuilt == idx                                        # same index_hash
    assert np.array_equal(rebuilt.charter_id, idx.charter_id)
    assert np.array_equal(rebuilt.charter_to_archive_idx, idx.charter_to_archive_idx)


def test_index_hash_mismatch_detected():
    idx = sample_index()
    blocks = {name: getattr(idx, name) for name in si.FSDBSharedIndex.DB_BLOCKS}
    bad = si.SharedIndexContainer.serialize(blocks, index_hash="deadbeef" * 8)  # header claims wrong hash
    with pytest.raises(si.IndexMismatch):
        si.FSDBSharedIndex.from_db_bytes(bad)


# --- FSDBSharedIndex.from_fsdb_root (filesystem walk) ----------------------------------

def _mk_fsdb(root, layout):
    """Create an FSDB directory tree (names only; from_fsdb_root reads no files)."""
    for archive, fonds in layout.items():
        for fond, charters in fonds.items():
            for charter in charters:
                (root / archive / fond / charter).mkdir(parents=True)


def _sample_layout():
    import hashlib
    h = lambda s: hashlib.md5(s.encode()).hexdigest()
    return {
        "AT-DOZA": {h("f1"): [h("c1"), h("c2")]},
        "IT-BSNSP": {h("f2"): [h("c3")], h("f3"): [h("c4"), h("c5")]},
    }


def test_from_fsdb_root(tmp_path):
    layout = _sample_layout()
    _mk_fsdb(tmp_path, layout)
    idx = si.FSDBSharedIndex.from_fsdb_root(tmp_path)

    assert len(idx) == 5
    assert [a.decode() for a in idx.archive_id] == ["AT-DOZA", "IT-BSNSP"]
    assert len(idx.fond_id) == 3
    # every charter resolves to the archive that actually contains it
    for archive, fonds in layout.items():
        a_idx = [a.decode() for a in idx.archive_id].index(archive)
        for charters in fonds.values():
            for charter in charters:
                pos = idx.position_of(charter)
                assert pos >= 0
                assert idx.charter_to_archive_idx[pos] == a_idx
    # and it round-trips through the wire container unchanged
    assert si.FSDBSharedIndex.from_db_bytes(idx.to_db_bytes()) == idx


def test_from_fsdb_root_threaded_matches_serial(tmp_path):
    _mk_fsdb(tmp_path, _sample_layout())
    a = si.FSDBSharedIndex.from_fsdb_root(tmp_path, n_workers=1)
    b = si.FSDBSharedIndex.from_fsdb_root(tmp_path, n_workers=4)
    assert a == b
    assert np.array_equal(a.charter_to_fond_idx, b.charter_to_fond_idx)
    assert np.array_equal(a.fond_to_archive_idx, b.fond_to_archive_idx)


def test_from_fsdb_root_ignores_noise(tmp_path):
    layout = _sample_layout()
    _mk_fsdb(tmp_path, layout)
    (tmp_path / ".git").mkdir()                       # not an archive (leading dot)
    (tmp_path / "README.md").write_text("x")          # a file, not a dir
    (tmp_path / "AT-DOZA" / "not-an-md5").mkdir()      # fond dir that isn't 32-hex
    (tmp_path / "lowercase").mkdir()                   # archive must start uppercase
    idx = si.FSDBSharedIndex.from_fsdb_root(tmp_path)
    assert len(idx) == 5
    assert len(idx.fond_id) == 3


def test_from_fsdb_root_empty_raises(tmp_path):
    with pytest.raises(ValueError):
        si.FSDBSharedIndex.from_fsdb_root(tmp_path)


def test_from_fsdb_root_presence(tmp_path):
    import hashlib
    h = lambda s: hashlib.md5(s.encode()).hexdigest()
    layout = _sample_layout()
    _mk_fsdb(tmp_path, layout)
    have = {h("c1"), h("c4")}                       # give 2 of 5 charters the app output
    for archive, fonds in layout.items():
        for fond, charters in fonds.items():
            for charter in charters:
                if charter in have:
                    (tmp_path / archive / fond / charter / "CH.layout.pred.json").write_text("{}")

    idx = si.FSDBSharedIndex.from_fsdb_root(tmp_path, filepattern="CH.layout.pred.json")
    assert idx.filepattern == "CH.layout.pred.json"
    assert idx.presence_mask.dtype == np.bool_
    assert len(idx.presence_mask) == len(idx)
    assert idx.presence_mask.flags.writeable is False
    present_ids = {idx.id_of(p) for p in np.where(idx.presence_mask)[0]}
    assert present_ids == have


def test_from_fsdb_root_no_filepattern_has_no_presence(tmp_path):
    _mk_fsdb(tmp_path, _sample_layout())
    idx = si.FSDBSharedIndex.from_fsdb_root(tmp_path)
    assert idx.filepattern is None
    assert idx.presence_mask is None


def test_presence_mask_pairing_validation():
    idx = sample_index()
    arrs = (idx.archive_id, idx.fond_id, idx.charter_id, idx.charter_to_fond_idx, idx.fond_to_archive_idx)
    with pytest.raises(ValueError):                                        # pattern without mask
        si.FSDBSharedIndex(*arrs, filepattern="x")
    with pytest.raises(ValueError):                                        # mask without pattern
        si.FSDBSharedIndex(*arrs, presence_mask=np.ones(len(idx), bool))
    with pytest.raises(TypeError):                                         # wrong dtype
        si.FSDBSharedIndex(*arrs, filepattern="x", presence_mask=np.ones(len(idx), np.int8))
    with pytest.raises(TypeError):                                         # wrong length
        si.FSDBSharedIndex(*arrs, filepattern="x", presence_mask=np.ones(len(idx) + 1, bool))
