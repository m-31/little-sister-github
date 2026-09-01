# ADR-0005 — The `actions` aspect asks per workflow

- **Status:** Accepted
- **Date:** 2026-08-30
- **Related:** [ADR-0003](0003-an-aspect-is-one-question-asked-of-the-whole-scope.md)
  (an aspect is one question asked of the whole scope, which this changes the *asking*
  of), [ADR-0002](0002-a-read-failure-is-not-a-finding.md) (a line that grades nothing,
  and why the coverage line here is not one of those),
  [ADR-0001](0001-a-second-check-type-in-this-package.md) (the API budget, and the run
  this check declines to make)

A bare ADR number here is this repository's; a reference to one of little-sister's
is always written out, because the two numbering spaces overlap.

## Context

The aspect read `/repos/<repo>/actions/runs?per_page=100&branch=<default>`: one page,
the newest hundred runs **across all of a repository's workflows**. A workflow whose
newest run fell below that cut therefore contributed nothing at all — no entry, and
nothing in the aspect's coverage count. With `show_healthy: false`, which is the
default, a workflow nobody had read rendered exactly as one that had passed. The leaf
reported OK with no entries while workflows on the default branch were failing, which
is worse than an incomplete dashboard: it is a reassuring one.

Found on a running deployment against sixteen repositories, not by a suite.

The first correction stated the fact the read *could* support — `total_count` above
the rows returned means the page was a cut — and then went one step further than the
data allows: it named the workflows that had no row in the page. That is unsound, and
the reason is worth keeping. Absence from a branch-filtered page has two causes and
this read cannot distinguish them: the workflow's newest run fell outside the window,
or the workflow does not run on that branch at all. `/actions/workflows` carries
neither a branch nor a trigger, so a `pull_request` linter, a tag-triggered release
job and a `workflow_dispatch` restore job are indistinguishable from a genuinely
unread workflow — permanently, at any page size. On the deployment it was found on,
that produced dozens of standing amber lines accusing workflows that were doing
exactly what they were written to do.

Two bounded reads about a repository cannot answer a question about a workflow.

## Decision

**The default-branch mode asks per workflow.**
`/repos/<repo>/actions/workflows/<id>/runs?branch=<default>&per_page=10` is exact by
construction: an empty answer means this workflow does not run on this branch, which
is an answer and not a gap. The aspect reads the workflow list once per repository as
it already did, filters it by `ignore_workflow_name_patterns` **before** spending
anything, and asks once per surviving workflow.

Ten rows rather than one, because the scan keeps the newest in-flight run and the
newest useful completed verdict independently — one row cannot carry both, and a
`status=` filter would need two reads to get what ten rows get in one. A workflow
whose ten newest runs are all neutral reports no verdict, which is what the old read
also did.

**`all_branches` keeps the wide read, and keeps the coverage line.** There, the
question is the newest state per `(workflow, branch)` over an unbounded set of
branches, and no per-workflow query bounds it. One page remains the only bounded
question available, so that mode stays incomplete and says so.

**The aspect prices its own budget.** The pre-run guard multiplies repositories by
endpoints and runs before any aspect, while the number of workflows in a repository is
not known until the aspect has read its list; the guard is therefore a floor for this
aspect and no longer an approximation. Before spending a read per workflow the aspect
compares the count against the budget **GitHub stated on the response already in
hand** — the `x-ratelimit-*` headers, which cost nothing and describe the bucket the
next requests will be charged to. Below it, that repository degrades to the wide read
and is reported as short rather than reported not at all. Absent headers mean proceed:
a path to GitHub that strips them is not a reason to give every repository behind it a
worse answer.

**One coverage line for the whole leaf, and it grades `WARN`.** Where any repository
was answered about only partly — `all_branches`, a thin budget, or a workflow list
longer than one page — one line names those repositories. Not one per repository and
never one per workflow: it reports the same fact everywhere it is true, and repeating
a fact an operator cannot act on differently is how a leaf teaches its reader to skip
the color. `WARN` rather than `UNDEFINED` because a leaf that *knows* it is incomplete
and renders green is the defect this record exists to remove; `UNDEFINED` is for a
repository GitHub would not answer about, which is a wait-and-see, and this is not one.

## Consequences

The aspect's cost changes shape: from two reads per repository to one plus one per
watched workflow. That is bounded by how many workflows a repository has rather than
by how often they run, it is known before it is spent, and an ignored workflow now
costs nothing where it used to be filtered after the fact.

In the default-branch mode the coverage line disappears — not less often, but for
every estate, because the read it was describing is gone. What remains of it is the
honest residue: two modes and one budget condition that are genuinely partial.

A dashboard that was green because the aspect had nothing to say may become amber,
and that is the defect surfacing rather than a new one.

## Alternatives

**Follow the `Link` header on `/actions/runs`.** The client cannot: `get_paginated`
refuses anything that is not a bare JSON list, and both Actions endpoints are
object-wrapped. Written, it would still be unbounded by run history — a repository
with fifty thousand runs on its default branch is five hundred requests from an aspect
that made two.

**Cap the pages.** Bounded, but it answers nothing: at the cap the aspect still does
not know whether it is complete, so the coverage line survives and merely fires less
often — and the cap is an arbitrary constant that is wrong for somebody. Making it a
configuration key would be worse, not better: a key is a stored key and surface, and
this one would push a decision about *our* read strategy onto every deployment, which
is not in a position to take it.

**Page until every known workflow has been seen.** Self-limiting in appearance only.
A workflow that never runs on the branch is never seen, and every repository has one,
so the condition never holds and the read degenerates into full pagination.

**Name the unread workflows** — what the first correction did. Recorded here as
rejected rather than merely replaced, so that it is not re-proposed as the cheap half
of this: the naming is exactly what the two wide reads cannot support.
