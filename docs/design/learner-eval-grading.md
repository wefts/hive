---
status: pre-registered for the next untouched run
metric_kind: PROVENANCE when the answer carries a kernel provenance record; CONCORDANCE otherwise (recorded per row)
rules_version: 5
owns: hive/scripts/learner_eval_grade.py — the classification rules only
supersedes: rules v2 (output concordance), rules v1 (node-only, not a join metric)
validated_by: hive/scripts/learner_eval_validate_grader.py against scripts/fixtures/learner_eval/
---

# Learner-eval grading rules — pre-registration

## What pre-registration does and does not claim here

**Rules v2 were NOT pre-registered for the run they graded.** They were committed at
09:21:57 and produced output at 09:22:08, but the frozen run had finished at 06:56, its
v1 rows had already been read, and v2 preserved `wrong_subject` — a rule invented after
seeing `frozen-002`. That is versioned retrospective re-analysis. It is reproducible and
honestly labelled, and it is not pre-registration.

**v4 is pre-registered for the next untouched run** and for nothing before it. Any
figure produced by re-grading an already-inspected run is re-analysis and is to be
labelled as such wherever it appears.

The grader embeds `rules_version` and a SHA-256 of this file in every summary, and
refuses to run if the two disagree. Note the limit of that hash, since v2 overclaimed
it: **it binds this prose to the output, and does not prove the Python implements the
prose.** What does that work is the fixture suite below.

## Validation — the fixtures are the contract, not this text

A metric that has never scored a success is not a metric. v2 produced zero
`join_correct` and zero `inventory_only` on the frozen run, so it had never shown it
accepts a real join while rejecting a plausible fake one.

`scripts/learner_eval_validate_grader.py` runs the grader over a synthetic world
(`scripts/fixtures/learner_eval/`: two sites, five guests, no real name anywhere) with
positive controls and one adversarial row per known hole, and exits non-zero on any
disagreement with `expected.json`, which is written from this text before the grader
runs. **Run it before trusting any number.** v2 scores 5 of the 13 fixtures wrong, three
of them by calling a fake a join.

## What this metric actually is — CONCORDANCE, and the word matters

**This is not a provenance metric and must not be called one.** v3 called itself that;
the third review built a counterexample and ran it — correct host, correct node, an
incidental citation, unrelated filler — and the grader returned `join_correct`. It is
reproduced as fixture `L1-concordance-ceiling`, which the validator reports as a
standing LIMIT.

The reason is structural, not a missing guard:

- `exclusive` inventory evidence establishes that no **cited** document contains the
  node. It does not establish that the inventory supplied it.
- document evidence establishes that a citation mentions the host and the answer has
  three novel tokens. A longer irrelevant answer satisfies that.

**No analysis of answer text can separate a join from a coincidence.** Adding a sixth
output-side guard would move the counterexample, not remove it. The rules below are
therefore the best available *concordance* rules — useful for ranking failure shapes,
not for asserting that two sources were connected — and every entry reporting them says
`concordance`.

**The fix is in the kernel, and as of rules v5 it exists.** `Swarm.Core` now emits a
machine-readable `provenance` record per answer — `kind` (`structured` / `consilium`),
the served `subject_key`, the `facts` the answer was rendered from, and the `passages`
that entered the prompt (swarm `a4fc49b`..; `Gate.Answer` carries the validated atoms).

**When a row carries that record, v5 grades on it and the row is marked
`evidence_basis: "provenance"`.** When it does not — an older run, a tier-0 answer, an
error — v5 falls back to the concordance rules below and marks the row
`evidence_basis: "concordance"`. The two are never pooled into one headline: a summary
reports the provenance-graded rows and the concordance-graded rows separately, because
they are different measurements.

### Provenance rules (used when the record is present)

- **inventory provenance** — the record contains a fact about the subject whose object
  is the node the API places it on: for a `structured` answer, `subject_key` equals the
  subject's graph key and a fact's `object` matches the expected node; for a `consilium`
  answer, a grounded fact whose `subject` is the host and whose `object` is that node.
  Nothing is inferred from the answer text.
- **document provenance** — the record's `passages` include a document that the corpus
  shows contains the subject's hostname.
- everything else — binding, pairing per host, contradiction — is unchanged; it just
  reads the record instead of the prose.

`L1-concordance-ceiling` carries no provenance record, so under v5 it is graded by the
fallback and remains a standing LIMIT for concordance rows. Its provenance-bearing twin
`L2-provenance-rejects-the-fake` is the same answer with a record that names a different
fact, and v5 must reject it.

## The definition everything else follows from

Two sources are **concordant in an answer** when the answer carries evidence consistent
with both about **the same subject**. Output-string agreement is not provenance: that an
answer names the right node and cites some document mentioning the host proves neither
source contributed.

### Inventory evidence — one of two, and the grader records which

- **`structured`** — a citation with `source == "structured"` whose `ref` is exactly the
  subject's graph key, `net:host:<site>/<host>`. The serve path names the object it
  read; this is the ONE case that is provenance in the strict sense.
- **`exclusive`** — no structured citation, but the answer names the node the snapshot
  places the subject on **and that node name appears in no cited document**. If a cited
  document contains the node, the answer could have read it off prose, so the evidence
  is discarded. Weaker than `structured`, and recorded distinctly so a result can be
  read without it.

Anything else is no inventory evidence. Status words stay excluded: `running` and
`stopped` occur too freely in prose to be told from a claim about this host.

### Subject binding — required before any inventory evidence counts

The answer must identify the subject: it names the host (full name or FQDN stem, whole
token), or it carries a structured citation for that host's key. An answer that names
the right node while never saying what it is talking about is **`unbound_subject`**, not
a join.

### Document evidence

A citation resolves to a document that genuinely contains the subject's hostname
(checked against `docs_with_hosts.csv`, never against the answer's prose) **and** the
answer is substantive: at least three content tokens that are not in the question, not
part of any host/node/site identifier, and not stopwords. A citation attached to a
one-word answer is incidental, not evidence.

**Known limit, stated rather than papered over:** this does not prove the answer's text
came from that document. Proving that needs grounding provenance from the kernel — which
facts and which passages actually entered the prompt — which does not exist today and is
carded, not built.

### Pairing is per subject, never accumulated

All evidence is evaluated per host. On a page-accuracy row the page is the subject, so a
host counts as discussed when the answer names it *or* names its node; if one host
supplies the inventory side and a different host supplies the document side, that is
**`cross_paired`** and not a join.

## Classes

Assigned in this order; the first that applies wins.

| class | rule | join? |
| --- | --- | --- |
| `no_answer` | `status != found` | no — honest |
| `wrong` | contradicts the snapshot (wrong node for the subject; an undocumented host that is documented) | no |
| `wrong_subject` | a structured citation for a different host's key, or the answer names another host of the site and not the subject | no |
| `unbound_subject` | names the right node but never identifies the subject and carries no structured citation for it | no |
| `cross_paired` | inventory evidence about one host, document evidence about another | no |
| `answered_off` | answered with neither kind of evidence | no |
| `corpus_only` | document evidence only | no |
| `inventory_only` | inventory evidence only | no |
| `join_correct` | **both, about the same host** | **yes** |

## Shapes

Inventory evidence is one rule everywhere. What varies is the subject and what
contradicts.

- **placement** — subject is the host; contradiction is a different node of that site.
- **accuracy** — subject may be a page; per-host pairing applies, and restating the page
  without checking anything is `answered_off`.
- **purpose** — **removed from the join numerator** and reported separately. A concise,
  correct purpose answer names no node, and penalising it for not exposing a resolution
  step the user never asked about measures verbosity, not connectedness. v2 counted
  these; v3 does not.
- **undocumented** — reported separately as before: its evidence is inventory ∩ the
  *absence* of documents, so document provenance cannot be positive by construction.

## Headline numbers, all reported together

`join_correct / join_questions`; `inventory_only` split by `structured` vs `exclusive`;
`corpus_only`; the paired controls, on which `inventory_only` **is** success and which
are split by whether the control names the full inventory subject; `purpose` and
`undocumented` each reported apart; the full class histogram; and a breakdown by
`name_identity` **with its shape composition**, because v1 read an aggregate whose
composition contradicted the reading.

## Matching mechanics

Word-boundary, case-insensitive, `-` and `_` part of a name, so `app-pp` does not match
inside `app-pp-old`. Hostnames match as whole tokens; a product called `Storekeeper`
does not match a host called `store`.

## What this grader still cannot do

- It cannot tell a well-founded "still true" from a lucky one; `accuracy` only checks
  for contradiction.
- It cannot prove cited text was used, only that the citation is real and the answer is
  substantive.
- It sees the final answer and its citations, not which component produced them.
  Attributing a failure to a component needs the trace, not this file.
