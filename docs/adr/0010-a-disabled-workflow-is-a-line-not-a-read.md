# ADR-0010 — A disabled workflow is a line, not a read

- **Status:** Accepted
- **Date:** 2026-10-10 (accepted 2026-09-19)
- **Related:** [ADR-0005](0005-the-actions-aspect-asks-per-workflow.md) (the per-workflow
  read this stops making for some workflows),
  [ADR-0009](0009-named-branches-replace-the-default-branch.md) (the branches that read
  asks about — this line has none),
  [ADR-0016](0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
  (§3, forks are discovered unless a deployment says otherwise; §13, an undeclared
  severity grades WARN; §14, `severity_map` grades what was selected; §20, the
  workflow's node the line stands on; §21, every workflow's line is written, an `OK`
  disabled one too), [ADR-0007](0007-the-budget-is-read-where-it-is-spent.md) (the
  guard, which prices what this read no longer asks for)

A bare ADR number here is this repository's; a reference to one of little-sister's
is always written out, because the two numbering spaces overlap.

## Context

Since ADR-0005 the aspect reads the workflow list once per repository and then asks each
surviving workflow for its newest runs. A **disabled** workflow is in that list and was
asked like any other, which buys a verdict that cannot change: its newest run is frozen
at whatever it was when the workflow was switched off, and it stays on the dashboard as
a verdict about a workflow that is not running. A switched-off nightly job therefore
rendered either as an old green — reassuring, and about nothing — or as an old red
nobody could clear by fixing anything.

The list read already carries the answer. GitHub's own schema
(`github/rest-api-description`, `components.schemas.workflow.properties.state`) gives the
enum as `active`, `deleted`, `disabled_fork`, `disabled_inactivity` and
`disabled_manually`. The documentation pages show only `active` and `disabled_manually`
in their examples, which is how a read written from the prose alone sees two states
where there are five; this record names them from the schema.

The three disabled states are not one fact. `disabled_manually` is somebody's decision.
`disabled_inactivity` is GitHub's, and it is **scoped**: scheduled workflows are disabled
after sixty days without activity *in a public repository*, so on a private estate it is
rare. `disabled_fork` is GitHub's too, and it is the common one — scheduled workflows are
disabled by default on a fork, and `include_forks` is true unless a deployment says
otherwise (ADR-0016 §3), so an estate with forks has one per fork.

## Decision

**A disabled workflow's runs are not read.** The state is known before the request is
spent, and the request cannot return anything the list does not already say. The saving
is one read per disabled workflow per run.

**It gets one line, and the line names the cause.** *Somebody switched this off* and
*GitHub switched this off* are different facts with different answers, so the line says
which — in GitHub's own wording, because an operator who wants the workflow back on will
meet those words in the Actions tab and nowhere else — and ends `no runs read`, so the
absence of a verdict is stated rather than left to be noticed.

**Whether a workflow is disabled is the `disabled_` prefix, not a list of states.** A
state GitHub adds later is then graded and named rather than silently treated as active
and charged a request for runs it cannot have. Such a state grades WARN, as an undeclared
severity does (ADR-0016 §13), and is rendered as GitHub spelled it.

**The shipped grades are `disabled_manually` WARN, `disabled_inactivity` WARN and
`disabled_fork` OK**, and a deployment overrides them in `actions.disabled_severity_map`,
the `severity_map` shape of ADR-0016 §14. `disabled_fork` is the exception to ADR-0016
§15's rule that a default is pessimistic, and for that rule's own reason: it is a state
nobody chose, on repositories nobody is going to act on, and one standing amber per fork
is how a node teaches its reader to skip the color. `disabled_inactivity` keeps WARN for
the case it fires at all rather than as this deployment's likely story.

**The line stands on the workflow's own node**, which stands for the workflow while it
is off (ADR-0016 §20), **and an `OK` one is written as any other** (ADR-0016 §21): a
deployment that discovers forks sees a green node for each workflow GitHub disabled on a
fork, and the wall's *hide ok* and its chips are what put it away.

**The line is keyed without a branch** — `<repo id>-workflow-<workflow id>` — because the
workflow is off everywhere and the line is not about one branch. **This is a breaking
change for a pin**: a maintenance pin held against that workflow's frozen verdict was
keyed with a branch and stops matching, so it has to be made again (PL10).

**The guard prices only the reads that are made.** `_workflow_counts`, which the pre-run
guard prices `1 + W × B` from, counts the workflows whose runs will be read — after the
ignore patterns and after the disabled ones. Counting the whole list would over-price
every run after this change by exactly the workflows it stopped reading.

## Consequences

An estate learns about its switched-off workflows, which is the finding this item
existed for, and pays fewer reads than before rather than more. A repository whose
workflows are mostly disabled gets cheaper every run.

A deployment with forks sees a green node for each workflow GitHub disabled on a fork,
and can turn the fork state up in one line if it wants to. A deployment that runs
seasonal workflows turns `disabled_manually` down the same way.

The re-pin is the visible cost, and it is one line in the release notes rather than a
migration: the frozen verdict the pin was holding is exactly the line this record
removes.

## Alternatives

**Skip disabled workflows entirely** — no read and no line. Cheapest, and it makes the
check silent about a switched-off CI, which is the failure this item was filed to fix.

**Read them as today and mark the line.** Keeps the pin working and keeps the cost, and
what it marks is still a verdict from before the workflow was switched off — a fact
about the past presented among facts about the present.

**Grade every disabled state the same.** Simpler to state and wrong in the common case:
`disabled_fork` is not a decision anybody made about that repository, and grading it with
`disabled_manually` would bury the one that was.

**Name the states from the documentation pages** rather than the schema. That is how this
read would have shipped knowing two of the three; recorded here so the next person
checks `github/rest-api-description` rather than the prose.
