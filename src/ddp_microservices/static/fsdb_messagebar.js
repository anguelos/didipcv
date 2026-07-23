// fsdb_messagebar.js -- a global, full-width message bar pinned to the bottom of the page.
//
// Markup lives in base.html (#ddp-msgbar); this module styles it and wires its behaviour.
// Collapsed by default: one line showing the toggle, the LAST message, and a Clear button.
// Expanded (grows upward): a scrollable log of all messages, oldest at top / newest at bottom
// (auto-scrolled to the newest). Expand state persists in localStorage.
//
// DECOUPLED integration: any code posts a message by dispatching a DOM CustomEvent --
//   document.dispatchEvent(new CustomEvent("ddp:message", { detail: { text: "…" } }))
// so callers need not import this module. `postMessage(text)` is a convenience wrapper.
// Dependency-free, web-standard ES module.

const EXPANDED_KEY = "ddp:msgbar:expanded";
const MAX_LINES = 1000; // cap the DOM so a chatty page can't grow unbounded

/** Convenience: post a message to the bar (no import needed elsewhere -- dispatch the event). */
export function postMessage(text) {
  try { document.dispatchEvent(new CustomEvent("ddp:message", { detail: { text } })); } catch { /* no DOM */ }
}

function injectStyle() {
  if (document.getElementById("ddp-msgbar-style")) return;
  const style = document.createElement("style");
  style.id = "ddp-msgbar-style";
  style.textContent = CSS;
  document.head.append(style);
}

function append(text, log, last, isExpanded) {
  if (text == null) return;
  const line = `${new Date().toLocaleTimeString()}  ${text}`;
  const div = document.createElement("div");
  div.className = "ddp-msg-line";
  div.textContent = line;
  log.append(div);
  while (log.childElementCount > MAX_LINES) log.firstElementChild.remove();
  last.textContent = line;
  if (isExpanded()) log.scrollTop = log.scrollHeight; // newest into view
}

function init(bar) {
  const log = bar.querySelector(".ddp-msg-log");
  const last = bar.querySelector(".ddp-msg-last");
  const toggle = bar.querySelector(".ddp-msg-toggle");
  const clear = bar.querySelector(".ddp-msg-clear");
  let expanded = localStorage.getItem(EXPANDED_KEY) === "1";

  const render = () => {
    bar.classList.toggle("expanded", expanded);
    bar.classList.toggle("collapsed", !expanded);
    toggle.textContent = expanded ? "▼" : "▲";
    toggle.title = expanded ? "Collapse messages" : "Expand messages";
    if (expanded) log.scrollTop = log.scrollHeight;
  };

  toggle.addEventListener("click", () => {
    expanded = !expanded;
    localStorage.setItem(EXPANDED_KEY, expanded ? "1" : "0");
    render();
  });
  clear.addEventListener("click", () => { log.textContent = ""; last.textContent = ""; });
  document.addEventListener("ddp:message", (e) => append(e.detail && e.detail.text, log, last, () => expanded));
  render();
}

const CSS = `
body { padding-bottom: 2.2rem; } /* keep page content clear of the collapsed bar */
/* The right edge yields to the context rail (--ddp-ctxnav-w, see ddp_style.css); 0px while the rail
   is collapsed. The declaration lives HERE because this injected stylesheet outranks ddp_style.css.
   NOTE: no backticks in this comment -- it sits inside a template literal. */
.ddp-msgbar { position: fixed; left: 0; right: var(--ddp-ctxnav-w, 0px); bottom: 0; z-index: 2000;
  display: flex; flex-direction: column; background: Canvas; color: CanvasText;
  border-top: 1px solid rgba(128,128,128,.5); box-shadow: 0 -2px 10px rgba(0,0,0,.15);
  font: 12px/1.5 ui-monospace, monospace; }
.ddp-msgbar .ddp-msg-log { display: none; overflow-y: auto; max-height: 40vh;
  padding: .3rem .6rem; border-bottom: 1px solid rgba(128,128,128,.3); }
.ddp-msgbar.expanded .ddp-msg-log { display: block; }
.ddp-msgbar .ddp-msg-line { white-space: pre-wrap; word-break: break-word; padding: .05rem 0; }
.ddp-msgbar .ddp-msg-head { display: flex; align-items: center; gap: .5rem; padding: .2rem .6rem; }
.ddp-msgbar .ddp-msg-last { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; opacity: .85; }
.ddp-msgbar.expanded .ddp-msg-last { visibility: hidden; }
.ddp-msgbar button { cursor: pointer; border: 1px solid rgba(128,128,128,.5); background: transparent;
  color: inherit; border-radius: .3rem; padding: .1rem .5rem; font: inherit; line-height: 1.3; }
`;

injectStyle();
const bar = document.getElementById("ddp-msgbar");
if (bar) init(bar);

// surface a deprecation notice when we arrived via a redirected /paged_* URL (?_deprecated=…)
try {
  const dep = new URLSearchParams(location.search).get("_deprecated");
  if (dep) postMessage(`Deprecated URL "${dep}" — redirected to the current route; please update links.`);
} catch { /* no location */ }
