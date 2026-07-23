// fsdb_sharedindex.js -- JavaScript mirror of fsdb/shared_index.py (the "/basket/db"
// binary container). Dependency-free ES module: works via <script type="module"> in a
// plain HTML5 page, imports cleanly into React/Vite/webpack, and runs under Node.
//
// LITTLE-ENDIAN ONLY. The container's multi-byte integers are little-endian and JS
// TypedArrays use the host byte order, which is LE on every browser / WASM / mainstream
// target. We refuse to load on a (hypothetical) big-endian host rather than misread data.
// The Python side (fsdb/shared_index.py) is the authoritative format spec; keep the two
// byte-for-byte compatible -- test/shared_index/ exercises the cross-language round-trip.
//
// Container layout (see the Python module docstring for the canonical description):
//   magic "FSDBIDX\0" (8) | version u32-LE | sentinel u32-LE=0x01020304 | header_len u32-LE
//   | header JSON (H bytes) | pad to 4 | block data at data_start + block.offset
// Block kinds: "S" = fixed-width NUL-padded byte string, "i4" = little-endian int32.
// The container is uncompressed; gzip is applied at the HTTP layer (native
// CompressionStream / DecompressionStream), never in here.

// --- little-endian enforcement (load-time) -------------------------------------------
const _IS_LE = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;
if (!_IS_LE) {
  throw new Error("fsdb_sharedindex: big-endian host not supported (the wire format is little-endian only).");
}

const MAGIC = new Uint8Array([0x46, 0x53, 0x44, 0x42, 0x49, 0x44, 0x58, 0x00]); // "FSDBIDX\0"
const VERSION = 1;
const SENTINEL = 0x01020304;
const HEADER_FIXED = 20; // magic(8) + version(4) + sentinel(4) + header_len(4)

const align4 = (n) => (n + 3) & ~3;

/**
 * Serialise named blocks into the little-endian shared-index container.
 * @param {Object} blocks insertion-ordered name -> descriptor:
 *   {kind:"S", width:Number, values:string[]} or {kind:"i4", values:(number[]|Int32Array)}
 * @param {string} indexHash
 * @param {Object} meta
 * @returns {ArrayBuffer}
 */
export function serializeDb(blocks, indexHash = "", meta = {}) {
  const enc = new TextEncoder();
  const descriptors = [];
  const payloads = [];
  let rel = 0;
  for (const [name, blk] of Object.entries(blocks)) {
    let data, kind, width, count;
    if (blk.kind === "S") {
      kind = "S";
      width = blk.width;
      count = blk.values.length;
      data = new Uint8Array(count * width); // zero-filled => NUL padding, matching numpy 'S'
      for (let i = 0; i < count; i++) {
        const bytes = enc.encode(blk.values[i]);
        if (bytes.length > width) throw new Error(`block ${name}: value longer than width ${width}`);
        data.set(bytes, i * width);
      }
    } else if (blk.kind === "i4") {
      kind = "i4";
      width = 4;
      const ints = blk.values instanceof Int32Array ? blk.values : Int32Array.from(blk.values);
      count = ints.length;
      // host is LE (asserted above) => the Int32Array bytes are already little-endian.
      data = new Uint8Array(ints.buffer, ints.byteOffset, ints.byteLength).slice();
    } else {
      throw new Error(`block ${name}: unknown kind ${blk.kind}`);
    }
    rel = align4(rel);
    descriptors.push({ name, kind, width, count, offset: rel, nbytes: data.length });
    payloads.push(data);
    rel += data.length;
  }

  const header = { index_hash: indexHash, meta, blocks: descriptors };
  const headerBytes = enc.encode(JSON.stringify(header));
  const dataStart = align4(HEADER_FIXED + headerBytes.length);
  const out = new Uint8Array(dataStart + rel);
  out.set(MAGIC, 0);
  const dv = new DataView(out.buffer);
  dv.setUint32(8, VERSION, true);
  dv.setUint32(12, SENTINEL, true);
  dv.setUint32(16, headerBytes.length, true);
  out.set(headerBytes, HEADER_FIXED);
  for (let i = 0; i < descriptors.length; i++) {
    out.set(payloads[i], dataStart + descriptors[i].offset);
  }
  return out.buffer;
}

/**
 * Inverse of serializeDb.
 * @param {ArrayBuffer|Uint8Array|ArrayBufferView} buf
 * @returns {{index_hash:string, meta:Object, blocks:Object}} blocks: name -> descriptor
 *   {kind:"S", width, count, values:string[]} or {kind:"i4", count, values:Int32Array}
 */
export function deserializeDb(buf) {
  // Normalise to a fresh, 0-offset ArrayBuffer so int32 views are guaranteed 4-aligned
  // (a Node Buffer's .buffer may start at an unaligned byteOffset).
  let ab;
  if (buf instanceof ArrayBuffer) {
    ab = buf;
  } else {
    const view = buf instanceof Uint8Array ? buf : new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
    ab = view.buffer.slice(view.byteOffset, view.byteOffset + view.byteLength);
  }
  const u8 = new Uint8Array(ab);
  const dv = new DataView(ab);
  for (let i = 0; i < 8; i++) {
    if (u8[i] !== MAGIC[i]) throw new Error("shared-index: bad magic (not an FSDBIDX container)");
  }
  const sentinel = dv.getUint32(12, true);
  if (sentinel !== SENTINEL) {
    throw new Error("shared-index: byte-order sentinel mismatch (payload is not little-endian)");
  }
  const headerLen = dv.getUint32(16, true);
  const dec = new TextDecoder();
  const header = JSON.parse(dec.decode(u8.subarray(HEADER_FIXED, HEADER_FIXED + headerLen)));
  const dataStart = align4(HEADER_FIXED + headerLen);

  const blocks = {};
  for (const d of header.blocks) {
    const off = dataStart + d.offset;
    if (d.kind === "S") {
      const values = new Array(d.count);
      for (let i = 0; i < d.count; i++) {
        const start = off + i * d.width;
        let end = start;
        const limit = start + d.width;
        while (end < limit && u8[end] !== 0) end++; // stop at NUL padding
        values[i] = dec.decode(u8.subarray(start, end));
      }
      blocks[d.name] = { kind: "S", width: d.width, count: d.count, values };
    } else if (d.kind === "i4") {
      blocks[d.name] = { kind: "i4", count: d.count, values: new Int32Array(ab, off, d.count) };
    } else {
      throw new Error(`shared-index: unknown block kind ${d.kind}`);
    }
  }
  return { index_hash: header.index_hash, meta: header.meta, blocks };
}

// --- baskets: compact wire transfer of a charter subset (mirror of FSDBSharedIndex) -----
//
// A basket travels as a plain object, identical in shape to the Python side:
//   { all_charters: boolean,
//     charter_ids: string[], fond_ids: string[], archive_ids: string[],
//     bit_vector: base64-string | null,   // packed bits over the sorted charter universe
//     bit_vector_hash: string }           // index_hash the basket references
// A basket that flattens to more than this fraction of the universe travels as the bit
// vector (1 bit/charter) instead of a 32-byte md5 list; must match the Python constant.
export const BITVECTOR_DENSITY_THRESHOLD = 1 / 1024;

/** Raised when a wire basket references a different index universe than the one decoding it.
 *  Mirror of fsdb.shared_index.IndexMismatch; carries the current index_hash for a re-sync. */
export class IndexMismatch extends Error {
  constructor(expected, got) {
    super(`index_hash mismatch: expected ${expected}, got ${got}`);
    this.name = "IndexMismatch";
    this.expected = expected;
    this.indexHash = got;
  }
}

// packbits/unpackbits use numpy's default big-endian bit order (bit i -> byte i>>3, MSB
// first: mask position 0 is the top bit of byte 0), so the packed bytes are byte-for-byte
// identical to np.packbits on the Python side.
function packBits(mask) {
  const out = new Uint8Array((mask.length + 7) >> 3);
  for (let i = 0; i < mask.length; i++) if (mask[i]) out[i >> 3] |= 1 << (7 - (i & 7));
  return out;
}
function b64encode(u8) {
  let s = ""; for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
  return typeof btoa === "function" ? btoa(s) : Buffer.from(u8).toString("base64");
}
function b64decode(str) {
  if (typeof atob === "function") {
    const s = atob(str); const u8 = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) u8[i] = s.charCodeAt(i);
    return u8;
  }
  return new Uint8Array(Buffer.from(str, "base64"));
}

/** A read-only charter/fond/archive universe built from a `/basket/db` container, mirroring
 *  the Python `FSDBSharedIndex`: it maps charter md5s <-> sorted positions and encodes/decodes
 *  baskets. Build it from raw container bytes (`SharedIndex.fromContainer(buf)`) or from the
 *  `blocks` of `deserializeDb`. Dependency-free; runs in the browser and under Node. */
export class SharedIndex {
  static fromContainer(buf) { return new SharedIndex(deserializeDb(buf)); }

  /** @param {{index_hash:string, blocks:Object}} db output of deserializeDb */
  constructor({ index_hash, blocks }) {
    this.indexHash = index_hash;
    this.charterId = blocks.charter_id.values;   // sorted md5 strings
    this.fondId = blocks.fond_id.values;
    this.archiveId = blocks.archive_id.values;
    const c2f = blocks.charter_to_fond_idx.values, f2a = blocks.fond_to_archive_idx.values;
    this.charterToFond = c2f;   // charter position -> fond position (kept for flatten/unflatten)
    this.fondToArchive = f2a;   // fond position -> archive position
    // fond/archive id -> sorted charter positions (int[]), the position analogue of buildResolver
    this._fondToPos = new Map(); this._archiveToPos = new Map();
    const push = (m, k, v) => { let a = m.get(k); if (!a) m.set(k, a = []); a.push(v); };
    for (let c = 0; c < this.charterId.length; c++) {
      const fi = c2f[c];
      push(this._fondToPos, this.fondId[fi], c);
      push(this._archiveToPos, this.archiveId[f2a[fi]], c);
    }
  }

  get length() { return this.charterId.length; }

  /** Charter md5 -> position in the sorted universe, or -1 (binary search; ASCII hex order
   *  matches numpy's byte sort of the S32 ids). */
  positionOf(md5) {
    const a = this.charterId; let lo = 0, hi = a.length - 1;
    while (lo <= hi) { const mid = (lo + hi) >> 1;
      if (a[mid] === md5) return mid;
      if (a[mid] < md5) lo = mid + 1; else hi = mid - 1; }
    return -1;
  }

  /** What KIND of entity is `id` in this slice: "charter" | "fond" | "archive" | null.
   *
   * Charter and fond ids are both 32-hex, so shape cannot tell them apart -- only the index can.
   * This is what lets a UI accept a dropped/pasted identifier without asking the user which of
   * the three it is (the Slicer's custom export zone). Checked charter-first: the charter table
   * is the one with a sorted binary search. */
  classify(id) {
    if (!id) return null;
    if (this.positionOf(id) >= 0) return "charter";
    if (this._fondToPos.has(id)) return "fond";
    if (this._archiveToPos.has(id)) return "archive";
    return null;
  }

  positionsToMd5s(positions) {
    const out = new Array(positions.length);
    for (let i = 0; i < positions.length; i++) out[i] = this.charterId[positions[i]];
    return out;
  }

  _selectionMask(charterIds = [], fondIds = [], archiveIds = []) {
    const mask = new Uint8Array(this.length);
    for (const md5 of charterIds) { const p = this.positionOf(md5); if (p >= 0) mask[p] = 1; }
    for (const f of fondIds) { const ps = this._fondToPos.get(f); if (ps) for (const p of ps) mask[p] = 1; }
    for (const a of archiveIds) { const ps = this._archiveToPos.get(a); if (ps) for (const p of ps) mask[p] = 1; }
    return mask;
  }

  /** Encode a selection into a wire basket, picking the denser representation (bit vector when
   *  the flattened charter count exceeds BITVECTOR_DENSITY_THRESHOLD of the universe). */
  sendBasket({ allCharters = false, charterIds = [], fondIds = [], archiveIds = [] } = {}) {
    const N = this.length;
    if (allCharters) {
      return { all_charters: true, charter_ids: [], fond_ids: [], archive_ids: [],
               bit_vector: null, bit_vector_hash: this.indexHash };
    }
    const mask = this._selectionMask(charterIds, fondIds, archiveIds);
    let k = 0; for (let i = 0; i < mask.length; i++) k += mask[i];
    if (k > N * BITVECTOR_DENSITY_THRESHOLD) {
      return { all_charters: false, charter_ids: [], fond_ids: [], archive_ids: [],
               bit_vector: b64encode(packBits(mask)), bit_vector_hash: this.indexHash };
    }
    return { all_charters: false, charter_ids: [...charterIds], fond_ids: [...fondIds],
             archive_ids: [...archiveIds], bit_vector: null, bit_vector_hash: this.indexHash };
  }

  /** Decode a wire basket into a Uint32Array of set charter positions (ascending). Throws
   *  IndexMismatch when the basket references a different universe AND uses a universe-relative
   *  feature (all_charters or bit_vector); pure id-list baskets are literal and tolerated. */
  receiveBasket(basket) {
    const N = this.length;
    const allCharters = !!basket.all_charters;
    const bv = basket.bit_vector ?? null;
    const bvHash = basket.bit_vector_hash || "";
    if (bvHash && bvHash !== this.indexHash && (allCharters || bv !== null)) {
      throw new IndexMismatch(bvHash, this.indexHash);
    }
    if (allCharters) { const out = new Uint32Array(N); for (let i = 0; i < N; i++) out[i] = i; return out; }
    let mask;
    if (bv !== null) {
      const bytes = typeof bv === "string" ? b64decode(bv) : Uint8Array.from(bv);
      mask = new Uint8Array(N);
      for (let i = 0; i < N; i++) mask[i] = (bytes[i >> 3] >> (7 - (i & 7))) & 1;
    } else {
      mask = this._selectionMask(basket.charter_ids, basket.fond_ids, basket.archive_ids);
    }
    let n = 0; for (let i = 0; i < N; i++) n += mask[i];
    const out = new Uint32Array(n);
    let j = 0; for (let i = 0; i < N; i++) if (mask[i]) out[j++] = i;
    return out;
  }

  /** Base64 of a bool charter mask packed to bits (np.packbits order) -- the storage form of an
   *  opaque basket's membership and the `bit_vector` wire field. */
  encodeMask(mask) { return b64encode(packBits(mask)); }

  /** A bool charter mask from a selection: a Uint8Array mask (returned as-is) or a wire basket. */
  maskOf(selection) {
    if (selection instanceof Uint8Array) return selection;
    const pos = this.receiveBasket(selection);
    const mask = new Uint8Array(this.length);
    for (const p of pos) mask[p] = 1;
    return mask;
  }

  /** Expand ANY selection (mask or wire basket) to an explicit flat charter set
   *  `{archive_ids:[], fond_ids:[], charter_ids:[every selected md5]}`. Inverse of unflatten(). */
  flatten(selection) {
    const mask = this.maskOf(selection), charter_ids = [];
    for (let c = 0; c < this.length; c++) if (mask[c]) charter_ids.push(this.charterId[c]);
    return { archive_ids: [], fond_ids: [], charter_ids };
  }

  /** Compress ANY selection (mask or wire basket) to the MINIMAL `{archive_ids, fond_ids,
   *  charter_ids}` that expands back to exactly it: a fond is emitted iff ALL its charters are
   *  selected, an archive iff ALL its fonds are; the rest stay explicit charters. Inverse of
   *  flatten(); works on already-hierarchical baskets (resolved to a mask first). */
  unflatten(selection) {
    const mask = this.maskOf(selection);
    const N = this.length, c2f = this.charterToFond, f2a = this.fondToArchive;
    const nf = this.fondId.length, na = this.archiveId.length;
    const fondTotal = new Int32Array(nf), fondSel = new Int32Array(nf);
    for (let c = 0; c < N; c++) { fondTotal[c2f[c]]++; if (mask[c]) fondSel[c2f[c]]++; }
    const fullFond = new Uint8Array(nf);
    for (let f = 0; f < nf; f++) fullFond[f] = (fondTotal[f] > 0 && fondSel[f] === fondTotal[f]) ? 1 : 0;
    const archTotal = new Int32Array(na), archFull = new Int32Array(na);
    for (let f = 0; f < nf; f++) { archTotal[f2a[f]]++; if (fullFond[f]) archFull[f2a[f]]++; }
    const fullArch = new Uint8Array(na);
    for (let a = 0; a < na; a++) fullArch[a] = (archTotal[a] > 0 && archFull[a] === archTotal[a]) ? 1 : 0;
    const archive_ids = [], fond_ids = [], charter_ids = [];
    for (let a = 0; a < na; a++) if (fullArch[a]) archive_ids.push(this.archiveId[a]);
    for (let f = 0; f < nf; f++) if (fullFond[f] && !fullArch[f2a[f]]) fond_ids.push(this.fondId[f]);
    for (let c = 0; c < N; c++) if (mask[c] && !fullFond[c2f[c]]) charter_ids.push(this.charterId[c]);
    return { archive_ids, fond_ids, charter_ids };
  }
}
