"""The ``github`` check type: a team's repositories, one child per aspect.

Discovers the repositories of a GitHub organization (optionally narrowed to one
team) and reports **one child per aspect** — aspect-first. Most aspect children
are leaves listing the repositories they flag; the two severity-carrying security
aspects split once more into source-severity bands. Ported from a Ruby
overview-check dashboard; the endpoint-to-grade mapping and what was deliberately
changed are in ``docs/migrating-overview-checks.md``.

Each leaf's lines are **keyed entries** rather than plain strings (little-sister
ADR-0036), slugged ``<repo>-<kind>-<number>`` from GitHub's own per-repository
numbering: an engineer who opens a ticket for one finding pins that line and the
rest of the aspect keeps reporting. The parts are identifiers the provider minted,
never the rendered text and never a position (little-sister ADR-0050).

Registered in little-sister's ``CHECK_TYPES`` on import — importing
``little_sister_github`` is the one line a deployment's ``wsgi.py`` adds, before
``little_sister.app``, so the ``github`` type is known when the engine loads the
check configs.

Everything imported from little-sister below is part of its **check-authoring
surface** (architecture.md §11), which is what the ``require_api(1)`` in this
package's ``__init__`` pins.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from little_sister import values
from little_sister.checks import (
    Check,
    CheckError,
    CheckResult,
    Entry,
    coerce_code,
    config_markdown,
    parse_secret_refs,
    parse_subnodes,
    plain,
    register,
    resolve_text,
)
from little_sister.reasons import slug
from little_sister.status import StatusCode

#: This package's own logger. little-sister does not promise its ``logger`` to
#: check authors and does not need to: the library configures the root handlers,
#: so an ordinary module logger's records land in the same place, under a name
#: that says which package emitted them.
logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Source-severity bands, worst first. Dependabot uses the first four; code
# scanning can also return the analysis severities below them. A deployment may
# grade every band independently; the defaults below are deliberately strict, and
# the band shape is what makes overriding one of them a one-line change
# (little-sister ADR-0042).
SECURITY_SEVERITY_ORDER = (
    "critical", "high", "medium", "low", "error", "warning", "note", "none",
)
DEFAULT_ADVISORY_SEVERITY_MAP = {
    "critical": StatusCode.ERROR,
    "high": StatusCode.ERROR,
    "medium": StatusCode.WARN,
    "low": StatusCode.WARN,
}
DEFAULT_CODE_SCANNING_SEVERITY_MAP = dict.fromkeys(
    SECURITY_SEVERITY_ORDER, StatusCode.ERROR)

def _severity_map(value: object, field: str) -> dict[str, StatusCode]:
    """One configured source-severity → dashboard-code mapping."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CheckError(f"github '{field}.severity_map' must be a mapping")
    return {str(severity).lower(): coerce_code(code)
            for severity, code in value.items()}


def _positive_int(value: object, field: str) -> int:
    """A configuration integer that cannot disable a coverage backstop."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CheckError(f"github '{field}' must be an integer of at least 1")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise CheckError(
            f"github '{field}' must be an integer of at least 1") from error
    if parsed < 1:
        raise CheckError(f"github '{field}' must be an integer of at least 1")
    return parsed


#: The sentence every aspect's `about` ends with, referenced as `{pin_note}` so it
#: is written once (little-sister ADR-0025). GitHub numbers each finding per
#: repository, so every line here is separately addressable — which is the part an
#: operator needs to know before they open a ticket for one of twenty.
PIN_NOTE = ("Each line is one finding and can be put into maintenance on its own — "
            "pin the line you are working on and the rest keeps reporting.")


#: Built-in display text for the aspect leaves this check emits (little-sister
#: ADR-0025) — **type-inherent**, so it is written once here rather than copied into
#: every deployment config, which matters as soon as the type runs more than once
#: (one check per team). `{org}` / `{team}` expand from the check's own config
#: (`_subnode_tokens`). A check config's `subnodes:` block
#: replaces any of these, or extends one by writing `{default}` into its own text;
#: `nodes.yaml` still wins over both, per node path.
#:
#: What is written here is what is true of the **type** — what the aspect reads
#: and what the reader is looking at. What an installation *does about it* — a
#: remediation deadline, the day of the week dependency bumps are cleared, who to
#: tell — is a deployment's policy and belongs in its own `subnodes:` block,
#: appended with `{default}`. A promise this file cannot keep for a stranger has
#: no business shipping in the package.
SUBNODES: dict[str, dict[str, str]] = {
    "pull_requests": {
        "title": "Pull requests",
        "about": """\
Open pull requests on the repositories in scope, one line per pull request, with
its author. Pull requests whose title starts with one of
`pull_requests.ignore_title_prefixes` are not listed.

{pin_note}
""",
    },
    "security_advisories": {
        "title": "Dependabot advisories",
        "about": """\
GitHub Security Advisories (Dependabot) found known vulnerabilities in the
dependencies of this team's repositories.
[All open advisories](https://github.com/orgs/{org}/security/alerts/dependabot?q=is:open%20team:{team}).

Findings are grouped into severity-band children; which bands are reported and
how each one is graded is this deployment's call
(`security_advisories.severities` and `.severity_map`).

{pin_note}
""",
    },
    "code_scanning_alerts": {
        "title": "Code-scanning alerts",
        "about": """\
GitHub Code Scanning found potential vulnerabilities in this team's code.
[All open alerts](https://github.com/orgs/{org}/security/alerts/code-scanning?query=is%3Aopen+team%3A{team}+).

Findings are grouped into severity-band children, graded by
`code_scanning_alerts.severity_map`.

{pin_note}
""",
    },
    "secret_scanning_alerts": {
        "title": "Secret-scanning alerts",
        "about": """\
GitHub Secret Scanning found secrets committed to this team's repositories.
[All open alerts](https://github.com/orgs/{org}/security/alerts/secret-scanning?query=team%3A{team}+is%3Aopen).

**Remove the secret, rotate it, and clean the history — take care of this
immediately.** Secret scanning also covers
[non-provider patterns](https://docs.github.com/en/enterprise-cloud@latest/code-security/secret-scanning/introduction/supported-secret-scanning-patterns#non-provider-patterns)
(formerly the "experimental" alerts).

A repository flagged **"secret scanning not enabled"** has the feature turned
off, so nothing is watching it for leaked secrets — enable it under the
repo's Settings → Code security and analysis at "Secret Protection".

{pin_note}
""",
    },
    "sbom_check": {
        "title": "SBOM presence",
        "about": """\
Every repository with code must have a dependency graph (SBOM) so its libraries
can be checked for known issues. A repository listed here has none. Some repos
are exempt — see `sbom_check.ignore` in this check's config.

{pin_note}
""",
    },
    "issues": {
        "title": "Open issues",
        "about": """\
Open issues across the team's repositories, one line per issue. **Pull requests are
excluded**: GitHub's REST API counts every pull request as an issue and returns both
from this endpoint, and open PRs are already reported by *Pull requests*.
Repositories listed under `issues.ignore` are skipped.

{pin_note}
""",
    },
    "actions": {
        "title": "Workflow runs",
        "about": """\
The last completed GitHub Actions verdict per workflow (default branch unless
configured otherwise), with a newer in-flight run shown on the same line: a failed
run → ERROR, a run awaiting approval → WARN. Workflows matching
`actions.ignore_workflow_name_patterns` are skipped.

{pin_note}
""",
    },
}


def _is_pull_request(row: object) -> bool:
    """True for a row the *issues* endpoint returned that is really a pull request.

    GitHub's REST API considers every pull request an issue, so
    ``/repos/{owner}/{repo}/issues`` returns both; only a PR row carries a
    ``pull_request`` object."""
    return isinstance(row, dict) and "pull_request" in row


def _link(text: str, url: str) -> str:
    """A Markdown link, or just the text when there is no URL."""
    return f"[{text}]({url})" if url else text


def _entry_slug(repo: Repo, kind: str, number: int = 0, url: str = "") -> str:
    """The slug for one per-repo finding — ``<repo>-<kind>-<number>``.

    GitHub numbers pull requests, issues and every alert family **per repository**,
    and the number is minted with the finding and retired with it. That is the
    identity little-sister ADR-0036 asks for, and it stays readable in a URL:
    `platform-a-pr-42`.

    A payload with no number falls back to the API's own URL, which is unique per
    finding, and finally to ``<repo>-<kind>`` for the aspects that report at most
    one line per repository (a missing SBOM, secret scanning switched off). What it
    never falls back to is the line's position: an entry above it closing would slide
    the pin onto somebody else's finding.
    """
    if number:
        return slug(repo.name, kind, number)
    return slug(repo.name, kind, url) if url else slug(repo.name, kind)


@dataclass(frozen=True)
class Repo:
    """One repository from discovery — the fields every aspect reads.

    Built once at the seam (:meth:`from_api`) so the aspects work on typed values
    rather than an ``Any`` bag whose wrong types only surface at runtime
    (little-sister ADR-0026). ``name`` / ``full_name`` are **raw**: Markdown escaping
    is a render-time step (``plain``), and an escaped value can no longer be
    interpolated into a URL.
    """

    name: str
    full_name: str
    archived: bool = False
    fork: bool = False
    default_branch: str = ""

    @classmethod
    def from_api(cls, row: object) -> Repo:
        """Read one ``/repos`` row. ``name`` and ``full_name`` are **structural** —
        every aspect addresses the repo by them — so their absence means the payload
        is not what we think and is raised, not defaulted. The rest are display or
        filter fields and default quietly."""
        try:
            return cls(
                name=values.text(row, "name", required=True, where="repository"),
                full_name=values.text(row, "full_name", required=True,
                                      where="repository"),
                archived=values.flag(row, "archived"),
                fork=values.flag(row, "fork"),
                default_branch=values.text(row, "default_branch"))
        except CheckError as error:
            raise GitHubError(f"unexpected repository payload: {error}") from error


class GitHubError(Exception):
    """A GitHub API request failed (``status`` is the HTTP code, when known)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _next_link(link_header: str) -> str | None:
    """The ``rel="next"`` URL from a GitHub ``Link`` header, if present."""
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().lstrip("<").rstrip(">")
        if any(seg.strip() == 'rel="next"' for seg in segments[1:]):
            return url
    return None


class GitHubClient:
    """A minimal GitHub REST client over stdlib ``urllib`` with Link pagination."""

    def __init__(self, token: str, *, api_url: str = GITHUB_API,
                 timeout: float = 30.0) -> None:
        self._token = token
        self._api = api_url.rstrip("/")
        self._timeout = timeout

    def _request(self, path_or_url: str,
                 params: dict[str, Any] | None = None) -> tuple[Any, str]:
        url = (path_or_url if path_or_url.startswith("http")
               else f"{self._api}{path_or_url}")
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
                link = response.headers.get("Link", "")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:200]
            raise GitHubError(f"HTTP {error.code} for {url}: {detail}",
                              status=error.code) from error
        except Exception as error:  # any transport failure
            raise GitHubError(f"request failed for {url}: {error}") from error
        data = json.loads(body) if body else None
        return data, link

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        data, _ = self._request(path, params)
        return data

    def get_paginated(self, path: str,
                      params: dict[str, Any] | None = None) -> list[Any]:
        merged = dict(params or {})
        merged.setdefault("per_page", 100)
        items: list[Any] = []
        data, link = self._request(path, merged)
        while True:
            if not isinstance(data, list):
                raise GitHubError(f"expected a list from {path}")
            items.extend(data)
            nxt = _next_link(link)
            if not nxt:
                return items
            data, link = self._request(nxt)

    def rate_limit(self) -> tuple[int, int, int]:
        core = self.get("/rate_limit")["resources"]["core"]
        return int(core["limit"]), int(core["remaining"]), int(core["reset"])


@register("github")
class GitHubCheck(Check):
    """Discover a team's repositories and report one child per aspect.

    Aspect-first, like the dashboard this was ported from: the check's node
    (``path``) is a container with one child per aspect. Flat aspects list the
    repositories they flag; severity-carrying aspects contain band leaves.
    Discovery is team-scoped and filtered by ``name_prefix`` /
    ``include_archived`` / ``include_forks``.
    """

    #: Aspects this check runs. Used for the rate estimate.
    ASPECTS = ("pull_requests", "security_advisories", "code_scanning_alerts",
               "secret_scanning_alerts", "sbom_check", "actions", "issues")

    def __init__(self, *, org: str, team: str = "", name_prefix: str = "",
                 include_archived: bool = False, include_forks: bool = True,
                 api_url: str = GITHUB_API, rate_limit_safety_factor: int = 4,
                 expect_min_repos: int = 1,
                 pr_ignore_prefixes: tuple[str, ...] = (),
                 dependabot_severities: tuple[str, ...] = ("critical", "high"),
                 advisory_severity_map: dict[str, StatusCode] | None = None,
                 code_scanning_severity_map: dict[str, StatusCode] | None = None,
                 secret_scanning_require_enabled: bool = True,
                 sbom_ignore: tuple[str, ...] = (),
                 actions_ignore_patterns: tuple[re.Pattern[str], ...] = (),
                 actions_all_branches: bool = False,
                 actions_show_healthy: bool = False,
                 issues_ignore: tuple[str, ...] = (),
                 subnodes: dict[str, dict[str, str]] | None = None,
                 token_ref: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # The API token, resolved **once here** from the reference the config
        # names in its `secrets:` block — `env://GITHUB_TOKEN`, or an
        # `aws-sm://…` address (little-sister ADR-0023) — never re-read
        # during a run. An unresolvable reference leaves this empty and records
        # the failure, and the engine pins this check to a visible ERROR without
        # ever calling run(); a malformed one already raised a CheckError.
        self.token = self.resolve_secret(token_ref)
        self.org = org
        self.team = team
        self.name_prefix = name_prefix
        self.include_archived = include_archived
        self.include_forks = include_forks
        self.api_url = api_url
        self.rate_limit_safety_factor = rate_limit_safety_factor
        self.expect_min_repos = _positive_int(expect_min_repos, "expect_min_repos")
        self.pr_ignore_prefixes = pr_ignore_prefixes
        self.dependabot_severities = dependabot_severities
        self.advisory_severity_map = {
            **DEFAULT_ADVISORY_SEVERITY_MAP,
            **(advisory_severity_map or {}),
        }
        self.code_scanning_severity_map = {
            **DEFAULT_CODE_SCANNING_SEVERITY_MAP,
            **(code_scanning_severity_map or {}),
        }
        self.secret_scanning_require_enabled = secret_scanning_require_enabled
        self.sbom_ignore = sbom_ignore
        self.actions_ignore_patterns = actions_ignore_patterns
        self.actions_all_branches = actions_all_branches
        self.actions_show_healthy = actions_show_healthy
        self.issues_ignore = issues_ignore
        # Per-aspect display text (title/about) declared in this check's own config
        # (`subnodes:`), carried onto each aspect child (little-sister
        # ADR-0025). nodes.yaml still overrides per path.
        self.subnodes = subnodes or {}

    @classmethod
    def _extra_from_config(cls, config: dict[str, Any],
                           base_dir: Path) -> dict[str, Any]:
        org = config.get("org")
        if not org:
            raise CheckError("github check requires an 'org'")
        pull_requests = config.get("pull_requests") or {}
        if not isinstance(pull_requests, dict):
            raise CheckError("github 'pull_requests' must be a mapping")
        ignore = pull_requests.get("ignore_title_prefixes") or []
        if not isinstance(ignore, list):
            raise CheckError("pull_requests.ignore_title_prefixes must be a list")
        security = config.get("security_advisories") or {}
        if not isinstance(security, dict):
            raise CheckError("github 'security_advisories' must be a mapping")
        severities = security.get("severities")
        if severities is None:
            severities = ["critical", "high"]
        if not isinstance(severities, list):
            raise CheckError("security_advisories.severities must be a list")
        advisory_severity_map = _severity_map(
            security.get("severity_map"), "security_advisories")
        code_scanning = config.get("code_scanning_alerts") or {}
        if not isinstance(code_scanning, dict):
            raise CheckError("github 'code_scanning_alerts' must be a mapping")
        code_scanning_severity_map = _severity_map(
            code_scanning.get("severity_map"), "code_scanning_alerts")
        secret_scanning = config.get("secret_scanning") or {}
        sbom = config.get("sbom_check") or {}
        sbom_ignore = sbom.get("ignore") or []
        if not isinstance(sbom_ignore, list):
            raise CheckError("sbom_check.ignore must be a list")
        actions = config.get("actions") or {}
        patterns = actions.get("ignore_workflow_name_patterns") or []
        if not isinstance(patterns, list):
            raise CheckError(
                "actions.ignore_workflow_name_patterns must be a list")
        try:
            action_patterns = tuple(
                re.compile(str(p), re.IGNORECASE) for p in patterns)
        except re.error as error:
            raise CheckError(
                f"actions.ignore_workflow_name_patterns: {error}") from error
        issues = config.get("issues") or {}
        issues_ignore = issues.get("ignore") or []
        if not isinstance(issues_ignore, list):
            raise CheckError("issues.ignore must be a list")
        return {
            "org": str(org),
            "team": str(config.get("team", "")),
            "name_prefix": str(config.get("name_prefix", "")),
            "include_archived": bool(config.get("include_archived", False)),
            "include_forks": bool(config.get("include_forks", True)),
            "api_url": str(config.get("api_url", GITHUB_API)),
            "rate_limit_safety_factor": int(
                config.get("rate_limit_safety_factor", 4)),
            "expect_min_repos": _positive_int(
                config.get("expect_min_repos", 1), "expect_min_repos"),
            "pr_ignore_prefixes": tuple(str(p) for p in ignore),
            "dependabot_severities": tuple(str(s).lower() for s in severities),
            "advisory_severity_map": advisory_severity_map,
            "code_scanning_severity_map": code_scanning_severity_map,
            "secret_scanning_require_enabled": bool(
                secret_scanning.get("require_enabled", True)),
            "sbom_ignore": tuple(str(r) for r in sbom_ignore),
            "actions_ignore_patterns": action_patterns,
            "actions_all_branches": bool(actions.get("all_branches", False)),
            "actions_show_healthy": bool(actions.get("show_healthy", False)),
            "issues_ignore": tuple(str(r) for r in issues_ignore),
            "subnodes": parse_subnodes(config),
            # `secrets: {token: …}` — required, so two checks of this type can
            # each carry their own team's credential (little-sister ADR-0023).
            "token_ref": parse_secret_refs(config, "token")["token"],
        }

    def config_summary(self) -> str:
        scope = f"{self.org}/{self.team}" if self.team else self.org
        return config_markdown({
            "scope": scope,
            "name prefix": self.name_prefix or None,
            "include archived": "yes" if self.include_archived else "no",
            "expected repositories": str(self.expect_min_repos),
            "show healthy Actions": "yes" if self.actions_show_healthy else "no",
        })

    def _subnode_tokens(self) -> dict[str, str]:
        """Values a `subnodes:` `about` may reference as `{token}` — this check's
        own `org` / `team`, and the shared `{pin_note}` sentence."""
        return {"org": self.org, "team": self.team, "pin_note": PIN_NOTE}

    def _meta(self, name: str) -> tuple[str, str]:
        """The (title, about) for aspect `name`: this check type's built-in
        `SUBNODES` text, which the config's `subnodes:` block replaces — or extends,
        where it writes `{default}` into its own text (little-sister ADR-0025).
        Tokens expand in either case."""
        configured = self.subnodes.get(name, {})
        default = SUBNODES.get(name, {})
        tokens = self._subnode_tokens()
        return (resolve_text(configured.get("title", ""),
                             default.get("title", ""), tokens),
                resolve_text(configured.get("about", ""),
                             default.get("about", ""), tokens))

    # --- helpers -------------------------------------------------------------

    def _make_client(self, token: str) -> GitHubClient:
        """Build the API client. Overridden in tests to avoid live calls."""
        return GitHubClient(token, api_url=self.api_url,
                            timeout=self.timeout_seconds)

    def _discover(self, client: GitHubClient) -> list[Repo]:
        """The in-scope repositories (team-scoped when ``team`` is set), as typed
        :class:`Repo` values — this is the seam where the API's ``Any`` stops."""
        if self.team:
            teams = client.get_paginated(f"/orgs/{self.org}/teams")
            match = next((t for t in teams
                          if self.team in (values.text(t, "slug"),
                                           values.text(t, "name"))), None)
            if match is None:
                raise GitHubError(
                    f"team {self.team!r} not found in org {self.org!r}")
            # NOT `slug`: that name holds the imported slug builder, and rebinding
            # it here would shadow the function for the rest of this scope — a trap
            # for the next aspect that needs it.
            team_slug = values.text(match, "slug", required=True, where="team")
            repos = client.get_paginated(
                f"/orgs/{self.org}/teams/{team_slug}/repos")
        else:
            repos = client.get_paginated(f"/orgs/{self.org}/repos")
        kept: list[Repo] = []
        for row in repos:
            repo = Repo.from_api(row)
            if repo.archived and not self.include_archived:
                continue
            if repo.fork and not self.include_forks:
                continue
            if self.name_prefix and not repo.name.startswith(self.name_prefix):
                continue
            kept.append(repo)
        return kept

    # --- aspects -------------------------------------------------------------

    def _pull_requests(self, client: GitHubClient,
                       repos: list[Repo]) -> CheckResult:
        """WARN if any repo has an open pull request (excluding ignored titles)."""
        description = "Open pull requests awaiting attention"
        title, about = self._meta("pull_requests")
        try:
            entries: list[tuple[str, str]] = []
            for repo in repos:
                prs = client.get_paginated(
                    f"/repos/{repo.full_name}/pulls", {"state": "open"})
                for pull in prs:
                    # NOT `title`: that name holds this leaf's display label, and
                    # rebinding it here handed the leaf the last PR's subject.
                    subject = values.text(pull, "title")
                    if any(subject.upper().startswith(prefix.upper())
                           for prefix in self.pr_ignore_prefixes):
                        continue
                    user = values.text(pull, "user", "login", default="?")
                    url = values.text(pull, "html_url")
                    number = values.number(pull, "number")
                    name = plain(repo.name)
                    entries.append((
                        _entry_slug(repo, "pr", number, url),
                        f"{_link(f'{name}: {plain(subject)}', url)} "
                        f"[{plain(user)}]"))
        except GitHubError as error:
            # Prose, not members: one condition (the aspect could not run) rather
            # than a list of independently pinnable findings — so it stays a plain
            # string and the node pin remains its unit of suppression
            # (little-sister ADR-0036).
            return CheckResult(
                StatusCode.ERROR,
                [f"failed to check pull requests: {plain(str(error))}"],
                name="pull_requests", description=description,
                title=title, about=about)
        code = StatusCode.WARN if entries else StatusCode.OK
        return CheckResult(code, entries, name="pull_requests",
                           description=description, title=title, about=about)

    def _collect(self, client: GitHubClient, repos: list[Repo],
                 suffix: str) -> tuple[
                     list[tuple[Repo, list[Any]]],
                     list[tuple[str, str]], list[Repo]]:
        """Fetch an open-alerts list per repo. A **404** means the feature is not
        enabled for that repo; those repos are returned separately (as
        ``not_enabled``) so a caller can skip them quietly *or* flag them. Any
        other failure is surfaced as an error note, so a token/scope problem
        never reads as 'no alerts'.

        The error notes are keyed like the findings: "this repository could not be
        read" is a condition somebody may well be working on (a missing scope, an
        archived repo), and it should be pinnable without silencing the alerts that
        *did* come back."""
        results: list[tuple[Repo, list[Any]]] = []
        errors: list[tuple[str, str]] = []
        not_enabled: list[Repo] = []
        for repo in repos:
            try:
                alerts = client.get_paginated(
                    f"/repos/{repo.full_name}{suffix}", {"state": "open"})
            except GitHubError as error:
                if error.status == 404:
                    not_enabled.append(repo)
                    continue
                errors.append((_entry_slug(repo, "unreadable"),
                               f"{plain(repo.name)}: could not read "
                               f"({plain(str(error))})"))
                continue
            results.append((repo, alerts))
        return results, errors, not_enabled

    def _finalize(self, name: str, description: str,
                  entries: list[tuple[str, str]], base_code: StatusCode,
                  errors: list[tuple[str, str]]) -> CheckResult:
        """One aspect leaf from its findings and its read failures.

        The reason is handed over as ``(slug, text)`` pairs, which declares the
        lines **members** (little-sister ADR-0036): each is an independent
        finding, so an operator
        who opens a ticket for one can pin that line and leave the rest of the
        aspect reporting."""
        reason = [*entries, *errors]
        code = base_code
        if errors and code is StatusCode.OK:
            code = StatusCode.WARN
        title, about = self._meta(name)
        return CheckResult(code, reason, name=name, description=description,
                           title=title, about=about)

    def _severity_bands(
        self, name: str, description: str,
        groups: dict[str, list[tuple[str, str]]],
        severity_map: dict[str, StatusCode],
        errors: list[tuple[str, str]],
        *, order: tuple[str, ...] = SECURITY_SEVERITY_ORDER,
    ) -> CheckResult:
        """One aspect branch with one uncoded leaf per source-severity band.

        The grouping carries severity onto the node rather than burying it in the
        reason text. Empty configured bands still render as OK, making it visible that
        they were watched. Read failures stay on the aspect container at WARN:
        they have no honest source severity and must not be smuggled into one.
        """
        seen = set(order)
        band_order = [*order,
                      *(severity for severity in severity_map
                        if severity not in seen),
                      *(severity for severity in groups
                        if severity not in seen and severity not in severity_map)]
        children: list[CheckResult] = []
        for severity in band_order:
            if severity not in severity_map and severity not in groups:
                continue
            entries = groups.get(severity, [])
            code = (severity_map.get(severity, StatusCode.WARN) if entries
                    else StatusCode.OK)
            children.append(CheckResult(
                code, entries, name=severity,
                description=f"{severity.capitalize()} {description}",
                title=severity.capitalize()))
        title, about = self._meta(name)
        return CheckResult(
            StatusCode.WARN if errors else StatusCode.OK,
            errors,
            name=name,
            description=description,
            children=tuple(children),
            title=title,
            about=about,
        )

    def _security_advisories(self, client: GitHubClient,
                             repos: list[Repo]) -> CheckResult:
        """Open Dependabot alerts, grouped into configured severity bands."""
        results, errors, _ = self._collect(client, repos, "/dependabot/alerts")
        groups: dict[str, list[tuple[str, str]]] = {}
        for repo, alerts in results:
            for alert in alerts:
                severity = values.text(alert, "security_advisory", "severity",
                                       default="unknown").lower()
                if severity not in self.dependabot_severities:
                    continue
                summary = values.text(alert, "security_advisory", "summary")
                url = values.text(alert, "html_url")
                number = values.number(alert, "number")
                name = plain(repo.name)
                groups.setdefault(severity, []).append((
                    _entry_slug(repo, "advisory", number, url),
                    _link(f"{name}: {plain(summary)}", url)))
        severity_map = {
            severity: self.advisory_severity_map.get(severity, StatusCode.WARN)
            for severity in self.dependabot_severities
        }
        return self._severity_bands(
            "security_advisories", "Dependabot advisories", groups,
            severity_map, errors, order=self.dependabot_severities)

    def _code_scanning_alerts(self, client: GitHubClient,
                              repos: list[Repo]) -> CheckResult:
        """Open code-scanning alerts, grouped into source-severity bands."""
        results, errors, _ = self._collect(client, repos, "/code-scanning/alerts")
        groups: dict[str, list[tuple[str, str]]] = {}
        for repo, alerts in results:
            for alert in alerts:
                severity = values.text(alert, "rule", "security_severity_level",
                                       default="none").lower()
                detail = (values.text(alert, "rule", "description")
                          or values.text(alert, "rule", "id")
                          or "alert")
                url = values.text(alert, "html_url")
                number = values.number(alert, "number")
                name = plain(repo.name)
                groups.setdefault(severity, []).append((
                    _entry_slug(repo, "codescan", number, url),
                    _link(f"{name}: {plain(detail)}", url)))
        return self._severity_bands(
            "code_scanning_alerts", "code-scanning alerts", groups,
            self.code_scanning_severity_map, errors)

    def _secret_scanning_alerts(self, client: GitHubClient,
                                repos: list[Repo]) -> CheckResult:
        """Any open secret-scanning alert → ERROR. Unless
        ``secret_scanning.require_enabled`` is false, a repo with secret scanning
        **not enabled** is flagged too (also → ERROR): the alerts endpoint 404s
        when scanning is disabled for the repo, which would otherwise read as
        'no alerts'."""
        results, errors, not_enabled = self._collect(
            client, repos, "/secret-scanning/alerts")
        entries: list[tuple[str, str]] = []
        for repo, alerts in results:
            for alert in alerts:
                secret = (values.text(alert, "secret_type_display_name")
                          or values.text(alert, "secret_type")
                          or "secret")
                created = values.text(alert, "created_at")
                url = values.text(alert, "html_url")
                number = values.number(alert, "number")
                name = plain(repo.name)
                entries.append((
                    _entry_slug(repo, "secret", number, url),
                    _link(f"{name}: {plain(secret)} detected {plain(created)}",
                          url)))
        if self.secret_scanning_require_enabled:
            for repo in not_enabled:
                name = plain(repo.name)
                settings = (f"https://github.com/{repo.full_name}"
                            "/settings/security_analysis")
                # A distinct kind, not `secret`: "scanning is off" is a different
                # condition from "an alert fired", and pinning the one must not
                # need the other's number.
                entries.append((
                    _entry_slug(repo, "secret-scanning-off"),
                    _link(f"{name}: secret scanning not enabled", settings)))
        code = StatusCode.ERROR if entries else StatusCode.OK
        return self._finalize(
            "secret_scanning_alerts",
            "Open secret-scanning alerts (and repos with it disabled)",
            entries, code, errors)

    def _sbom_check(self, client: GitHubClient,
                    repos: list[Repo]) -> CheckResult:
        """A repo with code but no dependency graph (SBOM) → ERROR. Repos in
        ``sbom_ignore`` are skipped; a 404 counts as missing, a permission error
        is surfaced."""
        entries: list[tuple[str, str]] = []
        errors: list[tuple[str, str]] = []
        for repo in repos:
            if repo.name in self.sbom_ignore:
                continue
            name = plain(repo.name)
            network = f"https://github.com/{repo.full_name}/network/dependencies"
            # At most one line per repository, so the repo and the aspect are the
            # whole identity — no number to hang it on and none needed.
            missing = (_entry_slug(repo, "sbom"),
                       _link(f"{name}: missing SBOM", network))
            try:
                sbom = client.get(
                    f"/repos/{repo.full_name}/dependency-graph/sbom")
            except GitHubError as error:
                if error.status == 404:
                    entries.append(missing)
                    continue
                errors.append((_entry_slug(repo, "unreadable"),
                               f"{name}: could not read ({plain(str(error))})"))
                continue
            if not values.rows(sbom, "sbom", "relationships", where="sbom"):
                entries.append(missing)
        code = StatusCode.ERROR if entries else StatusCode.OK
        return self._finalize("sbom_check",
                              "Repositories missing an SBOM (dependency graph)",
                              entries, code, errors)

    #: Workflow-run conclusions that count as a failure.
    _ACTIONS_FAIL = ("failure", "timed_out", "startup_failure")
    #: Empty-conclusion states that positively mean work is in flight. Unknown
    #: states do not default to running (little-sister ADR-0032 rule 7).
    _ACTIONS_RUNNING = ("queued", "in_progress", "pending", "requested")

    @classmethod
    def _action_verdict(cls, run: object) -> tuple[StatusCode, str] | None:
        """The completed verdict a run contributes, or none when it contributes
        only an in-flight/neutral fact. Cancelled and skipped runs deliberately do
        not erase the last useful verdict beneath them."""
        status = values.text(run, "status").lower()
        conclusion = values.text(run, "conclusion").lower()
        if conclusion in cls._ACTIONS_FAIL:
            return StatusCode.ERROR, "failed"
        if status == "waiting" or conclusion == "action_required":
            return StatusCode.WARN, "waiting"
        if conclusion == "success":
            return StatusCode.OK, "passed"
        return None

    @staticmethod
    def _action_text(repo: Repo, workflow: str, branch: str,
                     completed: object | None, running: object | None,
                     verdict: tuple[StatusCode, str] | None) -> str:
        """One workflow line carrying its last verdict and current run together."""
        where = (f"{plain(repo.name)} ({plain(branch)}) / "
                 f"{plain(workflow)}")
        completed_url = (values.text(completed, "html_url")
                         if completed is not None else "")
        running_url = (values.text(running, "html_url")
                       if running is not None else "")
        run_number = (values.number(completed, "run_number")
                      if completed is not None else 0)
        if verdict is None:
            text = f"{_link(where, running_url)}: no completed run"
        else:
            _code, word = verdict
            number = f" (#{run_number})" if run_number else ""
            text = f"{_link(where, completed_url)}: {word}{number}"
        if running is not None:
            running_number = values.number(running, "run_number")
            label = f"#{running_number} running" if running_number else "running"
            text += f" · {_link(label, running_url)}"
        return text

    def _actions(self, client: GitHubClient,
                 repos: list[Repo]) -> CheckResult:
        """One coded entry per workflow/branch that has something to say.

        The entry code is the newest useful completed verdict. A newer in-flight
        run is an additional flag and words on that same stable entry, so a retry
        cannot hide the failure it is trying to fix. Healthy idle workflows are
        optional; a run in flight is always emitted. Only runs of a currently
        existing workflow count.
        """
        problem_entries: list[Entry] = []
        running_entries: list[Entry] = []
        healthy_entries: list[Entry] = []
        for repo in repos:
            name = plain(repo.name)
            full = repo.full_name
            try:
                existing = self._existing_workflow_ids(client, full)
            except GitHubError as error:
                if error.status == 404:
                    continue                        # Actions not enabled
                problem_entries.append(Entry(
                    _entry_slug(repo, "workflows-unreadable"),
                    f"{name}: could not read workflows ({plain(str(error))})",
                    code=StatusCode.WARN))
                continue
            params: dict[str, Any] = {"per_page": 100}
            if not self.actions_all_branches and repo.default_branch:
                params["branch"] = repo.default_branch
            try:
                data = client.get(f"/repos/{full}/actions/runs", params)
            except GitHubError as error:
                if error.status == 404:
                    continue                        # Actions not enabled
                problem_entries.append(Entry(
                    _entry_slug(repo, "runs-unreadable"),
                    f"{name}: could not read ({plain(str(error))})",
                    code=StatusCode.WARN))
                continue
            # GitHub returns newest first. Keep the first in-flight run and the
            # first useful completed verdict independently for each stable
            # (workflow, branch) identity. The old one-set loop let the former
            # claim the key and made the latter disappear.
            states: dict[
                tuple[int, str],
                tuple[str, object | None, object | None,
                      tuple[StatusCode, str] | None],
            ] = {}
            for run in values.rows(data, "workflow_runs"):
                workflow_id = values.number(run, "workflow_id")
                if workflow_id not in existing:
                    continue                        # run of a deleted workflow
                workflow = (values.text(run, "name")
                            or str(workflow_id) or "workflow")
                branch = values.text(run, "head_branch", default="?")
                key = (workflow_id, branch)
                if any(p.search(workflow) for p in self.actions_ignore_patterns):
                    continue
                status = values.text(run, "status").lower()
                current = states.get(key, (workflow, None, None, None))
                current_workflow, completed, running, verdict = current
                if running is None and status in self._ACTIONS_RUNNING:
                    running = run
                run_verdict = self._action_verdict(run)
                if completed is None and run_verdict is not None:
                    completed, verdict = run, run_verdict
                states[key] = (current_workflow, completed, running, verdict)

            for (workflow_id, branch), state in states.items():
                workflow, completed, running, verdict = state
                if verdict is None and running is None:
                    continue
                entry_code = verdict[0] if verdict else StatusCode.UNDEFINED
                if (entry_code is StatusCode.OK and running is None
                        and not self.actions_show_healthy):
                    continue
                entry = Entry(
                    slug(repo.name, "workflow", workflow_id, branch),
                    self._action_text(
                        repo, workflow, branch, completed, running, verdict),
                    code=entry_code,
                    running=running is not None,
                )
                if entry_code in (StatusCode.ERROR, StatusCode.WARN):
                    problem_entries.append(entry)
                elif running is not None:
                    running_entries.append(entry)
                else:
                    healthy_entries.append(entry)
        title, about = self._meta("actions")
        return CheckResult(
            reason=(*problem_entries, *running_entries, *healthy_entries),
            name="actions",
            description="Latest completed and in-flight workflow-run state",
            title=title,
            about=about,
            entries=True,
        )

    @staticmethod
    def _existing_workflow_ids(client: GitHubClient, full_name: str) -> set[int]:
        """IDs of the repo's workflows that still exist (``state`` != ``deleted``),
        so runs of a deleted workflow can be dropped."""
        data = client.get(f"/repos/{full_name}/actions/workflows",
                          {"per_page": 100})
        return {values.number(workflow, "id")
                for workflow in values.rows(data, "workflows")
                if values.text(workflow, "state") != "deleted"}

    def _issues(self, client: GitHubClient, repos: list[Repo]) -> CheckResult:
        """An open issue → WARN, one line per issue. Repos listed under
        ``issues.ignore`` are skipped.

        **Pull requests are dropped** (:func:`_is_pull_request`): the issues endpoint
        returns them too, and counting them here would report every open PR twice —
        once here and once under ``pull_requests``. A **404** means issues are turned
        off for that repo, which is worth showing rather than reading as 'none'."""
        entries: list[tuple[str, str]] = []
        errors: list[tuple[str, str]] = []
        for repo in repos:
            if repo.name in self.issues_ignore:
                continue
            name = plain(repo.name)
            issues_url = f"https://github.com/{repo.full_name}/issues"
            try:
                # paginated: the plain endpoint caps at GitHub's default page size,
                # which would silently under-report a busy repository
                rows = client.get_paginated(
                    f"/repos/{repo.full_name}/issues", {"state": "open"})
            except GitHubError as error:
                if error.status == 404:
                    entries.append((
                        _entry_slug(repo, "issues-off"),
                        _link(f"{name}: issues are disabled", issues_url)))
                    continue
                errors.append((_entry_slug(repo, "unreadable"),
                               f"{name}: could not read ({plain(str(error))})"))
                continue
            for row in rows:
                if _is_pull_request(row):
                    continue
                # a real int, so it can be put in the URL — an escaped value could not
                number = values.number(row, "number")
                subject = values.text(row, "title", default="unknown")
                entries.append((
                    _entry_slug(repo, "issue", number),
                    f"{_link(f'{name}: has issue {number}', f'{issues_url}/{number}')}"
                    f": {plain(subject)}"))
        code = StatusCode.WARN if entries else StatusCode.OK
        return self._finalize("issues", "Open issues per repository",
                              entries, code, errors)


    # --- run -----------------------------------------------------------------

    def _scope_reading(self, repos: list[Repo]) -> tuple[StatusCode, str]:
        """The coverage backstop on the check's owned container
        (little-sister ADR-0043)."""
        count = len(repos)
        noun = "repository" if count == 1 else "repositories"
        if count >= self.expect_min_repos:
            return StatusCode.OK, f"{count} {noun} in scope"
        if count:
            return (StatusCode.WARN,
                    f"{count} {noun} in scope, expected at least "
                    f"{self.expect_min_repos}")
        filters = [f"org {plain(self.org)}"]
        if self.team:
            filters.append(f"team {plain(self.team)}")
        if self.name_prefix:
            filters.append(f'prefix "{plain(self.name_prefix)}"')
        return (StatusCode.WARN,
                f"no repositories in scope ({', '.join(filters)})")

    @staticmethod
    def _scope_report(repos: list[Repo]) -> str:
        """The discovered roster: presence without a status claim
        (little-sister ADR-0044)."""
        return "\n".join(
            f"- [{plain(repo.name)}](https://github.com/{repo.full_name})"
            for repo in repos)

    def run(self) -> CheckResult:
        try:
            client = self._make_client(self.token)
            repos = self._discover(client)
        except GitHubError as error:
            return CheckResult(StatusCode.ERROR,
                               [f"discovery failed: {plain(str(error))}"])
        # names only — the payloads are large and one line per run is enough to see
        # what the discovery filter actually selected
        logger.info("%s: %d repositories in scope: %s", self.path, len(repos),
                    ", ".join(repo.name for repo in repos) or "(none)")
        scope_code, scope_reason = self._scope_reading(repos)
        scope_report = self._scope_report(repos)

        try:
            _limit, remaining, _reset = client.rate_limit()
            needed = max(1, len(repos)) * len(self.ASPECTS)
            if remaining < self.rate_limit_safety_factor * needed:
                return CheckResult(
                    StatusCode.WARN,
                    [f"skipped this run: {remaining} API calls left, need > "
                     f"{self.rate_limit_safety_factor}×{needed} "
                     f"for {len(repos)} repo(s)", scope_reason],
                    report=scope_report)
        except GitHubError:
            pass   # rate-limit endpoint unavailable — proceed rather than block

        children = (
            self._pull_requests(client, repos),
            self._security_advisories(client, repos),
            self._code_scanning_alerts(client, repos),
            self._secret_scanning_alerts(client, repos),
            self._sbom_check(client, repos),
            self._actions(client, repos),
            self._issues(client, repos),
        )
        return CheckResult(
            scope_code,
            [scope_reason],
            children=children,
            report=scope_report,
        )
