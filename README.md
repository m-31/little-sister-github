# little-sister-github

GitHub check types for [little-sister](https://github.com/m-31/little-sister).

**`github`** — one node per configured account — an organization, optionally
narrowed to one team, or a personal account — with a child per enabled aspect:
open pull requests, Dependabot advisories, code-scanning and secret-scanning
alerts, SBOM presence, workflow runs and open issues.

**`github-rate-limit`** — one node per **token**, with a line per API budget.
Cheap enough to run every minute, and it is what explains a `github` check that
has started skipping its runs.

Every finding is an individually addressable line, so an operator who opens a
ticket for one alert can put **that line** into maintenance and the rest of the
aspect keeps reporting.

## The contract

- **Requires** `little-sister >= 0.3.12` — a floor, never a pin.
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
dependencies = ["little-sister==0.3.12", "little-sister-github==0.1.0"]
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

The token needs `read:org`, `repo`, `security_events` and dependency-graph read
access. Each team's check carries its own credential, so a second team is a second
config file rather than a code change.

### `owner:` may name a person

GitHub has one account namespace with two kinds in it, and `owner:` takes a login
of either kind. **`kind:` says which — `organization` or `user` — and it is
required.** It is declared rather than discovered because every load-time decision
is taken from it, and it is **verified** on every run against `GET /users/{login}`,
because a claim nobody checks decays: GitHub lets a personal account convert to an
organization. A disagreement is a refusal naming both the config's claim and
GitHub's answer, not a wrong scope.

What follows from the kind:

- **`team:` is an organization's.** A personal account has no teams, so the
  combination is a config error, refused before the check ever runs.
- **`advanced_security_on_private`** defaults to `true` for an organization and
  `false` for a personal account — see below.
- **A personal account's private repositories need its own token.**
  `/users/{login}/repos` returns public repositories only, however privileged the
  token — the private ones are listed by `/user/repos`, and only for the account
  the token belongs to. When they are out of reach the check's own reading says
  `(public only)`, because a scope smaller than the one configured otherwise reads
  exactly like a complete one.
- The link to the organization security overview is dropped on a personal account
  rather than pointing at a page that 404s.

### Advanced Security is about visibility, not about the kind

Code scanning and secret scanning need GitHub Advanced Security.
`advanced_security_on_private:` says whether this deployment has it on **private**
repositories; the account kind only picks the default. With it off, private
repositories drop out of those two aspects entirely and are **named on the aspect's
node**; the account's public repositories are still read. Every other aspect reads
private repositories either way. Why the switch is drawn on visibility rather than
on the account kind, and why the skipped repositories are named instead of reported
as unscanned, is
[ADR-0003](docs/adr/0003-an-aspect-is-one-question-asked-of-the-whole-scope.md),
decision 6.

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
aspect off is a config error. The one block not named after its aspect is
`secret_scanning:`, which switches `secret_scanning_alerts` — the block is named
for GitHub's feature, the node for what it reports, and both are keys somebody
holds, which is why neither was renamed
([ADR-0003](docs/adr/0003-an-aspect-is-one-question-asked-of-the-whole-scope.md),
decisions 5 and 7).

The per-aspect display text ships **with the type** and expands `{owner}` /
`{team}` from the config, so it is not copied per team. Your deployment's own policy — a
remediation deadline, who to notify — goes in that config's `subnodes:` block,
appended to the shipped text with `{default}`.

## What it reads

A severity band's title is a **colored circle** — 🔴 `critical`, 🟠 `high`, 🟡 `medium`,
🔵 `low`, and the analysis severities `error` / `warning` / `note` on the same three
rungs — with **❓** for a severity this package does not name. The colour is by name, so
the same severity wears the same circle in every aspect and in every deployment; the
band's own name sits beside it, and `nodes.yaml` sets a different title per node path if
you want the word.

| Aspect | Endpoint | Grade |
|---|---|---|
| `pull_requests` | `GET /repos/{r}/pulls?state=open` | any open PR (minus `ignore_title_prefixes`) → **WARN** |
| `security_advisories` | `GET /repos/{r}/dependabot/alerts?state=open` | one leaf per selected severity, graded by `security_advisories.severity_map` |
| `code_scanning_security` | `GET /repos/{r}/code-scanning/alerts?state=open` | the alerts GitHub gave a **security** severity: one leaf per `critical` / `high` / `medium` / `low`, graded by `code_scanning_security.severity_map` (all **ERROR** by default) |
| `code_scanning_quality` | *(the same read)* | everything else, by the rule's own **analysis** severity: `error` / `warning` / `note`, graded by `code_scanning_quality.severity_map` (**WARN** / **WARN** / **OK** by default) |
| `secret_scanning_alerts` | `GET /repos/{r}/secret-scanning/alerts?state=open` | any open alert → **ERROR**; scanning disabled → **ERROR** (`secret_scanning.require_enabled`) |
| `sbom_check` | `GET /repos/{r}/dependency-graph/sbom` | no dependency graph → **ERROR** (`sbom_check.ignore`) |
| `actions` | `GET /repos/{r}/actions/workflows` + `…/actions/runs` | one coded line per workflow and branch **that has something to say**: the newest useful verdict, plus a newer in-flight run (default branch unless `actions.all_branches`; a passing idle workflow only with `actions.show_healthy`) |
| `issues` | `GET /repos/{r}/issues?state=open` | any open issue → **WARN** (`issues.ignore`); issues disabled → **WARN** |

Discovery is one call verifying the declared kind — plus, for a personal account,
one asking whose token this is — and then the repository listing: the
organization's, the account's, or (with `team:`) the org's teams followed by that
team's repositories. It is filtered by `name_prefix`, `include_archived` and
`include_forks`, and every aspect starts from that one set — narrowed only where an
aspect says so: the two Advanced Security aspects drop private repositories when
`advanced_security_on_private` is off, and `sbom_check` and `issues` skip their own
`ignore` lists. The check's own node carries the
discovery coverage reading (`expect_min_repos`) and the repository roster, and rolls
up worst-of its aspects. Only stdlib `urllib` is used — the package has no
dependency but little-sister itself.

The table says what each aspect grades. **Why** it grades that way — why the tree
is aspect-first, why a missing dependency graph is red while an open pull request
is amber, and what a severity band asserts while it is empty — is
[ADR-0003](docs/adr/0003-an-aspect-is-one-question-asked-of-the-whole-scope.md) and
[ADR-0004](docs/adr/0004-a-finding-grades-the-repository-does-not.md).

**Which of these you can change, and which you cannot.** The two alert aspects are
graded by settings that exist in order to be overruled — `severity_map` says what a
severity means here, `severities` says which ones are looked at at all. The other
five carry the codes in this table with no knob for them: what a deployment decides
about those is whether the aspect runs (`enabled: false`) and which repositories or
titles it skips.

### When GitHub is the one having a bad day

A read that fails is not automatically a finding about the repository, and this type
tells the two apart by **status**
([ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md)).

| what came back | what you see |
|---|---|
| **5xx** or a transport failure, twice | a line saying *could not ask GitHub* that **grades nothing** — the repository is not painted amber for GitHub's outage |
| a **rate limit** — 403 or 429 with a throttle header | the same quiet line, and the wait GitHub named is honored if the run can afford it |
| **404** | unchanged: the thing is absent, which is a finding |
| **401 / 403** with no throttle header | unchanged: still grades, because a token that may not read a repository is a fact about that repository |
| an answer this check **cannot read** | a line saying *could not read*, which grades — waiting changes nothing about a payload of the wrong shape |

**A rate limit is *not now*, not *no*.** GitHub answers one with 403 **or** 429, and a
bare 403 also means *this token may not see it* — so the status alone cannot tell the
two apart, and the headers decide: `retry-after`, then `x-ratelimit-remaining: 0` with
`x-ratelimit-reset`, otherwise 60 seconds. A bare 429 counts as a throttle, because
GitHub sends that status for nothing else; a bare 403 does not. **The wait stays inside
`timeout:`** — a short secondary limit is absorbed and the run carries on, while a
reset twenty minutes out is not slept through: the line says how long GitHub asked for,
and the run reports what it has. When to ask again is your `frequency:`.

A transient failure is **retried once** before any of that, so what reaches the
dashboard has usually survived a second ask — usually, because a run short of budget
skips the retry, which is why the sentences below claim only that GitHub did not
answer (ADR-0002, decision 3).

Because those repository lines grade nothing, the **coverage** does, in two places
that say different things. An aspect that could not ask about a repository carries one
amber line of its own — `GitHub did not answer for 1 of 40 repositories` — so an
aspect that could not look is never mistaken for one with nothing to report, whether
it missed one repository or all of them. That line appears **only** when something
could not be asked about, so an aspect whose only trouble was a permission error stays
as quiet as before: that repository is already amber on its own line, and counting it
twice would make the number mean two things. And the check's own node states the run's
total, once: `3 repository reads could not be completed this run`.

Discovery follows the same rule as everything under it. If GitHub cannot answer *which
repositories exist*, the check says `WARN` and reports **nothing else** — which leaves
every aspect showing what the last good run found, rather than replacing a working
dashboard with one red line. A discovery failure GitHub *answered* — an owner or team
that is not there, a token that may not look — is a defect in your configuration and
stays `ERROR`.

### Three budgets, and they are not the same one

- **`timeout:`** is the **whole run's** deadline, and the check honors it. When it
  runs out the aspects that finished are kept, the rest are absent, and the node
  says so. Size it for the scope: a run makes at least one request per repository
  per aspect — `actions` makes two — and pages on top of that.
- **`request_timeout:`** (default `15s`) bounds **one request**, and a request is
  additionally clamped to whatever is left of `timeout:`.
- **`max_pause:`** (default: half of `timeout:`) bounds how much of the run may be
  spent **asleep** waiting out a GitHub rate limit. The deadline cannot say this on
  its own: a run that sleeps for its whole budget never overruns it, and reports
  nothing. A wait the budget cannot afford is refused whole and ends the run like
  an exhausted `timeout:` — what finished is kept, and the node says which wait it
  would have taken. It must be **less** than `timeout:`, or no run could reach it.

`request_timeout:` is what reaches the socket layer, which bounds a socket
*operation* rather than a whole request; `timeout:` is checked between requests and
clamps that socket timeout down to whatever is left of the run. A response body is
read in bounded chunks against `timeout:` as well, which is what closes the last gap
in that pair: a socket timeout alone never fires on a server dribbling one byte at a
time, because every individual read succeeds.

If you are upgrading, re-read your `timeout:` against that first bullet: it used to
be spent per request and now bounds the run.

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
core: 4812 of 5000 requests left, resets in 43min
graphql: 122 of 5000 points left, resets in 12min
```

Readings it will not fake:

- an **unreadable endpoint** says the *asking* failed, rather than claiming a
  budget nobody read;
- a **watched resource GitHub did not report** is a warning line naming it, not a
  missing line;
- a **row this check cannot read** — a missing `limit`, a field in a shape that is
  not a number — is a warning line for *that resource only*;
- a resource whose reported limit is **not positive** says nothing at all rather
  than grading.

Why each of those is the reading it is, is
[ADR-0001](docs/adr/0001-a-second-check-type-in-this-package.md), decision 5.

Grading is on what is left and **only** on that: the reset time is on the line so
you can see a red budget is about to refill, and it does not soften the verdict
([ADR-0001](docs/adr/0001-a-second-check-type-in-this-package.md), decision 4). The
thresholds in force are rendered on the node.

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
