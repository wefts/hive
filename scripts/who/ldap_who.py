#!/usr/bin/env python3
"""Host-side LDAP "who is who" connector (world-map master-plan E1).

Reads the org directory (anonymous bind, INSIDE the network — this is reference data already
anonymously readable there, NOT chat-derived private person data), applies a STRICT field
allowlist, and emits DISTILLED facts as JSON for the kernel loader. The kernel never speaks LDAP;
only distilled facts enter the graph (a Docker volume).

LEAK POSTURE (workspace hard boundary):
  - The directory host + base DN are intranet specifics: they come from ENV, never this committed
    file. Set them in a gitignored env (hive/env/<stage>.env or the shell) before running:
        SWARM_LDAP_HOST     e.g. directory.<...>.intranet   (host or ldap:// URL)
        SWARM_LDAP_BASE_DN  e.g. dc=example,dc=org
        SWARM_LDAP_PORT     optional, default 389
  - Output JSON goes to tmp/ (gitignored). Only DISTILLED facts (allowlisted attrs + org-structure
    relations) are ever written; auth/system attrs are never even fetched.
  - This script prints ONLY aggregate counts to stdout — never a name, uid, or attribute value —
    so a captured log leaks nothing.

FIELD ALLOWLIST (ADR-16, non-negotiable): only org-relevant identity/structure attributes. Auth-
sensitive / system-internal attributes (ssh keys, MFA secrets, phone, home dir, login shell,
uid/gid numbers, password hashes, pwd* policy state) are NEVER requested from the server.

Emits (to --out, default tmp/who_facts.json):
  {
    "origin": "ldap:directory",           # single authoritative evidential source
    "reliability": 0.9,                    # authoritative directory read (not an LLM hypothesis)
    "evidence_kind": "observation",        # external fact, not a generated claim
    "lineage": "ldap:directory",           # single upstream source
    "profiles": [ {uid, cn, given_name, sn, title, ou, department, o, l, mail, room, employment} ],
    "facts": [ {subject, subject_kind, relation, object, object_kind} ],  # org-structure + in_group +
                                                                          # managed_by_team
    "groups": [ {slug, name, aliases} ],  # curated group overlay metadata (from SWARM_WHO_GROUPS)
    "services": [ {slug, name, aliases} ],  # curated service overlay metadata (from SWARM_WHO_SERVICES)
    "teams": [ {slug, name} ]  # owning-team metadata referenced by managed_by_team facts
  }

Relations (governed): person managed_by person | works_in org (employing entity, from ou) |
member_of team (finer department, from departmentNumber, decoded to a readable label) |
has_title role | located_at site | has_employment status (employee/contractor) |
has_role_family family (developer/hr/sysadmin/etc., clustered from the messy free-text title).
Subjects/objects are STABLE keys (person=uid, org=ou, team=decoded dept, role=title, site=l,
status/family=the category). Human-readable attrs ride in `profiles` so the kernel stores a citable,
searchable profile.

Run (host-side, from the workspace):
    python3 hive/scripts/who/ldap_who.py --out hive/tmp/who_facts.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from groups import load_groups, evaluate

# Path to groups.<stage>.yaml (curated group overlay spec); empty ⇒ skip group evaluation entirely.
GROUPS_SPEC = os.environ.get("SWARM_WHO_GROUPS", "")

# Path to services.<stage>.yaml (curated service->team overlay spec); empty ⇒ skip entirely.
SERVICES_SPEC = os.environ.get("SWARM_WHO_SERVICES", "")

# Path to ldap_schema.<stage>.yaml (directory-specific schema/decoder maps).
LDAP_SCHEMA_SPEC = os.environ.get("SWARM_WHO_LDAP_SCHEMA", "")


LDAP_SCHEMA_REQUIRED_KEYS = frozenset(
    {
        "staff_object_classes",
        "mfa_attr",
        "employment_class",
        "non_staff_pattern",
        "role_family_rules",
        "dept_country",
        "dept_seg",
        "dept_func",
        "loc_company_prefix",
        "loc_city",
        "leaver_pwdlock",
    }
)


def load_services(path):
    import yaml
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    out = []
    for s in doc.get("services", []):
        if not s.get("slug") or not (s.get("team") or {}).get("slug"):
            continue
        out.append({"slug": str(s["slug"]), "name": str(s.get("name", s["slug"])),
                    "aliases": [str(a) for a in (s.get("aliases") or [])],
                    "team_slug": str(s["team"]["slug"]), "team_name": str(s["team"].get("name", s["team"]["slug"]))})
    return out


def _default_ldap_schema_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "..", "env", "ldap_schema.example.yaml")


def load_ldap_schema(path):
    import yaml

    schema_path = path
    if not schema_path:
        schema_path = _default_ldap_schema_path()
        print(
            "using SYNTHETIC example schema — set SWARM_WHO_LDAP_SCHEMA for real output",
            file=sys.stderr,
        )
    with open(schema_path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"LDAP schema {schema_path} must be a mapping")
    missing = sorted(LDAP_SCHEMA_REQUIRED_KEYS - set(doc))
    if missing:
        raise ValueError(f"LDAP schema {schema_path} missing required keys: {', '.join(missing)}")
    return doc


LDAP_SCHEMA = load_ldap_schema(LDAP_SCHEMA_SPEC)
MFA_ATTR = str(LDAP_SCHEMA["mfa_attr"])

# The ONLY attributes we ever fetch. Anything not here is never requested (defense at the source,
# not just at emit time). Keys are LDAP attribute names; values are the profile field we map to.
ALLOWLIST = {
    "uid": "uid",
    "cn": "cn",
    "givenName": "given_name",
    "sn": "sn",
    "title": "title",
    "ou": "ou",
    "departmentNumber": "department",
    "o": "o",
    "l": "l",
    "mail": "mail",
    "roomNumber": "room",
    "manager": "manager",  # a DN — resolved to a uid locally, never emitted as a raw DN
}

# Attributes we MUST NOT fetch (asserted for clarity; anything outside ALLOWLIST is excluded anyway).
DENYLIST = frozenset(
    {
        "sshPublicKey",
        MFA_ATTR,
        "mobile",
        "telephoneNumber",
        "homeDirectory",
        "loginShell",
        "uidNumber",
        "gidNumber",
        "userPassword",
    }
)

# Directory-specific schema and decoder maps. These values live in SWARM_WHO_LDAP_SCHEMA; the
# committed fallback is synthetic and intentionally not useful for real output.
STAFF_OBJECT_CLASSES = [str(v) for v in LDAP_SCHEMA["staff_object_classes"]]
LEAVER_PWDLOCK = str(LDAP_SCHEMA["leaver_pwdlock"])
EMPLOYMENT_CLASS = {str(k).lower(): str(v) for k, v in LDAP_SCHEMA["employment_class"].items()}
NON_STAFF = re.compile(str(LDAP_SCHEMA["non_staff_pattern"]), re.I)
ROLE_FAMILY_RULES = [(str(fam), str(rx)) for fam, rx in LDAP_SCHEMA["role_family_rules"]]
ROLE_FAMILY = [(fam, re.compile(rx, re.I)) for fam, rx in ROLE_FAMILY_RULES]


def is_non_staff(title: str) -> bool:
    return bool(NON_STAFF.search(title or ""))


def role_family(title: str):
    """Coarse role family for a title, or None if unmatched (no family edge emitted)."""
    for fam, rx in ROLE_FAMILY:
        if rx.search(title or ""):
            return fam
    return None


# departmentNumber is the fine TEAM/department, often coded as country_entity_function. Decode known
# segments to readable labels (unknown segments kept as-is) so team answers and search read naturally.
DEPARTMENT_COUNTRY = {str(k).lower(): str(v) for k, v in LDAP_SCHEMA["dept_country"].items()}
DEPARTMENT_SEGMENT = {str(k).lower(): str(v) for k, v in LDAP_SCHEMA["dept_seg"].items()}
DEPARTMENT_FUNCTION = {str(k).lower(): str(v) for k, v in LDAP_SCHEMA["dept_func"].items()}


# E-location: the `l` field may code entity_agency or a bare agency/city. located_at resolves to the
# clean agency/site; the entity prefix is stripped because the entity is captured separately.
LOCATION_COMPANY_PREFIX = {str(v).lower() for v in LDAP_SCHEMA["loc_company_prefix"]}
LOCATION_SITE = {str(k).lower(): str(v) for k, v in LDAP_SCHEMA["loc_city"].items()}


def decode_location(code: str) -> str:
    """Decode a location code to the clean agency/site; unknown codes are kept title-cased."""
    parts = [p for p in (code or "").split("_") if p]
    city_parts = [p for p in parts if p.lower() not in LOCATION_COMPANY_PREFIX]
    if not city_parts:
        city_parts = parts
    key = city_parts[-1].lower()
    return LOCATION_SITE.get(key, city_parts[-1].replace("-", " ").title())


def decode_department(code: str) -> str:
    """Decode a department code. Known segments are mapped and unknown segments are kept raw."""
    parts = [p for p in (code or "").split("_") if p]
    out = []
    for i, p in enumerate(parts):
        pl = p.lower()
        if i == 0 and pl in DEPARTMENT_COUNTRY:
            out.append(DEPARTMENT_COUNTRY[pl])
        elif pl in DEPARTMENT_SEGMENT:
            out.append(DEPARTMENT_SEGMENT[pl])
        elif pl in DEPARTMENT_FUNCTION:
            out.append(DEPARTMENT_FUNCTION[pl])
        else:
            out.append(p)
    return " / ".join(out) if out else code


def _first(value):
    """LDAP multi-valued attrs come back as lists; take the first non-empty scalar."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _uid_from_dn(dn: str) -> str | None:
    """Extract the uid RDN from a DN (`uid=jdoe,ou=people,...` -> `jdoe`). Manager is a DN in LDAP;
    we resolve it to the stable uid key locally so the graph never stores a raw DN."""
    if not dn:
        return None
    for part in dn.split(","):
        part = part.strip()
        if part.lower().startswith("uid="):
            return part[4:].strip() or None
    return None


def fetch(host: str, base_dn: str, port: int):
    """Anonymous-bind search for active people; return (profiles, dn_to_uid). Raw records never
    leave this function — only the distilled profile dicts do."""
    import ldap3  # imported lazily so --help / import-checks work without the dep

    # Never request denied attrs; ask only for the allowlist keys + objectClass (structural, used
    # only to classify employment + gate out non-staff; the raw value is never stored).
    attrs = sorted((set(ALLOWLIST.keys()) - DENYLIST) | {"objectClass"})
    staff_filter = "".join(f"(objectClass={oc})" for oc in STAFF_OBJECT_CLASSES)
    # Active STAFF only: a person, not a leaver, AND tagged with one of the configured staff
    # objectClasses. The objectClass gate drops service / functional accounts.
    ldap_filter = (
        "(&(objectClass=inetOrgPerson)(uid=*)"
        f"(!(pwdAccountLockedTime={LEAVER_PWDLOCK}))"
        f"(|{staff_filter}))"
    )

    server = ldap3.Server(host, port=port, get_info=ldap3.NONE)
    conn = ldap3.Connection(server, auto_bind=True)  # anonymous bind

    profiles = []
    dn_to_uid = {}
    entries = conn.extend.standard.paged_search(
        search_base=base_dn,
        search_filter=ldap_filter,
        search_scope=ldap3.SUBTREE,
        attributes=attrs,
        paged_size=500,
        generator=True,
    )
    for entry in entries:
        if entry.get("type") != "searchResEntry":
            continue
        a = entry.get("attributes", {})
        uid = _first(a.get("uid"))
        if not uid:
            continue
        # Drop non-staff placeholder/service accounts by title.
        if is_non_staff(str(_first(a.get("title")) or "")):
            continue
        dn = entry.get("dn")
        if dn:
            dn_to_uid[dn.lower()] = uid
        prof = {"uid": uid}
        for ldap_attr, field in ALLOWLIST.items():
            if ldap_attr in ("uid", "manager"):
                continue
            v = _first(a.get(ldap_attr))
            if v:
                prof[field] = str(v)
        # employment category (derived from objectClass, not stored raw).
        ocs = {str(x).lower() for x in (a.get("objectClass") or [])}
        for oc_name, label in EMPLOYMENT_CLASS.items():
            if oc_name in ocs:
                prof["employment"] = label
                break
        # keep the manager DN transiently for the relation pass; resolved below, not emitted
        mgr_dn = _first(a.get("manager"))
        if mgr_dn:
            prof["_manager_dn"] = str(mgr_dn)
        profiles.append(prof)

    conn.unbind()
    return profiles, dn_to_uid


def distill(profiles, dn_to_uid):
    """Turn profiles into (clean_profiles, relation_facts, groups_meta, services_meta, teams_meta).
    Manager DNs resolve to uids here and the transient `_manager_dn` is dropped so no raw DN is
    emitted. Curated group membership (in_group facts) is evaluated from the SAME per-person axes
    used for the relation facts above. Curated service ownership (managed_by_team facts) is a
    separate overlay keyed by service, not by person."""
    facts = []
    known = {p["uid"] for p in profiles}
    clean = []
    for p in profiles:
        uid = p["uid"]
        mgr_dn = p.pop("_manager_dn", None)
        clean.append(p)

        # ou = employing subsidiary/entity.
        if p.get("ou"):
            facts.append(_fact(uid, "person", "works_in", p["ou"], "org"))
        # departmentNumber = the finer team/department (decoded to a readable label)
        if p.get("department"):
            facts.append(_fact(uid, "person", "member_of", decode_department(p["department"]), "team"))
        if p.get("title"):
            facts.append(_fact(uid, "person", "has_title", p["title"], "role"))
            fam = role_family(p["title"])
            if fam:
                facts.append(_fact(uid, "person", "has_role_family", fam, "family"))
        if p.get("l"):
            facts.append(_fact(uid, "person", "located_at", decode_location(p["l"]), "site"))
        if p.get("employment"):
            facts.append(_fact(uid, "person", "has_employment", p["employment"], "status"))
        if mgr_dn:
            mgr_uid = dn_to_uid.get(mgr_dn.lower()) or _uid_from_dn(mgr_dn)
            # only emit a managed_by edge to a manager who is themselves an active person, so we
            # never mint a dangling person node for a leaver/out-of-scope manager
            if mgr_uid and mgr_uid in known and mgr_uid != uid:
                facts.append(_fact(uid, "person", "managed_by", mgr_uid, "person"))

    specs = load_groups(GROUPS_SPEC) if GROUPS_SPEC and os.path.exists(GROUPS_SPEC) else []
    for p in clean:
        ax = {
            "uid": p["uid"],
            "org": p.get("ou", ""),
            "team": decode_department(p.get("department", "")),
            "family": role_family(p.get("title", "")) or "",
            "status": p.get("employment", ""),
            "site": p.get("l", ""),
            "title": p.get("title", ""),
        }
        for g in specs:
            if evaluate(g, ax):
                facts.append(_fact(p["uid"], "person", "in_group", g["slug"], "group"))
    groups_meta = [{"slug": g["slug"], "name": g["name"], "aliases": g["aliases"]} for g in specs]

    svcs = load_services(SERVICES_SPEC) if SERVICES_SPEC and os.path.exists(SERVICES_SPEC) else []
    for s in svcs:
        facts.append(_fact(s["slug"], "service", "managed_by_team", s["team_slug"], "group"))
    services_meta = [{"slug": s["slug"], "name": s["name"], "aliases": s["aliases"]} for s in svcs]
    teams_meta = [{"slug": s["team_slug"], "name": s["team_name"]} for s in svcs]

    return clean, facts, groups_meta, services_meta, teams_meta


def _fact(subject, subject_kind, relation, obj, object_kind):
    return {
        "subject": subject,
        "subject_kind": subject_kind,
        "relation": relation,
        "object": obj,
        "object_kind": object_kind,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Distill org-directory who-is-who facts (leak-free).")
    ap.add_argument("--out", default="hive/tmp/who_facts.json", help="output JSON path (gitignored)")
    ap.add_argument("--host", default=os.environ.get("SWARM_LDAP_HOST"))
    ap.add_argument("--base-dn", default=os.environ.get("SWARM_LDAP_BASE_DN"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("SWARM_LDAP_PORT", "389")))
    args = ap.parse_args()

    if not args.host or not args.base_dn:
        print(
            "ldap_who: SWARM_LDAP_HOST and SWARM_LDAP_BASE_DN must be set (env or --host/--base-dn); "
            "these are intranet specifics kept out of committed files.",
            file=sys.stderr,
        )
        return 2

    profiles, dn_to_uid = fetch(args.host, args.base_dn, args.port)
    clean, facts, groups_meta, services_meta, teams_meta = distill(profiles, dn_to_uid)

    payload = {
        "origin": "ldap:directory",
        "reliability": 0.9,
        "evidence_kind": "observation",
        "lineage": "ldap:directory",
        "profiles": clean,
        "facts": facts,
        "groups": groups_meta,
        "services": services_meta,
        "teams": teams_meta,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)

    # aggregate counts ONLY — never a name/uid/value
    rel_counts = {}
    for f in facts:
        rel_counts[f["relation"]] = rel_counts.get(f["relation"], 0) + 1
    emp_counts = {}
    for p in clean:
        emp_counts[p.get("employment", "unknown")] = emp_counts.get(p.get("employment", "unknown"), 0) + 1
    print(
        f"WHO-DISTILL people={len(clean)} "
        + " ".join(f"{k}={v}" for k, v in sorted(emp_counts.items()))
        + f" facts={len(facts)} "
        + " ".join(f"{k}={v}" for k, v in sorted(rel_counts.items()))
        + f" -> {args.out}"
    )
    grp_counts = {}
    for f in facts:
        if f["relation"] == "in_group":
            grp_counts[f["object"]] = grp_counts.get(f["object"], 0) + 1
    print("WHO-GROUPS " + " ".join(f"{k}={v}" for k, v in sorted(grp_counts.items())))
    print(f"WHO-SERVICES services={len(services_meta)} teams={len(set(t['slug'] for t in teams_meta))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
