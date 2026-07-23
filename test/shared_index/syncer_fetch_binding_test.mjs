// Regression driver for the SharedIndexSyncer "Illegal invocation" bug.
//   node syncer_fetch_binding_test.mjs <store.mjs>
// Prints "ok" and exits 0 on success; throws (non-zero exit) on any failed assertion.
//
// THE BUG: the syncer kept its fetch function on the instance and called it as `this.fetchFn(url)`,
// which invokes it with `this === syncer`. The browser's global `fetch` rejects that outright --
// "Failed to execute 'fetch' on 'Window': Illegal invocation" -- so EVERY background poll threw,
// on every service. It went unnoticed because the failure is swallowed and mapped to
// `this.indexHash ? "synced" : "error"`: a service whose OPFS cache had been filled by some other
// path (syncIndex, used by Flatten / the slicer download) showed a happy "synced", while a service
// with a cold cache showed "error". Hence: Static + Slicer looked fine, cei2json + layout did not.
//
// Node's fetch is NOT this-sensitive, so the browser symptom cannot be reproduced here. We assert
// the CALL SHAPE instead, which is the actual invariant: a plain (non-arrow) mock must observe
// `this === undefined`, i.e. the syncer calls it detached.
import { pathToFileURL } from "node:url";

const [, , storePath] = process.argv;
const storeMod = await import(pathToFileURL(storePath).href);

function assert(cond, msg) { if (!cond) throw new Error("assert failed: " + msg); }

function mockStore() {
  const files = new Map();
  return {
    async getFileHandle(name, opts) {
      if (!files.has(name)) {
        if (opts && opts.create) files.set(name, new Uint8Array(0));
        else { const e = new Error("NotFound"); e.name = "NotFoundError"; throw e; }
      }
      return {
        async createWritable() {
          return { async write(data) {
                     const u8 = data instanceof ArrayBuffer ? new Uint8Array(data)
                       : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
                     files.set(name, u8.slice());
                   },
                   async close() {} };
        },
        async getFile() {
          const u8 = files.get(name);
          return { size: u8.length, async arrayBuffer() { return u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength); } };
        },
      };
    },
    async removeEntry(name) { files.delete(name); },
    async *entries() { for (const n of files.keys()) yield [n, { kind: "file" }]; },
  };
}

const seenThis = [];
const dbBytes = new Uint8Array([1, 2, 3, 4]);
// deliberately a `function`, not an arrow: only a non-arrow mock can observe the call's `this`
const fetchFn = async function (url) {
  seenThis.push(this);
  if (url.endsWith("/basket/db")) return { async arrayBuffer() { return dbBytes.slice(0).buffer; } };
  return { async json() { return { index_hash: "cafe", counts: { charters: 1, fonds: 1, archives: 1 } }; } };
};

const store = mockStore();
const syncer = new storeMod.SharedIndexSyncer({ store, fetchFn, intervalMs: 3000, recheckMs: 5000,
                                                now: () => 1_000_000 });

await syncer.checkOnce();
assert(seenThis.length > 0, "checkOnce never called fetchFn");
assert(seenThis.every((t) => t === undefined),
       `fetchFn called as a method (this === ${seenThis.find((t) => t !== undefined)}); ` +
       "the browser's global fetch throws 'Illegal invocation' for that");

seenThis.length = 0;
await syncer.resync();
assert(seenThis.length > 0, "resync never called fetchFn");
assert(seenThis.every((t) => t === undefined), "resync called fetchFn as a method");

console.log("ok");
