// slicer_basket_download.js -- the Slicer's four-zone export form.
//
// ZONES (see .claude/slicer_redesign.md):
//   1 export parameters (content / format / filename / missing-ids)
//   2 source mode      -- "from a basket" or "custom selection"
//   3 the selection    -- a basket combobox, or a drop area for archives/fonds/charters
//   4 the download button
//
// BOTH zone-3 modes end in the SAME place: a compact wire basket POSTed as the standard `scope`
// field to /sl/download_basket, which is a `@self.scoped_route`. There is one download path, not
// two. The basket mode encodes via BasketStore.compactNamed (the shared basket layer); the custom
// mode builds the identical wire shape from the dropped ids via SharedIndex.sendBasket. Nothing
// about the compact/POST/409 sequence is hand-rolled here -- that is ddp_scope.js's contract.
//
// This page must NOT include `_scope.html`: ddp_scope.js's auto-init re-navigates on every basket
// change, which on a form would discard what the user has entered. Only the encoding half
// (`scopeWire` / `dbBytes`) is imported.
//
// NOTE ON NAMES: the export's own scope (fsdb / fsdb_noimg / fsdb_and_apps) travels as
// `export_scope`. `scope` belongs to the basket layer; using it for both made the scope guard
// reject a working export.
import { BasketStore, CLIENT_CONFIG, loadClientConfig, ddpBase, DRAG_TYPE } from "./fsdb_basket.js";
import { SharedIndex } from "./fsdb_sharedindex.js";
import { dbBytes } from "./ddp_scope.js";   // OPFS-cached /basket/db sync (encoding stays shared)

const msg = (text) => document.dispatchEvent(new CustomEvent("ddp:message", { detail: { text } }));
const $ = (id) => document.getElementById(id);

// ---- export-size confirmation ----------------------------------------------------------------
const KB = 1024, MB = 1024 * 1024;
const PER_CHARTER_WITH_IMAGES = 20 * MB;   // rough upper bound for a full-FSDB (images) charter
const PER_CHARTER_NO_IMAGES = 500 * KB;    // rough upper bound for a no-images (FSDB+CEI) charter
const ID_SPLIT = /[\s,;]+/;

function humanSize(bytes) {
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let v = bytes, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

/** Confirm an export of `n` charters, estimating the upper-bound size from the export scope. */
function confirmExport(n, exportScope) {
  const withImages = exportScope !== "fsdb_noimg";
  const per = withImages ? PER_CHARTER_WITH_IMAGES : PER_CHARTER_NO_IMAGES;
  const each = withImages ? "~20 MB (with images)" : "~500 KB (without images)";
  return window.confirm(
    `Export ${n} charter${n === 1 ? "" : "s"}.\n\n` +
    `This could be as big as ${humanSize(n * per)} (${each} per charter).\n\nContinue?`);
}

/** The shared index for `base`, parsed. Container bytes come from ddp_scope.js's OPFS-cached
 *  `dbBytes`, so this file keeps no copy of that sync. */
async function sharedIndex(base, force = false) {
  return SharedIndex.fromContainer(await dbBytes(base, force));
}

// ---- zone 1: export parameters ---------------------------------------------------------------
function exportOptions() {
  return {
    format: $("format_select").value,
    export_scope: $("scope_select").value,
    prefered_filename: $("prefered_filename").value || "fsdb_slice",
    tolerate_missing: $("tolerate_missing").checked,
  };
}

// ---- zone 3b: the custom selection (dropped / pasted ids, classified by the index) ------------
class CustomSelection {
  constructor(ix, onChange) {
    this.ix = ix;
    this.onChange = onChange;
    this.items = new Map();   // id -> "archive" | "fond" | "charter"
  }

  /** Add whitespace/comma-separated ids, classifying each against the shared index. Unknown ids
   *  are reported rather than silently dropped -- a typo'd md5 would otherwise just vanish. */
  add(text) {
    const unknown = [];
    let added = 0;
    for (const raw of String(text || "").split(ID_SPLIT)) {
      const id = raw.trim();
      if (!id || this.items.has(id)) continue;
      const kind = this.ix.classify(id);
      if (!kind) { unknown.push(id); continue; }
      this.items.set(id, kind);
      added++;
    }
    if (unknown.length) {
      msg(`Not in this slice, ignored: ${unknown.slice(0, 3).join(", ")}` +
          (unknown.length > 3 ? ` (+${unknown.length - 3} more)` : ""));
    }
    if (added) this.onChange();
    return added;
  }

  remove(id) { if (this.items.delete(id)) this.onChange(); }

  /** The wire basket for this selection, or null when nothing is selected. */
  wire() {
    if (!this.items.size) return null;
    const of = (kind) => [...this.items].filter(([, k]) => k === kind).map(([id]) => id);
    return this.ix.sendBasket({
      charterIds: of("charter"), fondIds: of("fond"), archiveIds: of("archive"),
    });
  }

  render(box) {
    box.textContent = "";
    if (!this.items.size) {
      const p = document.createElement("span");
      p.className = "placeholder";
      p.textContent = "Drag items from any DiDip service, or paste identifiers below.";
      box.appendChild(p);
      return;
    }
    for (const [id, kind] of this.items) {
      const chip = document.createElement("span");
      chip.className = `chip ${kind}`;
      chip.title = `${kind}: ${id}`;
      chip.append(`${kind[0].toUpperCase()} ${id.length > 12 ? id.slice(0, 10) + "…" : id}`);
      const x = document.createElement("button");
      x.type = "button";
      x.textContent = "×";
      x.setAttribute("aria-label", `remove ${id}`);
      x.addEventListener("click", () => this.remove(id));
      chip.appendChild(x);
      box.appendChild(chip);
    }
  }
}

// ---- the form ---------------------------------------------------------------------------------
export async function initSlicerForm({ prefill = {} } = {}) {
  await loadClientConfig();
  const base = ddpBase();
  const store = await BasketStore.open({ maxSize: CLIENT_CONFIG.max_basket_size });
  const ix = await sharedIndex(base);

  const dropBox = $("ddp-drop"), note = $("selection-note"), btn = $("ddp-download");
  const basketSel = $("basket_select");
  const custom = new CustomSelection(ix, () => { custom.render(dropBox); refresh(); });

  // zone 3a: every basket, with the ACTIVE one pre-selected (choosing another does not switch it)
  for (const name of store.names) {
    const o = document.createElement("option");
    o.value = name;
    // `All` carries its meaning in a flag, not an id list, so its counts are 0 by construction --
    // labelling it "0 items" would read as an empty basket.
    o.textContent = store.isAll(name) ? `${name} (whole database)`
                                      : `${name} (${store.totalCount(name)} items)`;
    if (name === store.activeName) o.selected = true;
    basketSel.appendChild(o);
  }

  const mode = () => document.querySelector('input[name="source_mode"]:checked').value;
  const isAllSelected = () => mode() === "basket" && store.isAll(basketSel.value);

  /** The wire basket for whichever zone-3 mode is active (null = nothing selected).
   *  Always `compactNamed` in basket mode -- going through `scopeWire` for the active basket
   *  would encode `All` as null there and as `{all_charters:true}` elsewhere. */
  const currentWire = async () =>
    (mode() === "basket"
      ? store.compactNamed(basketSel.value, await dbBytes(base))
      : custom.wire());

  /** Recompute the "n charters" note and the button's enabled state. */
  async function refresh() {
    $("zone-basket").hidden = mode() !== "basket";
    $("zone-custom").hidden = mode() !== "custom";
    // `All` means the whole DB, which the export route refuses on purpose; say so and stop
    // rather than enabling a button whose request is guaranteed to fail.
    if (isAllSelected()) {
      note.textContent = "The whole database — pick a narrower basket; the Slicer will not export everything.";
      btn.disabled = true;
      return;
    }
    let n = 0;
    try {
      const wire = await currentWire();
      n = wire ? ix.receiveBasket(wire).length : 0;
    } catch (e) {
      note.textContent = "Could not resolve the selection: " + ((e && e.message) || e);
      btn.disabled = true;
      return;
    }
    note.textContent = `${n} charter${n === 1 ? "" : "s"} selected.`;
    btn.disabled = n === 0;
  }

  // zone 2 + zone 3a changes
  for (const r of document.querySelectorAll('input[name="source_mode"]')) {
    r.addEventListener("change", refresh);
  }
  basketSel.addEventListener("change", refresh);
  $("scope_select").addEventListener("change", refresh);

  // zone 3b: drops (the shared charter DRAG_TYPE, or any text/plain identifiers) + paste box
  dropBox.addEventListener("dragover", (e) => { e.preventDefault(); dropBox.classList.add("over"); });
  dropBox.addEventListener("dragleave", () => dropBox.classList.remove("over"));
  dropBox.addEventListener("drop", (e) => {
    e.preventDefault();
    dropBox.classList.remove("over");
    const dt = e.dataTransfer;
    custom.add(dt.getData(DRAG_TYPE) || dt.getData("text/plain") || "");
  });
  $("paste_ids").addEventListener("change", (e) => { custom.add(e.target.value); e.target.value = ""; });

  // a pre-filled /sl/charter|fond|archive/<id> hand-off lands in the custom zone
  const seeded = [prefill.charters, prefill.fonds, prefill.archives].filter(Boolean).join(" ").trim();
  if (seeded) {
    custom.add(seeded);
    document.querySelector('input[name="source_mode"][value="custom"]').checked = true;
  }
  custom.render(dropBox);

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try { await runDownload(base, store, ix, custom, currentWire); }
    catch (e) { msg("Export error: " + ((e && e.message) || e)); }
    finally { refresh(); }
  });

  await refresh();
}

async function runDownload(base, store, ix, custom, currentWire) {
  const opts = exportOptions();
  let wire = await currentWire();
  if (!wire) { msg("Nothing selected to export."); return; }

  const n = ix.receiveBasket(wire).length;
  if (n === 0) { msg("The selection resolves to 0 charters in this slice."); return; }
  if (!confirmExport(n, opts.export_scope)) { msg("Export cancelled."); return; }

  msg(`Preparing export (${n} charters)…`);
  let resp = await postExport(base, wire, opts);
  if (resp.status === 409) {                         // index changed -> resync + retry once
    msg("Shared index changed on the server; re-syncing and retrying…");
    await dbBytes(base, true);
    resp = await postExport(base, await currentWire(), opts);
  }
  if (!resp.ok) {
    let detail = "";
    try { detail = " (" + ((await resp.json()).error || "") + ")"; } catch { /* non-JSON body */ }
    msg(`Export failed: HTTP ${resp.status}${detail}.`);
    return;
  }
  triggerDownload(await resp.blob(), `${opts.prefered_filename}.${opts.format}`);
  msg("Export ready.");
}

/** POST the wire basket as the standard `scope` field; the server reads it via the inherited
 *  scope proxy and answers 409 index_mismatch from the base handler. */
function postExport(base, scope, opts) {
  return fetch(`${base}/download_basket`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, ...opts }),
  });
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}
