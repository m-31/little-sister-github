"""Fixture-based tests for the github check — no live GitHub calls."""
from __future__ import annotations

import re

import pytest
from little_sister.checks import CheckError
from little_sister.status import StatusCode

from little_sister_github.github import (
    SUBNODES,
    GitHubCheck,
    GitHubError,
    Repo,
)

_SBOM_PRESENT = {"sbom": {"relationships": [{"spdxElementId": "root"}]}}
_SBOM_EMPTY = {"sbom": {"relationships": []}}


class FakeClient:
    """Stands in for GitHubClient; routes by path, returns canned data.

    ``data`` maps ``(repo_full_name, kind)`` -> list (kind: pulls / dependabot /
    code_scanning / secret_scanning); ``errors`` maps the same key -> a
    ``GitHubError`` to raise; ``sboms`` maps repo_full_name -> an SBOM object or a
    ``GitHubError`` (absent -> present SBOM); ``runs`` maps repo_full_name -> an
    ``/actions/runs`` object or a ``GitHubError`` (absent -> no runs).
    """

    _KINDS = (
        ("/pulls", "pulls"),
        ("/dependabot/alerts", "dependabot"),
        ("/code-scanning/alerts", "code_scanning"),
        ("/secret-scanning/alerts", "secret_scanning"),
        ("/issues", "issues"),
    )

    def __init__(self, repos, data=None, errors=None, sboms=None, runs=None,
                 workflows=None, rate=(5000, 5000, 0)):
        self._repos = repos
        self._data = data or {}
        self._errors = errors or {}
        self._sboms = sboms or {}
        self._runs = runs or {}
        self._workflows = workflows or {}
        self._rate = rate
        self.calls: list[str] = []
        self.get_calls: list[tuple[str, dict]] = []
        self.paginated_calls: list[str] = []

    def get_paginated(self, path, params=None):
        self.calls.append(path)
        self.paginated_calls.append(path)
        if path.endswith("/teams"):
            return [{"slug": "platform", "name": "platform"}]
        if path.endswith("/repos"):
            return self._repos
        for suffix, kind in self._KINDS:
            if path.endswith(suffix):
                full = path[len("/repos/"):-len(suffix)]
                if (full, kind) in self._errors:
                    raise self._errors[(full, kind)]
                return self._data.get((full, kind), [])
        return []

    def get(self, path, params=None):
        self.calls.append(path)
        self.get_calls.append((path, params or {}))
        if path.endswith("/dependency-graph/sbom"):
            full = path[len("/repos/"):-len("/dependency-graph/sbom")]
            value = self._sboms.get(full, _SBOM_PRESENT)
            if isinstance(value, GitHubError):
                raise value
            return value
        if path.endswith("/actions/workflows"):
            full = path[len("/repos/"):-len("/actions/workflows")]
            if full in self._workflows:
                value = self._workflows[full]
                if isinstance(value, GitHubError):
                    raise value
                return value
            # default: every workflow_id seen in this repo's runs, all active
            runs = self._runs.get(full, {"workflow_runs": []})
            ids = {r.get("workflow_id") for r in (runs.get("workflow_runs") or [])}
            return {"workflows": [{"id": i, "state": "active"}
                                  for i in ids if i is not None]}
        if path.endswith("/actions/runs"):
            full = path[len("/repos/"):-len("/actions/runs")]
            value = self._runs.get(full, {"workflow_runs": []})
            if isinstance(value, GitHubError):
                raise value
            return value
        return {}

    def rate_limit(self):
        return self._rate


def _check(**over):
    cfg = {"path": "/github", "org": "example-org", "team": "platform",
           "name_prefix": "platform", "token_ref": "env://GITHUB_TOKEN"}
    cfg.update(over)
    return GitHubCheck(**cfg)


def _repo(name, archived=False, fork=False, default_branch="main"):
    return {"name": name, "full_name": f"example-org/{name}", "archived": archived,
            "fork": fork, "default_branch": default_branch}


def _repos(*names, **kw):
    """Typed ``Repo`` values, exactly as ``_discover`` hands them to an aspect."""
    return [Repo.from_api(_repo(name, **kw)) for name in names]


def _wf_run(name="ci", branch="main", status="completed", conclusion="success",
            wf_id=1, url="https://gh/run/1", run_number=1):
    return {"name": name, "head_branch": branch, "status": status,
            "conclusion": conclusion, "workflow_id": wf_id, "html_url": url,
            "run_number": run_number}


# --- discovery ---------------------------------------------------------------

def test_discovery_filters_archived_fork_and_prefix():
    check = _check(include_forks=False)
    fake = FakeClient([
        _repo("platform-a"),
        _repo("platform-old", archived=True),
        _repo("platform-fork", fork=True),
        _repo("other-service"),
    ])
    assert {r.name for r in check._discover(fake)} == {"platform-a"}


# --- pull_requests -----------------------------------------------------------

def test_pull_requests_warn_and_ignore_prefix():
    check = _check(pr_ignore_prefixes=("[PLATFORM-",))
    fake = FakeClient(
        [_repo("platform-a"), _repo("platform-b")],
        data={
            ("example-org/platform-a", "pulls"): [
                {"title": "Fix bug", "user": {"login": "alice"},
                 "html_url": "https://gh/pr/1"},
                {"title": "[PLATFORM-123] bot bump", "user": {"login": "bot"}},
            ],
            ("example-org/platform-b", "pulls"): [],
        },
    )
    result = check._pull_requests(fake, check._discover(fake))
    assert result.name == "pull_requests"
    assert result.code is StatusCode.WARN
    assert len(result.reason_texts) == 1
    assert "platform-a" in result.reason_texts[0]
    assert "(https://gh/pr/1)" in result.reason_texts[0]       # linked to the PR


def test_pull_requests_ok_when_none():
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      data={("example-org/platform-a", "pulls"): []})
    result = check._pull_requests(fake, check._discover(fake))
    assert result.code is StatusCode.OK
    assert result.reason_texts == []


# --- security_advisories (Dependabot) ---------------------------------------

def test_dependabot_high_is_error_and_filters_severity():
    check = _check(dependabot_severities=("critical", "high"))
    fake = FakeClient(
        [_repo("platform-a")],
        data={("example-org/platform-a", "dependabot"): [
            {"security_advisory": {"severity": "high", "summary": "RCE in x"},
             "html_url": "https://gh/dependabot/1"},
            {"security_advisory": {"severity": "low", "summary": "minor"}},
        ]},
    )
    result = check._security_advisories(fake, check._discover(fake))
    assert result.name == "security_advisories"
    bands = {child.name: child for child in result.children}
    assert result.code is StatusCode.OK
    assert bands["critical"].code is StatusCode.OK
    assert bands["high"].code is StatusCode.ERROR
    assert "low" not in bands                     # filter controls band existence
    assert len(bands["high"].reason_texts) == 1
    assert "RCE in x" in bands["high"].reason_texts[0]
    assert "(https://gh/dependabot/1)" in bands["high"].reason_texts[0]
    assert bands["high"].reason_texts[0].startswith("[platform-a: RCE in x]")


def test_dependabot_medium_only_is_warn():
    check = _check(dependabot_severities=("critical", "high", "medium", "low"))
    fake = FakeClient(
        [_repo("platform-a")],
        data={("example-org/platform-a", "dependabot"): [
            {"security_advisory": {"severity": "medium", "summary": "m"}},
        ]},
    )
    result = check._security_advisories(fake, check._discover(fake))
    medium = next(child for child in result.children if child.name == "medium")
    assert medium.code is StatusCode.WARN


def test_dependabot_404_skipped_but_403_surfaced():
    check = _check()
    repos = _repos("platform-a", "platform-b")
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "dependabot"): GitHubError("not found", status=404),
        ("example-org/platform-b", "dependabot"): GitHubError("forbidden", status=403),
    })
    result = check._security_advisories(fake, repos)
    assert result.code is StatusCode.WARN
    assert any("platform-b" in m for m in result.reason_texts)
    assert not any("platform-a" in m for m in result.reason_texts)


# --- code_scanning / secret_scanning ----------------------------------------

def test_code_scanning_any_alert_is_error():
    check = _check()
    fake = FakeClient(
        [_repo("platform-a")],
        data={("example-org/platform-a", "code_scanning"): [
            {"rule": {"security_severity_level": "high",
                      "description": "SQL injection"},
             "html_url": "https://gh/codescan/1"},
        ]},
    )
    result = check._code_scanning_alerts(fake, check._discover(fake))
    high = next(child for child in result.children if child.name == "high")
    assert result.code is StatusCode.OK
    assert high.code is StatusCode.ERROR
    assert "SQL injection" in high.reason_texts[0]
    assert "(https://gh/codescan/1)" in high.reason_texts[0]


def test_code_scanning_not_enabled_is_ok():
    check = _check()
    repos = _repos("platform-a")
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "code_scanning"):
            GitHubError("no analysis", status=404)})
    result = check._code_scanning_alerts(fake, repos)
    assert result.code is StatusCode.OK
    assert result.reason_texts == []
    assert result.children
    assert all(child.code is StatusCode.OK for child in result.children)


def test_each_security_aspect_has_its_own_configurable_severity_map():
    check = _check(
        dependabot_severities=("low",),
        advisory_severity_map={"low": StatusCode.OK},
        code_scanning_severity_map={"low": StatusCode.WARN},
    )
    fake = FakeClient(
        [_repo("platform-a")],
        data={
            ("example-org/platform-a", "dependabot"): [
                {"security_advisory": {"severity": "low", "summary": "minor"}}],
            ("example-org/platform-a", "code_scanning"): [
                {"rule": {"security_severity_level": "low",
                          "description": "minor"}}],
        },
    )
    advisory = check._security_advisories(fake, _repos("platform-a"))
    scanning = check._code_scanning_alerts(fake, _repos("platform-a"))
    assert advisory.children[0].code is StatusCode.OK
    assert next(child for child in scanning.children
                if child.name == "low").code is StatusCode.WARN


def test_secret_scanning_any_alert_is_error():
    check = _check()
    fake = FakeClient(
        [_repo("platform-a")],
        data={("example-org/platform-a", "secret_scanning"): [
            {"secret_type": "github_pat", "created_at": "2026-07-01T00:00:00Z",
             "html_url": "https://gh/secret/1"},
        ]},
    )
    result = check._secret_scanning_alerts(fake, check._discover(fake))
    assert result.code is StatusCode.ERROR
    assert "github_pat" in result.reason_texts[0]
    assert "(https://gh/secret/1)" in result.reason_texts[0]


def test_secret_scanning_not_enabled_is_flagged():
    check = _check()
    repos = _repos("platform-a")
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "secret_scanning"):
            GitHubError("secret scanning disabled", status=404)})
    result = check._secret_scanning_alerts(fake, repos)
    assert result.code is StatusCode.ERROR
    assert len(result.reason_texts) == 1
    assert "platform-a" in result.reason_texts[0]
    assert "not enabled" in result.reason_texts[0]
    # linked to the setting that turns it on
    assert "settings/security_analysis)" in result.reason_texts[0]


def test_secret_scanning_alert_and_not_enabled_both_listed():
    check = _check()
    repos = _repos("platform-a", "platform-b")
    fake = FakeClient(
        repos,
        data={("example-org/platform-a", "secret_scanning"): [
            {"secret_type": "github_pat", "created_at": "2026-07-01T00:00:00Z",
             "html_url": "https://gh/secret/1"}]},
        errors={("example-org/platform-b", "secret_scanning"):
                GitHubError("disabled", status=404)},
    )
    result = check._secret_scanning_alerts(fake, repos)
    assert result.code is StatusCode.ERROR
    assert len(result.reason_texts) == 2
    assert any("github_pat" in m for m in result.reason_texts)
    assert any("platform-b" in m and "not enabled" in m for m in result.reason_texts)


def test_secret_scanning_403_is_surfaced_not_flagged():
    check = _check()
    repos = _repos("platform-a")
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "secret_scanning"):
            GitHubError("forbidden", status=403)})
    result = check._secret_scanning_alerts(fake, repos)
    # a permission problem is an error note (WARN), never read as 'not enabled'
    assert result.code is StatusCode.WARN
    assert any("could not read" in m for m in result.reason_texts)
    assert not any("not enabled" in m for m in result.reason_texts)


def test_secret_scanning_require_enabled_false_suppresses_flag():
    check = _check(secret_scanning_require_enabled=False)
    repos = _repos("platform-a")
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "secret_scanning"):
            GitHubError("secret scanning disabled", status=404)})
    result = check._secret_scanning_alerts(fake, repos)
    assert result.code is StatusCode.OK
    assert result.reason_texts == []


# --- sbom_check --------------------------------------------------------------

def test_sbom_missing_is_error():
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _SBOM_EMPTY})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.name == "sbom_check"
    assert result.code is StatusCode.ERROR
    assert "missing SBOM" in result.reason_texts[0]
    assert "network/dependencies)" in result.reason_texts[0]   # linked to the dep graph


def test_sbom_present_is_ok():
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _SBOM_PRESENT})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.code is StatusCode.OK
    assert result.reason_texts == []


def test_sbom_ignore_list_skips_repo():
    check = _check(sbom_ignore=("platform-a",))
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _SBOM_EMPTY})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.code is StatusCode.OK
    assert result.reason_texts == []


def test_sbom_404_counts_as_missing():
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": GitHubError("nope", status=404)})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.code is StatusCode.ERROR
    assert "missing SBOM" in result.reason_texts[0]


# --- actions (failed workflow runs) -----------------------------------------

def test_actions_failure_is_error():
    check = _check()
    fake = FakeClient([_repo("platform-a")], runs={
        "example-org/platform-a": {"workflow_runs": [_wf_run(conclusion="failure")]}})
    result = check._actions(fake, check._discover(fake))
    assert result.name == "actions"
    assert result.code is None
    assert result.stored_code is StatusCode.ERROR
    assert result.reason_entries[0].code is StatusCode.ERROR
    assert "failed" in result.reason_texts[0]


def test_actions_waiting_is_warn():
    check = _check()
    fake = FakeClient([_repo("platform-a")], runs={
        "example-org/platform-a": {"workflow_runs": [
            _wf_run(status="waiting", conclusion="")]}})
    result = check._actions(fake, check._discover(fake))
    assert result.code is None
    assert result.stored_code is StatusCode.WARN
    assert "waiting" in result.reason_texts[0]


def test_actions_running_keeps_the_last_success_visible():
    check = _check()
    fake = FakeClient([_repo("platform-a")], runs={
        "example-org/platform-a": {"workflow_runs": [
            _wf_run(status="in_progress", conclusion="", run_number=12,
                    url="https://gh/run/12"),
            _wf_run(conclusion="success", run_number=11,
                    url="https://gh/run/11")]}})
    result = check._actions(fake, check._discover(fake))
    assert result.stored_code is StatusCode.OK
    assert len(result.reason_entries) == 1
    assert result.reason_entries[0].code is StatusCode.OK
    assert result.reason_entries[0].running is True
    assert "passed (#11)" in result.reason_texts[0]
    assert "#12 running" in result.reason_texts[0]


def test_actions_healthy_idle_is_hidden_by_default_and_can_be_enabled():
    fake = FakeClient([_repo("platform-a")], runs={
        "example-org/platform-a": {"workflow_runs": [
            _wf_run(conclusion="success")]}})
    hidden = _check()._actions(fake, _repos("platform-a"))
    shown = _check(actions_show_healthy=True)._actions(fake, _repos("platform-a"))
    assert hidden.stored_code is StatusCode.OK
    assert hidden.reason_texts == []
    assert shown.reason_entries[0].code is StatusCode.OK
    assert shown.reason_entries[0].running is False


def test_actions_latest_run_per_workflow_branch_wins():
    check = _check()
    fake = FakeClient([_repo("platform-a")], runs={
        "example-org/platform-a": {"workflow_runs": [
            _wf_run(conclusion="failure", wf_id=1),     # newest
            _wf_run(conclusion="success", wf_id=1)]}})   # older, same (wf, branch)
    result = check._actions(fake, check._discover(fake))
    assert result.stored_code is StatusCode.ERROR
    assert len(result.reason_texts) == 1


def test_actions_retry_does_not_hide_the_last_failure():
    check = _check()
    fake = FakeClient([_repo("platform-a")], runs={
        "example-org/platform-a": {"workflow_runs": [
            _wf_run(status="in_progress", conclusion="", run_number=22),
            _wf_run(conclusion="failure", run_number=21)]}})
    result = check._actions(fake, check._discover(fake))
    entry = result.reason_entries[0]
    assert result.stored_code is StatusCode.ERROR
    assert entry.code is StatusCode.ERROR
    assert entry.running is True
    assert "failed (#21)" in entry.text
    assert "#22 running" in entry.text


def test_actions_cancelled_retry_does_not_hide_the_last_failure():
    check = _check()
    fake = FakeClient([_repo("platform-a")], runs={
        "example-org/platform-a": {"workflow_runs": [
            _wf_run(conclusion="cancelled", run_number=22),
            _wf_run(conclusion="failure", run_number=21)]}})
    result = check._actions(fake, check._discover(fake))
    assert result.stored_code is StatusCode.ERROR
    assert "failed (#21)" in result.reason_texts[0]


def test_actions_a_first_run_in_flight_is_undefined_but_visible():
    check = _check()
    fake = FakeClient([_repo("platform-a")], runs={
        "example-org/platform-a": {"workflow_runs": [
            _wf_run(status="queued", conclusion="", run_number=1)]}})
    result = check._actions(fake, check._discover(fake))
    assert result.stored_code is StatusCode.UNDEFINED
    assert result.reason_entries[0].code is StatusCode.UNDEFINED
    assert result.reason_entries[0].running is True
    assert "no completed run" in result.reason_texts[0]


def test_actions_order_problems_then_running_then_healthy():
    check = _check(actions_show_healthy=True)
    fake = FakeClient([_repo("platform-a")], runs={
        "example-org/platform-a": {"workflow_runs": [
            _wf_run(name="healthy", wf_id=3, conclusion="success"),
            _wf_run(name="rebuilding", wf_id=2, status="in_progress",
                    conclusion="", run_number=2),
            _wf_run(name="rebuilding", wf_id=2, conclusion="success",
                    run_number=1),
            _wf_run(name="broken", wf_id=1, conclusion="failure"),
        ]}})
    result = check._actions(fake, check._discover(fake))
    assert [entry.code for entry in result.reason_entries] == [
        StatusCode.ERROR, StatusCode.OK, StatusCode.OK]
    assert [entry.running for entry in result.reason_entries] == [False, True, False]


def test_actions_ignore_pattern_skips_workflow():
    check = _check(actions_ignore_patterns=(re.compile("nightly", re.IGNORECASE),))
    fake = FakeClient([_repo("platform-a")], runs={
        "example-org/platform-a": {"workflow_runs": [
            _wf_run(name="Nightly Load", conclusion="failure")]}})
    result = check._actions(fake, check._discover(fake))
    assert result.stored_code is StatusCode.OK


def test_actions_default_branch_only_queries_default_branch():
    check = _check()
    fake = FakeClient([_repo("platform-a", default_branch="develop")])
    check._actions(fake, check._discover(fake))
    runs_calls = [p for (p, params) in fake.get_calls
                  if p.endswith("/actions/runs") and params.get("branch") == "develop"]
    assert runs_calls


def test_actions_all_branches_omits_branch_param():
    check = _check(actions_all_branches=True)
    fake = FakeClient([_repo("platform-a", default_branch="main")])
    check._actions(fake, check._discover(fake))
    runs_calls = [params for (p, params) in fake.get_calls
                  if p.endswith("/actions/runs")]
    assert runs_calls and all("branch" not in params for params in runs_calls)


def test_actions_ignores_runs_of_deleted_workflows():
    check = _check()
    fake = FakeClient(
        [_repo("platform-a")],
        runs={"example-org/platform-a": {"workflow_runs": [
            _wf_run(name="ci", conclusion="failure", wf_id=1),
            _wf_run(name="old-ci", conclusion="failure", wf_id=99)]}},
        workflows={"example-org/platform-a": {"workflows": [
            {"id": 1, "state": "active"}]}},          # workflow 99 no longer exists
    )
    result = check._actions(fake, check._discover(fake))
    assert result.stored_code is StatusCode.ERROR
    assert len(result.reason_texts) == 1                    # only the existing workflow
    assert "old-ci" not in result.reason_texts[0]           # the deleted one is dropped


def test_actions_deleted_only_repo_is_ok():
    check = _check()
    fake = FakeClient(
        [_repo("platform-a")],
        runs={"example-org/platform-a": {"workflow_runs": [
            _wf_run(name="old-ci", conclusion="failure", wf_id=99)]}},
        workflows={"example-org/platform-a": {"workflows": [
            {"id": 1, "state": "active"}]}},          # 99 absent -> deleted
    )
    result = check._actions(fake, check._discover(fake))
    assert result.stored_code is StatusCode.OK
    assert result.reason_texts == []


def test_actions_workflow_state_deleted_is_excluded():
    check = _check()
    fake = FakeClient(
        [_repo("platform-a")],
        runs={"example-org/platform-a": {"workflow_runs": [
            _wf_run(conclusion="failure", wf_id=5)]}},
        workflows={"example-org/platform-a": {"workflows": [
            {"id": 5, "state": "deleted"}]}},          # present but deleted
    )
    result = check._actions(fake, check._discover(fake))
    assert result.stored_code is StatusCode.OK


# --- run() -------------------------------------------------------------------

def test_run_returns_all_aspect_leaves(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    assert check.token == "x"          # resolved once, at construction
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "pulls"): [{"title": "Fix", "user": {"login": "a"}}],
    })
    monkeypatch.setattr(check, "_make_client", lambda token: fake)
    result = check.run()
    assert [c.name for c in result.children] == [
        "pull_requests", "security_advisories", "code_scanning_alerts",
        "secret_scanning_alerts", "sbom_check", "actions", "issues"]
    assert result.children[0].code is StatusCode.WARN
    assert result.code is StatusCode.OK
    assert result.reason_texts == ["1 repository in scope"]
    assert result.report == (
        "- [platform-a](https://github.com/example-org/platform-a)")


def test_run_warns_on_empty_scope_but_keeps_every_aspect(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    fake = FakeClient([])
    monkeypatch.setattr(check, "_make_client", lambda token: fake)
    result = check.run()
    assert result.code is StatusCode.WARN
    assert result.reason_texts == [
        'no repositories in scope (org example-org, team platform, prefix "platform")']
    assert result.report == ""
    assert [child.name for child in result.children] == list(check.ASPECTS)


def test_run_warns_below_the_configured_repository_minimum(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check(expect_min_repos=3)
    fake = FakeClient([_repo("platform-a"), _repo("platform-b")])
    monkeypatch.setattr(check, "_make_client", lambda token: fake)
    result = check.run()
    assert result.code is StatusCode.WARN
    assert result.reason_texts == [
        "2 repositories in scope, expected at least 3"]
    assert "platform-a" in result.report
    assert "platform-b" in result.report


@pytest.mark.parametrize("value", [0, -1, False, "none"])
def test_repository_minimum_cannot_disable_the_coverage_backstop(value):
    with pytest.raises(CheckError, match=r"expect_min_repos.*at least 1"):
        _check(expect_min_repos=value)


def test_unresolvable_token_is_recorded_at_construction(monkeypatch):
    """An unset variable is no longer a per-run failure: the check loads with the
    failure recorded and the engine pins it to a visible ERROR without ever
    calling run() (little-sister ADR-0023)."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    check = _check()
    assert check.token == ""
    assert len(check.secret_errors) == 1
    assert "GITHUB_TOKEN" in check.secret_errors[0]


def test_each_check_names_its_own_token(monkeypatch):
    """Two checks of this type, one per team, each with its own credential."""
    monkeypatch.setenv("GITHUB_TOKEN_PLATFORM", "q")
    monkeypatch.setenv("GITHUB_TOKEN_PAYMENTS", "v")
    platform = _check(path="/github/platform",
                     token_ref="env://GITHUB_TOKEN_PLATFORM")
    payments = _check(path="/github/payments", team="payments",
                 token_ref="env://GITHUB_TOKEN_PAYMENTS")
    assert (platform.token, payments.token) == ("q", "v")
    assert platform.owned_nodes().isdisjoint(payments.owned_nodes())


def test_pasted_token_is_rejected_as_a_config_error():
    """A secret value where a name belongs fails the load — and the message never
    echoes it onward into a visible reason."""
    # Written in two pieces so no `ghp_…` string exists in this file. It is GitHub's
    # own documentation example and not a live credential — which is also why the
    # denylist deliberately carries no token shape, since this fixture is the point —
    # but a secret scanner matches the *shape*, and a first push to a public
    # repository refused by push protection costs more to explain than a `+` costs to
    # read. The value at runtime is unchanged, so the test exercises exactly what it
    # did.
    pasted = "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a"
    with pytest.raises(CheckError) as caught:
        _check(token_ref=pasted)
    assert pasted not in str(caught.value)


def test_rate_limit_guard_skips(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check(rate_limit_safety_factor=4)
    fake = FakeClient([_repo("platform-a")], rate=(5000, 3, 0))   # 3 left, need 4×6
    monkeypatch.setattr(check, "_make_client", lambda token: fake)
    result = check.run()
    assert result.code is StatusCode.WARN
    assert "skipped" in result.reason_texts[0]
    assert result.reason_texts[1] == "1 repository in scope"
    assert "platform-a" in result.report


def test_config_loads_via_loader(tmp_path):
    # tmp_path is a *configuration root*: the loader reads its `checks/` aspect
    # (little-sister ADR-0031), so the config goes one level down.
    from little_sister.checks import load_checks
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "github.yaml").write_text(
        "type: github\n"
        "path: /github\n"
        "secrets:\n"
        "  token: env://GITHUB_TOKEN\n"
        "org: example-org\n"
        "team: platform\n"
        "name_prefix: platform\n"
        "expect_min_repos: 3\n"
        "pull_requests:\n"
        "  ignore_title_prefixes: ['[PLATFORM-']\n"
        "security_advisories:\n"
        "  severities: [critical, high]\n"
        "  severity_map: {critical: ERROR, high: WARN}\n"
        "code_scanning_alerts:\n"
        "  severity_map: {critical: ERROR, low: WARN}\n"
        "secret_scanning:\n"
        "  require_enabled: false\n"
        "sbom_check:\n"
        "  ignore: [platform-b]\n"
        "actions:\n"
        "  all_branches: true\n"
        "  show_healthy: true\n"
        "  ignore_workflow_name_patterns: ['nightly']\n"
        "issues:\n"
        "  ignore: [platform-a]\n"
    )
    checks = load_checks(str(tmp_path))
    assert len(checks) == 1
    assert isinstance(checks[0], GitHubCheck)
    assert checks[0].dependabot_severities == ("critical", "high")
    assert checks[0].expect_min_repos == 3
    assert checks[0].advisory_severity_map["high"] is StatusCode.WARN
    assert checks[0].code_scanning_severity_map["low"] is StatusCode.WARN
    assert checks[0].secret_scanning_require_enabled is False
    assert checks[0].sbom_ignore == ("platform-b",)
    assert checks[0].actions_all_branches is True
    assert checks[0].actions_show_healthy is True
    assert len(checks[0].actions_ignore_patterns) == 1
    # the config has always written the nested `issues:` block; the parser used to
    # read a top-level `issues_ignore`, so this list silently had no effect
    assert checks[0].issues_ignore == ("platform-a",)


# --- issues ------------------------------------------------------------------

def _issue(number, title="something broke", pull_request=False):
    row = {"number": number, "title": title}
    if pull_request:
        # every pull request is also an issue on this endpoint
        row["pull_request"] = {"url": f"https://api/pulls/{number}"}
    return row


def test_issues_lists_each_open_issue():
    check = _check()
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "issues"): [_issue(7, "disk full"), _issue(9)],
    })
    result = check._issues(fake, check._discover(fake))
    assert result.code is StatusCode.WARN
    assert len(result.reason_texts) == 2
    assert "has issue 7" in result.reason_texts[0]
    assert "disk full" in result.reason_texts[0]
    # the number goes into the URL as a number, not an escaped string
    assert ("https://github.com/example-org/platform-a/issues/7"
            in result.reason_texts[0])


def test_issues_excludes_pull_requests():
    """GitHub returns PRs from the issues endpoint; counting them here would report
    every open PR twice, since `pull_requests` already lists them."""
    check = _check()
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "issues"): [
            _issue(7, "disk full"), _issue(8, "Bump lib", pull_request=True)],
    })
    result = check._issues(fake, check._discover(fake))
    assert len(result.reason_texts) == 1
    assert "has issue 7" in result.reason_texts[0]
    assert "has issue 8" not in " ".join(result.reason_texts)


def test_issues_are_paginated():
    """The plain endpoint caps at GitHub's default page size, silently
    under-reporting a busy repository."""
    check = _check()
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "issues"): [_issue(1)],
    })
    check._issues(fake, check._discover(fake))
    assert "/repos/example-org/platform-a/issues" in fake.paginated_calls


def test_issues_no_open_issues_is_ok():
    check = _check()
    fake = FakeClient([_repo("platform-a")])
    result = check._issues(fake, check._discover(fake))
    assert result.code is StatusCode.OK
    assert result.reason_texts == []


def test_issues_ignore_list_skips_repo():
    check = _check(issues_ignore=("platform-a",))
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "issues"): [_issue(7)],
    })
    result = check._issues(fake, check._discover(fake))
    assert result.code is StatusCode.OK
    assert result.reason_texts == []


def test_issues_disabled_repo_is_flagged_not_read_as_none():
    check = _check()
    fake = FakeClient([_repo("platform-a")], errors={
        ("example-org/platform-a", "issues"): GitHubError("not found", status=404)})
    result = check._issues(fake, check._discover(fake))
    assert result.code is StatusCode.WARN
    assert "issues are disabled" in result.reason_texts[0]


def test_issues_other_error_is_surfaced_as_a_note():
    check = _check()
    fake = FakeClient([_repo("platform-a")], errors={
        ("example-org/platform-a", "issues"): GitHubError("forbidden", status=403)})
    result = check._issues(fake, check._discover(fake))
    assert result.code is StatusCode.WARN
    assert "could not read" in result.reason_texts[0]


def test_issues_leaf_carries_its_built_in_text():
    check = _check()
    fake = FakeClient([_repo("platform-a")])
    result = check._issues(fake, check._discover(fake))
    assert result.name == "issues"
    assert result.title == SUBNODES["issues"]["title"]


# --- subnode metadata (title / about) ----------------------------------------

def test_aspect_carries_expanded_title_and_about():
    # the check provides each aspect's label from its own `subnodes:` config,
    # expanding {org} / {team} (little-sister ADR-0025).
    check = _check(subnodes={
        "security_advisories": {
            "title": "Dependabot advisories",
            "about": "See https://github.com/orgs/{org}/security/alerts/dependabot"
                     "?q=is:open+team:{team}.",
        }})
    fake = FakeClient([_repo("platform-a")],
                      data={("example-org/platform-a", "dependabot"): []})
    result = check._security_advisories(fake, check._discover(fake))
    assert result.title == "Dependabot advisories"
    assert "orgs/example-org/" in result.about                       # {org}
    assert "team:platform" in result.about                        # {team}


def test_aspect_without_config_uses_the_built_in_text():
    """The type ships the labels, so a per-team config carries none of this prose."""
    check = _check()   # no subnodes configured
    fake = FakeClient([_repo("platform-a")],
                      data={("example-org/platform-a", "pulls"): []})
    result = check._pull_requests(fake, check._discover(fake))
    assert result.title == SUBNODES["pull_requests"]["title"]
    assert "Open pull requests" in result.about
    assert "repositories in scope" in result.about
    assert "{" not in result.about                  # no token left behind


def test_built_in_text_leaves_no_token_behind():
    """Whatever the config, every `{token}` in the shipped text resolves."""
    assert "{" not in _check()._meta("pull_requests")[1]
    assert "{" not in _check(team="payments")._meta("pull_requests")[1]
    # the token that does differ per team is in the security aspects' links
    assert "team:payments" in _check(team="payments")._meta(
        "security_advisories")[1]


def test_config_replaces_the_built_in_text():
    check = _check(subnodes={"sbom_check": {"title": "SBOMs",
                                            "about": "Tracked in the catalog."}})
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _SBOM_PRESENT})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.title == "SBOMs"
    assert result.about == "Tracked in the catalog."


def test_config_extends_the_built_in_text_with_the_default_token():
    check = _check(subnodes={"sbom_check": {
        "about": "{default}\n\nAsk {team} before adding an exemption."}})
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _SBOM_PRESENT})
    result = check._sbom_check(fake, check._discover(fake))
    assert "dependency graph (SBOM)" in result.about          # the built-in text
    assert result.about.endswith("Ask platform before adding an exemption.")
    assert result.title == SUBNODES["sbom_check"]["title"]    # title still default


def test_config_loads_subnodes_via_loader(tmp_path):
    from little_sister.checks import load_checks
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "github.yaml").write_text(
        "type: github\n"
        "path: /github\n"
        "secrets:\n"
        "  token: env://GITHUB_TOKEN\n"
        "org: example-org\n"
        "team: platform\n"
        "subnodes:\n"
        "  actions:\n"
        "    title: Workflow runs\n"
        "    about: Latest run per workflow.\n"
    )
    checks = load_checks(str(tmp_path))
    assert isinstance(checks[0], GitHubCheck)
    assert checks[0].subnodes["actions"] == {
        "title": "Workflow runs", "about": "Latest run per workflow."}


# --- keyed entries (little-sister ADR-0036) -----------------------------------

def test_pull_request_lines_are_members_keyed_by_repo_and_number():
    """`<repo>-pr-<number>`: GitHub numbers a PR per repository, and the number is
    minted with it and retired with it."""
    check = _check()
    fake = FakeClient(
        [_repo("platform-a"), _repo("platform-b")],
        data={
            ("example-org/platform-a", "pulls"): [
                {"title": "Fix bug", "number": 42, "user": {"login": "alice"},
                 "html_url": "https://gh/pr/42"}],
            ("example-org/platform-b", "pulls"): [
                {"title": "Bump", "number": 7, "user": {"login": "bot"},
                 "html_url": "https://gh/pr/7"}],
        },
    )
    result = check._pull_requests(fake, check._discover(fake))
    assert result.members
    assert [e.slug for e in result.reason_entries] == ["platform-a-pr-42",
                                                       "platform-b-pr-7"]


def test_a_pull_request_slug_does_not_move_when_one_above_it_is_merged():
    """The property the whole scheme exists for: a pin on the second line must
    still name the same PR after the first one is gone."""
    check = _check()
    both = {("example-org/platform-a", "pulls"): [
        {"title": "First", "number": 1, "html_url": "https://gh/pr/1"},
        {"title": "Second", "number": 2, "html_url": "https://gh/pr/2"}]}
    left = {("example-org/platform-a", "pulls"): [
        {"title": "Second", "number": 2, "html_url": "https://gh/pr/2"}]}
    before = check._pull_requests(FakeClient([_repo("platform-a")], data=both),
                                  _repos("platform-a"))
    after = check._pull_requests(FakeClient([_repo("platform-a")], data=left),
                                 _repos("platform-a"))
    assert before.reason_entries[1].slug == after.reason_entries[0].slug


def test_the_pull_request_leaf_keeps_its_own_title():
    """The leaf's label is `Pull requests`, not the subject of the last PR read —
    the loop used to rebind the name holding it."""
    check = _check()
    fake = FakeClient(
        [_repo("platform-a")],
        data={("example-org/platform-a", "pulls"): [
            {"title": "Fix bug", "number": 1, "html_url": "https://gh/pr/1"}]})
    result = check._pull_requests(fake, check._discover(fake))
    assert result.title == "Pull requests"


def test_issue_lines_are_keyed_by_repo_and_issue_number():
    check = _check()
    fake = FakeClient(
        [_repo("platform-a")],
        data={("example-org/platform-a", "issues"): [
            {"number": 7, "title": "disk full"},
            {"number": 9, "title": "flaky test"}]})
    result = check._issues(fake, check._discover(fake))
    assert [e.slug for e in result.reason_entries] == ["platform-a-issue-7",
                                                       "platform-a-issue-9"]


def test_a_workflow_line_is_keyed_by_workflow_and_branch_not_by_the_run():
    """A push mints a new run id; a pin keyed on the run would fall off a workflow
    that is still red. The (workflow, branch) pair is what the line *is*."""
    check = _check(actions_all_branches=True)

    def leaf(run_url):
        runs = {"example-org/platform-a": {"workflow_runs": [
            _wf_run(name="ci", branch="release/1.2", conclusion="failure",
                    wf_id=5, url=run_url)]}}
        return check._actions(FakeClient([_repo("platform-a")], runs=runs),
                              _repos("platform-a"))

    first, second = leaf("https://gh/run/100"), leaf("https://gh/run/200")
    assert first.reason_entries[0].slug == "platform-a-workflow-5-release-1.2"
    assert second.reason_entries[0].slug == first.reason_entries[0].slug


def test_scanning_switched_off_is_its_own_kind_of_entry():
    """"Scanning is off" is a different condition from "an alert fired", so pinning
    the one must not need the other's number."""
    check = _check()
    fake = FakeClient([_repo("platform-a")], errors={
        ("example-org/platform-a", "secret_scanning"): GitHubError("off", status=404)})
    result = check._secret_scanning_alerts(fake, check._discover(fake))
    assert [e.slug for e in result.reason_entries] == \
        ["platform-a-secret-scanning-off"]


def test_a_read_failure_is_keyed_too():
    """A repository that cannot be read is a condition somebody may be fixing — it
    should be pinnable without silencing the alerts that did come back."""
    check = _check()
    fake = FakeClient([_repo("platform-a")], errors={
        ("example-org/platform-a", "secret_scanning"): GitHubError("boom", status=500)})
    result = check._secret_scanning_alerts(fake, check._discover(fake))
    assert [e.slug for e in result.reason_entries] == ["platform-a-unreadable"]


def test_an_aspect_that_could_not_run_at_all_stays_prose():
    """One condition, not a list of findings — so the node pin remains its unit of
    suppression rather than the line's (little-sister ADR-0036)."""
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      errors={("example-org/platform-a", "pulls"):
                              GitHubError("boom", status=500)})
    result = check._pull_requests(fake, check._discover(fake))
    assert result.code is StatusCode.ERROR
    assert not result.members
