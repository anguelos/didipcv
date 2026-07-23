"""Shared FSDB index + its little-endian binary wire container.

An :class:`FSDBSharedIndex` is a load-time reduction of an FSDB slice into a single,
sorted, reproducible namespace of charter md5s (plus the archive/fond hierarchy), so a
Python service and a JavaScript client agree on the same ``index_hash`` and the same
md5 <-> position mapping. Subsets of charters (baskets / slices) then travel between
services, or between a service and the browser, as compact position/bit payloads instead
of md5 strings.

The **wire container** that ships the index to the client (``GET /basket/db``) is
:class:`SharedIndexContainer`. Its JavaScript mirror is
``ddp_microservices/static/fsdb_sharedindex.js``; the two must stay byte-for-byte
compatible -- the cross-language round-trip is exercised by ``test/shared_index/``.

LITTLE-ENDIAN ONLY. The format is defined solely for little-endian platforms (every
browser / WASM target and mainstream CPU is LE). We refuse to import on a big-endian
interpreter rather than silently emit a payload a LE-only client would misread.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from tqdm import tqdm

# --- little-endian enforcement (load-time) ---------------------------------------------
if sys.byteorder != "little":
    raise RuntimeError(
        f"fsdb.shared_index requires a little-endian platform (sys.byteorder={sys.byteorder!r}); "
        "the shared-index wire format is little-endian only."
    )

# A basket that flattens to more than this fraction of the universe travels as a packed bit
# vector (1 bit/charter) instead of a 32-byte md5 list -- the bit vector is ~256x denser, so
# the crossover is well before 1/256; 1/1024 keeps id-lists only for genuinely small baskets.
# (User-set 2026-07-11; a candidate for the client config later -- see config-constants-policy.)
BITVECTOR_DENSITY_THRESHOLD = 1 / 1024


class SharedIndexContainer:
    """The little-endian binary wire format for the shared index (``GET /basket/db``).

    Mirrored byte-for-byte by ``ddp_microservices/static/fsdb_sharedindex.js``. Layout::

        off 0  : magic   b"FSDBIDX\\0"              (8 bytes)
        off 8  : version u32-LE                      (currently 1)
        off 12 : sentinel u32-LE = 0x01020304        (byte-order marker; 04 03 02 01 on wire)
        off 16 : header_len u32-LE                   (= H)
        off 20 : header    H bytes, compact UTF-8 JSON:
                 {"index_hash", "meta", "blocks":[{"name","kind","width","count","offset","nbytes"}...]}
        <pad to 4-byte boundary>  -> data_start = align4(20 + H)
        block data, each block at data_start + block["offset"] (offsets are 4-aligned)

    Block kinds: ``"S"`` = fixed-width NUL-padded byte string (e.g. S32 hex md5s) and
    ``"i4"`` = little-endian int32. The container is *uncompressed*; gzip is a transport
    concern applied at the HTTP layer (native ``CompressionStream`` on the client).
    """

    MAGIC = b"FSDBIDX\x00"          # 8 bytes
    VERSION = 1
    SENTINEL = 0x01020304           # LE byte-order marker: reads 04 03 02 01 on the wire
    HEADER_FIXED = 20               # magic(8) + version(4) + sentinel(4) + header_len(4)

    @staticmethod
    def _align4(n: int) -> int:
        """Round ``n`` up to a multiple of 4 (so int32 blocks are aligned for JS
        zero-copy ``Int32Array`` views)."""
        return (n + 3) & ~3

    @classmethod
    def serialize(cls, blocks: dict[str, np.ndarray], index_hash: str = "",
                  meta: dict | None = None) -> bytes:
        """Serialise insertion-ordered named numpy arrays into the container.

        Fixed-width byte-string arrays (dtype kind ``'S'``) are stored verbatim; integer
        arrays are stored as ``<i4``. Returns the raw (uncompressed) container bytes.
        """
        meta = {} if meta is None else meta
        descriptors: list[dict] = []
        payloads: list[bytes] = []
        rel = 0
        for name, arr in blocks.items():
            arr = np.asarray(arr)
            if arr.ndim != 1:
                raise ValueError(f"block {name!r} must be 1-D")
            if arr.dtype.kind == "S":
                kind, width = "S", int(arr.dtype.itemsize)
                data = np.ascontiguousarray(arr).tobytes()
            elif arr.dtype.kind in ("i", "u"):
                kind, width = "i4", 4
                data = arr.astype("<i4", copy=False).tobytes()
            else:
                raise TypeError(f"block {name!r}: unsupported dtype {arr.dtype!r} (need 'S' or integer)")
            rel = cls._align4(rel)
            descriptors.append({"name": name, "kind": kind, "width": width,
                                "count": int(arr.shape[0]), "offset": rel, "nbytes": len(data)})
            payloads.append(data)
            rel += len(data)

        header = {"index_hash": index_hash, "meta": meta, "blocks": descriptors}
        header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
        data_start = cls._align4(cls.HEADER_FIXED + len(header_bytes))
        buf = bytearray(data_start + rel)
        buf[0:8] = cls.MAGIC
        struct.pack_into("<III", buf, 8, cls.VERSION, cls.SENTINEL, len(header_bytes))
        buf[cls.HEADER_FIXED:cls.HEADER_FIXED + len(header_bytes)] = header_bytes
        for desc, data in zip(descriptors, payloads):
            off = data_start + desc["offset"]
            buf[off:off + len(data)] = data
        return bytes(buf)

    @classmethod
    def deserialize(cls, buf) -> tuple[dict, dict[str, np.ndarray]]:
        """Inverse of :meth:`serialize`. Returns ``(info, blocks)`` where
        ``info = {"index_hash", "meta"}`` and ``blocks`` maps name -> a writable numpy
        array. Raises ``ValueError`` on bad magic or a byte-order sentinel mismatch."""
        b = bytes(buf)
        if b[0:8] != cls.MAGIC:
            raise ValueError("shared-index: bad magic (not an FSDBIDX container)")
        _version, sentinel, header_len = struct.unpack_from("<III", b, 8)
        if sentinel != cls.SENTINEL:
            raise ValueError("shared-index: byte-order sentinel mismatch (payload is not little-endian)")
        header = json.loads(b[cls.HEADER_FIXED:cls.HEADER_FIXED + header_len].decode("utf-8"))
        data_start = cls._align4(cls.HEADER_FIXED + header_len)
        blocks: dict[str, np.ndarray] = {}
        for desc in header["blocks"]:
            off = data_start + desc["offset"]
            if desc["kind"] == "S":
                arr = np.frombuffer(b, dtype=f"S{desc['width']}", count=desc["count"], offset=off)
            elif desc["kind"] == "i4":
                arr = np.frombuffer(b, dtype="<i4", count=desc["count"], offset=off)
            else:
                raise ValueError(f"shared-index: unknown block kind {desc['kind']!r}")
            blocks[desc["name"]] = arr.copy()
        return {"index_hash": header.get("index_hash", ""), "meta": header.get("meta", {})}, blocks


class IndexMismatch(Exception):
    """A wire basket / rebuilt index does not match the expected index_hash (carry the
    current hash so an online service can answer HTTP 409 and the client re-syncs)."""

    def __init__(self, expected: str, got: str) -> None:
        super().__init__(f"index_hash mismatch: expected {expected!r}, got {got!r}")
        self.expected = expected
        self.got = got


def _freeze(*arrays: np.ndarray) -> None:
    """Mark numpy arrays read-only in place (immutability with no copy)."""
    for a in arrays:
        a.setflags(write=False)


#: default worker count for the per-charter scan: one PROCESS per core. The work (scandir + regex,
#: and in subclasses JSON parsing) is CPU-bound and GIL-serialised, so threads plateau ~1.4x while
#: processes reach ~4-7x; measured to peak at ``cpu_count`` and degrade beyond it.
def _default_workers() -> int:
    return os.cpu_count() or 4


def iter_charter_scan(base, worker, *, workers=None, verbose=0, desc="scanning charters"):
    """Map ``worker`` over EVERY charter directory of ``base``, in parallel, yielding its results
    **in charter-position order** (so callers stay identical to a serial scan).

    This is the one shared driver for "phase 2" of every FSDB reduce: the charter namespace is
    built once by the (cheap, sequential) name crawl, and then the expensive per-charter work --
    opening the directory, reading app files -- is fanned out over processes. Subclasses supply
    their own ``worker`` (see :meth:`FSDBSharedImageIndex._scan_images` for the image one, or
    ``ddp_layout``'s layout+prediction one) instead of re-implementing the pool.

    ``worker`` MUST be a module-level (importable, picklable) callable taking one ``(pos,
    charter_dir)`` tuple -- a closure or a local function cannot cross a process boundary. It
    receives a plain path so it needs nothing from ``base``, and must not touch shared state; the
    PARENT aggregates whatever it returns.

    ``workers=None`` -> one process per core; ``workers<=1`` runs serially in-process (no pool, no
    pickling), which is faster for small slices and simplifies debugging.
    """
    if base.fsdb_root is None:
        raise ValueError("charter scan requires the index to know its fsdb_root")
    root = str(base.fsdb_root)
    n = len(base)
    workers = workers or _default_workers()
    tasks = ((pos, os.path.join(root, base.charter_relpath(pos))) for pos in range(n))

    if workers <= 1:
        results = map(worker, tasks)
        if verbose >= 2:
            results = tqdm(results, total=n, unit="charter", desc=desc, file=sys.stderr)
        yield from results
        return

    # chunksize amortises per-task IPC/pickling over many charters.
    chunksize = max(16, min(512, n // (workers * 8) or 1))
    with ProcessPoolExecutor(max_workers=workers) as ex:
        results = ex.map(worker, tasks, chunksize=chunksize)
        if verbose >= 2:
            results = tqdm(results, total=n, unit="charter", desc=desc, file=sys.stderr)
        yield from results


def _scan_charter_images(task):
    """Per-charter worker for the image layer: ``(pos, charter_dir)`` -> ``[(imgmd5, pos, ext), ...]``.

    Module-level so a process pool can pickle it by reference (see :func:`iter_charter_scan`)."""
    pos, cdir = task
    out = []
    try:
        with os.scandir(cdir) as it:
            for e in it:
                m = FSDBSharedImageIndex._IMG_RE.match(e.name)
                if m and e.is_file():
                    out.append((m.group(1), pos, m.group(2)))
    except OSError:
        pass
    return out


class FSDBSharedIndex:
    """Sorted, hashed archive/fond/charter namespace over an FSDB slice.

    The canonical ``__init__`` requires the full *independent* state -- the three sorted
    id arrays and the two fundamental reverse maps -- and **takes ownership of them**,
    freezing them read-only (no copy). Everything else is derived and also frozen:

    - ``archive_id`` / ``fond_id`` / ``charter_id`` : sorted id arrays (``S32``/``S``;
      ids are ``str`` at the API boundary), len Na / Nf / Nc
    - ``charter_to_fond_idx`` / ``fond_to_archive_idx`` : dense ``int32`` reverse maps (given)
    - ``charter_to_archive_idx`` : dense ``int32`` reverse map (derived)
    - ``archive_to_fond_idx`` / ``fond_to_charter_idx`` / ``archive_to_charter_idx`` :
      forward maps ``dict[str, int32[]]`` (derived; shipped as CSR only if ever needed)
    - ``index_hash`` : ``sha256("\\n".join(charter_id))`` (derived; reproducible by any holder)
    - ``filepattern`` / ``presence_mask`` : optional overlay -- a charter-relative filename
      (an app output, e.g. ``CH.layout.pred.json``) and a bool array (len Nc, aligned to
      ``charter_id``) marking which charters carry it. Given together or not at all; charters
      missing the file are ``False``.

    id <-> idx uses ``np.searchsorted`` over the sorted id arrays. Build one from the
    filesystem with :meth:`from_fsdb_root` or from a wire container with
    :meth:`from_db_bytes`; the plain constructor is for callers that already hold the arrays.
    """

    # The independent blocks shipped over the wire; the rest is derived on the client.
    DB_BLOCKS = ("archive_id", "fond_id", "charter_id",
                 "charter_to_fond_idx", "fond_to_archive_idx")

    # FSDB directory-name patterns: archive dir vs 32-hex md5 (fond / charter) dir.
    _MD5_RE = re.compile(r"^[0-9a-f]{32}$")
    _ARCHIVE_RE = re.compile(r"^[A-Z][A-Za-z0-9-]*$")

    def __init__(self, archive_id: np.ndarray, fond_id: np.ndarray, charter_id: np.ndarray,
                 charter_to_fond_idx: np.ndarray, fond_to_archive_idx: np.ndarray,
                 *, filepattern: str | None = None, presence_mask: np.ndarray | None = None,
                 fsdb_root: str | Path | None = None) -> None:
        for name, arr, kind in (("archive_id", archive_id, "S"), ("fond_id", fond_id, "S"),
                                ("charter_id", charter_id, "S"),
                                ("charter_to_fond_idx", charter_to_fond_idx, "i"),
                                ("fond_to_archive_idx", fond_to_archive_idx, "i")):
            if arr.ndim != 1 or arr.dtype.kind != kind:
                raise TypeError(f"{name}: expected 1-D {kind!r}-kind array, got {arr.ndim}-D {arr.dtype!r}")
        # filepattern and its per-charter presence mask are paired: both or neither.
        if (filepattern is None) != (presence_mask is None):
            raise ValueError("filepattern and presence_mask must be given together (or neither)")
        if presence_mask is not None:
            presence_mask = np.asarray(presence_mask)
            if presence_mask.dtype != np.bool_ or presence_mask.ndim != 1 or len(presence_mask) != len(charter_id):
                raise TypeError("presence_mask must be a 1-D bool array aligned to charter_id")
        self.archive_id = archive_id
        self.fond_id = fond_id
        self.charter_id = charter_id
        self.charter_to_fond_idx = charter_to_fond_idx.astype("<i4", copy=False)
        self.fond_to_archive_idx = fond_to_archive_idx.astype("<i4", copy=False)
        self.filepattern = filepattern
        self.presence_mask = presence_mask
        self.fsdb_root = Path(fsdb_root) if fsdb_root is not None else None

        # --- derive the dependent state ---
        self.charter_to_archive_idx = self.fond_to_archive_idx[self.charter_to_fond_idx].astype("<i4")
        self.archive_to_fond_idx = self._forward(self.fond_to_archive_idx, self.archive_id)
        self.fond_to_charter_idx = self._forward(self.charter_to_fond_idx, self.fond_id)
        self.archive_to_charter_idx = self._forward(self.charter_to_archive_idx, self.archive_id)
        self.index_hash = hashlib.sha256(b"\n".join(self.charter_id.tolist())).hexdigest()

        # --- freeze everything (immutability, no copy) ---
        _freeze(self.archive_id, self.fond_id, self.charter_id, self.charter_to_fond_idx,
                self.fond_to_archive_idx, self.charter_to_archive_idx)
        for m in (self.archive_to_fond_idx, self.fond_to_charter_idx, self.archive_to_charter_idx):
            _freeze(*m.values())
        if self.presence_mask is not None:
            _freeze(self.presence_mask)

    @staticmethod
    def _forward(child_to_parent: np.ndarray, parent_id: np.ndarray) -> dict[str, np.ndarray]:
        """Invert a dense reverse map into ``{parent_id_str: sorted child idx int32[]}``."""
        n_parents = len(parent_id)
        order = np.argsort(child_to_parent, kind="stable").astype("<i4")
        counts = np.bincount(child_to_parent, minlength=n_parents)
        groups = np.split(order, np.cumsum(counts)[:-1])
        return {parent_id[p].decode("ascii"): groups[p] for p in range(n_parents)}

    # ---- alternative constructors ------------------------------------------------------
    @classmethod
    def from_db_bytes(cls, buf) -> "FSDBSharedIndex":
        """Rebuild a client-side index from a ``/basket/db`` container (no filesystem).
        Verifies the recomputed index_hash against the one in the payload."""
        info, blocks = SharedIndexContainer.deserialize(buf)
        self = cls(blocks["archive_id"], blocks["fond_id"], blocks["charter_id"],
                   blocks["charter_to_fond_idx"], blocks["fond_to_archive_idx"])
        if info["index_hash"] and info["index_hash"] != self.index_hash:
            raise IndexMismatch(info["index_hash"], self.index_hash)
        return self

    @staticmethod
    def _derive_charter_arrays(charter_md5s: list[str], charter_fonds: list[str],
                               charter_archives: list[str]):
        """Vectorised sort + id/idx derivation shared by every ``from_fsdb_root``.

        Returns ``(archive_id, fond_id, charter_id, charter_to_fond_idx, fond_to_archive_idx,
        order)`` where ``order`` is the stable argsort that put the charters in sorted-md5
        order (callers reuse it to reorder any per-charter overlay, e.g. the presence mask)."""
        md5_arr = np.array(charter_md5s, dtype="S32")
        fond_arr = np.array(charter_fonds, dtype="S32")
        arch_arr = np.array(charter_archives, dtype="S")
        order = np.argsort(md5_arr, kind="stable")
        charter_id = np.ascontiguousarray(md5_arr[order])

        fond_id, charter_to_fond_idx = np.unique(fond_arr[order], return_inverse=True)
        archive_id, charter_to_archive = np.unique(arch_arr[order], return_inverse=True)
        charter_to_fond_idx = charter_to_fond_idx.reshape(-1).astype("<i4")
        charter_to_archive = charter_to_archive.reshape(-1).astype("<i4")

        fond_to_archive_idx = np.empty(len(fond_id), dtype="<i4")
        fond_to_archive_idx[charter_to_fond_idx] = charter_to_archive  # every fond's archive
        return archive_id, fond_id, charter_id, charter_to_fond_idx, fond_to_archive_idx, order

    @classmethod
    def from_fsdb_root(cls, fsdb_root: str | Path, *, filepattern: str | None = None,
                       verbose: int = 0, workers=None) -> "FSDBSharedIndex":
        """Build by walking an FSDB slice, then delegate to the canonical constructor.

        A single ``os.scandir`` pass over the fixed 3-level layout
        (``fsdb_root/<archive>/<32hex fond>/<32hex charter>``), directory names only -- no
        ``Charter`` objects, no file reads. Arrays are derived vectorised.

        ``workers`` is accepted for signature compatibility with the subclasses/callers that
        parallelise their per-charter pass, and IGNORED here: this crawl never opens a charter
        directory and measured ~3% of a full build (0.13s of 5.3s on 12k charters), so a pool
        would cost more than it saves.

        ``verbose`` is an integer verbosity level (fargv convention); all output goes to
        stderr. ``0`` is silent; ``>= 1`` prints a start line and an end line (elapsed time
        plus the charter / fond / archive counts scanned); ``>= 2`` additionally shows a
        ``tqdm`` progress bar over the archives.
        """
        root = str(fsdb_root)
        md5_re = cls._MD5_RE
        t0 = time.time()
        if verbose >= 1:
            print(f"[FSDBSharedIndex] scanning {root!r} ...", file=sys.stderr, flush=True)

        with os.scandir(root) as ait:
            archives = [(a.path, a.name) for a in ait if a.is_dir() and cls._ARCHIVE_RE.match(a.name)]
        if verbose >= 2:
            archives = tqdm(archives, unit="archive", desc="[FSDBSharedIndex] scanning archives", file=sys.stderr)

        charter_md5s: list[str] = []
        charter_fonds: list[str] = []
        charter_archives: list[str] = []
        charter_present: list[bool] = []
        for archive_path, archive_name in archives:
            try:
                with os.scandir(archive_path) as fit:
                    for f in fit:
                        if not (f.is_dir() and md5_re.match(f.name)):
                            continue
                        with os.scandir(f.path) as cit:
                            for c in cit:
                                if c.is_dir() and md5_re.match(c.name):
                                    charter_md5s.append(c.name)
                                    charter_fonds.append(f.name)
                                    charter_archives.append(archive_name)
                                    if filepattern is not None:
                                        charter_present.append(os.path.exists(os.path.join(c.path, filepattern)))
            except OSError:
                pass

        if not charter_md5s:
            raise ValueError(f"no charters found under {root!r}")

        (archive_id, fond_id, charter_id, charter_to_fond_idx,
         fond_to_archive_idx, order) = cls._derive_charter_arrays(charter_md5s, charter_fonds, charter_archives)

        # presence overlay: reorder the per-charter file-exists flags into sorted order
        presence_mask = np.array(charter_present, dtype=bool)[order] if filepattern is not None else None

        if verbose >= 1:
            print(f"[FSDBSharedIndex] scanned {len(charter_id)} charters, {len(fond_id)} fonds, "
                  f"{len(archive_id)} archives in {time.time() - t0:.2f}s", file=sys.stderr, flush=True)

        return cls(archive_id, fond_id, charter_id, charter_to_fond_idx, fond_to_archive_idx,
                   filepattern=filepattern, presence_mask=presence_mask, fsdb_root=fsdb_root)

    # ---- serialisation -----------------------------------------------------------------
    def to_db_bytes(self, meta: dict | None = None) -> bytes:
        """Serialise the independent state to the ``/basket/db`` container."""
        blocks = {name: getattr(self, name) for name in self.DB_BLOCKS}
        return SharedIndexContainer.serialize(blocks, index_hash=self.index_hash, meta=meta or {})

    # ---- namespace / lookups -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self.charter_id)

    def position_of(self, md5: str) -> int:
        """Charter md5 -> position in the sorted universe, or -1 if absent."""
        key = md5.encode("ascii")
        i = int(np.searchsorted(self.charter_id, key))
        return i if i < len(self.charter_id) and self.charter_id[i] == key else -1

    def id_of(self, position: int) -> str:
        """Position -> charter md5."""
        return self.charter_id[position].decode("ascii")

    def _position(self, key) -> int:
        """Coerce a charter md5 (str/bytes) or an int position into a validated position."""
        if isinstance(key, (int, np.integer)):
            pos = int(key)
        else:
            md5 = key.decode("ascii") if isinstance(key, (bytes, bytearray)) else key
            pos = self.position_of(md5)
        if not 0 <= pos < len(self.charter_id):
            raise KeyError(f"charter {key!r} not in index")
        return pos

    def charter_relpath(self, key) -> str:
        """Charter md5 (or position) -> ``<archive>/<fond_md5>/<charter_md5>``, the charter
        directory relative to the FSDB root, reconstructed from the hierarchy arrays alone
        (no filesystem access)."""
        pos = self._position(key)
        archive = self.archive_id[self.charter_to_archive_idx[pos]].decode("ascii")
        fond = self.fond_id[self.charter_to_fond_idx[pos]].decode("ascii")
        charter = self.charter_id[pos].decode("ascii")
        return f"{archive}/{fond}/{charter}"

    def charter_path(self, key) -> Path:
        """Absolute charter directory for a charter md5/position. Requires the index to
        know its ``fsdb_root`` (set by :meth:`from_fsdb_root`)."""
        if self.fsdb_root is None:
            raise ValueError("charter_path requires fsdb_root (build the index via from_fsdb_root)")
        return self.fsdb_root / self.charter_relpath(key)

    def __contains__(self, md5: str) -> bool:
        return self.position_of(md5) >= 0

    def __eq__(self, other) -> bool:
        return isinstance(other, FSDBSharedIndex) and self.index_hash == other.index_hash

    def __hash__(self) -> int:
        return int(self.index_hash[:16], 16)

    # ---- baskets: compact wire transfer of a charter subset ----------------------------
    #
    # A basket travels as a dict (the same shape on the JS side):
    #     {"all_charters": bool,
    #      "charter_ids": [md5...], "fond_ids": [md5...], "archive_ids": [id...],
    #      "bit_vector": packed-uint8 | base64-str | None,   # over the sorted charter universe
    #      "bit_vector_hash": index_hash the basket references}
    # ``send_basket`` encodes one; ``receive_basket`` decodes one into a bool mask over the
    # sorted charter universe. The two are symmetric (send -> receive round-trips), and the
    # JS ``SharedIndex.sendBasket``/``receiveBasket`` mirror them.

    def send_basket(self, *, all_charters: bool = False, charter_ids=(), fond_ids=(),
                    archive_ids=()) -> dict:
        """Encode a charter selection into a wire basket, choosing the denser representation.

        The selection flattens (fonds/archives -> their charters) only to *decide* density:
        if the flattened charter count exceeds ``BITVECTOR_DENSITY_THRESHOLD`` of the universe
        a packed bit vector is emitted (id lists cleared); otherwise the id lists travel as-is
        (fonds/archives stay compact). ``bit_vector_hash`` is always this index's hash."""
        N = len(self)
        if all_charters:
            return {"all_charters": True, "charter_ids": [], "fond_ids": [], "archive_ids": [],
                    "bit_vector": None, "bit_vector_hash": self.index_hash}
        charter_ids, fond_ids, archive_ids = list(charter_ids), list(fond_ids), list(archive_ids)
        mask = self._selection_mask(charter_ids, fond_ids, archive_ids)
        if int(mask.sum()) > N * BITVECTOR_DENSITY_THRESHOLD:
            return {"all_charters": False, "charter_ids": [], "fond_ids": [], "archive_ids": [],
                    "bit_vector": np.packbits(mask), "bit_vector_hash": self.index_hash}
        return {"all_charters": False, "charter_ids": charter_ids, "fond_ids": fond_ids,
                "archive_ids": archive_ids, "bit_vector": None, "bit_vector_hash": self.index_hash}

    def receive_basket(self, basket: dict) -> np.ndarray:
        """Decode a wire basket into a bool mask (length ``len(self)``) over the sorted charter
        universe, ready to index any per-charter array.

        Raises :class:`IndexMismatch` when the basket references a *different* universe
        (``bit_vector_hash`` set and unequal) **and** it uses a universe-relative feature
        (``all_charters`` or ``bit_vector``); pure id-list baskets are literal and tolerated."""
        N = len(self)
        all_charters = bool(basket.get("all_charters"))
        bv = basket.get("bit_vector")
        bv_hash = basket.get("bit_vector_hash") or ""
        if bv_hash and bv_hash != self.index_hash and (all_charters or bv is not None):
            raise IndexMismatch(bv_hash, self.index_hash)
        if all_charters:
            return np.ones(N, dtype=bool)
        if bv is not None:
            if isinstance(bv, str):
                bv = np.frombuffer(base64.b64decode(bv), dtype=np.uint8)
            else:
                bv = np.asarray(bv, dtype=np.uint8)
            return np.unpackbits(bv)[:N].astype(bool)
        return self._selection_mask(basket.get("charter_ids", ()), basket.get("fond_ids", ()),
                                    basket.get("archive_ids", ()))

    def basket_charter_ids(self, basket: dict) -> list[str]:
        """Decode a wire basket straight to the list of selected charter md5s (sorted order)."""
        return [m.decode("ascii") for m in self.charter_id[self.receive_basket(basket)].tolist()]

    def flatten(self, selection) -> dict:
        """Expand ANY selection to an explicit flat charter set:
        ``{archive_ids: [], fond_ids: [], charter_ids: [every selected md5]}``. ``selection`` is a
        bool mask over the charter universe, or a wire basket dict (fonds/archives are resolved to
        their charters). Inverse of :meth:`unflatten`."""
        mask = self.receive_basket(selection) if isinstance(selection, dict) else np.asarray(selection, dtype=bool)
        return {"archive_ids": [], "fond_ids": [],
                "charter_ids": [m.decode("ascii") for m in self.charter_id[mask].tolist()]}

    def unflatten(self, selection) -> dict:
        """Compress ANY selection to the MINIMAL ``{archive_ids, fond_ids, charter_ids}`` that
        expands back to exactly it: a fond is emitted iff **all** its charters are selected, an
        archive iff **all** its fonds are (hence all its charters); every other selected charter
        stays explicit. ``selection`` is a bool mask or a wire basket dict (already-hierarchical
        baskets are resolved to a charter mask first, so it need not be flat). Inverse of
        :meth:`flatten`."""
        mask = self.receive_basket(selection) if isinstance(selection, dict) else np.asarray(selection, dtype=bool)
        c2f, f2a = self.charter_to_fond_idx, self.fond_to_archive_idx
        nf, na = len(self.fond_id), len(self.archive_id)
        fond_total = np.bincount(c2f, minlength=nf)          # charters per fond
        fond_sel = np.bincount(c2f[mask], minlength=nf)      # selected charters per fond
        full_fond = (fond_total > 0) & (fond_sel == fond_total)
        arch_total = np.bincount(f2a, minlength=na)          # fonds per archive
        arch_full = np.bincount(f2a[full_fond], minlength=na)  # full fonds per archive
        full_arch = (arch_total > 0) & (arch_full == arch_total)
        emit_fond = full_fond & ~full_arch[f2a]              # full fond, but its archive is not full
        emit_charter = mask & ~full_fond[c2f]               # selected charter whose fond is not full
        return {
            "archive_ids": [self.archive_id[a].decode("ascii") for a in np.flatnonzero(full_arch)],
            "fond_ids": [self.fond_id[f].decode("ascii") for f in np.flatnonzero(emit_fond)],
            "charter_ids": [self.charter_id[c].decode("ascii") for c in np.flatnonzero(emit_charter)],
        }

    def _selection_mask(self, charter_ids, fond_ids, archive_ids) -> np.ndarray:
        """Union of the given charters (+ each fond's / archive's charters) as a bool mask;
        ids absent from this universe are silently skipped."""
        mask = np.zeros(len(self), dtype=bool)
        for md5 in charter_ids:
            p = self.position_of(md5)
            if p >= 0:
                mask[p] = True
        for f in fond_ids:
            idx = self.fond_to_charter_idx.get(f)
            if idx is not None:
                mask[idx] = True
        for a in archive_ids:
            idx = self.archive_to_charter_idx.get(a)
            if idx is not None:
                mask[idx] = True
        return mask


class FSDBSharedImageIndex(FSDBSharedIndex):
    """Lightweight per-image extension of the charter namespace: a sorted image-md5 universe
    plus the image->charter and charter->image maps. This is what a per-image service (e.g.
    the Static FSDB gateway's ``/image`` + ``/iiif`` routes) reduces the FSDB into at load,
    replacing ad-hoc image globbing.

    It reuses the parent wholesale: :meth:`from_fsdb_root` runs
    :meth:`FSDBSharedIndex.from_fsdb_root` first -- the fast charter-only crawl that only
    lists ``<root>/<archive>/<fond>/<charter>`` and never opens a charter directory -- and
    then makes a **second pass** (:meth:`_scan_images`) that scandirs only the charter dirs
    the charter index already found, for their ``<md5>.img.<ext>`` files. So the charter
    index is never slowed down by images; the image cost is a clearly separate phase.

    On top of everything :class:`FSDBSharedIndex` provides (charter/fond/archive namespace,
    baskets, the ``/basket`` wire container), it adds:

    - ``image_id`` : sorted ``S32`` array of image md5s (the ``<md5>`` before ``.img.``), len Ni
    - ``image_ext`` : per-image file extension (``S``; e.g. ``b"jpg"`` / ``b"png"``), aligned to
      ``image_id`` -- lets :meth:`image_relpath` rebuild the filename with no filesystem access
    - ``image_to_charter_idx`` : dense ``int32``, image row -> owning charter position
    - ``charter_to_image_idx`` : forward map ``dict[charter_md5, int32[]]`` (derived) so a charter
      basket slices straight to image rows
    - ``image_index_hash`` : ``sha256("\\n".join(image_id))`` -- the image-universe analogue of
      ``index_hash``

    Images are **not** shipped over the wire: there is no image-level compact basket (image
    slicing derives from a resolved charter set), so the image universe is server-side only
    and :meth:`from_db_bytes` is unavailable. Build one with :meth:`from_fsdb_root`.
    """

    # <md5>.img.<ext> image files: 32-hex lowercase md5, extension e.g. jpg / jpeg / png.
    _IMG_RE = re.compile(r"^([0-9a-f]{32})\.img\.([A-Za-z0-9]+)$")

    def __init__(self, archive_id: np.ndarray, fond_id: np.ndarray, charter_id: np.ndarray,
                 charter_to_fond_idx: np.ndarray, fond_to_archive_idx: np.ndarray,
                 *, image_id: np.ndarray, image_to_charter_idx: np.ndarray,
                 image_ext: np.ndarray | None = None,
                 filepattern: str | None = None, presence_mask: np.ndarray | None = None,
                 fsdb_root: str | Path | None = None) -> None:
        super().__init__(archive_id, fond_id, charter_id, charter_to_fond_idx, fond_to_archive_idx,
                         filepattern=filepattern, presence_mask=presence_mask, fsdb_root=fsdb_root)
        image_id = np.asarray(image_id)
        image_to_charter_idx = np.asarray(image_to_charter_idx)
        if image_id.ndim != 1 or image_id.dtype.kind != "S":
            raise TypeError(f"image_id: expected 1-D 'S'-kind array, got {image_id.ndim}-D {image_id.dtype!r}")
        if (image_to_charter_idx.ndim != 1 or image_to_charter_idx.dtype.kind not in ("i", "u")
                or len(image_to_charter_idx) != len(image_id)):
            raise TypeError("image_to_charter_idx: expected a 1-D integer array aligned to image_id")
        if image_ext is not None:
            image_ext = np.asarray(image_ext)
            if image_ext.dtype.kind != "S" or image_ext.ndim != 1 or len(image_ext) != len(image_id):
                raise TypeError("image_ext: expected a 1-D 'S'-kind array aligned to image_id")
        self.image_id = image_id
        self.image_ext = image_ext
        self.image_to_charter_idx = image_to_charter_idx.astype("<i4", copy=False)

        # forward map (charter -> its image rows) + the image-universe hash
        self.charter_to_image_idx = self._forward(self.image_to_charter_idx, self.charter_id)
        self.image_index_hash = hashlib.sha256(b"\n".join(self.image_id.tolist())).hexdigest()

        _freeze(self.image_id, self.image_to_charter_idx)
        if self.image_ext is not None:
            _freeze(self.image_ext)
        _freeze(*self.charter_to_image_idx.values())

    # ---- alternative constructors ------------------------------------------------------
    @classmethod
    def from_db_bytes(cls, buf) -> "FSDBSharedImageIndex":
        raise NotImplementedError(
            "FSDBSharedImageIndex has no wire container: the image universe is server-side only "
            "(image baskets derive from a resolved charter set). Build it with from_fsdb_root().")

    @classmethod
    def from_fsdb_root(cls, fsdb_root: str | Path, *, filepattern: str | None = None,
                       verbose: int = 0, workers=None) -> "FSDBSharedImageIndex":
        """Build the charter index with the parent's fast walk, then add the image layer.

        Phase 1 delegates to :meth:`FSDBSharedIndex.from_fsdb_root` (charter-only crawl -- no
        charter directory is ever opened; cheap and sequential). Phase 2 (:meth:`_scan_images`)
        scandirs each of those already-known charter dirs for ``<md5>.img.<ext>`` files, in
        PARALLEL over processes -- it is ~97% of the build. The charter arrays are reused verbatim
        from phase 1; only the image arrays are new. ``workers=None`` -> one process per core.
        """
        base = FSDBSharedIndex.from_fsdb_root(fsdb_root, filepattern=filepattern, verbose=verbose)
        image_id, image_ext, image_to_charter_idx = cls._scan_images(base, verbose=verbose,
                                                                     workers=workers)
        return cls(base.archive_id, base.fond_id, base.charter_id,
                   base.charter_to_fond_idx, base.fond_to_archive_idx,
                   image_id=image_id, image_to_charter_idx=image_to_charter_idx, image_ext=image_ext,
                   filepattern=base.filepattern, presence_mask=base.presence_mask, fsdb_root=base.fsdb_root)

    @classmethod
    def _scan_images(cls, base: "FSDBSharedIndex", *, verbose: int = 0, workers=None):
        """Second pass over an already-built charter index ``base``: scandir each charter
        directory (reconstructed from ``base`` -- no re-walking of archives/fonds) for its
        ``<md5>.img.<ext>`` files. Returns ``(image_id, image_ext, image_to_charter_idx)``
        sorted by image md5. The charter row is known directly (no ``searchsorted``).

        Runs on the shared parallel driver (:func:`iter_charter_scan`): this pass opens every
        charter directory and dominates index construction (~97% of it), so it is process-parallel;
        ``workers=None`` -> one per core, ``workers<=1`` -> serial."""
        if base.fsdb_root is None:
            raise ValueError("image scan requires the charter index to know its fsdb_root")
        t0 = time.time()
        if verbose >= 1:
            how = "serial" if (workers or _default_workers()) <= 1 else f"{workers or _default_workers()} processes"
            print(f"[FSDBSharedImageIndex] scanning images in {len(base)} charters ({how}) ...",
                  file=sys.stderr, flush=True)

        img_ids: list[str] = []
        img_pos: list[int] = []
        img_exts: list[str] = []
        for found in iter_charter_scan(base, _scan_charter_images, workers=workers, verbose=verbose,
                                       desc="[FSDBSharedImageIndex] scanning images"):
            for mid, pos, ext in found:              # results arrive in charter-position order
                img_ids.append(mid)
                img_pos.append(pos)
                img_exts.append(ext)

        if img_ids:
            image_md5_arr = np.array(img_ids, dtype="S32")
            iorder = np.argsort(image_md5_arr, kind="stable")
            image_id = np.ascontiguousarray(image_md5_arr[iorder])
            image_ext = np.ascontiguousarray(np.array(img_exts, dtype="S")[iorder])
            image_to_charter_idx = np.array(img_pos, dtype="<i4")[iorder]  # charter row is known directly
        else:
            image_id = np.empty(0, dtype="S32")
            image_ext = np.empty(0, dtype="S1")
            image_to_charter_idx = np.empty(0, dtype="<i4")

        if verbose >= 1:
            print(f"[FSDBSharedImageIndex] found {len(image_id)} images in {time.time() - t0:.2f}s",
                  file=sys.stderr, flush=True)
        return image_id, image_ext, image_to_charter_idx

    # ---- image namespace / lookups -----------------------------------------------------
    @property
    def n_images(self) -> int:
        return len(self.image_id)

    def image_position_of(self, imgmd5) -> int:
        """Image md5 -> row in the sorted image universe, or -1 if absent."""
        key = imgmd5.encode("ascii") if isinstance(imgmd5, str) else bytes(imgmd5)
        i = int(np.searchsorted(self.image_id, key))
        return i if i < len(self.image_id) and self.image_id[i] == key else -1

    def has_image(self, imgmd5) -> bool:
        return self.image_position_of(imgmd5) >= 0

    def _image_position(self, key) -> int:
        """Coerce an image md5 (str/bytes) or an int row into a validated image row."""
        if isinstance(key, (int, np.integer)):
            pos = int(key)
        else:
            md5 = key.decode("ascii") if isinstance(key, (bytes, bytearray)) else key
            pos = self.image_position_of(md5)
        if not 0 <= pos < len(self.image_id):
            raise KeyError(f"image {key!r} not in index")
        return pos

    def image_charter(self, key) -> str:
        """Image md5 (or row) -> owning charter md5."""
        ipos = self._image_position(key)
        return self.charter_id[self.image_to_charter_idx[ipos]].decode("ascii")

    def image_relpath(self, key) -> str:
        """Image md5 (or row) -> ``<archive>/<fond>/<charter>/<imgmd5>.img.<ext>`` relative to
        the FSDB root, reconstructed from the hierarchy arrays. No filesystem access when
        ``image_ext`` is present; otherwise the extension is recovered by a single glob (which
        needs ``fsdb_root``)."""
        ipos = self._image_position(key)
        imgmd5 = self.image_id[ipos].decode("ascii")
        charter_rel = self.charter_relpath(int(self.image_to_charter_idx[ipos]))
        if self.image_ext is not None:
            ext = self.image_ext[ipos].decode("ascii")
            return f"{charter_rel}/{imgmd5}.img.{ext}"
        if self.fsdb_root is None:
            raise ValueError("image_relpath needs image_ext or fsdb_root to recover the extension")
        matches = sorted((self.fsdb_root / charter_rel).glob(f"{imgmd5}.img.*"))
        if not matches:
            raise FileNotFoundError(f"no image file {imgmd5}.img.* under {charter_rel}")
        return f"{charter_rel}/{matches[0].name}"

    def image_path(self, key) -> Path:
        """Absolute path to an image file for an image md5/row. Requires the index to know its
        ``fsdb_root`` (set by :meth:`from_fsdb_root`)."""
        if self.fsdb_root is None:
            raise ValueError("image_path requires fsdb_root (build the index via from_fsdb_root)")
        return self.fsdb_root / self.image_relpath(key)

    def charter_image_rows(self, charter_key) -> np.ndarray:
        """Charter md5 (or position) -> its image rows (``int32[]``), for slicing per-image
        data by a charter basket. A charter with no images -> empty array; an unknown charter
        raises ``KeyError``."""
        pos = self._position(charter_key)
        return self.charter_to_image_idx[self.charter_id[pos].decode("ascii")]
