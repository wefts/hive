/* Memory Map dashboard. Drives vendored Cytoscape against same-origin, scope-
   enforcing JSON endpoints. The channel renders verbatim route data only; empty
   and unavailable states are explicit rather than fabricated. */
(function () {
  "use strict";

  // Cytoscape style is JS, not CSS-var aware; these colors mirror app.css tokens.
  var STYLE = [
    { selector: "node", style: {
        "background-color": "#1c222c", "border-color": "#3a4452", "border-width": 1,
        label: "data(label)", color: "#e9eef5", "font-size": 13,
        "text-valign": "center", "text-halign": "center", "text-wrap": "wrap",
        "text-max-width": 96, width: "data(size)", height: "data(size)", "text-margin-y": 0 } },
    { selector: "node.center", style: {
        "background-color": "#1f6feb", "border-color": "#6cb0ff", width: 40, height: 40,
        color: "#ffffff", "font-weight": 600 } },
    { selector: "node.entity", style: { "background-color": "#151a22", shape: "round-rectangle" } },
    { selector: "node.scope-public", style: { "border-color": "#6cb0ff" } },
    { selector: "node.scope-group", style: { "border-color": "#8b95a4", "border-width": 2 } },
    { selector: "node.depth-2", style: { opacity: 0.78 } },
    { selector: "node.selected", style: {
        "border-color": "#ffffff", "border-width": 3, "underlay-color": "#6cb0ff",
        "underlay-opacity": 0.18, "underlay-padding": 7 } },
    { selector: "edge", style: {
        width: "data(width)", "line-color": "#3a4452", "target-arrow-color": "#3a4452",
        "target-arrow-shape": "triangle", "curve-style": "bezier",
        label: "data(relation)", "font-size": 11, color: "#8b95a4",
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
      selectNode(n);
      load(n.id(), nodePayload(n), true);
    });
    return cy;
  }

  function hide(id) { var el = document.getElementById(id); if (el) el.style.display = "none"; }
  function setText(id, t) { var el = document.getElementById(id); if (el) el.textContent = t; }
  function setHidden(id, value) { var el = document.getElementById(id); if (el) el.hidden = value; }

  async function search(event) {
    event.preventDefault();
    var q = document.getElementById("graph-q").value.trim();
    var hits = document.getElementById("graph-hits");
    hits.innerHTML = "";
    setText("graph-status", q ? "Searching memory..." : "Enter an entity to search.");
    if (!q) return false;
    try {
      var res = await fetch("/dashboard/search?q=" + encodeURIComponent(q));
      var data = await res.json();
      if (!data.hits || data.hits.length === 0) {
        hits.innerHTML = '<li class="muted">no matches for "' + escapeHtml(q) + '"</li>';
        setText("graph-status", "No visible matches.");
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
        a.onclick = function (e) {
          e.preventDefault();
          hits.innerHTML = "";
          load(h.id, { id: h.id, key: h.key, type: h.type, score: h.score }, false);
        };
        li.appendChild(a);
        hits.appendChild(li);
      });
      setText("graph-status", data.hits.length + " visible match" + (data.hits.length === 1 ? "" : "es") + ".");
    } catch (e) {
      hits.innerHTML = '<li class="muted">search unavailable</li>';
      setText("graph-status", "Search unavailable.");
    }
    return false;
  }

  async function load(id, seed, expand) {
    var c = ensureCy();
    hide("graph-empty");
    setText("graph-status", "Loading visible neighborhood...");
    try {
      var res = await fetch("/dashboard/graph/" + encodeURIComponent(id));
      var g = await res.json();
      if (g.status !== "found") {
        if (!expand) { c.elements().remove(); }
        setText("graph-status", g.status === "error" ? "Graph unavailable." : "Nothing connected to that entity is visible to you.");
        updateMeta({ center_id: id, nodes: [], edges: [], relations: [], truncated: false });
        if (seed) {
          markCenter(c, String(id));
          selectNode(addNode(c, normalizeNode(seed, 0, true)));
          c.layout({ name: "concentric", animate: true, animationDuration: 200 }).run();
          c.fit(undefined, 40);
        }
        return;
      }
      if (!expand) { c.elements().remove(); }
      var center = normalizeNode(seed || { id: id, key: "#" + id }, 0, true);
      addNode(c, center);
      (g.nodes || []).forEach(function (n) {
        addNode(c, normalizeNode(n, n.depth, false));
      });
      (g.edges || []).forEach(function (e) {
        var eid = "e" + e.src_id + "-" + e.dst_id + "-" + e.relation;
        if (c.getElementById(eid).length === 0 &&
            c.getElementById(String(e.src_id)).length && c.getElementById(String(e.dst_id)).length) {
          c.add({ data: {
            id: eid, source: String(e.src_id), target: String(e.dst_id),
            relation: e.relation || "related", reliability: e.reliability,
            width: edgeWidth(e.reliability),
          } });
        }
      });
      markCenter(c, String(id));
      selectNode(c.getElementById(String(id)));
      c.layout({ name: "concentric", concentric: function (n) { return n.hasClass("center") ? 3 : 2 - Number(n.data("depth") || 1); },
                 levelWidth: function () { return 1; }, minNodeSpacing: 44, animate: true, animationDuration: 250 }).run();
      c.fit(undefined, 40);
      updateMeta(g);
      setText("graph-status", "Showing visible neighborhood for #" + g.center_id + ".");
    } catch (e) {
      setText("graph-status", "Graph unavailable.");
    }
  }

  function normalizeNode(n, depth, center) {
    var id = String(n.id);
    var confidence = typeof n.confidence === "number" ? n.confidence : undefined;
    if (confidence === undefined && typeof n.score === "number") confidence = n.score;
    return {
      id: id,
      label: displayLabel(n.key || ("#" + id)),
      key: n.key || "",
      type: n.type || "",
      scope: n.scope || "",
      confidence: confidence,
      depth: Number(depth || n.depth || 0),
      center: Boolean(center),
      size: nodeSize(confidence, center),
    };
  }

  function addNode(c, data) {
    var id = String(data.id);
    var existing = c.getElementById(id);
    var n = existing.length ? existing : c.add({ data: data });
    if (existing.length) n.data(data);
    n.removeClass("entity center scope-public scope-group depth-2");
    if (data.type === "entity") n.addClass("entity");
    if (data.center) n.addClass("center");
    if (data.scope) n.addClass("scope-" + cleanClass(data.scope));
    if (Number(data.depth || 0) >= 2) n.addClass("depth-2");
    return n;
  }

  function markCenter(c, id) {
    c.nodes(".center").removeClass("center");
    var n = c.getElementById(id);
    if (n.length) n.addClass("center");
  }

  function selectNode(n) {
    if (!n || !n.length) return;
    if (cy) cy.nodes(".selected").removeClass("selected");
    n.addClass("selected");
    renderSelected(n.data());
  }

  function renderSelected(data) {
    var kv = document.getElementById("selected-node-kv");
    if (!kv) return;
    var rows = [
      ["id", data.id],
      ["key", data.key || data.label],
      ["type", data.type],
      ["scope", data.scope],
      ["confidence", formatNumber(data.confidence)],
      ["depth", data.depth],
    ].filter(function (row) {
      return row[1] !== undefined && row[1] !== null && row[1] !== "";
    });
    kv.innerHTML = rows.map(function (row) {
      return "<div><dt>" + escapeHtml(row[0]) + "</dt><dd>" + escapeHtml(row[1]) + "</dd></div>";
    }).join("");
    setHidden("selected-node-empty", rows.length > 0);
    kv.hidden = rows.length === 0;
  }

  function updateMeta(g) {
    var nodes = g.nodes || [];
    var edges = g.edges || [];
    var relations = g.relations || unique(edges.map(function (e) { return e.relation; }).filter(Boolean));
    setText("graph-center", g.center_id ? "#" + g.center_id : "none");
    setText("graph-node-count", String(nodes.length + (g.center_id ? 1 : 0)));
    setText("graph-edge-count", String(edges.length));
    setText("graph-relations", relations.length ? relations.join(", ") : "none");
    setText("graph-bounds", g.truncated ? "truncated by kernel bounds" : "visible neighborhood");
  }

  function nodePayload(n) {
    return {
      id: n.id(),
      key: n.data("key") || n.data("label"),
      type: n.data("type"),
      scope: n.data("scope"),
      confidence: n.data("confidence"),
      depth: n.data("depth"),
    };
  }

  function nodeSize(confidence, center) {
    if (center) return 38;
    if (typeof confidence !== "number") return 28;
    return 24 + Math.max(0, Math.min(1, confidence)) * 12;
  }

  function edgeWidth(reliability) {
    if (typeof reliability !== "number") return 1.5;
    return 1 + Math.max(0, Math.min(1, reliability)) * 2;
  }

  function cleanClass(value) {
    return String(value).toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
  }

  function unique(values) {
    return values.filter(function (v, idx) { return values.indexOf(v) === idx; }).sort();
  }

  function formatNumber(value) {
    if (typeof value !== "number") return "";
    return value.toFixed(2);
  }

  function displayLabel(value) {
    var s = String(value || "");
    if (s.length <= 34) return s;
    return s.slice(0, 15) + "..." + s.slice(-14);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  window.swarmGraph = { search: search, load: load };
})();
