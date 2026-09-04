#!/usr/bin/env python3
"""The LEARNER: a foreign model (Gemini) plays a new engineer and asks questions.

Role boundary, deliberate: this script is the ONLY place questions are written.
The orchestrator never authors a question it later measures against, and the
learner never sees the Proxmox inventory that grades it -- it sees only the
documentation excerpts in the briefing pack.

Sends intranet documentation excerpts to Google. Call count and character
volume are printed and recorded so the cost of the run is visible.

Usage:
  scripts/learner_eval_ask.py --briefing tmp/learner-eval/briefing.json \
      --out tmp/learner-eval/candidates.jsonl [--batch 5] [--per-batch 8]
"""
import argparse
import datetime
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

SHAPES = {
    "placement": "Which Proxmox node (hypervisor) runs a given machine or service.",
    "purpose": "What a given host is for -- what runs on it, who uses it.",
    "accuracy": "Whether what this page states about a cluster's or service's machines is still true today.",
    "undocumented": "Which machines at a site have no documentation at all.",
}

PROMPT = """You are a new infrastructure engineer who just joined this company. You have
been given some pages from the internal Confluence and MediaWiki. You have NOT been given
access to the hypervisors yet.

Your job right now is to write down the questions you would need answered to be able to
operate this estate. Someone will answer them from the company's knowledge system, and the
answers about machines will be checked against the live Proxmox API, so ask about things
that have a definite factual answer.

Only two sites exist for this exercise: `forge` and `galaxy`. Ignore anything else.

Write questions of these shapes:
{shapes}

Rules:
- Ask in the language the page is written in when that feels natural, otherwise English.
- For `placement`, refer to the machine or service THE WAY THE PAGE NAMES IT (its role,
  product name, or purpose), not by copying a raw hostname -- you are asking as someone who
  read the documentation, not someone who already has the inventory. Then ALSO write
  `control_question`: the same ask, phrased with the raw hostname instead of the role, so the
  two can be compared. For every other shape `control_question` is null.
- For `purpose`, name the host as it appears in the page.
- Put your best guess at the underlying hostname in `subject`, or null if the page does not
  give one. Put the wording you used in the question in `subject_as_written`.
- One question per object, {n} objects total for this batch, spread across the shapes.
- Do not invent machines that are not mentioned in the pages below.
- Write hostnames the way an engineer says them -- the bare name, never a `site/name` path.
  The site goes in the `site` field, not into the question text.
- For `accuracy` and `undocumented`, `subject` may be null.

Return ONLY a JSON array of objects with exactly these keys:
  shape, question, control_question, subject, subject_as_written, site, doc_ref, why

PAGES:
{pages}
"""

TITLES_NOTE = """
You are given only the CATALOGUE of pages: each page's title and which machines are named on
it. You have not read the pages themselves. Ask what you would need to know from someone who
has read them. When a shape asks you to name a service the way the page names it, use the
page TITLE's wording; still put the raw hostname in `subject`.
"""


def call_gemini(key, model, prompt, timeout=180):
    body = json.dumps(
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
        }
    ).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body,
        headers={"content-type": "application/json", "x-goog-api-key": key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.load(r)
    return "".join(p.get("text", "") for p in doc["candidates"][0]["content"]["parts"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--briefing", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch", type=int, default=5, help="docs per Gemini call")
    ap.add_argument("--per-batch", type=int, default=8, help="questions per Gemini call")
    ap.add_argument("--max-calls", type=int, default=12, help="hard cap on calls that leave the building")
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-pro-latest"))
    a = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("learner_eval_ask: GEMINI_API_KEY is not set (source hive/secrets.env)")

    pack = json.loads(pathlib.Path(a.briefing).read_text())
    docs = pack["docs"]
    batches = [docs[i : i + a.batch] for i in range(0, len(docs), a.batch)]
    if len(batches) > a.max_calls:
        sys.exit(f"learner_eval_ask: {len(batches)} calls exceeds --max-calls {a.max_calls}")

    mode = pack.get("selection", {}).get("mode", "bodies")
    shapes = "\n".join(f"- `{k}`: {v}" for k, v in SHAPES.items())
    out, calls, sent = [], 0, 0
    for i, batch in enumerate(batches, 1):
        if mode == "titles":
            pages = "\n".join(
                f"--- page ref={d['ref']} title={d['title']}\n"
                f"    machines named on it: "
                + (", ".join(f"{h['name']} (site {h['site']})" for h in d.get("hosts_mentioned", []))
                   or "(none)")
                for d in batch
            )
        else:
            pages = "\n\n".join(f"--- page ref={d['ref']} title={d['title']}\n{d['excerpt']}" for d in batch)
        prompt = PROMPT.format(shapes=shapes, n=a.per_batch, pages=pages)
        if mode == "titles":
            prompt = prompt.replace("PAGES:", TITLES_NOTE + "\nPAGES:")
        sent += len(prompt)
        text = None
        for attempt in range(3):
            try:
                text = call_gemini(key, a.model, prompt)
                break
            except urllib.error.HTTPError as e:
                if attempt == 2:
                    sys.exit(f"learner_eval_ask: batch {i} failed http {e.code}: {e.read()[:400]}")
                time.sleep(5 * (attempt + 1))
        calls += 1
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            print(f"learner_eval_ask: batch {i} returned non-JSON, skipped", file=sys.stderr)
            continue
        for it in items:
            it["batch"] = i
            it["learner"] = a.model
            out.append(it)
        print(f"learner_eval_ask: batch {i}/{len(batches)} -> {len(items)} questions", file=sys.stderr)

    with open(a.out, "w") as f:
        f.write(
            json.dumps(
                {
                    "kind": "learner_run",
                    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                    "learner": a.model,
                    "gemini_calls": calls,
                    "prompt_chars_sent": sent,
                    "briefing": a.briefing,
                    "docs": len(docs),
                }
            )
            + "\n"
        )
        for it in out:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    print(
        f"learner_eval_ask: {len(out)} candidate questions, {calls} Gemini calls, "
        f"{sent} chars sent -> {a.out}"
    )


if __name__ == "__main__":
    main()
