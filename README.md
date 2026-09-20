# little-sister-github

GitHub check types for [little-sister](https://github.com/m-31/little-sister).

**`github`** — one node per configured account — an organization, optionally
narrowed to one team, or a personal account — with a child per enabled aspect:
open pull requests, Dependabot advisories, code-scanning and secret-scanning
alerts, dependency-graph presence, workflow runs and open issues.

**`github-rate-limit`** — one node per **token**, with a line per API budget.
Cheap enough to run every minute, and it is what explains a `github` check that
has started skipping its runs.

Every finding is an individually addressable line, so an operator who opens a
ticket for one alert can put **that line** into maintenance and the rest of the
aspect keeps reporting.

This file is the front door: what the package needs, how it is installed and
configured, and what the token must be allowed to read. How the two checks are built
— the client, the run, the budget ledger, what every line on a node and in the log
means — is [`docs/architecture.md`](docs/architecture.md); why they are built that way
is [`docs/decisions.md`](docs/decisions.md) and the records in
[`docs/adr/`](docs/adr/).

## The contract

- **Requires** `little-sister >= 0.3.13` — a floor, never a pin.
- **Runs on** Python **3.11 or newer** — the library's floor, not a higher
  one of its own.
- **Registers** two check types: **`github`** and **`github-rate-limit`**. One
  import registers both.

## Install

```toml
# your deployment's pyproject.toml — both come from the index
[project]
# Pin them. A deployment names exact versions so an upgrade is a deliberate edit
# rather than drift; a plugin is the one that declares a floor, because two plugins
# that each pinned could not be installed together.
dependencies = ["little-sister==0.3.17", "little-sister-github==0.1.8"]
```

```python
# wsgi.py — registrations first, the app last. The order is load-bearing:
# importing little_sister.app builds the engine and loads the check configs, so
# every check type must already be registered. `isort: off` keeps an import
# sorter from quietly reversing that.
# isort: off
import little_sister_github          # noqa: F401  registers both check types
from little_sister.app import app
# isort: on

__all__ = ["app"]                    # without it, lint calls the app import unused
```

## Configure `github`

Copy [`examples/github.yaml`](examples/github.yaml) into your deployment's
`config/checks/`, set `owner`, `kind`, `team` and the token reference, and you are done —
one file per team. The credential is a **reference**, never a value:

```yaml
type: github
path: /platform/github
secrets:
  token: env://PLATFORM_GITHUB_TOKEN
owner: example-org
kind: organization
team: platform
```

What the token needs, read by read, is under *What the token needs* below. Each
team's check carries its own credential, so a second team is a second config file rather
than a code change.

### The settings

The common keys every check has — `path`, `title`, `about`, `frequency`, `timeout`,
`secrets` — are little-sister's; `timeout:` is the **whole run's** budget here (it used
to be spent per request — if you are upgrading, re-read it against your scope). The
example file carries a comment per key; this is the same list with its defaults.

| Key | Default | What it decides |
|---|---|---|
| `owner` | required | the account login — an organization or a person |
| `kind` | required | `organization` or `user`; declared, and verified against GitHub on every run |
| `team` | — | narrows an organization to one team, by slug or name; refused with `kind: user` |
| `name_prefix` | — | keeps only repositories whose name starts with it |
| `include_archived` | `false` | archived repositories are ones nobody can act on |
| `include_forks` | `true` | a fork the account holds is a repository, and its pull requests are real |
| `api_url` | `https://api.github.com` | a GitHub Enterprise Server's `…/api/v3`; its GraphQL endpoint is found beside it |
| `advanced_security_on_private` | `true` for an organization, `false` for a personal account | whether the code-scanning and secret-scanning aspects read **private** repositories — a question about visibility, since Advanced Security is paid there; the ones they skip are named on the aspect ([ADR-0003](docs/adr/0003-an-aspect-is-one-question-asked-of-the-whole-scope.md), decision 6) |
| `expect_min_repos` | `1` | fewer repositories discovered is WARN on the check's node; cannot be `0` |
| `request_timeout` | `15s` | one request's budget; a request is also clamped to what is left of `timeout` |
| `max_pause` | half of `timeout` | how much of the run may be spent asleep, waiting out a rate limit or on a retry's backoff; must be less than `timeout` |
| `rate_limit_safety_factor` | `4` | the guard: a run is skipped when a budget window it would spend has less than this times the reads landing on it |
| `<aspect>.enabled` | `true` | in the aspect's own block; `secret_scanning:` is the block of `secret_scanning_alerts` |
| `pull_requests.ignore_title_prefixes` | `[]` | open pull requests whose title starts with one are not listed (case-insensitive) |
| `security_advisories.severities` | `[critical, high]` | which Dependabot severities are reported at all |
| `security_advisories.severity_map` | `critical`, `high` → ERROR; `medium`, `low` → WARN | what each reported severity means here |
| `code_scanning_security.severity_map` | every band ERROR | the alerts GitHub gave a security severity |
| `code_scanning_quality.severity_map` | `error`, `warning` → WARN; `note` → OK | everything else, by the rule's own analysis severity |
| `secret_scanning.require_enabled` | `true` | a repository with secret scanning switched off is ERROR |
| `sbom_check.ignore` | `[]` | repositories exempt from the dependency-graph requirement |
| `actions.all_branches` | `false` | watch every branch rather than the default one — the one mode that stays incomplete, and says so |
| `actions.branches` | `[]` | watch these branches **instead of** the default one, each workflow asked about each name; refused together with `all_branches` |
| `actions.disabled_severity_map` | `disabled_manually`, `disabled_inactivity` → WARN; `disabled_fork` → OK | what a switched-off workflow means here; its runs are not read |
| `actions.show_healthy` | `false` | also list workflows that passed and are idle |
| `actions.ignore_workflow_name_patterns` | `[]` | regexes, case-insensitive, applied before anything is spent on a workflow |
| `issues.ignore` | `[]` | repositories exempt from the issues check |
| `subnodes` | — | your own display text per aspect, appended to the shipped one with `{default}` |

### `owner:` may name a person

GitHub has one account namespace with two kinds in it, and `owner:` takes a login
of either kind. **`kind:` says which — `organization` or `user` — and it is
required.** It is declared rather than discovered because every load-time decision
is taken from it, and it is **verified** on every run against `GET /users/{login}`,
because a claim nobody checks decays: GitHub lets a personal account convert to an
organization. A disagreement is a refusal naming both the config's claim and
GitHub's answer, not a wrong scope. `team:` is an organization's, so `kind: user`
with a team is refused before the check ever runs.

**A personal account's private repositories need its own token.**
`/users/{login}/repos` returns public repositories only, however privileged the
token — the private ones are listed by `/user/repos`, and only for the account
the token belongs to. When they are out of reach the check's own reading says
`(public only)`, because a scope smaller than the one configured otherwise reads
exactly like a complete one.

### Switching an aspect off

Every aspect runs unless its own block says `enabled: false`:

```yaml
secret_scanning:
  enabled: false        # switches off the `secret_scanning_alerts` aspect
```

A switched-off aspect is skipped whole — no node, none of its API calls, and the
rate-limit estimate shrinks with it. **No node also means no pin**: a maintenance
pin held against that node, or against a line under it, matches nothing while the
aspect is off. An aspect that says nothing is on, so an aspect a later release
adds arrives switched on in configs written before it existed. Switching every
aspect off is a config error
([ADR-0003](docs/adr/0003-an-aspect-is-one-question-asked-of-the-whole-scope.md),
decisions 5 and 7).

The per-aspect display text ships **with the type** and expands `{owner}` /
`{team}` from the config, so it is not copied per team. Your deployment's own policy — a
remediation deadline, who to notify — goes in that config's `subnodes:` block,
appended to the shipped text with `{default}`. little-sister reads that block itself,
so it works the same way for every branch check type you install.

## What it reads

| Aspect | Endpoint | Grade |
|---|---|---|
| `pull_requests` | `GET /repos/{r}/pulls?state=open` | any open PR (minus `ignore_title_prefixes`) → **WARN** |
| `security_advisories` | `GET /repos/{r}/dependabot/alerts?state=open` | one leaf per selected severity, graded by `security_advisories.severity_map` |
| `code_scanning_security` | `GET /repos/{r}/code-scanning/alerts?state=open` | the alerts GitHub gave a **security** severity: one leaf per `critical` / `high` / `medium` / `low`, graded by `code_scanning_security.severity_map` (all **ERROR** by default) |
| `code_scanning_quality` | *(the same read)* | everything else, by the rule's own **analysis** severity: `error` / `warning` / `note`, graded by `code_scanning_quality.severity_map` (**WARN** / **WARN** / **OK** by default) |
| `secret_scanning_alerts` | `GET /repos/{r}/secret-scanning/alerts?state=open` | any open alert → **ERROR**; scanning disabled → **ERROR** (`secret_scanning.require_enabled`) |
| `sbom_check` | `POST /graphql` — `repository(owner:, name:) { dependencyGraphManifests(first: 10) { totalCount nodes { filename parseable exceedsMaxSize } } }`, one query per repository, one point each | no dependency graph → **ERROR** (`platform-api: no dependency graph (0 manifests)`); manifests none of which could be parsed → **ERROR**, with the cause on the line (`platform-api: 2 manifests, none parseable (package-lock.json exceeds the size limit)`); more than ten manifests is a graph whatever the first ten say (`sbom_check.ignore`; [ADR-0008](docs/adr/0008-the-dependency-graph-is-asked-not-exported.md)) |
| `actions` | `GET /repos/{r}/actions/workflows` + `…/actions/workflows/{id}/runs` per workflow and branch | one coded line per workflow and branch **that has something to say**: the newest useful verdict, plus a newer in-flight run (the default branch, or the branches `actions.branches` names, or every branch with `actions.all_branches`; a passing idle workflow only with `actions.show_healthy`). Asking per workflow is exact — nothing back means that workflow does not run on that branch. `actions.all_branches` and a budget too thin to pay per workflow fall back to one page of `…/actions/runs`, and one WARN line then names the repositories the answer was short about; where the named branches matched no workflow in a repository, another names it and its default branch ([ADR-0005](docs/adr/0005-the-actions-aspect-asks-per-workflow.md), [ADR-0009](docs/adr/0009-named-branches-replace-the-default-branch.md)) |
| `issues` | `GET /repos/{r}/issues?state=open` | any open issue → **WARN** (`issues.ignore`); issues disabled → **WARN** |

Discovery is one call verifying the declared kind — plus, for a personal account,
one asking whose token this is — and then the repository listing, filtered by
`name_prefix`, `include_archived` and `include_forks`. Every aspect starts from that
one set, narrowed only where an aspect says so. The check's own node carries the
discovery coverage reading (`expect_min_repos`) and the repository roster, and rolls
up worst-of its aspects. A severity band's title is a **colored circle** by severity
name — 🔴 `critical`, 🟠 `high`, 🟡 `medium`, 🔵 `low`, the analysis severities on the
same three rungs, ❓ for one this package does not name — and `nodes.yaml` sets a
different title per node path if you want the word. Only stdlib `urllib` is used — the
package has no dependency but little-sister itself.

The table says what each aspect grades. **Why** it grades that way — why the tree
is aspect-first, why a missing dependency graph is red while an open pull request
is amber, and what a severity band asserts while it is empty — is
[ADR-0003](docs/adr/0003-an-aspect-is-one-question-asked-of-the-whole-scope.md) and
[ADR-0004](docs/adr/0004-a-finding-grades-the-repository-does-not.md).

**Which of these you can change, and which you cannot.** The three banded aspects are
graded by settings that exist in order to be overruled — `severity_map` says what a
severity means here, `severities` says which ones are looked at at all. The other
five carry the codes in this table with no knob for them: what a deployment decides
about those is whether the aspect runs (`enabled: false`) and which repositories or
titles it skips.

### What the token needs

The check works with a **classic** personal access token and with a **fine-grained**
one; nothing in it cares which, as long as the token may read what an aspect reads. A
fine-grained token is issued with the organization as its resource owner and granted the
repositories the check should see — every repository the scope will contain, or the
listing is quietly shorter than the organization. Read access is enough everywhere; the
check writes nothing.

| Read | Classic scope | Fine-grained permission |
|---|---|---|
| discovery — `/orgs/{org}/repos`, `/users/{login}/repos` | `repo` (`public_repo` for public repositories only) | *Metadata* (read), which every fine-grained token has |
| discovery with `team:` — `/orgs/{org}/teams`, `…/teams/{team}/repos` | `read:org` | *Members* — an **organization** permission (read) |
| `pull_requests` | `repo` | *Pull requests* (read) |
| `security_advisories` — Dependabot alerts | `security_events` | *Dependabot alerts* (read) |
| `code_scanning_security`, `code_scanning_quality` | `security_events` | *Code scanning alerts* (read) |
| `secret_scanning_alerts` | `repo` or `security_events` | *Secret scanning alerts* (read) |
| `sbom_check` — the dependency graph | `repo` | *Contents* (read) |
| `actions` | `repo` | *Actions* (read) |
| `issues` | `repo` | *Issues* (read) |
| `github-rate-limit` — `GET /rate_limit` | none | none |

A token that may not read something answers `401` or `403`, and the check reports that
as a fact about the repository rather than as a failure of the run; a token that may
not read the **repository list** fails discovery, which the node says in as many words.
A GitHub App's installation token is not a fit today: it expires after an hour, and
little-sister resolves a credential once, at startup.

### When GitHub is the one having a bad day

A read that fails is not automatically a finding about the repository, and this type
tells the two apart by **status**
([ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md)): a 5xx, a dropped
connection or a rate limit — retried once — is a line saying *could not ask GitHub*
that **grades nothing**, while a `404` and a `401` or `403` without a throttle header
are answers, and still grade. Because those quiet lines grade nothing, the coverage
does: an aspect that could not ask about a repository carries one amber line of its
own, `GitHub did not answer for 1 of 40 repositories`, and the check's own node states
the run's total once, with what the run slept, by cause — `paused 61s for a GitHub rate
limit`, `paused 3s retrying after GitHub did not answer`. A wait GitHub asks for is
taken when the run and `max_pause` can afford it and refused whole when they cannot;
when to ask again is your `frequency:`. If GitHub cannot answer *which repositories
exist*, the check says `WARN` and reports nothing else, so every aspect keeps what the
last good run found; a discovery failure GitHub *answered* — an owner or team that is
not there, a token that may not look — is a defect in your configuration and stays
`ERROR`. The fault table, the throttle reading and the two coverage lines are
[`docs/architecture.md`](docs/architecture.md) §2 and §3.

### The run is skipped before it can exhaust the token

A run is skipped, with a WARN naming the window, when a budget window it would spend
cannot afford it: `rate_limit_safety_factor` times the reads that will land on that
window, less what something else is measured to be spending on it. A token has more
than one `core` window, split by request path, and `GET /rate_limit` reports one of
them — for some tokens one nothing spends — so the guard prices each window from the
budget headers of this process's own reads, and reads the endpoint only where it knows
nothing yet ([ADR-0007](docs/adr/0007-the-budget-is-read-where-it-is-spent.md),
decision 3):

```
skipped this run: 310 API calls left on the window GitHub charges the dependabot, code-scanning and actions reads to (resets in 12min), need > 4×57 for 19 repo(s)
```

The `github-rate-limit` check on the same token explains the skipped runs.

## `github-rate-limit` — the API budget

One node per **token**, with a line per API budget. It is a check type of its own
rather than an eighth aspect of `github`, because a rate limit belongs to the
credential and not to an account — the argument is
[ADR-0001](docs/adr/0001-a-second-check-type-in-this-package.md). Reading
`GET /rate_limit` does not count against the budget it reports, so this check can
run every minute beside a `github` check that runs every fifteen, and it keeps
reporting through the runs the `github` check skips for want of budget.

```yaml
type: github-rate-limit
path: /platform/github-rate-limit
frequency: 1m
secrets:
  token: env://PLATFORM_GITHUB_TOKEN   # the same token you want the budget of
warn_below: 1000                       # the default for every resource below
error_below: 500
# resources:                           # leave the key out to watch core + graphql
#   core:
#   search:
#     warn_below: 10                   # a small budget wants both of its own
#     error_below: 3                   # numbers — error_below is the lower one
```

One **line per resource**, each carrying its own verdict, keyed by GitHub's own
resource name — so a maintenance pin held against `core` survives a config that
starts watching `search` next year:

```
core: 2441 of 5000 requests left, resets in 1min; 269 of it this process's own; ~1250/h of it is spent by something else using this token — the tightest of 2 windows GitHub keeps for this token; the other has 3932 left, resets in 6min
graphql: 5000 of 5000 points left, resets in 59min — as /rate_limit reports it; nothing here has spent it
```

The line is written from the budget headers on every response this process has had on
that token, kept in a ledger for the life of the process, with `GET /rate_limit` merged
in as one more reading: the tightest window grades the resource, and the line says how
many windows there are, what the others hold, how much of the window **this process**
itself spent, at what rate something else is spending the token, and — once this process
has seen a window open — what it already carried at the first reading
([ADR-0007](docs/adr/0007-the-budget-is-read-where-it-is-spent.md) decision 2). Its own
spend is a count of what the ledger recorded, not an
estimate, and it is bounded by the ledger's life: it is this process's share since it
first saw that window, so on a token a second instance or a pipeline also spends, the
rest of `used` is what the other two clauses are for. After a restart the line is the endpoint's until the first `github` run
refills the memory. What every clause means, and the readings the check refuses to
fake, is [`docs/architecture.md`](docs/architecture.md) §4.

Grading is on what is left and **only** on that: the reset time is on the line so
you can see a red budget is about to refill, and it does not soften the verdict
([ADR-0001](docs/adr/0001-a-second-check-type-in-this-package.md), decision 4), and
the foreign rate is on the line and not in the grade. `warn_below` and
`error_below` are compared with the **tightest** window, so a deployment that
tuned them against the endpoint's number may see the node go amber sooner — that
is the node telling the truth. The thresholds in force are rendered on the node.

Between runs most of what the `github` check reads has not changed, so it asks
conditionally: it keeps each read's `ETag` and sends `If-None-Match`, and GitHub answers
`304 Not Modified` without charging the primary rate limit — measured, not assumed. The
held answer is returned unchanged, so no reading and no line differs from what a full
read would have said. Nothing is configured, and the check's own page says how many
payloads are held and how many of a run's requests were free
([ADR-0011](docs/adr/0011-conditional-requests-and-the-cache-that-holds-them.md)).

The token needs **no scopes** for this endpoint — but it has to resolve, and it has
to be one GitHub accepts. A reference that resolves to nothing pins the check to a
visible ERROR before it ever runs; a token GitHub rejects is a 401, which this check
reports as *could not ask GitHub for the rate limit*. Neither is a quiet failure.
Copy [`examples/github-rate-limit.yaml`](examples/github-rate-limit.yaml) — one file
per token, not per team.

## Develop

little-sister is declared as a **floor** — the release that promised the surface
this package imports — and it resolves **from the index**, like any other
dependency. There is no `[tool.uv.sources]` table here, and the committed
`uv.lock` is what a release runs against. To work against a local library
checkout, add the redirect and **do not commit it**: uv reads the sources table of
a dependency it resolves from a path or a checkout, so a committed line would
follow this package into every deployment that installs it.

```toml
# pyproject.toml — locally, never committed
[tool.uv.sources]
little-sister = { git = "file:///path/to/little-sister" }
```

Restore `uv.lock` with it. The next `uv run` — the pre-commit gate is one — rewrites
the lock to `source = { directory = … }`, so a redirect kept out of `pyproject.toml`
can still reach a commit through the lock beside it.

```bash
uv sync
uv run ruff check
uv run shellcheck $(git ls-files -- '*.sh' 'hooks/pre-commit')
uv run mypy
uv run mypy --python-version 3.11   # against the floor, not the interpreter you have
uv run pytest -q
# The same gate runs before every commit once the hook is enabled:
git config core.hooksPath hooks
```

A release runs it once more on a real 3.11, because the floor is a promise a type
checker alone cannot keep.

The tests are fixture-based; nothing in this repository calls GitHub.

## License

MIT — see [LICENSE](LICENSE).
