# Changelog

All notable changes to little-sister-github, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/). These notes are for the
deployments that install this package, not a work log.

Treat **a new or renamed `type:` name, a changed slug shape, or a removed config
key as a breaking change** and say so at the top: every deployment with
maintenance pins or dashboards built on those is affected, even though nothing in
the code says so.

## [Unreleased]

## [0.1.11] - 2026-10-10

**`actions` is a tree now, and every line of it moves: a pin on an `actions` line has to
be set again.** Each repository with a workflow that has run on a watched branch is a
node beneath `actions`, named by the repository's name, and each such workflow is a node
beneath it, named by its file — `ci.yml`, or `dependabot-updates` for Dependabot's — and
titled by its name. The workflow's line stands on that node, and the node draws the
workflow's runs. Where `actions.branches` names two branches or more, or with
`actions.all_branches`, each branch is a node beneath its workflow's, with every `/` in
its name written `:` — `release/1.2` is the node `release:1.2`. A line keeps its slug,
but its path is new, so a pin held on it stops matching and is set again; nothing is
carried over, and a `nodes.yaml` entry for a path beneath `actions` is written against
the new paths. **`actions.show_healthy` is retired** and refused at load: every
workflow's line is written, a passing idle one's too. Remove the key. And at
little-sister's default `record_limit` every start now says that this type's declared
record is close to the limit; `record_limit: 2560` quiets it (see *Changed*).

**`sbom_check` no longer turns amber each time GitHub times out on a repository.** A
repository GitHub does not answer for on a run keeps its last answer for up to an hour:
the line it had, with the same code, ending *as of* the time GitHub last answered for
it. A repository GitHub's own time limit cut is asked once more before the aspect ends.
The aspect still turns amber where there is no such answer to keep: on the first run
after a start, since what is remembered is the process's, and for a repository GitHub
has not answered for in an hour. There is one new key, `sbom_check.max_answer_age`,
default `1h`, and nothing to configure to get any of this on a check that runs more
often than hourly. The hour does not grow with `frequency`, so a check that runs hourly
or slower keeps no answer until it writes the key, longer than its `frequency`. Nothing
in `sbom_check` you configure or pin moves, and the library this release needs is still
little-sister 0.3.19.

### Removed

- **`actions.show_healthy`.** Every workflow that has run on a watched branch is a node
  with its line on it, a passing idle workflow's too — the line is what makes the node
  stand for the workflow and draw its runs — and so is a disabled workflow's `OK` line,
  `disabled_fork`'s by default. A configuration that still sets the key, to either
  value, is refused at load, saying why. What a viewer sees of the quiet nodes is the
  dashboard's *hide ok* and its chips
  ([ADR-0016](docs/adr/0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
  §21).
- **The two records the tree and the grading were first written in.** What still stands
  of them is
  [ADR-0016](docs/adr/0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
  §1–§16, beside the decisions that give `actions` its nodes; a link to either finds
  nothing now.

### Added

- **A node for each repository and each workflow beneath `actions`**, and for each
  branch beneath a workflow where several are configured
  ([ADR-0016](docs/adr/0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
  §17–§19). A repository's node grades nothing of its own unless the repository could
  not be read, and then its read line — `<id>-workflows-unreadable` or
  `<id>-runs-unreadable`, under the slug it had — stands on it. The lines about many
  repositories — `read`, `runs-window-partial`, `branches-unmatched` — stay on the
  aspect's node. A disabled workflow's line stands on the workflow's own node, which
  stands for the workflow while it is off. A repository archived or out of scope, one
  whose Actions were switched off, a workflow deleted or ignored, leaves its node with
  the run that no longer names it; a repository GitHub did not answer for keeps its
  workflows' nodes as they were. Every node beneath `actions` says that a run names it,
  so a repository or a workflow called like one of the aspects is not shown under that
  aspect's label.
- **A workflow's node draws how long each run took.** Each run block of an `actions`
  record gains `duration_s`: the whole seconds from GitHub's `run_started_at` to its
  `updated_at` once the run has completed, and `null` while it has not. The type
  declares `completed.duration_s` as a measure in `s`, labeled *Duration*, so each run
  is a stem to it; `measures: {completed.duration_s: null}` in the check's YAML takes it
  away, and the runs are ticks
  ([ADR-0012](docs/adr/0012-the-actions-line-carries-what-it-read.md) §3).
- **The record of every `actions` line carries the workflow's `file`**, which names its
  node, **and its own time, `at`**, which places it in its series: a run line's is the
  start of the run it names, so the node draws each run where it began and not where the
  check first read it, and a disabled line's is when GitHub last changed the workflow,
  its switch-off as a rule. Where GitHub sent no such time, `at` is `null` and the
  record stands where the check first read it
  ([ADR-0012](docs/adr/0012-the-actions-line-carries-what-it-read.md) §3,
  [ADR-0013](docs/adr/0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md)
  §5).
- **A repository GitHub does not answer for keeps its last answer.** `sbom_check`
  remembers, for each repository it asks, the last dependency graph GitHub answered
  with and when. On a run GitHub does not answer for a repository — its own time limit
  on that repository's query, a 5xx, a rate limit — the repository is graded on that
  answer while it is younger than **`sbom_check.max_answer_age`**, a new key of the
  aspect's block, a positive duration, **`1h` by default**. It keeps the line it had,
  on the same slug, so a pin holds, and with the same code; the line ends *as of* the
  time GitHub last answered, in the configured timezone and format —
  `platform-api: no dependency graph (0 manifests) — as of 2026-10-10 11:45:00`. *As
  of* means the run did not read the repository, and what the line says was true then.
  Such a repository counts as read: it has no *could not ask GitHub* line, and it is
  neither in the aspect's `GitHub did not answer for …` line nor in the count on the
  check's own node. So a repository without a dependency graph stays red through the
  runs GitHub does not answer for it, where it used to lose its line, and one with a
  graph stays silent. An hour after GitHub last answered — however often the answer
  stood in between — the repository is *could not ask* again, as before. An answer
  that is not a graph, a refusal or *not found*, ends the hold; one the check cannot
  read does not. The last answers are the process's memory: **the first run after a
  start has none**, and a repository GitHub does not answer for on that run is *could
  not ask*. While answers stand, GitHub not answering this aspect shows on the node
  only as *as of* on the lines there are — a repository with a graph has none — and
  in the log; the key bounds how long that can be. Keep it longer than the check's
  `frequency`: by the next run an answer is about that old, and where none is young
  enough the aspect reads as it did before this release
  ([ADR-0008](docs/adr/0008-the-dependency-graph-is-asked-not-exported.md), decision
  7).
- **A dependency graph's record carries its own time, `at`**: `null` on the run that
  read the graph, and the time GitHub answered — in UTC, to the second — on a graph that
  stands for a run GitHub did not answer. A graph that stands on its last answer weighs
  1635 bytes at its heaviest, where a graph weighed 1605; the heaviest record the type
  declares is an `actions` run's now (see *Changed*)
  ([ADR-0014](docs/adr/0014-a-run-is-its-readings-and-the-estate-is-its-object.md)).
- **A repository GitHub's time limit cut is asked once more**, when every repository
  has been asked. A second asking is one request — not retried, and no wait taken for
  it — and is made only while the run has more than one `request_timeout` left; one
  that GitHub answers with a rate limit is the last of the run. What the second asking
  says is the repository's reading for the run, and one cut again is graded on its
  last answer, as above. The second askings spend the run's time, a `request_timeout`
  each at most, and a query each of the `graphql` budget, which the guard does not
  price
  ([ADR-0008](docs/adr/0008-the-dependency-graph-is-asked-not-exported.md), decision
  8).
- **Two `INFO` lines in the log, each with a fixed phrase to count by.** A pass that
  met a cut writes `sbom_check: GitHub's time limit cut 2 repositories — 2 asked once
  more, 1 answered, 0 left unasked`, and a pass GitHub did not answer everything on
  writes `sbom_check: GitHub did not answer for 3 repositories — 2 on the last answer
  (platform-a since 2026-10-10 11:45:00, platform-b since 2026-10-10 11:38:02), 1 with
  none`. The first says how often the second asking earns its request.

### Changed

- **The heaviest record a `github` check declares is an `actions` run's, 1773 bytes**,
  where it was a dependency graph's 1605: the workflow's file, the run's start as the
  record's own time and how long the run took add to a run's record, as a graph's own
  time adds to a graph's (see *Added*). At little-sister's default `record_limit` of
  2048 that is past the 80 % at which every start says that a declared record is close
  to the limit — a warning in the log and a fact on the engine's report, never a coded
  line. `record_limit: 2560` in `settings.yaml` quiets it, as anything from 2217 does; a
  deployment whose `record_limit` is below 1773 is refused at startup, naming the check
  ([ADR-0014](docs/adr/0014-a-run-is-its-readings-and-the-estate-is-its-object.md) §4).
- **The text shipped for `sbom_check` says what *as of* means** on a line, and names
  the key. A deployment whose `subnodes:` block extends that text with `{default}` gets
  the sentence; one that replaces the `about` keeps its own.

### Fixed

- **GitHub's own time limit on one repository is *could not ask*, not an answer that
  cannot be read.** Where GitHub does not answer in time for one repository's part of a
  query, its answer is a `200` that carries, for that repository, an error whose message
  is `timedout` and which has no type. `sbom_check` read that as *could not read* — an
  amber line naming the repository — and changed its status on such lines 80 times in a
  day and a half on one deployment of twenty repositories. It is now `could not ask
  GitHub (error: timedout)`, a line that grades nothing, counted like any other question
  GitHub did not answer — in the aspect's `GitHub did not answer for …` line and on the
  check's own node — and with the first entry above, a repository GitHub answered for
  within the hour does not show it at all. An error GitHub attaches to one repository
  without a type, with any other message, still reads as *could not read*.
- **The coverage line counts a repository that is gone once.** `sbom_check` could write
  `GitHub did not answer for 1 of 4 repositories` about a scope of three. A repository
  GitHub answers *not found* about between discovery and the query was counted among
  the repositories read and again for its own line. The number of repositories GitHub
  did not answer for was right; the total was one too many for each repository gone,
  and it is now every repository the aspect tried, each once
  ([ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md) §5).

## [0.1.10] - 2026-10-04

**Upgrade the library first:** this release needs little-sister 0.3.19 (see
*Requires*). Nothing you configure or pin moves — no `type:` name, no configuration
key, no slug. What moves is in the log: the clock time a line names a budget window's
end with is the configured timezone's, where it was the machine's (see *Changed*).

### Changed

- **A log line names a budget window's end in the configured timezone.** Every log
  line that names a rate-limit window writes its end as a clock time beside the
  minutes — `resets in 34min (21:50:07)`. That time was the machine's local clock,
  because the stamp at the head of a little-sister log line was. little-sister 0.3.19
  stamps its log in the zone `timezone` names, and the clause follows it: it is the
  time of day in that zone, written by the library, so a line's stamp and the time
  inside it are one clock on a machine in any zone. On a machine whose zone is the
  configured one nothing changes; on any other, the clock times in these lines move
  to the configured zone's at the upgrade, as the log's stamps do. A node's own lines
  keep the minutes alone, as before. A window's end no time can be written for still
  costs its line the clock time and nothing else, and an end past the year 9999 is
  now one of those
  ([ADR-0007](docs/adr/0007-the-budget-is-read-where-it-is-spent.md), little-sister
  ADR-0120).

### Requires

- **little-sister 0.3.19 or newer.** The floor rises from 0.3.18 because that release
  is the first to stamp its log's lines in the configured timezone (little-sister
  ADR-0120), the clock this package's log lines now name a window's end on. Against
  an older library nothing fails, and a line's stamp and the clock time inside it are
  two clocks wherever the machine's zone is not the configured one. Upgrade the
  library first, and read its notes for that release before you do: they say what the
  upgrade asks of a running instance. Nothing you configure changes with this package.

## [0.1.9] - 2026-09-27

**Nothing you configure or pin moves** — no `type:` name, no configuration key, and no
slug but one on a branch name of some 180 characters with non-ASCII letters among them.
What moves is the `subject` an `actions` line carries, which nothing pins, and **one
field of its record: a run block's `updated` is now `updated.at`**, so a line template
or a grading map that reads it must read the new name. See *Changed*.

### Added

- **The two check types run as two halves, on the library's third check API
  epoch.** Each run first reads GitHub and hands back what it read, then says what that
  means from those readings alone, so the same verdict can be reached again later over
  a reading this process did not take
  ([ADR-0014](docs/adr/0014-a-run-is-its-readings-and-the-estate-is-its-object.md),
  little-sister ADR-0086). A `github` run's first reading is about the **estate** the
  check watches, and it names it: the owner, then only the filters your configuration
  set away from their defaults — `example-org;prefix=web`,
  `example-org;team=platform;forks=no`. Two checks watching one account through two
  prefixes are two estates, and say so. A `github-rate-limit` run reads one record per
  watched resource, and each names **whose budget** it is:
  `example-user;resource=core`. A window's reset in it — the tightest one's, and each
  other one's under `others` — is a time, `reset.at`, where GitHub counts seconds.
  Each resource is its own budget at GitHub, so each is its own object — and each line
  carries its reading: the budget as its `subject` and what was read as its `data`,
  the line for a resource GitHub did not report included. With an installation token
  the `data` is still there and the `subject` is empty.
- **Every pull request, alert, issue and dependency-graph line carries what was read
  about it**, and so does each line about a repository that could not be read or is
  gone: the reading's record as the line's `data`, as the `actions` and
  `github-rate-limit` lines already do
  ([ADR-0014](docs/adr/0014-a-run-is-its-readings-and-the-estate-is-its-object.md)).
  No sentence, slug or pin moves. The record shows on the node's own page and in the
  JSON a client reads — a few hundred bytes a finding — and its field names are keys
  from now on, so a line template or a grading map may be written against them. A time
  in it is kept as one: a secret-scanning alert's creation is `created.at`.
- **`github-rate-limit` asks `GET /user` once per process**, on its first run, to learn
  whose token it reads the budget of — one `core` request, where every run before spent
  none, and every later run still spends none; a lookup GitHub throttles for longer
  than the run's `timeout:` is not waited out but asked again next run. A GitHub App
  installation token is not a user and GitHub refuses it there; its readings then name
  no account, because its budget is the installation's and nothing at run time names
  that. No new permission is needed.
- **An `actions` reading names the run attempt it is of**, so a `github` check with
  `series_keep` set keeps each run of a workflow on its branch once, where it kept one
  record per poll: a finished run read again replaces its own record, a re-run is a
  record of its own beside the attempt it retried, and a run in flight rides on the
  record of the run before it until it completes. Each run block of the record gains
  GitHub's `id` for the run and its `attempt`
  ([ADR-0013](docs/adr/0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md)).
  A `github-rate-limit` reading names none: every poll is a new reading of the budget.
- **A switched-off workflow's reading names when the workflow last changed**, so a
  `github` check with `series_keep` set keeps one record per change of it — each time it
  is switched off, and whenever else GitHub updates it — where it kept one per poll for
  as long as it stayed off. Its record gains `updated.at`, GitHub's `updated_at` for
  the workflow, and the reading names that instant in UTC, however GitHub writes it;
  where GitHub sends none, it names the workflow's `state` instead
  ([ADR-0013](docs/adr/0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md)).
  The line's sentence, code and slug do not move.
- **A `github` check declares the heaviest record it writes**, 1605 bytes — a
  dependency graph's, with every field at its longest — so a deployment whose
  `record_limit` is set below that is refused at startup, naming the check, rather
  than failing a run on that reading
  ([ADR-0014](docs/adr/0014-a-run-is-its-readings-and-the-estate-is-its-object.md)).
  At the library's default of 2048 nothing changes, and no start says the record is
  close to the limit.
- **One `INFO` line in the log for every answer that goes back on a held run**, with a
  fixed phrase to count it by — *contradicts the held run* — naming the line's
  repository, workflow and branch, the held run and the answer's newest, the answer's
  `total_count` and the rows it returned, whether GitHub or the conditional cache gave
  it (`200` or `304`), and what reading the run by id found. The `github` check's
  estate reading gains `runs_held`, the runs held when the run ended, and
  `contradictions` and `holds_let_go`, that run's counts; its node's report says them
  after the cache's line
  ([ADR-0015](docs/adr/0015-a-workflow-line-holds-the-newest-run-it-has-read.md)).

### Changed

- **A run block's `updated` is now `updated.at`** — `completed.updated.at` and
  `running.updated.at` in an `actions` line's record — a time, under the name a
  surface that shows a record's times reads as one; it is still not `ended`, since a
  workflow run has no completion time
  ([ADR-0012](docs/adr/0012-the-actions-line-carries-what-it-read.md)). **A line
  template or a grading map that reads `updated` must read `updated.at`.** Every other
  time a record carries is typed the same way from its first release
  ([ADR-0014](docs/adr/0014-a-run-is-its-readings-and-the-estate-is-its-object.md)).
- **An `actions` line is about the workflow on its branch, not the repository.** Its
  `subject` was the repository's numeric id and is now
  `<repository id>:<workflow id>:<branch>` — `1001:42:main` — and a disabled
  workflow's line is `<repository id>:<workflow id>`
  ([ADR-0013](docs/adr/0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md)).
  Nothing is pinned by subject, so no pin moves; a view that grouped lines by it groups
  per workflow now, and groups per repository through the record's new
  `repository_id`. The record also gains `workflow_id`, `aspect` and `kind`; every
  field it had keeps its name but a run block's `updated`, above. A renamed branch
  starts a new history.
- **Free text is clipped once, at 300 characters**, so that what a check read always
  fits the record it is kept in. A pull request title, an alert's summary or rule, a
  secret's type and the error a failed read quotes are shorter on their line than they
  were when they ran past that — the line says exactly the text the record keeps. A
  workflow's name is clipped at 150 bytes: one past 148 ASCII characters, or 12
  emoji, is shorter on its line than it was; a slug is keyed on ids, so no pin moves
  with it. A branch is clipped at 300 bytes wherever a line names it — a workflow's
  line, and the one that says the configured branches matched nothing — so one past
  49 Cyrillic letters or 24 emoji is shorter there than it was, and no plain ASCII
  branch GitHub allows is. A workflow line's slug is still built from the whole
  branch, so no pin moves with it either, but on a branch of some 180 characters that
  is clipped too: its slug is now built from a digest of its name, and a pin held on it
  has to be made again, once. A dependency manifest's path is kept to 70 bytes, from
  its end: past 68 characters, the line that names an unparseable manifest names the
  end of its path behind a `…`, where it named the whole path.
- **An `actions` line is named after its workflow**, as the workflow list names it,
  and after its latest run only where the list gives no name. Dependabot's lines — its
  update jobs and its dependency-graph jobs, whose runs are each named after their job
  — now read `Dependabot Updates` and `Dependency Graph` in the line and in the
  record's `workflow` field, from run to run, where they changed their name with every
  job. No slug moves: a slug is keyed on ids
  ([ADR-0015](docs/adr/0015-a-workflow-line-holds-the-newest-run-it-has-read.md)).

### Fixed

- **An `actions` line no longer goes back to an older run.** GitHub's runs read for a
  workflow on a branch is a search, and it has answered out of order and without its
  newest runs, so a line could show a run older than one it had already read — an
  older failure over a newer pass, and an older success over a newer failure. Each
  answer is now sorted by run id, and the check holds the newest completed run each
  line has read: an answer that goes back on it is checked by reading that run by id,
  and the line lets it go only when GitHub no longer has it. That read is one request
  per such answer, and a repeat is a `304`, which GitHub does not charge. The hold is
  kept in the process, so the first answer after a restart is believed
  ([ADR-0015](docs/adr/0015-a-workflow-line-holds-the-newest-run-it-has-read.md)).
- **A time GitHub sends that is not one no longer fails the whole run.** A workflow
  run whose `run_started_at` had no offset, or was not a time at all, made the library
  refuse the `github` check's whole result, every aspect of the run with it; that time
  is now `null`, and so is any other time GitHub gets wrong
  ([ADR-0014](docs/adr/0014-a-run-is-its-readings-and-the-estate-is-its-object.md)).
- **A window's end the machine's clock cannot hold no longer ends a run.** Every log
  line naming a budget window writes its end as a clock time, and an
  `x-ratelimit-reset` past what the platform's clock holds raised out of that line and
  ended the run of either check type; such a line now says the minutes alone
  ([ADR-0007](docs/adr/0007-the-budget-is-read-where-it-is-spent.md)).
- **`github-rate-limit` no longer waits however long GitHub asks.** Its `timeout:`
  bounded each request and nothing else, so a throttle on the `/rate_limit` read was
  waited out whatever its length. It is now the whole run's budget, as the `github`
  check's is: a wait the run cannot afford is refused, and the node says it could not
  ask GitHub for the rate limit, as it does when GitHub does not answer
  ([ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md)). Each request keeps
  `timeout:` as its limit, and nothing new is configurable.

### Requires

- **little-sister 0.3.18 or newer.** The floor rises from 0.3.17 because that release
  is the first to speak check API epoch 3, where a check type measures and then grades
  (little-sister ADR-0086); this package says `require_api(3)`, and against an older
  library it refuses at import, naming both epochs. Upgrade the library first; no
  configuration changes with it.

## [0.1.8] - 2026-09-20

**Breaking for anyone who pinned a disabled workflow's line**: that line is keyed
without a branch now, so the pin stops matching and has to be made again. Nothing else
moves — no `type:` name, no other slug, no configuration key. See *Changed*.

### Added

- **An `actions` line now carries what the check read, beside the sentence it wrote.**
  Each line says which repository it is **about** — the numeric id, the one a rename
  cannot change — and carries a **record**: the repository's name, the workflow, the
  branch, the verdict, and a block per run with its number, its URL, GitHub's own
  `status` and `conclusion`, and its times. A disabled workflow's line carries the
  repository, the workflow, its URL and GitHub's `state`
  ([ADR-0012](docs/adr/0012-the-actions-line-carries-what-it-read.md)). **Nothing you
  see today moves**: the line's text is unchanged, character for character, and the
  record is invisible until a surface renders it — the node's own page shows it as a
  list of names and values. It is what the library's line templates, its grading seam
  and its per-subject views will be built on, and **the field names are stored keys**
  from now on, like a slug: a deployment's template will address them.

- **The `github` check asks conditionally, and an unchanged read now costs nothing.** It
  keeps each read's `ETag` and sends `If-None-Match` on the next run; GitHub answers `304
  Not Modified` and does not charge an authorized conditional request against the primary
  rate limit. Nothing about what the check reports changes — the held answer is the answer
  the full read gave — and nothing is configured. A second run against an organization
  that has not changed spends a fraction of the requests it used to. The check's own page
  gains a line saying how many payloads are held, how many were dropped, and how many of
  this run's requests were free against what the pre-run guard priced it at; the guard
  itself is unchanged and still prices a run at full cost, which is conservative
  ([ADR-0011](docs/adr/0011-conditional-requests-and-the-cache-that-holds-them.md)).

- **`actions.branches:` watches the branches you name instead of the default one.** Each
  watched workflow is asked about each name, so the read stays exact — nothing back means
  that workflow does not run on that branch — and the guard prices it as one plus workflows
  times branches. The names **replace** the repository's default branch rather than adding
  to it, so write your trunk in the list if you want it watched too; setting this and
  `actions.all_branches` together is refused at startup, since they ask different
  questions. Where none of the named branches matched a repository at all, one WARN line
  says so and names that repository's default branch — the usual cause is an estate that
  spells its trunk `main` in some repositories and `master` in others. No existing key
  changes meaning, and a config without this key reads exactly as before
  ([ADR-0009](docs/adr/0009-named-branches-replace-the-default-branch.md)).

- **A switched-off workflow is reported, and its runs are no longer read.** A workflow
  GitHub reports as disabled had its runs fetched like any other, which bought a verdict
  frozen at the moment it was switched off — an old green that reassured about nothing, or
  an old red no fix could clear. It is now one line naming the cause in GitHub's own
  wording and ending `no runs read`, and the request is saved: one per disabled workflow
  per run. `disabled_manually` and `disabled_inactivity` are WARN, `disabled_fork` is OK
  because GitHub disables scheduled workflows on a fork by default and forks are
  discovered unless you say otherwise — an OK line follows `show_healthy` like a passing
  idle workflow. `actions.disabled_severity_map` grades any of them your way, in the same
  shape as the other severity maps, and a state this package has not met is WARN and is
  said as GitHub spelled it
  ([ADR-0010](docs/adr/0010-a-disabled-workflow-is-a-line-not-a-read.md)).

- **The `github-rate-limit` line says how much of a window this process itself spent** —
  `core: 2441 of 5000 requests left, resets in 1min; 269 of it this process's own; ~1250/h
  of it is spent by something else using this token`. A count of what the ledger recorded
  rather than an estimate, said whenever it is not zero, and it completes the sentence the
  node was already half telling: what is ours, what somebody else is spending, and what the
  window carried before this process first read it. It is this process's share since it
  first saw that window, so on a token shared with a second instance or a pipeline the
  remainder is what the other two clauses describe. No config changes, and no line that did
  not carry this clause has moved
  ([ADR-0007](docs/adr/0007-the-budget-is-read-where-it-is-spent.md)).

- **`docs/architecture.md` says how the two checks are built, and `docs/decisions.md`
  digests the records.** What used to be reconstructible only from the eight records and
  the source — the client's two dialects and three faults, the run's order and its
  deadline, the guard's three rungs, the ledger and every clause of the budget node, the
  keys nobody may rename, and what each line in the log means — is one document that
  names the record beside each rule; the register is one digest per record, held to its
  records by the release check. The README is cut to the front door and gains a table of
  every `github` setting with its default: the fault table, the budget ledger and the
  guard's mechanics moved out of it, and nothing about what a deployment writes changed.

- **The README says what the token needs, read by read** — classic scope and fine-grained
  permission per aspect and for discovery, that read access is enough everywhere, that a
  fine-grained token is issued with the organization as resource owner and granted the
  repositories the scope will contain, and that a GitHub App's installation token is not a
  fit today because little-sister resolves a credential once at startup and such a token
  expires after an hour. One sentence used to say `read:org`, `repo`, `security_events`
  and *dependency-graph read access*; the table replaces it.

- **ADR-0008 records how `sbom_check` will outlive the synchronous SBOM export** GitHub
  removes on `2026-11-13`: the aspect asks GraphQL whether a repository has dependency
  manifests — one query per run, on the `graphql` budget — instead of downloading an SBOM
  per repository, and grades as before with the cause on the line. Built in this release,
  the entry below.

### Changed

- **Re-pin any maintenance pin held on a disabled workflow's line.** A disabled
  workflow's line is now keyed `<repository id>-workflow-<workflow id>`, without a
  branch, because the workflow is off on every branch. It replaces the frozen verdict
  line that was keyed with one, so a pin held against that slug stops matching and has
  to be made again. Nothing else changes key: a running or recently-run workflow keeps
  `<repository id>-workflow-<workflow id>-<branch>`
  ([ADR-0010](docs/adr/0010-a-disabled-workflow-is-a-line-not-a-read.md)).

- **`sbom_check` asks the dependency graph instead of downloading an SBOM.** The aspect
  read `GET /repos/{owner}/{repo}/dependency-graph/sbom` for every repository — the
  synchronous export GitHub removes on `2026-11-13`, and the source of the `Failed to
  generate SBOM: Request timed out` 500s that were most of this aspect's amber. It now
  asks GraphQL's `dependencyGraphManifests`, one query per repository — GitHub ends a
  GraphQL request at ten seconds, a query costs one point whatever it carries, and a
  query for many repositories was as slow as its heaviest — at one point a repository,
  and grades what it always graded: no manifests is no dependency graph and stays
  **ERROR**, now worded `platform-api: no dependency graph (0 manifests)` — **and a
  repository the export read green may read red now**: an SPDX export always carried the
  relationship describing itself, so the old line fired on a `404` alone, and a graph
  with no manifests at all went unseen; list such a repository under `sbom_check.ignore`
  if that is intended, as the record always meant; manifests none
  of which could be parsed is **ERROR** too, new, with the cause on the line —
  `platform-api: 2 manifests, none parseable (package-lock.json exceeds the size
  limit)`; a graph with more than ten manifests has content whatever the first ten say.
  A repository the query is refused for is amber on its own line, one that vanished
  between discovery and the query grades nothing, and a query that fails whole is
  *could not ask* on the coverage line. The aspect name, its `ignore` key and the `sbom`
  slug of its lines are unchanged, so every pin holds; **the `graphql` budget is now
  spent by this package** — a few points a run — the `github-rate-limit` node watches it
  by default, and the `github` guard prices the queries against that window rather than
  one `core` read per repository. `GitHubClient.graphql()` is the seam, a `POST` with
  the same headers, deadline, retry and throttle reading as every REST read; on a GitHub
  Enterprise Server `api_url` ending in `/api/v3`, the endpoint is `/api/graphql`. The
  token needs nothing new. [ADR-0008](docs/adr/0008-the-dependency-graph-is-asked-not-exported.md).

- **The `github-rate-limit` node reads the budget where it is spent, and says how many
  windows there are.** The budget headers on every response the `github` check makes on
  the same token feed a ledger, kept per window for the life of the process, and the
  node's line is written from it: the window with the least left grades the resource,
  and the line says when it is one of several and what the others hold —
  `core: 2441 of 5000 requests left, resets in 1min — the tightest of 2 windows GitHub
  keeps for this token; the other has 3932 left, resets in 6min`. `GET /rate_limit` is
  still read every run and merged in as one more reading; a resource nothing in this
  process spends is its number and says so — `graphql: 5000 of 5000 points left, resets
  in 59min — as /rate_limit reports it; nothing here has spent it` — and a window the
  endpoint reports at its full limit that nothing here spent is not a window on the
  line. When `used` on a window rises faster than this process's own requests, the line
  says at what rate something else is spending the token, with a tilde; and when this
  process saw a window open — the previous window on that path ended and this one
  appeared — it says what the window already carried at the first reading, `45 of it
  were spent by something else before this process first read this window`, which is
  where a consumer that spends before every run of yours, such as an hourly job on the
  same token, shows up. After a restart the second clause waits for the first rollover,
  because until then the number would be your own reads. `dependency_sbom`
  gets the same treatment — graded by its own numbers, a hundred a minute. **The grade
  can move:** `warn_below` and `error_below` are compared with the tightest window, so a
  token whose endpoint reported a pristine counter stops reading green while the
  `github` check beside it spends thousands an hour, and a deployment that tuned its
  thresholds against the endpoint's number may see amber sooner. No slug, `type:` name or
  configuration key moves. ADR-0007, decisions 1 and 2.

- **The `github` check's pre-run guard prices the run per window, and can fire where it
  never could.** It read `GET /rate_limit` and compared one number with `factor ×
  repositories × endpoints`; on two of the three tokens measured that number was 5,000
  every time. It now prices each endpoint's reads against the window this process's own
  reads of it were charged to, `actions` as one plus one read per workflow the last run
  saw, less what something else is measured to be spending on that window over a run's
  length, and skips the run naming the window: `skipped this run: 310 API calls left on
  the window GitHub charges the dependabot, code-scanning and actions reads to (resets in
  12min), need > 4×57 for 19 repo(s)`. A path the ledger has not seen this window is
  priced against the tightest window it knows of the budget that path is charged to,
  and by the endpoint where that budget has no window open; before the first run of a
  process the endpoint is read and the sentence is the old one. A window that resets before the run would be
  through — `dependency_sbom`'s minute, or an hourly window in its last seconds — is
  outside the guard: it cannot lock the next run out, which is what the guard is for,
  and the throttle path already handles a wait that short. `rate_limit_safety_factor`
  keeps its meaning and its name. ADR-0007, decision 3.

- **A pause on the node is named by its cause.** Every transient retry's wait was added
  to one total and rendered as `paused Ns for a GitHub rate limit` — in two weeks of one
  deployment's logs, eighty-six times, every one of them the library's own one-second
  backoff after a 500 from the SBOM endpoint or a connection that did not answer, and
  not one throttle. The node now says `paused 61s for a GitHub rate limit` only when a
  throttle was read — a `retry-after`, or an exhausted primary window — and `paused 3s
  retrying after GitHub did not answer` for the rest, or both. A reason string, not a
  key: nothing stored against this package moves, but a dashboard reader has been told
  *rate limit* for every retry so far. ADR-0007, decision 4.

- **A read that got no answer leaves no budget claim on the trace**, where it used to
  carry the previous response's numbers as its own — the source of two false early
  resets in the logs — and every trace line that names a window names its end as a
  clock time beside the minutes, `resets in 34min (21:50:07)`, so two lines about two
  windows read as two. The node keeps the minutes. ADR-0007, decision 5.

- **ADR-0001 says what the budget turned out to be.** An update on the record names the
  two sentences a deployment's logs measured false — that every call the `github` check
  makes is charged to `core`, and that the pre-run guard reads the budget the run spends —
  and points at [ADR-0007](docs/adr/0007-the-budget-is-read-where-it-is-spent.md), an
  accepted record: GitHub keeps two `core` counters per token, split by request path,
  each with its own hourly window, and `GET /rate_limit` reports one of them — for some
  tokens one that nothing spends. The SBOM read is charged to `dependency_sbom`, a hundred
  a minute. What the `github-rate-limit` node, the guard and the node's pause sentence
  do about it is the record's five decisions, and they are the four entries above that
  cite ADR-0007.

### Requires

- **little-sister 0.3.17 or newer.** The floor rises from 0.3.13 because the `actions`
  lines above carry `Entry.subject` and `Entry.data`, which that release is the first to
  promise. A deployment on an older library cannot install this version; upgrade the
  library first. Nothing else about the floor moves, and no configuration changes with it.

## [0.1.7] - 2026-09-06

### Added

- **The code-scanning split of 0.1.1 has its record**,
  [ADR-0006](docs/adr/0006-code-scanning-has-two-scales.md): why an alert's two
  severities are two aspects, why one read serves both, and why their defaults
  differ. The three code comments that cited "ADR-0005" for it point there now;
  ADR-0005 was always the `actions` record.

### Changed

- **A short run no longer starves the same aspects every time.** The aspect roster
  was walked in a fixed order, so a run that ran out of budget after four of eight
  refreshed those four and never reached the rest — and an aspect nothing refreshes
  keeps its last reading, so it reads as answered rather than as absent. A run now
  resumes after the last aspect that finished, so the tail one run misses is the head
  of the next and every aspect is read once per cycle. The row's order is unchanged
  (it comes from the aspect's rank, not from the run), and one INFO line names the
  aspect a run resumes at. [ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md)
  carries it.

- **The records say what changed under them.** ADR-0002 and the records of the tree
  and of the grading were written for seven aspects and the old `actions` read; each
  now says at its head that there are eight aspects since 0.1.1 and that `actions`
  asks per workflow since 0.1.6, so a reader arriving from a code comment is not sent
  to a count that stopped being true. `examples/github.yaml` prices a run the way
  0.1.6 spends it.

## [0.1.6] - 2026-08-30

### Changed

- **The `actions` aspect now asks GitHub about each workflow separately, and the
  answer is exact.** It read one page of `/actions/runs` — the newest 100 runs across
  *all* of a repository's workflows on the branch — and a workflow whose newest run
  fell outside that page contributed nothing at all. It now reads
  `/actions/workflows/<id>/runs?branch=…` once per workflow, where nothing back means
  that workflow does not run on that branch: an answer rather than a gap. The reasoning
  is in [ADR-0005](docs/adr/0005-the-actions-aspect-asks-per-workflow.md).

  **What you will see:** workflows that were missing from the leaf now appear, with
  their real state. The `runs-window-partial` line disappears for ordinary
  configurations, because the read it described is gone — it now appears only where
  something genuinely still reads one shared page: `actions.all_branches`, a repository
  with more than 100 workflows, or a budget too thin to pay for a read per workflow.
  No node path, slug shape or configuration key changes.

  **What it costs:** one read per repository plus one per workflow, where it was two
  per repository. Bounded by how many workflows a repository has rather than by how
  often they run, and workflows excluded by `actions.ignore_workflow_name_patterns`
  now cost nothing at all — they are filtered before the reads rather than after. The
  pre-run rate-limit estimate cannot price this, because the workflow count is not
  known until the aspect has read the list; it is a floor for this aspect now, and the
  aspect checks the budget GitHub states on its own responses before spending. A
  repository the budget will not cover degrades to the old one-page read and is
  reported as short rather than dropped.

  **`actions.all_branches` is unchanged and stays inexact**, deliberately: the newest
  state per (workflow, branch) is unbounded over branches, so one page is the only
  bounded question there, and that mode keeps saying when the page was a cut.

## [0.1.5] - 2026-08-30

**Breaking for anyone who pinned one of 0.1.4's two new `actions` lines**: both slugs
are gone. See *Removed*.

### Removed

- **The per-workflow `…-workflow-<id>-unread` and per-repository `…-workflows-unread`
  entries added in 0.1.4.** They were wrong, and the *Fixed* entry below says why. A
  maintenance pin held against either slug now points at a line that is never emitted;
  the replacement is one line per leaf with the slug `runs-window-partial`, which is
  what to pin instead if you were pinning these.

### Fixed

- **The `actions` aspect no longer accuses a workflow of being unread when it simply
  does not run on the branch.** 0.1.4 named every workflow that had no run in the page
  it read, whenever that page was a cut. But the cut is a fact about the *repository's*
  run list, and being missing from a branch-filtered page is a fact about *one
  workflow* — and the two reads this aspect makes cannot tell "its newest run fell
  outside the window" from "it never runs on this branch at all". A `pull_request`
  linter, a tag-triggered release job and a `workflow_dispatch` restore job produce
  exactly the same absence, forever. On a real dashboard that turned into dozens of
  standing amber lines accusing workflows that were doing precisely what they are
  meant to do.

  What replaces it says only what the reads support: **one WARN line for the whole
  leaf**, naming the repositories whose answer was short — `not all runs read in 9 of
  16 repositories — a workflow whose newest run falls outside the window has no state
  here and is not reported above: …`. The line still keeps the leaf from rendering
  green while knowingly incomplete, which is what 0.1.4 set out to do; it no longer
  claims to know which workflow is missing, because it does not.

  Which workflow is unread is answerable, and only, by asking per workflow. That is
  the remaining work and it is a change to how the aspect reads, not a line of prose.

## [0.1.4] - 2026-08-30

### Fixed

- **The `actions` aspect no longer reports OK about a workflow it did not read.** It
  reads one page of `/actions/runs` — the newest 100 runs across *all* of a
  repository's workflows on the branch — so on a busy repository a workflow whose
  newest run fell below that cut contributed nothing at all: no line, and nothing in
  the aspect's coverage count. With `actions.show_healthy: false`, which is the
  default, a workflow nobody had read rendered exactly as one that passed, so the leaf
  reported **OK with no entries while workflows were failing**. Found on a running
  deployment against 16 repositories, not in a suite.

  The read is unchanged and still one page; what changes is that the aspect now says
  when that page was a cut. `total_count` on the same response says whether anything
  was left out, and the workflow list the aspect already fetches says which ones — so
  a workflow with no run in the page read now gets its own WARN line naming it, and a
  repository with more workflows than one page carries gets a line saying how many of
  them were read. Neither costs an extra API call.

  **What you will see:** an `actions` node that was green may go amber, on
  repositories busy enough to fill a page. That is the defect surfacing, not a new
  one — the runs it is amber about were already unread. The two new lines are ordinary
  entries and can be pinned like any other; their slugs are
  `<repo-id>-workflow-<workflow-id>-unread` and `<repo-id>-workflows-unread`. No
  existing slug, node path or configuration key changes, and
  `actions.ignore_workflow_name_patterns` applies to the new lines exactly as it does
  to the old ones.

  Reading every run rather than only saying that we did not is the larger change and
  is deliberately not in this one: it needs a client that can paginate an
  object-wrapped list, and a decision about what it costs per repository.

## [0.1.3] - 2026-08-29

### Added

- **Both checks now log the budget GitHub states on its own responses.** Every REST
  answer carries `x-ratelimit-remaining`, `-limit`, `-used`, `-reset` and
  `-resource`, and this package read them only when deciding whether a refusal was a
  throttle. They are on the trace now, at `INFO`, in three places: the `github`
  check's per-aspect line ends with what GitHub says the run has spent, so our count
  of reads and theirs sit in one column; its rate-limit probe logs the headers of the
  **same response** whose body it just reported; and the `github-rate-limit` check's
  line does the same for its own reading. Nothing on any node changes, and no
  configuration changes.

  Why you would want it: `GET /rate_limit` reports a bucket GitHub looks up by
  identity, while the headers report the bucket *the request that just went out* was
  charged to, and `x-ratelimit-resource` names it. Those normally restate each other.
  Where they do not, a `github-rate-limit` node reports an untouched budget while the
  `github` check beside it spends hundreds of calls an hour — a contradiction with no
  third number to settle it, which is what these lines supply. A response that
  carries no budget headers at all is logged as exactly that, since a path to GitHub
  that strips them otherwise reads as a token that spends nothing.

### Fixed

- **`little_sister_github.__version__` reports the installed version again.** It was a
  literal, frozen at `0.1.0` since the first release, so installations of 0.1.1 and
  0.1.2 answered `0.1.0` to anything that asked — a second source of a fact
  `pyproject.toml` already owns. It is read from the installed distribution's metadata
  now, the way little-sister reads its own, so it cannot disagree with the package
  that carries it; in a source tree with no install it reads `0+unknown` rather than a
  number that would be wrong.

## [0.1.2] - 2026-08-23

### Changed

- **Breaking: this package speaks check API epoch 2.** little-sister reads the
  whole `subnodes:` block itself now, for every check type, and a type only
  **declares** what it ships (little-sister ADR-0025). So this check no
  longer parses that block, no longer layers its own defaults, and no longer hands
  a `title` / `about` back on an aspect result: it declares its `SUBNODES` text and
  its `{owner}` / `{team}` / link / grading tokens, and the library resolves and
  applies them. **A deployment's configuration does not change** — the same keys,
  the same `{default}` extension, the same precedence under `nodes.yaml`, the same
  refusal of a key naming something this check does not report and of the retired
  `{org}` token. What a deployment gains is that the block now works for every
  installed branch type rather than for the ones that chose to read it. Installed
  beside an older library this package refuses at startup, naming both epochs.

### Requires

- **`little-sister >= 0.3.13`**, up from 0.3.12: that is the release which speaks
  epoch 2. The previous reasons still hold — this package imports
  `little_sister.transport` and `little_sister.fetch`, which 0.3.11 does not carry,
  and the band glyphs need 0.3.12's rule that a title with no word in it cannot
  stand in for a name.

## [0.1.1] - 2026-08-16

**Breaking, and every existing config is affected: `org:` is now `owner:`, and a
new `kind:` is required.** Both are one-line edits and the check refuses at load
until they are made — it does not start with a guess.

**Breaking: the `code_scanning_alerts` aspect is now two, `code_scanning_security`
and `code_scanning_quality`.** Node paths move, so every maintenance pin and every
dashboard built on `…/code_scanning_alerts/<band>` stops matching, and the
configuration key splits. **The check refuses to load** while a
`code_scanning_alerts:` block is present, naming both halves — it does not start with
a guess, and it does not silently drop your grading.

*What to do:* rename the block to `code_scanning_security:` and move `critical`,
`high`, `medium` and `low` from its `severity_map` there. If you had graded `error`,
`warning` or `note`, those belong in a new `code_scanning_quality:` block — and read
the defaults below before you copy them across, because they changed. An
`enabled: false` goes on whichever half you do not want. Then re-place your pins:
an unmatched pin is **suspended, not deleted**, so the old ones sit in your
maintenance file doing nothing.

*Why:* GitHub gives a code-scanning alert **two** severities and files them under two
headings of its own — *Security* for the alerts whose rule has a security severity,
*Other* for the rest, graded by the rule's own analysis severity. This check read one
field and rendered one eight-band row from it, which had two consequences worth the
break. **Three of those eight bands could never be non-zero**: `error`, `warning` and
`note` were watched, permanently green and permanently unreachable, because nothing
ever classified an alert into them. And **one `severity_map` forced one answer onto
both scales** — the shipped default answered `ERROR` for everything, a `note`
included. Each aspect now declares only the bands it can fill, and carries its own
map and its own default.

*What it costs you at runtime:* **nothing.** Both aspects are built from a single
`/code-scanning/alerts` read per repository, and the pre-run rate estimate counts
distinct endpoints rather than aspects, so an eighth aspect does not reserve an
eighth call that the run never makes.

*New defaults.* `code_scanning_security` grades every band **ERROR** — an alert
GitHub gave a security severity is a vulnerability in your own code, and the mildest
one is still that. `code_scanning_quality` grades `error` and `warning` **WARN** and
`note` **OK**: red on a status dashboard means act now, and a non-security finding is
not that, however CodeQL grades its rule. **If your dashboard was red for lint
findings it will go amber**; if you want the old answer, say so in the new block.

**Breaking, and every existing maintenance pin on a repository line is affected:
the slug shape changed from `<repo-name>-…` to `<repo-id>-…`.** A pin is keyed by
slug, so every pin held on a pull request, issue, alert, workflow or SBOM line stops
matching on upgrade. Nothing is lost loudly and that is the awkward part: an
unmatched pin is **suspended, not deleted**, and a pin with no expiry is never
reaped while its node still exists — so it sits in the deployment's maintenance
file doing nothing, invisibly. **Re-pin what you still want silenced, and clear the
rest.** The reason for the break is in
[ADR-0016](docs/adr/0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
§8: a repository *name* is not something GitHub minted, so a rename used to orphan
those same pins with no upgrade and no warning at all.

### Added

- **A run traces itself at `INFO`, so a run that does not finish can be read
  rather than guessed at.** Roughly a dozen lines per run, on the logger this
  package already uses (`little_sister_github.github`), and every one of them
  answers a question the node deliberately does not:
  - `run starting — timeout 60s, request timeout 15s, max pause 30s, 7 aspect(s)`,
    before anything is spent. Two of those three are **derived** when your config
    does not name them, so they cannot be read off the config file.
  - `discovery took 4.1s in 3 read(s) — 56s of the run left`, because discovery
    spends the same budget the aspects do and no aspect line can show that.
  - `900 API calls left, this run needs 4×14 = 56`, whether or not it stops the
    run: the interesting run is the one that *just* cleared the factor.
  - One line per aspect — `aspect 4/8 actions took 8.4s in 12 read(s) — 31s of the
    run left`. **This is the column that says where a run's budget went.** A run
    that always stops at the same aspect is answered by reading it, and
    `code_scanning_quality` showing `0 read(s)` is the single-payload split above,
    visible for the first time.
  - `run ended after 61.0s of its 60s timeout — 96 read(s) in 58.2s, slowest read
    14.9s (/repos/…/code-scanning/alerts), 0s paused, 5 of 7 aspects reported`. The
    slowest read is what tells *one endpoint sitting at the request timeout* apart
    from *everything is slow*, which are different problems with the same node
    sentence.

- **The cut-short warning now names the aspect the run stopped in and the ones it
  never reached** — `… — code_scanning_quality (6 of 7) was cut off after 12.3s and
  9 read(s); never reached: pull_requests, issues`. The node's sentence is
  unchanged and still counts rather than names; what is new is that a reader no
  longer has to rebuild the roster by hand from the aspect order minus their own
  `disabled_aspects` to find out **which** aspects a starving run is starving.
  - **The order is fixed, so it is the same tail every run.** An aspect that is
    never reached keeps its last reading and stays on the dashboard looking
    answered; the check's own node says the run was cut short, and now the log says
    who paid for it.
  - *Never started* and *cut off partway* are told apart on the line. Both read as
    `0 read(s)` in `0.0s` and they are different findings: the first says the
    aspects **before** it were too slow, the second says **this one** is.

- **`request_timeout:`** — how long **one request** may take, default `15s`, in the
  same spelling `timeout:` takes (`15s`, `2m`, or a bare number of seconds). A
  request is additionally clamped to whatever is left of `timeout:`, so neither
  bound can be talked past.

- **One retry for a transient failure.** A 5xx or a transport failure is asked
  again once, after a second, and only while the run has more than that second left.
  So what *usually* reaches a dashboard is a failure GitHub gave twice — which is
  what makes the quiet line above trustworthy. Not always: a run short of budget
  skips the retry, which is why the node's sentence claims only that GitHub did not
  answer.

- **A second check type: `github-rate-limit`** — what is left of a token's GitHub
  API budget, one coded line per resource. Additive: nothing about the `github`
  type changes, no node path moves, and a deployment that does not want it writes
  no config. The one import line already in your `wsgi.py` registers both types.
  - **It is a type of its own, not an eighth aspect of `github`**, because a rate
    limit belongs to the **token** and not to an account: two `github` checks
    sharing a credential share one budget, and a token also spent by CI has a
    budget this package does not control. It also keeps reporting when the
    `github` check's rate-limit guard *skips a whole run* — which is exactly when
    the number is wanted — and reading `GET /rate_limit` does not count against the
    budget it reports, so it can run every minute beside a check that runs every
    fifteen. The reasoning is
    [ADR-0001](docs/adr/0001-a-second-check-type-in-this-package.md).
  - **`warn_below` / `error_below`** default to 1000 and 500, at the config's top
    level for every resource and inside a resource's own block for that one. They
    are counts, not a fraction of the limit — the number that matters during an
    incident is how many calls are left. `error_below` above `warn_below` is
    refused at load: no budget could ever land in the warning band.
  - **`resources:`** defaults to `core` and `graphql` and takes any key GitHub
    returns; write a resource with nothing under it to take the shared thresholds.
    Names are deliberately **not** validated at load — GitHub's set is open, so an
    allow-list would reject a resource that exists before it rejected a typo. A
    name GitHub does not report becomes a warning line instead of a missing one.
    Leave the key out to take the default set: a `resources:` that is *present and
    empty* is refused, because naming the key replaces the set and YAML cannot tell
    "empty" from "nothing" any other way.
  - Each line is keyed by GitHub's own resource name, so a maintenance pin held
    against `core` survives a config that starts watching `search` later.
  - **Readings it will not fake:** an unreadable endpoint reports that the *asking*
    failed rather than claiming a budget; a watched resource GitHub did not report
    is a warning line naming it; a row this check cannot read is a warning line for
    that resource alone, so one malformed budget never costs the others their
    reading; and a resource whose reported limit is not positive says nothing rather
    than going permanently red on an installation that does not rate-limit at all.
  - The token needs no scopes, but it has to resolve and it has to be one GitHub
    accepts: a reference that resolves to nothing pins the check to a visible ERROR
    before it runs, and a token GitHub rejects is reported as a failed read. What
    nothing catches is a token that authenticates as somebody else — the budget on
    the line is then plausible and belongs to the wrong credential, and the `limit`
    is the only tell.
  - Grading is on what is left and only on that. The reset time is on the line so a
    red budget that is about to refill is visible, but it does not soften the
    verdict.
  - **Two limits are named in the record**
    ([ADR-0001](docs/adr/0001-a-second-check-type-in-this-package.md)). This is the
    **primary** budget: GitHub's *secondary* rate limits — the
    ones a burst trips — are reported by no endpoint and no header, and they arrive
    instead as `could not read` lines on your `github` check while this node stays
    green. An untroubled budget beside a rash of unreadable repositories is that
    signature, not a permission problem. And the `github` check's own pre-run guard
    reads `core` alone, which is the budget its calls are charged to — so its number
    and this check's `core` line are the same reading taken at different moments, and
    may legitimately disagree on a dashboard.

- **`owner:` may name a personal account, not only an organization.** The key
  always meant "the account whose repositories are in scope", but discovery only
  ever read `/orgs/{login}/repos`, which is a 404 for a person: the check reported
  `discovery failed: HTTP 404` and nothing else. A personal account is now
  discovered through its own listing.
  - A personal account's **private** repositories are listed by `/user/repos`
    (with `affiliation=owner`) when the check's own token belongs to that account.
    `/users/{login}/repos` returns public repositories only however privileged the
    token is, so when the private ones are out of reach the check's reading says
    `(public only)` rather than reporting a smaller scope as though it were whole.

- **`kind:` — `organization` or `user` — is required, and verified.** Declared,
  because it is what every load-time decision is taken from; verified against
  `GET /users/{login}` on every run, because a claim nobody checks decays and
  GitHub lets a personal account convert to an organization. A disagreement is a
  refusal naming both the config's claim and GitHub's answer.
  - **`team:` with `kind: user` is a config error**, refused at load rather than
    on the first run: only an organization has teams.

- **`advanced_security_on_private:`** — whether this account has GitHub Advanced
  Security on its private repositories. Code scanning and secret scanning are free
  on a public repository and paid on a private one, **for both kinds of account**,
  so the switch is about visibility and the kind only picks its default: `true`
  for an organization, `false` for a personal account. With it off, private
  repositories drop out of those two aspects entirely — and are **named on the
  aspect's node** — rather than every one of them being reported as "scanning not
  enabled". Every other aspect reads private repositories either way.

- **Any aspect can be switched off** with `enabled: false` in its own block. A
  switched-off aspect is skipped whole: no node, none of its API calls, and the
  rate-limit estimate shrinks with it. An aspect that says nothing is on, so an
  aspect a later release adds arrives switched on in configs written before it
  existed. Switching every aspect off is a config error. The one block not named
  after its aspect is `secret_scanning:`, which switches `secret_scanning_alerts`.
  - **A disabled aspect leaves no node, so any maintenance pin held against that
    node or a line under it is orphaned.** Clear them before switching one off.
  - The check's node names the switched-off aspects in its config summary: without
    it, "that aspect is off" and "the check is broken" look the same on the page an
    operator opens to find out which.

- **A severity band's leaf page says how it is graded.** Each band child now
  carries a `config` card naming the code its findings get and what an empty band
  reads as — `WARN when this band has findings` where no `severity_map` entry
  covers it, and it says so. A band's page previously showed a Time card and
  nothing about the grading that produced what was on it.

- **`max_pause:`** — how much of a run may be spent **asleep** waiting out a GitHub
  rate limit, in the same duration spelling `timeout:` takes. **Default: half of
  `timeout:`**, derived rather than fixed because `timeout:` is the number a
  deployment sizes to its own account.
  - **`timeout:` does not cover this.** A run that sleeps for its entire budget
    never overruns it — it just reports nothing, which is the wedged check a
    throttle is the easiest way to build.
  - A wait the budget cannot afford is **refused whole, not trimmed**: a wait
    shorter than the one GitHub asked for does not satisfy it, so taking it would
    spend the rest of the budget and still be throttled.
  - At the cap the run ends the way an exhausted `timeout:` ends it — the aspects
    that finished are kept and reported, the rest are absent, and the node says
    `run cut short after 4 of 7 aspects reported — a further 45s wait would pass
    its 30s pause budget`. The rejected alternative is worth naming because it is
    what a naive fix does: simply not passing GitHub's `retry-after` on does not
    stop the retry, it drops it to a one-second backoff — so a throttled run with
    thirty-nine repositories left would press a service that had just asked it to
    wait, thirty-nine more times.
  - **A `max_pause:` at or above `timeout:` is refused at load**, on the same
    argument as `error_below` above `warn_below`: the deadline would always end the
    run first, so the cap could never apply and the config summary would state a
    bound that does nothing.

- **A run that had to wait says so on the check's node** —
  `paused 47s for a GitHub rate limit`, beside the coverage and cut-short
  sentences. It grades nothing on its own: waiting when a service asks is correct
  behavior. What it stops is a paused run being indistinguishable from a slow one.

- **The reasoning behind the `github` type now ships, as two records**, which are
  [ADR-0016](docs/adr/0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
  §1–§16 today: why the tree has seven children named after aspects rather than after
  your repositories, what one aspect is, and what the check's own node claims; and
  what a finding asserts and why each grading is the code it is — why a leaked secret
  and a missing dependency graph are both red, what a severity band means while it is
  empty, and the difference between `severities` (what is looked at) and
  `severity_map` (what it means). No behavior changes. If you have ever wanted to
  overrule one of these defaults, that is the record to overrule.

### Changed

- **A read failure is no longer a finding about the repository**
  ([ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md)). GitHub's SBOM
  endpoint 500s often enough that `warn — platform-a: could not read (HTTP 500 …)`
  was routine, and read as a monitoring statement it was wrong: `sbom_check` asks
  *does this repository have a dependency graph?*, and a 500 is not an answer to
  it. What happened is that the check **could not ask**.
  - A **5xx or a transport failure** is now a line that grades nothing — displayed,
    keyed and pinnable, but skipped when the node's code is derived. Your dashboard
    gets quieter here on purpose: a repository nobody could ask about is no longer
    amber.
  - The split is **by status, never by message text**. A **404** still means the
    thing is absent, and **401 / 403** still grade — a token that may not read a
    repository is a true statement about that repository.
  - **The check's own node is where an outage becomes visible**, once:
    `3 repository reads could not be completed this run — GitHub did not answer`. Beside the `expect_min_repos` reading, because both are coverage. On an
    aspect it would be the same sentence on up to seven nodes for one outage.
  - **An aspect that missed something states what it did read** —
    `39 of 40 repositories read`. Only when a repository was *unreachable*, which is
    the case where the quiet lines would otherwise erase the clean ones; a healthy
    aspect is as quiet as before. An aspect that read *nothing* codes itself
    UNDEFINED rather than OK. (The two severity-band aspects are the exception and
    still render green in that case, because a container defers to its bands — the
    check's own node is where it shows.)
  - **`pull_requests` is isolated per repository**, like every other aspect. Its
    whole loop was inside one `try`, so the first repository's failure returned the
    aspect as ERROR and the others' open pull requests were never looked for.
  - Node paths are unchanged and so are the per-repository slugs, so **maintenance
    pins still match** — with one exception: `pull_requests`' failure line was prose,
    so its slug was derived from the wording; it is a keyed `<repo>-unreadable` now,
    and a pin held against the old derived slug does not carry over.

- **A `subnodes:` key that names nothing is refused at load**, where it used to be
  accepted and quietly do nothing. The keys are aspect names; a key that is not one
  is a paragraph a deployment wrote and no page ever draws, which is worse than a
  config error because nothing anywhere says so.
  - **This is why it surfaced now.** The aspect split above renames a key, and a
    config that split its `code_scanning_alerts:` block but left the matching
    `subnodes:` entry alone would have loaded cleanly and silently lost that text.
    Naming the retired aspect there now says what it became and where the paragraph
    should go, rather than only that the key is unknown.
  - Two other keys are worth knowing about: a **band** name (`critical`) is refused,
    because `subnodes:` addresses aspects and not the bands beneath them — set a
    band's title or about per node path in `nodes.yaml`; and `secret_scanning`, which
    is the *configuration block* for the `secret_scanning_alerts` aspect and not the
    aspect's own name.
  - **A switched-off aspect may still carry its text.** The check validates against
    the aspect roster, not against what is enabled, so `enabled: false` does not also
    require deleting the paragraph.

- **A severity band wears a colored circle instead of its own name again.** Every
  band's title was the name back with a capital letter — `critical Critical` — twice
  the width of a chip for no second fact. The rows now read 🔴 🟠 🟡 🔵 beside the
  names.
  - **The color is by name, never by rank**, and this package is the reason. A rank
    here is *your* tuple: `security_advisories` reports the severities
    `dependabot_severities` names, so if you watch `high` and `medium`, `high` is
    first there and second under code scanning. A rank-derived circle would put one
    `high` 🔴 and the other 🟠 on one dashboard. Same severity, same circle.
  - **Two scales share the ramp** — `error` 🟠, `warning` 🟡, `note` 🔵 for the
    analysis severities, matching their security counterparts rung for rung. Since
    the split above they never appear in one row, so nothing repeats where you can
    see it.
  - **A severity this package does not name gets `❓`**, never a borrowed color —
    including the `none` band an alert with no severity of either kind lands in.
  - **The word is not lost.** The name is beside the title on every chip, and the two
    surfaces that draw a title *instead of* a name — the `/copy` hand-off and the
    hover card — now draw both, so a pasted ticket reads `critical 🔴`. That is the
    library's rule (little-sister ADR-0061), and it is why the floor below matters
    for more than an import.
  - **To get the word back**, set `title:` per node path in your deployment's
    `nodes.yaml`. A `subnodes:` block in the check's own config reaches the *aspect*
    nodes here, not the bands beneath them.

- **Both rows are in an order somebody chose.** Siblings sort by name, and a name
  sort destroys the one order a severity scale has. The band row read
  `critical high low medium` — **`low` before `medium`**, which is not a cosmetic
  complaint but a wrong statement about severity — and the aspect row was pure
  alphabet: `actions` first, `sbom_check` wedged between `pull_requests` and
  `secret_scanning_alerts`. Both now declare a rank (little-sister ADR-0055).
  - **The aspect row is now** `secret_scanning_alerts`, `security_advisories`,
    `code_scanning_alerts`, `actions`, `sbom_check`, `pull_requests`, `issues` —
    worst-first by what a finding costs, with the hygiene aspects after the security
    ones. **This is also the order the run asks in**, because it is one constant and
    not a display copy: a run that loses its budget to a slow GitHub now loses the
    cheapest aspects rather than whichever the alphabet put last.
  - **A band row follows its own aspect's declared severities**, not a constant in
    this package — so if you configured `dependabot_severities`, the sequence you
    wrote is the sequence you get. A band only a `severity_map` names ranks after
    those, and a severity that arrived in the data and nobody declared shares one
    rank at the end, sorted by name rather than by whatever order the payload had.
  - **Your `nodes.yaml` still wins**, per node, as it already does for `title` and
    `about`. One thing to know before writing one: `0` is the rank the *unranked*
    carry, so `order: 0` on one child of a ranked row moves it to the **front**, not
    back to its alphabetical place.
  - **The JSON `children` order moves with it** — little-sister ADR-0055 decision 5
    accepts that as a small incompatible change. No node path, no slug and no
    grading changes, so maintenance pins are untouched.

- **The two severity-band aspects now state the grading in force** instead of
  naming the key that sets it. The shipped text said *graded by
  `<aspect>.severity_map`* — which withholds the answer and points a dashboard
  reader at a default written in this package's source, the one place they cannot
  look. The text is expanded per deployment, so a config that overrode the map
  sees its own mapping; when every band grades the same it collapses to
  `**ERROR** for every band` rather than repeating itself eight times.

- **A throttle wait is logged at warning, and says what is left of the run.**
  little-sister logs the wait at info, and a minute of silence with no visible
  line reads exactly like a hang. This check now logs its own line —
  `paused 47s before retrying (312s of the run left)` — so a throttled run is
  visible without turning info on.

- **`timeout:` now bounds the run it always claimed to.** It is documented as the
  per-run budget and was being handed to the socket layer as the *per-request*
  timeout — spent afresh on each of the several hundred requests a run makes, and
  bounding nothing. Nothing else bounded it either: the engine runs a check without
  a deadline of its own. A slow GitHub could therefore hold a run past its own
  frequency indefinitely, and a wedged check reports nothing at all.
  - When the budget runs out, the aspects that finished are **kept and reported**,
    the rest are absent, and the node says
    `run cut short after 63s of its 60s timeout — 4 of 7 aspects reported`.
  - **Check your `timeout:`** if you set it near a single request's length: it is
    now the whole run's, and a run makes at least one request per repository per
    aspect (`actions` makes two) plus pagination.

- **`org:` → `owner:`, a hard cut.** A config still using `org:` is refused with a
  message naming the new key. The old name said something the value need not mean —
  which is how `org: m-31` came to be written for a person — and accepting both
  spellings forever would keep the misleading one alive in every config anybody
  copies. The value does not change.

- **The `{org}` token is `{owner}`.** A `subnodes:` text still writing `{org}` is
  refused at load: an unknown token is left as-is by the library, so it would
  otherwise have reached a dashboard as a literal `{org}`.

- **The organization security overview is no longer linked on a personal
  account.** `github.com/orgs/{login}/security/alerts/…` is an organization page
  and 404s for a person, so the three security aspects' shipped `about` text now
  carries the link as a token (`{advisories_link}`, `{code_scanning_link}`,
  `{secret_scanning_link}`) that expands to nothing there. A deployment's own
  `subnodes:` text may use the tokens.

- **That link no longer writes an empty team clause.** With no `team:` it used to
  render `team:` with nothing after it — a filter that matches nothing rather than
  an absent filter — so the "all open alerts" page opened empty. The three links
  are also now built the same way; one of them spelled the query parameter `q=`
  where the security overview reads `query=`.

- The shipped aspect text says **"the repositories in scope"** where it used to
  say "this team's repositories": there may be no team, and now there may be no
  organization either.

- The check's own node names the account kind — `scope: m-31 (user)` in the config
  summary, `no repositories in scope (user m-31)` in the coverage reading — and
  the discovery log line says which endpoint the scope came from.

- An aspect configuration block that is not a mapping is now a `CheckError` naming
  the key. Four of the seven were read with a bare `.get` and answered a scalar
  with an `AttributeError` naming neither the check nor the key.

- **The request, the budgets and the retry are the library's now**, and only what is
  actually about GitHub stays here — the auth and API-version headers, the `Link`
  page walk, and the throttle reader above. No behavior changes from the move itself;
  the two effects you can see are that requests identify themselves as
  `little-sister/<version>` rather than `Python-urllib/<x.y>`, which is a name a
  support thread can do something with and one an outbound filter is less likely to
  answer with an uninterpretable 403 — and that a response body is now read in bounded
  chunks against the run's clock, which closes the one hole `timeout:` and
  `request_timeout:` both left open: a server dribbling a byte at a time never trips a
  socket timeout, so neither budget stopped it.

- **A read failure can now say three things rather than two.** *We could not ask*,
  *GitHub answered no*, and — new — *GitHub answered with something this check cannot
  read*. The third used to be indistinguishable from a 404 on the line. All three grade
  exactly as before; the wording is what got more specific.

### Fixed

- **An aspect that could not look was showing green.** A repository this check could
  not ask GitHub about produces a line that grades nothing (by design), so an aspect
  that reached *nothing at all* derived `UNDEFINED` — which the roll-up ignores. For
  the two severity-band aspects it was worse: their bands render `OK` when empty, so
  `security_advisories` and `code_scanning_alerts` showed **green** during a total
  outage. Every aspect now carries one amber line of its own when it could not ask —
  `GitHub did not answer for 1 of 40 repositories` — so *could not look* never reads
  as *nothing to report*
  ([ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md) decision 5).
  - **Replaces the `39 of 40 repositories read` line**, which was `OK` and said the
    complement. Same slug (`read`), so a pin on it carries over; new wording and a
    new code.
  - The count is the *could-not-ask* kind only. An aspect whose only trouble was a
    permission error is as quiet as before — that repository already grades amber on
    its own line.

- **A GitHub outage during discovery turned the whole tree red.** Failing to list the
  repositories returned a flat `ERROR`, which is precisely the outage-grades-us that
  this check exists to avoid, and it replaced every aspect's reading with one line. A
  **transient** discovery failure is now `WARN` with no children, which leaves every
  aspect showing what the last good run found. A failure GitHub *answered* — an owner
  or team that is not there, a token that may not look — is still `ERROR`, because it
  is a defect somebody has to fix (ADR-0002 decision 9).

- **A rate limit was being reported as though GitHub had said *no*.** GitHub answers a
  rate limit — primary or secondary — with **403 or 429**, and the failure split read
  anything below 500 as *GitHub answered*. So a throttled request became an amber
  `could not read` line naming a repository that was in perfect health, and it was
  never retried. The reading is *not now* now: it grades nothing, and it is retried
  once, after the wait GitHub itself named
  ([ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md)).
  - **The status cannot tell you**, which is why this lives here and not in the
    library: GitHub documents both 403 and 429 for both of its limits, and a bare 403
    equally means *this token may not see it*. So the headers decide, in GitHub's own
    order — `retry-after`, then `x-ratelimit-remaining: 0` with `x-ratelimit-reset`,
    and otherwise 60 seconds. A **bare 429** is a throttle, because a rate limit is
    the only thing GitHub sends that status for. A **bare 403 is not**, and stays the
    permission answer it always was; reading it as *not now* would retry every
    repository your token cannot see and then paint the real problem gray.
  - **The wait stays inside `timeout:`.** A short secondary limit is absorbed and the
    run carries on. A long primary limit — a reset twenty minutes out — is *not* slept
    through: the request is not retried, the line says how long GitHub asked for, and
    the run reports what it has. When to ask again is the check's schedule, and a
    request is not the place to hold a run past its budget.
  - If you have a dashboard where a burst of activity produced amber lines across
    several repositories at once, **that is what this was** — and those lines are gray
    now, with the count on the check's own node.

- **An answer that is not JSON is no longer retried.** A proxy login page, or an error
  page where a payload was expected, used to be classified as *could not ask* and asked
  again — a second request spent to be handed the same bytes. It now reports what it
  is: an answer this check cannot read, which grades and says so on the line.

### Requires

- **`little-sister >= 0.3.12`**, up from 0.3.11, now for **two** reasons. This package
  imports `little_sister.transport` and `little_sister.fetch`, and 0.3.11 has neither —
  so a floor still naming it would let this install against a library missing every
  name it needs, and would say so only as an `ImportError` at startup.
  - The second reason is quieter and worth stating, because nothing would crash: the
    band glyphs above depend on 0.3.12 knowing that **a title with no word in it cannot
    stand in for a name** (little-sister ADR-0061). On an older library the check would
    run perfectly and every `/copy` would paste a bare 🔴. A floor that only ever
    guards imports would have missed this one.

  The two releases go out together, the library first. Still a **floor, never a pin**,
  and the check API epoch is still **1**: adding names to the surface does not move it.

## [0.1.0] - 2026-08-09

### Added

- **First release: the `github` check type**, extracted from the private
  deployment it grew up in. The behavior is unchanged — same tree, same aspect
  codes, same entry slugs — but the shipped per-aspect prose was rewritten to be
  deployment-neutral: remediation deadlines and internal practices are a
  deployment's policy and are now added with `{default}` (see below) rather than
  shipped. One node per configured team, with a child per aspect:
  open pull requests, Dependabot advisories, code-scanning alerts, secret-scanning
  alerts, SBOM presence, workflow runs and open issues. Discovery is org- or
  team-scoped and filtered by `name_prefix`, `include_archived` and
  `include_forks`.
- Every finding is an **individually addressable line**, slugged from GitHub's own
  per-repository numbering, so one alert can be put into maintenance while the
  rest of its aspect keeps reporting.
- The **per-aspect display text ships with the type** and expands `{org}` /
  `{team}` from the check's own config, so it is not copied per team. A deployment
  replaces a label in its own `subnodes:` block, or extends the shipped text by
  writing `{default}` into its own.
- A **rate-limit guard** (`rate_limit_safety_factor`) skips a run with a WARN
  rather than exhausting the API budget, and a **coverage backstop**
  (`expect_min_repos`) refuses to report a quiet green over an empty scope.
- The credential is a little-sister **secret reference** in the config's
  `secrets:` block, so each team's check carries its own token.

### Requires

- `little-sister >= 0.3.11` — the floor is 0.3.11, not the 0.3.0 that first
  promised the check-authoring surface this package imports: 0.3.0 was tagged but
  never uploaded, so it names no version a resolver can fetch, and it is 0.3.11
  that lowered the library's own Python floor to the one below.
  A **floor, never a pin**. The package declares
  check API epoch **1**; a library that has moved past that surface refuses at
  startup rather than failing as an import error from inside this package.
- **Python 3.11 or newer** — the library's floor, not a higher one of this
  package's own: a plugin that asked for more would mean somebody installs
  little-sister and then cannot install the package they came for. The claim is
  checked rather than declared — the type check runs against 3.11 on every commit
  and the whole suite against a real 3.11 on every release.
