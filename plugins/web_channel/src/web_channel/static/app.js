/* web_channel — the one page-level behaviour that Basecoat (CSS-only here) does not
   ship: an object row `<tr data-href="…">` navigates like its primary link. The
   primary cell still holds a real <a> (keyboard, middle-click, crawlers); this only
   widens the click target to the whole row. Clicks on controls inside the row are
   left alone. Ctrl/⌘-click opens a new tab, like a link would. */
document.addEventListener("click", function (e) {
  var tr = e.target.closest("tr[data-href]");
  if (!tr) return;
  if (e.target.closest("a, button, input, select, textarea, label, form")) return;
  var href = tr.getAttribute("data-href");
  if (!href) return;
  if (e.metaKey || e.ctrlKey || e.button === 1) {
    window.open(href, "_blank", "noopener");
  } else {
    window.location.assign(href);
  }
});
