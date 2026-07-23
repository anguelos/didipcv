// Node driver for basket export/restore (fsdb_basket_io.js + BasketStore.importBaskets).
//   node basket_io_test.mjs <basket_io.mjs> <basket.mjs>
// Prints "ok" and exits 0 on success; throws (non-zero exit) on any failed assertion.
//
// Covers the whole round trip without a browser: build the envelope, gzip it, read it back
// (gzipped AND plain AND envelope-less), merge it into a store whose names already collide, and
// validate against an index that has LOST some ids -- the "the database changed" case, which is the
// one that must warn rather than silently drop.
import { pathToFileURL } from "node:url";
import { gunzipSync } from "node:zlib";

const [, , ioPath, basketPath] = process.argv;
const io = await import(pathToFileURL(ioPath).href);
const bk = await import(pathToFileURL(basketPath).href);

function assert(cond, msg) { if (!cond) throw new Error("assert failed: " + msg); }

function mockStore() {
  const files = new Map();
  return {
    files,
    async getFileHandle(name, opts) {
      if (!files.has(name)) {
        if (opts && opts.create) files.set(name, new Uint8Array(0));
        else { const e = new Error("NotFound"); e.name = "NotFoundError"; throw e; }
      }
      return {
        async createWritable() {
          return { async write(d) {
                     files.set(name, d instanceof ArrayBuffer ? new Uint8Array(d)
                       : new Uint8Array(d.buffer, d.byteOffset, d.byteLength).slice());
                   },
                   async close() {} };
        },
        async getFile() {
          const u8 = files.get(name);
          return { size: u8.length,
                   async arrayBuffer() { return u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength); },
                   async text() { return new TextDecoder().decode(u8); } };
        },
      };
    },
    async removeEntry(n) { files.delete(n); },
    async *entries() { for (const n of files.keys()) yield [n, { kind: "file" }]; },
  };
}

const md5 = (c) => c.repeat(32);

// ---- 1. export ----------------------------------------------------------------------------
const src = await bk.BasketStore.open({ store: mockStore() });
await src.create("alpha");
await src.add("charters", md5("a"));
await src.add("charters", md5("b"));
await src.add("fonds", md5("f"));
await src.add("archives", "IT-BSNSP");
await src.setMemo("my notes");
await src.create("beta");
await src.add("charters", md5("c"));

const bytes = await io.exportBytes(src, { indexHash: "IDXOLD" });
assert(bytes[0] === 0x1f && bytes[1] === 0x8b, "export is not gzip");
// cross-check with Node's own zlib: our CompressionStream output must be a normal gzip member
const viaZlib = JSON.parse(gunzipSync(Buffer.from(bytes)).toString());
assert(viaZlib.format === "ddp-baskets" && viaZlib.version === 1, "envelope header wrong");
assert(!("All" in viaZlib.baskets), "the reserved All basket must not be exported");
assert(viaZlib.baskets.alpha.charters.length === 2, "charters lost");
assert(viaZlib.baskets.alpha.fonds.length === 1 && viaZlib.baskets.alpha.archives.length === 1,
       "baskets must stay UNFLATTENED (fonds/archives kept as such)");
assert(viaZlib.baskets.alpha.memo === "my notes", "memo lost");

// ---- 2. parse: gzipped, plain, and envelope-less ------------------------------------------
const parsed = await io.parseBasketsFile(bytes);
assert(parsed.indexHash === "IDXOLD", "index_hash lost");
assert(Object.keys(parsed.baskets).sort().join() === "alpha,beta,default",
       "basket names lost (a fresh store always carries `default`)");

const plain = new TextEncoder().encode(JSON.stringify(viaZlib));
assert(Object.keys((await io.parseBasketsFile(plain)).baskets).length === 3, "plain json rejected");

const bare = new TextEncoder().encode(JSON.stringify({ solo: { charters: [md5("a")] } }));
const bareOut = await io.parseBasketsFile(bare);
assert(Object.keys(bareOut.baskets).join() === "solo", "envelope-less map rejected");

for (const [bad, why] of [[new Uint8Array([1, 2, 3]), "garbage"],
                          [new TextEncoder().encode("{}"), "empty object"],
                          [new TextEncoder().encode("nope"), "not json"]]) {
  let threw = false;
  try { await io.parseBasketsFile(bad); } catch { threw = true; }
  assert(threw, `${why} should be rejected with a readable error`);
}

// ---- 3. restore into a store whose names collide -------------------------------------------
const dst = await bk.BasketStore.open({ store: mockStore() });
await dst.create("alpha");
await dst.add("charters", md5("z"));      // pre-existing content that must SURVIVE
const renamed = await dst.importBaskets(parsed.baskets);
assert(renamed.alpha === "alpha (imported)", `collision not renamed: ${renamed.alpha}`);
assert(renamed.beta === "beta", "free name should not be renamed");
assert(renamed.default === "default (imported)", "the default basket collides too");
assert(dst.basket("alpha").charters.join() === md5("z"), "existing basket was overwritten");
assert(dst.basket("alpha (imported)").charters.length === 2, "imported basket incomplete");
assert(dst.basket("alpha (imported)").memo === "my notes", "memo not restored");
assert(!dst.names.includes("All (imported)"), "reserved All must be skipped on import");

// persisted, not just in memory
const reopened = await bk.BasketStore.open({ store: dst._store });
assert(reopened.basket("alpha (imported)").charters.length === 2, "import did not persist");

// ---- 4. the DB changed: ids missing from the current index ---------------------------------
const fakeIndex = {                       // only knows charter a and fond f
  index_hash: "IDXNEW",
  classify(id) {
    if (id === md5("a")) return "charter";
    if (id === md5("f")) return "fond";
    return null;
  },
};
const res = io.validateAgainstIndex(parsed.baskets, fakeIndex);
assert(res.total === 3, `expected 3 missing (b, c, IT-BSNSP), got ${res.total}`);
assert(res.counts.charters === 2 && res.counts.archives === 1, "missing counted per type wrongly");
assert(res.perBasket.alpha === 2 && res.perBasket.beta === 1, "per-basket attribution wrong");
const warn = io.missingWarning(res, { indexHash: "IDXNEW", fileIndexHash: "IDXOLD" });
assert(warn && warn.includes("3 of the imported ids"), "warning text missing the total");
assert(warn.includes("IDXOLD".slice(0, 8)) && warn.includes("IDXNEW".slice(0, 8)),
       "warning should name both index hashes when they differ");
assert(io.missingWarning(io.validateAgainstIndex({ ok: { charters: [md5("a")] } }, fakeIndex)) === null,
       "a fully-resolving import must not warn");

// nothing was dropped by the import despite being unknown to the index
assert(dst.basket("alpha (imported)").charters.includes(md5("b")),
       "an id missing from the index must be KEPT, not silently dropped");

// ---- 5. abort ------------------------------------------------------------------------------
const ctl = new AbortController();
ctl.abort();
let aborted = false;
try { io.validateAgainstIndex(parsed.baskets, fakeIndex, { signal: ctl.signal }); }
catch (e) { aborted = e.name === "AbortError"; }
assert(aborted, "validation must honour an aborted signal");

console.log("ok");
