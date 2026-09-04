#!/usr/bin/env python3
"""Validate the learner-eval grader against synthetic positive and adversarial fixtures.

A metric that has never scored a success is not a metric yet. The re-graded frozen run
produced zero `join_correct` and zero `inventory_only`, so the corrected classifier had
never demonstrated that it accepts one real join while rejecting a plausible fake one.
Hashing and pre-registration make an unvalidated classifier reproducible, not correct.

The fixture world is entirely synthetic (`scripts/fixtures/learner_eval/`): two sites,
five guests, no real host or node name anywhere. `store` and `store-dev` deliberately
share a node so a same-node coincidence can be staged, and one document deliberately
contains a node name so "the node came from prose" can be staged.

Each fixture row names the hole it probes; `expected.json` is written from the rule
text before the grader runs, so the fixtures test the rules and not the implementation's
habits.

Exit status is the point: non-zero if any row lands in a class other than its expected
one. Run this before trusting any number the grader produces.

Usage:
  scripts/learner_eval_validate_grader.py [--grader scripts/learner_eval_grade.py]
                                          [--rules-doc docs/design/learner-eval-grading.md]
"""
import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "learner_eval"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grader", default=str(HERE / "learner_eval_grade.py"))
    ap.add_argument("--rules-doc", default=str(HERE.parent / "docs" / "design" / "learner-eval-grading.md"))
    ap.add_argument("--expected", default=str(FIX / "expected.json"))
    ap.add_argument("--quiet-pass", action="store_true", help="print only failures and the tally")
    a = ap.parse_args()

    spec = json.loads(pathlib.Path(a.expected).read_text())
    expected = spec["expected"]
    limits = {k: v for k, v in spec.get("known_limitations", {}).items() if not k.startswith("_")}

    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "graded.jsonl"
        proc = subprocess.run(
            [sys.executable, a.grader,
             "--run", str(FIX / "run_fixture.jsonl"),
             "--truth", str(FIX / "truth.json"),
             "--overlap", str(FIX / "overlap.csv"),
             "--docs-with-hosts", str(FIX / "docs_with_hosts.csv"),
             "--rules-doc", a.rules_doc,
             "--out", str(out)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout)
            sys.exit(f"validate: grader failed\n{proc.stderr[-3000:]}")
        graded = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]

    summary, rows = graded[0], {r["row_id"]: r for r in graded[1:]}
    version = summary.get("rules_version")

    print(f"validating grader rules v{version} "
          f"(sha {str(summary.get('rules_sha256'))[:12]}) on {len(rows)} synthetic fixtures\n")

    passed, failed = [], []
    for row_id, want in expected.items():
        got = rows.get(row_id)
        got_class = got and got.get("class")
        ok = got_class == want["class"]
        if ok and "control_class" in want:
            ok = (got or {}).get("control_class") == want["control_class"]
        (passed if ok else failed).append(row_id)
        if ok and a.quiet_pass:
            continue
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {row_id}")
        print(f"       expected {want['class']!r}, got {got_class!r}")
        if not ok:
            if "control_class" in want:
                print(f"       control expected {want['control_class']!r}, "
                      f"got {(got or {}).get('control_class')!r}")
            print(f"       probes: {want['probes']}")
            if got and got.get("why"):
                print(f"       grader said: {got['why']}")

    print(f"\n  {len(passed)} passed, {len(failed)} failed")

    positives = [r for r, w in expected.items() if w["class"] in ("join_correct", "inventory_only")]
    scored = [r for r in positives if rows.get(r, {}).get("class") == expected[r]["class"]]
    print(f"  positive controls: {len(scored)}/{len(positives)} rows whose expected class is a "
          f"SUCCESS scored as one")
    p_rows = [r for r in expected if r.startswith("P")]
    print(f"  ({len(p_rows)} P-rows in the fixture file; {len(positives)} of them have a success "
          f"as their expected class — P4's is corpus_only)")

    # The ceiling of output-side grading, executable rather than only admitted in prose.
    if limits:
        print("\n  KNOWN LIMITATIONS — the grader gets these wrong and that is the point:")
        for row_id, lim in limits.items():
            got = (rows.get(row_id) or {}).get("class")
            state = "still wrong" if got == lim["grader_says"] else f"CHANGED -> {got!r}"
            print(f"    LIMIT {row_id}: says {lim['grader_says']!r}, truth {lim['truth']!r} [{state}]")
            print(f"          {lim['why']}")
            print(f"          fix: {lim['fix']}")

    if failed:
        print(f"\nvalidate: FAILED on {len(failed)} fixtures: {', '.join(failed)}")
        sys.exit(1)
    print("\nvalidate: all fixtures classified as pre-registered")


if __name__ == "__main__":
    main()
