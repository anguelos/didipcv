// ddp_scope.js -- turn the ACTIVE BASKET into the request scope of a server-rendered page.
//
// The shared client half of the DiDip scope contract (server half: ddp_microservices/scope.py +
// @scoped_ms / scoped_route). A scoped page is rendered by the server ALREADY scoped; this module
// only decides how the basket travels and re-navigates when it changes:
//
//   * SCOPE TRAVELS BY POST. A basket can be huge -- a packed bit-vector over ~1M charters is
//     ~90 kB -- so it is never put in a URL (that yields 414 Request-URI Too Long). Every scoped
//     navigation submits a hidden form field `scope`; the server reads it from `request.values`,
//     so plain `?scope=` still works for curl/API clients.
//   * INTERNAL LINKS ARE BARE. A scoped page renders its own links as ordinary paths carrying
//     class="ddp-nav" (pagination, class lists, ...). A plain click on one would be a GET and would
//     drop the scope, so we intercept it and re-POST the active basket to the same href. This is
//     why paging keeps working with the ordinary `create_pagers` / generic_pagination.html links.
//   * RECONCILE ON LOAD. Landing on a scoped route unscoped (bookmark, sibling hand-off, topnav)
//     while a basket is active re-POSTs once so the view matches the basket. (An HTTP redirect
//     cannot do this: a redirect cannot invent a body the client never sent.)
//
// Consequence, accepted by design: POST-rendered scoped views are not bookmarkable. A bookmarkable
// GET path for small baskets is a deferred power-user option.
//
// Usage: templates just `{% include "_scope.html" %}` and emit the `#ddp-scope` marker; the include
// auto-initialises this module. Nothing else is needed in app templates.
import { openStore, syncIndex } from "./fsdb_sharedindex_store.js";
import { BasketStore, CLIENT_CONFIG, loadClientConfig, ddpBase } from "./fsdb_basket.js";

const msg = (t) => document.dispatchEvent(new CustomEvent("ddp:message", { detail: { text: t } }));

/** Raw `/basket/db` container bytes for this service, OPFS-cached (`force` re-checks the server). */
export async function dbBytes(base, force = false) {
  const ns = base.replace(/^\//, "");
  return syncIndex(base, {
    store: await openStore(ns), blobStore: await openStore(),
    deserialize: (b) => b, ...(force ? { maxAgeMs: 0 } : {}),
  });
}

/** The active basket as a compact WIRE BASKET object, or null for the whole DB (All / empty).
 *
 * The encoding half of this module, usable WITHOUT the navigation half. A page that wants the
 * basket as the argument of an *action* rather than as the filter of a *view* -- the Slicer's
 * export is the case in point -- imports this and posts it itself; it must NOT include
 * `_scope.html`, whose auto-init re-navigates the whole page whenever the basket changes and
 * would discard anything the user had typed into a form. */
export async function scopeWire(store, base, force = false) {
  if (store.isActiveAll || store.isEmpty()) return null;
  return store.compactAgainst(await dbBytes(base, force));
}

/** The active basket as a wire-basket JSON string, or "" for the whole DB (All / empty basket). */
async function wireStr(store, base, force = false) {
  const wire = await scopeWire(store, base, force);
  return wire ? JSON.stringify(wire) : "";
}

/** Navigate to `path` under scope `s`: POST form when scoped, plain GET when whole-DB. */
function go(path, s) {
  if (!s) { location.assign(path); return; }
  const f = document.createElement("form");
  f.method = "POST";
  f.action = path;
  const i = document.createElement("input");
  i.type = "hidden";
  i.name = "scope";
  i.value = s;
  f.appendChild(i);
  document.body.appendChild(f);
  f.submit();
}

/** Did the server render this page under a scope? (`data-applied` is set by the template.) */
function alreadyScoped(marker) {
  return marker.dataset.applied === "1" || !!new URLSearchParams(location.search).get("scope");
}

/** Wire the current page: intercept `a.ddp-nav` clicks, reconcile on load, re-navigate on change. */
export async function initScope(store) {
  const marker = document.getElementById("ddp-scope");
  if (!marker) return;                       // not a scoped page
  await loadClientConfig();
  const base = ddpBase();
  const basepath = marker.dataset.basepath || location.pathname;

  document.addEventListener("click", async (e) => {
    const a = e.target.closest && e.target.closest("a.ddp-nav");
    // plain left-click only: let modified/middle clicks open a new tab (unscoped) as usual
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    try {
      go(a.getAttribute("href"), await wireStr(store, base));
    } catch (err) {
      msg("scope error: " + ((err && err.message) || err));
    }
  });

  try {
    if (!alreadyScoped(marker)) {
      const s = await wireStr(store, base);
      if (s) { go(basepath, s); return; }     // reconcile: render this page under the active basket
    }
    store.addEventListener("change", async () => {
      try {
        go(basepath, await wireStr(store, base));
      } catch (err) {
        msg("scope error: " + ((err && err.message) || err));
      }
    });
  } catch (err) {
    msg("scope error: " + ((err && err.message) || err));
  }
}

/** Auto-initialise from the shared include: reuse the basket widget's store when one is mounted, so
 *  both read (and react to) the SAME active basket. `_basket.html` publishes the still-pending
 *  mount as `window.ddpBasketWidgetReady`, so we await it rather than racing it; with no widget on
 *  the page we open the store directly. */
export async function autoInitScope() {
  if (!document.getElementById("ddp-scope")) return;
  await loadClientConfig();
  let widget = null;
  try {
    widget = await (window.ddpBasketWidgetReady || Promise.resolve(null));
  } catch { /* widget failed to mount; fall back to our own store */ }
  const store = (widget && widget.store)
    || await BasketStore.open({ maxSize: CLIENT_CONFIG.max_basket_size });
  return initScope(store);
}
