// Node bridge for the cross-language shared-index tests. Invoked by test_shared_index.py:
//
//   node js_bridge.mjs <module.mjs> serialize   <dataset.json> <out.bin>
//   node js_bridge.mjs <module.mjs> reserialize  <in.bin>       <out.bin>
//   node js_bridge.mjs <module.mjs> todataset    <in.bin>       <out.json>
//
// <module.mjs> is a copy of ddp_microservices/static/fsdb_sharedindex.js with an .mjs
// extension (so Node loads the browser ES module without a package.json). A "dataset" is
// the neutral JSON both languages build blocks from:
//   {index_hash, meta, blocks:[{name, kind:"S", width, values:[...]} | {name, kind:"i4", values:[...]}]}
import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [, , modulePath, cmd, inPath, outPath] = process.argv;
const mod = await import(pathToFileURL(modulePath).href);

function datasetToBlocks(dataset) {
  const blocks = {};
  for (const b of dataset.blocks) {
    blocks[b.name] = b.kind === "S"
      ? { kind: "S", width: b.width, values: b.values }
      : { kind: "i4", values: b.values };
  }
  return blocks;
}

function infoToDataset(info) {
  const blocks = [];
  for (const [name, blk] of Object.entries(info.blocks)) {
    blocks.push(blk.kind === "S"
      ? { name, kind: "S", width: blk.width, values: blk.values }
      : { name, kind: "i4", values: Array.from(blk.values) });
  }
  return { index_hash: info.index_hash, meta: info.meta, blocks };
}

function readBin(p) {
  const d = readFileSync(p);
  return d.buffer.slice(d.byteOffset, d.byteOffset + d.byteLength); // fresh, aligned ArrayBuffer
}

if (cmd === "serialize") {
  const dataset = JSON.parse(readFileSync(inPath, "utf8"));
  const ab = mod.serializeDb(datasetToBlocks(dataset), dataset.index_hash ?? "", dataset.meta ?? {});
  writeFileSync(outPath, Buffer.from(new Uint8Array(ab)));
} else if (cmd === "reserialize") {
  const info = mod.deserializeDb(readBin(inPath));
  const ab = mod.serializeDb(info.blocks, info.index_hash, info.meta);
  writeFileSync(outPath, Buffer.from(new Uint8Array(ab)));
} else if (cmd === "todataset") {
  const info = mod.deserializeDb(readBin(inPath));
  writeFileSync(outPath, JSON.stringify(infoToDataset(info)));
} else {
  throw new Error(`unknown command: ${cmd}`);
}
