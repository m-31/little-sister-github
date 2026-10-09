# ADR-0013 — The object of an `actions` line is the workflow on its branch

- **Status:** Accepted
- **Date:** 2026-10-10 (accepted 2026-09-20)
- **Related:** [ADR-0012](0012-the-actions-line-carries-what-it-read.md) (the record
  this keeps, whose §1 states the same subject),
  [ADR-0016](0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
  (§18, the node a record's `file` names; §21, every workflow's line written),
  [ADR-0010](0010-a-disabled-workflow-is-a-line-not-a-read.md) (the disabled line, keyed
  without a branch), [ADR-0009](0009-named-branches-replace-the-default-branch.md) and
  [ADR-0005](0005-the-actions-aspect-asks-per-workflow.md) (the reads that name the
  pair), little-sister **ADR-0086** (the split this is the first multi-object case of,
  and whose decision 8 forces it), little-sister **ADR-0050** (why a slug cannot be a
  subject), little-sister **ADR-0085** (what a subject is for), little-sister
  **ADR-0087** (the identity that keeps one event in a series once, and what a reading
  of a frozen state names instead, decision 3)
- **Register:** [`../decisions.md`](../decisions.md)

A bare ADR number here is this repository's; a reference to one of little-sister's is
always written out, because the two numbering spaces overlap.

## Context

The **repository** would be a sound subject for every `actions` line if a subject were
only a grouping hint on a line. little-sister ADR-0086 makes it more: a check hands its
grading one `Measurement` per object it read, and decision 8 says a run hands over **one
measurement per object, never one object twice**. A repository with fifteen
workflow-branches would hand over fifteen readings with one subject, which is the shape
that decision refuses.

The other way out was measured and fails. One measurement per repository, carrying all
its workflows, would have to hold every line the grading derives, because the grading
may read nothing else. A workflow-branch record weighs 291 bytes, so a repository
reaches 2910 bytes at ten workflow-branches against the default `record_limit` of 2048
— before any other aspect is counted, and with no clipping available, because dropping
a workflow from the reading would be a lie about what was read.

## Decision

### 1. The object is the workflow on one branch; a disabled line's is the workflow

A run line is about **one workflow on one branch**, and that pair is what its reading
is of. The disabled line is keyed without a branch (ADR-0010) because it is about the
workflow everywhere it would run, so its object is **the workflow**, and it gets a
subject of its own shape.

The repository stays in the record, as the grouping field it already was. Everything
about one repository is then a **projection over records** — which it has to be anyway,
since a repository is spread across several aspect children and no single `subject`
could index it.

### 2. The subject is ids joined by colons, with the branch last

```
<repository id>:<workflow id>:<branch>     a run line
<repository id>:<workflow id>              a disabled line
```

- **Ids, not names.** The repository's and the workflow's numeric ids are the fields a
  rename cannot move. The branch has no id, so it is its name, and **a renamed branch
  starts a new history** — accepted, since a rename is the rare case and the name is
  all GitHub keys a branch by.
- **Not the slug.** `slug()` narrows what it is given, so `feature/a-b` and
  `feature-a/b` can meet — the lossiness little-sister ADR-0050 documents. A slug only
  has to tell siblings apart on one node; a subject names a thing in the world across
  every run, so it keeps the branch **verbatim**.
- **The colon, because git refuses it.** `git check-ref-format` forbids `:` anywhere
  in a ref name, so no branch contains one and the string splits back into its parts
  unambiguously, with no escaping. The two shapes differ by their number of parts, so a
  workflow and one of its branches can never share a subject. A run GitHub named no
  branch for is keyed `?` today, which git forbids too, so it cannot collide with a
  real branch either.
- **Within 200 characters.** Where the verbatim form would pass the library's
  `MAX_SUBJECT_LENGTH`, the branch is written as `sha256:` and the first 32 hex digits
  of the SHA-256 of its UTF-8 name. That part carries a colon, which no branch can, so
  the hashed form can never be mistaken for a branch that happens to look like it.

### 3. One measurement, one line, and the record is the `Entry`'s `data`

Each workflow-branch the run read is one `Measurement`, and each maps one-to-one onto
the line the grading writes for it: the grading carries the reading through onto that
`Entry` as its `data`, and the line's `subject` is the measurement's. Every reading is a
line, a passing idle workflow's too, on its workflow's node — or its branch's, where the
configuration names several (ADR-0016 §19, §21).

The record keeps ADR-0012 §2's fields, the workflow's `file` among them (ADR-0016 §18),
and gains **`repository_id`** and **`workflow_id`**, the same ids
the subject is built from, so a projection that groups by repository survives the rename
that changes `repository` — and, like every reading of this check, its `aspect` and
`kind` ([ADR-0014](0014-a-run-is-its-readings-and-the-estate-is-its-object.md)).
`verdict` stays: it is a fixed reading of GitHub's `status` and `conclusion` that no
configuration enters, and keeping it is what makes the line's `data` and the
measurement's record one value.

### 4. A reading names the attempt of the run its verdict is of

A workflow-branch's series keeps each event once only if each reading says which event
it is (little-sister **ADR-0087** decision 3). A run line's reading that named none
would append on every poll, and a window over a quiet workflow would be one finished
run, repeated. So the measurement carries an **identity**, `<run id>/<attempt>`, and
both run blocks of the record carry its two halves, `id` and `attempt`, beside the run
number.

- **The run the verdict is of** — the `completed` block's — and the run in flight only
  while nothing on that branch has completed. A first run read in flight and again
  finished is then one record, not two. Once a verdict exists, a run in flight rides on
  that verdict's record, and the identity changes when the new run completes rather
  than when it starts: the code a kept reading carries is the verdict's, so the event
  it is kept under is the verdict's run. A cancelled run leaves no verdict and never
  becomes a record of its own; named by its run in flight, the reading would have left
  one that names the cancelled run and carries the verdict before it.
- **The id and the attempt, together.** A re-run is a new attempt of the same run:
  GitHub keeps its `id` and its `run_number` — *this number does not change if you
  re-run the workflow run* — and counts `run_attempt`. With the id alone a re-run that
  passed would replace the attempt that failed and take the failure out of the window;
  with the attempt, a re-run is a point of its own beside the attempt it retried. Where
  GitHub sends no attempt the identity is the id alone, and a run with no id names
  nothing and appends.

A disabled line names no run; what it names is §5's. Every other reading of the check
is read anew on every run, so none of them names an identity or a state.

### 5. A disabled line names when its workflow last changed

A disabled workflow names no run, so §4 leaves its reading nothing to name, and every
poll appended the same frozen state to the workflow's series. little-sister
**ADR-0087** decision 3 says what such a reading names — the source's own
*last-changed* field where one moves whenever the state does, and otherwise the state —
and that the record which takes the field says what the source's documentation says
moves it.

**GitHub keeps `updated_at` on every workflow, and documents it only in general.** The
REST schema, `components.schemas.workflow` in `github/rest-api-description`, requires
the field and gives it no description. GitHub's GraphQL reference describes the same
object's `updatedAt` — the `Workflow` a REST answer names by its `node_id` — as *the
date and time when the object was last updated*, and the REST reference documents both
switches as setting that object's `state`: *Disables a workflow and sets the `state` of
the workflow to `disabled_manually`*, and the enable endpoint the same way, to `active`.
Read together, each switch updates the object and moves the field. No page says so of
either switch in as many words, so it was measured before the release, on one workflow
of a real estate switched off and on again. `updated_at` read
`2026-03-12T10:38:49.000+01:00` before — the workflow's `created_at`, so nothing had
moved it since — `2026-09-24T14:13:13.000+02:00` after the disable and
`2026-09-24T14:13:21.000+02:00` after the enable: it moved at both switches, each time
to the second of the call and in the same answer as the `state`, and the list this check
reads said the same as the workflow at every step. What keeps two switch-offs apart is
only that the value changes somewhere between them, and it changes at each.

**So the identity is the instant the workflow's `updated_at` names, exactly as the
record keeps it under `updated.at`** — typed as a run block's is (ADR-0012 decision 3) —
and the record's own `at` is the same instant. While the workflow is off, the instant it
last changed is the switch that turned it off, or a later change, which is a record of
its own, so it is the instant the record is of, and its series places the record there,
as a run line's stands at its run's start (ADR-0012 decision 3). Where GitHub sent no
instant, `at` is `null`, and the record stands where it was first seen. The identity is
read back out of the record little-sister built, so it is the library's own string for
the instant, in UTC with a `Z`, and never GitHub's text for it: the values above came
with offsets, and one instant is one identity however GitHub writes it. Where GitHub
sends something that is not an instant, the identity is its text as sent and the record
keeps `null` ([ADR-0014](0014-a-run-is-its-readings-and-the-estate-is-its-object.md)
§8). A disabled workflow read again unchanged replaces its own record; switched off
again, it is a record of its own, whatever the cause. Whatever else GitHub counts as
updating a workflow moves the series too — an edit of its file may — and that is a
record per change of the workflow, never one of *no change*.

**Not the state, which the rule would take otherwise.** A disabled line's series sees
the workflow only while it is off, since a running workflow's readings are of its
branches (§1). Compared with the newest record alone, a state cannot tell a workflow
switched off, on and off again by the same cause from one that stayed off: the second
spell would replace the first.

**The state is the answer where GitHub sent no `updated_at`**, or sent one that is no
instant and that a series cannot keep — longer than an identity may be, or with a
control character. The reading then
names GitHub's `state`, the one field that constitutes it, and a state that would not
travel either is `sha256:` and 32 hex digits of it. No answer makes a reading the library
refuses, which would be an error out of the measuring half on every poll.

### 6. Nothing else moves

The line's `text` and its slug are unchanged, so no pin moves. The coverage line,
`runs-window-partial` and `branches-unmatched` stay without a subject: they are about
the estate the aspect was asked about. The other aspects keep emitting lines with no
subject — whether a pull request or a finding is an object with a history is its own
decision, taken against its own surface.

## Consequences

- **The `subject` on an `actions` line changes value**, from `1001` to `1001:42:main`.
  Nothing is pinned by subject, so no pin moves; a consumer that grouped lines by it
  groups finer now, and groups by repository through `repository_id` instead.
- **In `all_branches` mode the number of subjects is unbounded**, because the number
  of workflow-branches is. That is the case the library's runtime ceiling on held
  records exists for, and why the series will need that ceiling before this mode meets
  it.
- **Each record grows by the two ids and its `aspect` and `kind`**, some seventy
  bytes, and stays inside the limit on its own — and by each run block's `id` and
  `attempt`, some thirty bytes a block.
- **A deployment that sets `series_keep` keeps one record per attempt** of each
  workflow on its branch, from the first poll that saw its verdict, where it kept one
  per poll. What a workflow-branch's history holds is then its runs, failed attempts
  included, rather than how often somebody looked.
- **A switched-off workflow keeps one record per change** — each time it is switched
  off, and whenever else GitHub updates it — where it kept one per poll for as long as
  it stayed off. Its record grows by `updated.at` and by its own `at`, the same instant,
  some seventy-three bytes together.

## Alternatives considered

- **Keeping the repository as the subject**, one measurement per workflow-branch.
  Refused by little-sister ADR-0086 decision 8: one object, twice.
- **One measurement per repository, carrying its workflows.** Measured above: the
  record passes `record_limit` at ten workflow-branches, before any other aspect.
- **The slug as the subject.** It is already an identifier and already unique on its
  node, but it narrows the branch name, so two branches can share one.
- **The workflow's name, or its file path, in place of its id.** Readable, and moved
  by a rename.
- **The measurement declaring its own place for the engine to put it**, the other
  answer little-sister ADR-0086 decision 2 left open. It buys nothing here: the
  mapping is one-to-one, and the grading is where the slug is minted anyway.
- **No identity.** Every poll appends, and a quiet workflow's window is one finished
  run repeated.
- **The run id alone.** A re-run keeps it, so the attempt that passed would replace the
  attempt that failed.
- **The run number.** Kept across a re-run exactly as the id is, and unique only per
  workflow.
- **The run in flight wherever there is one.** A cancelled run would leave a point
  that names it and carries the verdict before it, and a kept reading's code would
  belong to another run than the one its identity names.
- **No name for a disabled line.** Every poll appends, and a switched-off workflow's
  window is one frozen state repeated.
- **The state alone for a disabled line**, as the rule has it where a source keeps no
  last-changed field. Refused in §5: its series sees nothing while the workflow runs,
  so a second switch-off by the same cause would replace the first.
- **`updated_at` as GitHub sent it**, offset and all. It moves at every switch too, and
  needs no parsing; refused in §5, because it is not the string the record keeps.
- **No top-level `at` on a disabled record**, which would leave it where the check first
  saw it: on a first start, a switch-off of months before would stand at that start.
