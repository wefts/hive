#!/usr/bin/env python3
"""QA fixtures for parse_wiki_html.extract_table (network-map Phase-2). Run:
  uv run --with beautifulsoup4 python scripts/netmap/test_parse_wiki_html.py
All values are placeholders (no intranet data)."""
import sys
from bs4 import BeautifulSoup
import parse_wiki_html as P


def facts(html):
    return P.extract_table(BeautifulSoup(html, "html.parser").find("table"))


def check(name, got, want):
    ok = sorted(map(tuple, got)) == sorted(map(tuple, want))
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print("   got :", sorted(map(tuple, got)))
        print("   want:", sorted(map(tuple, want)))
    return ok


T_SITE = """<table class="wikitable"><tr><th>Site</th><th>IP</th><th>Reverse DNS</th></tr>
<tr><td>CityA</td><td>10.0.0.1</td><td>fw.a.example.net</td></tr>
<tr><td>CityB</td><td>10.0.0.2</td><td></td></tr></table>"""

T_NOIP = """<table><tr><th>Project</th><th>State</th></tr><tr><td>Alpha</td><td>active</td></tr></table>"""

T_IPNAME = """<table><tr><th>Host</th><th>IP</th></tr><tr><td>10.0.0.9</td><td>10.0.0.9</td></tr></table>"""

T_MULTIIP = """<table><tr><th>Server</th><th>Addresses</th></tr>
<tr><td>srv1</td><td>10.0.0.3 10.0.0.4</td></tr></table>"""

# colspan/rowspan anywhere → whole table skipped (can't align → would pair wrong IPs)
T_SPAN = """<table class="wikitable"><tr><th>Site</th><th>IP</th></tr>
<tr><td rowspan="2">CityC</td><td>10.0.0.5</td></tr><tr><td>10.0.0.6</td></tr></table>"""

# nested table → whole table skipped (recursive flatten would cross-pollinate)
T_NESTED = """<table><tr><th>Host</th><th>IP</th></tr>
<tr><td>srv2</td><td><table><tr><td>10.0.0.7</td></tr></table></td></tr></table>"""

# a repeated header row mid-body must not be parsed as data
T_MIDHDR = """<table><tr><th>Host</th><th>IP</th></tr><tr><td>srv3</td><td>10.0.0.8</td></tr>
<tr><th>Host</th><th>IP</th></tr></table>"""

# prose / CIDR / range in the IP cell → rejected (only clean IP lists)
T_PROSE = """<table><tr><th>Host</th><th>IP</th></tr>
<tr><td>srv4</td><td>replaced 10.0.0.10</td></tr>
<tr><td>srv5</td><td>10.0.0.11/24</td></tr>
<tr><td>srv6</td><td>10.0.0.12-10.0.0.20</td></tr></table>"""

# "Host IP" header must classify as IP (not entity) → no entity col → no facts (safe)
T_COLLIDE = """<table><tr><th>Host IP</th><th>Notes</th></tr><tr><td>10.0.0.13</td><td>x</td></tr></table>"""

ok = True
# fqdn side-fact DROPPED (reverse-DNS is often an ISP PTR → false host) — only entity has_address
ok &= check("site table → site has_address (no fqdn side-fact)", facts(T_SITE), [
    ("citya", "site", "has_address", "10.0.0.1", "address"),
    ("cityb", "site", "has_address", "10.0.0.2", "address"),
])
ok &= check("no IP column → no facts", facts(T_NOIP), [])
ok &= check("IP-named entity skipped", facts(T_IPNAME), [])
ok &= check("multiple IPs (Addresses plural) → one fact each", facts(T_MULTIIP), [
    ("srv1", "host", "has_address", "10.0.0.3", "address"),
    ("srv1", "host", "has_address", "10.0.0.4", "address"),
])
ok &= check("rowspan/colspan table → skipped entirely", facts(T_SPAN), [])
ok &= check("nested table → skipped entirely", facts(T_NESTED), [])
ok &= check("mid-body header row → not data", facts(T_MIDHDR), [
    ("srv3", "host", "has_address", "10.0.0.8", "address"),
])
ok &= check("prose/CIDR/range IP cells → rejected", facts(T_PROSE), [])
ok &= check("'Host IP' header classifies as IP, not entity → no facts", facts(T_COLLIDE), [])

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
