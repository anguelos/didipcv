// Node driver for SharedIndex.flatten / unflatten (static/fsdb_sharedindex.js): the symmetric
// compress/expand pair. Builds a synthetic 2-archive universe and asserts the hierarchy cases
// (full fond -> fond, full archive -> archive, partial -> explicit charters) + the round-trip
// receiveBasket(unflatten(x)) == receiveBasket(x). Mirrors test_flatten_unflatten.py.
//   node flatten_unflatten_test.mjs ./fsdb_sharedindex.mjs
// Prints "ok" and exits 0 on success; throws on any failed assertion.
import { pathToFileURL } from "node:url";

const [, , modulePath] = process.argv;
const { serializeDb, SharedIndex } = await import(pathToFileURL(modulePath).href);

const eq = (a, b, m) => {
  if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${m}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`);
};

// A0={f0=[c0,c1]}  |  A1={f1=[c2], f2=[c3]}
const container = serializeDb({
  archive_id: { kind: "S", width: 4, values: ["A0", "A1"] },
  fond_id:    { kind: "S", width: 4, values: ["f0", "f1", "f2"] },
  charter_id: { kind: "S", width: 4, values: ["c0", "c1", "c2", "c3"] },
  charter_to_fond_idx: { kind: "i4", values: [0, 0, 1, 2] },
  fond_to_archive_idx: { kind: "i4", values: [0, 1, 1] },
}, "hash");
const ix = SharedIndex.fromContainer(container);
const wire = (c = []) => ({ all_charters: false, charter_ids: c, fond_ids: [], archive_ids: [],
                            bit_vector: null, bit_vector_hash: "hash" });

eq(ix.unflatten(wire(["c0", "c1"])),      { archive_ids: ["A0"], fond_ids: [], charter_ids: [] }, "whole A0");
eq(ix.unflatten(wire(["c0"])),            { archive_ids: [], fond_ids: [], charter_ids: ["c0"] }, "partial fond");
eq(ix.unflatten(wire(["c2", "c3"])),      { archive_ids: ["A1"], fond_ids: [], charter_ids: [] }, "whole A1");
eq(ix.unflatten(wire(["c0", "c1", "c2"])),{ archive_ids: ["A0"], fond_ids: ["f1"], charter_ids: [] }, "A0 + f1");
eq(ix.flatten({ archive_ids: ["A0"], fond_ids: [], charter_ids: [] }),
   { archive_ids: [], fond_ids: [], charter_ids: ["c0", "c1"] }, "flatten A0");

for (const sel of [["c0", "c1", "c2"], ["c0", "c3"], ["c0", "c1", "c2", "c3"], ["c2"], []]) {
  const u = ix.unflatten(wire(sel));
  const back = [...ix.receiveBasket({ all_charters: false, ...u, bit_vector: null, bit_vector_hash: "hash" })];
  const orig = [...ix.receiveBasket(wire(sel))];
  eq(back, orig, "round-trip " + sel);
}
console.log("ok");
