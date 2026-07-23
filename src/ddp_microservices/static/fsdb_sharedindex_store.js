// fsdb_sharedindex_store.js -- persistent client-side cache for the shared index.
//
// The server-side index is IMMUTABLE for the life of a slice, so a client keeps a local
// copy in OPFS (Origin Private File System -- a sandboxed, origin-private, binary-capable
// browser filesystem) and only re-checks the server once per `maxAgeMs` (default 24h).
// Within that window it works fully offline; after it, one tiny GET /basket confirms the
// hash, and the big GET /basket/db runs only when the slice actually changed.
//
// Pure cache: everything is always recoverable from GET /basket/db, so eviction is safe.
// The storage backend and fetch are INJECTABLE -- production uses real OPFS + fetch;
// tests pass an in-memory directory handle and a stub fetch (there is no OPFS in Node).
//
// Dependency-free ES module; pairs with fsdb_sharedindex.js (pass its deserializeDb as
// `deserialize`). Web-standard APIs only.

const META = "meta.json";
const DAY_MS = 24 * 60 * 60 * 1000;

// All DDP OPFS data lives under one top-level directory, so it is namespaced apart from any
// other code sharing this origin's OPFS and is trivial to inspect / wipe. (OPFS is already
// isolated per-origin by the browser; this is the intra-origin namespace.)
export const OPFS_ROOT = "DDP-VRE";

/**
 * Open (creating if needed) a nested OPFS directory under the origin root, requesting
 * persistent storage so the browser is less likely to evict it. Browser-only.
 * e.g. openOpfsDir("DDP-VRE", "baskets", "default_user"). @returns {Promise<FileSystemDirectoryHandle>}
 */
export async function openOpfsDir(...segments) {
  const root = await navigator.storage.getDirectory();
  if (navigator.storage.persist) {
    try { await navigator.storage.persist(); } catch { /* best effort */ }
  }
  let dir = root;
  for (const seg of segments) dir = await dir.getDirectoryHandle(seg, { create: true });
  return dir;
}

/** The shared-index cache directory: `DDP-VRE/shared_index[/<namespace>]`.
 *
 *  Layout (dedup): the big index containers are stored ONCE by content hash at the shared root
 *  `DDP-VRE/shared_index/<hash>.fsdbidx` (the **blob store**, `openStore()` with no namespace), so
 *  services on the SAME slice reuse one copy behind a single-origin gateway. Each service keeps only
 *  its own tiny `meta.json` in its namespace dir `DDP-VRE/shared_index/<prefix>/` (`openStore(prefix)`),
 *  which references a blob by hash. `syncIndex`/`SharedIndexSyncer` take both (`store` = the meta dir,
 *  `blobStore` = the shared root); `gcBlobs` deletes a blob only when no meta references its hash.
 *  Baskets are separate and stay shared (index-independent). */
export async function openStore(namespace = "") {
  return namespace ? openOpfsDir(OPFS_ROOT, "shared_index", namespace)
                   : openOpfsDir(OPFS_ROOT, "shared_index");
}

/** Total bytes stored under an OPFS directory (recursively sums file sizes). */
export async function dirBytes(dir) {
  let total = 0;
  for await (const [, handle] of dir.entries()) {
    if (handle.kind === "directory") total += await dirBytes(handle);
    else total += (await handle.getFile()).size;
  }
  return total;
}

async function readBytes(store, name) {
  try {
    const fh = await store.getFileHandle(name);
    return await (await fh.getFile()).arrayBuffer();
  } catch {
    return null; // NotFoundError -> cache miss
  }
}

async function writeBytes(store, name, data) {
  const fh = await store.getFileHandle(name, { create: true });
  const w = await fh.createWritable();
  await w.write(data);
  await w.close();
}

/** Garbage-collect shared index blobs (`<hash>.fsdbidx`) in `blobStore` that NO service still
 *  references. Behind a single-origin gateway several services share one blob dir; a blob is safe
 *  to delete only when no `meta.json` (in the blob dir itself, or in any per-service subdirectory)
 *  points at its hash. `keep` protects a just-written hash before its meta is read back. A blob
 *  modified within `graceMs` is spared (another tab/service may be mid-download of it). Also prunes
 *  stray `.fsdbidx` files left inside per-service subdirs by the OLD (pre-dedup) layout. */
async function gcBlobs(blobStore, { keep = null, now = Date.now, graceMs = 60_000 } = {}) {
  const referenced = new Set(keep ? [keep] : []);
  const blobNames = [];
  const subdirs = [];
  for await (const [name, handle] of blobStore.entries()) {
    if (handle && handle.kind === "directory") subdirs.push(handle);
    else if (name.endsWith(".fsdbidx")) blobNames.push(name);
  }
  const addRef = async (dir) => { const m = await readMeta(dir); if (m && m.index_hash) referenced.add(m.index_hash); };
  await addRef(blobStore);
  for (const d of subdirs) {
    await addRef(d);
    // old layout stored blobs inside the per-service dir; they are unused now -> prune them.
    for await (const [n] of d.entries()) { if (n.endsWith(".fsdbidx")) { try { await d.removeEntry(n); } catch { /* ignore */ } } }
  }
  for (const name of blobNames) {
    if (referenced.has(name.slice(0, -".fsdbidx".length))) continue;
    try {
      const f = await (await blobStore.getFileHandle(name)).getFile();
      if (now() - f.lastModified < graceMs) continue;   // recently written elsewhere -> keep
    } catch { /* no mtime (mock) -> fall through and delete */ }
    try { await blobStore.removeEntry(name); } catch { /* ignore */ }
  }
}

export async function readMeta(store) {
  const ab = await readBytes(store, META);
  if (!ab) return null;
  try { return JSON.parse(new TextDecoder().decode(ab)); } catch { return null; }
}

async function writeMeta(store, meta) {
  await writeBytes(store, META, new TextEncoder().encode(JSON.stringify(meta)));
}

/**
 * Return the deserialised shared index, using the local OPFS cache and re-checking the
 * server at most once per `maxAgeMs`. The result is whatever `deserialize(ArrayBuffer)`
 * returns (pass fsdb_sharedindex.js's deserializeDb).
 *
 * @param {string} baseUrl                       service origin, e.g. "" or "https://host:port"
 * @param {Object} opts
 * @param {FileSystemDirectoryHandle} opts.store  OPFS dir from openStore() (or a mock)
 * @param {Function} opts.deserialize            (ArrayBuffer) => index
 * @param {Function} [opts.fetchFn=fetch]         injectable fetch
 * @param {number}   [opts.maxAgeMs=86400000]     re-check interval (default 24h)
 * @param {Function} [opts.now=Date.now]          injectable clock
 */
export async function syncIndex(baseUrl, { store, blobStore = store, deserialize, fetchFn = fetch,
                                           maxAgeMs = DAY_MS, now = Date.now } = {}) {
  // `store` holds this service's meta.json; `blobStore` holds the shared `<hash>.fsdbidx` blobs
  // (dedup: services on the same slice share one blob). Default blobStore=store = legacy same-dir.
  const meta = await readMeta(store);
  const confirmedAt = meta ? (meta.confirmedAt ?? meta.checkedAt) : undefined; // (legacy fallback)
  if (meta && (now() - confirmedAt) < maxAgeMs) {
    const ab = await readBytes(blobStore, meta.name);
    if (ab) return deserialize(ab); // within the window and cached -> zero network
  }

  // stale or missing: confirm the current hash with the tiny manifest
  const manifest = await (await fetchFn(`${baseUrl}/basket`)).json();
  const name = `${manifest.index_hash}.fsdbidx`;
  let ab = await readBytes(blobStore, name);            // maybe already downloaded by a sibling service
  let downloaded = false;
  if (!ab) {
    ab = await (await fetchFn(`${baseUrl}/basket/db`)).arrayBuffer();
    await writeBytes(blobStore, name, ab);
    downloaded = true;
  }
  await writeMeta(store, { name, index_hash: manifest.index_hash,
    updatedAt: downloaded ? now() : ((meta && meta.updatedAt) || now()), confirmedAt: now() });
  await gcBlobs(blobStore, { keep: manifest.index_hash, now }); // drop blobs no service references
  return deserialize(ab);
}

function _concatChunks(chunks) {
  let len = 0;
  for (const c of chunks) len += c.length;
  const out = new Uint8Array(len);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}

/**
 * Keeps the local OPFS shared-index cache in sync with the server. Polls `GET /basket` every
 * `intervalMs`; when the server's index_hash differs from the cached one, downloads
 * `GET /basket/db` (streamed, with progress) and refreshes the cache (pruning the old slice).
 *
 * The OPFS meta records two timestamps: `updatedAt` (when the index bytes were last
 * downloaded) and `confirmedAt` (when the hash was last confirmed against the server). A
 * poll is THROTTLED on the shared `confirmedAt`: the timer fires every `intervalMs` (3s), but
 * only actually hits the server when `confirmedAt` is at least `recheckMs` (5s) old. Since
 * `confirmedAt` lives in OPFS, the throttle is shared across reloads and tabs.
 *
 * `maxStorageBytes` caps what we keep on disk: an index container larger than the budget is
 * NOT persisted (the hash is still recorded so we don't re-download every tick; Flatten
 * fetches it on demand). It limits disk, not bandwidth.
 *
 * Dispatches plain Events "statechange" and "syncprogress"; read the public fields:
 *   - `state`     : "unknown" | "synced" | "syncing" | "error"
 *   - `indexHash` : the currently-cached index hash (or null)
 *   - `progress`  : during a sync, `{ loaded, total }` (total is an estimate); else null
 *
 * The storage backend, fetch and clock are INJECTABLE (production: real OPFS + fetch; tests
 * pass a mock store, a stub fetch and a fake clock). Web-standard APIs only.
 */
/** A thrown value as a one-line string (Error, DOMException or anything else). */
function _msg(e) { return String((e && e.message) || e); }


export class SharedIndexSyncer extends EventTarget {
  // `intervalMs`/`recheckMs` are REQUIRED (no defaults): default_client_config.json is the single
  // source of truth for those timings (sync_interval_ms / sync_recheck_ms), so callers must pass
  // them. `maxStorageBytes` defaults to Infinity meaning "no cap" -- a disabled sentinel, not a
  // duplicate of the config value. `baseUrl`/`fetchFn`/`now` are dependency-injection seams.
  constructor({ baseUrl = "", store, blobStore = store, fetchFn = fetch, intervalMs, recheckMs,
                maxStorageBytes = Infinity, now = Date.now } = {}) {
    super();
    this.baseUrl = baseUrl;
    this.store = store;          // this service's meta.json
    this.blobStore = blobStore;  // shared <hash>.fsdbidx blobs (defaults to store = legacy same-dir)
    // NEVER store the browser's global `fetch` as a method. `this.fetchFn(url)` would invoke it
    // with `this === syncer`, and the browser refuses: "Failed to execute 'fetch' on 'Window':
    // Illegal invocation". The wrapper keeps every call DETACHED (this === undefined, which the
    // global fetch accepts) while leaving fetchFn injectable for the tests. The module-level
    // `syncIndex` never hit this because it calls its fetchFn as a free variable.
    this.fetchFn = (...args) => fetchFn(...args);
    this.intervalMs = intervalMs;
    this.recheckMs = recheckMs;
    this.maxStorageBytes = maxStorageBytes;
    this.now = now;
    this.state = "unknown";
    //: why the last attempt failed (null when it did not) -- the syncer swallows its exceptions to
    //: stay a background poller, so this is the ONLY place the reason survives. Surfaced by the
    //: context rail's index row; without it a failure is just an opaque "error".
    this.lastError = null;
    this.indexHash = null;
    this.updatedAt = null;
    this.confirmedAt = null;
    this.progress = null;
    this._timer = null;
    this._busy = false;
  }

  /** First check on startup (adopts any cached hash, then polls unless recently confirmed). */
  async init() { return this.checkOnce(); }

  start() { if (!this._timer) this._timer = setInterval(() => { this.checkOnce(); }, this.intervalMs); }
  stop() { if (this._timer) { clearInterval(this._timer); this._timer = null; } }

  /** One tick: adopt the OPFS-cached hash, and -- only if `confirmedAt` is at least
   *  `recheckMs` old -- GET /basket and sync when the server's index_hash differs. */
  async checkOnce() {
    if (this._busy) return this.state;
    const meta = await readMeta(this.store);
    if (meta && meta.index_hash) {
      this.indexHash = meta.index_hash;                 // OPFS is the shared source of truth
      this.updatedAt = meta.updatedAt ?? this.updatedAt;
      this.confirmedAt = meta.confirmedAt ?? this.confirmedAt;
      if (this.state !== "syncing") this._setState("synced");
    }
    const confirmedAt = (meta && meta.confirmedAt) || 0;
    if (this.now() - confirmedAt < this.recheckMs) return this.state; // recently confirmed -> no network

    let manifest;
    try {
      manifest = await this._manifest();
    } catch (e) {
      this.lastError = _msg(e);
      this._setState(this.indexHash ? "synced" : "error"); // offline but maybe usable from cache
      return this.state;
    }
    const serverHash = manifest && manifest.index_hash;
    if (serverHash && serverHash === this.indexHash) {
      await this._writeMeta(meta, serverHash, false);    // confirm only: bump confirmedAt
      this._setState("synced");
      return this.state;
    }
    return this._sync(serverHash, manifest, meta);
  }

  /** Force a full re-download of the container, whatever the cache says -- the manual escape hatch
   *  behind the context rail's index row ("update"). Unlike :meth:`checkOnce` it ignores both the
   *  `recheckMs` throttle and the "server hash equals ours" shortcut, so it re-fetches even when
   *  everything already looks in sync (which is the point: it is how you prove it). */
  async resync() {
    if (this._busy) return this.state;
    this.lastError = null;
    const meta = await readMeta(this.store);
    let manifest;
    try {
      manifest = await this._manifest();
    } catch (e) {
      this.lastError = _msg(e);
      this._setState(this.indexHash ? "synced" : "error");   // offline; keep whatever we cached
      return this.state;
    }
    const serverHash = manifest && manifest.index_hash;
    if (!serverHash) {                                       // reachable but unusable manifest
      this.lastError = `GET ${this.baseUrl}/basket returned no index_hash: ${JSON.stringify(manifest).slice(0, 200)}`;
      this._setState("error");
      return this.state;
    }
    return this._sync(serverHash, manifest, meta);
  }

  /** GET the manifest, failing LOUDLY on an http error or a non-JSON body -- `fetch` resolves
   *  happily on a 404/500, and its html error page then dies in `.json()` with a parse error that
   *  says nothing about the request. Note the `ok === false` test rather than `!ok`: it must stay
   *  true for the bare `{json(){}}` stubs the store tests inject as responses. */
  async _manifest() {
    const url = `${this.baseUrl}/basket`;
    const resp = await this.fetchFn(url);
    if (resp && resp.ok === false) throw new Error(`GET ${url} -> HTTP ${resp.status}`);
    try {
      return await resp.json();
    } catch (e) {
      throw new Error(`GET ${url} -> body is not JSON (${_msg(e)})`);
    }
  }

  async _sync(serverHash, manifest, prevMeta) {
    this._busy = true;
    this.progress = { loaded: 0, total: this._estimate(manifest) };
    this._setState("syncing");
    try {
      const url = `${this.baseUrl}/basket/db`;
      const resp = await this.fetchFn(url);
      // same `ok === false` guard as _manifest: without it a 404/500 html page was happily stored
      // AS the container, and only failed much later when something tried to deserialise it.
      if (resp && resp.ok === false) throw new Error(`GET ${url} -> HTTP ${resp.status}`);
      let bytes;
      if (resp.body && typeof resp.body.getReader === "function") {
        const reader = resp.body.getReader();
        const chunks = [];
        let loaded = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          loaded += value.length;
          this.progress = { loaded, total: this.progress.total };
          this._emit("syncprogress");
        }
        bytes = _concatChunks(chunks);
      } else {
        bytes = new Uint8Array(await resp.arrayBuffer());
        this.progress = { loaded: bytes.length, total: bytes.length };
        this._emit("syncprogress");
      }
      if (bytes.length > this.maxStorageBytes) {
        // over the storage budget: don't persist the container (keep OPFS small). Record the
        // hash so we don't re-download every tick; Flatten will fetch it on demand instead.
        await this._writeMeta(prevMeta, serverHash, false);
      } else {
        await writeBytes(this.blobStore, `${serverHash}.fsdbidx`, bytes);
        await this._writeMeta(prevMeta, serverHash, true);
      }
      // prune blobs no service references (incl. the previous slice's, once meta moved off it).
      await gcBlobs(this.blobStore, { keep: serverHash, now: this.now });
      this.indexHash = serverHash;
      this.lastError = null;
      this._setState("synced");
    } catch (e) {
      this.lastError = _msg(e);
      this._setState(this.indexHash ? "synced" : "error");
    } finally {
      this._busy = false;
      this.progress = null;
    }
    return this.state;
  }

  /** Persist meta. `confirmedAt` is bumped to now every time; `updatedAt` advances only when
   *  bytes were actually (re)downloaded, otherwise it carries the previous value. Emits
   *  `"confirm"` so listeners can refresh even when `state` is unchanged (a plain re-confirm
   *  keeps `state==="synced"`, so `statechange` would NOT fire). */
  async _writeMeta(prevMeta, hash, downloaded) {
    const now = this.now();
    this.updatedAt = downloaded ? now : ((prevMeta && prevMeta.updatedAt) || now);
    this.confirmedAt = now;
    await writeMeta(this.store, {
      name: `${hash}.fsdbidx`,
      index_hash: hash,
      updatedAt: this.updatedAt,
      confirmedAt: this.confirmedAt,
    });
    this._emit("confirm");
  }

  /** Rough DECOMPRESSED container size from the manifest counts, for a progress denominator
   *  (the wire body may be gzipped, so Content-Length can't be trusted for decompressed bytes). */
  _estimate(manifest) {
    const c = manifest && manifest.counts;
    if (!c) return 0;
    return (c.charters || 0) * 36 + (c.fonds || 0) * 36 + (c.archives || 0) * 12 + 512;
  }

  _setState(s) { if (s !== this.state) { this.state = s; this._emit("statechange"); } }
  _emit(type) { this.dispatchEvent(new Event(type)); }
}
