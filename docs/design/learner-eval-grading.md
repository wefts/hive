---
status: pre-registered
rules_version: 2
owns: hive/scripts/learner_eval_grade.py — the classification rules only
supersedes: rules v1 (the ad-hoc rules used for the 2026-09-04 frozen run)
---

# Learner-eval grading rules — pre-registration

These rules are fixed **before** the run they grade. That is the whole point of the
document: rules v1 were written while grading, and one of them (`wrong_subject`) was
added after seeing the row it catches, which makes it a hypothesis fitted to its own
data. Any change here bumps `rules_version`, and a run graded under one version is
never compared to a run graded under another without both numbers being shown.

`learner_eval_grade.py` embeds `rules_version` and a SHA-256 of **this whole file** in
every summary it writes, and refuses to run if `--rules-doc` names a file whose
`rules_version` disagrees with the code. A grade whose recorded hash does not match
the file it claims to implement is not a measurement.

## What went wrong in v1, stated plainly

The v1 join metric **did not measure a join**. A `placement` row was scored correct
on the Proxmox node alone, with no document evidence required at all — so a pure
inventory lookup raised the number called "join rate". Every v1 figure, including the
ones already published on 2026-09-04, is unsafe to reason from as a measure of
connectedness. v1 numbers are retained and republished beside v2, never overwritten.

## The definition everything else follows from

Two sources are **joined in an answer** when the answer carries evidence from both of
them about **the same subject**. Evidence is judged mechanically:

- **Inventory evidence** — the answer names the node the Proxmox snapshot places the
  subject on. One rule for every shape, no per-shape variation. Status words are
  deliberately *not* accepted as evidence: `running` and `stopped` occur too freely in
  ordinary prose to be distinguished from a claim about this host, and a metric that
  counts them would credit fluency as a lookup. The subject must be the one asked
  about; a fact about a different machine is not evidence about this one.
- **Document evidence** — at least one citation resolves to a corpus document that
  genuinely contains the subject's hostname, verified against `docs_with_hosts.csv`,
  not against the answer's own prose. `Core.ask` cites by document *title*, so titles
  and `source_ref` are both indexed.

Neither is inferred from the other, and neither is inferred from the question. A
question that already contains the hostname does not supply inventory evidence, and a
question that names a page does not supply document evidence.

## Classes

Assigned in this order; the first that applies wins.

| class | rule | counts as a join? |
| --- | --- | --- |
| `no_answer` | Swarm did not answer (`status != found`) | no — honest |
| `wrong` | the answer contradicts the snapshot: it places the subject on a node the API does not, or names an undocumented host that is documented | no |
| `wrong_subject` | the answer's inventory claim is about a **different** machine than the one asked about — including the case where that machine happens to sit on the right node | no |
| `answered_off` | answered, but contains neither kind of evidence the shape can be checked on | no |
| `corpus_only` | document evidence, no inventory evidence | no |
| `inventory_only` | inventory evidence, no document evidence | no |
| `join_correct` | **both**, about the same subject, contradicting neither | **yes** |

`inventory_only` is new in v2 and is the class that v1 was silently scoring as a join.
It is a real success for the single-source control and a real failure for the join
metric, so it is reported in both places and conflated with neither.

## Per-shape reading

Inventory evidence is the same rule everywhere — the correct node, named. What varies
is what counts as a contradiction and what the subject is.

- **placement** — contradiction is naming a different node of that site. If the answer
  names a *host* other than the subject and gives no evidence about the subject
  itself, that is `wrong_subject`.
- **purpose** — contradiction is placing the subject on the wrong node. In practice a
  good purpose answer names no node at all, so it lands in `corpus_only`; that is the
  honest reading, not a harness defect.
- **accuracy** — the subject may be a page rather than a host, in which case the
  page's hosts are the subjects and any node named must belong to one of them.
  Restating the page without checking anything is `answered_off`, not correct: the
  question asked whether the page is still true.
- **undocumented** — a special case. Its evidence is inventory ∩ *absence* of
  documents, so document evidence cannot be positive by construction. It is graded on
  precision — every host named must really have zero corpus mentions, and at least one
  must be named — and it is reported **separately**, never inside the join numerator.

## Headline numbers, all reported together

- `join_correct / join_questions` — the join rate. v1's number is not this.
- `inventory_only` — answers that reached the inventory and stopped there.
- `corpus_only` — answers that reached the documents and stopped there.
- `single_source / control` — the paired hostname controls, on which
  `inventory_only` **is** success, since no join is being asked for.
- the full class histogram, plus a breakdown by `name_identity` **and by shape**,
  because v1 read an aggregate whose composition contradicted the reading (19 `exact`
  rows were 15 purpose, 1 accuracy, only 3 placement).

## Control scoring, corrected

A paired control asks the same thing with the raw hostname, so its success condition
is `inventory_only` **or** `join_correct` — reaching the inventory is the whole task.
Note for interpretation, recorded because v1 missed it: **12 of the 18 controls do not
contain the full inventory subject** (a bare stem versus the FQDN the inventory
keys on, e.g. `svc-01` versus `svc-01.<site-domain>`), so a control failure may be
candidate resolution rather than an unreachable inventory. Controls are therefore reported split by whether
the control string is the full subject name.

## Matching mechanics

Word-boundary, case-insensitive, with `-` and `_` treated as part of a name, so
`app-pp` does not match inside `app-pp-old`. Hostnames are matched as whole
tokens; a product name that merely contains a hostname as a substring (a product
called `Storekeeper` containing a host called `store`) is not a match.

## What this grader still cannot do

- It cannot tell a well-founded "still true" from a lucky one; `accuracy` only checks
  for contradiction.
- It cannot judge whether prose is a *good* description, only whether the citation
  behind it is real.
- It sees only the final answer text and citations, not which component produced them.
  Attributing a failure to a component requires the trace, not this file.
