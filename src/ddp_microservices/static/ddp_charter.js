// ddp_charter.js -- behaviour for the shared charter viewer (_charter_modes.html).
//
// The charter view is one greedy full-height box (.ddp-charter-viewer) with a single radio row of
// view modes at the top. This module, dependency-free and loaded by base.html, makes it work and is
// a no-op on pages that have no viewer:
//   * height-fit: the viewer grows to fill the viewport left under the topnav + header (measured, so
//     it is robust to a header/subhead of any height) and re-fits on resize.
//   * radio switching: shows the panel whose data-mode matches the checked radio; honours the
//     server-set default (the `checked`/data-default-mode) -- so e.g. Static opens on the main image.
//   * image mode: prev/next steps through the images in CH.image_urls.json order (main image first).
//   * CEI mode: lazy-fetches the raw xml from data-cei-url the first time the mode is shown; copy button.

function fitHeight(viewer) {
  const top = viewer.getBoundingClientRect().top;
  // leave a hair at the bottom so a page scrollbar never appears just from the viewer
  const h = Math.max(240, Math.floor(window.innerHeight - top - 8));
  viewer.style.height = h + "px";
}

function showMode(viewer, mode) {
  for (const panel of viewer.querySelectorAll(".ddp-mode-panel"))
    panel.hidden = panel.dataset.mode !== mode;
  const active = viewer.querySelector(`.ddp-mode-panel[data-mode="${mode}"]`);
  if (active) initPanel(active);
}

function initPanel(panel) {
  const cei = panel.querySelector(".ddp-cei");
  if (cei) loadCei(cei);
}

async function loadCei(cei) {
  if (cei.dataset.loaded) return;
  cei.dataset.loaded = "1";                 // once; avoid refetch on every switch
  const pre = cei.querySelector(".ddp-cei-text");
  const url = cei.dataset.ceiUrl;
  if (!url) { pre.textContent = "no CEI source for this charter"; return; }
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error("HTTP " + r.status);
    pre.textContent = await r.text();
  } catch (e) {
    cei.dataset.loaded = "";               // allow a retry on next switch
    pre.textContent = "could not load CEI: " + e.message;
  }
}

function wireCeiCopy(viewer) {
  const btn = viewer.querySelector(".ddp-cei-copy");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const pre = viewer.querySelector(".ddp-cei-text");
    try {
      await navigator.clipboard.writeText(pre.textContent);
      const was = btn.textContent; btn.textContent = "copied"; setTimeout(() => (btn.textContent = was), 1200);
    } catch {
      // clipboard blocked -> select the text so the user can copy manually
      const sel = window.getSelection(); const range = document.createRange();
      range.selectNodeContents(pre); sel.removeAllRanges(); sel.addRange(range);
    }
  });
}

function wireImageFit(viewer) {
  const fit = viewer.querySelector(".ddp-imgfit");
  if (!fit) return;
  const imgs = [...fit.querySelectorAll(".ddp-imgfit-img")];
  if (imgs.length === 0) return;
  const counter = fit.querySelector(".ddp-imgfit-counter");
  let cur = imgs.findIndex((im) => im.classList.contains("current"));
  if (cur < 0) cur = 0;
  const render = () => {
    imgs.forEach((im, i) => im.classList.toggle("current", i === cur));
    if (counter) counter.textContent = `${cur + 1} / ${imgs.length}`;
    imgs[cur].loading = "eager";           // make sure the shown one loads
  };
  const step = (d) => { cur = (cur + d + imgs.length) % imgs.length; render(); };
  fit.querySelector(".ddp-imgfit-nav.prev")?.addEventListener("click", () => step(-1));
  fit.querySelector(".ddp-imgfit-nav.next")?.addEventListener("click", () => step(1));
  // arrow keys while the image mode is active
  viewer.addEventListener("keydown", (e) => {
    const active = viewer.querySelector('.ddp-mode-panel[data-mode]:not([hidden]) .ddp-imgfit');
    if (active !== fit) return;
    if (e.key === "ArrowLeft") { step(-1); e.preventDefault(); }
    else if (e.key === "ArrowRight") { step(1); e.preventDefault(); }
  });
  render();
}

function initViewer(viewer) {
  const radios = [...viewer.querySelectorAll('input[name="ddp-charter-mode"]')];
  const checked = radios.find((r) => r.checked && !r.disabled)
                || radios.find((r) => !r.disabled);
  const start = checked ? checked.value : (viewer.dataset.defaultMode || "");
  if (checked) checked.checked = true;

  for (const r of radios)
    r.addEventListener("change", () => { if (r.checked) showMode(viewer, r.value); });

  wireImageFit(viewer);
  wireCeiCopy(viewer);

  fitHeight(viewer);
  let raf = 0;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => fitHeight(viewer));
  });

  showMode(viewer, start);
}

function init() {
  for (const v of document.querySelectorAll(".ddp-charter-viewer")) initViewer(v);
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
}
