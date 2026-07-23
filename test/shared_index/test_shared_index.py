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


# --- baskets: send_basket / receive_basket (compact wire transfer) ----------------------

def big_index(n=4096):
    """A larger index (default N=4096 -> density threshold N/1024 = 4 charters) so both the
    id-list and the bit-vector send paths are reachable. Charters round-robin over 3 fonds
    across 2 archives (fond_0 -> AR-A; fond_1, fond_2 -> AR-B)."""
    charter_id = np.array([f"{i:032x}" for i in range(n)], dtype="S32")  # already sorted
    fond_id = np.array(["fond_0", "fond_1", "fond_2"], dtype="S32")
    archive_id = np.array(["AR-A", "AR-B"], dtype="S32")
    charter_to_fond_idx = (np.arange(n) % 3).astype("<i4")
    fond_to_archive_idx = np.array([0, 1, 1], dtype="<i4")
    return si.FSDBSharedIndex(archive_id, fond_id, charter_id, charter_to_fond_idx, fond_to_archive_idx)


def test_basket_small_selection_is_id_list():
    idx = big_index()
    some = [idx.id_of(0), idx.id_of(10)]
    b = idx.send_basket(charter_ids=some)
    assert b["bit_vector"] is None and b["charter_ids"] == some     # small -> id list, verbatim
    mask = idx.receive_basket(b)
    assert mask.sum() == 2 and mask[0] and mask[10]
    assert idx.basket_charter_ids(b) == sorted(some)                # md5s in sorted order


def test_basket_large_selection_is_bit_vector():
    idx = big_index()
    b = idx.send_basket(fond_ids=["fond_0"])                        # ~1366 charters > 4
    assert b["bit_vector"] is not None and b["fond_ids"] == []
    assert b["bit_vector_hash"] == idx.index_hash
    direct = np.zeros(len(idx), bool)
    direct[idx.fond_to_charter_idx["fond_0"]] = True
    assert np.array_equal(idx.receive_basket(b), direct)            # round-trips exactly


def test_basket_bit_vector_base64_wire():
    import base64
    idx = big_index()
    b = idx.send_basket(fond_ids=["fond_0"])
    wire = dict(b, bit_vector=base64.b64encode(bytes(b["bit_vector"])).decode())  # HTTP JSON form
    assert np.array_equal(idx.receive_basket(wire), idx.receive_basket(b))


def test_basket_density_crossover():
    idx = big_index()                                              # threshold N/1024 = 4
    four = [idx.id_of(i) for i in range(4)]
    assert idx.send_basket(charter_ids=four)["bit_vector"] is None      # 4 is not > 4 -> id list
    five = [idx.id_of(i) for i in range(5)]
    assert idx.send_basket(charter_ids=five)["bit_vector"] is not None  # 5 > 4 -> bit vector


def test_basket_all_charters():
    idx = big_index(64)
    b = idx.send_basket(all_charters=True)
    assert b["all_charters"] and b["bit_vector"] is None and b["charter_ids"] == []
    assert idx.receive_basket(b).all()


def test_basket_mismatch_rules():
    idx = big_index()
    bv = idx.send_basket(fond_ids=["fond_0"])["bit_vector"]
    with pytest.raises(si.IndexMismatch):                           # stale hash + bit vector
        idx.receive_basket({"bit_vector": bv, "bit_vector_hash": "deadbeef"})
    with pytest.raises(si.IndexMismatch):                           # stale hash + all_charters
        idx.receive_basket({"all_charters": True, "bit_vector_hash": "deadbeef"})
    # a pure id-list basket is universe-independent (literal ids) -> tolerated on a stale hash
    m = idx.receive_basket({"charter_ids": [idx.id_of(1)], "bit_vector_hash": "deadbeef"})
    assert m.sum() == 1


def test_basket_unknown_ids_skipped():
    idx = big_index(64)
    assert idx.receive_basket({"charter_ids": ["z" * 32], "fond_ids": ["nope"]}).sum() == 0


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


def test_charter_relpath_and_path(tmp_path):
    layout = _sample_layout()
    _mk_fsdb(tmp_path, layout)
    idx = si.FSDBSharedIndex.from_fsdb_root(tmp_path)
    # every charter reconstructs to <archive>/<fond>/<charter>, matching the real tree
    for archive, fonds in layout.items():
        for fond, charters in fonds.items():
            for charter in charters:
                assert idx.charter_relpath(charter) == f"{archive}/{fond}/{charter}"
                path = idx.charter_path(charter)
                assert path == tmp_path / archive / fond / charter
                assert path.is_dir()                          # the reconstructed path exists
    # md5 and position keys are interchangeable
    assert idx.charter_relpath(0) == idx.charter_relpath(idx.id_of(0))
    with pytest.raises(KeyError):                             # unknown charter
        idx.charter_relpath("f" * 32)


def test_charter_path_requires_fsdb_root():
    idx = sample_index()                                     # built without fsdb_root
    assert idx.fsdb_root is None
    assert idx.charter_relpath(0)                            # relpath needs no root
    with pytest.raises(ValueError):                          # absolute path does
        idx.charter_path(0)


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


def test_from_fsdb_root_verbose_levels(tmp_path, capsys):
    _mk_fsdb(tmp_path, _sample_layout())            # 2 archives, 3 fonds, 5 charters

    si.FSDBSharedIndex.from_fsdb_root(tmp_path, verbose=0)   # silent
    out = capsys.readouterr()
    assert out.err == "" and out.out == ""

    si.FSDBSharedIndex.from_fsdb_root(tmp_path, verbose=1)   # start + end summary on stderr
    out = capsys.readouterr()
    assert out.out == ""                                     # never writes to stdout
    assert "scanning" in out.err
    assert "5 charters" in out.err and "3 fonds" in out.err and "2 archives" in out.err


def test_from_fsdb_root_verbose_progress_bar(tmp_path, capsys):
    _mk_fsdb(tmp_path, _sample_layout())
    si.FSDBSharedIndex.from_fsdb_root(tmp_path, verbose=2)   # adds a tqdm bar over the scan
    err = capsys.readouterr().err
    assert "5 charters" in err                               # the >=1 summary is still emitted
    assert "scanning archives" in err                        # the tqdm bar's description


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


# --- FSDBSharedImageIndex (per-image extension) ----------------------------------------

def _mk_fsdb_with_images(root, layout, images):
    """Build an FSDB tree and drop ``<imgmd5>.img.<ext>`` files into charters.

    ``images`` maps charter md5 -> list of ``(imgmd5, ext)``; charters absent from it get
    no image files."""
    _mk_fsdb(root, layout)
    for fonds in layout.values():
        for fond, charters in fonds.items():
            for charter in charters:
                archive = next(a for a, fs in layout.items() if fond in fs)
                for imgmd5, ext in images.get(charter, []):
                    (root / archive / fond / charter / f"{imgmd5}.img.{ext}").write_bytes(b"x")


def _image_layout():
    import hashlib
    h = lambda s: hashlib.md5(s.encode()).hexdigest()
    layout = _sample_layout()                                   # charters c1..c5
    charters = [h(f"c{i}") for i in range(1, 6)]
    images = {
        charters[0]: [(h("i1"), "jpg"), (h("i2"), "png")],      # c1: two images
        charters[2]: [(h("i3"), "jpg")],                        # c3: one image; c2/c4/c5: none
    }
    return layout, images, charters, {"i1": h("i1"), "i2": h("i2"), "i3": h("i3")}


def test_image_index_from_fsdb_root(tmp_path):
    layout, images, charters, im = _image_layout()
    _mk_fsdb_with_images(tmp_path, layout, images)
    idx = si.FSDBSharedImageIndex.from_fsdb_root(tmp_path)

    assert isinstance(idx, si.FSDBSharedIndex)                  # IS-A charter index
    assert len(idx) == 5 and idx.n_images == 3
    ids = [b.decode() for b in idx.image_id]
    assert ids == sorted(ids)                                   # image universe is sorted
    # ownership
    assert idx.image_charter(im["i1"]) == charters[0]
    assert idx.image_charter(im["i3"]) == charters[2]
    # image-universe hash is independent of the charter hash
    assert idx.image_index_hash and idx.image_index_hash != idx.index_hash


def test_image_index_relpath_and_path(tmp_path):
    layout, images, charters, im = _image_layout()
    _mk_fsdb_with_images(tmp_path, layout, images)
    idx = si.FSDBSharedImageIndex.from_fsdb_root(tmp_path)
    for imgmd5 in (im["i1"], im["i2"], im["i3"]):
        rel = idx.image_relpath(imgmd5)
        assert rel.endswith(f"/{imgmd5}.img." + rel.rsplit(".", 1)[1])
        path = idx.image_path(imgmd5)
        assert path == tmp_path / rel and path.is_file()        # reconstructed path exists
    # md5 and row keys are interchangeable
    assert idx.image_relpath(0) == idx.image_relpath(idx.image_id[0].decode())
    # extension is preserved (png vs jpg)
    assert idx.image_relpath(im["i2"]).endswith(".img.png")
    assert idx.image_relpath(im["i1"]).endswith(".img.jpg")


def test_image_index_charter_image_rows(tmp_path):
    layout, images, charters, im = _image_layout()
    _mk_fsdb_with_images(tmp_path, layout, images)
    idx = si.FSDBSharedImageIndex.from_fsdb_root(tmp_path)
    rows_c1 = idx.charter_image_rows(charters[0])
    assert sorted(idx.image_id[r].decode() for r in rows_c1) == sorted([im["i1"], im["i2"]])
    assert len(idx.charter_image_rows(charters[1])) == 0        # charter with no images
    # a basket of charters slices to the union of their image rows
    basket = [charters[0], charters[2]]
    rows = np.concatenate([idx.charter_image_rows(c) for c in basket])
    assert sorted(idx.image_id[r].decode() for r in rows) == sorted(im.values())


def test_image_index_unknown_keys(tmp_path):
    layout, images, _, _ = _image_layout()
    _mk_fsdb_with_images(tmp_path, layout, images)
    idx = si.FSDBSharedImageIndex.from_fsdb_root(tmp_path)
    assert idx.image_position_of("f" * 32) == -1 and not idx.has_image("f" * 32)
    with pytest.raises(KeyError):
        idx.image_path("f" * 32)
    with pytest.raises(KeyError):
        idx.charter_image_rows("f" * 32)                        # unknown charter, not empty


def test_image_index_no_images(tmp_path):
    _mk_fsdb(tmp_path, _sample_layout())                        # charters only, zero images
    idx = si.FSDBSharedImageIndex.from_fsdb_root(tmp_path)
    assert len(idx) == 5 and idx.n_images == 0
    assert idx.image_id.dtype.kind == "S"
    for c in range(len(idx)):
        assert len(idx.charter_image_rows(c)) == 0


def test_image_index_arrays_frozen(tmp_path):
    layout, images, _, _ = _image_layout()
    _mk_fsdb_with_images(tmp_path, layout, images)
    idx = si.FSDBSharedImageIndex.from_fsdb_root(tmp_path)
    assert idx.image_id.flags.writeable is False
    assert idx.image_to_charter_idx.flags.writeable is False
    assert idx.image_ext.flags.writeable is False
    assert all(v.flags.writeable is False for v in idx.charter_to_image_idx.values())


def test_image_index_no_wire_container():
    with pytest.raises(NotImplementedError):
        si.FSDBSharedImageIndex.from_db_bytes(b"whatever")


def test_image_index_relpath_glob_fallback(tmp_path):
    # image_ext=None -> extension recovered from the filesystem by a single glob
    layout, images, charters, im = _image_layout()
    _mk_fsdb_with_images(tmp_path, layout, images)
    full = si.FSDBSharedImageIndex.from_fsdb_root(tmp_path)
    idx = si.FSDBSharedImageIndex(
        full.archive_id, full.fond_id, full.charter_id, full.charter_to_fond_idx,
        full.fond_to_archive_idx, image_id=full.image_id,
        image_to_charter_idx=full.image_to_charter_idx, image_ext=None, fsdb_root=tmp_path)
    assert idx.image_ext is None
    assert idx.image_relpath(im["i2"]).endswith(".img.png")     # glob still finds the ext
    # without image_ext AND without fsdb_root, the extension cannot be recovered
    idx2 = si.FSDBSharedImageIndex(
        full.archive_id, full.fond_id, full.charter_id, full.charter_to_fond_idx,
        full.fond_to_archive_idx, image_id=full.image_id,
        image_to_charter_idx=full.image_to_charter_idx, image_ext=None)
    with pytest.raises(ValueError):
        idx2.image_relpath(im["i2"])


def test_image_index_verbose(tmp_path, capsys):
    layout, images, _, _ = _image_layout()
    _mk_fsdb_with_images(tmp_path, layout, images)

    si.FSDBSharedImageIndex.from_fsdb_root(tmp_path, verbose=0)
    out = capsys.readouterr()
    assert out.err == "" and out.out == ""

    si.FSDBSharedImageIndex.from_fsdb_root(tmp_path, verbose=1)
    err = capsys.readouterr().err
    assert "5 charters" in err and "3 images" in err and "2 archives" in err
