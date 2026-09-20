"""Fixture-based tests for the github-rate-limit check — no live GitHub calls.

Each test is written from a sentence the package claims — in the module, the
README, the example config or the ADR — with the values that sentence is about,
rather than from the branch that implements it.
"""
from __future__ import annotations

import logging
import re

import pytest
from little_sister.checks import CHECK_TYPES, CheckError
from little_sister.status import StatusCode
from little_sister.transport import Fault

from little_sister_github.budget import ledger
from little_sister_github.github import (
    NO_BUDGET_HEADERS,
    GitHubError,
    RateLimitHeaders,
)
from little_sister_github.rate_limit import (
    DEFAULT_ERROR_BELOW,
    DEFAULT_RESOURCES,
    DEFAULT_WARN_BELOW,
    Budget,
    GitHubRateLimitCheck,
)

#: Far enough ahead that `_resets_in` renders whole minutes on any test machine.
_RESET = 4_000_000_000
_ONE_HOUR = 3600
_LOGGER = "little_sister_github.rate_limit"


def _logged(caplog):
    """Every message this check logged, formatted as it would be written."""
    return [record.getMessage() for record in caplog.records
            if record.name == _LOGGER]


class FakeClient:
    """Stands in for GitHubClient: answers `/rate_limit` with a canned payload.

    ``payload`` may be a ``GitHubError`` to raise instead, which is how the
    "could not ask" path is exercised without a network.
    """

    def __init__(self, payload, last_rate_limit=None):
        self._payload = payload
        self.calls: list[str] = []
        # What the real client keeps from the answering response's own headers.
        # `None` — *GitHub sent no budget headers* — is this double's default
        # because these fixtures are payloads and nothing else; the test about the
        # two answers gives it a reading of its own.
        self.last_rate_limit = last_rate_limit

    def get(self, path, params=None):
        self.calls.append(path)
        if isinstance(self._payload, GitHubError):
            raise self._payload
        return self._payload


def _resources(**rows):
    """A `/rate_limit` payload. Each keyword is a resource, given as
    ``(limit, remaining)`` or ``(limit, remaining, reset)``."""
    out = {}
    for name, row in rows.items():
        limit, remaining, *rest = row
        out[name] = {"limit": limit, "remaining": remaining,
                     "used": limit - remaining,
                     "reset": rest[0] if rest else _RESET}
    return {"resources": out}


def _check(**over):
    cfg = {"path": "/github-rate-limit", "token_ref": "env://GITHUB_TOKEN",
           "budgets": (Budget("core", DEFAULT_WARN_BELOW, DEFAULT_ERROR_BELOW),
                       Budget("graphql", DEFAULT_WARN_BELOW,
                              DEFAULT_ERROR_BELOW))}
    cfg.update(over)
    return GitHubRateLimitCheck(**cfg)


def _run(check, payload, last_rate_limit=None):
    fake = FakeClient(payload, last_rate_limit)
    check._make_client = lambda token: fake        # type: ignore[method-assign]
    return check.run(), fake


def _from_config(**over):
    from pathlib import Path
    cfg = {"secrets": {"token": "env://GITHUB_TOKEN"}}
    cfg.update(over)
    return GitHubRateLimitCheck._extra_from_config(cfg, Path("."))


def _lines(result):
    """``{slug: (text, code)}`` for the lines a run produced."""
    return {entry.slug: (entry.text, entry.code)
            for entry in result.reason_entries}


def _spent(check, path, *, remaining, reset, resource="core", limit=5000,
           used=None, now=None, times=1):
    """A charged reading on this check's token, as a `github` check's response
    would have fed it — `times` of them, a second apart, when a test wants the
    attempts counted."""
    import time
    at = time.time() if now is None else now
    counter = None
    for step in range(times):
        counter = ledger().record(
            check.token, path, resource=resource, limit=limit,
            remaining=remaining, used=limit - remaining if used is None else used,
            reset=reset, now=at + step)
    return counter


_ON_A = "/repos/example-org/platform-a/dependabot/alerts"
_ON_B = "/repos/example-org/platform-a/pulls"
_ENDPOINT_SAYS = "— as /rate_limit reports it; nothing here has spent it"


# --- the type ----------------------------------------------------------------

def test_the_type_is_registered_under_its_hyphenated_name():
    """"Registers two check types" — a deployment writes `type: github-rate-limit`
    and the one import line registers it."""
    assert CHECK_TYPES["github-rate-limit"] is GitHubRateLimitCheck


# --- grading -----------------------------------------------------------------

def test_each_resource_is_graded_on_its_own_and_the_node_takes_the_worst():
    """One coded line per resource: the node's code is derived from them, so a
    healthy REST budget does not hide an exhausted GraphQL one."""
    result, _ = _run(_check(),
                     _resources(core=(5000, 4800), graphql=(5000, 200)))
    lines = _lines(result)
    assert lines["core"][1] is StatusCode.OK
    assert lines["graphql"][1] is StatusCode.ERROR
    assert result.stored_code is StatusCode.ERROR


def test_the_three_bands_are_the_ported_thresholds():
    """The shipped defaults: WARN below 1000, ERROR below 500 — and the boundary
    belongs to the healthier band, because the threshold is `below`."""
    check = _check()
    result, _ = _run(check, _resources(
        core=(5000, DEFAULT_WARN_BELOW), graphql=(5000, DEFAULT_WARN_BELOW - 1)))
    assert _lines(result)["core"][1] is StatusCode.OK
    assert _lines(result)["graphql"][1] is StatusCode.WARN

    result, _ = _run(check, _resources(
        core=(5000, DEFAULT_ERROR_BELOW), graphql=(5000, DEFAULT_ERROR_BELOW - 1)))
    assert _lines(result)["core"][1] is StatusCode.WARN
    assert _lines(result)["graphql"][1] is StatusCode.ERROR


def test_an_exhausted_budget_with_a_real_limit_is_red():
    """The single reading this check exists to make. It is a separate test from
    the bands above because `remaining: 0` is also what a row with no `limit`
    would look like, and the two must not be able to trade places."""
    result, _ = _run(_check(), _resources(core=(5000, 0), graphql=(5000, 5000)))
    lines = _lines(result)
    assert lines["core"][1] is StatusCode.ERROR
    assert lines["core"][0].startswith("core: 0 of 5000 requests left")
    assert result.stored_code is StatusCode.ERROR


def test_how_long_a_window_has_left_is_rendered_from_the_two_numbers():
    """Deterministic, because the integration paths below can only assert that a
    clause is present without pinning a machine's clock."""
    from little_sister_github.rate_limit import _resets_in
    assert _resets_in(1000 + _ONE_HOUR, 1000.0) == "resets in 60min"
    assert _resets_in(1000 + 90, 1000.0) == "resets in 1min"
    assert _resets_in(1000 + 30, 1000.0) == "resets in under a minute"
    assert _resets_in(1000, 1000.0) == "resetting now"
    assert _resets_in(900, 1000.0) == "resetting now"


def test_an_imminent_reset_does_not_soften_the_grade():
    """"Graded on remaining alone. The reset time is on the line ... but it is not
    in the grade." Same budget, a reset half a minute away and one hour away — one
    verdict."""
    import time
    soon = int(time.time()) + 30
    later = int(time.time()) + _ONE_HOUR
    result, _ = _run(_check(), _resources(core=(5000, 10, soon),
                                          graphql=(5000, 10, later)))
    lines = _lines(result)
    assert lines["core"][1] is lines["graphql"][1] is StatusCode.ERROR
    # …and the reader can still see which one is about to refill
    assert "under a minute" in lines["core"][0]
    # 59 or 60, depending on where in the second the clock happened to be
    assert re.search(r"resets in (59|60)min", lines["graphql"][0])


def test_a_reset_already_past_reads_as_resetting_now():
    import time
    result, _ = _run(_check(), _resources(core=(5000, 10, int(time.time()) - 5),
                                          graphql=(5000, 5000)))
    assert "resetting now" in _lines(result)["core"][0]


def test_a_payload_with_no_reset_leaves_the_clause_off():
    """A missing reset is not "resetting now" — the clause is simply absent
    rather than making a claim about a window nobody reported."""
    payload = {"resources": {"core": {"limit": 5000, "remaining": 4000},
                             "graphql": {"limit": 5000, "remaining": 4000}}}
    result, _ = _run(_check(), payload)
    text = _lines(result)["core"][0]
    assert "resets" not in text and "now" not in text
    assert text == f"core: 4000 of 5000 requests left {_ENDPOINT_SAYS}"


# --- the ledger's windows (ADR-0007, decision 2) -------------------------------

def test_the_tightest_of_two_windows_grades_the_resource_and_the_line_says_so():
    """"the counter with the least `remaining` grades the resource, and the
    sentence says it is one of several when it is" — against a pristine endpoint,
    which is what two of the three tokens measured report. The grade is the
    tighter window's (WARN) and not the endpoint's (OK), and the line says *2
    windows* and what the other holds."""
    import time
    now = int(time.time())
    check = _check()
    _spent(check, _ON_A, remaining=3932, reset=now + 400)
    _spent(check, _ON_B, remaining=800, reset=now + 100)
    result, _ = _run(check, _resources(core=(5000, 5000, now + 3600),
                                       graphql=(5000, 5000, now + 3600)))
    text, code = _lines(result)["core"]
    assert code is StatusCode.WARN
    assert text == ("core: 800 of 5000 requests left, resets in 1min; 1 of it "
                    "this process's own — the tightest of 2 windows GitHub keeps "
                    "for this token; the other has 3932 left, resets in 6min")
    assert result.stored_code is StatusCode.WARN


def test_a_resource_the_ledger_has_not_heard_of_is_read_from_the_endpoint_and_says_so():
    """"A resource the ledger has heard nothing about from a response is reported
    from the endpoint and says so" — `graphql` here, which nothing in this
    process spends, pristine or not."""
    import time
    now = int(time.time())
    check = _check()
    _spent(check, _ON_B, remaining=4000, reset=now + 400)
    result, _ = _run(check, _resources(core=(5000, 5000, now + 3600),
                                       graphql=(5000, 4900, now + 3570)))
    lines = _lines(result)
    assert lines["graphql"][0] == (
        f"graphql: 4900 of 5000 points left, resets in 59min {_ENDPOINT_SAYS}")
    assert lines["graphql"][1] is StatusCode.OK
    assert not lines["core"][0].endswith(_ENDPOINT_SAYS)


def test_a_pristine_endpoint_alone_is_reported_as_such_and_opens_no_window():
    """The token whose endpoint says `5000 of 5000, 0 used` on every read: before
    the `github` check has run, that is all there is, and the line says where it
    came from. A minute later the same reading with a newer reset is not a second
    window."""
    import time
    now = int(time.time())
    check = _check()
    first, _ = _run(check, _resources(core=(5000, 5000, now + 3600),
                                      graphql=(5000, 5000, now + 3600)))
    assert _lines(first)["core"][0] == (
        f"core: 5000 of 5000 requests left, resets in 59min {_ENDPOINT_SAYS}")
    second, _ = _run(check, _resources(core=(5000, 5000, now + 3660),
                                       graphql=(5000, 5000, now + 3660)))
    assert "windows" not in _lines(second)["core"][0]
    assert ledger().counters(check.token, "core", float(now)) == []


def test_the_endpoints_body_is_one_more_reading_of_the_window_its_reset_names():
    """"The `/rate_limit` body is still read every run and is merged into the
    ledger as one more reading, of whichever counter its `reset` names" — the
    second identity in the logs, whose endpoint restated counter B exactly. The
    node says the newer number, and the endpoint's sample raised nothing this
    process is believed to have spent."""
    import time
    now = int(time.time())
    check = _check()
    _spent(check, _ON_B, remaining=3000, reset=now + 1500, times=3)
    result, _ = _run(check, _resources(core=(5000, 2900, now + 1500),
                                       graphql=(5000, 5000, now + 3600)))
    assert _lines(result)["core"][0] == (
        "core: 2900 of 5000 requests left, resets in 24min; 3 of it this "
        "process's own")
    (counter,) = ledger().counters(check.token, "core", float(now))
    assert counter.attempts == 2 and counter.remaining == 2900


def test_a_real_counter_the_endpoint_alone_reports_is_graded_and_named_as_such():
    """The endpoint reporting a counter with spend that nothing here has spent —
    the other instance's reads on a shared token — is a reading, and it grades:
    the budget is the token's, whoever spends it (ADR-0001)."""
    import time
    now = int(time.time())
    check = _check()
    result, _ = _run(check, _resources(core=(5000, 300, now + 1500),
                                       graphql=(5000, 5000, now + 3600)))
    text, code = _lines(result)["core"]
    assert code is StatusCode.ERROR
    assert text == f"core: 300 of 5000 requests left, resets in 24min {_ENDPOINT_SAYS}"


def test_the_line_says_what_this_process_spent_and_the_three_clauses_reconcile():
    """Backlog #5: the node said what somebody else spends and what a window
    carried before it, and nothing about its own reads — the one number a reader
    can act on. It is a count, not a rate, so it carries no tilde, and it is the
    ledger's `own_spend`: the attempts plus the reading that opened the window."""
    import time
    now = int(time.time())
    check = _check()
    _spent(check, _ON_B, remaining=4900, used=100, reset=now + 1500,
           now=now - 3600)
    _spent(check, _ON_B, remaining=4800, used=200, reset=now + 1500,
           now=now - 60, times=99)
    result, _ = _run(check, _resources(core=(5000, 5000, now + 3600),
                                       graphql=(5000, 5000, now + 3600)))
    text = _lines(result)["core"][0]
    assert "; 100 of it this process's own" in text
    # the whole window is ours, so there is no rate to say about anybody else
    assert "spent by something else" not in text


def test_the_own_spend_clause_comes_before_the_other_two():
    """The three clauses are one sentence about where a window went — this
    process's, somebody else's rate, and what it carried before — and they are said
    in that order, because the reader's own spend is the one they can change."""
    import time
    now = int(time.time())
    check = _check()
    _spent(check, _ON_B, remaining=4000, reset=now - 3700, now=now - 4000)
    _spent(check, _ON_B, remaining=4954, used=46, reset=now + 1530,
           now=now - 3600)
    _spent(check, _ON_B, remaining=2000, used=1546, reset=now + 1530,
           now=now - 268, times=268)
    text = _lines(_run(check, _resources(core=(5000, 5000, now + 3600),
                                         graphql=(5000, 5000, now + 3600)))[0]
                  )["core"][0]
    ours = text.index("this process's own")
    else_ = text.index("spent by something else using this token")
    before = text.index("before this process first read this window")
    assert ours < else_ < before


def test_a_window_nothing_here_spent_says_nothing_about_its_own_spend():
    """A resource this process has not touched — the endpoint's own counter — has
    no spend of ours to report, and the line says what it already said: that the
    numbers are `/rate_limit`'s and nothing here has spent them."""
    import time
    now = int(time.time())
    check = _check()
    result, _ = _run(check, _resources(core=(5000, 300, now + 1500),
                                       graphql=(5000, 5000, now + 3600)))
    text = _lines(result)["core"][0]
    assert "this process's own" not in text
    assert text.endswith(_ENDPOINT_SAYS)


def test_the_line_says_what_something_else_spends_when_it_is_not_negligible():
    """"the spend this process did not make … `~1250/h of it is spent by
    something else using this token`" — measured between this process's own
    readings: an hour, 268 attempts, `used` up by 1,500; the rest is somebody's,
    and it is said with the tilde. Below one percent of the limit an hour it is
    not said."""
    import time
    now = int(time.time())
    check = _check()
    _spent(check, _ON_B, remaining=3500, used=1500, reset=now + 1500,
           now=now - 3600)
    _spent(check, _ON_B, remaining=2000, used=3000, reset=now + 1500,
           now=now - 268, times=268)
    result, _ = _run(check, _resources(core=(5000, 5000, now + 3600),
                                       graphql=(5000, 5000, now + 3600)))
    text = _lines(result)["core"][0]
    assert text.startswith("core: 2000 of 5000 requests left, resets in 24min; ")
    assert "~1230/h of it is spent by something else using this token" in text
    quiet = _check()
    _spent(quiet, _ON_A, remaining=4000, used=1000, reset=now + 1500,
           now=now - 3600)
    _spent(quiet, _ON_A, remaining=3970, used=1030, reset=now + 1500,
           now=now - 28, times=28)
    result, _ = _run(quiet, _resources(core=(5000, 5000, now + 3600),
                                       graphql=(5000, 5000, now + 3600)))
    assert "spent by something else" not in _lines(result)["core"][0]


def test_the_line_says_what_a_window_carried_before_this_process_first_read_it():
    """The hourly job on the deployment's second token opens each window and
    spends thirty to forty-five requests before this process reads it — in
    `first_used`, never in the rate. With the rollover seen, the tightest window
    says it, and there is no floor but zero: with the rollover seen the number is
    a measurement, not an estimate."""
    import time
    now = int(time.time())
    check = _check()
    _spent(check, _ON_B, remaining=4000, reset=now - 1000, now=now - 1500)
    _spent(check, _ON_B, remaining=4954, used=46, reset=now + 2630,
           now=now - 900)
    result, _ = _run(check, _resources(core=(5000, 5000, now + 3600),
                                       graphql=(5000, 5000, now + 3600)))
    assert _lines(result)["core"][0] == (
        "core: 4954 of 5000 requests left, resets in 43min; 1 of it this "
        "process's own; 45 of it were spent by something else before this "
        "process first read this window")


def test_a_window_this_process_opened_says_nothing_about_somebody_else():
    import time
    now = int(time.time())
    check = _check()
    _spent(check, _ON_B, remaining=4000, reset=now - 1000, now=now - 1500)
    _spent(check, _ON_B, remaining=4999, used=1, reset=now + 2630, now=now - 900)
    result, _ = _run(check, _resources(core=(5000, 5000, now + 3600),
                                       graphql=(5000, 5000, now + 3600)))
    assert _lines(result)["core"][0] == (
        "core: 4999 of 5000 requests left, resets in 43min; 1 of it this "
        "process's own")


def test_the_two_clauses_about_something_else_read_as_one():
    import time
    now = int(time.time())
    check = _check()
    _spent(check, _ON_B, remaining=4000, reset=now - 3700, now=now - 4000)
    _spent(check, _ON_B, remaining=4954, used=46, reset=now + 1530,
           now=now - 3600)
    _spent(check, _ON_B, remaining=2000, used=1546, reset=now + 1530,
           now=now - 268, times=268)
    result, _ = _run(check, _resources(core=(5000, 5000, now + 3600),
                                       graphql=(5000, 5000, now + 3600)))
    assert _lines(result)["core"][0] == (
        "core: 2000 of 5000 requests left, resets in 25min; 269 of it this "
        "process's own; ~1230/h of it is spent by something else using this "
        "token, and 45 of it before this process first read this window")


def test_three_windows_are_all_named():
    import time
    now = int(time.time())
    check = _check()
    _spent(check, _ON_A, remaining=3932, reset=now + 400)
    _spent(check, _ON_B, remaining=2441, reset=now + 100)
    _spent(check, "/orgs/example-org/repos", remaining=4100, reset=now + 1220)
    result, _ = _run(check, _resources(core=(5000, 5000, now + 3600),
                                       graphql=(5000, 5000, now + 3600)))
    assert _lines(result)["core"][0] == (
        "core: 2441 of 5000 requests left, resets in 1min; 1 of it this "
        "process's own — the tightest of 3 windows GitHub keeps for this token; "
        "the others have 3932 left, resets in 6min, and 4100 left, resets in "
        "20min")


def test_a_resource_the_endpoint_leaves_out_but_the_reads_spend_is_read_from_them():
    """`dependency_sbom` on an installation whose endpoint leaves it out: the
    reads still carry its headers, and the ledger keys by resource, so the
    watched line is a reading and not a warning about the payload."""
    import time
    now = int(time.time())
    check = _check(budgets=(Budget("dependency_sbom", 30, 10),))
    _spent(check, "/repos/example-org/platform-a/dependency-graph/sbom",
           resource="dependency_sbom", limit=100, remaining=84, reset=now + 50)
    result, _ = _run(check, _resources(core=(5000, 5000, now + 3600)))
    text, code = _lines(result)["dependency_sbom"]
    assert code is StatusCode.OK
    assert text == ("dependency_sbom: 84 of 100 requests left, resets in under "
                    "a minute; 1 of it this process's own")


def test_another_tokens_windows_are_not_this_nodes():
    """"per token (keyed by a digest of the token's value …)": a second check on
    another credential reports its own budget and nothing of this one's."""
    import time
    now = int(time.time())
    check = _check()
    ledger().record("some-other-token", _ON_B, resource="core", limit=5000,
                    remaining=10, used=4990, reset=now + 100, now=float(now))
    result, _ = _run(check, _resources(core=(5000, 5000, now + 3600),
                                       graphql=(5000, 5000, now + 3600)))
    assert _lines(result)["core"][1] is StatusCode.OK
    assert "windows" not in _lines(result)["core"][0]


# --- the units ---------------------------------------------------------------

def test_graphql_is_counted_in_points_and_the_rest_in_requests():
    """"one query can cost many of them — so '500 left' means something different
    there, and the word is the only thing on the line that says so"."""
    result, _ = _run(_check(), _resources(core=(5000, 4000), graphql=(5000, 4000)))
    lines = _lines(result)
    assert "4000 of 5000 requests left" in lines["core"][0]
    assert "4000 of 5000 points left" in lines["graphql"][0]


# --- what the check does not claim -------------------------------------------

def test_a_resource_github_did_not_report_is_a_warning_line_not_a_missing_one():
    """A watched resource that is absent must not simply vanish: a missing line
    reads as a budget that is fine."""
    result, _ = _run(_check(), _resources(core=(5000, 4000)))
    lines = _lines(result)
    assert set(lines) == {"core", "graphql"}
    assert lines["graphql"][1] is StatusCode.WARN
    assert "did not report" in lines["graphql"][0]
    assert result.stored_code is StatusCode.WARN


def test_a_resource_answered_in_the_wrong_shape_is_worded_apart_from_an_absent_one():
    """The two send a reader to different places — one to their own config, the
    other to the payload — and neither may be graded as a budget."""
    result, _ = _run(_check(), {"resources": {"core": [{"limit": 5000}],
                                              "graphql": (5000, 5000)}})
    lines = _lines(result)
    assert lines["core"][1] is lines["graphql"][1] is StatusCode.WARN
    assert "cannot read" in lines["core"][0]
    assert "did not report" not in lines["core"][0]


def test_a_resource_with_no_limit_says_nothing_rather_than_going_red():
    """"A limit of zero is not a budget of nothing, it is the absence of a
    budget." Graded, `remaining: 0` would be a permanent ERROR on an installation
    that does not rate-limit at all.

    Note the pairing with the exhausted-budget test above: there `remaining` is 0
    and the limit is real, and the answer is ERROR. It is the **limit** that
    decides which of the two this is."""
    result, _ = _run(_check(), _resources(core=(0, 0), graphql=(5000, 4000)))
    lines = _lines(result)
    assert lines["core"][1] is StatusCode.UNDEFINED
    assert "no limit" in lines["core"][0]
    # UNDEFINED is skipped when the node's code is derived
    assert result.stored_code is StatusCode.OK


def test_a_row_missing_a_structural_field_is_a_read_failure_not_a_reading():
    """An absent `limit` must not default to 0 and take the "no limit" path: an
    exhausted budget would then read green because a key went missing."""
    payload = {"resources": {"core": {"remaining": 0, "reset": _RESET},
                             "graphql": {"limit": 5000, "remaining": 5000}}}
    result, _ = _run(_check(), payload)
    lines = _lines(result)
    assert lines["core"][1] is StatusCode.WARN
    assert "could not read this resource" in lines["core"][0]
    assert "no limit" not in lines["core"][0]
    assert result.stored_code is StatusCode.WARN


def test_one_malformed_resource_does_not_cost_the_others_their_reading():
    """Per-resource isolation, as every aspect of the `github` type does it. If
    this escaped `run()`, the engine would replace every keyed line — and every
    maintenance pin held against one — with a check-error traceback."""
    payload = {"resources": {"core": {"limit": 5000, "remaining": {"nope": 1}},
                             "graphql": {"limit": 5000, "remaining": 10,
                                         "reset": _RESET}}}
    result, _ = _run(_check(), payload)
    lines = _lines(result)
    assert lines["core"][1] is StatusCode.WARN
    assert "could not read this resource" in lines["core"][0]
    # the healthy reading survives, and it is still the one that decides the node
    assert lines["graphql"][1] is StatusCode.ERROR
    assert "10 of 5000 points left" in lines["graphql"][0]
    assert result.stored_code is StatusCode.ERROR


def test_an_unreadable_endpoint_says_the_asking_failed():
    """Not "your budget is gone": what failed is the read, and the sentence has to
    be one an operator can act on."""
    result, _ = _run(_check(), GitHubError("HTTP 503 for /rate_limit", status=503,
                            fault=Fault.TRANSIENT))
    assert result.stored_code is StatusCode.ERROR
    text = result.reason_entries[0].text
    assert "could not ask GitHub" in text
    assert "503" in text
    assert "left" not in text


def test_a_payload_without_a_resources_object_is_an_error_not_an_empty_reading():
    for payload in ({"rate": {"limit": 5000}},          # a dict, wrong keys
                    [{"core": {"limit": 5000}}],        # not a mapping at all
                    None,                               # an empty body
                    {"resources": ["core"]}):           # resources, wrong shape
        result, _ = _run(_check(), payload)
        assert result.stored_code is StatusCode.ERROR
        assert "without a 'resources' object" in result.reason_entries[0].text


def test_the_reading_and_that_same_responses_headers_go_in_one_line(caplog):
    """The node reports the bucket GitHub looked up by identity; the headers on the
    very response that carried it report the bucket that lookup was **charged** to.
    Held apart here — a body saying the budget is untouched beside headers saying
    159 of it is gone — because that is the case the line exists for: the node
    reads green while the `github` check next to it spends hundreds of calls an
    hour, and without both numbers in one place there is no third fact to settle
    which of them is about the budget being spent."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    _run(_check(), _resources(core=(5000, 5000), graphql=(5000, 5000)),
         RateLimitHeaders(resource="core", limit=5000, remaining=4841, used=159))
    line = _logged(caplog)[-1]
    assert "core: 5000 of 5000 requests left" in line
    assert ("| that response's own headers: core: 4841 of 5000 left, 159 used"
            in line)


def test_a_response_that_stated_no_budget_headers_says_so_in_the_line(caplog):
    """The absent case is worded, not blank. A line that simply stopped after the
    reading would be read as *the two agreed*, which is the one thing it does not
    say."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    _run(_check(), _resources(core=(5000, 4000), graphql=(5000, 4000)))
    assert _logged(caplog)[-1].endswith(
        f"| that response's own headers: {NO_BUDGET_HEADERS}")


def test_the_check_spends_nothing_but_the_one_call():
    """"Reading it does not count against the budget it reports" — which is only
    true while this is the *only* endpoint the check reads."""
    _result, fake = _run(_check(), _resources(core=(5000, 4000),
                                              graphql=(5000, 4000)))
    assert fake.calls == ["/rate_limit"]


# --- pin identity -------------------------------------------------------------

def test_a_line_is_keyed_by_githubs_own_resource_name():
    """The slug is the identity a maintenance pin is held against, so it is
    GitHub's name for the resource and never the line's wording or position
    (little-sister ADR-0050)."""
    first, _ = _run(_check(), _resources(core=(5000, 4000), graphql=(5000, 4000)))
    # a differently ordered config, a different reading, a new resource in front
    other = _check(budgets=(Budget("search", 30, 10),
                            Budget("graphql", 1000, 500),
                            Budget("core", 1000, 500)))
    second, _ = _run(other, _resources(search=(30, 30), graphql=(5000, 12),
                                       core=(5000, 4999)))
    # `core` moved from first place to last and its wording changed; the key an
    # operator's pin is held against did neither.
    assert [e.slug for e in second.reason_entries] == ["search", "graphql", "core"]
    assert _lines(first)["core"][0] != _lines(second)["core"][0]
    assert "core" in _lines(first) and "core" in _lines(second)


def test_a_resource_name_that_is_not_slug_safe_is_still_a_valid_key():
    """Nothing validates a resource name at load, so a config can put a space or a
    slash into one — and a slug reaches a `?reason=` value, where it needs no
    escaping (little-sister ADR-0050)."""
    check = _check(budgets=(Budget("Odd Name/v2", 1000, 500),))
    result, _ = _run(check, {"resources": {"Odd Name/v2": {"limit": 30,
                                                           "remaining": 30}}})
    entry = result.reason_entries[0]
    assert entry.slug == "Odd-Name-v2"
    assert re.fullmatch(r"[A-Za-z0-9._-]+", entry.slug)
    # the *text* keeps the name the operator wrote
    assert entry.text.startswith("Odd Name/v2: 30 of 30")


def test_the_lines_keep_the_order_the_config_declared():
    """"A table whose rows keep their places is readable at a glance; one sorted
    by severity moves the line you were watching."""
    result, _ = _run(_check(), _resources(core=(5000, 10), graphql=(5000, 5000)))
    assert [entry.slug for entry in result.reason_entries] == ["core", "graphql"]


# --- configuration ------------------------------------------------------------

def test_a_config_that_names_no_resources_watches_core_and_graphql():
    extra = _from_config()
    assert tuple(b.name for b in extra["budgets"]) == DEFAULT_RESOURCES
    assert all(b.warn_below == DEFAULT_WARN_BELOW
               and b.error_below == DEFAULT_ERROR_BELOW
               for b in extra["budgets"])


def test_thresholds_fall_back_from_the_resource_to_the_config_to_the_package():
    extra = _from_config(warn_below=2000, resources={
        "core": {"warn_below": 3000, "error_below": 1500},
        "graphql": None,
        "search": {"error_below": 5},
    })
    budgets = {b.name: b for b in extra["budgets"]}
    assert (budgets["core"].warn_below, budgets["core"].error_below) == (3000, 1500)
    # the config's own default for warn, the package's for error
    assert (budgets["graphql"].warn_below,
            budgets["graphql"].error_below) == (2000, DEFAULT_ERROR_BELOW)
    assert (budgets["search"].warn_below, budgets["search"].error_below) == (2000, 5)


def test_a_resource_list_is_refused_with_the_spelling_that_carries_thresholds():
    with pytest.raises(CheckError) as caught:
        _from_config(resources=["core", "graphql"])
    assert "must be a mapping" in str(caught.value)
    assert "`core:` with nothing under it" in str(caught.value)


def test_an_empty_resources_mapping_is_refused():
    """The check would read the rate limit and report nothing about it, while
    looking from the dashboard exactly like one that does."""
    with pytest.raises(CheckError) as caught:
        _from_config(resources={})
    assert "report nothing" in str(caught.value)


def test_a_resources_key_whose_entries_are_all_commented_out_is_refused():
    """YAML parses `resources:` with nothing under it to `None`, not to `{}` —
    and falling back to the default set there would contradict the rule that
    naming the key *replaces* it. Present-and-empty says what it says."""
    with pytest.raises(CheckError) as caught:
        _from_config(resources=None)
    assert "report nothing" in str(caught.value)
    # …while leaving the key out entirely is the documented way to take the default
    assert tuple(b.name for b in _from_config()["budgets"]) == DEFAULT_RESOURCES


def test_an_error_threshold_above_the_warning_one_is_refused_naming_both():
    """No budget could ever land in the warning band, so the config would read as
    though a warning were possible."""
    with pytest.raises(CheckError) as caught:
        _from_config(resources={"core": {"warn_below": 100, "error_below": 500}})
    assert "500" in str(caught.value) and "100" in str(caught.value)
    # equal is allowed: it says "no warning band", which is a thing to mean
    assert _from_config(
        resources={"core": {"warn_below": 100, "error_below": 100}})


def test_the_refusal_names_the_key_the_operator_actually_wrote():
    """The contradiction can be built out of two keys in two places. A message
    blaming `resources.core.error_below` for a number that came from the package
    default would send them to the wrong line of the wrong file."""
    with pytest.raises(CheckError) as caught:
        _from_config(warn_below=400)          # error_below is the package's 500
    message = str(caught.value)
    assert "resource 'core'" in message
    assert "error_below (500)" in message     # the top-level key, unqualified…
    assert "warn_below (400)" in message
    assert "resources.core" not in message    # …because they wrote neither there

    with pytest.raises(CheckError) as caught:
        _from_config(warn_below=400, resources={"core": {"error_below": 900}})
    assert "resources.core.error_below (900)" in str(caught.value)
    assert "warn_below (400)" in str(caught.value)


def test_a_threshold_of_zero_switches_that_band_off_rather_than_being_refused():
    """Unlike `github`'s coverage floor, a threshold of 0 is a legitimate
    statement: watch this resource, do not grade it at that level."""
    extra = _from_config(resources={"core": {"warn_below": 0, "error_below": 0}})
    budget = extra["budgets"][0]
    assert budget.code(0) is StatusCode.OK


def test_a_negative_or_non_numeric_threshold_is_refused_by_name():
    with pytest.raises(CheckError) as caught:
        _from_config(resources={"core": {"error_below": -1}})
    assert "resources.core.error_below" in str(caught.value)
    with pytest.raises(CheckError):
        _from_config(warn_below="lots")
    with pytest.raises(CheckError):
        _from_config(resources={"core": ["warn_below"]})
    # a YAML boolean is not a number, however truthy Python finds it
    with pytest.raises(CheckError):
        _from_config(resources={"core": {"warn_below": True}})


def test_a_resource_name_github_does_not_have_is_not_refused_at_load():
    """"GitHub's set is open ... a load-time refusal would reject a resource that
    exists before it rejected a typo." The typo surfaces as a run-time line."""
    extra = _from_config(resources={"quantum_search": None})
    assert extra["budgets"][0].name == "quantum_search"


def test_the_configured_api_url_reaches_the_client():
    """`api_url:` is what makes this usable against GitHub Enterprise Server, so
    it has to arrive at the request and not only in the config summary."""
    check = _check(api_url="https://ghe.example.org/api/v3")
    client = check._make_client("t")
    assert client._api == "https://ghe.example.org/api/v3"
    assert "https://ghe.example.org/api/v3" in check.config_summary()


def test_the_token_reference_is_required():
    from pathlib import Path
    with pytest.raises(CheckError):
        GitHubRateLimitCheck._extra_from_config({}, Path("."))


# --- what the node's page says ------------------------------------------------

def test_the_shipped_example_loads_as_written_and_with_its_block_uncommented():
    """The example is the file a deployment copies. Both readings of it have to
    load — as shipped, and with the `resources:` block uncommented, which is what
    the block is there for. A comment that becomes an invalid config the moment it
    is used is worse than no example."""
    from pathlib import Path

    import yaml

    raw = Path(__file__).resolve().parents[1].joinpath(
        "examples", "github-rate-limit.yaml").read_text()
    shipped = GitHubRateLimitCheck._extra_from_config(
        yaml.safe_load(raw), Path("."))
    assert tuple(b.name for b in shipped["budgets"]) == DEFAULT_RESOURCES

    lines = raw.splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.startswith("# resources:"))
    uncommented = [*lines[:start],
                   *(re.sub(r"^# ?", "", line) for line in lines[start:])]
    opened = GitHubRateLimitCheck._extra_from_config(
        yaml.safe_load("\n".join(uncommented)), Path("."))
    # every resource the block names, and the inheritance the comments claim
    budgets = {b.name: b for b in opened["budgets"]}
    assert set(budgets) == {"core", "graphql", "search"}
    assert (budgets["graphql"].warn_below,
            budgets["graphql"].error_below) == (DEFAULT_WARN_BELOW,
                                                DEFAULT_ERROR_BELOW)


def test_the_node_carries_the_grading_in_force_not_a_pointer_to_the_knob():
    """"a reader on the node's page has no other way to see them" — the numbers
    live in a config file and a package default, neither of which is a dashboard."""
    summary = _check().config_summary()
    assert "`core` graded" in summary
    assert "WARN below 1000, ERROR below 500 requests" in summary
    assert "WARN below 1000, ERROR below 500 points" in summary


def test_the_summary_shows_this_deployments_numbers_not_the_shipped_ones():
    summary = _check(budgets=(Budget("core", 42, 7),)).config_summary()
    assert "WARN below 42, ERROR below 7 requests" in summary
    assert "1000" not in summary
