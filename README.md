# little-sister-github

The **`github`** check type for [little-sister](https://github.com/m-31/little-sister):
one node per configured team, with a child per aspect — open pull requests,
Dependabot advisories, code-scanning and secret-scanning alerts, SBOM presence,
workflow runs and open issues.

Every finding is an individually addressable line, so an operator who opens a
ticket for one alert can put **that line** into maintenance and the rest of the
aspect keeps reporting.

## The contract

- **Requires** `little-sister >= 0.3.11` — a floor, never a pin.
- **Runs on** Python **3.11 or newer** — the library's floor, not a higher
  one of its own.
- **Registers** one check type: **`github`**.

## Install

```toml
# your deployment's pyproject.toml
[project]
dependencies = ["little-sister", "little-sister-github"]

# Only while *this* one comes from git: little-sister resolves from the index.
# Delete the table once this package is on an index too — nothing else changes.
[tool.uv.sources]
little-sister-github = { git = "…/little-sister-github.git", tag = "v0.1.0" }
```

```python
# wsgi.py — registrations first, the app last. The order is load-bearing:
# importing little_sister.app builds the engine and loads the check configs, so
# every check type must already be registered. `isort: off` keeps an import
# sorter from quietly reversing that.
# isort: off
import little_sister_github          # noqa: F401  registers the `github` type
from little_sister.app import app
# isort: on

__all__ = ["app"]                    # without it, lint calls the app import unused
```

## Configure

Copy [`examples/github.yaml`](examples/github.yaml) into your deployment's
`config/checks/`, set `org`, `team` and the token reference, and you are done —
one file per team. The credential is a **reference**, never a value:

```yaml
type: github
path: /platform/github
secrets:
  token: env://PLATFORM_GITHUB_TOKEN
org: example-org
team: platform
```

The token needs `read:org`, `repo`, `security_events` and dependency-graph read
access. Each team's check carries its own credential, so a second team is a second
config file rather than a code change.

The per-aspect display text ships **with the type** and expands `{org}` / `{team}`
from the config, so it is not copied per team. Your deployment's own policy — a
remediation deadline, who to notify — goes in that config's `subnodes:` block,
appended to the shipped text with `{default}`.

## What it reads

| Aspect | Endpoint | Grade |
|---|---|---|
| `pull_requests` | `GET /repos/{r}/pulls?state=open` | any open PR (minus `ignore_title_prefixes`) → **WARN** |
| `security_advisories` | `GET /repos/{r}/dependabot/alerts?state=open` | one leaf per selected severity, graded by `security_advisories.severity_map` |
| `code_scanning_alerts` | `GET /repos/{r}/code-scanning/alerts?state=open` | one leaf per severity, graded by `code_scanning_alerts.severity_map` |
| `secret_scanning_alerts` | `GET /repos/{r}/secret-scanning/alerts?state=open` | any open alert → **ERROR**; scanning disabled → **ERROR** (`secret_scanning.require_enabled`) |
| `sbom_check` | `GET /repos/{r}/dependency-graph/sbom` | no dependency graph → **ERROR** (`sbom_check.ignore`) |
| `actions` | `GET /repos/{r}/actions/runs` | coded entries: last completed verdict, plus a newer in-flight run |
| `issues` | `GET /repos/{r}/issues?state=open` | any open issue → **WARN** (`issues.ignore`); issues disabled → **WARN** |

The check's own node carries the discovery coverage reading (`expect_min_repos`)
and the repository roster, and rolls up worst-of its aspects. Only stdlib
`urllib` is used — the package has no dependency but little-sister itself.

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
little-sister = { path = "../little-sister" }
```

Restore `uv.lock` with it. The next `uv run` — the pre-commit gate is one — rewrites
the lock to `source = { directory = … }`, so a redirect kept out of `pyproject.toml`
can still reach a commit through the lock beside it.

```bash
uv sync
uv run ruff check
uv run mypy
uv run mypy --python-version 3.11   # against the floor, not the interpreter you have
uv run pytest -q
# The same gate runs before every commit once the hook is enabled:
git config core.hooksPath hooks
```

The tests are fixture-based; nothing in this repository calls GitHub.

## License

MIT — see [LICENSE](LICENSE).
