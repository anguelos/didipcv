// Node driver for the shared-index BLOB DEDUP: behind one origin, services on the same slice must
// share a single <hash>.fsdbidx blob (downloaded once), each keeping only its own meta.json; a blob
// is GC'd only when no service references its hash. Uses a nested in-memory OPFS mock.
//   node store_dedup_test.mjs <store.mjs> <codec.mjs>
import { pathToFileURL } from "node:url";

const [, , storePath, codecPath] = process.argv;
const S = await import(pathToFileURL(storePath).href);
const codec = await import(pathToFileURL(codecPath).href);
const assert = (c, m) => { if (!c) throw new Error("assert failed: " + m); };

function memDir() {
  const files = new Map(), dirs = new Map();
  return {
    kind: "directory",
    async getFileHandle(name, opts) {
      if (!files.has(name)) { if (opts?.create) files.set(name, new Uint8Array(0)); else { const e = new Error("NF"); e.name = "NotFoundError"; throw e; } }
      return {
        kind: "file",
        async createWritable() { return { async write(d) { const u8 = d instanceof ArrayBuffer ? new Uint8Array(d) : new Uint8Array(d.buffer, d.byteOffset, d.byteLength); files.set(name, u8.slice()); }, async close() {} }; },
        async getFile() { const b = files.get(name); return { size: b.length, async arrayBuffer() { return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength); } }; },
      };
    },
    async getDirectoryHandle(name, opts) {
      if (!dirs.has(name)) { if (opts?.create) dirs.set(name, memDir()); else { const e = new Error("NF"); e.name = "NotFoundError"; throw e; } }
      return dirs.get(name);
    },
    async removeEntry(name) { files.delete(name); dirs.delete(name); },
    async *entries() { for (const k of files.keys()) yield [k, { kind: "file" }]; for (const [k, v] of dirs) yield [k, v]; },
    _files: files, _dirs: dirs,
  };
}

const blocks = { charter_id: { kind: "S", width: 32, values: ["a".repeat(32), "b".repeat(32)] }, charter_to_fond_idx: { kind: "i4", values: [0, 0] } };
const H1 = "1".repeat(64), H2 = "2".repeat(64);
const db1 = codec.serializeDb(blocks, H1, {}), db2 = codec.serializeDb(blocks, H2, {});
const state = { manifest: { index_hash: H1 }, dbBytes: db1 };
const counters = { db: 0 };
const fetchFn = async (url) => {
  if (url.endsWith("/basket/db")) { counters.db++; const b = state.dbBytes; return { async arrayBuffer() { return b.slice(0); } }; }
  if (url.endsWith("/basket")) { const m = state.manifest; return { async json() { return m; } }; }
  throw new Error("bad url " + url);
};

const root = memDir();                                   // DDP-VRE/shared_index (blob store)
const st = await root.getDirectoryHandle("st", { create: true });
const ly = await root.getDirectoryHandle("ly", { create: true });
let t = 1e6;
const sync = (store) => S.syncIndex("", { store, blobStore: root, deserialize: codec.deserializeDb, fetchFn, now: () => t });
const blobs = () => [...root._files.keys()].filter((n) => n.endsWith(".fsdbidx")).sort();

// 1) st syncs H1 -> one blob at the root, meta in st/
await sync(st);
assert(counters.db === 1, "st downloads once");
assert(blobs().join() === `${H1}.fsdbidx`, "one shared blob after st");
assert(st._files.has("meta.json") && !st._files.has(`${H1}.fsdbidx`), "st keeps only meta, blob is shared");

// 2) ly syncs the SAME slice (H1) -> NO new download, reuses the shared blob
await sync(ly);
assert(counters.db === 1, "ly reuses shared blob (no second download) — DEDUP");
assert(blobs().join() === `${H1}.fsdbidx`, "still one blob");
assert(ly._files.has("meta.json"), "ly has its own meta");

// 3) st moves to a different slice H2 -> H2 downloaded; H1 KEPT (ly still references it)
state.manifest = { index_hash: H2 }; state.dbBytes = db2; t += 25 * 3600e3;
await sync(st);
assert(counters.db === 2, "st downloads H2");
assert(blobs().join() === [`${H1}.fsdbidx`, `${H2}.fsdbidx`].sort().join(), "both blobs kept (ly still on H1)");

// 4) ly moves to H2 too -> no download; H1 now unreferenced -> GC'd
t += 25 * 3600e3;
await sync(ly);
assert(counters.db === 2, "ly reuses H2 blob (no download)");
assert(blobs().join() === `${H2}.fsdbidx`, "H1 GC'd once no service references it");

console.log("ok");
