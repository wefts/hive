/* Dashboard connections graph (ADR-3, hive). Drives the vendored Cytoscape against
   two scope-enforcing JSON endpoints — the kernel stays the scope authority; this
   only lays out + renders verbatim ids/keys/relations (presentation-determinism).
   No network of its own beyond same-origin fetches to our own routes. */
(function () {
  "use strict";

  // Dark palette mirrors app.css (Cytoscape style is JS, not CSS-var aware).
  var STYLE = [
    { selector: "node", style: {
        "background-color": "#1c222c", "border-color": "#262d38", "border-width": 1,
        label: "data(label)", color: "#e9eef5", "font-size": 11,
        "text-valign": "center", "text-halign": "center", "text-wrap": "wrap",
        "text-max-width": 90, width: 26, height: 26, "text-margin-y": 0 } },
    { selector: "node.center", style: {
        "background-color": "#1f6feb", "border-color": "#6cb0ff", width: 36, height: 36,
        color: "#ffffff", "font-weight": 600 } },
    { selector: "node.entity", style: { "background-color": "#151a22", shape: "round-rectangle" } },
    { selector: "edge", style: {
        width: 1.5, "line-color": "#3a4452", "target-arrow-color": "#3a4452",
        "target-arrow-shape": "triangle", "curve-style": "bezier",
        label: "data(relation)", "font-size": 9, color: "#8b95a4",
        "text-rotation": "autorotate", "text-background-color": "#0b0e14",
        "text-background-opacity": 0.7, "text-background-padding": 2 } },
  ];

  var cy = null;
  function ensureCy() {
    if (cy) return cy;
    cy = cytoscape({
      container: document.getElementById("cy"),
      style: STYLE,
      wheelSensitivity: 0.2,
      minZoom: 0.2, maxZoom: 3,
    });
    cy.on("tap", "node", function (evt) {
      var n = evt.target;
      load(n.id(), n.data("label"), true); // click to expand
    });
    return cy;
  }

  function hide(id) { var el = document.getElementById(id); if (el) el.style.display = "none"; }
  function setText(id, t) { var el = document.getElementById(id); if (el) el.textContent = t; }

  async function search(event) {
    event.preventDefault();
    var q = document.getElementById("graph-q").value.trim();
    var hits = document.getElementById("graph-hits");
    hits.innerHTML = "";
    if (!q) return false;
    try {
      var res = await fetch("/dashboard/search?q=" + encodeURIComponent(q));
      var data = await res.json();
      if (!data.hits || data.hits.length === 0) {
        hits.innerHTML = '<li class="muted">no matches for "' + escapeHtml(q) + '"</li>';
        return false;
      }
      data.hits.forEach(function (h) {
        var li = document.createElement("li");
        li.className = "hit";
        var a = document.createElement("a");
        a.className = "hit-link";
        a.href = "#";
        a.innerHTML = '<span class="htype">' + escapeHtml(h.type) + "</span> " +
          '<span class="hkey">' + escapeHtml(h.key) + "</span>";
        a.onclick = function (e) { e.preventDefault(); hits.innerHTML = ""; load(h.id, h.key, false); };
        li.appendChild(a);
        hits.appendChild(li);
      });
    } catch (e) {
      hits.innerHTML = '<li class="muted">search unavailable</li>';
    }
    return false;
  }

  async function load(id, label, expand) {
    var c = ensureCy();
    hide("graph-empty");
    try {
      var res = await fetch("/dashboard/graph/" + encodeURIComponent(id));
      var g = await res.json();
      if (g.status !== "found") {
        if (!expand) { c.elements().remove(); }
        setText("graph-meta", "Nothing connected to that entity is visible to you.");
        return;
      }
      if (!expand) { c.elements().remove(); }
      addNode(c, String(id), label || ("#" + id), "center");
      (g.nodes || []).forEach(function (n) {
        addNode(c, String(n.id), n.key || ("#" + n.id), n.type === "entity" ? "entity" : "");
      });
      (g.edges || []).forEach(function (e) {
        var eid = "e" + e.src_id + "-" + e.dst_id + "-" + e.relation;
        if (c.getElementById(eid).length === 0 &&
            c.getElementById(String(e.src_id)).length && c.getElementById(String(e.dst_id)).length) {
          c.add({ data: { id: eid, source: String(e.src_id), target: String(e.dst_id), relation: e.relation } });
        }
      });
      c.layout({ name: "concentric", concentric: function (n) { return n.hasClass("center") ? 2 : 1; },
                 levelWidth: function () { return 1; }, minNodeSpacing: 40, animate: true, animationDuration: 300 }).run();
      c.fit(undefined, 40);
      setText("graph-meta", "center #" + g.center_id + " · " + (g.nodes || []).length +
        " nodes · " + (g.edges || []).length + " edges" + (g.truncated ? " · truncated" : ""));
    } catch (e) {
      setText("graph-meta", "Graph unavailable.");
    }
  }

  function addNode(c, id, label, cls) {
    if (c.getElementById(id).length) return;
    var n = c.add({ data: { id: id, label: label } });
    if (cls) n.addClass(cls);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  window.swarmGraph = { search: search, load: load };
})();
