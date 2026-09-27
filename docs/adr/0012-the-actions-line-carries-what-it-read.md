# ADR-0012 — The `actions` line carries what it read

- **Status:** Accepted
- **Date:** 2026-09-27 (accepted 2026-09-19)
- **Related:** [ADR-0004](0004-a-finding-grades-the-repository-does-not.md) (the
  finding is what grades, and the repository is its subject),
  [ADR-0005](0005-the-actions-aspect-asks-per-workflow.md) (the read this record
  keeps the material of), [ADR-0010](0010-a-disabled-workflow-is-a-line-not-a-read.md)
  (the other `actions` line, which carries a record too),
  [ADR-0011](0011-conditional-requests-and-the-cache-that-holds-them.md) (the cache,
  which is **not** a record and is deliberately nowhere near one), little-sister
  **ADR-0082** (the field, its vocabulary and its limit), little-sister **ADR-0050**
  (the id a slug is keyed on, which is also the subject), little-sister **ADR-0018**
  (escaping is a render-time step),
  [ADR-0013](0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md) (the
  workflow on its branch, the subject of a run line),
  [ADR-0014](0014-a-run-is-its-readings-and-the-estate-is-its-object.md) (every time
  this package's records carry, and the heaviest of them declared),
  [ADR-0015](0015-a-workflow-line-holds-the-newest-run-it-has-read.md) (the workflow's
  name the record carries)
- **Register:** [`../decisions.md`](../decisions.md)

A bare ADR number here is this repository's; a reference to one of little-sister's is
always written out, because the two numbering spaces overlap.

## Context

This aspect reads a workflow run, decides a code from it, and writes a sentence —
*platform-a (main) / ci: failed (#41) · #42 running*. Everything else it knew at that
moment is gone by the time anything renders: the run's number survives only inside the
text, the two links are baked into Markdown the check escaped itself, and the
conclusion GitHub actually gave is nowhere at all.

little-sister **ADR-0082** added the place to keep it — an optional `data` on the
entry, beside an optional `subject` saying what the line is about — and named this
aspect as the first thing that should fill it. This record says what it fills it with,
because those names are read by things that are not this package.

## Decision

### 1. The subject is the workflow on its branch, as ids

A run line's subject is the **workflow on its branch**,
`<repository id>:<workflow id>:<branch>`, and a disabled line's is the workflow,
`<repository id>:<workflow id>` — the numeric ids, which are the only fields a rename
cannot change and which the line's slug is already keyed on
([ADR-0004](0004-a-finding-grades-the-repository-does-not.md), little-sister
[ADR-0050](0050-slugs-from-provider-identifiers.md) in that repository). Not the
repository: little-sister ADR-0086 hands the grading one measurement per object and
never one object twice, and a repository has many workflow-branches
([ADR-0013](0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md)). The
names a human reads go in the record instead, under `repository` and `workflow`, beside
`repository_id` and `workflow_id`.

Two lines are deliberately **without** a subject: the coverage line and the
`runs-window-partial` line are about the estate this leaf was asked about, not about
one repository, and giving them a subject would put them in a group where nobody
looking for that repository's workflows expects them.

Nothing is pinned by subject. That is little-sister ADR-0082's rule and this record
has no wish to bend it:
[ADR-0004](0004-a-finding-grades-the-repository-does-not.md) §1 already refuses a
per-repository status, and a subject pin would be one by the back door.

### 2. What the record carries, and the two words for one outcome

The run line's record is the repository, the workflow, the branch, the verdict, and a
block per run. The workflow is its name as the workflow list gives it, and the run's
only where the list gives none
([ADR-0015](0015-a-workflow-line-holds-the-newest-run-it-has-read.md) §5): a workflow
whose runs are named after their jobs, as Dependabot's are, reads one name on every
run, and no slug moves. The run blocks are the newest useful **completed** run, and
the **running** one where there is one. Each block carries the run number, the URL, GitHub's `status` and
`conclusion`, and its times.

**`conclusion` and `verdict` are both there because they have different jobs.**
`conclusion` is GitHub's own word — `success`, `failure`, `action_required` — and it
is what a grading seam maps; `verdict` is the word this check chose for the line —
`passed`, `failed`, `waiting` — and it is what a line template substitutes. A template
cannot compute *failed* from *failure*, and a grading map must not read a word this
package invented, so neither field can stand in for the other.

The disabled line's record is smaller and has no run in it: the repository, the
workflow, its URL, GitHub's `state`
([ADR-0010](0010-a-disabled-workflow-is-a-line-not-a-read.md)), and when the workflow
last changed, as `updated.at`
([ADR-0013](0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md) §5). `state` is free-form
here; the library types three names and this is not one of them.

### 3. `started` is claimed, `ended` is not

Of the three names little-sister reads as an instant — `at`, `started`, `ended` —
this record claims `started` at the top of a run block and `at` only nested, as
`updated.at`. `run_started_at` is GitHub's own field and means what
`started` means, so it is claimed. There is **no completion time on a workflow run**
at all; the nearest thing is `updated_at`, which is when anything about the run last
changed. That is worth keeping and is kept as `updated.at`, under the name the library
reads as an instant at any depth of a record, so a surface that shows a record's times
shows it as one — not under `ended`, which would publish an inference as a fact for the
sake of a prettier vocabulary, and not as the record's own top-level `at`, since it is
not the instant the record is of.

A field GitHub did not send is `null` and never `""`, and `started` and `updated.at` are
each read as a time or `null`, so a value GitHub sent that is not an instant with an
offset costs that field and not the whole result — *this run has not started* is a fact
the record should be able to state. Every other time a record of this package carries
is kept the same way
([ADR-0014](0014-a-run-is-its-readings-and-the-estate-is-its-object.md) §8).

### 4. The sentence does not move

`text` is unchanged, character for character, and stays what every surface shows. The
record is invisible until something renders it. Rewriting these sentences against the
record — dropping the Markdown links the check bakes in today, and letting a
deployment re-word the line — is the library's template stage and belongs to it; doing
half of it here would leave two places deciding what a line says.

## Consequences

- **The floor rose to `little-sister>=0.3.17`**, because `Entry.subject` and
  `Entry.data` are that release's surface.
- **The record's field names are stored keys now**, in the sense that matters: a
  deployment's line template and its grading map will address them, so renaming one
  is a breaking change even though nothing in this package's code says so (PL10).
  They are listed in [`../architecture.md`](../architecture.md) §3.6 with the rest.
- **The escaping moves, but not yet.** A record field is data escaped at render
  (little-sister ADR-0018), which is what will let `_action_text` stop escaping names
  itself — when the template stage arrives, not here.
- **A run's record is 1,521 bytes at its heaviest**, its branch clipped at 300 bytes,
  against the 2 KB default. The type declares its heaviest record, a dependency
  graph's ([ADR-0014](0014-a-run-is-its-readings-and-the-estate-is-its-object.md) §4).
- **A reader of the leaf sees nothing new** until the node's own page is opened,
  where the record renders as a plain list of dotted names and values.

## Alternatives considered

- **`full_name` as the subject**, which reads better in a URL and in a log. Rejected:
  it changes when a repository is renamed or transferred, and a subject that moves
  regroups every line about that repository into a new group with no history. The
  readable name is in the record, where a group label belongs.
- **Only the completed run in the record**, with the in-flight one left to
  `Entry.running`. Rejected: the flag says *something is in flight*, and the record
  says *which run, and where to look* — a reader who wants the second has nowhere
  else to get it, and the sentence carries it only as text.
- **`ended` from `updated_at`.** Rejected in decision 3: it is an inference, and the
  vocabulary is small precisely so that the names in it can be trusted.
- **A record on every aspect at once.** The other six read alerts, pull requests and
  issues, whose records are worth designing against their own surfaces rather than by
  analogy with this one. This is the aspect the library's phase named, and one emitter
  is what that phase asked for.
