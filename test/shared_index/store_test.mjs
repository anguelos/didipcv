// Node driver for the client-side OPFS store (fsdb_sharedindex_store.js). There is no
// OPFS in Node, so we inject an in-memory directory handle and a stub fetch, and assert
// the caching / daily-recheck behaviour of syncIndex across time and hash changes.
//   node store_test.mjs <store.mjs> <codec.mjs>
// Prints "ok" and exits 0 on success; throws (non-zero exit) on any failed assertion.
import { pathToFileURL } from "node:url";

const [, , storePath, codecPath] = process.argv;
const storeMod = await import(pathToFileURL(storePath).href);
const codec = await import(pathToFileURL(codecPath).href);

function assert(cond, msg) { if (!cond) throw new Error("assert failed: " + msg); }

function mockStore() {
  const files = new Map(); // name -> Uint8Array
  return {
    files,
    async getFileHandle(name, opts) {
      if (!files.has(name)) {
        if (opts && opts.create) files.set(name, new Uint8Array(0));
        else { const e = new Error("NotFound"); e.name = "NotFoundError"; throw e; }
      }
      return {
        async createWritable() {
          return {
            async write(data) {
              const u8 = data instanceof ArrayBuffer
                ? new Uint8Array(data)
                : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
              files.set(name, u8.slice());
            },
            async close() {},
          };
        },
        async getFile() {
          const b = files.get(name);
          return { async arrayBuffer() { return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength); } };
        },
      };
    },
    async removeEntry(name) { files.delete(name); },
    async *entries() { for (const k of [...files.keys()]) yield [k, null]; },
  };
}

function mockFetch(state, counters) {
  return async (url) => {
    if (url.endsWith("/basket/db")) {
      counters.db++;
      const bytes = state.dbBytes;
      return { async arrayBuffer() { return bytes.slice(0); } };
    }
    if (url.endsWith("/basket")) {
      counters.manifest++;
      const m = state.manifest;
      return { async json() { return m; } };
    }
    throw new Error("unexpected url " + url);
  };
}

const blocks = {
  charter_id: { kind: "S", width: 32, values: ["a".repeat(32), "b".repeat(32)] },
  charter_to_fond_idx: { kind: "i4", values: [0, 0] },
};
const H1 = "1".repeat(64);
const H2 = "2".repeat(64);
const db1 = codec.serializeDb(blocks, H1, {});
const db2 = codec.serializeDb(blocks, H2, {});

const store = mockStore();
const counters = { manifest: 0, db: 0 };
const state = { manifest: { index_hash: H1 }, dbBytes: db1 };
const fetchFn = mockFetch(state, counters);
let t = 1_000_000;
const opts = () => ({ store, deserialize: codec.deserializeDb, fetchFn, now: () => t });

// 1) cold start -> fetch manifest + db, cache both
let r = await storeMod.syncIndex("", opts());
assert(r.index_hash === H1, "cold hash");
assert(counters.manifest === 1 && counters.db === 1, "cold fetch counts");
assert(store.files.has(`${H1}.fsdbidx`) && store.files.has("meta.json"), "cold cached");

// 2) within TTL -> zero network
r = await storeMod.syncIndex("", opts());
assert(counters.manifest === 1 && counters.db === 1, "TTL hit -> no network");
assert(r.index_hash === H1, "TTL hit hash");

// 3) past TTL, same hash -> manifest only (db already cached)
t += 25 * 60 * 60 * 1000;
r = await storeMod.syncIndex("", opts());
assert(counters.manifest === 2 && counters.db === 1, "stale same-hash -> manifest only");

// 4) hash changed -> fetch both, prune the old file
state.manifest = { index_hash: H2 };
state.dbBytes = db2;
t += 25 * 60 * 60 * 1000;
r = await storeMod.syncIndex("", opts());
assert(counters.manifest === 3 && counters.db === 2, "new hash -> fetch both");
assert(r.index_hash === H2, "new hash");
assert(store.files.has(`${H2}.fsdbidx`) && !store.files.has(`${H1}.fsdbidx`), "old pruned");

// --- SharedIndexSyncer: periodic hash check + streamed sync with progress ---------------
{
  const st = mockStore();
  const cnt = { manifest: 0, db: 0 };
  const S = { manifest: { index_hash: H1, counts: { archives: 1, fonds: 1, charters: 2 } }, dbBytes: db1 };
  // streaming fetch (two chunks) so syncprogress fires per chunk
  const fetchS = async (url) => {
    if (url.endsWith("/basket/db")) {
      cnt.db++;
      const b = S.dbBytes, mid = Math.ceil(b.length / 2);
      const parts = [b.slice(0, mid), b.slice(mid)];
      let i = 0;
      return { body: { getReader: () => ({ async read() { return i < parts.length ? { done: false, value: parts[i++] } : { done: true }; } }) } };
    }
    if (url.endsWith("/basket")) { cnt.manifest++; return { async json() { return S.manifest; } }; }
    throw new Error("unexpected url " + url);
  };
  let tt = 5_000_000;
  const metaOf = (s) => JSON.parse(new TextDecoder().decode(s.files.get("meta.json")));
  const sy = new storeMod.SharedIndexSyncer({ store: st, fetchFn: fetchS, now: () => tt, intervalMs: 3000, recheckMs: 5000 });
  let progress = 0; const states = [];
  sy.addEventListener("syncprogress", () => progress++);
  sy.addEventListener("statechange", () => states.push(sy.state));

  await sy.init();                                   // cold -> sync
  assert(sy.state === "synced" && sy.indexHash === H1, "syncer synced H1");
  assert(cnt.manifest === 1 && cnt.db === 1, "cold: one manifest + one db");
  assert(progress >= 2, "progress fired per chunk");
  assert(states.includes("syncing") && states[states.length - 1] === "synced", "state syncing->synced");
  assert(sy.progress === null, "progress cleared after sync");
  let m = metaOf(st);
  assert(m.updatedAt === 5_000_000 && m.confirmedAt === 5_000_000, "both timestamps on download");

  tt += 4_000;                                        // within recheckMs -> throttled: no network
  await sy.checkOnce();
  assert(cnt.manifest === 1 && cnt.db === 1, "throttled: no poll within 5s of confirmedAt");

  tt += 2_000;                                        // 6s since confirm -> recheck, same hash
  await sy.checkOnce();
  assert(cnt.manifest === 2 && cnt.db === 1, "recheck confirms without re-download");
  m = metaOf(st);
  assert(m.confirmedAt === 5_006_000 && m.updatedAt === 5_000_000, "confirmedAt bumped, updatedAt kept");

  tt += 3_000;                                        // within 5s of the new confirm -> throttled again
  await sy.checkOnce();
  assert(cnt.manifest === 2, "throttled again after a confirm");

  S.manifest = { index_hash: H2, counts: S.manifest.counts }; S.dbBytes = db2;
  tt += 3_000;                                        // 6s since confirm -> recheck, hash changed
  await sy.checkOnce();
  assert(sy.indexHash === H2 && cnt.db === 2, "resynced on hash change");
  assert(st.files.has(`${H2}.fsdbidx`) && !st.files.has(`${H1}.fsdbidx`), "pruned old file");
  m = metaOf(st);
  assert(m.updatedAt === 5_012_000 && m.confirmedAt === 5_012_000, "both timestamps advance on resync");

  // a SECOND syncer sharing the same store is throttled by the persisted confirmedAt (cross-tab)
  tt += 1_000;
  const before = cnt.manifest;
  const sy2 = new storeMod.SharedIndexSyncer({ store: st, fetchFn: fetchS, now: () => tt, intervalMs: 3000, recheckMs: 5000 });
  await sy2.init();
  assert(sy2.state === "synced" && sy2.indexHash === H2, "second syncer adopts cache");
  assert(cnt.manifest === before, "second syncer throttled by shared confirmedAt (no poll)");

  // offline (fetch throws) past the throttle, with a cached index -> stays usable, no throw
  tt += 10_000;
  const syOff = new storeMod.SharedIndexSyncer({ store: st, fetchFn: async () => { throw new Error("net"); }, now: () => tt, intervalMs: 3000, recheckMs: 5000 });
  await syOff.init();
  assert(syOff.state === "synced", "offline with cache -> synced");
}

// --- SharedIndexSyncer maxStorageBytes: an over-budget index is NOT persisted -----------
{
  const st = mockStore();
  const cnt = { manifest: 0, db: 0 };
  const S = { manifest: { index_hash: H1, counts: { charters: 2 } }, dbBytes: db1 };
  const f = async (url) => {
    if (url.endsWith("/basket/db")) { cnt.db++; const b = S.dbBytes; return { async arrayBuffer() { return b.slice(0); } }; }
    if (url.endsWith("/basket")) { cnt.manifest++; return { async json() { return S.manifest; } }; }
    throw new Error("bad url " + url);
  };
  let tt = 9_000_000;
  const sy = new storeMod.SharedIndexSyncer({ store: st, fetchFn: f, now: () => tt, maxStorageBytes: 4, intervalMs: 3000, recheckMs: 5000 });
  await sy.init();                                    // db1 is far bigger than 4 bytes
  assert(sy.state === "synced" && sy.indexHash === H1, "over-budget: synced (hash in memory)");
  assert(!st.files.has(`${H1}.fsdbidx`), "over-budget index NOT cached to OPFS");
  assert(st.files.has("meta.json"), "hash + confirmedAt still recorded");
  await sy.checkOnce();                               // throttled
  assert(cnt.db === 1, "no re-download while throttled");
}

// --- dirBytes: recursively sums file sizes under an OPFS directory --------------------
{
  const file = (size) => ({ kind: "file", async getFile() { return { size }; } });
  const dir = (entries) => ({ kind: "directory",
    async *entries() { for (const e of Object.entries(entries)) yield e; } });
  const tree = dir({ "a.fsdbidx": file(100), "meta.json": file(50), sub: dir({ "c.bin": file(25) }) });
  assert(await storeMod.dirBytes(tree) === 175, "dirBytes sums nested file sizes");
  assert(storeMod.OPFS_ROOT === "DDP-VRE", "OPFS root namespace");
}

console.log("ok");
