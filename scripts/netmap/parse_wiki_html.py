#!/usr/bin/env python3
"""Server-rendered wiki HTML → governed network facts (network-map Phase-2; local, no LLM).

Reads MediaWiki API JSON files (parse.text = rendered HTML) from a dir (argv[1], default
tmp/wiki_html) and emits governed facts as JSON on stdout:
  {"origin":"wiki:api","reliability":0.6,"evidence_kind":"observation","facts":[...]}

THE CRUX = turning heterogeneous HTML tables into the RIGHT facts. Approach (conservative,
generic — no page-specific hardcoding):
  * parse every <table> with BeautifulSoup; header = the <th> row (or first row if none);
  * detect an ENTITY column (site/host/server/name/fqdn/…) → its KIND from the header word
    (Site→site, firewall/gateway→gateway, cluster→cluster, service→service, else host);
  * detect IP column(s) (ip/address/wan/lan) and an FQDN/reverse-DNS column;
  * per row: emit `<entity> has_address <ip>` for each IP cell; if an FQDN cell is present, also
    emit `<fqdn>(host) has_address <ip>` (a site's public IP lives on its named firewall/host).
  * skip a table with no clear entity col or no IP col; skip empty/pure-number/IP-named entities.
Only distilled facts leave here; fetched HTML stays gitignored. Well-typed by construction.
"""
import glob
import json
import os
import re
import sys

try:
    from bs4 import BeautifulSoup
except ImportError:
    print(json.dumps({"error": "beautifulsoup4 missing"}))
    sys.exit(1)

IN_DIR = sys.argv[1] if len(sys.argv) > 1 else "tmp/wiki_html"
IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
FIRST_FQDN = re.compile(r"[a-z0-9][a-z0-9.-]*?\.(?:intranet|fr|com|net|org|local|lan)", re.I)
ENTITY_HDR = re.compile(r"\b(site|host\s*name|hostname|host|role|server|machine|node|vm|name|fqdn|dns)\b", re.I)
FQDN_HDR = re.compile(r"\b(fqdn|dns|reverse|domain)\b", re.I)
IP_HDR = re.compile(r"\b(ip|address(?:es)?|adresse|wan|lan)\b", re.I)
MAC_HDR = re.compile(r"\bmac\b", re.I)


def clean_ips(s):
    """IPs from a cell ONLY if the cell is a clean IP list (IPs + whitespace/comma/semicolon).
    Rejects ranges (`-`), CIDR (`/24`), and prose ("replaced 10.0.0.5") — a wrong address is worse
    than a missed one (code review, both critics)."""
    ips = IP.findall(s or "")
    if not ips:
        return []
    residue = re.sub(r"\d{1,3}(?:\.\d{1,3}){3}", "", s)
    residue = re.sub(r"[\s,;]+", "", residue)
    return ips if residue == "" else []


def entity_kind(header):
    h = (header or "").lower()
    if "site" in h:
        return "site"
    if any(w in h for w in ("firewall", "gateway", "router")):
        return "gateway"
    if "cluster" in h:
        return "cluster"
    if "service" in h:
        return "service"
    return "host"


def first_fqdn(s):
    m = FIRST_FQDN.search(s or "")
    return m.group(0).lower() if m else ""


def clean(s):
    s = re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()
    return s


def clean_entity(s):
    s = clean(s)
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"^\d+\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return "" if (re.fullmatch(r"[\d\s]*", s) or len(s) < 2) else s


def header_cells(table):
    """Header = the first DIRECT-CHILD row with <th> (else the first row). Direct children only
    (recursive=False) so a nested table's rows aren't mistaken for this table's."""
    rows = table.find_all("tr")
    for tr in rows:
        ths = tr.find_all("th", recursive=False)
        if ths:
            return [clean(c.get_text(separator=" ")) for c in ths], rows[rows.index(tr) + 1:]
    if rows:
        return [clean(c.get_text(separator=" ")) for c in rows[0].find_all(["td", "th"], recursive=False)], rows[1:]
    return [], []


def extract_table(table):
    facts = []
    # Skip tables we can't align reliably (code review, both critics: misalignment = false facts):
    #  - a NESTED table (recursive find_all would flatten its cells into wrong columns);
    #  - any rowspan/colspan (shifts cell indices → entity paired with the wrong column's IP).
    if table.find("table"):
        return facts
    if table.find(["td", "th"], attrs={"colspan": True}) or table.find(["td", "th"], attrs={"rowspan": True}):
        return facts

    cols, body = header_cells(table)
    if not cols:
        return facts

    ent_idx = fqdn_idx = None
    ip_idxs = []
    for i, h in enumerate(cols):
        if MAC_HDR.search(h):
            continue
        is_ip = bool(IP_HDR.search(h))
        if is_ip:
            ip_idxs.append(i)
        if fqdn_idx is None and FQDN_HDR.search(h):
            fqdn_idx = i
        # entity col must NOT also be an IP col (a "Host IP" header prefers IP; else the entity
        # would BE the address). Mutual exclusion (code review).
        if ent_idx is None and not is_ip and ENTITY_HDR.search(h):
            ent_idx = i
    if (ent_idx is None and fqdn_idx is None) or not ip_idxs:
        return facts
    ncols = len(cols)
    ekind = entity_kind(cols[ent_idx]) if ent_idx is not None else "host"

    for tr in body:
        # a repeated/section HEADER row inside the body is not data
        if tr.find("th", recursive=False):
            continue
        cells = [clean(c.get_text(separator=" ")) for c in tr.find_all(["td", "th"], recursive=False)]
        # cell count must match the header — a mismatched row is misaligned; skip (no false pairing)
        if len(cells) != ncols:
            continue

        def cell(i):
            return cells[i] if i is not None and i < len(cells) else ""

        if ent_idx is not None:
            ent = first_fqdn(cell(ent_idx)) or clean_entity(cell(ent_idx))
        else:
            ent = first_fqdn(cell(fqdn_idx))
        ent = (ent or "").lower().strip()
        if not ent or len(ent) < 2 or IP.fullmatch(ent):
            continue
        for ii in ip_idxs:
            for ip in clean_ips(cell(ii)):
                facts.append((ent, ekind, "has_address", ip, "address"))
    return facts


def html_of(path):
    try:
        d = json.load(open(path))
    except Exception:
        return ""
    t = (d.get("parse", {}) or {}).get("text", "")
    return t.get("*", "") if isinstance(t, dict) else (t or "")


def main():
    seen, facts = set(), []
    files = sorted(glob.glob(os.path.join(IN_DIR, "*.json")))
    for path in files:
        soup = BeautifulSoup(html_of(path), "html.parser")
        for table in soup.find_all("table"):
            for f in extract_table(table):
                if f not in seen:
                    seen.add(f)
                    facts.append(f)
    out = {
        "origin": "wiki:api",
        "reliability": 0.6,
        "evidence_kind": "observation",
        "facts": [
            {"subject": s, "subject_kind": sk, "relation": r, "object": o, "object_kind": ok}
            for (s, sk, r, o, ok) in facts
        ],
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
