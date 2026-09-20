# little-sister-github — Decisions

> One digest per decision: the heading, the answer in a few lines, and a link to the
> Architecture Decision Record in [`adr/`](adr/) for the context, the alternatives and
> the date. The digest says **what** was decided; the record says why, and holds the
> history. A bare number here is this repository's; a reference to one of
> little-sister's is always written `little-sister ADR-00NN`, because the two numbering
> spaces overlap. What the decisions add up to, as one document, is
> [`architecture.md`](architecture.md).
>
> A decision is in force unless its heading is marked **superseded**.

---

### ADR-0001 — The API budget is a check type of its own, not an eighth aspect

`github-rate-limit` is a second `type:` in the same package: a rate limit belongs to
the token and not to an account, an aspect would go quiet exactly when the budget is
the story, and `GET /rate_limit` is free to read, so the two checks want different
frequencies. One flat node per token, a coded entry per resource keyed by GitHub's own
resource name; `warn_below` / `error_below` are counts (1000 / 500), per resource where
it says so, and `error_below` above `warn_below` is refused. Graded on what is left,
with four readings it refuses to fake; where the numbers come from is ADR-0007's.
→ [record](adr/0001-a-second-check-type-in-this-package.md)

### ADR-0002 — A read failure is not a finding about the repository

`request_timeout:` bounds one request; `timeout:` bounds the whole run and clamps each
request to what is left of it. A failure is **transient**, **answered** or **malformed**
by status and headers, never by the body: 5xx and transport are *could not ask*, a 403
or 429 with a throttle header and a bare 429 are *not now*, 404, 401 and a bare 403 are
answers, a payload of the wrong shape is malformed. Only a transient failure is retried,
once; its line is `UNDEFINED` and grades nothing, so the aspect grades its own coverage
gap in one WARN line and the node states the run's total once; the deadline keeps what
finished, and a run resumes after the last aspect that finished. → [record](adr/0002-a-read-failure-is-not-a-finding.md)

### ADR-0003 — An aspect is one question asked of the whole scope

The tree is aspect-first: one child per aspect, and a repository is a keyed line, never
a node. One aspect is one question, and the endpoint follows the question (`issues`
drops the pull-request rows). Scope is discovered once — the kind verified, then the
organization, team or user listing, filtered by `name_prefix`, `include_archived` and
`include_forks` — and every aspect starts from that set, narrowing it only where it says
so; the check's own node says only how much was looked at. `enabled: false` switches an
aspect off whole, all off is refused, an aspect a release adds arrives on, and
`secret_scanning:` keeps switching `secret_scanning_alerts`. → [record](adr/0003-an-aspect-is-one-question-asked-of-the-whole-scope.md)

### ADR-0004 — A finding grades; the repository does not

A line is what carries a status, keyed by what GitHub minted — the repository's numeric
id with the pull request, issue or alert number, the finding's URL, the workflow and
branch, or the aspect — never by a name or a position. Amber is a queue, red is something
to do now: an open pull request or issue is WARN; a secret alert, scanning switched off,
a missing dependency graph and a failed workflow are ERROR; the flat aspects' codes are
fixed and only the banded ones are a setting, where the band grades and the alert does
not, a watched band reports while empty, `severities` selects, `severity_map` grades and
the defaults are strict. A workflow's verdict is its last run that said something. → [record](adr/0004-a-finding-grades-the-repository-does-not.md)

### ADR-0005 — The `actions` aspect asks per workflow

On the default branch the aspect reads a repository's workflow list once, filters it by
`ignore_workflow_name_patterns` before spending anything, and asks
`…/actions/workflows/{id}/runs?branch=<default>&per_page=10` once per surviving
workflow — an empty answer means the workflow does not run on that branch, which is an
answer and not a gap. `all_branches` keeps the one wide page of `…/actions/runs`. Before
paying a read per workflow the aspect checks the count against the budget headers
already in hand and degrades that repository to the wide read when they do not cover it.
One WARN line for the whole leaf names the repositories answered about only partly. → [record](adr/0005-the-actions-aspect-asks-per-workflow.md)

### ADR-0006 — A code-scanning alert has two severities, and the check keeps them apart

`code_scanning_security` holds the alerts GitHub gave a security severity, as bands
`critical` / `high` / `medium` / `low`, all ERROR by default; `code_scanning_quality`
holds the rest by the rule's analysis severity, `error` / `warning` / `note`, graded
WARN / WARN / OK. Both are built from one `/code-scanning/alerts` read per repository,
so the guard counts distinct endpoints rather than aspects. The old
`code_scanning_alerts:` block is refused at load, naming both halves, never migrated.
→ [record](adr/0006-code-scanning-has-two-scales.md)

### ADR-0007 — The budget is read where it is spent, and there is more than one of it

A token has two `core` counters split by request path, and `GET /rate_limit` reports one
of them — for some tokens one nothing spends. So the `x-ratelimit-*` headers of every
response feed a process-wide ledger: per token digest and resource, one record per counter
(a distinct `reset`) with the reduced paths GitHub routed to it — not the set it charged
for, which ADR-0011 split off; a pristine reading opens no counter. `github-rate-limit` writes its line from the ledger: the tightest counter grades,
and the line says how many windows there are, how much of the window this process spent,
what something else spends, and what the window carried before this process saw it open.
The `github` guard prices the run per window it will spend; a pause is named by its cause. → [record](adr/0007-the-budget-is-read-where-it-is-spent.md)

### ADR-0008 — The dependency graph is asked, not exported

`sbom_check` asks GitHub's GraphQL API whether a repository has dependency manifests —
`repository { dependencyGraphManifests(first: 10) { totalCount nodes { filename
parseable exceedsMaxSize } } }`, one query per repository, one point each — in place of
the synchronous SBOM export, which GitHub removes on `2026-11-13`. Zero manifests is no
dependency graph and grades ERROR, as before; manifests none of which are parseable
grade ERROR with the cause on the line; more than ten is a graph. `GitHubClient` grows
`graphql()`, a POST with every REST read's deadline, retry and throttle reading, and a
`200` with a per-repository error is read as ADR-0002 reads a status. Every key stays. → [record](adr/0008-the-dependency-graph-is-asked-not-exported.md)

### ADR-0009 — Named branches replace the default branch

`actions.branches:` asks every watched workflow about every branch it names, exactly as
the default-branch mode asks about one, and **replaces** that default rather than adding
to it — so an estate spelling its trunk two ways stays the config's business and not this
check's. Both halves of the guard price `1 + W × B`. `all_branches` together with a
non-empty list is refused at startup naming both keys. Where the named branches matched
nothing in a repository, one WARN line says *no run on any branch this check names* —
never *no such branch*, which the read cannot support — and names the default branch it
saw; only an exact read may raise it. → [record](adr/0009-named-branches-replace-the-default-branch.md)

### ADR-0010 — A disabled workflow is a line, not a read

Its runs are not asked for: the state is on the list read already made, and the newest run
is frozen at whatever it was when the workflow was switched off. One line instead, naming
the cause in GitHub's wording and ending `no runs read`. Disabled is the `disabled_` prefix
rather than a list, so a state GitHub adds later is graded (WARN, as an undeclared severity
is) and not silently read. Shipped: `disabled_manually` and `disabled_inactivity` WARN,
`disabled_fork` OK because forks are discovered by default and nobody chose that state;
`actions.disabled_severity_map` overrides, and an OK line follows `show_healthy`. The line
is keyed without a branch, so a pin on the old frozen verdict must be made again. → [record](adr/0010-a-disabled-workflow-is-a-line-not-a-read.md)

### ADR-0011 — Conditional requests, and the cache that holds them

The client sends `If-None-Match` from a `{url: (etag, payload, link)}` cache held on the
**check** — the client is rebuilt every run — and per check, which is what makes a bare
URL key sound, since a check has one token. The `Link` is held because `get_paginated`
walks it. A `304` is answered from the cache in `_attempt` before `_refusal`, and reaches
the ledger as a reading of the window the path is charged to that spent nothing of it.
The budget read is never held: its body is itself a reading. Entries go after two missed
passes of the aspect that asked, never on a pass the deadline cut short. No byte cap; the
run's `report` says what is held and what it spent against the guard's estimate. → [record](adr/0011-conditional-requests-and-the-cache-that-holds-them.md)
### ADR-0012 — The `actions` line carries what it read

Every `actions` line about one repository says so — `subject` is the numeric id, the one
a rename cannot change and the one its slug is keyed on — and carries a **record**:
repository, workflow, branch, verdict, and a block per run with its number, URL,
`status`, `conclusion` and times. `conclusion` is GitHub's word for a grading map,
`verdict` ours for a line template; neither replaces the other. Of the three timed names
only `started` is claimed (a run has no completion time, so `updated_at` rides as
free-form `updated`), an absent time is null, `text` does not move, and the names are keys.
→ [record](adr/0012-the-actions-line-carries-what-it-read.md)
