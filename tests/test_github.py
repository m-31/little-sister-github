"""Fixture-based tests for the github check — no live GitHub calls."""
from __future__ import annotations

import contextlib
import logging
import re
from email.message import Message
from unittest import mock

import pytest
from little_sister import fetch as ls_fetch
from little_sister.checks import CheckError
from little_sister.fetch import Response
from little_sister.status import StatusCode
from little_sister.transport import Deadline, DeadlineExceeded, Fault

from little_sister_github import github as mod_github
from little_sister_github.github import (
    BAND_GLYPHS,
    DEFAULT_REQUEST_TIMEOUT,
    RETRY_BACKOFF_SECONDS,
    SECURITY_SEVERITY_ORDER,
    SUBNODES,
    THROTTLE_FLOOR_SECONDS,
    GitHubCheck,
    GitHubClient,
    GitHubError,
    Repo,
    _throttle_wait,
)

_SBOM_PRESENT = {"sbom": {"relationships": [{"spdxElementId": "root"}]}}
_SBOM_EMPTY = {"sbom": {"relationships": []}}


# Every fixture failure names its fault, because `GitHubError` requires one: the
# classification is the decision this package must never take by accident
# (little-sister ADR-0058), and a fixture that let it default would be taking it.

def _answered(message, status):
    """A failure GitHub *answered* with — the thing is absent, or not visible."""
    return GitHubError(message, status=status, fault=Fault.ANSWERED)


def _transient(message="502 Bad Gateway"):
    """A failure that means *we could not ask*, so it grades nothing."""
    return GitHubError(message, status=502, fault=Fault.TRANSIENT)


def _denied(message="403 Forbidden"):
    return _answered(message, 403)


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
                 workflows=None, rate=(5000, 5000, 0),
                 owner_type="Organization", auth_login=None,
                 private_repos=None, paused_seconds=0.0, read_seconds=0.0,
                 slowest_read=0.0, slowest_path=""):
        self._repos = repos
        # The real client counts what it slept on a throttle and the check reads it
        # for the node's sentence — so the double carries it too, and a test that
        # wants the sentence sets it.
        self.paused_seconds = paused_seconds
        # The rest of what the real client counts for the run's trace. The timings
        # have no meaning in a double — nothing here takes time — and a test that
        # wants the summary sentence sets them the way it sets `paused_seconds`.
        self.read_seconds = read_seconds
        self.slowest_read = slowest_read
        self.slowest_path = slowest_path
        # What `GET /users/<login>` reports the account to be, and who the token
        # belongs to — the two answers discovery branches on.
        self._owner_type = owner_type
        self._auth_login = auth_login
        # What `/user/repos` returns; absent means "the same list", so a test that
        # does not care about the private/public split does not have to say so.
        self._private_repos = private_repos
        self._data = data or {}
        self._errors = errors or {}
        self._sboms = sboms or {}
        self._runs = runs or {}
        self._workflows = workflows or {}
        self._rate = rate
        self.calls: list[str] = []
        self.get_calls: list[tuple[str, dict]] = []
        self.paginated_calls: list[str] = []
        self.paginated_params: list[tuple[str, dict]] = []

    @property
    def reads_made(self):
        """Every read this double has served. Derived from `calls` rather than
        counted a second time, so the trace's number cannot disagree with the list
        the rest of the suite asserts against."""
        return len(self.calls)

    def get_paginated(self, path, params=None):
        self.calls.append(path)
        self.paginated_calls.append(path)
        self.paginated_params.append((path, dict(params or {})))
        if path.endswith("/teams"):
            return [{"slug": "platform", "name": "platform"}]
        if path == "/user/repos":
            return (self._repos if self._private_repos is None
                    else self._private_repos)
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
        if path == "/user":
            # A token that cannot read its own account: what a fine-grained token
            # without the account scope does, and the reason `sees_private` is
            # allowed to answer "no" rather than fail the run.
            if isinstance(self._auth_login, GitHubError):
                raise self._auth_login
            return {"login": self._auth_login} if self._auth_login else {}
        if path.startswith("/users/") and "/" not in path[len("/users/"):]:
            if isinstance(self._owner_type, GitHubError):
                raise self._owner_type
            return {"login": path[len("/users/"):], "type": self._owner_type}
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
    cfg = {"path": "/github", "owner": "example-org", "kind": "organization",
           "team": "platform",
           "name_prefix": "platform", "token_ref": "env://GITHUB_TOKEN"}
    cfg.update(over)
    return GitHubCheck(**cfg)


def tmp_path_stub():
    """`_extra_from_config` takes the config's base directory; nothing on these
    paths reads it."""
    from pathlib import Path
    return Path(".")


def _repo_id(name):
    """A stable fake repository id, derived from the name so no test carries a
    magic number. GitHub's own are opaque integers, and the point of keying the
    slugs on them is that the **name is not in the key** — so the tests must not
    write the name into an expected slug either."""
    return 1000 + sum(ord(ch) * (i + 1) for i, ch in enumerate(name)) % 9000


def _slug(name, *parts):
    """The slug the code should build for a finding about `name`."""
    return "-".join(str(part) for part in (_repo_id(name), *parts))


def _repo(name, archived=False, fork=False, private=False,
          default_branch="main", repo_id=None):
    return {"id": _repo_id(name) if repo_id is None else repo_id,
            "name": name, "full_name": f"example-org/{name}", "archived": archived,
            "fork": fork, "private": private, "default_branch": default_branch}


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


# --- discovery: which kind of account `org` names ----------------------------
#
# One namespace, two kinds. `/orgs/{login}/repos` is a 404 for a personal account
# — the failure these tests exist for — and `/users/{login}/repos` cannot see a
# personal account's private repositories.

def test_the_declared_kind_decides_the_endpoint_and_is_verified():
    """An organization is discovered through the org endpoint — and the config's
    claim is checked against the account on the way."""
    check = _check()
    fake = FakeClient([_repo("platform-a")])
    check._discover(fake)
    assert "/users/example-org" in fake.calls                 # the verifying call
    assert "/orgs/example-org/teams/platform/repos" in fake.paginated_calls


def test_a_team_github_does_not_have_is_an_answer_and_not_a_shape_we_cannot_read():
    """The team list arrived and parsed; what is wrong is the config. `ANSWERED`, so
    asking again gets the same reply faster — and so the line points at `team:`
    rather than at the payload."""
    check = _check(team="platform-that-is-not-there")
    with pytest.raises(GitHubError) as caught:
        check._discover(FakeClient([_repo("platform-a")]))
    assert caught.value.fault is Fault.ANSWERED
    assert "'platform-that-is-not-there' not found" in str(caught.value)


def test_a_kind_that_disagrees_with_github_is_refused_naming_both():
    """The claim cannot rot: a login mistyped into the other kind's name, or an
    account GitHub converted, would otherwise discover a wrong scope in silence."""
    check = _check(owner="m-31", kind="organization", team="", name_prefix="")
    with pytest.raises(GitHubError) as caught:
        check._discover(FakeClient([], owner_type="User"))
    assert "kind: organization" in str(caught.value)
    assert "user" in str(caught.value)
    assert "'m-31'" in str(caught.value)
    # the same account, declared correctly, discovers
    ok = _check(owner="m-31", kind="user", team="", name_prefix="")
    assert ok._discover(FakeClient([_repo("little-sister")],
                                   owner_type="User")) != []


def test_the_kind_is_required_and_names_the_two_it_accepts():
    with pytest.raises(CheckError) as caught:
        GitHubCheck._extra_from_config(
            {"owner": "m-31", "secrets": {"token": "env://GITHUB_TOKEN"}},
            tmp_path_stub())
    assert "'organization'" in str(caught.value)
    assert "'user'" in str(caught.value)
    with pytest.raises(CheckError) as caught:
        GitHubCheck._extra_from_config(
            {"owner": "m-31", "kind": "person",
             "secrets": {"token": "env://GITHUB_TOKEN"}},
            tmp_path_stub())
    assert "'person'" in str(caught.value)


def test_a_user_account_is_discovered_through_the_user_endpoint():
    """The reported failure: `owner:` naming a person 404'd on `/orgs/…/repos`."""
    check = _check(owner="m-31", kind="user", team="", name_prefix="")
    fake = FakeClient([_repo("little-sister")], owner_type="User")
    assert [r.name for r in check._discover(fake)] == ["little-sister"]
    assert "/users/m-31/repos" in fake.paginated_calls
    assert not [p for p in fake.paginated_calls if p.startswith("/orgs/")]


def test_the_tokens_own_account_sees_its_private_repositories():
    """`/users/{login}/repos` is public-only however privileged the token is, so
    the token's own account is listed through `/user/repos` instead — and that is
    a different set of repositories, not just a different URL."""
    public = [_repo("little-sister")]
    everything = [_repo("little-sister"), _repo("secret-plans")]

    stranger = _check(owner="m-31", kind="user", team="", name_prefix="")
    seen_by_a_stranger = stranger._discover(
        FakeClient(public, owner_type="User", auth_login="somebody-else",
                   private_repos=everything))

    owner = _check(owner="m-31", kind="user", team="", name_prefix="")
    fake = FakeClient(public, owner_type="User", auth_login="m-31",
                      private_repos=everything)
    seen_by_the_owner = owner._discover(fake)

    assert [r.name for r in seen_by_a_stranger] == ["little-sister"]
    assert [r.name for r in seen_by_the_owner] == ["little-sister", "secret-plans"]
    # `affiliation=owner`, or the listing also carries repositories this account
    # only collaborates on — which no `owner:` asked for.
    assert ("/user/repos", {"affiliation": "owner"}) in fake.paginated_params


def test_a_login_that_only_differs_in_case_is_still_the_tokens_own_account():
    check = _check(owner="M-31", kind="user", team="", name_prefix="")
    fake = FakeClient([], owner_type="User", auth_login="m-31",
                      private_repos=[_repo("little-sister")])
    assert [r.name for r in check._discover(fake)] == ["little-sister"]


def test_a_narrower_scope_is_said_on_the_node_not_only_in_the_log():
    """A public-only listing is a smaller scope than the config asked for, so the
    reading says so — otherwise it reads exactly like a complete one."""
    check = _check(owner="m-31", kind="user", team="", name_prefix="")
    repos = check._discover(FakeClient([_repo("little-sister")], owner_type="User",
                                       auth_login="somebody-else"))
    code, reason = check._scope_reading(repos)
    assert code is StatusCode.OK
    assert reason == "1 repository in scope (public only)"

    own = _check(owner="m-31", kind="user", team="", name_prefix="")
    own_repos = own._discover(FakeClient([_repo("little-sister")],
                                         owner_type="User", auth_login="m-31"))
    assert own._scope_reading(own_repos)[1] == "1 repository in scope"


def test_an_empty_user_scope_names_the_account_kind():
    check = _check(owner="m-31", kind="user", team="", name_prefix="")
    repos = check._discover(FakeClient([], owner_type="User", auth_login="m-31"))
    assert check._scope_reading(repos) == (
        StatusCode.WARN, "no repositories in scope (user m-31)")


def test_a_token_that_cannot_read_its_own_account_still_reports():
    """A fine-grained token without the account scope answers "not mine" rather
    than failing the whole check."""
    check = _check(owner="m-31", kind="user", team="", name_prefix="")
    fake = FakeClient([_repo("little-sister")], owner_type="User",
                      auth_login=_answered("HTTP 403 for /user", 403))
    assert [r.name for r in check._discover(fake)] == ["little-sister"]
    assert "/users/m-31/repos" in fake.paginated_calls


def test_a_team_on_a_user_account_is_refused_at_load():
    """A user account has no teams. Declaring the kind is what turns this from a
    discovery failure found by a token on the first run into a config error."""
    with pytest.raises(CheckError) as caught:
        GitHubCheck._extra_from_config(
            {"owner": "m-31", "kind": "user", "team": "platform",
             "secrets": {"token": "env://GITHUB_TOKEN"}},
            tmp_path_stub())
    assert "'platform'" in str(caught.value)
    assert "only an organization has teams" in str(caught.value)


def test_an_account_that_is_neither_is_refused_rather_than_guessed():
    check = _check(owner="something-else", kind="user", team="", name_prefix="")
    with pytest.raises(GitHubError) as caught:
        check._discover(FakeClient([], owner_type="Enterprise"))
    assert "neither a user nor an organization" in str(caught.value)
    # GitHub answered, and the answer names a kind this check has never heard of:
    # unusable, and not a statement about the account. `MALFORMED` rather than
    # `ANSWERED` because the two send a reader to different places — one to their own
    # config, the other to the payload.
    assert caught.value.fault is Fault.MALFORMED


def test_a_repository_row_without_its_structural_fields_is_malformed():
    """`name` and `full_name` are what every aspect addresses a repository by, so a
    row without them means the payload is not what we think it is — and asking again
    returns the same shape. Under the old two-state reading this was
    indistinguishable from a 404, which is the new information in these lines."""
    with pytest.raises(GitHubError) as caught:
        Repo.from_api({"archived": False})
    assert caught.value.fault is Fault.MALFORMED
    assert caught.value.status is None
    assert "unexpected repository payload" in str(caught.value)


def test_a_failed_discovery_is_one_error_on_the_check(monkeypatch):
    """End to end: the 404 in the report becomes a single ERROR reason, not a
    traceback — unchanged behavior, pinned because the failing call moved."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    check = _check(owner="m-31", kind="user", team="", name_prefix="",
                   token_ref="env://GITHUB_TOKEN")
    fake = FakeClient([], owner_type=_answered(
        "HTTP 404 for https://api.github.com/users/m-31", 404))
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    assert result.stored_code is StatusCode.ERROR
    assert result.reason_texts[0].startswith("discovery failed: HTTP 404")


def test_a_transient_discovery_failure_keeps_every_aspect(monkeypatch):
    """The rule of ADR-0002 §2 applied one level up. GitHub failing to answer the
    repository list is *we could not ask*, so the check must not grade the tree
    for it: `WARN` on its own node, and **no children at all**, which is what
    leaves every aspect exactly as the last good run left it (little-sister
    ADR-0007 does not prune)."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    check = _check(owner="m-31", kind="user", team="", name_prefix="",
                   token_ref="env://GITHUB_TOKEN")
    fake = FakeClient([], owner_type=_transient("HTTP 503 for /users/m-31"))
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    assert result.stored_code is StatusCode.WARN
    assert result.children == ()
    assert result.reason_texts[0].startswith(
        "could not ask GitHub for the repository list")
    assert "keeps its last reading" in result.reason_texts[0]


def test_a_discovery_failure_github_answered_is_still_a_defect(monkeypatch):
    """The other half of the same split, and why it is a split. A token that may
    not look is not an outage — it is a deployment somebody has to fix — so it
    stays `ERROR` where a 503 became `WARN`."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    check = _check(owner="m-31", kind="user", team="", name_prefix="",
                   token_ref="env://GITHUB_TOKEN")
    fake = FakeClient([], owner_type=_answered("HTTP 401 for /users/m-31", 401))
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    assert result.stored_code is StatusCode.ERROR
    assert result.reason_texts[0].startswith("discovery failed: HTTP 401")


def test_a_refused_repository_alone_adds_no_coverage_line():
    """The coverage line is about the **could-not-ask** kind only. A repository
    GitHub refused already grades amber on its own line; a second line counting it
    again would report one condition twice and make the number mean two things."""
    check = _check()
    repos = _repos("platform-a", "platform-b")
    fake = FakeClient([_repo("platform-a"), _repo("platform-b")],
                      sboms={"example-org/platform-a": _denied()})
    result = check._sbom_check(fake, repos)
    assert not [e for e in result.reason_entries if e.slug == "read"]
    note = next(e for e in result.reason_entries if e.slug.endswith("unreadable"))
    assert note.code is StatusCode.WARN
    assert result.stored_code is StatusCode.WARN


def test_a_user_account_runs_every_aspect(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    check = _check(owner="m-31", kind="user", team="", name_prefix="",
                   token_ref="env://GITHUB_TOKEN")
    fake = FakeClient([_repo("little-sister")], owner_type="User",
                      auth_login="m-31")
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    assert {child.name for child in result.children} == set(check.ASPECTS)


# --- the `owner:` key --------------------------------------------------------

def test_the_retired_org_key_is_refused_by_name(tmp_path):
    """A hard cut: `org:` named an account login all along, and accepting both
    spellings forever would keep the misleading one alive in every config anybody
    copies. The refusal has to name the new key, or it is just a failure."""
    from little_sister.checks import load_checks
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "github.yaml").write_text(
        "type: github\npath: /github\n"
        "secrets:\n  token: env://GITHUB_TOKEN\norg: example-org\n")
    with pytest.raises(CheckError) as caught:
        load_checks(str(tmp_path))
    assert "'owner'" in str(caught.value)
    assert "renamed" in str(caught.value)


def test_a_config_with_neither_key_says_what_owner_is():
    with pytest.raises(CheckError) as caught:
        GitHubCheck._extra_from_config({}, tmp_path_stub())
    assert "requires an 'owner'" in str(caught.value)


def test_the_retired_org_token_is_refused_rather_than_rendered():
    """An unknown `{token}` is left as-is by the library, so a subnodes text still
    writing `{org}` would reach the dashboard literally. The key can refuse; the
    token has to be refused here or it fails in silence."""
    with pytest.raises(CheckError) as caught:
        GitHubCheck._extra_from_config(
            {"owner": "example-org", "kind": "organization", "secrets": {"token": "env://GITHUB_TOKEN"},
             "subnodes": {"issues": {"about": "Filed under {org}."}}},
            tmp_path_stub())
    assert "{org}" in str(caught.value)
    assert "{owner}" in str(caught.value)
    # and the same text with the new token is accepted
    extra = GitHubCheck._extra_from_config(
        {"owner": "example-org", "kind": "organization", "secrets": {"token": "env://GITHUB_TOKEN"},
         "subnodes": {"issues": {"about": "Filed under {owner}."}}},
        tmp_path_stub())
    assert extra["subnodes"]["issues"]["about"] == "Filed under {owner}."


def test_owner_expands_in_the_aspect_text():
    about = _check(subnodes={"issues": {"about": "Owned by {owner}."}})._meta(
        "issues")[1]
    assert about == "Owned by example-org."


# --- switching an aspect off -------------------------------------------------

def test_an_aspect_can_be_switched_off(monkeypatch):
    """`enabled: false` in the aspect's own block removes the node **and** its
    calls — not a hidden node, which would still be in the JSON and would still
    hold maintenance pins."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    on = _check(team="", name_prefix="", token_ref="env://GITHUB_TOKEN")
    fake_on = FakeClient([_repo("platform-a")])
    monkeypatch.setattr(on, "_make_client", lambda token, deadline=None: fake_on)
    before = {child.name for child in on.run().children}

    off = _check(team="", name_prefix="", token_ref="env://GITHUB_TOKEN",
                 disabled_aspects=("secret_scanning_alerts", "sbom_check"))
    fake_off = FakeClient([_repo("platform-a")])
    monkeypatch.setattr(off, "_make_client", lambda token, deadline=None: fake_off)
    after = {child.name for child in off.run().children}

    assert before - after == {"secret_scanning_alerts", "sbom_check"}
    assert "secret_scanning_alerts" not in after
    # and the calls those two aspects make are gone with them
    assert any("/secret-scanning/alerts" in c for c in fake_on.calls)
    assert not any("/secret-scanning/alerts" in c for c in fake_off.calls)
    assert not any("/dependency-graph/sbom" in c for c in fake_off.calls)


def test_a_switched_off_aspect_shrinks_the_rate_estimate(monkeypatch):
    """The guard's budget is per **endpoint the active aspects read**, so switching
    two off must lower the bar a run has to clear — a run that would have been
    skipped now proceeds."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    # 5 repos: 7 endpoints needs 4×35 = 140 calls, 5 endpoints needs 4×25 = 100.
    # Eight aspects, seven endpoints: the two code-scanning aspects share a read.
    names = ("platform-a", "platform-b", "platform-c", "platform-d", "platform-e")
    repos = [_repo(n) for n in names]

    on = _check(team="", name_prefix="", token_ref="env://GITHUB_TOKEN")
    monkeypatch.setattr(
        on, "_make_client",
        lambda token, deadline=None: FakeClient(repos, rate=(5000, 120, 0)))
    assert on.run().stored_code is StatusCode.WARN          # skipped: 120 < 140

    off = _check(team="", name_prefix="", token_ref="env://GITHUB_TOKEN",
                 disabled_aspects=("issues", "actions"))
    monkeypatch.setattr(
        off, "_make_client",
        lambda token, deadline=None: FakeClient(repos, rate=(5000, 120, 0)))
    result = off.run()
    assert result.stored_code is StatusCode.OK             # ran: 120 > 100
    assert len(result.children) == 6


def test_the_switched_off_aspects_are_named_on_the_node():
    """A disabled aspect leaves no node, so without this line "that aspect is off"
    and "the check is broken" look the same on the page an operator opens."""
    assert "aspects switched off" not in _check().config_summary()
    summary = _check(disabled_aspects=("issues", "actions")).config_summary()
    assert "**aspects switched off:** actions, issues" in summary


def test_enabled_is_read_from_the_aspects_own_block():
    extra = GitHubCheck._extra_from_config(
        {"owner": "example-org", "kind": "organization", "secrets": {"token": "env://GITHUB_TOKEN"},
         "issues": {"enabled": False},
         # the one aspect whose block is not named after it
         "secret_scanning": {"enabled": False}},
        tmp_path_stub())
    assert extra["disabled_aspects"] == ("secret_scanning_alerts", "issues")


def test_a_quoted_boolean_is_a_config_error_not_an_enabled_aspect():
    """`bool("false")` is True, so this would switch the aspect **on** while its
    config says off, and nothing downstream could notice."""
    with pytest.raises(CheckError) as caught:
        GitHubCheck._extra_from_config(
            {"owner": "example-org", "kind": "organization", "secrets": {"token": "env://GITHUB_TOKEN"},
             "issues": {"enabled": "false"}},
            tmp_path_stub())
    assert "issues.enabled" in str(caught.value)


def test_disabling_every_aspect_is_refused():
    config = {"owner": "example-org", "kind": "organization", "secrets": {"token": "env://GITHUB_TOKEN"}}
    for aspect in GitHubCheck.ASPECTS:
        config[GitHubCheck.ASPECT_CONFIG_KEY[aspect]] = {"enabled": False}
    with pytest.raises(CheckError) as caught:
        GitHubCheck._extra_from_config(config, tmp_path_stub())
    assert "every aspect disabled" in str(caught.value)


def test_an_aspect_block_that_is_not_a_mapping_names_itself():
    with pytest.raises(CheckError) as caught:
        GitHubCheck._extra_from_config(
            {"owner": "example-org", "kind": "organization", "secrets": {"token": "env://GITHUB_TOKEN"},
             "secret_scanning": "yes please"},
            tmp_path_stub())
    assert "'secret_scanning' must be a mapping" in str(caught.value)


def test_switching_aspects_off_loads_via_loader(tmp_path):
    from little_sister.checks import load_checks
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "github.yaml").write_text(
        "type: github\npath: /github\n"
        "secrets:\n  token: env://GITHUB_TOKEN\n"
        "owner: m-31\nkind: user\n"
        "secret_scanning:\n  enabled: false\n"
        "code_scanning_security:\n  enabled: false\n"
        "  # a switched-off aspect may still carry its other knobs\n"
        "  severity_map: {critical: ERROR}\n"
        "code_scanning_quality:\n  enabled: false\n")
    check = load_checks(str(tmp_path))[0]
    assert check.active_aspects() == (
        "security_advisories", "actions", "sbom_check", "pull_requests", "issues")


# --- Advanced Security on private repositories -------------------------------
#
# Code scanning and secret scanning are FREE on a public repository and PAID on a
# private one — for organizations and personal accounts alike. So the axis is
# visibility, and the account kind only picks the default.

def test_advanced_security_defaults_off_for_a_user_and_on_for_an_organization():
    for kind, expected in (("user", False), ("organization", True)):
        extra = GitHubCheck._extra_from_config(
            {"owner": "m-31", "kind": kind,
             "secrets": {"token": "env://GITHUB_TOKEN"}},
            tmp_path_stub())
        assert extra["advanced_security_on_private"] is expected
    # and either default is one line to override — a personal account that pays
    # for Advanced Security, or a free organization that does not have it
    for kind, declared in (("user", True), ("organization", False)):
        extra = GitHubCheck._extra_from_config(
            {"owner": "m-31", "kind": kind,
             "advanced_security_on_private": declared,
             "secrets": {"token": "env://GITHUB_TOKEN"}},
            tmp_path_stub())
        assert extra["advanced_security_on_private"] is declared


def test_a_private_repository_is_dropped_from_the_two_scanning_aspects():
    """Not "reported as not enabled" — dropped. A private repo without Advanced
    Security cannot answer these two aspects at all, and forty lines saying so is
    true and useless."""
    repos = [_repo("open-source"), _repo("private-thing", private=True)]

    watched = _check(team="", name_prefix="", advanced_security_on_private=True)
    fake_on = FakeClient(repos)
    watched._secret_scanning_alerts(fake_on, watched._discover(fake_on))

    paid_off = _check(team="", name_prefix="", advanced_security_on_private=False)
    fake_off = FakeClient(repos)
    result = paid_off._secret_scanning_alerts(fake_off, paid_off._discover(fake_off))

    read_on = [c for c in fake_on.calls if "/secret-scanning/" in c]
    read_off = [c for c in fake_off.calls if "/secret-scanning/" in c]
    assert any("private-thing" in c for c in read_on)
    assert read_off == ["/repos/example-org/open-source/secret-scanning/alerts"]
    # the public repository in the same account is still read
    assert result.stored_code is StatusCode.OK


def test_the_dropped_private_repositories_are_named_on_the_node():
    """An aspect that quietly reads half its scope and reports OK is a monitor
    that has stopped monitoring."""
    repos = [_repo("open-source"), _repo("private-thing", private=True),
             _repo("private-other", private=True)]
    check = _check(team="", name_prefix="", advanced_security_on_private=False)
    fake = FakeClient(repos)
    discovered = check._discover(fake)
    for result in (check._secret_scanning_alerts(fake, discovered),
                   check._code_scanning_security(fake, discovered)):
        assert "2 private repositories not read" in result.report
        assert "private-thing" in result.report and "private-other" in result.report
    # nothing to say when nothing was dropped
    on = _check(team="", name_prefix="", advanced_security_on_private=True)
    assert on._secret_scanning_alerts(fake, discovered).report == ""


def test_the_other_aspects_still_read_every_private_repository():
    """Dependabot alerts, pull requests, issues, workflow runs and the SBOM are
    free on a private repository, so the flag must not touch them."""
    repos = [_repo("open-source"), _repo("private-thing", private=True)]
    check = _check(team="", name_prefix="", advanced_security_on_private=False)
    fake = FakeClient(repos)
    discovered = check._discover(fake)
    check._security_advisories(fake, discovered)
    check._issues(fake, discovered)
    check._pull_requests(fake, discovered)
    for suffix in ("/dependabot/alerts", "/issues", "/pulls"):
        assert f"/repos/example-org/private-thing{suffix}" in fake.calls


def test_a_repository_is_private_or_not_by_what_the_api_said():
    assert Repo.from_api(_repo("a", private=True)).private is True
    assert Repo.from_api(_repo("a")).private is False


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
    assert result.stored_code is StatusCode.WARN
    assert len(result.reason_texts) == 1
    assert "platform-a" in result.reason_texts[0]
    assert "(https://gh/pr/1)" in result.reason_texts[0]       # linked to the PR


def test_pull_requests_ok_when_none():
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      data={("example-org/platform-a", "pulls"): []})
    result = check._pull_requests(fake, check._discover(fake))
    assert result.stored_code is StatusCode.OK
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
    assert result.stored_code is StatusCode.OK
    assert bands["critical"].stored_code is StatusCode.OK
    assert bands["high"].stored_code is StatusCode.ERROR
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
    assert medium.stored_code is StatusCode.WARN


def test_dependabot_404_skipped_but_403_surfaced():
    check = _check()
    repos = _repos("platform-a", "platform-b")
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "dependabot"): _answered("not found", 404),
        ("example-org/platform-b", "dependabot"): _answered("forbidden", 403),
    })
    result = check._security_advisories(fake, repos)
    assert result.stored_code is StatusCode.WARN
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
    result = check._code_scanning_security(fake, check._discover(fake))
    high = next(child for child in result.children if child.name == "high")
    assert result.stored_code is StatusCode.OK
    assert high.stored_code is StatusCode.ERROR
    assert "SQL injection" in high.reason_texts[0]
    assert "(https://gh/codescan/1)" in high.reason_texts[0]


def test_code_scanning_not_enabled_is_ok():
    check = _check()
    repos = _repos("platform-a")
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "code_scanning"):
            _answered("no analysis", 404)})
    result = check._code_scanning_security(fake, repos)
    assert result.stored_code is StatusCode.OK
    assert result.reason_texts == []
    assert result.children
    assert all(child.stored_code is StatusCode.OK for child in result.children)


def test_each_security_aspect_has_its_own_configurable_severity_map():
    check = _check(
        dependabot_severities=("low",),
        advisory_severity_map={"low": StatusCode.OK},
        code_scanning_security_map={"low": StatusCode.WARN},
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
    scanning = check._code_scanning_security(fake, _repos("platform-a"))
    assert advisory.children[0].stored_code is StatusCode.OK
    assert next(child for child in scanning.children
                if child.name == "low").stored_code is StatusCode.WARN


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
    assert result.stored_code is StatusCode.ERROR
    assert "github_pat" in result.reason_texts[0]
    assert "(https://gh/secret/1)" in result.reason_texts[0]


def test_secret_scanning_not_enabled_is_flagged():
    check = _check()
    repos = _repos("platform-a")
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "secret_scanning"):
            _answered("secret scanning disabled", 404)})
    result = check._secret_scanning_alerts(fake, repos)
    assert result.stored_code is StatusCode.ERROR
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
                _answered("disabled", 404)},
    )
    result = check._secret_scanning_alerts(fake, repos)
    assert result.stored_code is StatusCode.ERROR
    assert len(result.reason_texts) == 2
    assert any("github_pat" in m for m in result.reason_texts)
    assert any("platform-b" in m and "not enabled" in m for m in result.reason_texts)


def test_secret_scanning_403_is_surfaced_not_flagged():
    check = _check()
    repos = _repos("platform-a")
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "secret_scanning"):
            _answered("forbidden", 403)})
    result = check._secret_scanning_alerts(fake, repos)
    # a permission problem is an error note (WARN), never read as 'not enabled'
    assert result.stored_code is StatusCode.WARN
    assert any("could not read" in m for m in result.reason_texts)
    assert not any("not enabled" in m for m in result.reason_texts)


def test_secret_scanning_require_enabled_false_suppresses_flag():
    check = _check(secret_scanning_require_enabled=False)
    repos = _repos("platform-a")
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "secret_scanning"):
            _answered("secret scanning disabled", 404)})
    result = check._secret_scanning_alerts(fake, repos)
    assert result.stored_code is StatusCode.OK
    assert result.reason_texts == []


# --- sbom_check --------------------------------------------------------------

def test_sbom_missing_is_error():
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _SBOM_EMPTY})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.name == "sbom_check"
    assert result.stored_code is StatusCode.ERROR
    assert "missing SBOM" in result.reason_texts[0]
    assert "network/dependencies)" in result.reason_texts[0]   # linked to the dep graph


def test_sbom_present_is_ok():
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _SBOM_PRESENT})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.stored_code is StatusCode.OK
    assert result.reason_texts == []


def test_sbom_ignore_list_skips_repo():
    check = _check(sbom_ignore=("platform-a",))
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _SBOM_EMPTY})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.stored_code is StatusCode.OK
    assert result.reason_texts == []


def test_sbom_404_counts_as_missing():
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _answered("nope", 404)})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.stored_code is StatusCode.ERROR
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

def _row(children):
    """The children in the order little-sister will draw them.

    The real sort is the library's, once, in the snapshot — `(order, name)`, ADR-0055
    decision 1. This mirrors that key rather than calling it, because the check hands
    back a `CheckResult` and the library sorts a tree node; what is asserted here is
    the row those ranks produce, not the sorting."""
    return [child.name for child in sorted(children, key=lambda c: (c.order, c.name))]


def _scan_alert(number, url, security=None, analysis=None, rule="r"):
    """One code-scanning alert. `security` is `rule.security_severity_level`, which
    GitHub sets only where the rule is a security rule; `analysis` is `rule.severity`,
    which it sets on every alert. A fixture may omit either, because GitHub does."""
    payload = {"id": rule, "description": "d"}
    if security is not None:
        payload["security_severity_level"] = security
    if analysis is not None:
        payload["severity"] = analysis
    return {"number": number, "html_url": url, "rule": payload}


def test_one_field_decides_an_alert_so_the_two_aspects_partition(monkeypatch):
    """The classifier picks **one** field per alert — the security severity where
    GitHub assigned one, the analysis severity otherwise — so an alert appears in
    exactly one of the two aspects. Double-counting would make every dashboard
    number wrong twice over."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "code_scanning"): [
            _scan_alert(1, "https://x/1", security="high", analysis="error"),
            _scan_alert(2, "https://x/2", analysis="warning"),
        ]})
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    security = next(c for c in result.children if c.name == "code_scanning_security")
    quality = next(c for c in result.children if c.name == "code_scanning_quality")

    def lines(aspect):
        return {band.name: len(band.reason_entries) for band in aspect.children
                if band.reason_entries}

    # alert 1 has both fields and is filed by the security one, only
    assert lines(security) == {"high": 1}
    assert lines(quality) == {"warning": 1}


def test_the_three_unreachable_bands_are_gone(monkeypatch):
    """What this split was for. `error`, `warning` and `note` used to render under
    `code_scanning_alerts` because the shipped map named them, while the classifier
    read one field that could never produce them: watched, permanently green,
    permanently unreachable. Each scale now declares only what it can fill."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    monkeypatch.setattr(check, "_make_client",
                        lambda token, deadline=None: FakeClient([_repo("a")]))
    result = check.run()
    security = next(c for c in result.children if c.name == "code_scanning_security")
    quality = next(c for c in result.children if c.name == "code_scanning_quality")
    assert _row(security.children) == ["critical", "high", "medium", "low"]
    assert _row(quality.children) == ["error", "warning", "note"]


def test_a_security_finding_and_a_lint_note_are_graded_differently(monkeypatch):
    """The other half of the reason. One map forced one answer onto both scales, and
    the shipped default answered `ERROR` for everything — including a `note`, which
    is CodeQL telling you about a naming convention."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "code_scanning"): [
            _scan_alert(1, "https://x/1", security="low"),
            _scan_alert(2, "https://x/2", analysis="note"),
        ]})
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    security = next(c for c in result.children if c.name == "code_scanning_security")
    quality = next(c for c in result.children if c.name == "code_scanning_quality")
    # the aspect container defers to its bands, so the codes are on the bands
    low = next(c for c in security.children if c.name == 'low')
    note = next(c for c in quality.children if c.name == 'note')
    assert low.stored_code is StatusCode.ERROR    # the mildest security band, still red
    assert note.stored_code is StatusCode.OK      # a note with findings is not news
    assert note.reason_entries                    # and it really did find one
    assert check.code_scanning_quality_map["error"] is StatusCode.WARN


def test_an_alert_with_neither_severity_is_a_band_nobody_declared(monkeypatch):
    """GitHub may hand back an alert with no severity of either kind. It goes to
    quality's `none`, which no default map names — so it renders because it actually
    happened, not because somebody watched for it, and says so as an undeclared
    band rather than as a silent zero."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "code_scanning"): [_scan_alert(1, "https://x/1")]})
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    quality = next(c for c in result.children if c.name == "code_scanning_quality")
    assert _row(quality.children) == ["error", "warning", "note", "none"]
    band = next(c for c in quality.children if c.name == "none")
    assert "no `severity_map` entry, so the fallback applies" in band.config


def test_the_split_costs_no_extra_request(monkeypatch):
    """Two aspects, one payload. The read is memoized for the run, so the second
    aspect asks GitHub nothing — and the pre-run rate guard counts distinct
    endpoints rather than aspects, or it would reserve budget for a call the run
    never makes."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    fake = FakeClient([_repo("platform-a"), _repo("platform-b")])
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    check.run()
    scans = [path for path in fake.calls if "code-scanning" in path]
    assert len(scans) == 2                     # one per repository, not two
    assert len(check.ASPECTS) == 8
    assert len({check.ASPECT_ENDPOINT[a] for a in check.ASPECTS}) == 7


def test_one_unreadable_repository_is_counted_once_not_once_per_aspect(monkeypatch):
    """The shared payload's other hazard. Both code-scanning aspects read one
    `_Coverage`, and the check's node reports how many repositories could not be
    read this run — so counting per aspect would report two failures where one read
    was attempted, which is the double-count ADR-0002 kept off the aspects."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check(disabled_aspects=("pull_requests", "security_advisories",
                                     "secret_scanning_alerts", "sbom_check",
                                     "actions", "issues"))
    fake = FakeClient([_repo("platform-a")], errors={
        ("example-org/platform-a", "code_scanning"): _transient("boom")})
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    node = " | ".join(result.reason_texts)
    assert "1 repository read could not be completed this run" in node
    assert "2 repository reads" not in node


def _titles(aspect):
    return [(c.title, c.name) for c in sorted(aspect.children,
                                              key=lambda c: (c.order, c.name))]


def _aspects(check, monkeypatch, **fake):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    client = FakeClient([_repo("platform-a")], **fake)
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: client)
    return {c.name: c for c in check.run().children}


def _with_subnodes(**block):
    return GitHubCheck._extra_from_config(
        {"owner": "o", "kind": "user", "secrets": {"token": "env://T"},
         "subnodes": {name: {"about": "ours"} for name in block}},
        tmp_path_stub())


def test_a_subnodes_key_naming_a_band_is_refused():
    """The defect this closes: a key naming nothing was accepted and did nothing —
    a paragraph a deployment wrote, loaded without complaint, and never drawn. The
    likeliest mistake is a **band**, because the bands are what an operator looks at,
    and the message has to say where a band's text actually goes."""
    with pytest.raises(CheckError) as caught:
        _with_subnodes(critical=1)
    message = str(caught.value)
    assert "subnodes.critical" in message
    assert "not the severity bands beneath them" in message
    assert "nodes.yaml" in message
    # and it lists what would have worked
    assert "code_scanning_security" in message and "sbom_check" in message


def test_a_subnodes_key_naming_the_retired_aspect_says_what_happened():
    """*This key is wrong* is not enough: the text under it is a deployment's own
    policy paragraph, and a bare rejection leaves nowhere to put it. This is the
    case that actually happened — the split renamed an aspect a config had text
    for."""
    with pytest.raises(CheckError) as caught:
        _with_subnodes(code_scanning_alerts=1)
    message = str(caught.value)
    assert "no longer exists" in message
    assert "code_scanning_security" in message and "code_scanning_quality" in message
    assert "or under both" in message


def test_the_config_block_name_is_not_the_aspect_name_and_is_refused():
    """`secret_scanning_alerts` reads its knobs from a `secret_scanning:` block —
    the one place this package's two vocabularies disagree (`ASPECT_CONFIG_KEY`), and
    therefore the mistake most easily made twice."""
    with pytest.raises(CheckError) as caught:
        _with_subnodes(secret_scanning=1)
    assert "secret_scanning_alerts" in str(caught.value)


def test_every_aspect_this_check_reports_is_a_usable_subnodes_key():
    """The other half of a refusal: nothing that should work may stop working. The
    roster is closed, which is what makes refusing safe — so the roster is what the
    test iterates, not a list written out beside it."""
    extra = _with_subnodes(**dict.fromkeys(GitHubCheck.ASPECTS, 1))
    assert sorted(extra["subnodes"]) == sorted(GitHubCheck.ASPECTS)


def test_a_switched_off_aspect_may_still_carry_its_text():
    """Validated against the roster, not against what is switched on. Otherwise
    `enabled: false` would silently require deleting the paragraph too, and turning
    the aspect back on would mean writing it again."""
    extra = GitHubCheck._extra_from_config(
        {"owner": "o", "kind": "user", "secrets": {"token": "env://T"},
         "issues": {"enabled": False},
         "subnodes": {"issues": {"about": "ours"}}},
        tmp_path_stub())
    assert extra["disabled_aspects"] == ("issues",)
    assert extra["subnodes"]["issues"]["about"] == "ours"


def test_every_band_wears_the_colour_its_severity_is_named_with(monkeypatch):
    """Three rows, one table. The word `Critical` cost a chip's width and said what
    the name beside it already said; the circle says how bad it is instead."""
    rows = _aspects(_check(), monkeypatch)
    assert _titles(rows["security_advisories"]) == [("🔴", "critical"), ("🟠", "high")]
    assert _titles(rows["code_scanning_security"]) == [
        ("🔴", "critical"), ("🟠", "high"), ("🟡", "medium"), ("🔵", "low")]
    assert _titles(rows["code_scanning_quality"]) == [
        ("🟠", "error"), ("🟡", "warning"), ("🔵", "note")]


def test_the_same_severity_wears_the_same_circle_whatever_it_is_ranked(monkeypatch):
    """The decision, and this package is the reason for it. A rank here is a
    *deployment's* tuple — `security_advisories` is built with
    `order=self.dependabot_severities` — so an operator who watches `high` and
    `medium` gives `high` rank **1** in that aspect while it stays rank **2** under
    code scanning. Derive the circle from the rank and that operator's dashboard
    shows one `high` 🔴 and the other 🟠, which is not a thing anyone can read."""
    rows = _aspects(_check(dependabot_severities=("high", "medium")), monkeypatch)
    advisories = {c.name: c for c in rows["security_advisories"].children}
    scanning = {c.name: c for c in rows["code_scanning_security"].children}

    assert advisories["high"].order == 1 and scanning["high"].order == 2
    assert advisories["high"].title == scanning["high"].title == "🟠"
    assert advisories["medium"].title == scanning["medium"].title == "🟡"


def test_a_severity_this_package_does_not_name_gets_a_question_mark(monkeypatch):
    """Reachable today, not hypothetical: an alert carrying neither severity lands in
    quality's `none`. A band with no colour must not borrow one."""
    rows = _aspects(_check(), monkeypatch, data={
        ("example-org/platform-a", "code_scanning"): [
            _scan_alert(1, "https://x/1")]})
    assert _titles(rows["code_scanning_quality"])[-1] == ("❓", "none")
    assert "❓" not in BAND_GLYPHS.values()


def test_the_two_scales_repeat_colours_across_rows_and_never_within_one(monkeypatch):
    """`error` and `high` are comparable rungs of two scales GitHub keeps apart, so
    they wear the same circle — and since the split they are never in one row. Two
    🟠 in a single row would be the bug this asserts against; two aspects that each
    have one is the model."""
    rows = _aspects(_check(), monkeypatch)
    assert BAND_GLYPHS["error"] == BAND_GLYPHS["high"] == "🟠"
    for name in ("security_advisories", "code_scanning_security",
                 "code_scanning_quality"):
        drawn = [title for title, _ in _titles(rows[name])]
        assert len(drawn) == len(set(drawn)), f"{name} draws one colour twice"


def test_the_circle_survives_the_folding_rule_and_keeps_its_word(monkeypatch):
    """little-sister drops a title that only repeats its name, which is what used to
    happen to `Critical`; a circle repeats nothing and survives. And where a surface
    draws a title *instead of* the name it now draws both (little-sister ADR-0061),
    so the word is never lost — which is what makes the swap defensible at all."""
    from little_sister.titles import label_parts, shown_title

    rows = _aspects(_check(), monkeypatch)
    band = next(c for c in rows["code_scanning_security"].children
                if c.name == "critical")
    assert shown_title(band.name, band.title) == "🔴"
    assert label_parts(band.name, band.title) == ("critical", "🔴")


def test_each_scale_ranks_from_one_with_no_leftover(monkeypatch):
    """Four and three, both dense. If either aspect declared the *other* scale its
    bands would still render — a `severity_map` names them, so they enter as the
    stated-by-map tier — and the row would read identically while every rank was
    wrong. The ranks are the claim here, not the order they happen to produce."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    monkeypatch.setattr(check, "_make_client",
                        lambda token, deadline=None: FakeClient([_repo("a")]))
    result = check.run()
    security = next(c for c in result.children if c.name == "code_scanning_security")
    quality = next(c for c in result.children if c.name == "code_scanning_quality")
    assert [c.order for c in security.children] == [1, 2, 3, 4]
    assert [c.order for c in quality.children] == [1, 2, 3]


def test_the_rate_guard_reserves_for_reads_and_not_for_aspects(monkeypatch):
    """The split added an aspect and no request. Counting aspects would reserve
    5 × 8 rather than 5 × 7 per run, and the guard would skip a run the budget
    actually affords — reporting nothing, on a number that was never true."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    names = ("platform-a", "platform-b", "platform-c", "platform-d", "platform-e")
    repos = [_repo(n) for n in names]
    # 5 repos × 7 endpoints × 4 = 140 affordable; × 8 aspects would demand 160.
    check = _check(team="", name_prefix="", token_ref="env://GITHUB_TOKEN")
    monkeypatch.setattr(
        check, "_make_client",
        lambda token, deadline=None: FakeClient(repos, rate=(5000, 150, 0)))
    assert check.run().stored_code is StatusCode.OK          # 150 ≥ 140, so it runs


def test_the_old_code_scanning_block_is_refused_at_load():
    """A split configuration key, refused rather than ignored — the same shape as
    `org:` → `owner:`. Dropping it silently would take a deployment's whole grading
    with it and say so only as bands that suddenly read red."""
    with pytest.raises(CheckError) as caught:
        GitHubCheck._extra_from_config(
            {"owner": "o", "kind": "user", "secrets": {"token": "env://T"},
             "code_scanning_alerts": {"severity_map": {"critical": "ERROR"}}},
            tmp_path_stub())
    message = str(caught.value)
    assert "code_scanning_alerts" in message
    assert "code_scanning_security" in message and "code_scanning_quality" in message
    # and it says what to do, not only that something is wrong
    assert "severity_map" in message and "enabled" in message


def test_the_aspect_row_is_in_an_order_somebody_chose(monkeypatch):
    """It was sorted by the alphabet — `actions` first, `sbom_check` wedged between
    `pull_requests` and `secret_scanning_alerts` — because nothing carried `ASPECTS`
    to the screen and that tuple was written for a rate estimate anyway. The order is
    now a decision: worst-first by what a finding costs, hygiene after security."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    monkeypatch.setattr(check, "_make_client",
                        lambda token, deadline=None: FakeClient([_repo("platform-a")]))
    result = check.run()
    assert _row(result.children) == [
        "secret_scanning_alerts", "security_advisories", "code_scanning_security",
        "actions", "sbom_check", "code_scanning_quality", "pull_requests", "issues"]
    assert _row(result.children) != sorted(c.name for c in result.children)


def test_an_aspects_rank_starts_at_one_and_follows_the_roster():
    """`0` is not a neutral value: it is the rank the unranked carry and sorts before
    every positive one (little-sister ADR-0055 decision 4), so an aspect left at the
    default would move to the front of the row rather than stay where it was."""
    assert [GitHubCheck.aspect_rank(name) for name in GitHubCheck.ASPECTS] == [
        1, 2, 3, 4, 5, 6, 7, 8]
    assert GitHubCheck.aspect_rank(GitHubCheck.ASPECTS[0]) != 0


def test_switching_an_aspect_off_leaves_a_gap_and_the_row_still_reads(monkeypatch):
    """Ranks are the roster's, not the run's, so a disabled aspect leaves a hole
    nothing sorts into — rather than renumbering the survivors, which would move a
    row for a config change that has nothing to do with severity."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check(disabled_aspects=("security_advisories", "actions"))
    monkeypatch.setattr(check, "_make_client",
                        lambda token, deadline=None: FakeClient([_repo("platform-a")]))
    result = check.run()
    assert _row(result.children) == [
        "secret_scanning_alerts", "code_scanning_security", "sbom_check",
        "code_scanning_quality", "pull_requests", "issues"]
    ranks = {c.name: c.order for c in result.children}
    assert ranks["code_scanning_security"] == 3 and ranks["sbom_check"] == 5


def test_a_bands_rank_is_the_aspects_own_tuple_not_the_module_constant(monkeypatch):
    """`security_advisories` is built with `order=self.dependabot_severities`, an
    operator-configured tuple, so that aspect's sequence is a deployment's decision.
    Ranking from `SECURITY_SEVERITY_ORDER` would silently disagree with the order the
    bands were built in — which is the one thing the rank exists to stop."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check(dependabot_severities=("high", "critical"))   # deliberately not ours
    monkeypatch.setattr(check, "_make_client",
                        lambda token, deadline=None: FakeClient([_repo("platform-a")]))
    result = check.run()
    advisories = next(c for c in result.children if c.name == "security_advisories")
    assert _row(advisories.children) == ["high", "critical"]


def test_a_band_only_a_severity_map_names_ranks_after_the_declared_ones(monkeypatch):
    """A `severity_map` is a statement too — this package's default or a deployment's —
    so a band it names keeps a rank of its own. It just comes after everything the
    aspect declared, because that tuple is the sequence somebody wrote down first."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check(code_scanning_security_map={"zeta": StatusCode.WARN})
    monkeypatch.setattr(check, "_make_client",
                        lambda token, deadline=None: FakeClient([_repo("platform-a")]))
    result = check.run()
    scanning = next(c for c in result.children if c.name == "code_scanning_security")
    assert _row(scanning.children)[-1] == "zeta"
    ranks = {c.name: c.order for c in scanning.children}
    assert ranks["zeta"] == len(SECURITY_SEVERITY_ORDER) + 1


def test_bands_nobody_stated_share_one_rank_and_sort_by_name(monkeypatch):
    """These arrive from the payload in whatever order it had, which is nobody's
    decision — so they must not freeze that order into a rank. One shared rank after
    every stated band, and the name half of the sort key does the rest."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "code_scanning"): [
            {"number": 1, "html_url": "https://x/1", "rule": {
                "id": "r1", "description": "d",
                "security_severity_level": "zeta"}},
            {"number": 2, "html_url": "https://x/2", "rule": {
                "id": "r2", "description": "d",
                "security_severity_level": "alpha"}},
        ]})
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    scanning = next(c for c in result.children if c.name == "code_scanning_security")
    ranks = {c.name: c.order for c in scanning.children}
    assert ranks["alpha"] == ranks["zeta"] == len(SECURITY_SEVERITY_ORDER) + 1
    assert _row(scanning.children)[-2:] == ["alpha", "zeta"]


def test_run_returns_all_aspect_leaves(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    assert check.token == "x"          # resolved once, at construction
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "pulls"): [{"title": "Fix", "user": {"login": "a"}}],
    })
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    assert [c.name for c in result.children] == [
        "secret_scanning_alerts", "security_advisories", "code_scanning_security",
        "actions", "sbom_check", "code_scanning_quality", "pull_requests", "issues"]
    pulls = next(c for c in result.children if c.name == "pull_requests")
    assert pulls.stored_code is StatusCode.WARN
    assert result.stored_code is StatusCode.OK
    assert result.reason_texts == ["1 repository in scope"]
    assert result.report == (
        "- [platform-a](https://github.com/example-org/platform-a)")


def test_run_warns_on_empty_scope_but_keeps_every_aspect(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check()
    fake = FakeClient([])
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    assert result.stored_code is StatusCode.WARN
    assert result.reason_texts == [
        'no repositories in scope (organization example-org, team platform,'
        ' prefix "platform")']
    assert result.report == ""
    assert [child.name for child in result.children] == list(check.ASPECTS)


def test_run_warns_below_the_configured_repository_minimum(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    check = _check(expect_min_repos=3)
    fake = FakeClient([_repo("platform-a"), _repo("platform-b")])
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    assert result.stored_code is StatusCode.WARN
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
    monkeypatch.setattr(check, "_make_client", lambda token, deadline=None: fake)
    result = check.run()
    assert result.stored_code is StatusCode.WARN
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
        "owner: example-org\n"
        "kind: organization\n"
        "team: platform\n"
        "name_prefix: platform\n"
        "expect_min_repos: 3\n"
        "pull_requests:\n"
        "  ignore_title_prefixes: ['[PLATFORM-']\n"
        "security_advisories:\n"
        "  severities: [critical, high]\n"
        "  severity_map: {critical: ERROR, high: WARN}\n"
        "code_scanning_security:\n"
        "  severity_map: {critical: ERROR, low: WARN}\n"
        "code_scanning_quality:\n"
        "  severity_map: {note: WARN}\n"
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
    assert checks[0].code_scanning_security_map["low"] is StatusCode.WARN
    assert checks[0].code_scanning_quality_map["note"] is StatusCode.WARN
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
    assert result.stored_code is StatusCode.WARN
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
    assert result.stored_code is StatusCode.OK
    assert result.reason_texts == []


def test_issues_ignore_list_skips_repo():
    check = _check(issues_ignore=("platform-a",))
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "issues"): [_issue(7)],
    })
    result = check._issues(fake, check._discover(fake))
    assert result.stored_code is StatusCode.OK
    assert result.reason_texts == []


def test_issues_disabled_repo_is_flagged_not_read_as_none():
    check = _check()
    fake = FakeClient([_repo("platform-a")], errors={
        ("example-org/platform-a", "issues"): _answered("not found", 404)})
    result = check._issues(fake, check._discover(fake))
    assert result.stored_code is StatusCode.WARN
    assert "issues are disabled" in result.reason_texts[0]


def test_issues_other_error_is_surfaced_as_a_note():
    check = _check()
    fake = FakeClient([_repo("platform-a")], errors={
        ("example-org/platform-a", "issues"): _answered("forbidden", 403)})
    result = check._issues(fake, check._discover(fake))
    assert result.stored_code is StatusCode.WARN
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
    # expanding {owner} / {team} (little-sister ADR-0025).
    check = _check(subnodes={
        "security_advisories": {
            "title": "Dependabot advisories",
            "about": "See https://github.com/orgs/{owner}/security/alerts/dependabot"
                     "?q=is:open+team:{team}.",
        }})
    fake = FakeClient([_repo("platform-a")],
                      data={("example-org/platform-a", "dependabot"): []})
    result = check._security_advisories(fake, check._discover(fake))
    assert result.title == "Dependabot advisories"
    assert "orgs/example-org/" in result.about                     # {owner}
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
    assert "{" not in _check()._meta("security_advisories")[1]
    # the token that does differ per team is in the security aspects' links
    assert "team%3Apayments" in _check(team="payments")._meta(
        "security_advisories")[1]


def test_the_security_overview_link_is_dropped_for_a_user_account():
    """`github.com/orgs/<login>/security/…` is an organization page and 404s for a
    personal account, so the aspect ships without a link rather than with a dead
    one. The org form is the "before" that proves the user form changed."""
    org = _check(team="", name_prefix="")
    org._discover(FakeClient([]))
    assert "github.com/orgs/example-org/security/alerts/dependabot" in (
        org._meta("security_advisories")[1])

    user = _check(owner="m-31", kind="user", team="", name_prefix="")
    user._discover(FakeClient([], owner_type="User", auth_login="m-31"))
    for aspect in ("security_advisories", "code_scanning_security",
                   "secret_scanning_alerts"):
        about = user._meta(aspect)[1]
        assert "github.com/orgs/" not in about
        assert "{" not in about            # and no unexpanded token in its place


def test_the_security_overview_link_omits_an_absent_team():
    """`team:` with an empty value is a filter that matches nothing, not an absent
    filter — so a check with no team must not write the clause at all."""
    with_team = _check(team="platform")._meta("code_scanning_security")[1]
    without_team = _check(team="")._meta("code_scanning_security")[1]
    assert "team%3Aplatform" in with_team
    assert "team" not in without_team.split("security/alerts/")[1].split(")")[0]


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
        "owner: example-org\n"
        "kind: organization\n"
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
    assert [e.slug for e in result.reason_entries] == [
        _slug("platform-a", "pr", 42), _slug("platform-b", "pr", 7)]


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


def test_a_repository_rename_does_not_move_a_pin():
    """The property the id exists for. GitHub lets an owner rename a repository at
    any time and the findings under it are the same findings, so their keys must not
    change — keyed on the name, a rename silently re-keyed every line about that
    repository and orphaned every pin held on one."""
    check = _check()
    pr = {"title": "Ship it", "number": 42, "html_url": "https://gh/pr/42"}

    def leaf(name):
        row = _repo(name, repo_id=4242)
        return check._pull_requests(
            FakeClient([row], data={(f"example-org/{name}", "pulls"): [pr]}),
            [Repo.from_api(row)])

    before, after = leaf("platform-a"), leaf("platform-renamed")
    assert before.reason_entries[0].slug == "4242-pr-42"
    assert after.reason_entries[0].slug == before.reason_entries[0].slug
    # …and the half a person reads *did* follow the rename, which is the whole
    # bargain: the key is opaque and stable, the text is current and readable.
    assert "platform-a" in before.reason_entries[0].text
    assert "platform-renamed" in after.reason_entries[0].text


def test_a_repository_name_is_never_in_a_slug():
    """Stated as its own claim because it is what a reader checks by eye, and
    because every aspect has to obey it, not just the one under test above."""
    check = _check(actions_all_branches=True)
    row = _repo("platform-a")
    runs = {"example-org/platform-a": {"workflow_runs": [
        _wf_run(name="ci", branch="main", conclusion="failure", wf_id=5)]}}
    leaves = [
        check._pull_requests(FakeClient([row], data={
            ("example-org/platform-a", "pulls"): [
                {"title": "x", "number": 1, "html_url": "https://gh/pr/1"}]}),
            [Repo.from_api(row)]),
        check._sbom_check(
            FakeClient([row], sboms={"example-org/platform-a": _SBOM_EMPTY}),
            [Repo.from_api(row)]),
        check._actions(FakeClient([row], runs=runs), [Repo.from_api(row)]),
    ]
    slugs = [entry.slug for leaf in leaves for entry in leaf.reason_entries]
    assert slugs, "no lines to check"
    assert not [s for s in slugs if "platform-a" in s], slugs


def test_a_workflow_line_is_keyed_on_the_id_too():
    """`actions` builds its slug directly rather than through `_entry_slug`, so it is
    the site that quietly keeps the old shape when only the helper is changed."""
    check = _check(actions_all_branches=True)

    def leaf(name):
        row = _repo(name, repo_id=4242)
        runs = {f"example-org/{name}": {"workflow_runs": [
            _wf_run(name="ci", branch="main", conclusion="failure", wf_id=5)]}}
        return check._actions(FakeClient([row], runs=runs), [Repo.from_api(row)])

    assert leaf("platform-a").reason_entries[0].slug == "4242-workflow-5-main"
    assert (leaf("platform-renamed").reason_entries[0].slug
            == leaf("platform-a").reason_entries[0].slug)


def test_a_repository_row_without_an_id_is_a_read_failure():
    """`id` is structural now: without it there is nothing to key a line on, and a
    check that guessed would hand every pin to the wrong finding. So it is refused
    the way a missing `name` already was — MALFORMED, because asking again returns
    the same shape."""
    with pytest.raises(GitHubError) as caught:
        Repo.from_api({"name": "platform-a",
                       "full_name": "example-org/platform-a"})
    assert caught.value.fault is Fault.MALFORMED
    assert "unexpected repository payload" in str(caught.value)


def test_the_about_text_carries_the_grading_in_force_not_the_key():
    """The knob's *name* is no use to a reader on a dashboard — the value lives in
    this package's source, which is exactly where they cannot look. So the shipped
    text states the mapping the check is actually using."""
    check = _check()
    about = check._meta("security_advisories")[1]
    assert "graded **ERROR** for every band" in about
    assert "severity_map" not in about          # the key, withheld on purpose


def test_a_deployments_own_map_is_what_its_about_text_shows():
    """The reason it is a token and not a sentence: an override has to reach the
    text, or the package would be publishing somebody else's grading as theirs."""
    check = _check(advisory_severity_map={"critical": StatusCode.WARN})
    about = check._meta("security_advisories")[1]
    assert "`critical` → **WARN**" in about


def test_one_answer_for_every_band_is_said_once():
    """Eight identical arrows are a wall a reader skips, and skipping is how the
    setting stays invisible — which is the complaint this text answers."""
    check = _check()
    about = check._meta("code_scanning_security")[1]
    assert "**ERROR** for every band" in about
    assert "`note` → " not in about


def test_a_band_leaf_says_what_it_is_graded_as():
    """A band's own page showed a Time card and nothing about the grading that
    produced what is on it (little-sister ADR-0044 decision 1: the field, the
    renderer and the rule all existed; this check never filled them)."""
    check = _check()
    repos = _repos("platform-a")
    fake = FakeClient([_repo("platform-a")], data={
        ("example-org/platform-a", "dependabot"): [
            {"number": 1, "html_url": "https://gh/a/1",
             "security_advisory": {"summary": "boom"},
             "security_vulnerability": {"severity": "critical"}}]})
    result = check._security_advisories(fake, repos)
    critical = next(c for c in result.children if c.name == "critical")
    assert "ERROR" in critical.config
    assert "when empty" in critical.config


def test_a_paused_run_says_so_on_the_node():
    """A pause is correct behavior, so it grades nothing — but a run that slept a
    minute was indistinguishable from a slow one, which is how a throttle hides."""
    check = _check()
    code, reason = check._node_reading(StatusCode.OK, "1 repository in scope", "", 47.0)
    assert any("paused 47s for a GitHub rate limit" in line for line in reason)
    assert code is StatusCode.OK               # waiting when asked is not a fault


def test_a_run_that_did_not_pause_says_nothing():
    """The line appears only when there was a pause — a sentence on every run is a
    sentence nobody reads."""
    check = _check()
    _code, reason = check._node_reading(StatusCode.OK, "1 repository in scope", "", 0.0)
    assert not [line for line in reason if "paused" in line]


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
    assert [e.slug for e in result.reason_entries] == [
        _slug("platform-a", "issue", 7), _slug("platform-a", "issue", 9)]


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
    assert first.reason_entries[0].slug == _slug(
        "platform-a", "workflow", 5, "release-1.2")
    assert second.reason_entries[0].slug == first.reason_entries[0].slug


def test_scanning_switched_off_is_its_own_kind_of_entry():
    """"Scanning is off" is a different condition from "an alert fired", so pinning
    the one must not need the other's number."""
    check = _check()
    fake = FakeClient([_repo("platform-a")], errors={
        ("example-org/platform-a", "secret_scanning"): _answered("off", 404)})
    result = check._secret_scanning_alerts(fake, check._discover(fake))
    assert [e.slug for e in result.reason_entries] == \
        [_slug("platform-a", "secret-scanning-off")]


def test_a_read_failure_is_keyed_too():
    """A repository that cannot be read is a condition somebody may be fixing — it
    should be pinnable without silencing the alerts that did come back."""
    check = _check()
    fake = FakeClient([_repo("platform-a")], errors={
        ("example-org/platform-a", "secret_scanning"): GitHubError("boom", status=500,
                          fault=Fault.TRANSIENT)})
    result = check._secret_scanning_alerts(fake, check._discover(fake))
    assert [e.slug for e in result.reason_entries] == [
        _slug("platform-a", "unreadable"), "read"]


def test_pull_requests_isolates_per_repository_like_every_other_aspect():
    """**Replaces** `test_an_aspect_that_could_not_run_at_all_stays_prose`, whose
    claim this slice overturns (ADR-0002).

    That test pinned the old shape: the whole loop sat inside one `try`, so the
    first repository's failure returned the aspect as ERROR prose and the other
    repositories' open pull requests were never looked for at all. One
    repository's bad minute must not cost the findings about the rest."""
    check = _check()
    fake = FakeClient(
        [_repo("platform-a"), _repo("platform-b")],
        data={("example-org/platform-b", "pulls"): [
            {"title": "fix the thing", "number": 7,
             "user": {"login": "someone"}, "html_url": "https://gh/pr/7"}]},
        errors={("example-org/platform-a", "pulls"):
                GitHubError("boom", status=500,
                          fault=Fault.TRANSIENT)})
    result = check._pull_requests(fake, check._discover(fake))
    slugs = [entry.slug for entry in result.reason_entries]
    # b's finding survived a's failure — which is the whole point
    assert _slug("platform-b", "pr", 7) in slugs
    assert _slug("platform-a", "unreadable") in slugs
    assert result.members


# --- A read failure is not a finding about the repository (ADR-0002) ----------
#
# Each test below is written from a sentence the ADR, the README or the example
# config states, with the values that sentence is about.

def test_a_5xx_no_longer_paints_the_repository_amber():
    """The sentence this whole item exists for. `sbom_check` answers *does this
    repository have a dependency graph?* — and a 500 is not an answer to it, so
    the line says the asking failed and the aspect stays green for the
    repositories that did answer."""
    check = _check()
    repos = [_repo("platform-a"), _repo("platform-b")]
    fake = FakeClient(repos, sboms={"example-org/platform-a": _transient()})
    result = check._sbom_check(fake, check._discover(fake))
    lines = {e.slug: e for e in result.reason_entries}
    assert lines[_slug("platform-a", "unreadable")].code is StatusCode.UNDEFINED
    assert "could not ask GitHub" in lines[_slug("platform-a", "unreadable")].text
    # The repository's own line grades nothing — which is the sentence. What is
    # amber is the aspect's *coverage*, said once on its own line and nowhere
    # near platform-a's name.
    assert lines["read"].code is StatusCode.WARN
    assert result.stored_code is StatusCode.WARN


def test_the_split_is_by_status_and_a_permission_error_still_grades():
    """"403 and 401 are about the token and belong to the repository line they
    have today." A 403 *is* an answer about this repository, and somebody has to
    act on it."""
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _denied()})
    result = check._sbom_check(fake, check._discover(fake))
    line = result.reason_entries[0]
    assert line.code is StatusCode.WARN
    assert "could not read" in line.text and "could not ask" not in line.text
    assert result.stored_code is StatusCode.WARN


def test_a_404_still_means_missing():
    """Unchanged, and the ADR says so explicitly: a 404 is GitHub answering that
    the thing is absent, which is a finding about the repository."""
    check = _check()
    fake = FakeClient([_repo("platform-a")], sboms={
        "example-org/platform-a": _answered("nope", 404)})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.reason_entries[0].slug == _slug("platform-a", "sbom")
    assert result.stored_code is StatusCode.ERROR


def test_the_repositories_that_were_read_are_not_erased_by_the_one_that_was_not():
    """The trap `_Coverage.lines` exists for. An entry set of nothing but
    UNDEFINED derives UNDEFINED, so a leaf where one repository was unreachable
    and the others were clean would go **grey** — the clean readings thrown away
    with the unknown one. The `read` line is those clean readings."""
    check = _check()
    repos = [_repo(f"platform-{c}") for c in "abc"]
    fake = FakeClient(repos, sboms={"example-org/platform-a": _transient()})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.stored_code is StatusCode.WARN         # not UNDEFINED
    read = [e for e in result.reason_entries if e.slug == "read"]
    assert len(read) == 1
    assert read[0].code is StatusCode.WARN
    assert read[0].text == "GitHub did not answer for 1 of 3 repositories"


def test_an_aspect_that_read_nothing_at_all_is_amber_not_grey():
    """The other side of the same rule. With no repository read there is no clean
    reading to state — but the aspect must not be `OK`, which it has not earned,
    *nor* `UNDEFINED`, which the roll-up ignores and which therefore reads as
    silence. It is amber on its coverage, and every repository line still grades
    nothing."""
    check = _check()
    repos = [_repo("platform-a"), _repo("platform-b")]
    fake = FakeClient(repos, sboms={"example-org/platform-a": _transient(),
                                    "example-org/platform-b": _transient()})
    result = check._sbom_check(fake, check._discover(fake))
    assert result.stored_code is StatusCode.WARN
    assert all(e.code is StatusCode.UNDEFINED
               for e in result.reason_entries if e.slug != "read")
    gap = next(e for e in result.reason_entries if e.slug == "read")
    assert gap.text == "GitHub did not answer for 2 of 2 repositories"


def test_a_clean_aspect_stays_silent():
    """The `read` line appears only when something was missed. A healthy aspect
    renders exactly as it did before this slice — no line, no noise."""
    check = _check()
    fake = FakeClient([_repo("platform-a")])
    result = check._sbom_check(fake, check._discover(fake))
    assert result.reason_entries == ()
    assert result.stored_code is StatusCode.OK


def test_the_rule_is_the_same_in_every_aspect_that_reads_per_repository():
    """"every aspect ... behaves the same way, because this is one rule and not a
    fix in one method." Four aspects, four endpoints, one sentence."""
    check = _check()
    repos = [_repo("platform-a"), _repo("platform-b")]
    cases = {
        "pull_requests": FakeClient(repos, errors={
            ("example-org/platform-a", "pulls"): _transient()}),
        "issues": FakeClient(repos, errors={
            ("example-org/platform-a", "issues"): _transient()}),
        "secret_scanning_alerts": FakeClient(repos, errors={
            ("example-org/platform-a", "secret_scanning"): _transient()}),
        "sbom_check": FakeClient(repos, sboms={
            "example-org/platform-a": _transient()}),
    }
    for aspect, fake in cases.items():
        result = getattr(check, f"_{aspect}")(fake, check._discover(fake))
        note = next(e for e in result.reason_entries
                    if e.slug.endswith("unreadable"))
        assert note.code is StatusCode.UNDEFINED, aspect
        assert "could not ask GitHub" in note.text, aspect
        assert result.stored_code is StatusCode.WARN, aspect
        gap = next(e for e in result.reason_entries if e.slug == "read")
        assert gap.code is StatusCode.WARN, aspect


def test_a_banded_aspect_that_could_not_look_is_amber_and_its_bands_are_not():
    """The green-when-blind defect, gone — and gone without changing what a band
    means. A read failure has no honest source severity, so it stays on the
    container; the container used to declare no code and derive `UNDEFINED`, which
    the roll-up skips in favour of the bands — and a watched band is `OK` when
    empty, so a run that saw nothing rendered **green**. The coverage line grades
    the container instead. The bands are untouched: they still never have to tell
    *empty* from *unread*."""
    check = _check()
    repos = [_repo("platform-a"), _repo("platform-b")]
    fake = FakeClient(repos, errors={
        ("example-org/platform-a", "dependabot"): _transient()})
    result = check._security_advisories(fake, check._discover(fake))
    assert result.stored_code is StatusCode.WARN
    fake_all = FakeClient(repos, errors={
        ("example-org/platform-a", "dependabot"): _transient(),
        ("example-org/platform-b", "dependabot"): _transient()})
    nothing_read = check._security_advisories(fake_all, check._discover(fake_all))
    assert nothing_read.stored_code is StatusCode.WARN          # was UNDEFINED
    assert {c.name for c in nothing_read.children} == {"critical", "high"}
    # and the bands themselves say exactly what they said before
    assert all(c.stored_code is StatusCode.OK for c in nothing_read.children)


def test_the_check_node_is_where_an_outage_becomes_visible():
    """If the repository lines grade nothing, something has to — or an hour of
    5xx reads exactly like an hour of everything being fine."""
    check = _check()
    repos = [_repo("platform-a"), _repo("platform-b")]
    fake = FakeClient(repos,
                      sboms={"example-org/platform-a": _transient()},
                      errors={("example-org/platform-b", "issues"): _transient()})
    check._make_client = lambda token, deadline=None: fake  # type: ignore[method-assign]
    result = check.run()
    assert result.stored_code is StatusCode.WARN
    node = " | ".join(result.reason_texts)
    assert "2 repository reads could not be completed this run" in node
    # said once, on the node that owns coverage — not once per aspect
    assert node.count("could not be completed") == 1


def test_a_permission_error_is_not_counted_on_the_node():
    """It is already amber where it happened. Counting it here would report one
    condition twice and make the node's number mean two different things."""
    check = _check()
    fake = FakeClient([_repo("platform-a")],
                      sboms={"example-org/platform-a": _denied()})
    check._make_client = lambda token, deadline=None: fake  # type: ignore[method-assign]
    result = check.run()
    assert "could not be completed" not in " ".join(result.reason_texts)


# --- Two budgets, and they are not the same one (ADR-0002) --------------------

class _Clock:
    """A hand-wound monotonic clock, so a deadline can be spent without waiting."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Ticking:
    """A clock that advances every time it is read, so a budget can run out *inside*
    a call rather than only between calls."""

    def __init__(self, step, now=0.0):
        self.now = now
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


def _client(opener, **over):
    """A real `GitHubClient` with its one request stubbed out.

    `opener(url, timeout)` stands in for the whole of `_attempt`: it either raises,
    or returns the `(data, link)` pair `_attempt` would have parsed. Use this for
    what *surrounds* a request — the retry, the page walk, the deadline — and
    `_fetching` below for the request itself.
    """
    calls = []

    class _Stub(GitHubClient):
        def _attempt(self, url):
            calls.append(url)
            return opener(url, self._timeout)

    kwargs = {"api_url": "https://api.example.test", "sleep": lambda _s: None}
    kwargs.update(over)
    client = _Stub("t", **kwargs)
    return client, calls


class _Answer:
    """One HTTP response, in the shape `fetch`'s opener hands back."""

    def __init__(self, status=200, body=b"{}", headers=None, url=None):
        self.status = status
        self.url = url or "https://api.example.test/x"
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value
        self._body = body

    def read1(self, size=-1):
        """What `fetch._read` calls, and it calls nothing else on a body stream.
        `read1` rather than `read` because a real `read(n)` blocks until it has all *n*
        bytes: reading a body with it cannot be bounded by a clock, which is the defect
        little-sister ADR-0058's 2026-08-16 amendment records. A double offering `read`
        alone would still be modelling the version that could not be bounded."""
        size = len(self._body) if size < 0 else size
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Opener:
    """Stands in for the module-level opener inside `little_sister.fetch`.

    Patched in one level **below** `fetch`, so a test using it exercises the real
    request path: the status inversion, the header reading and the classification are
    all shipped code. `answer` is an `_Answer`, an exception to raise, or a callable
    of `(request, timeout)`.
    """

    def __init__(self, answer):
        self._answer = answer
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        answer = self._answer
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(request, timeout)
        return answer


@contextlib.contextmanager
def _through(answer):
    """A real client whose requests go through the real `fetch` and end at ``answer``.

    Yields `(client_factory, opener)`. The opener records the `urllib` request objects
    and the socket timeouts `fetch` computed — which is the only place the clamp is
    observable, and it was previously asserted against a copy of the clamp in the
    test rather than against the code.
    """
    opener = _Opener(answer)
    with mock.patch.object(ls_fetch, "_FOLLOWING", opener):
        def build(**over):
            kwargs = {"api_url": "https://api.example.test",
                      "sleep": lambda _s: None}
            kwargs.update(over)
            return GitHubClient("t", **kwargs)
        yield build, opener


def test_timeout_is_the_runs_budget_and_request_timeout_is_one_requests():
    """The defect this half of the slice is about: `timeout:` is documented as the
    **per-run** budget, and it used to be handed to `urlopen` as the per-socket-op
    value — spent afresh on each of the hundreds of requests a run makes, and so
    bounding nothing. They are two numbers now."""
    check = _check(timeout_seconds=60.0)
    assert check.request_timeout == DEFAULT_REQUEST_TIMEOUT
    assert check.request_timeout < check.timeout_seconds
    built = check._make_client("token")
    assert built._timeout == DEFAULT_REQUEST_TIMEOUT     # not 60
    def _rt(value):
        return GitHubCheck._extra_from_config(
            {"owner": "o", "kind": "user", "request_timeout": value,
             "secrets": {"token": "env://T"}}, tmp_path_stub())["request_timeout"]

    # the same spelling `timeout:` and `frequency:` take, or a surface where one
    # duration key accepts `15s` and the next refuses it
    assert _rt(4) == 4.0
    assert _rt("20s") == 20.0
    assert _rt("2m") == 120.0


def test_a_request_timeout_that_is_not_a_positive_number_is_refused():
    for bad in (0, -1, "soon", True, "0s"):
        with pytest.raises(CheckError) as caught:
            GitHubCheck._extra_from_config(
                {"owner": "o", "kind": "user", "request_timeout": bad,
                 "secrets": {"token": "env://T"}}, tmp_path_stub())
        assert "request_timeout" in str(caught.value)


def test_one_request_may_never_outlive_what_is_left_of_the_run():
    """The clamp. Without it a 15-second request could start with two seconds of
    run budget left and overrun `timeout:` by thirteen — the deadline would be a
    suggestion, not a bound.

    Asserted on the **socket timeout the request was actually issued with**, which is
    the only place the clamp is visible. The clamp itself is the library's now
    (`Deadline.budget`), and this pins that the client hands it the run's deadline
    rather than quietly spending `request_timeout` per socket operation."""
    clock = _Clock()
    deadline = Deadline(10.0, clock=clock)
    with _through(lambda _r, _t: _Answer()) as (build, opener):
        client = build(timeout=15.0, deadline=deadline)
        client.get("/a")
        assert opener.timeouts[0] == 10.0   # clamped to what the run has left
        clock.advance(7)
        client.get("/b")
        assert opener.timeouts[1] == 3.0
    # and with no deadline at all it is simply the request's own budget
    with _through(lambda _r, _t: _Answer()) as (build, plain_opener):
        build(timeout=15.0).get("/c")
        assert plain_opener.timeouts[0] == 15.0


def test_every_request_carries_the_auth_and_api_version_headers():
    """What is left of this client after the request moved into the library: the
    three headers only a GitHub client knows to send. A missing
    `X-GitHub-Api-Version` is the kind of omission that works until GitHub changes a
    default."""
    with _through(lambda _r, _t: _Answer()) as (build, opener):
        build().get("/x")
    sent = opener.requests[0]
    assert sent.get_header("Authorization") == "Bearer t"
    assert sent.get_header("Accept") == "application/vnd.github+json"
    assert sent.get_header("X-github-api-version") == "2022-11-28"
    # and the library's own identification, not `Python-urllib`
    assert ls_fetch.USER_AGENT.startswith("little-sister/")


def test_a_transient_failure_is_retried_once_and_a_4xx_is_not():
    """"Retry only what asking again could change." A 5xx may differ next time; a
    403 will not, and asking twice only spends the budget to be told twice."""
    attempts = []

    def flaky(url, timeout):
        attempts.append(url)
        if len(attempts) == 1:
            raise _transient("502")
        return {"ok": True}, ""

    client, _ = _client(flaky)
    assert client.get("/x") == {"ok": True}
    assert len(attempts) == 2                     # one retry, and it worked

    denied = []

    def forbidden(url, timeout):
        denied.append(url)
        raise _answered("403", 403)

    client2, _ = _client(forbidden)
    with pytest.raises(GitHubError):
        client2.get("/y")
    assert len(denied) == 1                       # asked once, believed it


def test_a_transient_failure_that_survives_the_retry_is_raised_as_transient():
    """What reaches an aspect — and therefore what becomes an UNDEFINED line — is
    a failure that GitHub gave twice, not a single hiccup."""
    calls = []

    def always(url, timeout):
        calls.append(url)
        raise _transient("502")

    client, _ = _client(always)
    with pytest.raises(GitHubError) as caught:
        client.get("/x")
    assert caught.value.fault is Fault.TRANSIENT
    assert len(calls) == 2


def test_the_retry_is_skipped_when_the_run_cannot_afford_the_wait():
    """A retry that is certain to be cut off spends the backoff to learn nothing."""
    clock = _Clock()
    calls = []

    def always(url, timeout):
        calls.append(url)
        raise _transient("502")

    client, _ = _client(always, deadline=Deadline(RETRY_BACKOFF_SECONDS / 2,
                                                   clock=clock))
    with pytest.raises(GitHubError):
        client.get("/x")
    assert len(calls) == 1


def test_a_payload_of_the_wrong_shape_is_malformed_and_not_transient():
    """The third fault, and why two would not do. A payload of the wrong shape has
    no status, so it cannot be read off one — and it is not the same fact as a 404:
    a 404 is about the thing being measured, while an object where a list was
    promised is about the reading, or about the API."""
    client, _ = _client(lambda url, t: ({"not": "a list"}, ""))
    with pytest.raises(GitHubError) as caught:
        client.get_paginated("/things")
    assert caught.value.fault is Fault.MALFORMED
    assert caught.value.status is None


def test_the_deadline_stops_the_run_and_what_finished_is_kept():
    """Lex's ruling: return what it has. The aspects that completed report
    normally, the rest are absent, and the node says the run was cut short."""
    clock = _Clock()
    check = _check(timeout_seconds=30.0)
    fake = FakeClient([_repo("platform-a")])
    # Whichever aspect the roster asks **first** — named through `ASPECTS` rather
    # than written out, so re-ordering the row does not silently turn this into a
    # test that the second aspect ate the budget.
    first = check.active_aspects()[0]
    real_first = getattr(check, f"_{first}")

    def spend_the_run(client, repos):
        result = real_first(client, repos)
        clock.advance(31)                     # the first aspect ate the budget
        return result

    setattr(check, f"_{first}", spend_the_run)
    check._make_client = lambda token, deadline=None: fake  # type: ignore[method-assign]
    check._new_deadline = lambda: Deadline(30.0, clock=clock)  # type: ignore[method-assign]
    result = check.run()
    names = [child.name for child in result.children]
    assert names == [first]                           # what finished is kept
    assert result.stored_code is StatusCode.WARN
    node = " | ".join(result.reason_texts)
    assert "run cut short after 31s of its 30s timeout" in node
    assert "1 of 8 aspects reported" in node


def test_a_wait_the_pause_budget_cannot_afford_stops_the_run():
    """Lex's ruling: at the cap the run ends the way an exhausted deadline ends
    it — what finished is kept, the rest are absent, and the node says why.

    The alternative that was rejected is worth naming, because it is what the code
    does if this stops working: withholding `retry_after` does not stop `ask`
    retrying, it only drops it to a one-second backoff — so a throttled run with
    thirty-nine repositories left would press a service that had just asked it to
    wait, thirty-nine more times."""
    check = _check(timeout_seconds=30.0, max_pause=10.0)
    fake = FakeClient([_repo('platform-a')])
    # The first two aspects the roster asks, named through it rather than written
    # out — the claim is about the *first* aspect finishing and the *second* hitting
    # the cap, not about which two those happen to be today.
    first, second = check.active_aspects()[:2]
    real_first = getattr(check, f'_{first}')

    def throttled(client, repos):
        result = real_first(client, repos)
        raise_next[0] = True
        return result

    raise_next = [False]
    real_second = getattr(check, f'_{second}')

    def refused(client, repos):
        if raise_next[0]:
            raise mod_github._PauseBudgetSpent(45.0)
        return real_second(client, repos)

    setattr(check, f'_{first}', throttled)
    setattr(check, f'_{second}', refused)
    check._make_client = lambda token, deadline=None: fake  # type: ignore[method-assign]
    result = check.run()
    assert [child.name for child in result.children] == [first]
    assert result.stored_code is StatusCode.WARN
    node = ' | '.join(result.reason_texts)
    assert 'run cut short after 1 of 8 aspects reported' in node
    assert 'a further 45s wait would pass its 10s pause budget' in node


# ── The run's trace ────────────────────────────────────────────────────────────
# The node says *how much* of the run reported; these lines say where its seconds
# went. They are asserted as whole messages rather than as fragments: a trace is
# read by a person, and a substring assertion cannot tell `aspect 6/8` from
# `aspect 6/80` or catch a number that moved to the wrong slot.

_LOGGER = "little_sister_github.github"


def _two_repos():
    """Two repositories, so a per-aspect read count is a number and not a coin
    flip between zero and one."""
    return [_repo("platform-a"), _repo("platform-b")]


def _lines(caplog):
    """Every message this package logged, formatted as it would be written."""
    return [record.getMessage() for record in caplog.records
            if record.name == _LOGGER]


def _traced_run(caplog, check, fake):
    """One run with its trace captured at INFO."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    check._make_client = lambda token, deadline=None: fake
    return check.run(), _lines(caplog)


def test_the_run_states_the_budgets_it_is_about_to_spend(caplog):
    """The first line of a run is the three numbers that decide how far it gets —
    and two of them cannot be read off a config file. `max_pause` is derived from
    `timeout:` when unset, and the aspect count is the roster *minus* whatever this
    deployment switched off, which is the denominator every later line uses."""
    check = _check(timeout_seconds=60.0, disabled_aspects=("issues",))
    _result, lines = _traced_run(caplog, check, FakeClient(_two_repos()))
    assert ("/github: run starting — timeout 60s, request timeout 15s, "
            "max pause 30s, 7 aspect(s)") in lines


def test_every_aspect_says_what_it_cost_and_what_was_left(caplog):
    """One line per aspect, and the four numbers a starving run is diagnosed from:
    which aspect, how long it took, how many requests that was, and the budget
    afterwards. `code_scanning_quality` costs **zero** reads, which is the
    single-payload claim the CHANGELOG makes for the code-scanning split and which
    nothing else in a running deployment shows."""
    check = _check()
    _result, lines = _traced_run(caplog, check, FakeClient(_two_repos()))
    assert ("/github: aspect 3/8 code_scanning_security took 0.0s in 2 read(s) "
            "— 30s of the run left") in lines
    assert ("/github: aspect 6/8 code_scanning_quality took 0.0s in 0 read(s) "
            "— 30s of the run left") in lines
    assert len([line for line in lines if ": aspect " in line]) == 8


def test_an_aspect_line_times_the_aspect_and_not_the_run(caplog):
    """The duration is that aspect's own. The run's elapsed is a different number,
    it is on the receipt already, and the two agree for exactly one aspect — the
    first — which is why it is the **second** that is asserted here. A line built
    from the run's clock would read correctly for the first aspect of every run and
    then blame each later one for everything before it."""
    clock = _Clock()
    check = _check(timeout_seconds=60.0)
    first, second = check.active_aspects()[:2]
    real_first = getattr(check, f"_{first}")
    real_second = getattr(check, f"_{second}")

    def five_seconds(client, repos):
        result = real_first(client, repos)
        clock.advance(5)
        return result

    def seven_seconds(client, repos):
        result = real_second(client, repos)
        clock.advance(7)
        return result

    setattr(check, f"_{first}", five_seconds)
    setattr(check, f"_{second}", seven_seconds)
    check._new_deadline = lambda: Deadline(60.0, clock=clock)
    _result, lines = _traced_run(caplog, check, FakeClient(_two_repos()))
    assert (f"/github: aspect 1/8 {first} took 5.0s in 2 read(s) — 55s of the "
            f"run left") in lines
    assert (f"/github: aspect 2/8 {second} took 7.0s in 2 read(s) — 48s of the "
            f"run left") in lines


def test_the_cut_short_log_names_the_aspect_and_the_starving_tail(caplog):
    """The node counts; the log names. `active_aspects()` is walked in a fixed
    order, so the aspects a cut-short run never reaches are the same ones on every
    run — and until this line existed a reader had to rebuild the roster by hand
    from `ASPECTS` minus their own `disabled_aspects` to find out which.

    An aspect is switched **off** here on purpose: every count on the line is the
    active roster's and not `ASPECTS`', and with a full roster the two agree, so a
    line built from the wrong one would read correctly all the way to the first
    deployment that disabled something."""
    clock = _Clock()
    check = _check(timeout_seconds=60.0, disabled_aspects=("issues",))
    first, second = check.active_aspects()[:2]
    real_first = getattr(check, f"_{first}")

    def spend_the_run(client, repos):
        result = real_first(client, repos)
        clock.advance(61)
        return result

    setattr(check, f"_{first}", spend_the_run)
    check._new_deadline = lambda: Deadline(60.0, clock=clock)
    _result, lines = _traced_run(caplog, check, FakeClient(_two_repos()))
    assert (f"/github: run cut short after 61s of its 60s timeout — 1 of 7 "
            f"aspects reported — {second} (2 of 7) was never started; never "
            f"reached: " + ", ".join(check.active_aspects()[2:])) in lines


def test_an_aspect_the_budget_died_inside_is_told_apart_from_one_never_started(
        caplog):
    """Two different findings the numbers cannot separate. An aspect the loop
    refused to start reads as *0 reads in 0.0s*, and so does one cut off on its
    first request — the first says the aspects before it were too slow, the second
    says this one is. The reads it did make before the deadline landed are on the
    line, and they are the evidence for which.

    The aspect that dies is the **second** one, and the first spends twenty
    seconds: the duration on the line is this aspect's own and not the run's, and
    those two are the same number whenever the first aspect is the one that
    dies."""
    clock = _Clock()
    check = _check(timeout_seconds=60.0)
    first, second = check.active_aspects()[:2]
    real_first = getattr(check, f"_{first}")

    def slow_but_finishes(client, repos):
        result = real_first(client, repos)
        clock.advance(20)
        return result

    def cut_off_partway(client, repos):
        client.get_paginated(f"/repos/{repos[0].full_name}/pulls")
        clock.advance(41)
        raise DeadlineExceeded("the body read found the budget gone")

    setattr(check, f"_{first}", slow_but_finishes)
    setattr(check, f"_{second}", cut_off_partway)
    check._new_deadline = lambda: Deadline(60.0, clock=clock)
    _result, lines = _traced_run(caplog, check, FakeClient(_two_repos()))
    assert (f"/github: run cut short after 61s of its 60s timeout — 1 of 8 "
            f"aspects reported — {second} (2 of 8) was cut off after 41.0s and "
            f"1 read(s); never reached: "
            + ", ".join(check.active_aspects()[2:])) in lines


def test_the_pause_budget_stop_names_where_it_stopped_too(caplog):
    """The other way a run ends early gets the same two halves — the aspect it
    stopped in and the tail that never ran. A throttled run starves exactly the
    same aspects as a slow one, and for a reader the question is the same."""
    check = _check(timeout_seconds=30.0, max_pause=10.0)
    _first, second = check.active_aspects()[:2]

    def refused(client, repos):
        # The wait `ask` was about to take, refused by the pause cap — raised out
        # of the second aspect, so the first one's result is what survives.
        raise mod_github._PauseBudgetSpent(45.0)

    setattr(check, f"_{second}", refused)
    _result, lines = _traced_run(caplog, check, FakeClient(_two_repos()))
    assert (f"/github: run cut short after 1 of 8 aspects reported — a further "
            f"45s wait would pass its 10s pause budget — {second} (2 of 8) was "
            f"cut off after 0.0s and 0 read(s); never reached: "
            + ", ".join(check.active_aspects()[2:])) in lines


def test_the_run_receipt_names_the_slowest_read(caplog):
    """What a run spent, and the one fact that says *why* it ran out: whether a
    single endpoint sat at the request timeout or everything was merely slow. Two
    runs that both spend sixty seconds on twenty reads are different problems, and
    only this number tells them apart."""
    check = _check(timeout_seconds=60.0)
    fake = FakeClient(_two_repos(), read_seconds=41.5, slowest_read=14.5,
                      slowest_path="/repos/example-org/platform-a/pulls",
                      paused_seconds=3.0)
    _result, lines = _traced_run(caplog, check, fake)
    assert ("/github: run ended after 0.0s of its 60s timeout — 19 read(s) in "
            "41.5s, slowest read 14.5s (/repos/example-org/platform-a/pulls), "
            "3s paused, 8 of 8 aspects reported") in lines


def test_a_run_whose_reads_took_no_measurable_time_says_that(caplog):
    """Rather than `slowest read 0.0s ()`, which reads as a path that went missing.
    A double takes no time and so does a cached run; neither is a slow endpoint."""
    check = _check(timeout_seconds=60.0)
    _result, lines = _traced_run(caplog, check, FakeClient(_two_repos()))
    assert ("/github: run ended after 0.0s of its 60s timeout — 19 read(s) in "
            "0.0s, no read took measurable time, 0s paused, 8 of 8 aspects "
            "reported") in lines


def test_the_rate_limit_headroom_is_logged_even_when_it_does_not_stop_the_run(
        caplog):
    """The interesting run is the one that *just* cleared the factor — which is
    invisible when only the refusal is reported, and is the run that stops
    clearing it next week."""
    check = _check()
    _result, lines = _traced_run(caplog, check,
                                 FakeClient(_two_repos(), rate=(5000, 900, 0)))
    assert "/github: 900 API calls left, this run needs 4×14 = 56" in lines


def test_discovery_is_on_the_trace_because_it_spends_the_same_budget(caplog):
    """A run that never reaches its last aspects may have spent the difference
    before the first one started, and no aspect line can show that."""
    check = _check()
    _result, lines = _traced_run(caplog, check, FakeClient(_two_repos()))
    assert ("/github: discovery took 0.0s in 3 read(s) — 30s of the run left"
            in lines)


def test_a_read_that_failed_is_still_counted():
    """Counted in a `finally`, because a run that spent its whole budget on
    requests that timed out would otherwise be traced as a run that made none —
    which is the exact run somebody is reading the trace to understand."""
    with _through(OSError("connection reset")) as (build, _opener):
        client = build(retries=0)
        with pytest.raises(GitHubError):
            client.get("/x")
    assert client.reads_made == 1


def test_the_slowest_read_is_the_slowest_one_and_names_its_api_path():
    """The path and not the URL: it is what a reader compares against
    `ASPECT_ENDPOINT`, and it does not move when `api_url` does."""
    clock = _Clock()

    def answer(request, _timeout):
        clock.advance(9.0 if request.full_url.endswith("/slow") else 1.0)
        return _Answer()

    with _through(answer) as (build, _opener):
        client = build(deadline=Deadline(100.0, clock=clock))
        client.get("/quick")
        client.get("/slow")
        client.get("/quick")
    assert client.reads_made == 3
    assert client.read_seconds == 11.0
    assert client.slowest_read == 9.0
    assert client.slowest_path == "/slow"


def test_the_client_refuses_a_wait_that_would_pass_the_budget_whole():
    """Refused, not trimmed: a wait shorter than the one the service asked for
    does not satisfy it, so taking what is left of the budget would spend the
    budget *and* still be throttled. And the refused wait is not counted."""
    slept = []
    client = GitHubClient('t', max_pause=10.0, sleep=slept.append)
    client._slept(6.0)
    assert slept == [6.0] and client.paused_seconds == 6.0
    with pytest.raises(mod_github._PauseBudgetSpent) as caught:
        client._slept(5.0)                      # 6 + 5 > 10
    assert caught.value.wait == 5.0
    assert slept == [6.0]                       # nothing partial was taken
    assert client.paused_seconds == 6.0         # and nothing was counted


def test_a_client_with_no_pause_budget_sleeps_whatever_it_is_asked():
    """The library's own clients and the rate-limit type build one without a cap;
    `None` has to mean unbounded rather than zero, or those stop retrying at all."""
    slept = []
    client = GitHubClient('t', sleep=slept.append)
    for wait in (30.0, 300.0):
        client._slept(wait)
    assert slept == [30.0, 300.0]


def test_the_pause_budget_defaults_to_half_the_runs_own_timeout():
    """Derived rather than fixed, because `timeout:` is what a deployment sizes to
    its account: a constant would be a no-op at the library's 30-second default and
    far too loose at ten minutes."""
    assert _check(timeout_seconds=30.0).max_pause_seconds == 15.0
    assert _check(timeout_seconds=600.0).max_pause_seconds == 300.0
    assert _check(timeout_seconds=600.0, max_pause=20.0).max_pause_seconds == 20.0
    assert '**pause budget:** 15s' in _check(timeout_seconds=30.0).config_summary()


def test_a_pause_budget_the_deadline_would_always_beat_is_refused():
    """Same argument as `error_below` above `warn_below`: a cap no run could ever
    reach is a bound stated in the config summary that does nothing. Refused at
    load, where an operator is still looking at the line they wrote."""
    for bad in (30.0, 45.0):
        with pytest.raises(CheckError) as caught:
            _check(timeout_seconds=30.0, max_pause=bad)
        assert 'max_pause' in str(caught.value)
        assert 'timeout' in str(caught.value)
    _check(timeout_seconds=30.0, max_pause=29.0)        # just under is fine


def test_max_pause_takes_the_same_duration_spelling_as_every_other_key():
    def _mp(value):
        return GitHubCheck._extra_from_config(
            {'owner': 'o', 'kind': 'user', 'max_pause': value,
             'secrets': {'token': 'env://T'}}, tmp_path_stub())['max_pause']

    assert _mp('20s') == 20.0
    assert _mp('2m') == 120.0
    assert _mp(4) == 4.0
    # absent means derive it from `timeout:`, which this classmethod cannot reach
    assert GitHubCheck._extra_from_config(
        {'owner': 'o', 'kind': 'user', 'secrets': {'token': 'env://T'}},
        tmp_path_stub())['max_pause'] is None
    for bad in (0, -1, 'soon', True, '0s'):
        with pytest.raises(CheckError) as caught:
            _mp(bad)
        assert 'max_pause' in str(caught.value)


def test_the_deadline_is_checked_before_every_page_not_only_every_call():
    """Pagination is where one call quietly becomes twenty, so the check has to
    sit inside the page loop or a paginated read could outlive the whole run."""
    clock = _Clock()
    pages = []

    def paging(url, timeout):
        pages.append(url)
        clock.advance(6)
        return [{"n": len(pages)}], '<https://api.example.test/next>; rel="next"'

    client, _ = _client(paging, deadline=Deadline(10.0, clock=clock))
    with pytest.raises(DeadlineExceeded):
        client.get_paginated("/things")
    assert len(pages) == 2            # 6s, 12s — the third was never started


def test_a_run_that_times_out_during_discovery_says_so_and_nothing_else():
    """The one place the deadline is the whole answer rather than a footnote:
    nothing was read, so there is no partial truth to keep."""
    clock = _Clock()
    check = _check(timeout_seconds=5.0)
    check._new_deadline = lambda: Deadline(5.0, clock=clock)  # type: ignore[method-assign]

    class _Slow(FakeClient):
        def get(self, path, params=None):
            clock.advance(6)
            raise DeadlineExceeded("the run's timeout of 5s ran out")

    check._make_client = lambda token, deadline=None: _Slow([])  # type: ignore[method-assign]
    result = check.run()
    assert result.stored_code is StatusCode.WARN
    assert result.children == ()
    assert "ran out" in " ".join(result.reason_texts)


# --- The status → fault mapping, through the REAL request path ----------------
#
# These exist because the mapping had no test: every other client test stubs
# `_attempt` out and hand-builds the fault, so the one decision the whole
# read-failure rule rests on was asserted by nobody. It shipped inverted —
# not-transient for every HTTP status — and the suite stayed green.
#
# The stub sits *below* `fetch` now, so what runs here is the shipped inversion
# (`HTTPError` → a `Response`), the shipped classification (`fault_for`) and the
# shipped throttle reader.

def _http_error(code, body=b'{"message": "nope"}', headers=None):
    import io
    import urllib.error
    carried = Message()
    for name, value in (headers or {}).items():
        carried[name] = value
    return urllib.error.HTTPError(
        "https://api.example.test/x", code, "err", carried, io.BytesIO(body))


@pytest.mark.parametrize(("code", "fault"), [
    (500, Fault.TRANSIENT), (502, Fault.TRANSIENT), (503, Fault.TRANSIENT),
    (599, Fault.TRANSIENT),
    (400, Fault.ANSWERED), (401, Fault.ANSWERED), (403, Fault.ANSWERED),
    (404, Fault.ANSWERED), (422, Fault.ANSWERED),
])
def test_a_status_decides_the_fault_and_nothing_else_does(code, fault):
    """"5xx is GitHub failing to answer; a 4xx *is* an answer." The bodies are
    identical across every case here, so only the status can be deciding — and the
    403 carries no throttle header, so it is the permission answer."""
    with _through(_http_error(code)) as (build, _opener):
        with pytest.raises(GitHubError) as caught:
            build(retries=0).get("/x")
    assert caught.value.status == code
    assert caught.value.fault is fault
    assert caught.value.retry_after is None


def test_a_transport_failure_is_transient():
    """No status at all, and asking again really might work — a dropped
    connection, a DNS blip, a socket timeout. The message is ours and names the URL;
    urllib's own text arrives as the cause rather than in the sentence a reader gets
    twice."""
    with _through(OSError("connection reset")) as (build, _opener):
        with pytest.raises(GitHubError) as caught:
            build(retries=0).get("/x")
    assert caught.value.status is None
    assert caught.value.fault is Fault.TRANSIENT
    assert "connection reset" in str(caught.value)
    assert str(caught.value).count("request failed for") == 1


def test_an_answer_that_is_not_json_is_malformed_and_not_retried():
    """New information in a line, and the reason it is worth having: this used to be
    reported as transient, so a second request was spent to be handed the same bytes,
    and the line said *could not ask GitHub* about an answer GitHub had given."""
    attempts = []

    def counted(request, timeout):
        attempts.append(request)
        return _Answer(body=b"<html>a proxy login page</html>")

    with _through(counted) as (build, _opener):
        with pytest.raises(GitHubError) as caught:
            build(retries=1).get("/x")
    assert caught.value.fault is Fault.MALFORMED
    assert len(attempts) == 1                 # asking again cannot change a shape
    assert "not JSON" in str(caught.value)


def test_a_real_5xx_reaches_the_aspect_as_a_line_that_grades_nothing():
    """End to end through the real request path, with the payload that started
    this: the motivating 500 must produce the quiet line, not the amber one.
    Nothing below hand-builds a fault."""
    body = b'{"message":"Failed to generate SBOM: Request timed out."}'
    with _through(lambda _r, _t: _http_error(500, body)) as (build, _opener):
        _the_500_reaches_the_aspect(build)


def _the_500_reaches_the_aspect(build):
    check = _check()
    result = check._sbom_check(build(retries=1),
                               _repos("platform-a", "platform-b"))
    notes = [e for e in result.reason_entries if e.slug.endswith("unreadable")]
    assert len(notes) == 2
    assert all(e.code is StatusCode.UNDEFINED for e in notes)
    assert "could not ask GitHub" in notes[0].text
    # Nothing was read, so the aspect is amber on its own coverage — not grey,
    # which is what an entry set of nothing but UNDEFINED used to derive.
    gap = next(e for e in result.reason_entries if e.slug == "read")
    assert gap.code is StatusCode.WARN
    assert gap.text == "GitHub did not answer for 2 of 2 repositories"
    assert result.stored_code is StatusCode.WARN
    # both repositories were counted toward the node's coverage line
    assert check._unreachable == 2


# --- *Not now* is not *no*: GitHub's throttle, in GitHub's own headers ---------
#
# The defect these are about shipped and is fixed here. `transient` was
# `500 <= code < 600`, so a rate-limited 403 or 429 was classified as *answered*:
# the check reported it as though GitHub had said **no** about the repository — an
# amber line naming a repository that was perfectly fine — and it was never retried.
#
# The status cannot decide it, which is the whole reason this reader exists rather
# than a fourth `Fault` member in the library: GitHub documents both 403 and 429 for
# both its primary and its secondary limits, and a bare 403 equally means *this token
# may not see it*. So the headers decide, and never the body.

def test_a_secondary_limit_with_retry_after_is_not_now_and_is_waited_out():
    """The case Lex named: a 403 with `retry-after` is exactly what this plugin is
    here to read. Transient, so it grades nothing, and the wait is GitHub's number
    rather than our backoff.

    The response carries the **primary** limit's headers as well, because a real one
    does — GitHub puts `x-ratelimit-*` on every answer it gives. So this pins the
    precedence too: `retry-after` is rung one, and a reset an hour out must not win
    over the two seconds GitHub actually asked for."""
    slept = []
    answers = [_http_error(403, b'{"message":"secondary rate limit"}',
                           {"retry-after": "2",
                            "x-ratelimit-remaining": "0",
                            "x-ratelimit-reset": "1700003600"}),
               _Answer(body=b'{"ok": true}')]

    with _through(lambda _r, _t: _pop(answers)) as (build, _opener):
        client = build(sleep=slept.append)
        assert client.get("/x") == {"ok": True}
    assert slept == [2.0]                 # GitHub's two seconds, not our one


def test_a_throttle_that_survives_the_retry_grades_nothing():
    """What reaches an aspect when GitHub keeps saying *not now*: a transient
    failure carrying the number, which becomes the quiet UNDEFINED line rather than
    an amber claim about somebody's repository."""
    def throttled(_request, _timeout):
        return _http_error(429, b'{"message":"too many requests"}',
                           {"retry-after": "1"})

    with _through(throttled) as (build, _opener):
        with pytest.raises(GitHubError) as caught:
            build(sleep=lambda _s: None).get("/x")
    assert caught.value.fault is Fault.TRANSIENT
    assert caught.value.status == 429
    assert caught.value.retry_after == 1.0
    assert "asked us to wait 1s" in str(caught.value)


def test_an_exhausted_primary_limit_is_read_from_the_ratelimit_headers():
    """GitHub's precedence, second rung: no `retry-after`, but
    `x-ratelimit-remaining: 0` says the budget is gone and `x-ratelimit-reset` says
    when it returns. That is an **epoch** timestamp, so it is read against the wall
    clock — the run's own clock is monotonic and the two do not compare."""
    reset = 1_700_000_060
    exhausted = _http_error(403, b'{"message":"API rate limit exceeded"}',
                            {"x-ratelimit-remaining": "0",
                             "x-ratelimit-reset": str(reset)})
    with _through(exhausted) as (build, _opener):
        with mock.patch.object(mod_github.time, "time",
                               lambda: float(reset - 45)):
            with pytest.raises(GitHubError) as caught:
                build(retries=0).get("/x")
    assert caught.value.fault is Fault.TRANSIENT
    assert caught.value.retry_after == 45.0


def test_an_exhausted_primary_limit_whose_window_already_rolled_over_waits_nothing():
    """A reset in the past means the budget is already back, so `0.0` — and not a
    negative number, which `ask` would hand to `sleep`."""
    reset = 1_700_000_000
    exhausted = _http_error(429, b"{}", {"x-ratelimit-remaining": "0",
                                         "x-ratelimit-reset": str(reset)})
    with _through(exhausted) as (build, _opener):
        with mock.patch.object(mod_github.time, "time",
                               lambda: float(reset + 300)):
            with pytest.raises(GitHubError) as caught:
                build(retries=0).get("/x")
    assert caught.value.retry_after == 0.0


def test_an_exhausted_primary_limit_with_no_readable_reset_takes_the_floor():
    """We already know from `x-ratelimit-remaining: 0` that the budget is gone, so
    the one answer that cannot be right is *retry immediately*."""
    for headers in ({"x-ratelimit-remaining": "0"},
                    {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "soon"}):
        with _through(_http_error(403, b"{}", headers)) as (build, _opener):
            with pytest.raises(GitHubError) as caught:
                build(retries=0).get("/x")
        assert caught.value.retry_after == THROTTLE_FLOOR_SECONDS


def test_a_bare_403_is_still_the_permission_answer():
    """The half of this that must **not** move. A 403 with no throttle header is a
    token that may not read the thing — it grades WARN and somebody has to act on
    it. Reading it as *not now* would retry every unreadable repository in the scope
    and paint the real permission problem grey."""
    with _through(_http_error(403, b'{"message":"Resource not accessible"}')) \
            as (build, _opener):
        with pytest.raises(GitHubError) as caught:
            build(retries=0).get("/x")
    assert caught.value.fault is Fault.ANSWERED
    assert caught.value.retry_after is None


def test_a_403_with_budget_left_is_a_permission_answer_and_not_a_throttle():
    """GitHub puts `x-ratelimit-remaining` on **every** answer, so the header being
    present says nothing — it is the value `0` that says the budget is gone. Reading
    the header's presence instead would turn every unreadable repository in the scope
    into a sixty-second wait and then a grey line."""
    plenty = _http_error(403, b'{"message":"Resource not accessible"}',
                         {"x-ratelimit-remaining": "4998",
                          "x-ratelimit-reset": "1700000060"})
    with _through(plenty) as (build, _opener):
        with pytest.raises(GitHubError) as caught:
            build(retries=0).get("/x")
    assert caught.value.fault is Fault.ANSWERED
    assert caught.value.retry_after is None


def test_a_bare_429_is_a_throttle_because_that_is_all_github_sends_it_for():
    """The asymmetry with the 403 above, and it is deliberate. The library keeps 429
    as *answered* because a 429 in general may be crawler protection with no stated
    end; here we know whose 429 it is, and GitHub sends that status for a rate limit
    and nothing else. The floor is what makes it safe: `ask` refuses a wait the run
    cannot afford, so a run with a normal budget re-raises rather than hammering."""
    with _through(lambda _r, _t: _http_error(
            429, b'{"message":"too many requests"}')) as (build, _opener):
        with pytest.raises(GitHubError) as caught:
            build(retries=1, sleep=lambda _s: None).get("/x")
    assert caught.value.fault is Fault.TRANSIENT
    assert caught.value.retry_after == THROTTLE_FLOOR_SECONDS


def test_a_throttle_longer_than_the_run_has_left_is_refused_not_slept():
    """`ask`'s veto, from this side of it: **a wait outliving the check's budget is
    not the request layer's to take.** A twenty-minute primary limit inside a
    thirty-second run re-raises at once, and the check reports what it has."""
    slept = []
    clock = _Clock()
    long_wait = _http_error(403, b"{}", {"retry-after": "1200"})
    with _through(long_wait) as (build, opener):
        with pytest.raises(GitHubError) as caught:
            build(deadline=Deadline(30.0, clock=clock), retries=1,
                  sleep=slept.append).get("/x")
    assert slept == []                    # never waited
    assert len(opener.requests) == 1      # never asked twice
    assert caught.value.retry_after == 1200.0


def test_the_reset_window_is_measured_against_a_clock_the_caller_can_name():
    """Read directly, and with **no clock patched**, which is the only way the
    injection point is proved rather than assumed: everything above patches
    `time.time`, and a reader that ignored ``now`` entirely would pass all of it.

    The parameter earns its place beyond the tests, too — a caller that would rather
    not trust two machines' clocks to agree can measure against the response's own
    `Date` header instead of this one's."""
    reset = 1_700_000_060
    headers = Message()
    headers["x-ratelimit-remaining"] = "0"
    headers["x-ratelimit-reset"] = str(reset)
    response = Response(status=403, url="https://api.example.test/x",
                        headers=headers, body=b"{}")
    assert _throttle_wait(response, now=lambda: float(reset - 30)) == 30.0
    assert _throttle_wait(response, now=lambda: float(reset - 600)) == 600.0


def test_the_throttle_is_never_read_out_of_the_body():
    """ADR-0002's rule, on the one classification most tempting to break it for.
    GitHub's throttle bodies do say so in prose, and a body that says every word of
    it with no header on the response is still the permission answer."""
    prose = b'{"message":"You have exceeded a secondary rate limit. Please wait."}'
    with _through(_http_error(403, prose)) as (build, _opener):
        with pytest.raises(GitHubError) as caught:
            build(retries=0).get("/x")
    assert caught.value.fault is Fault.ANSWERED


def _pop(answers):
    """The next canned answer; raises it if it is an exception."""
    answer = answers.pop(0)
    if isinstance(answer, BaseException):
        raise answer
    return answer


# --- What the request layer must **not** absorb -------------------------------

def test_a_status_that_is_neither_success_nor_error_is_still_a_refusal():
    """The boundary is *not 2xx*, and not *4xx or worse*. A 3xx arriving here means
    `fetch` declined to follow it, so there is no payload behind it — and reading
    that as a successful empty answer would hand an aspect `None` where a list was
    promised and blame the repository for it."""
    with _through(_Answer(status=304, body=b"")) as (build, _opener):
        with pytest.raises(GitHubError) as caught:
            build(retries=0).get("/x")
    assert caught.value.status == 304
    assert caught.value.fault is Fault.ANSWERED


def test_a_budget_already_spent_is_not_a_request_at_all():
    """`ask` checks the deadline on entry, so the request that finds the budget gone
    is never issued — rather than issued with a socket timeout of zero, which most
    socket APIs read as *non-blocking* and report as something else entirely."""
    clock = _Clock()
    spent = Deadline(5.0, clock=clock)
    clock.advance(6)
    with _through(_Answer()) as (build, opener):
        with pytest.raises(DeadlineExceeded):
            build(deadline=spent, retries=0).get("/x")
    assert opener.requests == []


def test_the_runs_deadline_is_not_absorbed_into_a_github_error():
    """`DeadlineExceeded` is not a `RemoteError`, and `_attempt` must not widen its
    catch to include it. If it did, every aspect's per-repository handler would turn
    one run-level fact into one line per repository — the shape ADR-0002 §7 rejects —
    and `run()` would never see that the run was cut short.

    The budget has to run out **inside** the request to reach that catch at all, which
    is what the ticking clock is for: `ask` and `fetch` both let it through, and it is
    the body read that finds the budget gone. That is also the realistic shape — a
    server dribbling a byte at a time is the failure the chunked read exists for.

    The budget is **14s and not the 10s it was**, because `_attempt` now reads the
    run's clock once before the request to time it: with a clock that advances on
    every read, one more reader moves where the budget runs out, and at 10s it now
    runs out *before* the request is issued — which proves nothing about this
    catch. The claim is unchanged and so is the step; the ticks are what moved."""
    ticking = _Ticking(step=4.0)
    with _through(_Answer(body=b'{"ok": true}')) as (build, opener):
        with pytest.raises(DeadlineExceeded) as caught:
            build(deadline=Deadline(14.0, clock=ticking), retries=0).get("/x")
    assert opener.requests                # it really did ask
    assert "budget of 14s" in str(caught.value)


def test_an_unreadable_answer_still_grades_the_line():
    """`MALFORMED` is not `TRANSIENT`, and the line has to say so. An answer this
    check cannot read is a defect in the check or in the API — waiting it out changes
    nothing, so it grades WARN and is not counted as an outage on the node."""
    check = _check()
    fake = FakeClient([_repo("platform-a")], sboms={
        "example-org/platform-a": GitHubError("not JSON", status=200,
                                              fault=Fault.MALFORMED)})
    result = check._sbom_check(fake, _repos("platform-a"))
    line = result.reason_entries[0]
    assert line.code is StatusCode.WARN
    assert "could not read" in line.text
    assert check._unreachable == 0


def test_the_backoff_is_actually_slept_before_the_retry():
    """A retry with no wait is not the retry the record describes: an endpoint
    that just failed is asked again in the same millisecond."""
    slept = []
    attempts = []

    def flaky(url, timeout):
        attempts.append(url)
        if len(attempts) == 1:
            raise _transient("502")
        return {}, ""

    client, _ = _client(flaky, sleep=slept.append)
    client.get("/x")
    assert slept == [RETRY_BACKOFF_SECONDS]


def test_the_shipped_per_request_default_is_fifteen_seconds():
    """Pinned as a number, not as `== DEFAULT_REQUEST_TIMEOUT`, which asserts
    nothing. The README, the CHANGELOG and the example config all promise 15s."""
    assert DEFAULT_REQUEST_TIMEOUT == 15.0


def test_every_aspect_reports_its_unreachable_repositories_to_the_node():
    """The node's count is assembled from all seven aspects, and the two that
    build severity bands take a different route to it (`_severity_bands` rather
    than `_finalize`). A count that silently dropped those two would leave a
    Dependabot outage invisible on the only node that grades it."""
    repos = [_repo("platform-a")]
    banded = _check()
    banded._security_advisories(
        FakeClient(repos, errors={("example-org/platform-a", "dependabot"):
                                  _transient()}), _repos("platform-a"))
    assert banded._unreachable == 1

    flat = _check()
    flat._issues(FakeClient(repos, errors={("example-org/platform-a", "issues"):
                                           _transient()}), _repos("platform-a"))
    assert flat._unreachable == 1


def test_actions_counts_each_repository_once_though_it_reads_two_endpoints():
    """It asks for the workflow list and then the runs. Counting per *request*
    would make `2 of 2 repositories read` out of one repository."""
    check = _check()
    repos = _repos("platform-a", "platform-b")
    active = {"workflows": [{"id": 1, "state": "active"}]}
    fake = FakeClient(
        [_repo("platform-a"), _repo("platform-b")],
        workflows={"example-org/platform-a": active},
        runs={"example-org/platform-a": _transient()})
    result = check._actions(fake, repos)
    read = next(e for e in result.reason_entries if e.slug == "read")
    assert read.text == "GitHub did not answer for 1 of 2 repositories"


def test_actions_says_which_of_its_two_reads_failed():
    """Two endpoints, two slugs — and now two sentences, because the slug is not
    on the rendered line."""
    check = _check()
    repos = _repos("platform-a")
    workflows = check._actions(
        FakeClient([_repo("platform-a")],
                   workflows={"example-org/platform-a": _transient()}), repos)
    assert "could not ask GitHub workflows" in workflows.reason_entries[0].text
    runs = check._actions(
        FakeClient([_repo("platform-a")],
                   workflows={"example-org/platform-a":
                              {"workflows": [{"id": 1, "state": "active"}]}},
                   runs={"example-org/platform-a": _transient()}), repos)
    assert "could not ask GitHub (" in runs.reason_entries[0].text


def test_a_permission_error_alone_does_not_add_the_read_line():
    """The `read` line rescues clean readings from an UNDEFINED-only entry set. A
    403 already grades WARN, so there is nothing to rescue and nothing to say."""
    check = _check()
    fake = FakeClient([_repo("platform-a"), _repo("platform-b")],
                      sboms={"example-org/platform-a": _denied()})
    result = check._sbom_check(fake, check._discover(fake))
    assert not [e for e in result.reason_entries if e.slug == "read"]
    assert result.stored_code is StatusCode.WARN


def test_the_deadline_at_the_rate_limit_probe_is_a_reading_not_a_traceback():
    """`DeadlineExceeded` is deliberately not a `RemoteError`, and the
    rate-limit probe is the one client call outside the guarded aspect loop — so
    it needs its own catch or the engine turns the whole check into a traceback
    (little-sister ADR-0040)."""
    check = _check(timeout_seconds=10.0)

    class _Wedged(FakeClient):
        def rate_limit(self):
            raise DeadlineExceeded("the run's timeout of 10s ran out")

    check._make_client = (                                # type: ignore[method-assign]
        lambda token, deadline=None: _Wedged([_repo("platform-a")]))
    result = check.run()
    assert result.stored_code is StatusCode.WARN
    texts = " | ".join(result.reason_texts)
    assert "ran out" in texts
    assert "1 repository in scope" in texts               # the reading survives


def test_the_read_count_counts_every_repository_it_got_an_answer_about():
    """A 404 is an answer — "the feature is not enabled here" — so the repository
    was read. Counting only the 200s would report `1 of 3` for a scope where two
    repositories answered perfectly well."""
    check = _check()
    repos = _repos("platform-a", "platform-b", "platform-c")
    fake = FakeClient(
        [_repo(n) for n in ("platform-a", "platform-b", "platform-c")],
        errors={("example-org/platform-a", "secret_scanning"): _transient(),
                ("example-org/platform-b", "secret_scanning"):
                    _answered("not enabled", 404)})
    result = check._secret_scanning_alerts(fake, repos)
    read = next(e for e in result.reason_entries if e.slug == "read")
    assert read.text == "GitHub did not answer for 1 of 3 repositories"


def test_the_read_count_includes_every_kind_of_miss_not_only_the_quiet_ones():
    """One repository unreachable, one forbidden, one read. The line is about
    coverage, so `1 of 3` — a total that counted only the quiet misses would call
    it `1 of 2` and quietly write the forbidden repository out of the scope."""
    check = _check()
    repos = _repos("platform-a", "platform-b", "platform-c")
    fake = FakeClient(
        [_repo(n) for n in ("platform-a", "platform-b", "platform-c")],
        sboms={"example-org/platform-a": _transient(),
               "example-org/platform-b": _denied()})
    result = check._sbom_check(fake, repos)
    read = next(e for e in result.reason_entries if e.slug == "read")
    assert read.text == "GitHub did not answer for 1 of 3 repositories"


def test_the_nodes_count_reads_as_english_for_exactly_one():
    """It is the sentence an operator sees first on a bad morning; "1 repository
    reads could not be completed" is not it."""
    check = _check()
    one = FakeClient([_repo("platform-a")],
                     sboms={"example-org/platform-a": _transient()})
    check._make_client = lambda token, deadline=None: one  # type: ignore[method-assign]
    assert "1 repository read could not be completed" in \
        " ".join(check.run().reason_texts)

    other = _check()
    two = FakeClient([_repo("platform-a"), _repo("platform-b")],
                     sboms={"example-org/platform-a": _transient(),
                            "example-org/platform-b": _transient()})
    other._make_client = lambda token, deadline=None: two  # type: ignore[method-assign]
    assert "2 repository reads could not be completed" in \
        " ".join(other.run().reason_texts)
