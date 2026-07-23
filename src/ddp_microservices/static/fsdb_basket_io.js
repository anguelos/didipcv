// fsdb_basket_io.js -- export every basket to one gzipped JSON file, and restore it.
//
// FILE FORMAT (`baskets.json.gz`): gzip of
//   { "format": "ddp-baskets", "version": 1, "exported_at": <iso>, "index_hash": <hash|null>,
//     "baskets": { "<name>": { "archives": [...], "fonds": [...], "charters": [...],
//                              "memo": "", ... } } }
// Baskets are stored UNFLATTENED -- archives and fonds stay as archives and fonds, exactly as the
// user built them -- so a restore keeps its meaning even against a DB that has grown since.
// An OPAQUE basket (a packed membership bit-vector produced by subtract/complement) additionally
// carries `bitVector`/`bitHash`/`n`; those only mean anything against the index whose hash is
// `bitHash`, which is why `index_hash` travels with the file. The reserved `All` basket is never
// exported: BasketStore.normalize rebuilds it on load.
//
// `parseBasketsFile` also accepts a PLAIN (ungzipped) file, and a bare `{name: basket}` object with
// no envelope -- so a file written by hand or by another tool imports fine.
//
// Everything here is pure or injectable, and the compression is the platform's own
// CompressionStream/DecompressionStream (available in browsers and in Node >= 17) -- no dependency,
// and the output is a normal gzip file that `gunzip`/`zcat` and Python's `gzip` read directly.

import { throwIfAborted } from "./ddp_busy.js";

export const EXPORT_FORMAT = "ddp-baskets";
export const EXPORT_VERSION = 1;
export const EXPORT_FILENAME = "baskets.json.gz";

const GZIP_MAGIC = [0x1f, 0x8b];

/** Concatenate stream chunks into one Uint8Array. */
function concat(chunks) {
  let n = 0;
  for (const c of chunks) n += c.length;
  const out = new Uint8Array(n);
  let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}

async function pipeThrough(bytes, stream) {
  const src = new Blob([bytes]).stream().pipeThrough(stream);
  const reader = src.getReader();
  const chunks = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
  }
  return concat(chunks);
}

export const gzip = (bytes) => pipeThrough(bytes, new CompressionStream("gzip"));
export const gunzip = (bytes) => pipeThrough(bytes, new DecompressionStream("gzip"));

const isGzip = (bytes) => bytes.length >= 2 && bytes[0] === GZIP_MAGIC[0] && bytes[1] === GZIP_MAGIC[1];

/** The exportable projection of one basket: id lists + memo, plus the opaque fields when present. */
function exportableBasket(b) {
  const out = { archives: [...(b.archives || [])], fonds: [...(b.fonds || [])],
                charters: [...(b.charters || [])], memo: b.memo || "" };
  if (b.bitVector) { out.bitVector = b.bitVector; out.bitHash = b.bitHash; out.n = b.n || 0; }
  return out;
}

/** Build the export envelope from a BasketStore (every EDITABLE basket; `All` is excluded). */
export function buildExport(store, { indexHash = null, now = () => new Date() } = {}) {
  const baskets = {};
  for (const name of store.editableNames) baskets[name] = exportableBasket(store.basket(name));
  return { format: EXPORT_FORMAT, version: EXPORT_VERSION, exported_at: now().toISOString(),
           index_hash: indexHash, baskets };
}

/** The gzipped bytes of `buildExport`, ready for a Blob/download. */
export async function exportBytes(store, opts = {}) {
  const json = JSON.stringify(buildExport(store, opts), null, 1);
  return gzip(new TextEncoder().encode(json));
}

/** Read an exported file (gzipped or plain) into `{ indexHash, baskets, exportedAt }`.
 *  Throws a human-readable Error on anything unusable -- the caller shows it to the user. */
export async function parseBasketsFile(bytes) {
  bytes = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let text;
  try {
    text = new TextDecoder().decode(isGzip(bytes) ? await gunzip(bytes) : bytes);
  } catch (e) {
    throw new Error(`not a readable gzip file (${(e && e.message) || e})`);
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    throw new Error(`file is not JSON (${(e && e.message) || e})`);
  }
  if (!parsed || typeof parsed !== "object") throw new Error("file is not a JSON object");
  // envelope, or a bare {name: basket} map written by hand / another tool
  const baskets = (parsed.baskets && typeof parsed.baskets === "object") ? parsed.baskets : parsed;
  const names = Object.keys(baskets).filter((n) => baskets[n] && typeof baskets[n] === "object");
  if (names.length === 0) throw new Error("no baskets in this file");
  return { baskets, indexHash: parsed.index_hash || null, exportedAt: parsed.exported_at || null };
}

/**
 * Check imported ids against the CURRENT shared index: the DB may have grown, been re-sliced or
 * re-cut since the export, so ids can be gone. Returns per-type missing ids (capped for display)
 * and totals; the caller warns the user. Nothing is dropped -- a missing id stays in the basket, so
 * re-importing against the full DB later resurrects it.
 *
 * @param baskets {name: {archives, fonds, charters}}
 * @param index   a SharedIndex (fsdb_sharedindex.js); `classify(id)` answers what an id is now
 */
export function validateAgainstIndex(baskets, index, { signal = null, sample = 5 } = {}) {
  const missing = { archives: [], fonds: [], charters: [] };   // a few ids each, for the message
  const counts = { archives: 0, fonds: 0, charters: 0 };       // how many are gone
  const totals = { archives: 0, fonds: 0, charters: 0 };       // how many were checked
  const singular = { archives: "archive", fonds: "fond", charters: "charter" };
  const perBasket = {};
  for (const [name, b] of Object.entries(baskets)) {
    throwIfAborted(signal);
    let n = 0;
    for (const type of ["archives", "fonds", "charters"]) {
      for (const id of b[type] || []) {
        totals[type]++;
        if (index.classify(id) !== singular[type]) {
          n++;
          counts[type]++;
          if (missing[type].length < sample) missing[type].push(id);
        }
      }
    }
    if (n) perBasket[name] = n;
  }
  const total = counts.archives + counts.fonds + counts.charters;
  return { total, counts, totals, missing, perBasket };
}

/** The alert() text for a validation result; null when everything resolved. */
export function missingWarning(result, { indexHash = null, fileIndexHash = null } = {}) {
  if (!result || !result.total) return null;
  const lines = [
    `${result.total} of the imported ids are NOT in the database currently loaded by this service.`,
    "",
    `  archives: ${result.counts.archives} of ${result.totals.archives}`,
    `  fonds:    ${result.counts.fonds} of ${result.totals.fonds}`,
    `  charters: ${result.counts.charters} of ${result.totals.charters}`,
  ];
  const affected = Object.entries(result.perBasket);
  if (affected.length) {
    lines.push("", "affected baskets: " + affected.map(([n, c]) => `${n} (${c})`).join(", "));
  }
  const sample = [...result.missing.archives, ...result.missing.fonds, ...result.missing.charters];
  if (sample.length) lines.push("", "for example: " + sample.slice(0, 5).join(", "));
  if (fileIndexHash && indexHash && fileIndexHash !== indexHash) {
    lines.push("", `The file was exported against index ${fileIndexHash.slice(0, 8)}, this service`
                 + ` serves ${indexHash.slice(0, 8)} — a different slice or a changed database.`);
  }
  lines.push("", "The baskets were imported unchanged: the missing ids are kept, so importing the"
              + " same file against the full database will resolve them.");
  return lines.join("\n");
}
