// ddp_ctxnav.js -- the context navigation rail (#ctx_nav): the right-hand dock every DiDip page
// carries. Markup lives in base.html; this module owns its behaviour: the two layout gestures
// (collapse, resize) and the shared-index status row pinned under it.
//
//   * COLLAPSE, devtools-style: expanded, the rail is docked to the right edge and PUSHES the page
//     (body padding + the message bar follow --ddp-ctxnav-w). Totally collapsed, the rail is gone --
//     no strip, no reserved width -- and the only thing left is a single expand button floating at
//     the top right of the topnav (#ctx_nav_expand).
//   * RESIZE: drag the rail's left edge; double-click it to reset to the configured default.
//
// State is one CSS custom property on <html> plus one class:
//     --ddp-ctxnav-w   the rail's width; ABSENT (-> 0px from ddp_style.css) means collapsed
//     .ctxnav-collapsed
// so CSS alone lays out the page and there is nothing to keep in sync. Both are restored before
// first paint by the tiny inline script in base.html <head>; this module then takes over.
//
// Widths are a percentage of the viewport (the user asked for "15% of the page width"), so the rail
// keeps its proportion when the window is resized. The default and the clamps come from the client
// config (static/default_client_config.json), never from a constant here.

import { CLIENT_CONFIG, loadClientConfig } from "./fsdb_basket.js";
import { postMessage } from "./fsdb_messagebar.js";

const PCT_KEY = "ddp:ctxnav:pct";              // last width the user dragged to, in vw percent
const COLLAPSED_KEY = "ddp:ctxnav:collapsed";  // "1" while totally collapsed

/** The config knobs, read through CLIENT_CONFIG so the JSON stays the single source of truth.
 *  The `??` fallbacks only matter before loadClientConfig() resolves (or if the fetch failed). */
const defaultPct = () => Number(CLIENT_CONFIG.ctxnav_default_pct ?? 15);
const minPct = () => Number(CLIENT_CONFIG.ctxnav_min_pct ?? 8);
const maxPct = () => Number(CLIENT_CONFIG.ctxnav_max_pct ?? 60);

const clamp = (pct) => Math.min(maxPct(), Math.max(minPct(), pct));

function storedPct() {
  try {
    const v = Number(localStorage.getItem(PCT_KEY));
    return Number.isFinite(v) && v > 0 ? v : null;
  } catch { return null; }   // private mode / storage disabled
}

function store(key, value) {
  try { localStorage.setItem(key, value); } catch { /* not persisted; the session still works */ }
}

/** Write the width to the DOM. Collapsing REMOVES the property rather than zeroing it, so the
 *  stylesheet's `:root { --ddp-ctxnav-w: 0px }` is what applies -- one declaration, no !important. */
function applyWidth(pct) {
  document.documentElement.style.setProperty("--ddp-ctxnav-w", pct + "%");
}

export class CtxNav {
  constructor(rail, { expandBtn = null } = {}) {
    this.rail = rail;
    this.expandBtn = expandBtn;
    this.pct = clamp(storedPct() ?? defaultPct());
  }

  get collapsed() { return document.documentElement.classList.contains("ctxnav-collapsed"); }

  mount() {
    if (!this.collapsed) applyWidth(this.pct);   // first visit: the head script had no stored width
    this._syncAria();
    this.rail.querySelector(".ctx-nav-collapse")?.addEventListener("click", () => this.setCollapsed(true));
    this.expandBtn?.addEventListener("click", () => this.setCollapsed(false));
    this._initResize();
  }

  setCollapsed(on) {
    document.documentElement.classList.toggle("ctxnav-collapsed", on);
    if (on) document.documentElement.style.removeProperty("--ddp-ctxnav-w");
    else applyWidth(this.pct);
    store(COLLAPSED_KEY, on ? "1" : "0");
    this._syncAria();
    // the charter viewer (and anything else) sizes itself off the viewport: let it re-fit
    window.dispatchEvent(new Event("resize"));
  }

  setPct(pct, { persist = false } = {}) {
    this.pct = clamp(pct);
    applyWidth(this.pct);
    if (persist) store(PCT_KEY, String(this.pct));
  }

  _syncAria() {
    this.expandBtn?.setAttribute("aria-expanded", String(!this.collapsed));
    this.rail.querySelector(".ctx-nav-collapse")?.setAttribute("aria-expanded", String(!this.collapsed));
  }

  /** Drag the rail's left edge. Pointer capture keeps the drag alive over iframes/images, and the
   *  width is persisted once on release (not on every move) so localStorage is not hammered. */
  _initResize() {
    const grip = this.rail.querySelector(".ctx-nav-resize");
    if (!grip) return;
    const pctFromX = (clientX) => ((window.innerWidth - clientX) / window.innerWidth) * 100;

    grip.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      grip.setPointerCapture(e.pointerId);
      document.documentElement.classList.add("ctxnav-resizing");   // kills transitions + text selection
      const move = (ev) => this.setPct(pctFromX(ev.clientX));
      const stop = () => {
        grip.removeEventListener("pointermove", move);
        document.documentElement.classList.remove("ctxnav-resizing");
        this.setPct(this.pct, { persist: true });
        window.dispatchEvent(new Event("resize"));
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", stop, { once: true });
      grip.addEventListener("pointercancel", stop, { once: true });
    });

    grip.addEventListener("dblclick", () => {
      this.setPct(defaultPct(), { persist: true });
      window.dispatchEvent(new Event("resize"));
    });
  }
}

/** The shared-index status row at the foot of the rail (#ctx_nav_index): does our OPFS-cached index
 *  match the server's, and a button to force a fresh fetch.
 *
 *  The syncer belongs to the basket widget, so we reach it the same decoupled way ddp_scope.js does
 *  -- await the published mount promise -- and then `widget.syncerReady`, because the syncer itself
 *  is built asynchronously and would otherwise still be undefined here.
 *
 *  The row is developer-mode-only, but that is enforced in CSS (body.ddp-debug), not here: the
 *  wiring is cheap and staying mounted means toggling `window.ddpDebug` at the console reveals a
 *  LIVE row rather than a dead one. */
export async function mountIndexRow() {
  const row = document.getElementById("ctx_nav_index");
  if (!row) return null;                          // no shared index on this service -> no row
  const mark = row.querySelector(".ctx-nav-idx-mark");
  const hash = row.querySelector(".ctx-nav-idx-hash");
  const btn = row.querySelector(".ctx-nav-idx-update");

  let widget = null;
  try {
    widget = await (window.ddpBasketWidgetReady || Promise.resolve(null));
    if (widget) await widget.syncerReady;         // resolves once this.syncer exists (or failed)
  } catch { widget = null; }                      // basket store unavailable -> the row just says ✗
  const syncer = widget ? widget.syncer : null;

  const render = () => {
    const syncing = !!syncer && syncer.state === "syncing";
    // "matching" = the syncer confirmed our cached hash against the server's. NOTE it also reports
    // "synced" when the poll could not reach the server but a cached index exists, so the title
    // carries the confirmation time -- a stale timestamp is how you spot that case.
    const ok = !!syncer && syncer.state === "synced" && !!syncer.indexHash;
    // "synced" is also what the syncer reports when the poll FAILED but a cached index exists, so
    // a lingering lastError means "usable from cache, not actually confirmed" -- amber, not green.
    // (That mapping is exactly what hid the Illegal-invocation bug for so long.)
    const stale = ok && !!syncer.lastError;
    mark.textContent = syncing ? "…" : (ok ? "✓" : "✗");
    mark.className = "ctx-nav-idx-mark" + (syncing ? "" : (stale ? " warn" : ok ? " ok" : " bad"));
    mark.title = !syncer ? "no shared-index syncer (OPFS unavailable on an insecure origin?)"
      : syncing ? "fetching the shared index…"
      : stale ? `serving a CACHED index — the last server check failed:\n${syncer.lastError}`
      : ok ? `index matches the server · confirmed ${new Date(syncer.confirmedAt).toLocaleString()}`
      : `index NOT confirmed against the server (state: ${syncer.state})`
        + (syncer.lastError ? `\n${syncer.lastError}` : "");
    hash.textContent = syncer && syncer.indexHash ? syncer.indexHash.slice(0, 8) : "—";
    btn.disabled = !syncer || syncing;
  };

  btn.addEventListener("click", async () => {
    if (!syncer) return;
    postMessage("shared index: fetching…");
    btn.disabled = true;
    try {
      await syncer.resync();                      // ignores the recheck throttle AND the same-hash shortcut
      // a "synced" state with a lastError is a CACHED index, not a successful fetch -- say so
      // rather than reporting a false "done" (which is what the old code did on Static/Slicer).
      postMessage(syncer.lastError
        ? `shared index: fetch failed — ${syncer.lastError}`
        : `shared index: done — ${syncer.indexHash ? syncer.indexHash.slice(0, 8) : "(no hash)"}`);
    } catch (e) {
      postMessage("shared index: fetch failed — " + (e && e.message ? e.message : e));
    } finally {
      render();
    }
  });

  for (const ev of ["statechange", "syncprogress", "confirm"]) syncer?.addEventListener(ev, render);
  render();
  return { row, syncer, render };
}

/** Wire the rail rendered by base.html. No-op on a page without one. */
export async function mountCtxNav() {
  const rail = document.getElementById("ctx_nav");
  if (!rail) return null;
  await loadClientConfig();                       // the width default/clamps live in the client config
  const nav = new CtxNav(rail, { expandBtn: document.getElementById("ctx_nav_expand") });
  nav.mount();
  mountIndexRow();                                // independent: it waits on the basket widget
  return nav;
}

if (typeof document !== "undefined") {
  window.ddpCtxNavReady = mountCtxNav();
}
