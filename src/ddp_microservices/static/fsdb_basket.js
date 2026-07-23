// fsdb_basket.js -- client-side, named "shopping carts" of FSDB entities.
//
// STORAGE and UI are separate classes:
//   * BasketStore -- the data model + OPFS persistence, NO DOM. A basket is
//        { charters: [md5], fonds: [md5], archives: [id], memo: "" }
//     and the whole store is { active, prevActive, baskets: { "<name>": basket } }, persisted
//     as one OPFS file `baskets.json`. Index-independent (raw ids). Emits "change".
//     A reserved, undeletable, non-editable basket named `All` (ALL_BASKET) is always present:
//     selecting it scopes to every charter (wire flag `all_charters:true`) and disables item
//     add/remove; `prevActive` remembers the editable basket to switch back to.
//   * BasketWidget -- all the DOM (built in JS; the Jinja partial is just a mount point) and
//     user interaction; it only talks to a BasketStore. It is also a drop target: dropping a
//     DRAG_TYPE payload (whitespace-separated charter md5s) adds them to the active basket.
//
// The storage backend is INJECTABLE (production: OPFS; Node tests: memoryStore()).
// Dependency-free, web-standard ES module. Imports the shared-index codec for the reserved
// compact-basket hook (future); unused otherwise.

import { SharedIndex } from "./fsdb_sharedindex.js";
import { runBlocking, throwIfAborted, isAbort } from "./ddp_busy.js";
import { exportBytes, parseBasketsFile, validateAgainstIndex, missingWarning, EXPORT_FILENAME }
  from "./fsdb_basket_io.js";
import { openStore, openOpfsDir, OPFS_ROOT, dirBytes, readMeta, syncIndex, SharedIndexSyncer }
  from "./fsdb_sharedindex_store.js";
import { viewedContext, viewed, VIEW_TYPES } from "./ddp_views.js";

// ---- debug mode -----------------------------------------------------------------------------
// Effective debug = the config `developer_mode` flag, unless overridden LIVE from the console via
// `ddpDebug(true|false)` (a transient in-memory override; a page reload resets to the config value).
// When on: a collapsible debug panel appears below the basket, and `<body>` gets `.ddp-debug` so
// the standardized view regions + chrome are tinted (see the CSS). See ddp_views.js for the views.
let _debugOverride = null;   // null = follow config; true/false = console override (until reload)
function debugOn() { return _debugOverride !== null ? _debugOverride : !!CLIENT_CONFIG.developer_mode; }

/** Toggle/set debug mode from the console: `ddpDebug()` flips it, `ddpDebug(true|false)` sets it.
 *  Transient (not persisted): a reload reverts to the JSON config `developer_mode`. */
export function ddpDebug(on) {
  _debugOverride = (on === undefined) ? !debugOn() : !!on;
  try { document.dispatchEvent(new CustomEvent("ddp:debugchange")); } catch { /* no DOM */ }
  return debugOn();
}
if (typeof window !== "undefined") window.ddpDebug = ddpDebug;   // console entry point

// map a standard view type -> the basket list it belongs in (scalar views only; set views
// like charter_list are handled later).
const VIEW_TO_BASKET = { charter: "charters", fond: "fonds", archive: "archives" };

/** Derive the basket's "current entity" from the page's declared context view. */
function currentFromContext() {
  const ctx = viewedContext();
  if (!ctx || !ctx.value) return null;
  const type = VIEW_TO_BASKET[ctx.type];
  return type ? { type, id: ctx.value } : null; // non-scalar / unhandled context -> no +/-
}

const FILE = "baskets.json";
const DEFAULT_USER = "default_user";
const DEFAULT = "default";
const TYPES = ["archives", "fonds", "charters"]; // display order

/** The reserved "whole Monasterium" basket: always present, undeletable, non-editable. Selecting
 *  it scopes to every charter (the wire flag `all_charters:true`) rather than an id list; while it
 *  is active, per-item add/remove is disabled. Its name is reserved (no user basket may use it). */
export const ALL_BASKET = "All";
/** MIME type of a drag payload the basket widget accepts as a drop: whitespace-separated charter
 *  md5s. Producers (e.g. layout thumbnails) `dataTransfer.setData(DRAG_TYPE, md5)`. */
export const DRAG_TYPE = "text/ddp-charter-md5";

/** The SINGLE SOURCE OF TRUTH for client configuration. Populated by loadClientConfig() from
 *  static/default_client_config.json (which holds the default values -- see that file for the
 *  keys: max_storage_bytes, sync_interval_ms, sync_recheck_ms, max_basket_size, developer_mode).
 *  Read this object; never keep a private copy. Later it may be made editable, or fetched
 *  per-user from the server -- but always through this one object. Consumers that read a key
 *  before it is loaded fall back to their own parameter defaults. */
export const CLIENT_CONFIG = {};

/** Load configuration into CLIENT_CONFIG from static/default_client_config.json (the current
 *  source). Returns CLIENT_CONFIG. A failed load leaves it unchanged. `url`/`fetchFn` injectable. */
export async function loadClientConfig({ url, fetchFn = fetch } = {}) {
  url = url || new URL("./default_client_config.json", import.meta.url);
  try {
    Object.assign(CLIENT_CONFIG, await (await fetchFn(url)).json());
  } catch { /* keep whatever CLIENT_CONFIG already holds */ }
  return CLIENT_CONFIG;
}
const TYPE_LABEL = { archives: "Archives", fonds: "Fonds", charters: "Charters" };
const LINK = {
  charters: (id) => `/charter/${id}`,
  fonds: (id) => `/paged_fond/${id}`,
  archives: (id) => `/paged_archive/${id}`,
};
const SINGULAR = { charters: "charter", fonds: "fond", archives: "archive" };
const emptyBasket = () => ({ charters: [], fonds: [], archives: [], memo: "" });
/** The canonical reserved-basket object. Marked with `all: true`; its id lists stay empty (its
 *  meaning is "everything", carried by the flag, not enumerated). Rebuilt on every load so it can
 *  never drift or be edited on disk. */
const allBasket = () => ({ charters: [], fonds: [], archives: [], memo: "Whole Monasterium — every charter.", all: true });

// ---- OPFS-backed store (injectable, mirrors fsdb_sharedindex_store.js) -----------------

/** The active user's basket directory: `DDP-VRE/baskets/<user>/` (user from CLIENT_CONFIG,
 *  defaulting to "default_user" -- a logical partition ready for real user management later).
 *  Falls back to a non-persistent in-memory store when OPFS is unavailable. */
export async function openBasketStore(user = CLIENT_CONFIG.user || DEFAULT_USER) {
  try {
    return await openOpfsDir(OPFS_ROOT, "baskets", user);
  } catch {
    return memoryStore();
  }
}

/** Minimal in-memory FileSystemDirectoryHandle look-alike (fallback + tests). */
export function memoryStore() {
  const files = new Map(); // name -> Uint8Array
  const handle = (name) => ({
    kind: "file",
    async getFile() {
      const d = files.get(name) ?? new Uint8Array();
      return { size: d.length, async text() { return new TextDecoder().decode(d); },
               async arrayBuffer() { return d.slice().buffer; } };
    },
    async createWritable() {
      const parts = [];
      return {
        async write(chunk) { parts.push(chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk)); },
        async close() {
          let len = 0; for (const p of parts) len += p.length;
          const out = new Uint8Array(len); let o = 0;
          for (const p of parts) { out.set(p, o); o += p.length; }
          files.set(name, out);
        },
      };
    },
  });
  return {
    async getFileHandle(name, { create = false } = {}) {
      if (!files.has(name)) {
        if (!create) { const e = new Error("NotFound"); e.name = "NotFoundError"; throw e; }
        files.set(name, new Uint8Array());
      }
      return handle(name);
    },
    async *entries() { for (const k of files.keys()) yield [k, handle(k)]; },
    async removeEntry(name) { files.delete(name); },
  };
}

async function readJson(store, name) {
  try { return JSON.parse(await (await (await store.getFileHandle(name)).getFile()).text()); }
  catch { return null; }
}
async function writeJson(store, name, obj) {
  const fh = await store.getFileHandle(name, { create: true });
  const w = await fh.createWritable();
  await w.write(new TextEncoder().encode(JSON.stringify(obj)));
  await w.close();
}

// ---- data model -----------------------------------------------------------------------

function cleanBasket(b) {
  const uniq = (a) => Array.from(new Set(Array.isArray(a) ? a.filter((x) => typeof x === "string") : []));
  b = (b && typeof b === "object") ? b : {};
  const out = { charters: uniq(b.charters), fonds: uniq(b.fonds), archives: uniq(b.archives),
                memo: typeof b.memo === "string" ? b.memo : "" };
  // An OPAQUE basket carries a packed membership bit-vector (base64) over a specific index universe
  // instead of id lists -- used for near-whole-DB sets whose compressed id form exceeds maxSize.
  // Its id lists stay empty; it is not editable (add/remove/flatten/... are no-ops); `n` is the
  // charter count (for display). See BasketStore.subtract's complement path.
  if (typeof b.bitVector === "string" && typeof b.bitHash === "string") {
    out.bitVector = b.bitVector; out.bitHash = b.bitHash;
    out.n = Number.isFinite(b.n) ? b.n : 0;
  }
  return out;
}

function normalize(j) {
  const baskets = {};
  const src = (j && j.baskets && typeof j.baskets === "object") ? j.baskets : {};
  for (const [k, v] of Object.entries(src)) baskets[k] = cleanBasket(v);
  delete baskets[ALL_BASKET];                          // never store an editable copy of the reserved one
  if (Object.keys(baskets).length === 0) baskets[DEFAULT] = emptyBasket();
  baskets[ALL_BASKET] = allBasket();                   // reserved, canonical, rebuilt every load
  const editable = Object.keys(baskets).filter((n) => n !== ALL_BASKET).sort();
  const active = (j && typeof j.active === "string" && baskets[j.active])
    ? j.active : editable[0];
  const prevActive = (j && typeof j.prevActive === "string" && j.prevActive !== ALL_BASKET
    && baskets[j.prevActive]) ? j.prevActive : editable[0];
  return { active, prevActive, baskets };
}

/** The data model: named baskets + an active one, persisted to `store` as one JSON file.
 *  No DOM. Dispatches a "change" Event after every mutation. */
export class BasketStore extends EventTarget {
  constructor({ store, maxSize = Infinity }) {
    super();
    this._store = store;
    this.maxSize = maxSize;   // max items (all types) in one basket; also the flatten cap
    this._data = { active: DEFAULT, prevActive: DEFAULT,
                   baskets: { [DEFAULT]: emptyBasket(), [ALL_BASKET]: allBasket() } };
  }

  static async open({ store, maxSize } = {}) {
    store = store || await openBasketStore();
    const s = new BasketStore({ store, maxSize });
    s._data = normalize(await readJson(store, FILE));
    await s._persist(false); // ensure the file exists & is normalized, no event
    return s;
  }

  // --- reads ---
  get names() { return Object.keys(this._data.baskets).sort(); }
  /** Editable basket names (everything except the reserved `All`), sorted. */
  get editableNames() { return this.names.filter((n) => n !== ALL_BASKET); }
  get activeName() { return this._data.active; }
  get active() { return this._data.baskets[this._data.active]; }
  /** Is the reserved whole-Monasterium basket the active one? (scope = every charter) */
  get isActiveAll() { return this._data.active === ALL_BASKET; }
  /** The editable basket that was active before switching to `All` (for switching back). */
  get prevActiveName() { return this._data.prevActive; }
  isAll(name = this._data.active) { return name === ALL_BASKET; }
  /** Opaque basket = carries a packed membership bit-vector instead of id lists (non-editable). */
  isOpaque(name = this._data.active) { return !!(this._data.baskets[name] || {}).bitVector; }
  get isActiveOpaque() { return this.isOpaque(this._data.active); }
  basket(name) { return this._data.baskets[name]; }
  counts(name = this._data.active) {
    const b = this._data.baskets[name];
    if (b.bitVector) return { archives: 0, fonds: 0, charters: b.n || 0 };   // opaque: charter count only
    return { archives: b.archives.length, fonds: b.fonds.length, charters: b.charters.length };
  }
  totalCount(name = this._data.active) {
    const c = this.counts(name); return c.archives + c.fonds + c.charters;
  }
  isEmpty(name = this._data.active) { return this.totalCount(name) === 0; }
  isFull(name = this._data.active) { return this.totalCount(name) >= this.maxSize; }
  has(type, id, name = this._data.active) {
    const b = this._data.baskets[name];
    return b.bitVector ? false : b[type].includes(id);   // opaque membership needs the index, not this
  }
  /** A wire-selection dict for basket `name` (id-lists, its stored bit-vector, or all_charters for
   *  the reserved All) -- the input to the shared index's maskOf / flatten / unflatten. */
  selectionOf(name = this._data.active, indexHash = null) {
    const b = this._data.baskets[name];
    if (name === ALL_BASKET) return { all_charters: true, charter_ids: [], fond_ids: [], archive_ids: [], bit_vector: null, bit_vector_hash: indexHash };
    if (b.bitVector) return { all_charters: false, charter_ids: [], fond_ids: [], archive_ids: [], bit_vector: b.bitVector, bit_vector_hash: b.bitHash };
    return { all_charters: false, charter_ids: b.charters, fond_ids: b.fonds, archive_ids: b.archives, bit_vector: null, bit_vector_hash: indexHash };
  }
  /** Deletable: an existing, empty, non-reserved basket, and never the last editable one. */
  canDelete(name) {
    return !this.isAll(name) && !!this._data.baskets[name] && this.isEmpty(name)
      && this.editableNames.length > 1;
  }
  /** A name usable for create/rename: non-empty, not reserved, and not an existing basket
   *  (except `exclude`, the name being renamed). */
  canUseName(name, exclude = null) {
    name = (name || "").trim();
    return !!name && name !== ALL_BASKET && (name === exclude || !this._data.baskets[name]);
  }

  // --- mutations (each persists + emits "change") ---
  async setActive(name) {
    if (!this._data.baskets[name] || name === this._data.active) return;
    // remember where we came from so a later "back" restores the editable basket
    if (name === ALL_BASKET) this._data.prevActive = this._data.active;
    this._data.active = name;
    await this._persist();
  }
  async add(type, id) {
    if (this.isActiveAll || this.isActiveOpaque) return false;   // reserved / opaque: not editable
    const arr = this.active[type];
    if (id && TYPES.includes(type) && !arr.includes(id) && !this.isFull()) {
      arr.push(id); await this._persist(); return true;
    }
    return false;
  }
  async remove(type, id) {
    if (this.isActiveAll || this.isActiveOpaque) return;         // reserved / opaque: not editable
    const arr = this.active[type]; const i = arr.indexOf(id);
    if (i >= 0) { arr.splice(i, 1); await this._persist(); }
  }
  async toggle(type, id) { return this.has(type, id) ? this.remove(type, id) : this.add(type, id); }
  async setMemo(text) {
    if (this.isActiveAll) return;                // the reserved basket is not editable (memo ok on opaque)
    this.active.memo = String(text ?? ""); await this._persist();
  }

  /** Empty the active basket's archives/fonds/charters (keeps its name + memo). Clearing an opaque
   *  basket drops its bit-vector too, making it an ordinary empty basket. */
  async clear() {
    if (this.isActiveAll || this.isEmpty()) return false;
    if (this.isActiveOpaque) { const b = this.active; delete b.bitVector; delete b.bitHash; delete b.n; }
    const b = this.active;
    b.archives = []; b.fonds = []; b.charters = [];
    await this._persist();
    return true;
  }

  async create(name) {
    if (!this.canUseName(name)) return false;
    name = name.trim();
    this._data.baskets[name] = emptyBasket();
    this._data.active = name;               // switch to the basket you just made
    await this._persist();
    return true;
  }
  async rename(oldName, newName) {
    if (oldName === ALL_BASKET) return false;   // the reserved basket cannot be renamed
    if (!this._data.baskets[oldName] || !this.canUseName(newName, oldName)) return false;
    newName = newName.trim();
    if (newName === oldName) return true;
    this._data.baskets[newName] = this._data.baskets[oldName];
    delete this._data.baskets[oldName];
    if (this._data.active === oldName) this._data.active = newName;
    await this._persist();
    return true;
  }
  async delete(name) {
    if (!this.canDelete(name)) return false;   // only empty, non-reserved, never the last editable
    delete this._data.baskets[name];
    if (this.editableNames.length === 0) this._data.baskets[DEFAULT] = emptyBasket();  // safety: keep >=1 editable
    if (this._data.active === name) this._data.active = this.editableNames[0];
    if (this._data.prevActive === name || !this._data.baskets[this._data.prevActive]
        || this._data.prevActive === ALL_BASKET) this._data.prevActive = this.editableNames[0];
    await this._persist();
    return true;
  }

  async _persist(emit = true) {
    await writeJson(this._store, FILE, this._data);
    if (emit) this.dispatchEvent(new Event("change"));
  }

  /** A basket name that is free right now: `name`, else "name (imported)", "name (imported 2)"… */
  freeName(name) {
    name = String(name || "").trim() || DEFAULT;
    if (this.canUseName(name)) return name;
    for (let i = 1; ; i++) {
      const cand = `${name} (imported${i > 1 ? " " + i : ""})`;
      if (this.canUseName(cand)) return cand;
    }
  }

  /**
   * Restore baskets from an exported file (see fsdb_basket_io.js). MERGES: nothing existing is
   * deleted or overwritten -- a name that is taken is imported under `freeName()` instead, so a
   * mis-click costs a cleanup rather than data. `All` is skipped (rebuilt on every load).
   *
   * One `_persist` for the whole import: routing thousands of ids through `add()` would rewrite the
   * OPFS file per id AND enforce maxSize per call, silently truncating a restore of a big basket.
   * Every basket goes through `cleanBasket`, so an arbitrary/hostile file cannot corrupt the store.
   *
   * @returns {name: importedAs} for the caller to report
   */
  async importBaskets(baskets) {
    const renamed = {};
    for (const [rawName, raw] of Object.entries(baskets || {})) {
      if (rawName === ALL_BASKET) continue;
      const name = this.freeName(rawName);
      this._data.baskets[name] = cleanBasket(raw);
      renamed[rawName] = name;
    }
    await this._persist();
    return renamed;
  }

  // --- clipboard / export (active basket's charters -- the spreadsheet use case) ---
  chartersColumn() { return this.active.charters.join("\n"); }  // paste DOWN a column
  chartersRow() { return this.active.charters.join("\t"); }     // paste ACROSS a row
  chartersFile() { const c = this.active.charters; return c.length ? c.join("\n") + "\n" : ""; }

  /** ALL baskets as TSV: one column per basket, the basket name as the header row; cells are
   *  the basket's charter md5s (flatten first if you want fonds/archives resolved too). */
  allBasketsChartersTsv() {
    const names = this.editableNames;                 // the reserved 'All' basket enumerates nothing
    const cols = names.map((n) => this._data.baskets[n].charters);
    const rows = cols.reduce((m, c) => Math.max(m, c.length), 0);
    const lines = [names.join("\t")];
    for (let r = 0; r < rows; r++) lines.push(cols.map((c) => c[r] ?? "").join("\t"));
    return lines.join("\n");
  }

  /** Item wire-selection helper: {charter_ids|fond_ids|archive_ids} -> full wire dict. */
  _itemSelection(item, indexHash) {
    return { all_charters: false, charter_ids: item.charter_ids || [], fond_ids: item.fond_ids || [],
             archive_ids: item.archive_ids || [], bit_vector: null, bit_vector_hash: indexHash };
  }

  /** Flatten the active basket via the shared index's single `flatten()` method: replace its
   *  fonds & archives with their charters, IFF the result stays under `limit` (default `maxSize`).
   *  Returns {ok, count, limit}; ok=false and no change when it would reach `limit`. */
  async flatten(ix, { limit } = {}) {
    if (this.isActiveAll || this.isActiveOpaque) return { ok: false, count: 0, limit: limit ?? this.maxSize };
    const cap = limit ?? this.maxSize;
    const { charter_ids } = ix.flatten(this.selectionOf(this._data.active, ix.indexHash));
    if (charter_ids.length >= cap) return { ok: false, count: charter_ids.length, limit: cap };
    const b = this.active;
    b.charters = charter_ids; b.fonds = []; b.archives = [];
    await this._persist();
    return { ok: true, count: charter_ids.length, limit: cap };
  }

  /** Unflatten (inverse of flatten): compress the active basket to the minimal {archives, fonds,
   *  charters} via the shared index's `unflatten()`. Returns {ok, archives, fonds, charters}. */
  async unflatten(ix) {
    if (this.isActiveAll || this.isActiveOpaque) return { ok: false };
    const u = ix.unflatten(this.selectionOf(this._data.active, ix.indexHash));
    const b = this.active;
    b.charters = u.charter_ids; b.fonds = u.fond_ids; b.archives = u.archive_ids;
    await this._persist();
    return { ok: true, archives: u.archive_ids.length, fonds: u.fond_ids.length, charters: u.charter_ids.length };
  }

  /** Set-subtract an `item` (a wire selection of charter/fond/archive ids) from the active basket:
   *  flatten both via the index, remove the item's charters, store the flat result. Guarded by
   *  `limit` (the basket's flatten must stay under it). Returns {ok, removed, count, limit}. */
  async subtract(ix, item, { limit } = {}) {
    if (this.isActiveAll || this.isActiveOpaque) return { ok: false, removed: 0 };
    const cap = limit ?? this.maxSize;
    const flat = new Set(ix.flatten(this.selectionOf(this._data.active, ix.indexHash)).charter_ids);
    if (flat.size >= cap) return { ok: false, count: flat.size, limit: cap, removed: 0 };
    const itemCharters = ix.flatten(this._itemSelection(item, ix.indexHash)).charter_ids;
    let removed = 0;
    for (const c of itemCharters) if (flat.delete(c)) removed++;
    const b = this.active;
    b.charters = [...flat]; b.fonds = []; b.archives = [];
    await this._persist();
    return { ok: true, removed, count: flat.size, limit: cap };
  }

  /** Create a NEW basket = the whole DB minus `item` (the complement of the viewed entity). Stores
   *  the minimal unflattened {archives, fonds, charters} when its id-count fits `maxSize`, else an
   *  opaque packed bit-vector. Returns {ok, name, n, opaque} (n = charters in the complement). */
  async createComplement(ix, item, name) {
    if (!this.canUseName(name)) return { ok: false };
    name = name.trim();
    const N = ix.length;
    const itemMask = ix.maskOf(this._itemSelection(item, ix.indexHash));
    const comp = new Uint8Array(N);
    let n = 0;
    for (let i = 0; i < N; i++) { const v = itemMask[i] ? 0 : 1; comp[i] = v; n += v; }
    const u = ix.unflatten(comp);                                     // minimal {archives, fonds, charters}
    const idCount = u.archive_ids.length + u.fond_ids.length + u.charter_ids.length;
    let basket;
    if (idCount <= this.maxSize) {
      basket = { charters: u.charter_ids, fonds: u.fond_ids, archives: u.archive_ids, memo: "" };
    } else {                                                           // fall back to an opaque bit-vector
      basket = { charters: [], fonds: [], archives: [], memo: "",
                 bitVector: ix.encodeMask(comp), bitHash: ix.indexHash, n };
    }
    this._data.baskets[name] = cleanBasket(basket);
    this._data.active = name;                                         // switch to the new basket
    await this._persist();
    return { ok: true, name, n, opaque: idCount > this.maxSize };
  }

  /** Compact-encode the ACTIVE basket against a shared-index container (raw `/basket/db`
   *  bytes) into a wire basket (id lists or a packed bit vector). The server decodes it with
   *  FSDBSharedIndex.receive_basket. See fsdb_sharedindex.js SharedIndex. */
  compactAgainst(dbBytes) {
    return this.compactNamed(this._data.active, dbBytes);
  }

  /** Compact-encode ANY basket by name (same wire shape as :meth:`compactAgainst`).
   *
   * Exists so a UI can offer a basket *chooser* -- exporting or scoping by a basket that is not
   * the active one -- WITHOUT switching the active basket as a side effect. The active basket is
   * shared across every service behind the gateway, so silently switching it to satisfy a local
   * "which basket do you want to export?" would change what every sibling page sees. */
  compactNamed(name, dbBytes) {
    const b = this._data.baskets[name];
    if (!b) throw new Error(`no such basket: ${name}`);
    const ix = SharedIndex.fromContainer(dbBytes);
    if (this.isAll(name)) return ix.sendBasket({ allCharters: true });   // whole Monasterium
    if (b.bitVector) return this.selectionOf(name);                      // opaque: ship its bit-vector as-is
    return ix.sendBasket({ charterIds: b.charters, fondIds: b.fonds, archiveIds: b.archives });
  }
}

// ---- UI ---------------------------------------------------------------------------------

const short = (s, n = 8) => (s && s.length > n ? s.slice(0, n) + "…" : (s || ""));
const fmtMB = (bytes) => (bytes / 1048576).toFixed(2);
const fmtTime = (ms) => (ms ? new Date(ms).toLocaleString() : "—"); // human-readable, to the second

/** Post a line to the global bottom message bar (base.html #ddp-msgbar); decoupled via a DOM
 *  event so this module needs no import of the bar. No-op if there is no bar/DOM. */
function logMsg(text) {
  try { document.dispatchEvent(new CustomEvent("ddp:message", { detail: { text } })); } catch { /* no DOM */ }
}

/** Tiny hyperscript helper. */
function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid);
  return e;
}

/** The widget: two stacked, independently-expandable frames over a BasketStore. Rebuilds
 *  its DOM on every store "change"; transient inputs (add-new, inline edit) are local swaps. */
export class BasketWidget {
  constructor(root, store, { current = null, baseUrl = "", indexNamespace = "" } = {}) {
    this.root = root;
    this.store = store;
    this.current = current;          // { type: "charters"|"fonds"|"archives", id } or null
    this.baseUrl = baseUrl;          // this service's owned path prefix (e.g. "/st"); "" = origin root
    this.indexNamespace = indexNamespace;  // per-service OPFS index-cache namespace (e.g. "st")
    this.TOP_KEY = "ddp:basket:top";
    this.BOT_KEY = "ddp:basket:bottom";
    this.DBG_KEY = "ddp:basket:debug";
  }

  mount() {
    injectStyle();
    this.root.classList.add("ddp-basket");
    // persistent chrome (survives re-render): a sync progress bar + the frames container
    this._syncbar = h("div", { class: "ddp-syncbar", hidden: true, title: "syncing shared index…" },
      h("div", { class: "ddp-syncfill" }));
    this._frames = h("div", { class: "ddp-frames" });
    this.root.append(this._syncbar, this._frames);
    this.store.addEventListener("change", () => this.render());
    document.addEventListener("ddp:debugchange", () => this.render());   // live console toggle
    this._initDrop();
    this.render();
    // published (not awaited): the syncer is created asynchronously, so anything that needs it --
    // the context rail's index row -- awaits this rather than racing an undefined `this.syncer`.
    this.syncerReady = this._initSyncer();
  }

  /** Make the whole widget a drop target for charter md5s (DRAG_TYPE payload): dropping adds the
   *  charter(s) to the active basket. Disabled while the reserved `All` basket is active (every
   *  charter is already in scope). */
  _initDrop() {
    const accepts = (e) => !this.store.isActiveAll
      && Array.from(e.dataTransfer ? e.dataTransfer.types : []).includes(DRAG_TYPE);
    this.root.addEventListener("dragover", (e) => {
      if (!accepts(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      this.root.classList.add("ddp-dragover");
    });
    this.root.addEventListener("dragleave", (e) => {
      if (!this.root.contains(e.relatedTarget)) this.root.classList.remove("ddp-dragover");
    });
    this.root.addEventListener("drop", async (e) => {
      const data = e.dataTransfer ? e.dataTransfer.getData(DRAG_TYPE) : "";
      if (!data) return;
      e.preventDefault();
      this.root.classList.remove("ddp-dragover");
      if (this.store.isActiveAll) { logMsg("'All' is active — switch to a basket to add charters."); return; }
      const ids = data.split(/\s+/).filter(Boolean);
      let added = 0;
      for (const id of ids) if (await this.store.add("charters", id)) added++;   // -> change -> render
      logMsg(added ? `Added ${added} charter(s) to '${this.store.activeName}'.`
                   : "Nothing added (already present, or basket full).");
    });
  }

  _expanded(key) { return localStorage.getItem(key) === "1"; }
  _setExpanded(key, on) { localStorage.setItem(key, on ? "1" : "0"); this.render(); }

  render() {
    const s = this.store;
    const dbg = debugOn();
    this.root.classList.toggle("ddp-scope-all", s.isActiveAll);  // CSS disables item toggles/edits
    this.root.classList.toggle("ddp-dev", dbg);                  // reveal .ddp-dev-only in the basket
    try { document.body.classList.toggle("ddp-debug", dbg); } catch { /* no DOM */ }  // tint regions/chrome
    this._frames.textContent = "";
    this._frames.append(this._topFrame(s), this._botFrame(s));
    if (dbg) this._frames.append(this._debugFrame(s));            // collapsible panel below the basket
  }

  /** Keep the shared-index cache in sync (poll every 3s). While a sync runs, the widget's
   *  controls (except the copy-to-clipboard buttons) are disabled and a progress bar shows. */
  async _initSyncer() {
    try {
      this.syncer = new SharedIndexSyncer({ baseUrl: this.baseUrl,
        store: await openStore(this.indexNamespace), blobStore: await openStore(),  // shared <hash>.fsdbidx
        intervalMs: CLIENT_CONFIG.sync_interval_ms, recheckMs: CLIENT_CONFIG.sync_recheck_ms,
        maxStorageBytes: CLIENT_CONFIG.max_storage_bytes });
      const onSync = () => { this._renderSync(); this._refreshDevInfo(); };
      this.syncer.addEventListener("statechange", onSync);
      this.syncer.addEventListener("syncprogress", onSync);
      this.syncer.addEventListener("confirm", onSync); // periodic re-confirm bumps confirmedAt only
      await this.syncer.init();
      this.syncer.start();
      this._renderSync();
    } catch {
      // no syncer (OPFS unavailable on an insecure origin like 0.0.0.0 / a LAN IP over HTTP, or no
      // /basket endpoint): render the UNSYNCED state so the panel goes red.
      this._renderSync();
    }
  }

  _renderSync() {
    const state = this.syncer ? this.syncer.state : null;
    const syncing = state === "syncing";
    // "unsynced" = the shared index is NOT confirmed in sync with the server -- red until the state
    // is exactly "synced" (covers no syncer / OPFS off, not-yet-confirmed, actively syncing, error).
    const unsynced = state !== "synced";
    this.root.classList.toggle("ddp-syncing", syncing);
    this.root.classList.toggle("ddp-unsynced", unsynced);
    this._syncbar.hidden = !syncing;
    if (!syncing) return;
    const p = this.syncer.progress;
    const fill = this._syncbar.firstChild;
    const pct = p && p.total ? Math.min(0.99, p.loaded / p.total) : null;
    this._syncbar.classList.toggle("indeterminate", pct == null);
    fill.style.width = pct == null ? "" : (pct * 100).toFixed(1) + "%";
  }

  // --- top frame: current basket name / all-baskets management ---
  _topFrame(s) {
    const open = this._expanded(this.TOP_KEY);
    // the active-basket combobox lives in the HEAD, so it stays usable even when collapsed
    const select = h("select", { class: "ddp-select", title: "Current basket",
      onchange: (e) => s.setActive(e.target.value) },
      ...s.names.map((n) => h("option", { value: n, ...(n === s.activeName ? { selected: true } : {}) }, n)));
    // combobox + copy-to-clipboard sit together on the LEFT; the toggle is pushed to the right.
    const head = h("div", { class: "ddp-frame-head" },
      h("span", { class: "ddp-basket-icon", text: "📋" }),
      select,
      h("button", { class: "ddp-copy-all", type: "button",
        title: "Copy ALL baskets as TSV — one column per basket (charters), name as header",
        onclick: (e) => flashCopy(e.currentTarget, s.allBasketsChartersTsv()) }, "▦"),
      h("button", { class: "ddp-export-all", type: "button",
        title: "Export ALL baskets to baskets.json.gz (unflattened)",
        onclick: () => this._exportBaskets() }, "⬇"),
      h("button", { class: "ddp-import-all", type: "button",
        title: "Restore baskets from an exported baskets.json.gz (merges; never overwrites)",
        onclick: () => this._importBaskets() }, "⬆"),
      h("span", { class: "ddp-head-spacer" }),
      h("button", { class: "ddp-toggle", type: "button", title: open ? "Hide baskets" : "Manage baskets",
        onclick: () => this._setExpanded(this.TOP_KEY, !open), text: open ? "▾" : "▸" }));

    if (!open) return h("div", { class: "ddp-frame ddp-frame-top" }, head);

    // when a charter/fond/archive is in view, each basket row gets a membership CHECK column
    // (green=all, orange=partial, red=none; All always green; a charter is binary). Filled async.
    const showChecks = !!this.current;
    const list = h("ul", { class: "ddp-basket-mgmt" + (showChecks ? " ddp-has-checks" : "") },
      ...s.names.map((n) => h("li", { class: s.isAll(n) ? "ddp-mgmt-all" : "", "data-basket": n },
        showChecks ? h("span", { class: "ddp-chk ddp-chk-wait", title: "checking…", text: "·" }) : null,
        h("span", { class: "ddp-mgmt-name" + (n === s.activeName ? " active" : ""),
          title: s.isAll(n) ? "Whole Monasterium — every charter (cannot be edited or deleted)" : n,
          text: short(n, 16) }),
        h("span", { class: "ddp-mgmt-count",
          text: s.isAll(n) ? "∞" : `(${s.counts(n).archives + s.counts(n).fonds + s.counts(n).charters})` }),
        h("button", { class: "ddp-del", type: "button",
          title: s.isAll(n) ? "The 'All' basket is permanent" : (s.canDelete(n) ? "Delete (empty)" : "Only empty, non-last baskets can be deleted"),
          disabled: !s.canDelete(n), onclick: () => s.delete(n), text: "×" }))));
    if (showChecks) this._fillMembership(list);

    const body = h("div", { class: "ddp-frame-body" },
      this._newBasketControl(s),
      list,
      this._devInfo(s));
    return h("div", { class: "ddp-frame ddp-frame-top" }, head, body);
  }

  /** Fill the per-basket membership checks (green all / orange partial / red none) of the viewed
   *  item against each basket. Async (needs the shared index); a single charter is binary. */
  async _fillMembership(listEl) {
    const cur = this.current;
    if (!cur) return;
    let ix;
    try { ix = await this._syncedIndex(); } catch { return; }
    const s = this.store;
    const item = cur.type === "fonds" ? { fond_ids: [cur.id] }
               : cur.type === "archives" ? { archive_ids: [cur.id] } : { charter_ids: [cur.id] };
    let itemPos;
    try { itemPos = [...ix.receiveBasket(s._itemSelection(item, ix.indexHash))]; } catch { return; }
    const total = itemPos.length;
    for (const li of listEl.querySelectorAll("li[data-basket]")) {
      const name = li.getAttribute("data-basket");
      const chk = li.querySelector(".ddp-chk");
      if (!chk) continue;
      let status;
      if (s.isAll(name) || total === 0) {
        status = s.isAll(name) ? "all" : "none";
      } else {
        let inB = 0;
        try {
          const bmask = ix.maskOf(s.selectionOf(name, ix.indexHash));
          for (const p of itemPos) if (bmask[p]) inB++;
        } catch { continue; }
        status = inB === 0 ? "none" : inB === total ? "all" : "partial";
      }
      chk.classList.remove("ddp-chk-wait", "ddp-chk-all", "ddp-chk-partial", "ddp-chk-none");
      chk.classList.add("ddp-chk-" + status);
      chk.textContent = status === "none" ? "✗" : "✓";
      chk.title = { all: "all in this basket", partial: "partially in this basket", none: "not in this basket" }[status];
    }
  }

  /** Refresh the dev-info block in place on every sync/confirm tick (without rebuilding the whole
   *  widget) so `sync:<state>` and especially the `confirmed <time>` stamp track the syncer.
   *  No-op when the top frame is collapsed or not in developer_mode (the block isn't in the DOM). */
  _refreshDevInfo() {
    const info = this._frames.querySelector(".ddp-dev-info");
    if (!info || !this.syncer) return;
    const idx = this.syncer.indexHash ? this.syncer.indexHash.slice(0, 8) : "—";
    info.firstChild.textContent =
      `dev · user:${CLIENT_CONFIG.user || DEFAULT_USER} · sync:${this.syncer.state} idx:${idx}`;
    this._fillDevStorage(info.querySelector(".ddp-dev-storage"));
  }

  /** A dev-only diagnostics block (hidden unless developer_mode): sync state, and the storage
   *  usage / timestamps (filled asynchronously since they read OPFS). Marked `.ddp-dev-only`. */
  _devInfo(s) {
    const idx = (this.syncer && this.syncer.indexHash) ? this.syncer.indexHash.slice(0, 8) : "—";
    const st = this.syncer ? this.syncer.state : "…";
    const el = h("div", { class: "ddp-dev-info ddp-dev-only", title: "developer_mode diagnostics" },
      h("div", {}, `dev · user:${CLIENT_CONFIG.user || DEFAULT_USER} · sync:${st} idx:${idx}`),
      h("div", { class: "ddp-dev-storage" }, "storage: …"));
    this._fillDevStorage(el.querySelector(".ddp-dev-storage"));
    return el;
  }

  async _fillDevStorage(line) {
    try {
      // meta (timestamps) is this service's; the size is the SHARED blob store (all deduped indexes).
      const [idxBytes, meta, basketsBytes] = await Promise.all([
        dirBytes(await openStore()), readMeta(await openStore(this.indexNamespace)),
        dirBytes(await openOpfsDir(OPFS_ROOT, "baskets")),
      ]);
      line.textContent =
        `shared index (all): ${fmtMB(idxBytes)} MB · stored ${fmtTime(meta && meta.updatedAt)} · `
        + `confirmed ${fmtTime(meta && meta.confirmedAt)} · all baskets: ${fmtMB(basketsBytes)} MB`;
    } catch {
      line.textContent = "storage: (unavailable)";
    }
  }

  // --- debug frame: the standardized views on this page + context + DnD + diagnostics + basket ---
  _debugFrame(s) {
    const open = this._expanded(this.DBG_KEY);
    const head = h("div", { class: "ddp-frame-head" },
      h("span", { class: "ddp-basket-icon", text: "🐞" }),
      h("span", { class: "ddp-dbg-title", text: "debug" }),
      h("span", { class: "ddp-head-spacer" }),
      h("button", { class: "ddp-toggle", type: "button", title: open ? "Hide debug" : "Show debug",
        onclick: () => this._setExpanded(this.DBG_KEY, !open), text: open ? "▾" : "▸" }));
    if (!open) return h("div", { class: "ddp-frame ddp-frame-debug" }, head);

    // standardized views declared on this page (ddp_views.js), with the current context flagged
    const ctx = viewedContext();
    const viewRows = [];
    for (const t of VIEW_TYPES) {
      const v = viewed(t);
      if (v === null) continue;
      const val = Array.isArray(v) ? `[${v.length}] ${short(v.join(" "), 22)}` : (short(v, 26) || "—");
      const isCtx = ctx && ctx.type === t;
      viewRows.push(h("div", { class: "ddp-dbg-view" + (isCtx ? " ctx" : "") },
        h("span", { class: "ddp-dbg-vt", text: t }), h("span", { class: "ddp-dbg-vv", title: Array.isArray(v) ? v.join(" ") : v, text: val }),
        isCtx ? h("span", { class: "ddp-dbg-ctx", text: "◀ context" }) : null));
    }
    if (!viewRows.length) viewRows.push(h("div", { class: "ddp-dbg-view", text: "(no standardized views on this page)" }));

    const syncLine = h("div", {}, `sync: ${this.syncer ? this.syncer.state : "—"} · `
      + `idx: ${(this.syncer && this.syncer.indexHash) ? this.syncer.indexHash.slice(0, 12) : "—"}`);
    const storageLine = h("div", { class: "ddp-dbg-storage" }, "storage: …");
    this._fillDevStorage(storageLine);

    const total = s.totalCount();
    const basketBlock = s.isActiveOpaque
      ? h("div", {}, `(opaque bit-vector basket · ${s.counts().charters} charters)`)
      : (total < 100 ? h("pre", { class: "ddp-dbg-json" }, JSON.stringify(s.active, null, 1))
                     : h("div", {}, `${total} items — too many to show (≥ 100)`));

    const section = (label, ...kids) => h("div", { class: "ddp-dbg-section" }, h("b", { text: label }), ...kids);
    const body = h("div", { class: "ddp-frame-body ddp-dbg-body" },
      section("views", ...viewRows),
      section("context", h("div", {}, ctx ? `${ctx.type} = ${Array.isArray(ctx.value) ? "[" + ctx.value.length + "]" : short(ctx.value, 26)}` : "(none)")),
      section("drag payload", h("div", {}, DRAG_TYPE)),
      section("sync", syncLine, storageLine),
      section(`active basket (${short(s.activeName, 16)})`, basketBlock));
    return h("div", { class: "ddp-frame ddp-frame-debug" }, head, body);
  }

  _newBasketControl(s) {
    const wrap = h("div", { class: "ddp-row ddp-newbasket" });
    const startBtn = h("button", { class: "ddp-new-btn", type: "button", text: "+ New…",
      onclick: () => openInput() });
    wrap.append(startBtn);
    const openInput = () => {
      const input = h("input", { class: "ddp-new-input", type: "text", placeholder: "basket name",
        maxlength: "40", oninput: () => { ok.disabled = !s.canUseName(input.value); } });
      const ok = h("button", { class: "ddp-ok", type: "button", disabled: true, text: "OK",
        onclick: async () => { if (await s.create(input.value)) { /* render via change */ } } });
      const cancel = h("button", { class: "ddp-cancel", type: "button", text: "Cancel",
        onclick: () => { wrap.textContent = ""; wrap.append(startBtn); } });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !ok.disabled) ok.click();
        else if (e.key === "Escape") cancel.click();
      });
      wrap.textContent = "";
      wrap.append(input, ok, cancel);
      input.focus();
    };
    return wrap;
  }

  // --- bottom frame: the current basket's own contents ---
  _botFrame(s) {
    const open = this._expanded(this.BOT_KEY);
    const all = s.isActiveAll;
    const c = s.counts();
    const inHere = this.current ? s.has(this.current.type, this.current.id) : false;
    const noun = this.current ? (SINGULAR[this.current.type] || "item") : "item";

    const head = h("div", { class: "ddp-frame-head" },
      h("button", { class: "ddp-add", type: "button", title: all ? "Every charter is already in scope" : `Add this ${noun}`,
        disabled: all || !this.current || inHere,
        onclick: () => this.current && s.add(this.current.type, this.current.id), text: "+" }),
      h("button", { class: "ddp-sub", type: "button",           // set-subtraction of the viewed item
        title: !this.current ? "No charter/fond/archive in view"
             : all ? `Create a basket: everything except this ${noun}`
             : s.isActiveOpaque ? "This basket is not editable"
             : `Subtract this ${noun} from '${s.activeName}'`,
        disabled: !this.current || (!all && (s.isActiveOpaque || s.isEmpty())),
        onclick: () => this._subtractContext(), text: "∖" }),
      h("span", { class: "ddp-head-spacer" }),
      h("span", { class: "ddp-counts", title: all ? "every charter" : "archives / fonds / charters" },
        all ? "∞ all" : `A${c.archives} F${c.fonds} C${c.charters}`),
      h("button", { class: "ddp-toggle", type: "button", title: open ? "Hide basket" : "Show basket",
        onclick: () => this._setExpanded(this.BOT_KEY, !open), text: open ? "▾" : "▸" }));

    if (!open) return h("div", { class: "ddp-frame ddp-frame-bottom" }, head);

    // the reserved 'All' basket has no editable contents -- explain, and offer a return path
    if (all) {
      const back = s.prevActiveName;
      const body = h("div", { class: "ddp-frame-body" },
        h("p", { class: "ddp-all-note" },
          "Whole Monasterium — every charter is in scope. Adding to baskets is disabled here."),
        (back && back !== ALL_BASKET)
          ? h("div", { class: "ddp-row" },
              h("button", { class: "ddp-back-btn", type: "button", title: `Switch back to '${back}'`,
                onclick: () => s.setActive(back), text: `↩ Back to '${short(back, 16)}'` }))
          : null);
      return h("div", { class: "ddp-frame ddp-frame-bottom" }, head, body);
    }

    const body = h("div", { class: "ddp-frame-body" },
      this._editableRow(s, "Name", () => s.activeName,
        (v) => s.rename(s.activeName, v), (v) => s.canUseName(v, s.activeName)),
      this._editableRow(s, "Memo", () => s.active.memo, (v) => s.setMemo(v), () => true),
      h("div", { class: "ddp-actions" },   // fixed, above the (scrolling) item list
        h("button", { class: "ddp-copy-col", type: "button", title: "Copy charter md5s to paste down a column",
          onclick: (e) => flashCopy(e.currentTarget, s.chartersColumn()) }, "Copy ▼"),
        h("button", { class: "ddp-copy-row", type: "button", title: "Copy charter md5s to paste across a row",
          onclick: (e) => flashCopy(e.currentTarget, s.chartersRow()) }, "Copy ▶"),
        h("button", { class: "ddp-export", type: "button", title: "Download the basket's charters",
          onclick: () => download("charters.basket", s.chartersFile()) }, "⬇"),
        h("button", { class: "ddp-flatten", type: "button",
          title: `Replace fonds & archives with their charters (only if under ${s.maxSize})`,
          onclick: (e) => this._flatten(e.currentTarget) }, "Flatten"),
        h("button", { class: "ddp-unflatten", type: "button",
          title: "Compress charters back into whole fonds & archives (inverse of Flatten)",
          onclick: (e) => this._unflatten(e.currentTarget) }, "Unflatten"),
        h("button", { class: "ddp-clear-basket", type: "button", title: "Empty this basket",
          disabled: s.isEmpty(), onclick: () => this._clear() }, "Clear")),
      h("div", { class: "ddp-items-scroll" }, ...this._itemsBody(s)));
    return h("div", { class: "ddp-frame ddp-frame-bottom" }, head, body);
  }

  /** The shared index (synced from /basket/db, OPFS-cached) used by flatten/unflatten/subtract. */
  async _syncedIndex() {
    const bytes = await syncIndex(this.baseUrl, { store: await openStore(this.indexNamespace),
      blobStore: await openStore(), deserialize: (b) => b });
    return SharedIndex.fromContainer(bytes);
  }

  // ---- export / restore of ALL baskets (fsdb_basket_io.js) --------------------------------
  // Both run through runBlocking, so the whole context rail is covered by a moving progress
  // signal and an Abort button for as long as they take (the index load is the slow part).

  /** Download every editable basket as one gzipped JSON, UNFLATTENED (archives/fonds stay as
   *  archives/fonds). The index hash travels with the file so a later restore can tell whether it
   *  is looking at the same database. */
  async _exportBaskets() {
    try {
      const bytes = await runBlocking(async ({ signal, status }) => {
        status("reading the shared index hash…");
        let indexHash = null;
        try { indexHash = (await readMeta(await openStore(this.indexNamespace)) || {}).index_hash || null; }
        catch { /* no cached index: export without a hash rather than failing the export */ }
        throwIfAborted(signal);
        status("compressing…");
        return exportBytes(this.store, { indexHash });
      }, { label: "exporting baskets" });

      const url = URL.createObjectURL(new Blob([bytes], { type: "application/gzip" }));
      const a = h("a", { href: url, download: EXPORT_FILENAME });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      logMsg(`Exported ${this.store.editableNames.length} basket(s) to ${EXPORT_FILENAME}`
             + ` (${fmtMB(bytes.length)} MB).`);
    } catch (e) {
      logMsg(isAbort(e) ? "Export aborted." : "Export failed — " + ((e && e.message) || e));
    }
  }

  /** Restore from a file the user picks. Merges (never overwrites; collisions are renamed), and
   *  WARNS in an alert box when ids in the file are absent from the database this service loaded --
   *  the DB may have been re-sliced or re-cut since the export. */
  async _importBaskets() {
    const input = h("input", { type: "file", accept: ".gz,.json,application/gzip,application/json",
                               style: "display:none" });
    document.body.append(input);
    const file = await new Promise((resolve) => {
      input.addEventListener("change", () => resolve(input.files && input.files[0]), { once: true });
      input.addEventListener("cancel", () => resolve(null), { once: true });
      input.click();
    });
    input.remove();
    if (!file) return;

    try {
      const out = await runBlocking(async ({ signal, status }) => {
        status(`reading ${file.name}…`);
        const parsed = await parseBasketsFile(new Uint8Array(await file.arrayBuffer()));
        throwIfAborted(signal);
        const names = Object.keys(parsed.baskets);

        // validate against the CURRENT index; without one, import unvalidated rather than refuse
        let warning = null;
        try {
          status("loading the shared index…");
          const ix = await this._syncedIndex();
          throwIfAborted(signal);
          status(`checking ids against the database…`);
          const result = validateAgainstIndex(parsed.baskets, ix, { signal });
          warning = missingWarning(result, { indexHash: ix.index_hash, fileIndexHash: parsed.indexHash });
        } catch (e) {
          if (isAbort(e)) throw e;
          warning = `The baskets could not be checked against the database (${(e && e.message) || e}).`
                  + "\nThey were imported unchecked; some ids may not exist here.";
        }

        status(`importing ${names.length} basket(s)…`);
        const renamed = await this.store.importBaskets(parsed.baskets);
        return { renamed, warning, count: names.length };
      }, { label: "restoring baskets" });

      // outside runBlocking: alert() is modal, so never raise it under the overlay
      if (out.warning) alert(out.warning);
      const collisions = Object.entries(out.renamed).filter(([from, to]) => from !== to);
      logMsg(`Imported ${out.count} basket(s) from ${file.name}.`
             + (collisions.length ? " Renamed: " + collisions.map(([f, t]) => `${f} → ${t}`).join(", ") : ""));
    } catch (e) {
      logMsg(isAbort(e) ? "Import aborted — nothing was changed." : "Import failed — " + ((e && e.message) || e));
    }
  }

  /** Flatten the active basket (fonds/archives → charters) via the shared index's flatten(). The
   *  basket UI is disabled (`.ddp-busy`) until it completes; the outcome goes to the message bar. */
  async _flatten(btn) {
    const c = this.store.counts(), name = this.store.activeName;
    if (!c.fonds && !c.archives) { flash(btn, "nothing"); logMsg(`Flatten '${name}': nothing to resolve.`); return; }
    this.root.classList.add("ddp-busy"); btn.textContent = "…";
    try {
      const r = await this.store.flatten(await this._syncedIndex());
      logMsg(r.ok ? `Flattened '${name}' → ${r.count} charters.`
                  : `Flatten '${name}' skipped: ${r.count} charters ≥ limit ${r.limit}.`);
    } catch (e) { logMsg(`Flatten '${name}' failed: ${e && e.message ? e.message : e}`); }
    finally { this.root.classList.remove("ddp-busy"); btn.textContent = "Flatten"; }
  }

  /** Unflatten the active basket (charters → the minimal archives/fonds/charters) via unflatten(). */
  async _unflatten(btn) {
    const name = this.store.activeName;
    this.root.classList.add("ddp-busy"); btn.textContent = "…";
    try {
      const r = await this.store.unflatten(await this._syncedIndex());
      if (r.ok) logMsg(`Unflattened '${name}' → ${r.archives} archives, ${r.fonds} fonds, ${r.charters} charters.`);
    } catch (e) { logMsg(`Unflatten '${name}' failed: ${e && e.message ? e.message : e}`); }
    finally { this.root.classList.remove("ddp-busy"); btn.textContent = "Unflatten"; }
  }

  /** Set-subtraction of the viewed item (charter/fond/archive). On an editable basket it removes
   *  the item's charters (flatten-aware). On the reserved 'All' basket -- which is immutable -- it
   *  instead creates a NEW basket = the whole DB MINUS the item (prompting for a name + confirming
   *  the charter count), stored compressed (or as an opaque bit-vector when too large). */
  async _subtractContext() {
    const s = this.store, ctx = this.current;
    if (!ctx) return;
    const noun = SINGULAR[ctx.type] || "item";
    const item = ctx.type === "fonds" ? { fond_ids: [ctx.id] }
               : ctx.type === "archives" ? { archive_ids: [ctx.id] } : { charter_ids: [ctx.id] };
    this.root.classList.add("ddp-busy");
    try {
      const ix = await this._syncedIndex();
      if (s.isActiveAll) {                                       // complement -> a NEW basket
        const itemCount = ix.flatten(s._itemSelection(item, ix.indexHash)).charter_ids.length;
        const n = ix.length - itemCount;
        const name = (window.prompt(`New basket = everything except this ${noun}.\nName:`, "") || "").trim();
        if (!name) return;
        if (!s.canUseName(name)) { logMsg(`'${name}' is not a valid basket name.`); return; }
        if (!window.confirm(`Create '${name}' with ${n} charters (the whole DB minus this ${noun})?`)) return;
        const r = await s.createComplement(ix, item, name);
        if (r.ok) logMsg(`Created '${r.name}' — ${r.n} charters${r.opaque ? " (stored as a bit-vector)" : ""}.`);
        return;
      }
      const name = s.activeName;
      const flat = new Set(ix.flatten(s.selectionOf(name, ix.indexHash)).charter_ids);
      if (flat.size >= s.maxSize) { logMsg(`Cannot subtract from '${name}': flattening reaches the ${s.maxSize} limit.`); return; }
      const itemCharters = ix.flatten(s._itemSelection(item, ix.indexHash)).charter_ids;
      const n = itemCharters.filter((c) => flat.has(c)).length;
      if (n === 0) { logMsg(`No charters of this ${noun} are in '${name}'.`); return; }
      const c = s.counts();
      if ((c.fonds || c.archives) && !window.confirm(`Subtract ${n} charters of this ${noun} from '${name}'? This flattens the basket.`)) return;
      const r = await s.subtract(ix, item);
      if (r.ok) logMsg(`Subtracted ${r.removed} charters of this ${noun} from '${name}' (${r.count} remain).`);
    } catch (e) { logMsg(`Subtract failed: ${e && e.message ? e.message : e}`); }
    finally { this.root.classList.remove("ddp-busy"); }
  }

  /** Empty the active basket after an "are you sure" confirmation reporting what will go. */
  async _clear() {
    const s = this.store, name = s.activeName, c = s.counts();
    const parts = [];
    if (c.charters) parts.push(`${c.charters} charters`);
    if (c.fonds) parts.push(`${c.fonds} fonds`);
    if (c.archives) parts.push(`${c.archives} archives`);
    if (!parts.length) return;
    if (!window.confirm(`Erase ${parts.join(", ")} from basket "${name}"? This cannot be undone.`)) return;
    await s.clear();   // -> "change" -> render
    logMsg(`Cleared basket '${name}' (${parts.join(", ")}).`);
  }

  /** The bottom-frame item area. Rendering thousands of item rows janks the whole widget
   *  (every add re-renders them all), so above `max_rendered_items` we show a short summary
   *  (unique charter / fond / archive counts) instead of the per-item list. */
  _itemsBody(s) {
    if (s.isEmpty()) return [h("p", { class: "ddp-empty", text: "Basket is empty." })];
    const total = s.totalCount();
    const limit = CLIENT_CONFIG.max_rendered_items ?? 500;
    if (total > limit) {
      const c = s.counts();
      return [h("p", { class: "ddp-basket-summary", title: "item list hidden for speed" },
        `${c.charters} unique charters · ${c.fonds} unique fonds · ${c.archives} unique archives `
        + `(${total} items; list hidden above ${limit} — use Copy/Download/Clear or a fond/archive page)`)];
    }
    return TYPES.filter((t) => s.active[t].length).map((t) => this._typeRow(s, t));
  }

  _typeRow(s, type) {
    const items = s.active[type].map((id) => h("span", { class: "ddp-item" },
      h("a", { class: "ddp-item-link", href: LINK[type](id), title: id, text: short(id, 10) }),
      h("button", { class: "ddp-item-rm", type: "button", title: "Remove", onclick: () => s.remove(type, id), text: "×" })));
    return h("div", { class: "ddp-typerow" },
      h("span", { class: "ddp-typelabel", text: TYPE_LABEL[type] + ":" }),
      h("span", { class: "ddp-items" }, ...items));
  }

  /** A "Label: value ✎" row whose value becomes an inline input on click. `commit(v)` returns
   *  a truthy/Promise when accepted; `valid(v)` gates it live. */
  _editableRow(s, label, get, commit, valid) {
    const val = h("span", { class: "ddp-editable", title: get() || "(click to edit)",
      text: short(get()) || "—" });
    val.addEventListener("click", () => {
      const input = h("input", { class: "ddp-edit-input", type: "text", value: get(), maxlength: "80" });
      let done = false;
      const finish = async (save) => {
        if (done) return;                                          // guard Enter+blur double-fire
        done = true;
        if (save && valid(input.value)) await commit(input.value); // -> change -> render
        else this.render();                                        // revert
      };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") finish(true);
        else if (e.key === "Escape") finish(false);
      });
      input.addEventListener("blur", () => finish(true));
      val.replaceWith(input);
      input.focus(); input.select();
    });
    return h("div", { class: "ddp-row ddp-editrow" }, h("label", { text: label + ":" }), val,
      h("span", { class: "ddp-edit-hint", title: "click to edit", text: "✎" }));
  }
}

async function flashCopy(btn, text) {
  try {
    await navigator.clipboard.writeText(text);
    flash(btn, "✓", 900);
  } catch { /* clipboard blocked */ }
}

function flash(btn, text, ms = 1100) {
  const prev = btn.textContent; btn.textContent = text;
  setTimeout(() => { btn.textContent = prev; }, ms);
}

/** Build fond->charters / archive->charters lookups from deserialised shared-index `blocks`
 *  (the output of fsdb_sharedindex.js deserializeDb). Pure -- no network/DOM. */
export function buildResolver(blocks) {
  const charter = blocks.charter_id.values, fond = blocks.fond_id.values, archive = blocks.archive_id.values;
  const c2f = blocks.charter_to_fond_idx.values, f2a = blocks.fond_to_archive_idx.values;
  const fondToCharters = new Map(), archiveToCharters = new Map();
  const push = (m, k, v) => { let a = m.get(k); if (!a) m.set(k, a = []); a.push(v); };
  for (let c = 0; c < charter.length; c++) {
    const fi = c2f[c];
    push(fondToCharters, fond[fi], charter[c]);
    push(archiveToCharters, archive[f2a[fi]], charter[c]);
  }
  return { chartersOfFond: (m) => fondToCharters.get(m) || [],
           chartersOfArchive: (i) => archiveToCharters.get(i) || [] };
}

function download(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/tab-separated-values" }));
  const a = h("a", { href: url, download: filename });
  document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}

let _styled = false;
function injectStyle() {
  if (_styled || document.getElementById("ddp-basket-style")) { _styled = true; return; }
  _styled = true;
  document.head.append(h("style", { id: "ddp-basket-style", text: CSS }));
}

const CSS = `
/* The widget is a block in the context rail (#ctx_nav, base.html), NOT the floating overlay it used
   to be: the rail owns the position, the width (user-resizable) and the scrolling. */
.ddp-basket { font: 13px/1.4 system-ui, sans-serif; color: CanvasText; }
.ddp-basket .ddp-frame { border: 1px solid rgba(128,128,128,.5); background: Canvas;
  box-shadow: 0 2px 10px rgba(0,0,0,.2); }
.ddp-basket .ddp-frame-top { border-radius: .4rem .4rem 0 0; border-bottom: none; }
.ddp-basket .ddp-frame-bottom { border-radius: 0 0 .4rem .4rem; }
/* shared index NOT in sync (no syncer / OPFS off on an insecure origin, offline, or error):
   tint the all-baskets (top) panel red so the state is obvious. */
.ddp-basket.ddp-unsynced .ddp-frame-top { background: rgba(210, 70, 70, .18); }
.ddp-basket.ddp-unsynced .ddp-frame-top .ddp-basket-icon::after { content: " ⚠"; }
.ddp-basket .ddp-frame-head { display: flex; align-items: center; gap: .3rem; padding: .3rem .4rem; }
/* combobox + copy stay LEFT (natural width); the toggle is pushed to the far RIGHT of each head */
.ddp-basket .ddp-frame-head .ddp-select { flex: 0 1 9rem; min-width: 0; }
.ddp-basket .ddp-frame-head .ddp-copy-all { padding: 0 .4rem; }
.ddp-basket .ddp-counts { font-variant-numeric: tabular-nums;
  background: rgba(128,128,128,.2); border-radius: .8rem; padding: .05rem .4rem; }
.ddp-basket button { cursor: pointer; border: 1px solid rgba(128,128,128,.5); background: transparent;
  color: inherit; border-radius: .3rem; line-height: 1; }
.ddp-basket button:disabled { opacity: .35; cursor: default; }
.ddp-basket .ddp-toggle, .ddp-basket .ddp-add, .ddp-basket .ddp-sub { width: 1.6rem; height: 1.6rem; font-size: 1rem; }
.ddp-basket .ddp-head-spacer { flex: 1 1 auto; }   /* pushes the toggle (and counts) to the right */
.ddp-basket .ddp-frame-body { padding: .4rem; border-top: 1px solid rgba(128,128,128,.3); }
.ddp-basket .ddp-items-scroll { max-height: 38vh; overflow: auto; margin-top: .35rem;
  border-top: 1px solid rgba(128,128,128,.2); padding-top: .3rem; }
.ddp-basket .ddp-row { display: flex; align-items: center; gap: .35rem; margin: .2rem 0; }
.ddp-basket .ddp-row > label { opacity: .7; min-width: 3.4rem; }
.ddp-basket .ddp-select, .ddp-basket .ddp-new-input, .ddp-basket .ddp-edit-input {
  flex: 1; min-width: 0; font: inherit; color: inherit; background: Canvas;
  border: 1px solid rgba(128,128,128,.5); border-radius: .3rem; padding: .1rem .3rem; }
.ddp-basket .ddp-newbasket button { padding: .15rem .45rem; }
.ddp-basket .ddp-basket-mgmt { list-style: none; margin: .3rem 0 0; padding: 0; max-height: 38vh; overflow: auto; }
.ddp-basket .ddp-basket-mgmt li { display: flex; align-items: center; gap: .35rem; padding: .1rem 0; }
.ddp-basket .ddp-mgmt-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ddp-basket .ddp-mgmt-name.active { font-weight: 700; }
.ddp-basket .ddp-mgmt-count { opacity: .6; font-variant-numeric: tabular-nums; }
/* membership check column (shown when a charter/fond/archive is in view): green all / orange partial / red none */
.ddp-basket .ddp-has-checks .ddp-mgmt-name { text-align: left; }
.ddp-basket .ddp-chk { width: 1.1rem; text-align: center; font-weight: 700; flex: 0 0 auto; }
.ddp-basket .ddp-chk-wait { opacity: .4; font-weight: 400; }
.ddp-basket .ddp-chk-all { color: #2e9e2e; }
.ddp-basket .ddp-chk-partial { color: #e08a00; }
.ddp-basket .ddp-chk-none { color: #cc4646; }
.ddp-basket .ddp-del, .ddp-basket .ddp-item-rm { width: 1.3rem; height: 1.3rem; font-size: .9rem; }
.ddp-basket .ddp-editrow .ddp-editable { flex: 1; cursor: text; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-family: ui-monospace, monospace; }
.ddp-basket .ddp-edit-hint { opacity: .5; }
.ddp-basket .ddp-typerow { display: flex; gap: .35rem; margin: .25rem 0; align-items: baseline; }
.ddp-basket .ddp-typelabel { opacity: .7; min-width: 3.9rem; }
.ddp-basket .ddp-items { display: flex; flex-wrap: wrap; gap: .3rem; }
.ddp-basket .ddp-item { display: inline-flex; align-items: center; gap: .15rem;
  background: rgba(128,128,128,.15); border-radius: .3rem; padding: 0 .1rem 0 .3rem; }
.ddp-basket .ddp-item-link { font-family: ui-monospace, monospace; font-size: 12px; }
.ddp-basket .ddp-actions { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .5rem; }
.ddp-basket .ddp-actions button { padding: .2rem .5rem; }
.ddp-basket .ddp-clear-basket { border-color: rgba(200,70,70,.6); color: rgb(200,70,70); }
.ddp-basket .ddp-empty { opacity: .7; margin: .3rem; }
.ddp-basket .ddp-basket-summary { opacity: .8; margin: .4rem .3rem; font-style: italic; line-height: 1.4; }
/* developer_mode: elements marked .ddp-dev-only are hidden unless the root has .ddp-dev */
.ddp-basket:not(.ddp-dev) .ddp-dev-only { display: none !important; }
.ddp-basket .ddp-dev-info { margin-top: .4rem; padding-top: .3rem; border-top: 1px dashed rgba(128,128,128,.4);
  font-family: ui-monospace, monospace; font-size: 11px; opacity: .6; word-break: break-all; }
/* debug panel (below the basket, when debug mode is on) */
.ddp-basket .ddp-frame-debug { border-radius: 0 0 .4rem .4rem; margin-top: .25rem; }
.ddp-basket .ddp-dbg-title { font-weight: 600; }
.ddp-basket .ddp-dbg-body { font: 11px/1.4 ui-monospace, monospace; max-height: 42vh; overflow: auto; }
.ddp-basket .ddp-dbg-section { margin: .3rem 0; border-top: 1px dashed rgba(128,128,128,.35); padding-top: .25rem; }
.ddp-basket .ddp-dbg-section b { display: block; opacity: .55; text-transform: uppercase; font-size: 10px; letter-spacing: .04em; }
.ddp-basket .ddp-dbg-view { display: flex; gap: .35rem; align-items: baseline; }
.ddp-basket .ddp-dbg-view.ctx { font-weight: 700; }
.ddp-basket .ddp-dbg-vt { min-width: 6.8rem; opacity: .8; }
.ddp-basket .ddp-dbg-vv { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.ddp-basket .ddp-dbg-ctx { color: #2e9e2e; font-size: 10px; }
.ddp-basket .ddp-dbg-json { white-space: pre-wrap; word-break: break-all; margin: .2rem 0; opacity: .85; }
/* debug region + chrome tints (toggled on <body>); view_marker regions are hidden so only the
   visible view_region boxes show colour. */
body.ddp-debug .ddp-view { outline: 1px dashed rgba(120,120,120,.5); }
body.ddp-debug #viewed_root { background: rgba(59,130,246,.12); }
body.ddp-debug #viewed_archive { background: rgba(147,51,234,.12); }
body.ddp-debug #viewed_fond { background: rgba(20,184,166,.13); }
body.ddp-debug #viewed_charter { background: rgba(34,197,94,.12); }
body.ddp-debug #viewed_charter_list { background: rgba(234,140,0,.12); }
body.ddp-debug #viewed_charter_ranking { background: rgba(220,60,60,.11); }
body.ddp-debug #viewed_other { background: rgba(128,128,128,.16); }
body.ddp-debug .topnav { outline: 2px solid rgba(59,130,246,.55); outline-offset: -2px; }
body.ddp-debug .content { outline: 2px solid rgba(34,197,94,.45); outline-offset: -2px; }
body.ddp-debug .ddp-msgbar { outline: 2px solid rgba(234,140,0,.55); }
/* shared-index sync progress bar */
.ddp-basket .ddp-syncbar { height: 4px; background: rgba(128,128,128,.25); border-radius: 2px 2px 0 0; overflow: hidden; }
.ddp-basket .ddp-syncfill { height: 100%; width: 0; background: #3b82f6; transition: width .15s linear; }
.ddp-basket .ddp-syncbar.indeterminate .ddp-syncfill { width: 40%; animation: ddp-indet 1.1s ease-in-out infinite; }
@keyframes ddp-indet { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
/* while syncing, disable every control EXCEPT the copy-to-clipboard buttons */
.ddp-basket.ddp-syncing .ddp-frames button:not(.ddp-copy-col):not(.ddp-copy-row):not(.ddp-copy-all),
.ddp-basket.ddp-syncing .ddp-frames select,
.ddp-basket.ddp-syncing .ddp-frames input,
.ddp-basket.ddp-syncing .ddp-frames .ddp-editable { pointer-events: none; opacity: .45; }
/* while flattening, disable the whole basket until it completes */
.ddp-basket.ddp-busy .ddp-frames button,
.ddp-basket.ddp-busy .ddp-frames select,
.ddp-basket.ddp-busy .ddp-frames input,
.ddp-basket.ddp-busy .ddp-frames .ddp-editable { pointer-events: none; opacity: .45; }
/* reserved 'All' basket active: item-add/remove is meaningless (everything is in scope) */
.ddp-basket.ddp-scope-all .ddp-add,
.ddp-basket.ddp-scope-all .ddp-sub { pointer-events: none; opacity: .35; }
.ddp-basket .ddp-mgmt-all .ddp-mgmt-name { font-style: italic; }
.ddp-basket .ddp-all-note { opacity: .8; margin: .3rem; font-style: italic; line-height: 1.4; }
.ddp-basket .ddp-back-btn { padding: .2rem .5rem; }
/* drag-and-drop: highlight the widget while a charter md5 is dragged over it */
.ddp-basket.ddp-dragover .ddp-frame { outline: 2px dashed #3b82f6; outline-offset: -2px; }
`;

/** This service's owned path prefix, injected by base.html as `<meta name="ddp-base">` (e.g.
 *  "/st"); "" at the origin root / in tests. Used to reach /basket & co. behind a gateway. */
export function ddpBase() {
  if (typeof document === "undefined") return "";
  const m = document.querySelector('meta[name="ddp-base"]');
  return (m && m.content) || "";
}

/** Convenience: open the store and mount the widget on `root`. `current` is the entity this
 *  page is about -- `{ type: "charters"|"fonds"|"archives", id }` -- whose +/- toggle it in
 *  the active basket. `baseUrl` defaults to the injected ddp-base (its trailing segment is the
 *  per-service OPFS index-cache namespace). */
export async function mountBasketWidget(root, { current = null, store = null, baseUrl = null } = {}) {
  await loadClientConfig();                 // populate CLIENT_CONFIG (the single source of truth)
  const bstore = await BasketStore.open({ store, maxSize: CLIENT_CONFIG.max_basket_size });
  // the "current entity" comes from the page's standardized context view (ddp_views.js);
  // an explicit `current` override is accepted (tests / non-standard hosts).
  const cur = (current && SINGULAR[current.type] && current.id) ? current : currentFromContext();
  const base = baseUrl != null ? baseUrl : ddpBase();
  const widget = new BasketWidget(root, bstore,
    { current: cur, baseUrl: base, indexNamespace: base.replace(/^\//, "") });
  widget.mount();
  return widget;
}
