// ddp_busy.js -- run a long task while blocking the context rail, with a live signal and an abort.
//
// Used for the basket export/import, which touch OPFS and the shared index and can take a while on
// a large slice. While the task runs the WHOLE rail (#ctx_nav) is covered: nothing inside it can be
// clicked, an indeterminate bar keeps moving so the page never looks hung, and an Abort button
// cancels via a standard AbortController.
//
// The task receives `{ signal, status }`:
//   signal - an AbortSignal; long loops should call `throwIfAborted(signal)` between chunks, and
//            any fetch/stream can simply be passed the signal.
//   status - `status("resolving 1200/98000 charters…")` updates the overlay's line in place.
// Aborting rejects with an AbortError, so callers catch it exactly like any other failure.
//
// Deliberately its own module (not part of ddp_ctxnav.js): the basket widget needs it too, and
// importing the rail from the basket -- which the rail already imports -- would make a cycle.
// Dependency-free, self-styling, web-standard ESM.

const OVERLAY_CLASS = "ddp-busy-overlay";

/** Throw the standard AbortError if `signal` has been aborted. Call it between chunks of work. */
export function throwIfAborted(signal) {
  if (signal && signal.aborted) {
    const e = new Error("aborted");
    e.name = "AbortError";
    throw e;
  }
}

export const isAbort = (e) => !!e && e.name === "AbortError";

let _styled = false;
function injectStyle() {
  if (_styled || (typeof document !== "undefined" && document.getElementById("ddp-busy-style"))) { _styled = true; return; }
  _styled = true;
  const style = document.createElement("style");
  style.id = "ddp-busy-style";
  style.textContent = CSS;
  document.head.append(style);
}

function buildOverlay(label, onAbort) {
  const el = document.createElement("div");
  el.className = OVERLAY_CLASS;
  el.innerHTML =
    '<div class="ddp-busy-box">' +
      '<div class="ddp-busy-bar"><div class="ddp-busy-fill"></div></div>' +
      '<div class="ddp-busy-label"></div>' +
      '<div class="ddp-busy-status"></div>' +
      '<button type="button" class="ddp-busy-abort">Abort</button>' +
    "</div>";
  el.querySelector(".ddp-busy-label").textContent = label;
  el.querySelector(".ddp-busy-abort").addEventListener("click", onAbort);
  return el;
}

/**
 * Run `task` with the rail blocked.
 *
 * @param task    async ({signal, status}) => result
 * @param target  element or selector to cover (default: the context rail; falls back to no overlay
 *                when the page has none, so the task still runs headless / in tests)
 * @param label   what is happening, shown above the status line
 * @returns the task's result; rejects with the task's error (AbortError when the user aborts)
 */
export async function runBlocking(task, { target = "#ctx_nav", label = "working…" } = {}) {
  const host = typeof target === "string"
    ? (typeof document === "undefined" ? null : document.querySelector(target))
    : target;
  const ctl = new AbortController();
  if (!host) return task({ signal: ctl.signal, status: () => {} });

  injectStyle();
  const overlay = buildOverlay(label, () => {
    ctl.abort();
    overlay.querySelector(".ddp-busy-abort").disabled = true;
    overlay.querySelector(".ddp-busy-status").textContent = "aborting…";
  });
  const statusEl = overlay.querySelector(".ddp-busy-status");
  host.append(overlay);
  host.setAttribute("aria-busy", "true");
  try {
    return await task({ signal: ctl.signal, status: (t) => { statusEl.textContent = t || ""; } });
  } finally {
    overlay.remove();
    host.removeAttribute("aria-busy");
  }
}

const CSS = `
.${OVERLAY_CLASS} { position: absolute; inset: 0; z-index: 10; display: flex;
  align-items: center; justify-content: center; padding: .8rem;
  background: color-mix(in srgb, Canvas 82%, transparent); backdrop-filter: blur(1px);
  font: 13px/1.4 system-ui, sans-serif; color: CanvasText; }
.${OVERLAY_CLASS} .ddp-busy-box { width: 100%; max-width: 16rem; text-align: center; }
.${OVERLAY_CLASS} .ddp-busy-bar { height: 4px; border-radius: 2px; overflow: hidden;
  background: rgba(128,128,128,.25); }
/* the "still working" signal: a bar that never stops moving, so a slow step never reads as a hang */
.${OVERLAY_CLASS} .ddp-busy-fill { width: 40%; height: 100%; background: #3b82f6;
  animation: ddp-busy-slide 1.1s ease-in-out infinite; }
@keyframes ddp-busy-slide { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
.${OVERLAY_CLASS} .ddp-busy-label { margin-top: .5rem; font-variant: small-caps; opacity: .85; }
.${OVERLAY_CLASS} .ddp-busy-status { margin-top: .2rem; min-height: 1.2em; opacity: .7;
  font-size: 12px; word-break: break-word; }
.${OVERLAY_CLASS} .ddp-busy-abort { margin-top: .6rem; cursor: pointer; padding: .2rem .8rem;
  font: inherit; color: inherit; background: transparent; border-radius: .3rem;
  border: 1px solid rgba(200,70,70,.6); color: rgb(200,70,70); }
.${OVERLAY_CLASS} .ddp-busy-abort:disabled { opacity: .4; cursor: default; }
@media (prefers-reduced-motion: reduce) {
  .${OVERLAY_CLASS} .ddp-busy-fill { animation-duration: 3s; }
}
`;
