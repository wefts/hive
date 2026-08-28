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

function scrollToAskFragment(fragment, block) {
  if (!fragment) return;
  var behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  fragment.scrollIntoView({ behavior: behavior, block: block || "center" });
}

document.body.addEventListener("htmx:afterSwap", function (e) {
  var path = e.detail && e.detail.requestConfig && e.detail.requestConfig.path;
  if (!path) return;

  if (path.endsWith("/ask/start")) {
    var pending = e.detail.target && e.detail.target.querySelector(".post-pending:last-child");
    scrollToAskFragment((pending && pending.querySelector(".reply-q, .post-q")) || pending, "center");
    return;
  }

  if (path.endsWith("/ask")) {
    var replies = document.querySelectorAll(".post-reply:not(.post-pending)");
    var posts = document.querySelectorAll(".post-object:not(.post-pending)");
    var latest = replies.length ? replies[replies.length - 1] : posts[posts.length - 1];
    scrollToAskFragment((latest && latest.querySelector(".post-answer")) || latest, "center");
  }
});
