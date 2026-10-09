# ADR-0016 — An aspect asks the whole scope, a finding grades, and a workflow is a node

- **Status:** Accepted
- **Date:** 2026-10-10 (§1–§16 accepted 2026-08-15)
- **Related:** [ADR-0002](0002-a-read-failure-is-not-a-finding.md) (a read failure is
  not a finding — the line that grades nothing, and the coverage line its aspect
  writes), [ADR-0005](0005-the-actions-aspect-asks-per-workflow.md) and
  [ADR-0009](0009-named-branches-replace-the-default-branch.md) (the `actions` read, and
  the branches it asks about), [ADR-0006](0006-code-scanning-has-two-scales.md) (the two
  code-scanning aspects), [ADR-0010](0010-a-disabled-workflow-is-a-line-not-a-read.md)
  (the disabled line), [ADR-0012](0012-the-actions-line-carries-what-it-read.md) and
  [ADR-0013](0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md) (what
  an `actions` line carries, and what it is about),
  [ADR-0014](0014-a-run-is-its-readings-and-the-estate-is-its-object.md) (the tree is
  built from the readings alone),
  [ADR-0015](0015-a-workflow-line-holds-the-newest-run-it-has-read.md) (the newest run a
  line holds), little-sister **ADR-0036** (a keyed line is a member), little-sister
  **ADR-0042** (an entry carries its own code), little-sister **ADR-0043** and
  little-sister **ADR-0044** (a coverage reading, and a node's report), little-sister
  **ADR-0050** (a slug is an identifier), little-sister **ADR-0106** (a subject with a
  history has a node of its own), little-sister **ADR-0109** (a node says that its
  children are complete), little-sister **ADR-0118** (a node a run names says so),
  little-sister-aws **ADR-0007** (a level stands only where the configuration names
  several)
- **Register:** [`../decisions.md`](../decisions.md)

A bare ADR number here is this repository's; a reference to one of little-sister's is
always written out, because the two numbering spaces overlap.

## Context

A `github` check reads a whole account — every repository an organization, a team or a
personal account holds — and nothing in its configuration names a repository. It
discovers its scope, and then has to decide what shape to report it in and what each
line of it asserts. The shape and the grading were the port's, written down on
2026-08-15 for the people who receive them: an aspect is one question asked of the whole
scope, and a finding grades while the repository does not. One answer held for every
aspect: a repository is a line, never a node.

`actions` is where that stops holding. Its lines are about objects that have a history —
a workflow on its branch (ADR-0013) — and little-sister gives such a subject a node of
its own, which shows its series and draws its runs (little-sister ADR-0106). The
aspect's node carried a line for each workflow that had something to say, and a workflow
that passed and was idle had none unless a key asked for it, so most of what the check
kept a series of stood nowhere on the wall: one table among many on the check's History
page, which nothing drew. A node for every workflow needs somewhere to stand, and a
repository is what holds the workflows: two repositories have a `ci.yml` each.

This record says what stands of the shape and the grading, beside the decisions that
give `actions` its nodes, so that one record says both.

## Decision

### The tree and the aspects

### 1. The tree is aspect-first

The check's node is a container with one child per aspect (`/<path>/<aspect>`). What an
operator opens this dashboard for is *which domain needs attention across everything we
own* — is a secret committed anywhere, is anything missing a dependency graph — and a
repository-first tree turns that into forty nodes that each have to be opened, with a
per-repository roll-up at the top that nobody acts on. Aspect-first costs the reading in
the other direction — *what is wrong with this one repository* is spread across the
aspects — and that is the deliberate trade.

In seven aspects a repository the aspect flags is a **keyed line** on it, or on one of
its severity bands (§12). In `actions` a repository is a node beneath the aspect, and
its workflows are nodes beneath it (§17–§19): a level inside the aspect, which is its
configuration, built for `actions` alone, and not a node that carries every aspect.
Another aspect gets such a level on the day its grouping is built, off by default, so
that no path moves on an upgrade.

### 2. One aspect is one question, and the endpoint follows the question

| Aspect | The question it answers |
|---|---|
| `secret_scanning_alerts` | is a credential committed anywhere |
| `security_advisories` | which known-vulnerable dependencies are we shipping |
| `code_scanning_security` | which vulnerabilities did static analysis find in the code |
| `actions` | is the build passing |
| `sbom_check` | can the dependencies of this repository be checked at all |
| `code_scanning_quality` | what else did static analysis find |
| `pull_requests` | what is waiting to be reviewed |
| `issues` | what is open against these repositories |

Where a question and an endpoint would disagree, the question wins. GitHub's REST API
counts every pull request as an issue and returns both from its issues endpoint, so
`issues` drops the pull-request rows rather than reporting each open pull request twice;
one code-scanning endpoint answers two questions on two scales, so it is two aspects
(ADR-0006). The number of aspects is a property of the questions, and an aspect added
later arrives switched on (§5).

### 3. Scope is discovered once, and every aspect starts from that one set

One discovery per run — the declared account kind verified against GitHub, then the
organization, team or user repository listing, filtered by `name_prefix`,
`include_archived` and `include_forks` — produces the values every aspect works from. An
aspect may narrow that set (§6, `sbom_check.ignore`, `issues.ignore`), and none widens
it or resolves its own, so a repository under one aspect and not another says something
about that aspect and never about discovery. It also bounds the cost: a run makes at
least one request per repository per aspect, and a discovery per aspect would add a
listing to a budget this check has to estimate before it dares spend it (ADR-0001,
ADR-0007).

`include_forks` defaults to true: a fork is a repository the account holds, and a
package that has never seen the estate is not the party to decide that somebody's forks
do not count. `include_archived` defaults the other way, because an archived repository
is one nobody can act on.

### 4. The check's own node says only how much was looked at

Everything the container writes for itself is about coverage: `14 repositories in
scope`, `WARN` below `expect_min_repos`, the discovered roster as its report — presence
without a status claim (little-sister ADR-0043 and little-sister ADR-0044) — and the two
run-level facts of ADR-0002, the reads that could not be completed and a run cut short
by its deadline. The node takes the worst of its aspects, as any container does; what it
*says* is nothing about a repository's contents. An empty scope is `WARN`, naming the
account, the team and the prefix it looked with, because a check that found nothing to
look at must not read like one that looked and found nothing wrong.

### 5. An aspect is switched off whole, or not at all

`enabled: false` in the aspect's block skips it entirely: no node, none of its calls,
and the guard's estimate shrinks with it. It is not a hidden node, which would still be
in the JSON and still hold pins. The setting is stored as what is off, so an aspect a
later release adds is on in every configuration written before it existed; and a
configuration that switches off every aspect is refused at load, since its node would
look like a check that reports on repositories while reporting nothing about any of
them.

### 6. Three aspects read a narrower scope, and say so where they narrow it

GitHub Advanced Security — code scanning and secret scanning — is free on a public
repository and paid on a private one, so `advanced_security_on_private` is a switch
about visibility, and the account kind only picks its default. With it off, private
repositories drop out of `code_scanning_security`, `code_scanning_quality` and
`secret_scanning_alerts` rather than being reported as *scanning not enabled* on every
private repository forever, and each of those aspects names the repositories it dropped
(little-sister ADR-0044): an aspect that quietly reads half its scope and reports OK is
a monitor that has stopped monitoring, and the check's own coverage reading cannot see
it.

### 7. Where a name is a stored key, a mismatch is recorded rather than repaired

`secret_scanning_alerts` reads the block `secret_scanning:`, named for the GitHub
feature while the node is named for what it reports. Both spellings are keys held
elsewhere — one in every deployment's configuration, the other in every pin and
dashboard on the node's path — so making them agree would break one set to tidy the
other. A name this package has published is not its to improve (PL10).

### The grading

### 8. The finding is what carries a status

The line is what grades. Its key is built from something GitHub minted — the
per-repository number of the pull request, issue or alert, the finding's `html_url`
where there is no number, the workflow and branch for a workflow line, and the aspect
where an aspect reports at most one line per repository — and never from the rendered
text or a line's position (little-sister ADR-0050). The repository half of every key is
its numeric id, the one field a rename does not touch: a name would re-key every line
about a repository its owner renamed, and orphan every pin held on one. The cost is
legibility — `6304-pr-42` reads worse than `platform-a-pr-42` — paid where it belongs,
since a slug is what a machine and a pin hold and the rendered line opens with the name.

The line is a keyed member (little-sister ADR-0036), so an engineer who opens a ticket
for one finding pins that line and the rest keep reporting. The five flat aspects hand
over lines that carry their own code and derive the aspect's as the worst of them
(little-sister ADR-0042); the banded aspects put their lines under bands that carry the
code (§12). A repository has no status of its own anywhere: in `actions` it is a node,
and that node grades nothing of its own (§17).

### 9. Amber is a queue; red is something to do now

| What the check found | Code | Why that one |
|---|---|---|
| an open pull request | `WARN` | review is normal work; a queue nobody drains is the problem |
| an open issue | `WARN` | the same reading, from the other endpoint |
| issues turned off for a repository | `WARN` | the question cannot be answered for it |
| a secret-scanning alert | `ERROR` | a committed credential is live until it is rotated |
| secret scanning not enabled | `ERROR` | §10 |
| no dependency graph | `ERROR` | §11 |
| a failed workflow run | `ERROR` | the build is the one signal that is supposed to be green |
| a workflow run awaiting approval | `WARN` | somebody has to press a button; nothing is broken |
| a dependency advisory, a code-scanning alert | banded | §12 |

Nothing is graded on a repository's importance: a leaked secret in a scratch repository
is red, because the credential does not know which repository it was committed to. These
codes are fixed, and only the banded ones are a setting: a severity is a vendor's word
whose meaning differs by estate, which is what `severity_map` translates, while *an open
pull request* means the same thing everywhere.

### 10. The absence of a watcher grades like what it would have watched for

A repository with secret scanning switched off is `ERROR`, as an open alert is, unless
`secret_scanning.require_enabled` is false. The endpoint answers `404` when the feature
is off, which an alert count cannot tell from *no alerts* — the shape of a false green,
on the repository most likely to be leaking a credential.

### 11. A missing dependency graph is red because it makes another aspect lie

Dependabot has nothing to say about a repository whose dependency graph is empty, so
that repository is green in `security_advisories` for the wrong reason and stays green
through every vulnerability ever published. The missing graph is graded as what it
costs. A repository that genuinely has no dependencies is named in `sbom_check.ignore` —
a deployment's call, which could not have traveled with this package.

### 12. The band grades, the alert does not

The three banded aspects — `security_advisories`, `code_scanning_security` and
`code_scanning_quality` — group their findings under one leaf per severity band, and the
band's status is the whole of what they assert about an alert: its mapped code where it
has findings, `OK` where it has none. One `severity_map` entry regrades every finding at
that severity in one place, and the severity is on the node rather than buried in forty
lines. A repository the aspect was refused is an amber line on the aspect itself, since
it belongs to no severity (ADR-0002).

### 13. A watched band reports while it is empty

A band renders when the grading map names it or the data holds it, so a declared band is
on screen every run, green, with nothing in it: *nothing critical today* stays
distinguishable from *the critical band stopped being produced*. A severity the data
brings and nothing declared gets a `WARN` band where the aspect grades everything it is
handed — loud enough to be seen, not loud enough to wake anybody — until a deployment
that knows what it means names it. `security_advisories` selects before it grades (§14):
a severity outside `severities` reaches no band, and `WARN` is what a selected severity
gets that the map does not name. The cost is a misspelled severity: a band that renders
every run, permanently empty and green.

### 14. `severities` is what is looked at; `severity_map` is what it means

In `security_advisories`, `severities` (default `critical`, `high`) selects what is
reported at all — a severity not selected produces no line and no band — and
`severity_map` grades what was selected, an `OK` keeping its band and its lines on
screen, green. A deployment that wants to see medium advisories without being paged for
them selects them and maps them to `OK`.

### 15. The defaults are strict, and they are a default rather than a verdict

Where the shipped map has an opinion it is the pessimistic one: advisories `critical`
and `high` are `ERROR`, `medium` and `low` `WARN`, and every security-severity band of
code scanning is `ERROR`. The quality bands are the exception ADR-0006 argues — `error`
and `warning` `WARN`, `note` `OK` — because red on this dashboard means act now, and a
lint finding is not that. Strict is the right way for a default to be wrong: one too
loud is found on the first run and turned down in one line, one too quiet by the finding
nobody was shown.

### 16. A workflow's verdict is the last one that said something

An `actions` line's code is the newest run that said something useful: a completed run
that failed or passed, or a run held for approval. A canceled or skipped run does not
erase the verdict beneath it, since it is not an outcome; a run in flight is a flag and
words on the same line, never a replacement for the verdict, so a retry cannot hide the
failure it is trying to fix; and only the states that positively mean work in flight
count as in flight (little-sister ADR-0032 rule 7). Runs of a workflow that no longer
exists are dropped: a deleted workflow's last failure is not a fact about the repository
today.

A workflow can be absent, and it is named here as a known limit rather than a mystery:
where it has not run on the branch it is watched on, where its ten newest runs there
were all canceled or skipped, where it sits past the hundredth workflow of a very large
repository, where `actions.ignore_workflow_name_patterns` matches its name, and where
Actions is switched off for the repository. Where the wide page is read — with
`all_branches`, or where the budget is too thin to ask per workflow (ADR-0005) — a
workflow whose newest run fell outside it has no state, and the aspect's window line
names the repository.

### The `actions` aspect's nodes

### 17. A repository is the box its workflows stand in

`actions` hands back a node for each repository it has something to say about, named by
the repository's name, which holds no `/`. The node grades nothing of its own, as a
region's node does in little-sister-aws (little-sister-aws ADR-0007 §3), unless the
repository could not be read: then its read lines, `<id>-workflows-unreadable` and
`<id>-runs-unreadable`, stand on it under the slugs they have (ADR-0002). It stands only
where it has a workflow's node or a read line of its own; a repository with Actions off,
or with no run on the watched branch, has none.

What is the same everywhere it is true stays on the aspect's node, said once: the
coverage line (`read`, ADR-0002 §5), `runs-window-partial` (ADR-0005) and
`branches-unmatched` (ADR-0009), each one line naming its repositories. Spread over the
repositories' nodes, each would turn every repository it names amber for a fact the
aspect says once; and a read line left on the aspect beside bare repository nodes would
leave the workflows that could not be read standing with nothing above them saying why.

### 18. A workflow's node is named by its file, and titled by its name

A workflow's node is named by the last segment of the `path` GitHub gives it —
`deploy.yml` for `.github/workflows/deploy.yml`, and for a workflow GitHub runs itself,
such as Dependabot's, the last segment of its `dynamic/…` path, `dependabot-updates` —
and titled by its display name, the one a line shows. The file is unique within a
repository, since workflow files sit directly in `.github/workflows`; it holds no `/`;
and it is how GitHub addresses a workflow, in the workflow page's URL and as the API's
`workflow_id`. An edit to `name:` moves nothing; a renamed file is a new node, whose
line keeps its subject and its series, keyed as they are by ids (ADR-0013 §2). Where
GitHub sends no path the workflow's id names the node.

What it costs: a path, a pin, `nodes.yaml` and a log line say the file where the wall
says the name; the record of every `actions` line gains the workflow's `file`, a stored
key whose bytes count against the heaviest record the type declares (ADR-0014 §4); and
an edited name shows on the line at once and on the title only after a restart, since
the library fills a node's title once.

### 19. A branch is a level only where the configuration names several

Where `actions.branches` names two branches or more, and with `actions.all_branches`,
the workflow's node holds a node for each branch it has a line on —
`…/<repository>/deploy.yml/release` — in the subject's own order, the branch last. The
level is counted as little-sister-aws ADR-0007 §1 counts one: by the configuration and
never by what GitHub answers, so the tree changes its shape when the configuration does
and at no other time; with `all_branches` the branches come and go with GitHub's one
page. A branch's node is named by the branch as the line's subject spells it (ADR-0013
§2), each `/` written `:` — git refuses `:` in a branch's name, so nothing is lost and
no two branches meet, and it is the character the subject already joins its parts with —
and titled by the branch where the two differ.

In the default mode, and with one named branch, there is no level: the workflow's node
stands for the workflow on that branch, its line names the branch, and a renamed default
branch moves no path, its history starting again as ADR-0013 accepted.

### 20. A disabled workflow's line is on the workflow's own node

While a workflow is off its line stands on its own node, which then stands for the
workflow — the subject without a branch (ADR-0013) — rather than for the workflow on its
branch. Nothing in its path moves, so a pin on it holds through a switch-off and back.
Where the configuration names several branches the workflow's node says that its
children are complete, so its branch nodes leave while nothing reads them and come back
with its next run, their series kept by ids; where it names one, the history of the
workflow's runs is shown on the check's History page while it is off and the history of
its switches on its node (little-sister ADR-0106), and switching it back on swaps the
two again. Its grading is ADR-0010's.

### 21. Every workflow's line is written, and `actions.show_healthy` is retired

Every workflow with a reading has a node, and its line stands on it whatever it says — a
passing idle workflow's says it passed, and an `OK` disabled line is written as any
other — because the line is what makes the node stand for the workflow and draw its runs
(little-sister ADR-0106); without it the node would stand for nothing, and its runs
would be back on the check's History page. `actions.show_healthy`, which left both out,
is retired: a configuration that still sets it, to either value, is refused at load,
saying why (`architecture.md` §3.6). What a viewer sees of the quiet nodes is the
wall's: *hide ok* and its chips. A workflow that has not run on the watched branch has
no reading, and so no line and no node.

### 22. Each node says what it knows of its children, and that a run names it

The library removes nothing by itself: a node says that its children are complete
(little-sister ADR-0109), and this aspect says it where its run listed every child and
nowhere else. The aspect's node says it of its repositories: it is graded only where its
run read every repository discovery named, which names the scope whole, so a repository
that left the scope, was archived or had Actions switched off leaves with that run. A
repository's node says it of its workflows where its answer was whole — read, and not
short — so a workflow deleted, or one the configuration now ignores, leaves with the
run; a repository that could not be read, or whose answer was a cut, is handed back
saying nothing, and its workflows stay as they were. A workflow's node in a mode without
a level says it, having no children, so that a branch's node an earlier configuration
left beneath it goes; so does a disabled workflow's (§20); and with a level, a
workflow's node says it of its branches where its repository's answer was whole. Nothing
that was kept goes with a node: a repository archived and made active again finds its
runs, the series being keyed by ids.

Every repository's, workflow's and branch's node says that a run names it (little-sister
ADR-0118), on every result handed back for it, that of a repository that could not be
read too. This type declares eight labels by name, `actions` and `issues` among them,
and a repository, a workflow file or a branch called like one of them would otherwise be
shown under that aspect's label.

### 23. Nothing migrates

Every `actions` line moves to a new path, and a pin held on one stops matching at its
old path: it is set again, or it is lost. Nothing is carried over at the first start,
and the release that carries this says so first in its notes.

## Consequences

- **Paths and pins move for `actions`, once.** Every line of the aspect leaves the
  aspect's node for its workflow's — or, where the configuration names several branches,
  its branch's — and its slug stays what it was.
- **The wall holds a node per workflow.** One deployment's estate is 101 workflows in 32
  repositories, and *hide ok* and the chips decide what a viewer sees of the quiet ones.
- **A workflow's node draws its runs**, each as a mark at its own time — the run's
  start, which its record carries as `at` — and as a stem to how long it took where its
  check declares that measure (ADR-0012 §3).
- **In seven aspects the repository is still a line.** An aspect that wants a
  repository's level gets one on the day it is built, off by default (§1).
- **A disagreement with §1–§16 has one record to argue with**, the same one that says
  where `actions` departs from them.

## Alternatives considered

- **A repository-first tree**, every aspect beneath a node per repository. Refused in
  §1: it answers *what is wrong with this repository*, which is not what the dashboard
  is opened for.
- **The records the shape and the grading were first written in, kept and amended by
  dated updates.** Refused: their first decisions — a repository is never a node — are
  what changes, and two records each mostly true beside a third that says where they are
  not are three to read for one answer.
- **The per-repository facts spread over the repositories' nodes.** Refused in §17.
- **The display name as a workflow's node's name**, which needs an escape for the `/` of
  a workflow GitHub names by its path and a rule for two workflows of one name — one
  deployment's kept series hold each once in 101 workflows — **and both names in the
  path**, long and still in need of the escape. Refused in §18.
- **The branch level above the workflows**, where a disabled workflow has no branch to
  stand under; **a level in every mode**, one node beneath every workflow and every path
  moved when a default branch is renamed; and **the branch in the workflow's node's
  name**, a name whose form would change with the configuration. Refused in §19.
- **A disabled workflow's line on the repository's node** with no workflow node while it
  is off, where the workflow is not where it is looked for and the repository would
  stand for it; and **its branch nodes kept frozen beneath it**, stale, or written again
  with a verdict ADR-0010 refused to report. Refused in §20.
- **`show_healthy` kept as `show_when_quiet`** on a passing workflow's node, a viewer's
  choice made in a check's configuration beside the wall's own; and **its meaning kept**
  — a passing workflow without a line, whose node stands for nothing, or without a node,
  whose pin leaves the day it turns green. Refused in §21.
- **A migration of pins at the first start.** Refused in §23.

