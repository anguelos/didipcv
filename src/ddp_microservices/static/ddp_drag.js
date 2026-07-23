// ddp_drag.js -- make charter thumbnails draggable into the basket widget.
//
// Any element carrying `data-charter-md5="<md5>"` (or `data-ddp-charter`) becomes draggable and,
// when dropped on the basket widget, adds that charter to the active basket. The payload MIME is
// the single source of truth `DRAG_TYPE` exported by fsdb_basket.js, so producers and the drop
// target can never disagree. Include once per page:
//   <script type="module" src="{{ url_for('static', filename='ddp_drag.js') }}"></script>
// It also wires elements added later (a MutationObserver), so paginated/AJAX thumbnails work.

import { DRAG_TYPE } from "./fsdb_basket.js";

const SELECTOR = "[data-charter-md5], [data-ddp-charter]";

function md5Of(el) {
  return el.getAttribute("data-charter-md5") || el.getAttribute("data-ddp-charter") || "";
}

function wire(el) {
  if (el._ddpDragWired) return;
  const md5 = md5Of(el);
  if (!md5) return;
  el._ddpDragWired = true;
  el.setAttribute("draggable", "true");
  el.classList.add("ddp-draggable");
  el.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData(DRAG_TYPE, md5);
    e.dataTransfer.effectAllowed = "copy";
  });
}

function wireAll(root = document) {
  for (const el of root.querySelectorAll(SELECTOR)) wire(el);
}

if (typeof document !== "undefined") {
  const run = () => {
    wireAll();
    // pick up thumbnails inserted after load (pagination, lazy render)
    new MutationObserver((muts) => {
      for (const m of muts) for (const n of m.addedNodes) {
        if (n.nodeType !== 1) continue;
        if (n.matches && n.matches(SELECTOR)) wire(n);
        if (n.querySelectorAll) wireAll(n);
      }
    }).observe(document.body, { childList: true, subtree: true });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else run();
}

export { wire, wireAll };
