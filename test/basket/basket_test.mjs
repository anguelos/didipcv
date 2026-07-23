// Node driver for the client-side basket data model (BasketStore in static/fsdb_basket.js).
// There is no OPFS/DOM in Node, so we drive BasketStore with its own memoryStore() and assert
// the named-basket model: per-type add/remove/dedup, counts, memo, create/rename/delete rules,
// active switching, persistence across reopen, and the charter clipboard/export renderings.
// The DOM widget (BasketWidget) is browser-only and not exercised here.
//   node basket_test.mjs <fsdb_basket.mjs> <fsdb_sharedindex.mjs>
// Prints "ok" and exits 0 on success; throws (non-zero exit) on any failed assertion.
import { pathToFileURL } from "node:url";

const [, , modulePath, codecPath] = process.argv;
const { BasketStore, memoryStore, buildResolver, loadClientConfig, CLIENT_CONFIG, ALL_BASKET } =
  await import(pathToFileURL(modulePath).href);
const { serializeDb, deserializeDb, SharedIndex } = await import(pathToFileURL(codecPath).href);

function assert(cond, msg) { if (!cond) throw new Error("assert failed: " + msg); }
const eq = (a, b, msg) => assert(JSON.stringify(a) === JSON.stringify(b), `${msg}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`);
const A = "a".repeat(32), B = "b".repeat(32), F = "f".repeat(32);

const store = memoryStore();
let s = await BasketStore.open({ store });

// --- fresh: one empty 'default' active, plus the reserved 'All' basket ---
eq(s.names, ["All", "default"], "starts with All + default");
assert(s.activeName === "default", "default active (not the reserved All)");
assert(s.isEmpty(), "default empty");
eq(s.counts(), { archives: 0, fonds: 0, charters: 0 }, "zero counts");

// --- per-type add / dedup / has / counts ---
await s.add("charters", A); await s.add("charters", A); await s.add("charters", B); // A once
await s.add("fonds", F);
await s.add("archives", "IT-BSNSP");
eq(s.active.charters, [A, B], "charters order+dedup");
eq(s.counts(), { archives: 1, fonds: 1, charters: 2 }, "counts");
assert(s.has("charters", A) && s.has("archives", "IT-BSNSP") && !s.has("fonds", A), "has()");
await s.add("charters", ""); assert(s.counts().charters === 2, "empty id ignored");

// --- memo ---
await s.setMemo("a memo");
assert(s.active.memo === "a memo", "memo set");

// --- persistence across reopen (same store) ---
let s2 = await BasketStore.open({ store });
eq(s2.active.charters, [A, B], "charters persisted");
assert(s2.active.memo === "a memo", "memo persisted");

// --- remove ---
await s2.remove("charters", A);
eq(s2.active.charters, [B], "remove charter");

// --- name validation + create ---
assert(!s2.canUseName(""), "empty name invalid");
assert(!s2.canUseName("default"), "existing name invalid");
assert(s2.canUseName("trip"), "new name valid");
assert(await s2.create("trip") === true, "create trip");
assert(s2.activeName === "trip", "create switches active");
assert(s2.isEmpty("trip"), "new basket empty");
eq(s2.names, ["All", "default", "trip"], "two editable baskets + All");
assert(await s2.create("trip") === false, "no duplicate create");
assert(await s2.create("  ") === false, "no blank create");
assert(await s2.create(ALL_BASKET) === false, "cannot create the reserved 'All' name");

// --- rename: change key, reject duplicates ---
assert(await s2.rename("trip", "default") === false, "rename to existing rejected");
assert(await s2.rename("trip", "journey") === true, "rename ok");
eq(s2.names, ["All", "default", "journey"], "renamed key");
assert(s2.activeName === "journey", "active follows rename");
assert(await s2.rename("journey", ALL_BASKET) === false, "cannot rename to reserved 'All'");

// --- delete rules: only empty, never the last ---
await s2.setActive("default"); // default has [B]
assert(s2.canDelete("default") === false, "non-empty not deletable");
assert(await s2.delete("default") === false, "delete non-empty rejected");
assert(s2.canDelete("journey") === true, "empty non-last deletable");
assert(await s2.delete("journey") === true, "delete empty ok");
eq(s2.names, ["All", "default"], "back to one editable + All");
assert(await s2.delete("default") === false, "cannot delete the last editable basket");

// --- change event ---
let fired = 0; s2.addEventListener("change", () => { fired++; });
await s2.add("charters", A);
assert(fired === 1, "change dispatched");

// --- clipboard / export (active charters) : default now [B, A] ---
assert(s2.chartersColumn() === `${B}\n${A}`, "chartersColumn newline-joined");
assert(s2.chartersRow() === `${B}\t${A}`, "chartersRow tab-joined");
assert(s2.chartersFile() === `${B}\n${A}\n`, "chartersFile trailing newline");

// --- flatten / unflatten / subtract via the shared index (single index methods) ---
{
  const c0 = "a".repeat(32), c1 = "b".repeat(32), c2 = "c".repeat(32);
  const f0 = "0".repeat(32), f1 = "1".repeat(32);        // IT-X ⊃ f0=[c0,c1], f1=[c2]
  const container = serializeDb({
    archive_id: { kind: "S", width: 8, values: ["IT-X"] },
    fond_id: { kind: "S", width: 32, values: [f0, f1] },
    charter_id: { kind: "S", width: 32, values: [c0, c1, c2] },
    charter_to_fond_idx: { kind: "i4", values: [0, 0, 1] },
    fond_to_archive_idx: { kind: "i4", values: [0, 0] },
  }, "H");
  const ix = SharedIndex.fromContainer(container);

  const bs = await BasketStore.open({ store: memoryStore() });
  await bs.add("fonds", f0); await bs.add("charters", c2);   // {fonds:[f0], charters:[c2]}
  const r = await bs.flatten(ix);
  assert(r.ok && r.count === 3, "flatten -> 3 charters");
  eq([...bs.active.charters].sort(), [c0, c1, c2], "flattened charters");
  eq(bs.active.fonds, [], "fonds cleared"); eq(bs.active.archives, [], "archives cleared");

  const u = await bs.unflatten(ix);                         // {c0,c1,c2} -> whole IT-X
  assert(u.ok && u.archives === 1 && u.fonds === 0 && u.charters === 0, "unflatten -> archive");
  eq(bs.active.archives, ["IT-X"], "compressed to IT-X");

  const bs2 = await BasketStore.open({ store: memoryStore() });
  await bs2.add("archives", "IT-X");                        // IT-X = {c0,c1,c2}
  const sub = await bs2.subtract(ix, { fond_ids: [f0] });   // subtract f0 (=[c0,c1])
  assert(sub.ok && sub.removed === 2 && sub.count === 1, "subtract fond -> 1 remains");
  eq(bs2.active.charters, [c2], "only c2 left");

  const bs3 = await BasketStore.open({ store: memoryStore(), maxSize: 2 });
  await bs3.add("archives", "IT-X");                        // flattens to 3 >= 2
  const r3 = await bs3.flatten(ix);
  assert(r3.ok === false && r3.limit === 2, "flatten refused by maxSize");

  // createComplement: whole DB minus charter c0 -> {c1,c2}. This fixture: single archive IT-X with
  // f0=[c0,c1], f1=[c2]; f1 becomes full -> fond f1; c1 stays explicit; IT-X not full.
  const bs4 = await BasketStore.open({ store: memoryStore() });
  const cr = await bs4.createComplement(ix, { charter_ids: [c0] }, "not-c0");
  assert(cr.ok && cr.n === 2 && cr.opaque === false, "complement of c0 -> 2 charters, compressed");
  assert(bs4.activeName === "not-c0", "complement becomes active");
  eq(bs4.active.archives, [], "complement archives");
  eq(bs4.active.fonds, [f1], "complement fond (f1 full)");
  eq(bs4.active.charters, [c1], "complement explicit charter");
}

// --- complement: opaque bit-vector fallback when the compressed id-count exceeds maxSize ---
{
  const c0 = "a".repeat(32), c1 = "b".repeat(32), c2 = "c".repeat(32);
  const f0 = "0".repeat(32), f1 = "1".repeat(32);
  const ix = SharedIndex.fromContainer(serializeDb({
    archive_id: { kind: "S", width: 8, values: ["IT-X", "IT-Y"] },
    fond_id: { kind: "S", width: 32, values: [f0, f1] },
    charter_id: { kind: "S", width: 32, values: [c0, c1, c2] },
    charter_to_fond_idx: { kind: "i4", values: [0, 0, 1] },   // f0=[c0,c1] (IT-X), f1=[c2] (IT-Y)
    fond_to_archive_idx: { kind: "i4", values: [0, 1] },
  }, "H"));
  const bs = await BasketStore.open({ store: memoryStore(), maxSize: 1 });    // tiny cap -> opaque
  const cr = await bs.createComplement(ix, { charter_ids: [c0] }, "opaque");
  assert(cr.ok && cr.opaque === true, "complement stored opaque under tiny maxSize");
  assert(bs.isOpaque("opaque"), "isOpaque true");
  eq(bs.counts("opaque"), { archives: 0, fonds: 0, charters: 2 }, "opaque counts = charter total");
  assert(await bs.add("charters", c1) === false, "opaque basket is not editable");
  // opaque compacts to its stored bit-vector, decoding to {c1,c2}
  const wire = bs.compactAgainst(serializeDb({
    archive_id: { kind: "S", width: 8, values: ["IT-X", "IT-Y"] },
    fond_id: { kind: "S", width: 32, values: [f0, f1] },
    charter_id: { kind: "S", width: 32, values: [c0, c1, c2] },
    charter_to_fond_idx: { kind: "i4", values: [0, 0, 1] },
    fond_to_archive_idx: { kind: "i4", values: [0, 1] },
  }, "H"));
  assert(wire.bit_vector, "opaque wire carries a bit_vector");
  eq([...ix.receiveBasket(wire)], [1, 2], "opaque decodes to {c1,c2}");
}

// --- allBasketsChartersTsv: one column per basket, name as header, ragged padded ---
{
  const st = memoryStore();
  const bs = await BasketStore.open({ store: st });
  await bs.add("charters", A); await bs.add("charters", B);   // default: A,B
  await bs.create("trip"); await bs.add("charters", "c".repeat(32)); // trip: C
  eq(bs.names, ["All", "default", "trip"], "two baskets sorted + All");
  eq(bs.allBasketsChartersTsv(), `default\ttrip\n${A}\t${"c".repeat(32)}\n${B}\t`, "columnar tsv excludes reserved All");
}

// --- buildResolver: fond/archive -> charters from a real round-tripped container ---
{
  const container = serializeDb({
    archive_id: { kind: "S", width: 8, values: ["AR-A", "AR-B"] },
    fond_id: { kind: "S", width: 8, values: ["f0", "f1", "f2"] },
    charter_id: { kind: "S", width: 8, values: ["c0", "c1", "c2", "c3"] },
    charter_to_fond_idx: { kind: "i4", values: [0, 0, 1, 2] }, // c0,c1->f0; c2->f1; c3->f2
    fond_to_archive_idx: { kind: "i4", values: [0, 1, 1] },    // f0->A; f1,f2->B
  }, "hash");
  const res = buildResolver(deserializeDb(container).blocks);
  eq(res.chartersOfFond("f0"), ["c0", "c1"], "fond f0 -> c0,c1");
  eq(res.chartersOfFond("f1"), ["c2"], "fond f1 -> c2");
  eq(res.chartersOfArchive("AR-A"), ["c0", "c1"], "archive A -> c0,c1");
  eq(res.chartersOfArchive("AR-B"), ["c2", "c3"], "archive B -> c2,c3");
  eq(res.chartersOfFond("nope"), [], "unknown fond -> empty");
}

// --- maxSize: add refuses beyond the basket-size cap; flatten defaults its cap to it ---
{
  const bs = await BasketStore.open({ store: memoryStore(), maxSize: 3 });
  assert(await bs.add("charters", A) === true, "add 1");
  assert(await bs.add("fonds", F) === true, "add 2");
  assert(await bs.add("archives", "IT-X") === true, "add 3");
  assert(bs.totalCount() === 3 && bs.isFull(), "full at maxSize");
  assert(await bs.add("charters", B) === false, "add beyond cap refused");
  assert(bs.totalCount() === 3, "unchanged when full");
}

// --- loadClientConfig: CLIENT_CONFIG is the single source of truth, loaded from source ---
{
  const cfg = await loadClientConfig({ fetchFn: async () => ({ json: async () => ({ sync_interval_ms: 1000, max_basket_size: 42, developer_mode: false, user: "alice" }) }) });
  assert(cfg === CLIENT_CONFIG, "loadClientConfig returns the one CLIENT_CONFIG object");
  assert(CLIENT_CONFIG.sync_interval_ms === 1000 && CLIENT_CONFIG.max_basket_size === 42, "values loaded from source");
  assert(CLIENT_CONFIG.developer_mode === false, "developer_mode loaded from source");
  assert(CLIENT_CONFIG.user === "alice", "user loaded from source (default_user in the shipped config)");
  await loadClientConfig({ fetchFn: async () => { throw new Error("404"); } });
  assert(CLIENT_CONFIG.sync_interval_ms === 1000, "a failed load leaves CLIENT_CONFIG unchanged");
  await loadClientConfig({ fetchFn: async () => ({ json: async () => ({ developer_mode: true }) }) });
  assert(CLIENT_CONFIG.developer_mode === true && CLIENT_CONFIG.sync_interval_ms === 1000, "later load merges into the same object");
}

// --- clear: empties the active basket (all types), keeps name + memo ---
{
  const bs = await BasketStore.open({ store: memoryStore() });
  await bs.add("charters", A); await bs.add("fonds", F); await bs.add("archives", "IT-X");
  await bs.setMemo("keep me");
  assert(bs.totalCount() === 3, "3 items before clear");
  assert(await bs.clear() === true, "clear returns true");
  assert(bs.isEmpty() && bs.totalCount() === 0, "emptied all types");
  assert(bs.active.memo === "keep me", "memo kept");
  assert(bs.activeName === "default", "basket kept");
  assert(await bs.clear() === false, "clear on empty basket is a no-op");
}

// --- reserved 'All' basket: always present, undeletable, non-editable, all_charters wire ---
{
  const st = memoryStore();
  const bs = await BasketStore.open({ store: st });
  assert(bs.names.includes(ALL_BASKET), "All always present");
  assert(!bs.isActiveAll, "All is not active by default");
  // selecting All: not editable, remembers where we came from
  await bs.add("charters", A);              // default now has A
  await bs.setActive(ALL_BASKET);
  assert(bs.isActiveAll, "All active after setActive");
  assert(bs.prevActiveName === "default", "prevActive remembers the editable basket");
  assert(await bs.add("charters", B) === false, "add refused while All active");
  await bs.remove("charters", A);           // no-op on All
  assert(bs.basket("default").charters.length === 1, "All did not mutate the editable basket");
  assert(await bs.clear() === false, "clear refused on All");
  await bs.setMemo("x");                     // no-op
  assert(!bs.basket(ALL_BASKET).memo.includes("x"), "setMemo refused on All");
  assert(bs.canDelete(ALL_BASKET) === false, "All not deletable");
  assert(await bs.delete(ALL_BASKET) === false, "delete All refused");
  assert(await bs.rename(ALL_BASKET, "z") === false, "rename All refused");
  // switching back restores the preserved editable basket
  await bs.setActive(bs.prevActiveName);
  assert(bs.activeName === "default" && !bs.isActiveAll, "switch back to preserved basket");
  // reopen: All survives persistence, prevActive too
  await bs.setActive(ALL_BASKET);
  const bs2 = await BasketStore.open({ store: st });
  assert(bs2.isActiveAll, "All active persisted across reopen");
  assert(bs2.prevActiveName === "default", "prevActive persisted");
}

// --- compactAgainst: the active 'All' basket encodes to the all_charters wire flag ---
{
  const container = serializeDb({
    archive_id: { kind: "S", width: 8, values: ["AR-A"] },
    fond_id: { kind: "S", width: 8, values: ["f0"] },
    charter_id: { kind: "S", width: 8, values: ["c0", "c1"] },
    charter_to_fond_idx: { kind: "i4", values: [0, 0] },
    fond_to_archive_idx: { kind: "i4", values: [0] },
  }, "hash");
  const bs = await BasketStore.open({ store: memoryStore() });
  await bs.add("charters", "c0");
  const wireSel = bs.compactAgainst(container);
  // (a 1-of-2 selection is dense enough to ship as a bit vector; the point is: NOT all_charters)
  assert(wireSel.all_charters === false, "editable basket -> not all_charters");
  await bs.setActive(ALL_BASKET);
  const wireAll = bs.compactAgainst(container);
  assert(wireAll.all_charters === true, "All basket -> all_charters:true");
  eq(wireAll.charter_ids, [], "all_charters carries no id list");
  assert(typeof wireAll.bit_vector_hash === "string" && wireAll.bit_vector_hash.length > 0, "all_charters carries the index hash");
}

// --- a separate store is independent ---
assert((await BasketStore.open({ store: memoryStore() })).isEmpty(), "separate store empty");

console.log("ok");
