# little-sister-github — Architecture

How the package is built **today**: what is true, and what follows from it. The
record beside a rule says where its argument lives — [`decisions.md`](decisions.md)
digests every record in [`adr/`](adr/) — and this document never restates the
argument. The [README](../README.md) is the front door: the contract, install, the
configuration keys and what the token needs. A bare `ADR-000N` here is this
repository's; one of little-sister's is always written `little-sister ADR-00NN`,
because the two numbering spaces overlap.

---

## 1. Two check types, one client

| | |
|---|---|
| Package | `little_sister_github` — importing it registers **`github`** and **`github-rate-limit`** in little-sister's check registry; `require_api(3)` refuses at startup, naming both epochs, when the library has moved past check API epoch 3 |
| Modules | `github.py` (the client and the `github` check), `rate_limit.py` (the `github-rate-limit` check), `budget.py` (the ledger both write to and read from) |
| Library floor | `little-sister >= 0.3.19`, the release that stamps its default log in the configured timezone, which is the clock this package's log lines name a window's end on (§5; little-sister ADR-0120). It speaks check API epoch 3 — a check is `measure()` and `grade()` (little-sister ADR-0086) — and promises the surface this package imports — `little_sister.checks`, `little_sister.fetch`, `little_sister.transport`, `little_sister.reasons`, `little_sister.spans`, `little_sister.status`, `little_sister.values` (little-sister `architecture.md` §11) — and it is never a pin, because two plugins that each pinned could not be installed together |
| Beneath it | the standard library's `urllib`, through the library's `fetch`; no dependency but little-sister itself |
| Credential | one token per check, a **reference** (`env://NAME`) resolved once at startup (little-sister ADR-0023); a reference that resolves to nothing pins the check to a visible ERROR before it ever runs |

Two types in one package because they read one host through one client with one
credential, and version together; the budget is a type of its own rather than an
aspect because a rate limit belongs to the token, not to an account (ADR-0001 §1).
Since ADR-0008 the one client speaks two dialects, REST and GraphQL, and that is the
same argument against a second client (ADR-0008 §3).

---

## 2. The client — `GitHubClient`

Built once per run with the token, `api_url`, `request_timeout`, the run's `Deadline`,
`max_pause` and the ledger — by `github-rate-limit` with `timeout:` as both the
request's limit and the run's, and no `max_pause` (ADR-0002 §1). The
request, the deadline, the retry and the `User-Agent` (`little-sister/<version>`) are
the library's (`fetch`, `transport.Deadline`, `transport.ask` — little-sister ADR-0058,
ADR-0002 §10); what is GitHub's lives here: the `Authorization` and API-version
headers, the `Link` walk of a paginated list, the throttle reading, and the budget
headers (ADR-0007 §1).

**Two dialects.** `get(path, params)` and `get_paginated(path, params)` are REST reads;
the paginated one refuses anything but a bare JSON list, which is why the two
object-wrapped Actions endpoints are read one page at a time. `graphql(query,
variables)` is a `POST` with a JSON body to `{api_url}/graphql` — beside a GitHub
Enterprise Server's `/api/v3` its sibling `/api/graphql` — through the same attempt as
every REST read, so the headers, the deadline, the retry, the throttle reading and the
ledger's feed are the client's and not the dialect's; nothing outside the client knows
GraphQL (ADR-0008 §3).

**Three budgets**, and they are not the same one (ADR-0002 §1, ADR-0007 §4):

| Budget | Bounds | Enforced |
|---|---|---|
| `request_timeout:` (15s) | one request, at the socket | handed to `fetch`, clamped to what is left of the run |
| `timeout:` | the whole run | checked before every request and every page, and the response body is read in bounded chunks against it — which is what a socket timeout alone never does against a server dribbling one byte at a time |
| `max_pause:` (half of `timeout:`) | how much of the run is spent asleep | in the client's sleep: a wait that would pass it is refused whole, never trimmed, and ends the run the way the deadline does, with what finished kept |

**Three faults**, set on `GitHubError` from the status and the headers and never from
the body (ADR-0002 §2, §3) — but for an error about one repository inside a GraphQL
`200`, which has neither and is read by its `type`, and by its one-word message where it
has no type (§3.5):

| What came back | Fault | Retried |
|---|---|---|
| 5xx, or a transport failure | `TRANSIENT` — could not ask | once, after a one-second backoff, only while the run can still afford the backoff |
| 403 or 429 with a throttle header; a bare 429 | `TRANSIENT` — not now | once, after the wait GitHub named — `retry-after`, then `x-ratelimit-remaining: 0` with `x-ratelimit-reset`, else 60s — when the run and `max_pause` can afford it; a wait past the run is refused and the error re-raised |
| 404 | `ANSWERED` — the thing is absent | no |
| 401; a bare 403 | `ANSWERED` — this token may not see it | no |
| a body that is not JSON; a payload of the wrong shape | `MALFORMED` | no: asking again is handed the same bytes |

The client keeps **two sleep totals** — `throttled_seconds` for the waits GitHub asked
for, `retried_seconds` for the library's backoff after a failure GitHub did not explain
— and `paused_seconds` is their sum, which is what `max_pause` is checked against. Every
wait is logged at warning with its cause and what is left of the run.

**Every answer's budget headers** — `x-ratelimit-resource`, `-limit`, `-remaining`,
`-used`, `-reset` — are parsed into `RateLimitHeaders`, kept as `last_rate_limit` for
the trace, and handed to the ledger (§4.1). `last_rate_limit` is cleared before every
attempt, so a read that got no answer reads as *no budget headers on that read* rather
than as the previous response's numbers (ADR-0007 §5).

---

## 3. The `github` check

One node per configured account, a child per enabled aspect, a keyed line per finding
(ADR-0016 §1) — and beneath `actions` a node per repository and per workflow (ADR-0016
§17–§19). A run is discovery, the guard, the aspects in roster order, the node's own
reading — in that order, all inside `timeout:`.

A run is also **two halves** (ADR-0014, little-sister ADR-0086). `measure()` does the
reading: it hands back the **estate** reading first, then one reading per repository in
scope, then each finished aspect's readings in the order it read them — one per finding,
workflow and repository it could not read — every record naming its `aspect` and `kind`.
`grade()` builds the node, the aspect children and every line out of those records and
nothing else: not the client, not the clock, not the run state the measuring half keeps
for the next run (the resume point, the workflow counts, the last run's length, the
conditional cache, the held runs and the last answers of §4.1b and §4.1c). An aspect the
deadline cut off hands back nothing, so its child is absent. Free text is clipped once,
in the measuring half — 300 characters and 600 bytes, a workflow's name and its file 150
bytes each, every copy of a branch 300, and a manifest's path 70, cut from the front
behind a `…` — and the line says the text the reading kept, and a run line's slug is
built from what its subject spells. Every time a record carries is typed, under `at`,
`started` or `ended`, and read as a time or `null` (ADR-0014 §8). The heaviest record is
a workflow run's, 1773 bytes, and `expected_record()` declares it for startup to hold
against `record_limit` — past the share of the library's default at which every start
says that a declared record is close, on purpose: a deployment quiets that with a
`record_limit` of 2217 or more (ADR-0014 §4). A dependency graph that stands on its last
answer (§4.1c) comes next, at 1635. A filter that spares a request is applied while
measuring; one that only chooses what is said, while grading.

Only two kinds of reading carry a **subject**, because a subject is what gives a
reading a history (little-sister ADR-0085 decision 2): the
estate, whose subject is the estate as its configuration draws it —
`<owner>[;team=<team>][;prefix=<prefix>][;archived=yes][;forks=no][;host=<host>]`, the
filters only where they are set away from their defaults — and an `actions` line
(§3.7). Declared at construction, so a run that raises is recorded against the
estate.

### 3.1 Discovery

`GET /users/{login}` verifies the declared `kind:` against the account, and a
disagreement is a refusal naming both. Then the listing: `/orgs/{org}/repos`; with
`team:`, `/orgs/{org}/teams` and `…/teams/{team}/repos`; for a personal account,
`/user/repos?affiliation=owner` when `GET /user` says the token is the account's own,
otherwise `/users/{login}/repos`, which is public repositories only, and the node says
`(public only)`. Filtered by `name_prefix`, `include_archived` (false) and
`include_forks` (true) into typed `Repo` values — the seam where the API's `Any` stops
— and every aspect starts from that one set, narrowing it only where it says so
(ADR-0016 §3). A transient discovery failure is WARN with **no children**, so every
aspect keeps its last reading; a discovery failure GitHub answered, or a malformed
listing, is ERROR (ADR-0002 §9).

### 3.2 The guard

After discovery and before any aspect, `_budget_short` prices the run against every
budget window it will spend and skips it, with a WARN naming the window where it knows
one, when a window cannot afford it (ADR-0007 §3). The reads are priced per path in the
ledger's reduced form (`ASPECT_ENDPOINT`): one per repository per endpoint, two aspects
on one endpoint counted once (ADR-0006 §2), `actions` as the workflow list plus one per
workflow the last run saw in that repository times the branches it is asked about
(`1 + W × B`, a floor before the first run, and `B` is one unless `actions.branches`
names more — ADR-0009); `W` counts only the workflows whose runs are read, so an
ignored or a disabled one is priced at nothing, as it costs nothing (ADR-0010), `sbom_check` as one point per repository asked. Which window a path lands on is found in
three rungs:

1. a path the ledger saw charged to a window this hour lands on that window;
2. a path it has not seen this window lands on the tightest window known of the
   resource that path was last charged to — of any resource for a path never seen;
3. a path whose resource has no window open, and every path when no known window
   outlasts the run, is priced by `GET /rate_limit` as every release before the ledger
   did it, and the skip line then names no window.

For each window the reads landing on it are summed, what something else is measured to
be spending on it over the length of the last run comes off its `remaining`, and the
run is skipped when what is left is under `rate_limit_safety_factor` (4) times those
reads. A window that resets before the run would be through — measured by the last
run's length, and by `timeout:` before one has been measured — is outside the guard:
neither priced nor a fallback, because a window that ends inside the run cannot lock
the next one out. The unit follows the window: *API calls* on a REST window, *points*
on `graphql`. A skipped run spends nothing beyond discovery and leaves the next guard's
run length as it was; the `github-rate-limit` check on the same token is what explains
it.

### 3.3 The roster and the deadline

`ASPECTS` is one constant answering three questions: the roster `enabled:` is read
against, the order a run asks in — worst-first by what a finding costs,
`secret_scanning_alerts`, `security_advisories`, `code_scanning_security`, `actions`,
`sbom_check`, `code_scanning_quality`, `pull_requests`, `issues` — and the rank each
aspect node declares (`aspect_rank`, little-sister ADR-0055). A run **resumes after the
last aspect that finished**, so a scope the budget never fits is read once per cycle
instead of the same head every time; the resume point is a name, held in memory, and a
restart begins at the head (ADR-0002).

The deadline is checked in two places: before each aspect, so one whose budget is gone
is never started, and inside the client, so a long aspect is cut off partway. Either
way the aspects that finished are reported, the rest are absent, and the node says
`run cut short after 63s of its 60s timeout — 4 of 8 aspects reported` (ADR-0002 §7). A
wait `max_pause` cannot afford ends the run the same way, naming the wait it refused.

### 3.4 The node's own reading

Everything the container says about itself is **coverage** (ADR-0016 §4): the scope
line — `14 repositories in scope`, WARN under `expect_min_repos`, `no repositories in
scope (…)` naming the filters it looked with; the run's could-not-ask total, once —
`3 repository reads could not be completed this run — GitHub did not answer`; what the
run slept, by cause — `paused 61s for a GitHub rate limit`, `paused 3s retrying after
GitHub did not answer`; and the cut-short sentence. Its report is the discovered roster.
The node's code is the worst of its aspects, as any container's is; permission errors
are counted nowhere here, being amber already on the line where they happened (ADR-0002
§6).

### 3.5 The aspects

An aspect declares no code of its own: under the five flat aspects every line is an
`Entry` carrying its own code and the aspect's is derived as the worst of them
(little-sister ADR-0042), under the three banded ones the band carries it (below) — the
shape that lets a *could not ask GitHub* line be `UNDEFINED`, displayed and pinnable,
and grade nothing (ADR-0002 §4). One read failure per repository is isolated in a
`_Coverage`: a transient one is that `UNDEFINED` line, an answered or malformed one a
WARN line (`<id>-unreadable`), and whenever anything could not be asked about, the
aspect adds one WARN line of its own, `GitHub did not answer for 1 of 40 repositories`
(ADR-0002 §5).

| Aspect | Block | Reads (the ledger's reduced path) | Shape |
|---|---|---|---|
| `secret_scanning_alerts` | `secret_scanning:` | `secret-scanning/alerts` | flat; an open alert is ERROR, a 404 is *scanning not enabled* and ERROR unless `require_enabled: false` |
| `security_advisories` | `security_advisories:` | `dependabot/alerts` | banded: `severities` selects, `severity_map` grades |
| `code_scanning_security` | `code_scanning_security:` | `code-scanning/alerts` | banded on the security severity, `critical` / `high` / `medium` / `low` |
| `actions` | `actions:` | `actions/workflows`, then a page of runs per workflow and branch; `actions/runs` in `all_branches` mode | a node per repository and per workflow beneath it, and per branch beneath that where several are configured; a line per workflow and branch that has a run |
| `sbom_check` | `sbom_check:` | `graphql` — one query per repository, one point | flat; at most one line per repository |
| `code_scanning_quality` | `code_scanning_quality:` | *the same read as `code_scanning_security`* | banded on the analysis severity, `error` / `warning` / `note` |
| `pull_requests` | `pull_requests:` | `pulls` | flat; WARN per open pull request |
| `issues` | `issues:` | `issues` | flat; WARN per open issue, minus the pull-request rows; *issues disabled* is WARN |

**Narrowing.** Three aspects read the Advanced Security scope: with
`advanced_security_on_private` off, private repositories drop out of both code-scanning
aspects and secret scanning, and are named on the aspect's node (ADR-0016 §6).
`sbom_check.ignore` and `issues.ignore` skip their repositories; a code-scanning or
Dependabot 404 is *not enabled* and skipped quietly.

**Banded aspects** (ADR-0016 §12–§15). The findings sit under one uncoded leaf per
severity band; the band carries the code for the lines beneath it, so one `severity_map`
entry regrades every finding at that severity. A band named in the map or present in
the data renders — empty and OK while it is watched; a severity nothing declared gets a
WARN band. Read failures stay on the aspect container, having no honest severity. The
band's title is a colored circle by severity name (`BAND_GLYPHS`), the same across
aspects and deployments.

**`actions`** (ADR-0005, ADR-0009, ADR-0010, ADR-0015, ADR-0016 §16). Per repository the
workflow list, filtered by `ignore_workflow_name_patterns` before anything is spent. A
workflow whose `state` begins `disabled_` is **not read**: its newest run is frozen at
whatever it was when it was switched off, so it gets one line keyed `<repo
id>-workflow-<id>`, with no branch, naming the cause in GitHub's words and ending `no
runs read` — `disabled_manually` and `disabled_inactivity` WARN, `disabled_fork` OK
because forks are discovered by default, `actions.disabled_severity_map` overriding any
of them, and an unnamed state WARN. For the rest — on the default branch, or on each
branch `actions.branches` names instead of it — ten runs per surviving workflow and
branch, sorted by run id — GitHub promises no order — from which the newest useful
verdict (a completed failure or pass, or a run held for approval) and the newest
in-flight run are read independently; canceled and skipped runs erase nothing, and a run
in flight is an extra flag on the line. The line takes its workflow's name from the
list, and the run's only where the list gives none. The check **holds** the newest
completed run each line has read (§4.1b): an answer that goes back on it — its newest
completed run older, or none — is checked by reading the held run by id, which a `404`
lets go and a `200` keeps, a completed newer attempt replacing it; a throttle or an
error keeps it for the poll, and the budget fallback below reads nothing by id.
`actions.branches` together with `all_branches` is refused at startup. The per-workflow
read is priced against the budget headers already in hand, a read per workflow per named
branch; when they do not cover it, that repository degrades to the one wide page of
`…/actions/runs` that `all_branches` always reads — asked of the one named branch where
there is one, and otherwise taken unfiltered and cut to the named branches, which makes
that page a cut by construction. One WARN line (`runs-window-partial`) names the
repositories answered about only partly: those whose wide page was a cut of the
repository's runs, and those whose workflow list was longer than one page. A second
(`branches-unmatched`) names the repositories where no watched workflow ran on any named
branch, with each one's default branch; it is raised only from an exact read, because a
degraded page cannot tell an unmatched branch from its own cut, and it says *no run on*
rather than *no such branch* for the same reason; nor is it raised while a held run
GitHub still has stands on a named branch. A 404 on the list is Actions switched off,
and no line.

**The aspect is a tree** (ADR-0016 §17–§22). Beneath the aspect's node is a node for
each repository with a line, named by the repository's name: it grades nothing of its
own unless the repository could not be read, and then its read line stands on it,
`<id>-workflows-unreadable` or `<id>-runs-unreadable`. Beneath that is a node for each
workflow with a line, named by `_workflow_file` — the last segment of the workflow's
`path`, `ci.yml`, or `dependabot-updates` for Dependabot's
`dynamic/dependabot/dependabot-updates`, or the workflow's id where GitHub sent no path
— and titled by its name. Its line stands on it, a passing idle workflow's too, and a
disabled workflow's in every mode. With two named branches or more, or `all_branches`, a
node for each branch beneath the workflow's holds the line instead, named by
`_branch_node` — the branch as the line's subject spells it, each `/` written `:` — and
titled by the branch where the two differ. The coverage line, `runs-window-partial` and
`branches-unmatched` stand on the aspect's node. The aspect's node says its repositories
are complete; a repository's node says its workflows are where its answer was whole — no
read line, and not short; a workflow's node says its branches are where it has none to
have — in a mode without a level, and while it is disabled — and otherwise where its
repository's answer was whole. Every node beneath the aspect says that a run names it
(`dynamic=True`, little-sister ADR-0118).

**`sbom_check`** (ADR-0008). `repository(owner:, name:) { dependencyGraphManifests(first:
10) { totalCount nodes { filename parseable exceedsMaxSize } } }`, aliased `r0`, one
repository a query. A `200` is read per alias: `totalCount` of zero is
`no dependency graph (0 manifests)` and ERROR; manifests none of which are parseable are
ERROR with the cause on the line; more than ten is a graph. Its `errors` are read as the
faults are: `NOT_FOUND` is a repository gone since discovery, a line that grades
nothing; `FORBIDDEN` or `INSUFFICIENT_SCOPES` is a permission answer, WARN; an error
with the message `timedout` and no type is GitHub's own time limit reaching that
repository's part of the query, *could not ask*; any other type is *could not read* with
the type on the line; errors with no `data` at all are *could not ask* for every
repository the query carried. A switched-off graph answers zero, and is red.

A repository the time limit cut is **asked once more** when the roster is through, in
the roster's order. A second asking is one request, `graphql(once=True)` — not retried,
and no wait taken for a throttle — and it is made only while the run has more than one
`request_timeout` left; one that GitHub answers with a throttle is the last of the pass.
What the second asking says is the repository's reading (ADR-0008 §8). And a
repository that ends the pass as *could not ask* — cut again, a 5xx, a throttle, errors
with no `data` — is graded on **its last answer** (§4.1c) while that is younger than
`sbom_check.max_answer_age`, an hour by default: its reading is the held graph with the
time GitHub answered as the record's `at`, its line is the line that graph has — the
same slug and code — ending `— as of <time>`, written by the library in the configured
zone and format, and it counts as read, so it is on no *could not ask* line, in no
coverage line and not in the node's count (§3.4). The readings stand in the roster's
order, whichever asking each came from.

### 3.6 The keys nobody may rename

A deployment's configuration and its maintenance pins are written against names this
package publishes, so every one of these is a breaking change to move, whatever the code
says (ADR-0016 §7 and §8):

- the two `type:` names;
- the eight aspect names, which are node-path segments;
- the configuration blocks — `secret_scanning:` switches `secret_scanning_alerts`, the
  one block not named for its aspect, and both spellings stay;
- a line's slug: the repository's **numeric id** with the pull request, issue or alert
  number (`6304-pr-42`), the finding's `html_url` where there is no number, the workflow
  id and branch for a workflow line, or a word for the finding's kind where an aspect
  reports at most one line per repository (`sbom`, `secret-scanning-off`, `issues-off`)
  — `<id>-unreadable` for a repository that could not be read (`actions` keeps its two
  reads apart, `<id>-workflows-unreadable` and `<id>-runs-unreadable`), and the two
  aspect-level lines, `read` and `runs-window-partial`;
- the names of the nodes beneath `actions` — a repository's name, a workflow's file, and
  a branch as its line's subject spells it with each `/` written `:` — which are
  node-path segments, and so what a pin and a `nodes.yaml` entry beneath the aspect are
  held against (ADR-0016 §17–§19);
- the resource names on the `github-rate-limit` node, GitHub's own (`core`, `graphql`);
- **the field names in a line's record** (§3.7) — every line one reading became
  carries one — nothing in the code reads them, and that is exactly why they are here:
  a deployment's line template and its grading map will, so renaming one breaks a
  deployment that this package cannot see (ADR-0012, ADR-0014 §7);
- **the subject shapes** — the estate's, the two of an `actions` line (§3.7) and a
  budget's (§4.2) — which are what a series is keyed by: changing one starts every
  history it names again (ADR-0013, ADR-0014).

A retired key is **refused at load**, naming its replacement or saying why there is
none, never migrated or ignored: `org:` (now `owner:`), `code_scanning_alerts:` (now two
blocks — ADR-0006 §4), a `subnodes:` entry naming a retired aspect, the `{org}` token in
display text (now `{owner}`), and `actions.show_healthy`, to either value — every
workflow's line is written now (ADR-0016 §21).

### 3.7 What a line carries beside its sentence

A line says what it is **about** and keeps what it **read** (ADR-0012, little-sister
ADR-0082). Every line a grading writes out of one reading carries it through: the
line's `data` is the reading's record, whole, and its `subject` the reading's (ADR-0013,
ADR-0014 §6 and §7) — in `github.py` by one helper, `_carrying`, so the aspects carry
what they read in one way. That is every `actions` line, every finding —
a pull request, an issue, a Dependabot, code-scanning or secret-scanning alert, a
dependency graph — every *secret scanning not enabled* and *issues are disabled* line,
every note about a repository that could not be read or is gone, and every
`github-rate-limit` line (§4.2). A line written out of many readings — the `read` count,
`runs-window-partial`, `branches-unmatched` — carries none. A banded aspect's lines
carry no code, so their band declares them members (`entries=True`), which is what
keeps each one a line a pin holds.

A finding names no subject (ADR-0014 §2), so its line's is empty, and its record is its
`aspect` and `kind`, `repository` and `repository_id`, and what the aspect read of it:
a pull request's `number`, `title`, `user` and `url`; a Dependabot alert's `number`,
`severity`, `summary` and `url`; a code-scanning alert's `number`, `severity`, `detail`
and `url`; a secret-scanning alert's `number`, `secret`, `created.at` and `url`; an
issue's `number` and `title`; a dependency graph's `total`, its first ten
`manifests`, each a `filename`, `parseable` and `exceeds_max_size`, and `at`, the
record's own time — `null` on the run that read the graph, and when GitHub answered
with it where the graph stands for a run GitHub did not answer (§4.1c). *Scanning not
enabled* and *issues are disabled* carry the repository alone; a note carries the
read's `fault`, `error` and `part`, or, for a repository gone, GitHub's `message`.

The object of a run line is **the workflow on one branch** —
`<repository id>:<workflow id>:<branch>`,
the ids a rename cannot move and the branch verbatim, joined by the colon git refuses in
a ref name; a branch that would pass 200 characters is written as `sha256:` and 32 hex
digits of its name. A disabled line's object is the workflow everywhere,
`<repository id>:<workflow id>`. A renamed branch starts a new history. The coverage,
`runs-window-partial` and `branches-unmatched` lines have no subject, because they are
about the estate and not about one object; a view that groups by repository groups by
`repository_id`.

The record of a run line is its `aspect` and `kind`, the repository and its id, the
workflow — its name as the workflow list gives it — and its id, its `file`, which names
its node (ADR-0016 §18), the branch, the verdict, and a block per run — the newest
useful **completed** one and the **running** one where there is one — each with GitHub's
`id` for the run and which `attempt` of it the block shows, the run number, the URL,
GitHub's `status` and `conclusion`, its times, and `duration_s`. `conclusion` is
GitHub's word and is what a grading map reads; `verdict` is this check's word for the
line and is what a template substitutes; neither stands in for the other. A block's
times are `started`, GitHub's `run_started_at`, and `updated.at`, GitHub's `updated_at`
— when anything about the run last changed, nested under `at` rather than claimed as
`ended`, since a workflow run has no completion time (ADR-0012 decision 3). The record's
own time, its top-level `at`, is the `started` of the block its identity names (below):
its series places the record there, and its node draws the run there; where GitHub sent
no start it is `null`, and the record stands where it was first seen (ADR-0012 §3).
`duration_s` is how long the run took, in whole seconds from `started` to `updated.at`,
once GitHub's `status` is `completed` — of a finished run the last change is taken for
its end in this one number — and `null` while it is not, where either time is missing,
and where the second lies before the first (ADR-0012 §3). The type declares it as the
measure `completed.duration_s`, in `s` and labeled *Duration*, so a workflow's node
draws each run as a stem to it; a deployment's `measures: {completed.duration_s: null}`
takes it away. A time GitHub did not send, or sent as something that is not an instant,
is `null`, never `""` — the library refuses a timed name whose value is not a time, and
the whole result with it. A disabled workflow's line carries its `aspect` and `kind`,
the repository and its id, the workflow and its id, its `file`, its URL, GitHub's
`state`, `updated.at` — the workflow's `updated_at`, typed as a run block's is — and the
same instant as its own `at`, so its series places it where the workflow last changed,
its switch-off as a rule (ADR-0013 §5).

A run line's reading also names the **event** it is of, its identity: `<id>/<attempt>`
of the completed block — the run the verdict is of — or, where nothing on the branch has
completed, of the running one (ADR-0013 §4). A finished run read again replaces its own
record in the workflow-branch's series; a run in flight rides on the record of the
verdict before it until it completes; a re-run, which keeps the id and the run number,
is an attempt and a point of its own; a cancelled run is none. Where GitHub sends no
attempt the id stands alone, and a run with no id names nothing and appends.

A disabled line's reading names **when its workflow last changed**: the instant
GitHub's `updated_at` names, exactly as `updated.at` keeps it — UTC, with a `Z`
(ADR-0013 §5) — is its identity, so a workflow that stays off is one record in its
series and each switch-off another, the same cause included, however GitHub writes the
time. Where the record keeps no time GitHub's text is the identity; where GitHub sent
none, or one longer than an identity may be or with a control character, the reading
names its **state** instead — GitHub's `state`, or `sha256:` and 32 hex digits of it
where that would not travel either. No other reading of the `github` check names an
identity or a state.

`text` is unchanged by all of this and stays what every surface shows; the record is
invisible until something renders it, which today is the node's own page.

---

## 4. The budget — the ledger and the `github-rate-limit` check

### 4.1 The ledger — `budget.py`

One `Ledger` for the process (`ledger()`), behind a lock, fed by every response either
check type receives and never persisted: an hour's memory is refilled by the first run
after a restart (ADR-0007 §1). Its unit is the **counter** — one window of one budget —
keyed by the token's digest (sixteen hex characters of SHA-256, never the value, never
logged), the resource GitHub named on `x-ratelimit-resource`, and the `reset` epoch,
which is the counter's identity. A counter holds the last `limit`, `remaining` and
`used`, when it was first and last read, this process's own attempts since the first
reading, and the **reduced paths** GitHub routed to it — which is not the set it was
charged for, and the paragraph after this one says why: `reduce_path` keeps the two
segments
under `/repos/{owner}/{repo}/` with ids and query gone (`dependabot/alerts`,
`actions/workflows`), or the first segment of anything else (`orgs`, `user`,
`graphql`, `rate_limit`) — the size of the API, and the form `ASPECT_ENDPOINT` is
priced in. Nothing says which path lands on which counter; the ledger observes the split.

What a reading does:

- a window whose `reset` has passed is pruned; the next reading on that path opens the
  new window's counter;
- a `/rate_limit` reading that reports **nothing used** opens no counter — on a token
  whose counter it cannot see the endpoint answers `0 used` with a fresh `reset` every
  call, and kept literally that is a new "window" a minute; a reading on a path — a
  `200`, or a `304` — always opens one;
- `/rate_limit` (`FREE_PATHS`) is merged as one more sample of whichever counter its
  `reset` names, and counts as no attempt; a counter the endpoint alone reports stands
  at most once per resource;
- the ledger remembers, per path, the last `reset` a reading named, outliving the
  counter — so it knows when it **saw a window open**, and only then a counter carries
  `before_us`: what the window held at its first reading less that reading's own
  request, a measurement of what somebody else spent before this process looked.

A reading's `status` reaches `record()` too, and the ledger asks two questions of every
reading, not one. **Routed** — did a request of this process land here: a response on a
path says where the path is charged, `200` and `304` alike, and that is what `paths`,
`counter_for` and the node's window count are built on; the endpoint's row is not
routed. **Spent** — did GitHub charge for it: a `/rate_limit` read costs nothing by its
endpoint, a `304` nothing by its answer (GitHub does not bill an authorized conditional
request it answers *not modified*, measured 2026-09-19), and `attempts` is what
`foreign_rate` subtracts, so a free reading counted as spent would make this process
look busier and somebody else quieter. The two stay apart because a `304` that opens a
window read as endpoint-only would be replaced by the next such window under the
one-per-resource rule — the warm run after a rollover opens both `core` windows with a
`304`, and the node would show one window flapping between two (ADR-0011).

What a counter can say: `own_spend`, the requests **this process** has had charged to
the window — its `attempts`, which count only the readings after the first, plus that
first one where it was charged (`first_charged`), because the opening request is already
inside `first_used`; a window `/rate_limit` opened is none of ours;
`foreign_rate()`, the requests an hour spent by something that
is not this process — `used` risen between this process's first and last readings, less
its own attempts, over the interval, never negative, and `None` until the interval is
five minutes long, and it subtracts `attempts` rather than `own_spend` for the same
reason, or the opening request would come off twice;
`foreign_rate_said()`, that rate rounded to tens and only when it is
at least one percent of the limit an hour. What the ledger can answer: the counters of a
resource, tightest first; `counter_for(path)`, the window a path was charged to this
hour; `resource_of(path)`, the resource it was last charged to, remembered beyond the
window; `tightest(outlasting=, resource=)`, the least `remaining` among the windows that
last past a horizon. Units: `graphql` is counted in points, everything else in requests
(`unit_of`).

### 4.1a The conditional-request cache

`GitHubClient` sends `If-None-Match` from a `{url: (etag, payload, link)}` cache the
**check** owns and injects — the client is rebuilt inside every run, so a cache on it
would always be empty — and one cache per check, which is what makes a bare URL key
sound: a check resolves one token, and GitHub's answers differ by credential (ADR-0011).
The `Link` is held because `_attempt` returns `(payload, Link)` and `get_paginated`
walks it. A `304` is answered from the cache **before** `_refusal`, where
`fault_for(304)` would read it as an answer about the repository; it is recorded in the
ledger as a reading of the window the path is charged to that spent nothing of it
(§4.1), and it counts in `reads_made` and in `free_reads`. `/rate_limit` is never held
— its body is a budget reading, and a stale
window fed in as a current one is what the guard reasons from. `sbom_check` is outside
it: `POST /graphql`, no `ETag`. Entries are dropped after two consecutive passes of the
reader that asked — an aspect, or `discovery` — without being touched; a pass the
deadline or the pause budget cut short is not swept at all. There is no byte cap: the
size and the run's spend against the guard's estimate go on the check's `report`, which
is display text and never a status claim.

### 4.1b The run hold

The `actions` aspect holds the newest completed run each line has read, keyed by the
line's object — repository id, workflow id, branch — on the **check**, beside the cache
and for its reasons: across runs, in this process, one credential (ADR-0015). It keeps
the fields a line is rendered from and nothing else of the run, verdicts only, and it is
lost at a restart. An answer speaks about a line when the exact read asked about it or
when it has a row on it; a line no answer spoke about for two finished passes is
forgotten, as a cache entry is. An answer whose newest completed run is older than the
held one, or none, **contradicts** it: the held run is read once by id through the
client — a `404` lets it go, a `200` keeps it, a completed newer attempt replaces it —
and each contradiction is one `INFO` line in the log (§5). The estate reading carries
`runs_held`, a level, and this run's `contradictions` and `holds_let_go`, and the
report says them after the cache's line: `held runs: 12 kept; this run's answers
contradicted them 3 time(s), 1 let go`. The guard does not price the reads by id:
bounded by the lines held, and a repeat is a `304`.

### 4.1c The last answers

`sbom_check` holds, for every repository it asks, the last dependency graph GitHub
answered with and when — keyed by the repository's id, on the **check**, beside the
cache and the run hold and for their reasons (ADR-0008 §7). It keeps what a
reading keeps of a graph, the count and the manifests read, and the time of the answer
to the second. A graph that arrives is the repository's last answer at once, on the
first asking or the second, whatever becomes of the pass; an answer that is not a graph
— `NOT_FOUND` or a refusal on the repository's alias, a `401` or an unthrottled `403`
for the query itself — lets the held one go; an answer the check cannot read, and no
answer, leave it. A repository that ends a pass as *could not ask* stands on its last
answer while that is younger than `sbom_check.max_answer_age`, counted on the wall clock
from the answer's own time, which standing does not move; an answer that old is
forgotten where it is met, and a pass that finished forgets every repository it did not
ask — gone from the scope, or under `sbom_check.ignore`. It is **empty at every start**:
the first run after one has no last answer, and a repository GitHub does not answer for
on it is *could not ask*. Only the measuring half reads it, and what stands reaches the
grading as a reading with a time in it (§3.5, §3.7). Neither the estate reading nor the
report counts it; the log does (§5). The guard does not price the second asking: one
more query for each repository cut.

### 4.2 The `github-rate-limit` node

One node per token, one coded entry per watched resource — `core` and `graphql` unless
`resources:` says otherwise — with `warn_below` (1000) and `error_below` (500) compared
against what is left; the node's code is the worst of its lines, and a resource with a
non-positive limit is `UNDEFINED` and skipped (ADR-0001 §2–§5). Every run reads
`GET /rate_limit`, which is free — the first run of a process also asks `GET /user`
whose token it is, one `core` request, before the budget so the budget includes it:
the body's row for each resource is merged into the
ledger as one more reading, then the line is written **from the ledger** wherever it
holds a counter with numbers for that resource, and from the row itself only where it
holds nothing — pristine on the endpoint, or unreadable there (ADR-0007 §2). The
ledger is read while **measuring**: the run hands back one reading per watched resource
carrying the tightest window's numbers and what the line's clauses say about it — its
reset, and each other window's, as a time under `reset.at` (ADR-0014 §8) — and
the grading writes each line from its reading and the instant it is given, which is
what *resets in* is counted from, and carries the reading onto the line as its `data`
and `subject` (ADR-0014 §6). A reading's object is one account's
budget for one resource, `<login>;resource=<resource>`, with `;host=<host>` off GitHub's
own API; the login is `/user`'s. An installation token, which `/user` refuses and whose
budget is the installation's, gives readings with no subject, and so does a run whose
question GitHub did not answer; the refusal is final for the process, the silence is
asked about again next run. Both reads are asked inside the run's `timeout:`, as the
`github` check's are, so a throttle's wait the run cannot afford is refused rather than
slept, and a run whose budget runs out is a silence too: the account is asked about
again next run, and the budget read is the node's ERROR (ADR-0002 §1).
A reading names no identity: what is left of a budget is read anew on every poll, so
each reading is a new point in its series (ADR-0014 §6).

A line's clauses, in order: the tightest counter's numbers (`2441 of 5000 requests
left, resets in 1min`); what this process spent (`; 269 of it this process's own`,
`own_spend`, a count and not a rate, said whenever it is not zero); what something else
spends (`; ~1250/h of it is spent by
something else using this token`) and, when this process saw the window open, what it
carried first (`, and 45 of it before this process first read this window` beside the
rate, or `; 45 of it were spent by something else before this process first read this
window` alone); then `— as /rate_limit reports it; nothing here has spent it` when no
counter of the resource was charged by this process, or `— the tightest of 2 windows
GitHub keeps for this token; the other has 3932 left, resets in 6min` when there is
more than one — a resource with one window this process charged ends after the numbers
and the spend clauses. A resource GitHub did not report
and a row with a missing `limit` or `remaining` are WARN lines for that resource alone,
unless the ledger holds a counter of it, which is a reading; a failed read is the
node's own ERROR, `could not ask GitHub for the rate limit: …`.

---

## 5. Reading the log

Both checks log under their node path — the client's own lines carry the request path
that produced them, or nothing — and every line below is `INFO` unless it says
otherwise. A clock time in a line is a budget window's end, beside the minutes it has
left: the time of day in the configured timezone, written by the library
(`little_sister.spans.local_time`), so on a machine in any zone it reads on the clock
little-sister stamps the line in. A window's end no time can be written for has the
minutes alone (ADR-0007 §5). The one other time in a line is when GitHub last answered
for a repository, which the library writes whole, in the configured zone and format. A
run of the `github` check reads top to bottom as:

- `run starting — timeout 60s, request timeout 15s, max pause 30s, 8 aspect(s)`: the
  budgets in force, two of them derived;
- `14 repositories in scope for organization example-org: …` and `discovery took 1.2s
  in 3 read(s) — 59s of the run left`; `(public only)` after the account when a personal
  account's private repositories are out of reach;
- the guard, one line per window: `310 API calls left on the window GitHub charges the
  dependabot, code-scanning and actions reads to (resets in 12min), this run needs
  4×57 = 228 there`, `— of which ~12 will be spent by something else during this run`
  when a foreign rate is known; a window that ends before the run `is not priced
  against it`; on the endpoint rung, `4990 API calls left, this run needs 4×57 = 228`
  and `the same response's own headers say core: …`;
- `roster resumes at sbom_check` when the rotation moved the head;
- `aspect 4/8 actions took 6.3s in 41 read(s) — 38s of the run left; GitHub says core:
  3932 of 5000 left, 1068 used, resets in 34min (21:50:07)`, one per aspect: our count
  of the requests and GitHub's `used` on one line, so a budget that moves by something
  else is a column to read down; `no budget headers on that read` when the last request
  got no answer;
- `dependabot/alerts: first reading of the core window, resets in 59min (22:15:00) — 45
  used before this process read it — this process saw it open` (or `— before a restart,
  or by something else`): the first sight of any window with spend already on it, under
  the reduced path that opened it here, and under the node path when the budget check
  opened it;
- `actions: example-org/platform-a · build (5) on main — the answer contradicts the
  held run 103/1: its newest completed run is 101/1 (total_count 57, 1 row, 200); held:
  the run is still there`, one per answer that went back on a held run (§4.1b): the
  answer's `total_count` and rows tell a partial search from a stale one, `200` or `304`
  says whether GitHub or the cache gave it, and the end says what the read by id found
  — `replaced: a newer attempt, …`, `let go: GitHub no longer has the run`, `held: not
  asked, the budget fallback`, `held: not answered, …`;
- `sbom_check: GitHub's time limit cut 2 repositories — 2 asked once more, 1 answered,
  0 left unasked`, one per pass that met a cut: how often the second asking brought an
  answer is this line's two middle numbers, summed over a day, and *left unasked* is
  what the run's time or a throttle kept from being asked again;
- `sbom_check: GitHub did not answer for 3 repositories — 2 on the last answer
  (platform-a since 2026-10-10 11:45:00, platform-b since 2026-10-10 11:38:02), 1 with
  none`, one per pass GitHub did not answer everything on, after the second asking:
  who stands on what it last said and since when, and how many had nothing to stand on
  and are *could not ask* (§4.1c);
- `paused 61s before retrying (GitHub asked; 38s of the run left)` at `WARNING`, or `(our
  backoff after a failure GitHub did not explain; …)`;
- `run cut short after …` at `WARNING`, with which aspect the budget died in, how long
  and how many reads it had, and which aspects were never reached;
- `run ended after 48.1s of its 60s timeout — 213 read(s) in 40.2s, slowest read 6.3s
  (…/actions/workflows/12/runs), 3s paused, 8 of 8 aspects reported`: the receipt.

The `github-rate-limit` check writes one line per run: what it read of each watched
resource — `core: 4990 of 5000 requests left`, and `search: absent` for one it read no
numbers of — then `| that response's own headers: core: 4990 of 5000 left, 10 used,
resets in 12min (21:28:00)`: the bucket GitHub charged that very lookup to, worth
nothing until it disagrees with the body beside it.

---

## 6. Known limits

- **Secondary rate limits are unwatchable.** No header and no endpoint reports them; a
  burst throttle arrives as gray *could not ask GitHub* lines on the `github` check
  beside a `github-rate-limit` node with budget to spare, and that pair is its signature
  (ADR-0001, *Consequences*).
- **`all_branches` is incomplete and says so** whenever the page was a cut — one page of
  the newest hundred runs across every workflow is the only bounded read there is
  (ADR-0005).
- **The ledger forgets at a restart**: the first run prices its reads by the endpoint,
  the node's line is the endpoint's until the first `github` run refills the memory, and
  the *before this process first read this window* clause waits for the first rollover.
- **So do the last answers**: on the first run after a start a repository GitHub does
  not answer for has nothing to stand on, is *could not ask*, and turns `sbom_check`
  amber for that run (§4.1c).
- **While answers stand, GitHub's silence is quiet.** A repository `sbom_check` grades on
  its last answer is in no count, so a stretch GitHub does not answer that aspect for
  shows only as *as of* on the lines there are, and in the log — for at most
  `sbom_check.max_answer_age` (§4.1c).
- **That key's default is an hour at any cadence.** It does not grow with `frequency`,
  so a check that runs hourly or slower has no answer young enough to stand on until
  its config says more (§4.1c).
- **A GitHub App's installation token is not a fit**: it expires after an hour, and
  little-sister resolves a credential once, at startup.
- **A dribbling server** is bounded by the chunked body read against `timeout:`, and by
  nothing shorter — `request_timeout:` bounds a socket operation, not a request.
