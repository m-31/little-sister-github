# little-sister-github — Decisions

> One digest per decision: the heading, the answer in a few lines, and a link to the
> Architecture Decision Record in [`adr/`](adr/) for the context, the alternatives and
> the date. The digest says **what** was decided; the record says why, and holds the
> history. A bare number here is this repository's; a reference to one of
> little-sister's is always written `little-sister ADR-00NN`, because the two numbering
> spaces overlap. What the decisions add up to, as one document, is
> [`architecture.md`](architecture.md).
>
> Every decision here is in force: a record that is superseded or withdrawn leaves, and
> its digest with it.

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

`request_timeout:` bounds one request; `timeout:` bounds the whole run, in both types,
and clamps each request to what is left of it. A failure is **transient**, **answered** or **malformed**
by status and headers, never by the body: 5xx and transport are *could not ask*, a 403
or 429 with a throttle header and a bare 429 are *not now*, 404, 401 and a bare 403 are
answers, a payload of the wrong shape is malformed. Only a transient failure is retried,
once; its line is `UNDEFINED` and grades nothing, so the aspect grades its own coverage
gap in one WARN line and the node states the run's total once; the deadline keeps what
finished, and a run resumes after the last aspect that finished. → [record](adr/0002-a-read-failure-is-not-a-finding.md)

### ADR-0005 — The `actions` aspect asks per workflow

On the default branch the aspect reads a repository's workflow list once, filters it by
`ignore_workflow_name_patterns` before spending anything, and asks
`…/actions/workflows/{id}/runs?branch=<default>&per_page=10` once per surviving
workflow — an empty answer means the workflow does not run on that branch, where the
check holds no run of it (ADR-0015). `all_branches` keeps the one wide page of
`…/actions/runs`. Before paying a read per workflow the aspect checks the count against
the budget headers in hand and degrades that repository to the wide read when they do
not cover it. One WARN line names the repositories answered about only partly. → [record](adr/0005-the-actions-aspect-asks-per-workflow.md)

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
the SBOM export. Zero manifests, or none parseable, is ERROR with the cause on the line;
more than ten is a graph. A `200` with a per-repository error is read as ADR-0002 reads a
status: `timedout`, GitHub's own time limit, is *could not ask* and asked once more. A
repository GitHub does not answer for is graded on its last answer, *as of* its time,
while that is younger than `sbom_check.max_answer_age` (1h), out of the check's memory. Every stored key stays. → [record](adr/0008-the-dependency-graph-is-asked-not-exported.md)

### ADR-0009 — Named branches replace the default branch

`actions.branches:` asks every watched workflow about every branch it names, exactly as
the default-branch mode asks about one, and **replaces** that default rather than adding
to it — so an estate spelling its trunk two ways stays the config's business and not this
check's. Both halves of the guard price `1 + W × B`. `all_branches` together with a
non-empty list is refused at startup naming both keys. Where the named branches matched
nothing in a repository, one WARN line says *no run on any branch this check names* —
never *no such branch*, which the read cannot support — and names the default branch it
saw; only an exact read may raise it, and not while a held run stands (ADR-0015). → [record](adr/0009-named-branches-replace-the-default-branch.md)

### ADR-0010 — A disabled workflow is a line, not a read

Its runs are not asked for: the state is on the list already read, and the newest run is
frozen from when the workflow was switched off. One line instead, naming the cause in
GitHub's words and ending `no runs read`. Disabled is the `disabled_` prefix, so a state
GitHub adds later is graded (WARN, as an undeclared severity is) and not silently read.
Shipped: `disabled_manually` and `disabled_inactivity` WARN, `disabled_fork` OK, a state
nobody chose on a fork discovered by default; `actions.disabled_severity_map` overrides,
and an OK line is written as any other (ADR-0016). Keyed without a branch, so a pin on
the old frozen verdict must be made again. → [record](adr/0010-a-disabled-workflow-is-a-line-not-a-read.md)

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

Every `actions` line says what it is about — the workflow on its branch, as ids — and
carries a **record**: repository, workflow (named as the list names it) and its file,
branch, verdict, and a block per run with its number, URL, `status`, `conclusion`, times
and, once completed, how long it took. `conclusion` is GitHub's word for a grading map,
`verdict` ours for a line template; neither replaces the other. `started` is claimed,
and the named run's is the record's own `at`; `ended` never is — a run has no completion
time — so `updated_at` is kept as `updated.at`, typed; a time that is not one is null,
`text` does not move, and the names are keys. → [record](adr/0012-the-actions-line-carries-what-it-read.md)

### ADR-0013 — The object of an `actions` line is the workflow on its branch

A run line's subject is `<repository id>:<workflow id>:<branch>`, a disabled line's
`<repository id>:<workflow id>`: ids a rename cannot move, the branch verbatim and last,
joined by a colon git forbids in a ref name; past 200 characters the branch is `sha256:`
and 32 hex digits. One measurement per line, its record the `Entry`'s `data`, with
`repository_id`, `workflow_id` and each run's `id` and `attempt`. A run line names
`<run id>/<attempt>` of the verdict's run, or of the run in flight before any; a
disabled line its `updated_at` as `updated.at` and the record's `at` keep it, in UTC, or
its `state` failing that. A renamed branch starts a new history. → [record](adr/0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md)

### ADR-0014 — A run is its readings, and the estate is the check's object

`measure()` hands back the estate, one reading per repository in scope, and each
finished aspect's readings — one per finding, workflow and unreadable repository, each
naming its `aspect` and `kind`; `grade()` reads nothing else, and every line one reading
became carries that reading as its `data`. Subjects only where there is to be a history:
the estate, `<owner>[;team=][;prefix=][;archived=yes][;forks=no][;host=]`; an `actions`
line; a budget, `<login>;resource=<resource>[;host=]`, asked of `/user` once per
process, none for an installation token, and no identity. Free text is clipped once, a
workflow's name and its file at 150 bytes, a branch's copy at 300, a manifest path at 70 cut from its front; the heaviest record, a workflow run's at 1773 bytes, is declared past the library's warning line at the default limit, on purpose; every time is typed, or null. → [record](adr/0014-a-run-is-its-readings-and-the-estate-is-its-object.md)

### ADR-0015 — A workflow line holds the newest run it has read

Each answer's runs are sorted by run id, and the check holds the newest completed run each
line has read — transport state like the cache, lost at a restart, swept two passes after
nothing spoke of the line. An answer that goes back on it, an empty one included, is
checked by reading the run by id: a `404` lets go, a `200` keeps it or a completed newer
attempt replaces it, anything else keeps it for the poll; none on the budget fallback, and
the guard prices none. Each contradiction is one `INFO` line; the estate carries
`runs_held`, a level, and this run's `contradictions` and `holds_let_go`. A line is named
after its workflow, from the list. → [record](adr/0015-a-workflow-line-holds-the-newest-run-it-has-read.md)

### ADR-0016 — An aspect asks the whole scope, a finding grades, and a workflow is a node

The tree is aspect-first, an aspect one question asked of one discovered scope, and the
check's own node says how much was looked at; a keyed line carries the status, amber for
a queue and red for something to do now, a band grading banded alerts. In `actions` a
repository is a node grading nothing of its own unless it could not be read, a workflow
a node beneath it named by its file and titled by its name, a branch a level beneath
that only where several are configured, a disabled workflow's line on its own node;
`show_healthy` is retired, nodes say their children are complete and that a run names
them, and nothing migrates. → [record](adr/0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
