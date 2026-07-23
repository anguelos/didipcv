// Node driver for the JS basket codec (SharedIndex.sendBasket/receiveBasket in
// static/fsdb_sharedindex.js). Self-asserts the id-list/bit-vector density rule, base64
// round-trip and the IndexMismatch rule; with an output path it also emits the wire baskets
// + expected positions so the Python side can assert cross-language parity.
//   node codec_basket_test.mjs ./fsdb_sharedindex.mjs [out.json]
// Prints "ok" and exits 0 on success; throws (non-zero) on any failed assertion.
import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [, , modulePath, outPath] = process.argv;
const { serializeDb, SharedIndex, IndexMismatch } = await import(pathToFileURL(modulePath).href);

const assert = (c, m) => { if (!c) throw new Error("assert failed: " + m); };

// Universe mirrored EXACTLY on the Python side (test_basket_codec.py::big_index-style).
const N = 4096;
const charter_id = Array.from({ length: N }, (_, i) => i.toString(16).padStart(32, "0"));
const blocks = {
  archive_id: { kind: "S", width: 8, values: ["AR-A", "AR-B"] },
  fond_id: { kind: "S", width: 8, values: ["fond_0", "fond_1", "fond_2"] },
  charter_id: { kind: "S", width: 32, values: charter_id },
  charter_to_fond_idx: { kind: "i4", values: Array.from({ length: N }, (_, i) => i % 3) },
  fond_to_archive_idx: { kind: "i4", values: [0, 1, 1] },
};
const ix = SharedIndex.fromContainer(serializeDb(blocks, "", {}));  // hash "" -> hash-agnostic

// small selection -> id list
const some = [charter_id[0], charter_id[10]];
const bSmall = ix.sendBasket({ charterIds: some });
assert(bSmall.bit_vector === null && bSmall.charter_ids.length === 2, "small -> id list");
assert([...ix.receiveBasket(bSmall)].join() === "0,10", "small round-trip positions");

// density crossover at N/1024 = 4
assert(ix.sendBasket({ charterIds: charter_id.slice(0, 4) }).bit_vector === null, "4 -> id list");
assert(typeof ix.sendBasket({ charterIds: charter_id.slice(0, 5) }).bit_vector === "string", "5 -> bit vector");

// fond selection -> bit vector; round-trips to the i%3===0 charters
const bFond = ix.sendBasket({ fondIds: ["fond_0"] });
assert(typeof bFond.bit_vector === "string" && bFond.fond_ids.length === 0, "fond -> bit vector");
const posFond = [...ix.receiveBasket(bFond)];
const expect = []; for (let i = 0; i < N; i++) if (i % 3 === 0) expect.push(i);
assert(posFond.length === expect.length && posFond.every((p, k) => p === expect[k]), "fond bit-vector round-trip");
assert(ix.positionsToMd5s(posFond)[0] === charter_id[0], "positionsToMd5s");

// all_charters
const bAll = ix.sendBasket({ allCharters: true });
assert(bAll.all_charters && ix.receiveBasket(bAll).length === N, "all_charters");

// mismatch rule
let threw = false;
try { ix.receiveBasket({ bit_vector: bFond.bit_vector, bit_vector_hash: "deadbeef" }); }
catch (e) { threw = e instanceof IndexMismatch; }
assert(threw, "stale bit_vector throws IndexMismatch");
assert(ix.receiveBasket({ charter_ids: some, bit_vector_hash: "deadbeef" }).length === 2, "id-list tolerated");

if (outPath) {
  writeFileSync(outPath, JSON.stringify({
    N, small: bSmall, fond0: bFond,
    small_positions: [...ix.receiveBasket(bSmall)], fond0_positions: posFond,
  }));
}
console.log("ok");
