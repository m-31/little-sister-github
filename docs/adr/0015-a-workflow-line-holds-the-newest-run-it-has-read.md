# ADR-0015 — A workflow line holds the newest run it has read

- **Status:** Accepted
- **Date:** 2026-10-10 (accepted 2026-09-26)
- **Related:** [ADR-0005](0005-the-actions-aspect-asks-per-workflow.md) (the
  per-workflow read, which this finds is a search),
  [ADR-0013](0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md) (the
  line's object, and the attempt a reading names),
  [ADR-0011](0011-conditional-requests-and-the-cache-that-holds-them.md) (the cache the
  hold is modelled on, whose report it joins),
  [ADR-0012](0012-the-actions-line-carries-what-it-read.md) (the record's `workflow`),
  [ADR-0009](0009-named-branches-replace-the-default-branch.md) (the line for named
  branches that matched nothing),
  [ADR-0007](0007-the-budget-is-read-where-it-is-spent.md) (the guard, which does not
  price the reads this adds),
  [ADR-0014](0014-a-run-is-its-readings-and-the-estate-is-its-object.md) (the estate
  reading that counts them), [ADR-0002](0002-a-read-failure-is-not-a-finding.md) (a read
  that fails is not a finding),
  [ADR-0016](0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
  (§18, the node the workflow's name titles)
- **Register:** [`../decisions.md`](../decisions.md)

A bare ADR number here is this repository's; a reference to one of little-sister's is
always written out, because the two numbering spaces overlap.

## Context

Over one night on a deployment watching ninety-four workflow-branches, `actions` lines
went back from the newest completed run the check had read to an older run of the same
workflow on the same branch — for a poll, and on some lines for many. By the evening 13
of the 94 series held such a run; by the next morning 20 held 34, Dependabot's update
jobs and ordinary workflows alike. The older runs were first attempts, not re-runs, some
months older than runs of that morning. One build workflow's line, which had read a
passing run of two days before, was listed as failed for a run a month older: on a green
aspect that is a false alarm, and an older success hides a newest failure the same way.

**Where it comes from.** The per-workflow read asks for ten rows on the branch, and the
scan took the first row with a verdict in the order GitHub answered, on the premise that
GitHub answers newest first. GitHub's documentation promises no order for either runs
list, and it documents the `branch` parameter as a search, answered up to a thousand
results. A run months older than the newest cannot be among the ten newest of a workflow
that ran every week since, so some answers had the wrong rows and not only the wrong
order: sorting cannot repair those alone.

**The cause, seen since.** A night on the same deployment with the hold in place logged
every answer that went back, and showed what those answers are. For one Dependabot
workflow with over two hundred runs on its branch, the `total_count` of its contradicting
answers ranged from 12 to 88 within a day: GitHub's branch search answers with a
partial set that varies from call to call, not only in the wrong order. Only
contradicting answers are logged, so that is the range among them, and what the answers
that agreed counted is not recorded.

**The conditional cache is not the origin.** It is keyed by URL, a `200` replaces what
it holds and a `304` repeats it (ADR-0011), so the first answer that moves a line back is
GitHub's own `200`; the cache can at most repeat a stale answer while GitHub keeps
answering `304` to it.

**GitHub's retention reaches workflow runs on October 1, 2026.** From that day every run
older than its repository's retention setting goes — 90 days by default, at most 90 for
a public repository and 400 for a private one — and the runs created before it are no
exception: the changelog's *not retroactive* means that data already evicted does not
come back, the documentation's 400-plus days regardless of the setting held until
October 1, and its sentence about new runs only is about changing the setting. From that
day a quiet workflow's runs all go in time, and the per-workflow read answers it empty.

**The line's name.** The scan named a line after the first run it met, and after its
workflow only where the run had no name. For most workflows the two are one. A dynamic
workflow names each run after its job instead — Dependabot's update jobs after the
ecosystem and directory and a job number or the dependencies they update
(`<ecosystem> in <directory> - Update #<job>`), its dependency-graph jobs likewise — so
such a line, and the name its records carried, changed with every run: 18 of the 94
series that night carried more than one name. The workflow list names the two
`Dependabot Updates` and `Dependency Graph`.

## Decision

### 1. Each answer is sorted by run id before the scan

The scan reads each answer newest first by the id GitHub gave the run, then by attempt,
and not in the order GitHub answered it. An id is assigned when a run is created, so the
newer of two runs has the higher id even where the older one started later — two runs
started in one minute are ordered by it. The newest useful completed run and the newest
run in flight are still kept independently (ADR-0005).

### 2. The check holds the newest completed run each line has read

For each line — the workflow on its branch in its repository (ADR-0013 §1) — the check
holds the newest completed run with a verdict it has read, as **transport state like the
conditional cache** (ADR-0011): on the check, in this process, lost at a restart, and the
first answer after one is believed. Runs compare by id, then attempt: a lower id is a
step back and never moves the line, and the same id with a newer attempt that has
completed is a re-run of the held run and replaces it. It holds what the line is
rendered from — the run's id, attempt, number, URL, status, conclusion and times — and
nothing else of it, not even its name (§5). It holds **verdicts only**: the run in
flight rides on the line from the current answer, the newest one in it by id, and an
answer with none has none on the line.

An answer **speaks about** a line when it was asked about it — each workflow and branch
of the exact read, an empty answer included — or when it has a row on it; the wide page
(ADR-0005's `all_branches`, and the budget's fallback) says nothing of a line it has no
row for. A line no answer spoke about for two finished passes of the aspect is
forgotten, as the cache forgets its entries: a workflow deleted, ignored or switched
off, a branch no longer asked, a repository gone from the scope, a line the wide page
stopped showing.

### 3. An answer that goes back on a held run is checked by id

An answer **contradicts** the held run when its newest completed run is older than the
held one, or when it has no completed run for the line at all — which is how GitHub's
retention shows up, every run of a quiet workflow gone. The held run is then read once
by id, `GET /repos/{owner}/{repo}/actions/runs/{run_id}`:

- A **`404`** means GitHub no longer has it. The hold lets go and the answer is
  believed: the line shows the newest run GitHub still has, or goes, as a workflow with
  no run on its branch always has.
- A **`200`** keeps the hold, and a newer attempt of the run that has completed replaces
  it. One still in flight does not: the hold holds verdicts.
- A **throttle** or an **error** keeps the hold for that poll. So do the **deadline** and
  the **pause budget**, which end the aspect as they end any read.

At most one such read per held run per poll, since a line is resolved once a pass, and
**none when the guard's budget fallback** took the repository to the wide page: a
repository the budget cannot pay a read per workflow for cannot pay for these either,
and the hold stands for that poll. The reads go through the client like every other, so
the run's trace and the ledger count them and the conditional cache holds their answers.
**The guard does not price them**: they are bounded by the lines held, and a repeat is a
`304`, which GitHub does not charge.

The division of labor is the decision in one sentence: **the sort repairs an answer's
order, the hold overrides an answer that lacks the newer runs, and the line in the log
counts only what the sort could not repair.**

### 4. Every contradiction is a line in the log, and the estate counts them

Each answer that contradicts a held run is **one `INFO` line**, with a fixed phrase for a
grep or a query to count — *contradicts the held run*:

```
/github: actions: example-org/platform-a · build (5) on main — the answer contradicts the held run 103/1: its newest completed run is 101/1 (total_count 57, 1 row, 200); held: the run is still there
```

It names the line's repository, workflow and branch, the held run, the answer's newest
completed run or `none`, the answer's `total_count` and the rows it returned — which
tell a partial search from a stale one — whether the answer was GitHub's (`200`) or the
cache's (`304`), which checks the cache's part above in production rather than arguing
it, and what the read by id found: `held: the run is still there`, `replaced: a newer
attempt, <id>/<attempt>`, `held: a newer attempt is in flight, <id>/<attempt>`,
`let go: GitHub no longer has the run`, `held: not asked, the budget fallback`, or
`held: not answered,` and `throttled`, `HTTP <status>`, `no answer`, `the deadline` or
`the pause budget`. `INFO` and not `WARNING`: nothing in it is an operator's to act on.

The estate reading counts them too, for ADR-0011's reason: the hold is transport state
the library cannot attribute, and it grows with the lines held. **`runs_held` is a
level**, as `cache_held` is — the runs held when the run ended; **`contradictions` and
`holds_let_go` are this run's counts**, as `free_reads` is. The node's report says them
in a clause beside the conditional cache's: `held runs: 12 kept; this run's answers
contradicted them 3 time(s), 1 let go`.

### 5. A line is named after its workflow

A line and its record's `workflow` take **the workflow's name from the list** the check
reads every run, and the run's name only where the list's entry has none — the reverse
of the old order. ADR-0013 made a line's object the workflow on its branch, and its
label names that object: a dynamic workflow whose runs are each named after their job
reads one name on every run, the one the list gives it — `Dependabot Updates` for
Dependabot's update jobs, `Dependency Graph` for its dependency-graph jobs — and a
workflow renamed in GitHub shows its new name on the next poll even while the hold
stands. The run's name is dropped. The record and the hold gain nothing by it, and the
run the line links to still carries it. **No slug moves**: a slug is keyed on ids
(ADR-0013 §2). The same name titles the workflow's node, which is named after the last
segment of the workflow's `path`
([ADR-0016](0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
§18).

A workflow the list's one page leaves out has no line and no node: its runs are
not asked for on the exact read and are skipped on the wide page, and its repository is
named on the line that says not all runs were read. The run's name stands in only for a
list entry with no name.

### 6. The line for named branches counts what the hold keeps

ADR-0009's line says no watched workflow ran on any named branch of a repository, and
only an exact read may raise it. A held run the read by id finds still there is a run on
that branch, so the line stays silent while one holds; where the hold lets go and
nothing is left, the line is raised as before.

## Consequences

- **A line no longer goes back** on an answer that lacks the newer runs, and an answer
  out of order no longer moves it at all. On the night after the change none did: 40
  answers contradicted a held run, and the read by id found each run still there — one
  of the answers was empty, and four returned 5 rows of the 10 asked for.
- **One read by id per contradiction**, bounded by the lines held and free when it
  repeats; none when nothing goes back.
- **The first answer after a restart is believed.** A check that restarts into an answer
  that goes back shows it until that workflow runs again.
- **One run's rendered fields per line held**, forgotten two passes after no answer
  speaks about the line — small beside the cache's whole payloads.
- **The record's `workflow` changes content** for a workflow whose runs are named after
  their jobs; its key and every slug stay.
- **A line whose every run GitHub's retention took goes** on the poll whose read by id
  answers `404` — from October 1, 2026 a routine event rather than a rare one.
- **Three fields on the estate reading** — `runs_held`, `contradictions`,
  `holds_let_go` — which are keys once shipped, and a clause on the node's report.

## Alternatives considered

- **GitHub's order, as before.** It moved lines back whenever GitHub answered out of
  order.
- **The sort alone.** It repairs order and nothing else: an answer without the newer
  runs still moves the line.
- **More rows, or a second page, when `total_count` says an answer is short.** A second
  read per workflow per poll, and still a search. `total_count` is on the line in the
  log so that how often an answer is short can be read before anything is built on it.
- **Holding the run's id alone.** An answer that leaves the held run out could not
  render it.
- **Holding the run in flight too.** A run in flight is a state that changes by the
  minute; the hold holds verdicts.
- **A hold kept across restarts**, in a file. State on disk for the first poll after a
  restart; that cost is accepted instead.
- **Letting a hold go after some number of contradicted polls**, with no read. A GitHub
  inconsistency longer than that brings the step back, and a run GitHub deleted stays
  that long.
- **Never letting a hold go** but to a newer run or a restart. From October 1, 2026 that
  keeps a verdict whose run retention has deleted, with a link that answers `404` — a
  red no run will clear.
- **Reading by id during the budget fallback.** The fallback exists because the budget
  cannot pay for the reads the aspect would make.
- **Pricing the reads by id in the guard.** They are bounded by the lines held and free
  when they repeat, and the guard already prices a warm run at full cost (ADR-0011).
- **A count only, on the run's closing line**, or **a line only when a held run is first
  contradicted.** The first says how often and not where; the second counts episodes,
  and a line that went back all night would read as going back once.
- **A `WARNING`.** Nothing in the line is an operator's to act on, and a warning nobody
  can act on is the line a reader learns to skip.
- **The run's name beside the workflow's**, the two sharing the name's budget. The
  record stays near its weight, but a workflow name past 73 ASCII characters is cut and
  the hold must keep a name.
- **The run's name in the sentence where it differs.** It needs the same second name in
  the record.
- **The newest completed run's name**, as the sort now finds it. A dynamic workflow's
  line would still change its name with every job.

## References

- <https://docs.github.com/en/rest/actions/workflow-runs> — the two runs lists, the
  `branch` parameter among those a search answers up to 1,000 results for, and a run
  read by id.
- <https://github.blog/changelog/2026-08-27-actions-retention-will-cover-checks-workflow-runs-and-statuses/>
  — retention reaches checks, workflow runs and statuses on October 1, 2026, and what
  *not retroactive* means there.
- <https://docs.github.com/en/organizations/managing-organization-settings/configuring-the-retention-period-for-github-actions-artifacts-and-logs-in-your-organization>
  — the retention period, its range, the 400-plus days until October 1, and what
  changing the setting applies to.

