# ADR-0009 — Named branches replace the default branch

- **Status:** Accepted
- **Date:** 2026-09-19
- **Related:** [ADR-0005](0005-the-actions-aspect-asks-per-workflow.md) (the read this
  extends, and the construction it rests on — this record adds a third mode and changes
  nothing ADR-0005 decided), [ADR-0007](0007-the-budget-is-read-where-it-is-spent.md)
  (the guard, which this multiplies), [ADR-0002](0002-a-read-failure-is-not-a-finding.md)
  (why the line this adds is not a coverage line)

A bare ADR number here is this repository's; a reference to one of little-sister's
is always written out, because the two numbering spaces overlap.

## Context

ADR-0005 left the aspect with two modes. The default-branch mode asks
`/repos/<repo>/actions/workflows/<id>/runs?branch=<default>` once per watched workflow
and is exact by construction: an empty answer means *this workflow does not run on this
branch*, which is an answer and not a gap. `all_branches` keeps the wide page and says
where it is incomplete, because "newest per (workflow, branch)" over an unbounded set of
branches has no bounded question.

What no mode covers is the estate that runs its deployments somewhere other than its
trunk — a `release` branch, a `staging` branch — and wants exactly that watched. The
wide mode answers it, badly and incompletely; the default mode does not answer it at
all. ADR-0005 named this as the first bounded form of the all-branches question: the
per-workflow read once per name, exact, and priced before it is spent. The read already
exists and nothing in the client needs to change.

The question the key settles is what the list *means* beside the branch the repository
already has, and the answer has to survive a mixed estate where some repositories call
their trunk `main` and others `master`.

## Decision

**`actions.branches:` replaces the default branch; it does not add to it.** The key
says *ask about these*, and a deployment that wants its trunk watched too writes it in
the list. Adding to the default would make this check resolve every repository's own
default branch into a list the deployment believes it controls, so an estate spelling
its trunk two ways becomes our problem rather than the config's — and it would multiply
the branch count the guard has to price by a number the config cannot see.

**Every watched workflow is asked about every named branch**, at `W × B` reads per
repository. That keeps ADR-0005's construction exactly: each read still asks one
workflow about one branch, and nothing back still means no run on that branch.

**Both halves of the guard price `1 + W × B`.** The pre-run guard (`_priced_reads`) and
the aspect's own per-repository check against the headers in hand (`_budget_covers`)
each multiply by the branch count. A branch count left out of either is a guard that
under-prices by exactly the factor the read multiplies by, which is the one way this
mode could walk a deployment into the rate limit it exists to respect.

**`all_branches: true` together with a non-empty `branches:` is refused at startup**,
naming both keys. The two ask different questions — one watches every branch and reports
where that answer is short, the other asks exactly what it is given and is exact — so a
precedence rule would silently answer a question this config did not ask, and the
deployment would read the mode it did not get off the leaf rather than off an error.

**Where the named branches matched nothing in a repository, one WARN line says so and
names that repository's default branch.** One line for the whole leaf, in the shape
ADR-0005 fixed for the coverage line: the same fact reported once wherever it is true.
It says *no workflow run on any branch this check names*, never *no such branch*, because
the read cannot tell an absent branch from an idle one and ADR-0005 rejected naming what
a read cannot support. The default branch is on the line because it costs nothing —
discovery already carries it — and because `main` against `master` is what this nearly
always is. The line fires only when **no** named branch produced anything in that
repository, so an estate where half the repositories have an idle `release` branch says
nothing as long as another name answered.

**Only an exact read may raise that line.** Where the budget degrades a repository to
the wide page, no row on a named branch is as likely to be the page's cut as the
branch's absence, so the line stays silent and the repository is reported short instead.
With one named branch the wide read still asks GitHub for it; with more it takes the
page unfiltered and keeps the rows on a named branch, which makes that page a cut by
construction and is reported as one whatever `total_count` says.

## Consequences

A third mode on the leaf, and the first one whose failure is a configuration mistake
rather than a budget or an API limit — hence a line that points at the config and names
the fact that contradicts it. The aspect's cost becomes a product rather than a sum,
which is visible before it is spent: `1 + W × B` is known from the last run's workflow
counts and the config's own list.

A deployment that names a branch no repository runs gets a standing amber until it fixes
the key. That is the intended reading — the check was asked about a branch that is not
there — and it is the reason the line names the default branch rather than merely
counting repositories.

## Alternatives

**Add the named branches to the default branch.** Friendlier for the estate that wants
its trunk plus one, and wrong for every other: the check would have to name each
repository's default branch itself, so `main` against `master` would produce reads and
lines the deployment did not ask for and cannot see in its own config. Writing the trunk
into the list costs one line and is visible.

**Resolve `all_branches` and `branches` by precedence.** Whichever won, the deployment
would be reading a mode it did not choose off a leaf that looks normal. A startup
refusal is the only shape that puts the mistake where it was made.

**Spend a read per repository on `/repos/<repo>/branches` to tell an absent branch from
an idle one.** It buys the exact distinction and costs one more read per repository on
the aspect this phase exists to make cheap — against a line that is already actionable
without it, since it names the default branch and the branches asked for. Declined for
the cost; the wording carries the uncertainty instead.

**Say nothing when a named branch matches nothing.** This is the unsound half ADR-0005
rejected when it refused to name unread workflows, turned around: a leaf that renders
green because the question matched no repository is the reassuring dashboard that record
exists to remove.
