// ddp_views.js -- the standardized "what's on screen" view-model, exposed in the DOM.
//
// A DiDip microservice page MAY declare, via `render_views` (templates/_macros.html), any of
// the standard views below. Each is a singleton marker element `#viewed_<type>` carrying its
// value in `data-value`; one of them is additionally tagged `.ddp-view-context` -- the page's
// current context, i.e. the thing a context-sensitive widget (like the basket) acts on.
//
//   viewed_archive          data-value = archive id (string)
//   viewed_fond             data-value = fond md5
//   viewed_charter          data-value = charter md5
//   viewed_charter_list     data-value = space-separated md5s ON THIS PAGE (page-scoped)
//   viewed_charter_ranking  data-value = space-separated md5s ON THIS PAGE, in rank order
//   viewed_root             (marker for the top-level / home view; value optional). The root view
//                           is the service's DEFAULT landing page (`/<prefix>/`).
//   viewed_other            (custom catch-all: a service-specific page that is none of the above;
//                           value optional). Not a basket context / DnD target by convention.
//
// Read them THROUGH this helper (never hardcode selectors) so the storage detail stays in one
// place. The values are READ-ONLY by convention. Dependency-free ES module.

export const VIEW_TYPES = ["root", "archive", "fond", "charter", "charter_list", "charter_ranking", "other"];
const LIST_VIEWS = new Set(["charter_list", "charter_ranking"]); // value is a list of md5s

/** The value of a standard view (`type` without the `viewed_` prefix), or null if the page
 *  does not expose it. Scalar views return a string; list/ranking views return an array. */
export function viewed(type) {
  const el = document.getElementById("viewed_" + type);
  if (!el) return null;
  const v = (el.dataset.value ?? "").trim();
  return LIST_VIEWS.has(type) ? (v ? v.split(/\s+/) : []) : v;
}

/** The page's declared current context -- the view also tagged `.ddp-view-context` -- as
 *  `{ type, value }`, or null. A context-sensitive widget acts on this. */
export function viewedContext() {
  const el = document.querySelector(".ddp-view-context");
  if (!el || !el.id.startsWith("viewed_")) return null;
  const type = el.id.slice(7); // "viewed_".length
  return { type, value: viewed(type) };
}
