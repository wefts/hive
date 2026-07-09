"""Pure host-side rule DSL for the curated who-group overlay. No LDAP, no graph — given a person's
axis-map and a group spec, decide membership. See board/doing/who-groups-overlay-design.md."""
from __future__ import annotations
import fnmatch
import yaml

AXES = ("org", "team", "family", "status", "site", "title")


def load_groups(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    out = []
    for g in doc.get("groups", []):
        if not g.get("slug"):
            continue
        out.append({
            "slug": str(g["slug"]),
            "name": str(g.get("name", g["slug"])),
            "aliases": [str(a) for a in (g.get("aliases") or [])],
            "rule": g.get("rule"),
            "include": [str(u) for u in (g.get("include") or [])],
            "exclude": [str(u) for u in (g.get("exclude") or [])],
        })
    return out


def match_leaf(axis_val: str, pattern: str) -> bool:
    return fnmatch.fnmatch((axis_val or "").lower(), (pattern or "").lower())


def _eval_node(node, axes) -> bool:
    if node is None:
        return False
    if "all" in node:
        return all(_eval_node(c, axes) for c in node["all"])
    if "any" in node:
        return any(_eval_node(c, axes) for c in node["any"])
    if "not" in node:
        return not _eval_node(node["not"], axes)
    # leaf: {axis: value | [values]}
    for axis in AXES:
        if axis in node:
            pats = node[axis]
            pats = pats if isinstance(pats, list) else [pats]
            return any(match_leaf(axes.get(axis, ""), p) for p in pats)
    return False


def evaluate(group: dict, axes: dict) -> bool:
    uid = axes.get("uid", "")
    if uid and uid in group.get("exclude", []):
        return False
    if uid and uid in group.get("include", []):
        return True
    rule = group.get("rule")
    return _eval_node(rule, axes) if rule else False
