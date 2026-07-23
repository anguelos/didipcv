// ddp_collapse.js -- remember whether each standardized view description is expanded.
//
// The description boxes (`view_description` in templates/_macros.html) are plain <details>, so the
// COLLAPSING itself is native and works with this module absent or broken. All this does is
// persist the user's choice, keyed by VIEW TYPE ('root', 'archive', 'fond', …) rather than by page,
// so collapsing the fond description once keeps every fond page's description collapsed -- while
// leaving the archive one alone.
//
// The server renders the box expanded; a stored preference overrides that on load. No stored
// preference means "leave whatever the template rendered", so a service can ship a view collapsed
// by default just by omitting `open`. Loaded by base.html; a no-op on a page with no descriptions.

const KEY = (type) => `ddp:collapse:${type}`;
const SELECTOR = "details.ddp-view-desc[data-view]";

function read(type) {
  try { return localStorage.getItem(KEY(type)); } catch { return null; }   // storage disabled
}

function write(type, open) {
  try { localStorage.setItem(KEY(type), open ? "1" : "0"); } catch { /* not persisted */ }
}

/** Apply the stored preference to one <details> and keep it in sync from then on. */
export function wire(el) {
  if (el._ddpCollapseWired) return;
  el._ddpCollapseWired = true;
  const type = el.dataset.view;
  const stored = read(type);
  if (stored === "0") el.open = false;
  else if (stored === "1") el.open = true;
  // `toggle` fires for both directions, and only on an actual change
  el.addEventListener("toggle", () => write(type, el.open));
}

export function wireAll(root = document) {
  for (const el of root.querySelectorAll(SELECTOR)) wire(el);
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => wireAll());
  else wireAll();
}
