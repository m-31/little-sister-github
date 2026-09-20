"""Fixture-based tests for the ledger — GitHub's budget, kept per counter.

Each test is written from a sentence of ADR-0007 or of the ledger's own docstrings,
with the values the sentence is about: the two-counter shape the deployment's logs
showed, the minute-long `dependency_sbom` window, and the arithmetic of the spend
this process did not make. No live call, and no token value anywhere but in the
test that proves it is never kept.
"""
from __future__ import annotations

import threading

from little_sister_github.budget import (
    FOREIGN_RATE_MIN_SECONDS,
    Counter,
    Ledger,
    ledger,
    reduce_path,
    token_key,
)

#: A fixed wall clock, far enough from any real reset to be nobody's window.
_NOW = 1_800_000_000.0
#: The shape the shared token showed: two `core` windows minutes apart, and the
#: SBOM resource's window a minute long.
_RESET_A = int(_NOW) + 34 * 60
_RESET_B = int(_NOW) + 39 * 60
_RESET_SBOM = int(_NOW) + 50

_REPO = "/repos/example-org/platform-a"
#: Counter A served the dependabot, code-scanning and per-workflow actions reads;
#: counter B the secret-scanning, pulls, issues and discovery reads (backlog #3 §6.1).
_ON_A = (f"{_REPO}/dependabot/alerts?state=open&per_page=100",
         f"{_REPO}/code-scanning/alerts?state=open",
         f"{_REPO}/actions/workflows?per_page=100",
         f"{_REPO}/actions/workflows/7/runs?branch=main")
_ON_B = (f"{_REPO}/secret-scanning/alerts?state=open",
         f"{_REPO}/pulls?state=open",
         f"{_REPO}/issues?state=open",
         "/orgs/example-org/teams/platform/repos?per_page=100")


def _read(book, path, *, remaining, reset, resource="core", limit=5000,
          used=None, now=_NOW, token="tok", status=None):
    """One reading, in the shape a response's headers arrive in. `used` is
    `limit - remaining` unless the test says otherwise — GitHub's headers agree
    with each other, and a test about a disagreement passes its own. `status` is
    the response's, where a test is about a `304` (ADR-0011)."""
    return book.record(token, path, resource=resource, limit=limit,
                       remaining=remaining,
                       used=limit - remaining if used is None else used,
                       reset=reset, now=now, status=status)


def _replay(book):
    """The logs' shape, replayed: one run's reads landing on two `core`
    counters and on `dependency_sbom`."""
    for offset, path in enumerate(_ON_A):
        _read(book, path, remaining=4000 - offset, reset=_RESET_A,
              now=_NOW + offset)
    for offset, path in enumerate(_ON_B):
        _read(book, path, remaining=3000 - offset, reset=_RESET_B,
              now=_NOW + offset)
    _read(book, f"{_REPO}/dependency-graph/sbom", resource="dependency_sbom",
          limit=100, remaining=84, reset=_RESET_SBOM, now=_NOW + 8)


# --- the done-when of the first change ------------------------------------------

def test_the_logs_shape_replays_into_two_core_counters_with_the_paths_each_served():
    """"Two counters that both call themselves `core` … which counter a request is
    charged to follows the path" — the ledger observes that split; nothing here
    declares it. Two `core` resets in, two counters out, each carrying what it
    served, tightest first."""
    book = Ledger()
    _replay(book)
    tighter, wider = book.counters("tok", "core", _NOW + 10)
    assert (tighter.reset, wider.reset) == (_RESET_B, _RESET_A)
    assert wider.paths == {"dependabot/alerts", "code-scanning/alerts",
                           "actions/workflows"}
    assert tighter.paths == {"secret-scanning/alerts", "pulls", "issues", "orgs"}
    assert (wider.remaining, tighter.remaining) == (3997, 2997)   # the last read
    assert tighter.charged and wider.charged


def test_the_sbom_read_is_its_own_resource_with_a_window_a_minute_out():
    """"`dependency_sbom` gets the same treatment for free … priced by its own
    `reset`" — a hundred a minute, not five thousand an hour, and the ledger reads
    the window's length off the reset rather than assuming one."""
    book = Ledger()
    _replay(book)
    (sbom,) = book.counters("tok", "dependency_sbom", _NOW + 10)
    assert sbom.limit == 100 and sbom.remaining == 84
    assert 0 < sbom.reset - (_NOW + 10) <= 60
    assert sbom.paths == {"dependency-graph/sbom"}


def test_a_counter_whose_reset_has_passed_is_gone():
    """"A counter whose `reset` has passed is gone" — from every reader, without a
    reading having to arrive first."""
    book = Ledger()
    _replay(book)
    assert len(book.counters("tok", "core", _RESET_A - 1)) == 2
    (left,) = book.counters("tok", "core", _RESET_A)
    assert left.reset == _RESET_B
    assert book.counters("tok", "dependency_sbom", _RESET_SBOM + 1) == []
    assert book.counter_for("tok", "dependabot/alerts", _RESET_A) is None


def test_the_next_reading_on_that_path_opens_the_counter_of_the_new_window():
    """"… the next reading on that path opens the counter of the new window": the
    path's counter is the new window, and the old one is not resurrected."""
    book = Ledger()
    _replay(book)
    later = _RESET_A + 30
    _read(book, _ON_A[0], remaining=4999, reset=_RESET_A + 3600, now=later)
    opened = book.counter_for("tok", "dependabot/alerts", later)
    assert opened is not None and opened.reset == _RESET_A + 3600
    assert opened.paths == {"dependabot/alerts"}
    assert {counter.reset for counter in book.counters("tok", "core", later)} == {
        _RESET_B, _RESET_A + 3600}


def test_the_foreign_rate_is_the_rise_in_used_less_this_processs_attempts():
    """"Between two of its own readings on one counter, `used` rose by this
    process's attempts plus everybody else's; the difference over the interval is a
    rate." Ten attempts in ten minutes while `used` rose by fifty: forty were
    somebody else's, which is 240 an hour."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=4900, used=100, reset=_RESET_A, now=_NOW)
    for attempt in range(1, 11):
        counter = _read(book, _ON_A[1], remaining=4900 - attempt * 5,
                        used=100 + attempt * 5, reset=_RESET_A,
                        now=_NOW + attempt * 60)
    assert counter is not None
    assert counter.attempts == 10
    assert counter.foreign_rate() == 240.0


def test_own_spend_counts_the_reading_that_opened_the_window():
    """`attempts` counts the readings **after** the first, because the first one's
    request is already inside `first_used`. So a window this process opened with a
    charged read has spent one more than `attempts` says, and `own_spend` is the
    number that reconciles with `used`: ten reads here, `used` up by ten from
    nothing, and all ten are ours."""
    book = Ledger()
    for attempt in range(10):
        counter = _read(book, _ON_A[attempt % len(_ON_A)],
                        remaining=5000 - (attempt + 1), used=attempt + 1,
                        reset=_RESET_A, now=_NOW + attempt)
    assert counter is not None
    assert counter.attempts == 9                 # nine after the first
    assert counter.first_charged is True
    assert counter.own_spend == 10               # …and the first makes ten
    assert counter.used == 10                    # which is the whole window


def test_own_spend_does_not_count_a_window_the_free_read_opened():
    """The other side: `/rate_limit` is free, so a counter it opened cost this
    process nothing and `own_spend` counts only what was charged afterwards. The
    opening reading is not ours to claim, and `used` here is somebody else's."""
    book = Ledger()
    opened = _read(book, "/rate_limit", remaining=4000, used=1000,
                   reset=_RESET_A, now=_NOW)
    assert opened is not None and opened.first_charged is False
    assert opened.own_spend == 0
    for attempt in range(1, 4):
        counter = _read(book, _ON_A[0], remaining=4000 - attempt,
                        used=1000 + attempt, reset=_RESET_A,
                        now=_NOW + attempt * 60)
    assert counter is not None
    assert counter.attempts == 3 and counter.own_spend == 3


def test_own_spend_is_not_subtracted_from_the_foreign_rate():
    """The opening request is inside `first_used`, which the foreign rate measures
    *from* — so the rate subtracts `attempts` and must not subtract `own_spend`, or
    it would take that request off twice and under-report what somebody else spends.
    The same replay as the rate's own test, read both ways."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=4900, used=100, reset=_RESET_A, now=_NOW)
    for attempt in range(1, 11):
        counter = _read(book, _ON_A[1], remaining=4900 - attempt * 5,
                        used=100 + attempt * 5, reset=_RESET_A,
                        now=_NOW + attempt * 60)
    assert counter is not None
    assert counter.own_spend == 11 and counter.attempts == 10
    assert counter.foreign_rate() == 240.0       # unchanged by the new field


def test_the_foreign_rate_is_never_below_zero():
    """"… and never below zero": `used` rising by less than the attempts is two
    readings landing either side of somebody's request, not a refund."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=4900, used=100, reset=_RESET_A, now=_NOW)
    for attempt in range(1, 4):
        counter = _read(book, _ON_A[1], remaining=4900, used=100,
                        reset=_RESET_A, now=_NOW + attempt * 200)
    assert counter is not None and counter.attempts == 3
    assert counter.foreign_rate() == 0.0


def test_no_rate_is_read_off_an_interval_shorter_than_the_floor():
    """A run's burst of a hundred reads inside a minute is not an interval to
    measure anybody's pace across — the rate exists only once the readings stand
    `FOREIGN_RATE_MIN_SECONDS` apart, and a minute-long window never has one."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=4900, used=100, reset=_RESET_A, now=_NOW)
    short = _read(book, _ON_A[1], remaining=4700, used=300, reset=_RESET_A,
                  now=_NOW + FOREIGN_RATE_MIN_SECONDS - 1)
    assert short is not None and short.foreign_rate() is None
    long = _read(book, _ON_A[1], remaining=4699, used=301, reset=_RESET_A,
                 now=_NOW + FOREIGN_RATE_MIN_SECONDS)
    assert long is not None and long.foreign_rate() is not None


def test_the_rate_the_node_says_is_rounded_to_tens_and_silent_when_negligible():
    """"~1250/h" is an estimate and reads like one: tens, with a tilde in front
    of it on the line. Below one percent of the limit an hour it is noise and is
    not said."""
    counter = Counter(resource="core", reset=_RESET_A, limit=5000,
                      remaining=2000, used=3000, read_at=_NOW + 3600,
                      first_at=_NOW, first_used=1500, attempts=268,
                      paths=frozenset({"pulls"}))
    assert counter.foreign_rate() == 1232.0
    assert counter.foreign_rate_said() == 1230
    quiet = Counter(resource="core", reset=_RESET_A, limit=5000,
                    remaining=2000, used=3000, read_at=_NOW + 3600,
                    first_at=_NOW, first_used=1500, attempts=1460,
                    paths=frozenset({"pulls"}))
    assert quiet.foreign_rate() == 40.0
    assert quiet.foreign_rate_said() is None


# --- the endpoint's readings -----------------------------------------------------

def test_a_free_read_is_one_more_sample_of_its_counter_and_no_attempt():
    """"The `/rate_limit` body … is merged into the ledger as one more reading, of
    whichever counter its `reset` names" — and it did not cost a request, so it
    raises nothing this process is believed to have spent."""
    book = Ledger()
    _replay(book)
    before = book.counter_for("tok", "pulls", _NOW + 10)
    assert before is not None
    sampled = _read(book, "/rate_limit", remaining=2900, reset=_RESET_B,
                    now=_NOW + 120)
    assert sampled is not None
    assert sampled.reset == _RESET_B
    assert sampled.attempts == before.attempts
    assert sampled.paths == before.paths                  # nothing new served
    assert sampled.remaining == 2900 and sampled.read_at == _NOW + 120


def test_a_pristine_endpoint_reading_opens_no_counter_however_often_it_arrives():
    """"A counter the endpoint alone reports, at its full limit with a reset an
    hour out, is a counter nothing spent this hour" — and on the tokens measured
    that reset moved with every read, so keeping each would be a new counter a
    minute. None of them is one."""
    book = Ledger()
    for minute in range(3):
        opened = _read(book, "/rate_limit", remaining=5000, reset=int(_NOW) + 3600
                       + minute * 60, now=_NOW + minute * 60)
        assert opened is None
    assert book.counters("tok", "core", _NOW + 180) == []
    assert book.tightest("tok", _NOW + 180) is None


def test_an_endpoint_reading_with_spend_is_a_counter_the_endpoint_alone_reports():
    """The other token: the endpoint restated a real counter exactly. Nothing here
    has spent it, and the counter says so (`charged` is false); a second such
    reading with another reset replaces it, because the endpoint reports one
    counter per resource."""
    book = Ledger()
    first = _read(book, "/rate_limit", remaining=3687, reset=_RESET_B, now=_NOW)
    assert first is not None and not first.charged and first.paths == frozenset()
    (only,) = book.counters("tok", "core", _NOW)
    assert only.reset == _RESET_B
    _read(book, "/rate_limit", remaining=4990, reset=_RESET_B + 3600,
          now=_NOW + 60)
    (replaced,) = book.counters("tok", "core", _NOW + 60)
    assert replaced.reset == _RESET_B + 3600


def test_an_endpoint_reading_that_names_a_charged_counter_joins_it():
    """Where the endpoint reports a counter the reads are landing on (the second
    identity in the logs, counter B), the two are one counter — the endpoint
    samples it between runs, and the paths it served stay on it."""
    book = Ledger()
    _replay(book)
    joined = _read(book, "/rate_limit", remaining=2990, reset=_RESET_B,
                   now=_NOW + 30)
    assert joined is not None and joined.charged
    assert len(book.counters("tok", "core", _NOW + 30)) == 2


# --- what is kept and what is not ------------------------------------------------

def test_a_reading_that_names_no_window_is_not_kept():
    """"A counter being whatever GitHub answered with a distinct `reset` epoch" —
    headers without one name no window and cannot be kept as one."""
    book = Ledger()
    assert _read(book, _ON_A[0], remaining=4000, reset=None) is None
    assert book.counters("tok", "core", _NOW) == []


def test_used_is_read_off_limit_and_remaining_when_the_header_is_missing():
    """An installation that sends no `x-ratelimit-used` still has a spend: the
    two numbers it does send say what it is."""
    book = Ledger()
    counter = book.record("tok", _ON_A[0], resource="core", limit=5000,
                          remaining=4800, used=None, reset=_RESET_A, now=_NOW)
    assert counter is not None and counter.used == 200
    # and with nothing to read it off, there is no spend to measure a rate from
    bare = book.record("tok", _ON_A[0], resource="core", limit=None,
                       remaining=None, used=None, reset=_RESET_B, now=_NOW)
    assert bare is not None and bare.used is None
    assert bare.foreign_rate() is None


def test_the_key_is_a_digest_of_the_token_and_never_the_token():
    """"keyed by a digest of the token's value, never the value and never
    logged" — the key contains no part of the token, two tokens have two keys,
    and one token's counters are invisible under the other."""
    token = "ghp_ThisIsNotARealTokenButItMustNeverBeAKey"
    key = token_key(token)
    assert len(key) == 16 and token not in key and "ghp_" not in key
    assert key != token_key(token + "x")
    book = Ledger()
    _read(book, _ON_A[0], remaining=4000, reset=_RESET_A, token=token)
    assert book.counters(token, "core", _NOW)
    assert book.counters(token + "x", "core", _NOW) == []


def test_reduce_path_keeps_what_a_read_was_of_and_drops_the_rest():
    """The resource under the repository — two segments — for anything under
    `/repos/{owner}/{repo}/`; the first segment for everything else; a full URL
    read like a bare path, because `Link` names the next page as one."""
    assert reduce_path(f"{_REPO}/dependabot/alerts?state=open") == (
        "dependabot/alerts")
    assert reduce_path(f"{_REPO}/actions/workflows/12/runs?branch=main") == (
        "actions/workflows")
    assert reduce_path(f"{_REPO}/pulls") == "pulls"
    assert reduce_path("/orgs/example-org/teams/platform/repos") == "orgs"
    assert reduce_path("/user/repos?per_page=100") == "user"
    assert reduce_path("/rate_limit") == "rate_limit"
    assert reduce_path(
        f"https://api.example.test{_REPO}/issues?page=2") == "issues"
    assert reduce_path("") == ""


def test_counter_for_answers_with_the_window_a_path_was_last_charged_to():
    """"The split … moved once inside the logs": a path seen on two open windows
    belongs to the one that answered most recently."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=4000, reset=_RESET_A, now=_NOW)
    _read(book, _ON_A[0], remaining=2000, reset=_RESET_B, now=_NOW + 5)
    moved = book.counter_for("tok", "dependabot/alerts", _NOW + 6)
    assert moved is not None and moved.reset == _RESET_B
    assert book.counter_for("tok", "never-read", _NOW + 6) is None


def test_the_tightest_counter_is_the_least_remaining_across_resources():
    """What an unknown path is priced against: the counter with the least left,
    whatever resource it belongs to."""
    book = Ledger()
    _replay(book)
    tightest = book.tightest("tok", _NOW + 10)
    assert tightest is not None
    assert (tightest.resource, tightest.remaining) == ("dependency_sbom", 84)
    later = _RESET_SBOM + 1                          # the SBOM window is over
    core = book.tightest("tok", later)
    assert core is not None and core.reset == _RESET_B
    # and a window that ends within `outlasting` seconds is left out while it is
    # still open — the guard's "before this run would be through"
    outlasted = book.tightest("tok", _NOW + 10, outlasting=120.0)
    assert outlasted is not None and outlasted.reset == _RESET_B
    assert book.tightest("tok", _NOW + 10, outlasting=3600.0) is None


def test_the_resource_a_path_was_charged_to_outlives_its_window():
    """What the guard falls back on after a rollover: the resource a path last
    landed on, remembered with the reset and kept when the window is pruned;
    nothing for a path never seen. And `tightest` narrowed to one resource."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=4000, reset=_RESET_A, now=_NOW)
    _read(book, "/graphql", resource="graphql", remaining=4900, reset=_RESET_B,
          now=_NOW)
    assert book.resource_of("tok", "dependabot/alerts") == "core"
    assert book.resource_of("tok", "graphql") == "graphql"
    assert book.resource_of("tok", "pulls") is None
    assert book.counters("tok", "core", _RESET_A + 1) == []       # window gone
    assert book.resource_of("tok", "dependabot/alerts") == "core"  # memory kept
    only = book.tightest("tok", _NOW + 10, resource="graphql")
    assert only is not None and only.resource == "graphql"
    assert book.tightest("tok", _RESET_A + 1, resource="core") is None
    assert book.tightest("tok", _RESET_A + 1) is not None          # any resource


def test_the_ledger_is_one_object_behind_a_lock():
    """"a module of this package behind a lock, because the engine runs checks on
    a thread pool": eight threads feeding one counter lose no reading."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=5000, reset=_RESET_A, now=_NOW)

    def feed() -> None:
        for step in range(200):
            _read(book, _ON_A[1], remaining=4000, reset=_RESET_A,
                  now=_NOW + 1 + step)

    threads = [threading.Thread(target=feed) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    (counter,) = book.counters("tok", "core", _NOW + 300)
    assert counter.attempts == 8 * 200
    assert ledger() is ledger()


# --- the spend a window carried before this process first read it ---------------

def test_a_window_whose_predecessor_was_seen_says_what_it_carried_before_us():
    """The number is about somebody else only when this process saw the window
    open — the previous window on that path ended and this one appeared. Then it
    is `first_used` less the opening request: forty-five here, the hourly job's
    on the deployment's second token."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=4000, reset=_RESET_A, now=_NOW)
    opened = _read(book, _ON_A[0], remaining=4954, used=46,
                   reset=_RESET_A + 3600, now=_RESET_A + 600)
    assert opened is not None and opened.before_us == 45


def test_without_a_seen_predecessor_the_number_is_not_claimed():
    """A process started at half past reads a window with thirteen hundred used,
    most of it this deployment's own reads before the restart — not somebody
    else's, and the ledger does not say it is."""
    book = Ledger()
    first = _read(book, _ON_A[0], remaining=3700, used=1300, reset=_RESET_A,
                  now=_NOW)
    assert first is not None and first.before_us is None


def test_a_window_this_process_opened_itself_carried_nothing():
    """One used at the opening reading is this process's own request: zero
    before us, which the node then leaves unsaid."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=4000, reset=_RESET_A, now=_NOW)
    opened = _read(book, _ON_A[0], remaining=4999, used=1,
                   reset=_RESET_A + 3600, now=_RESET_A + 5)
    assert opened is not None and opened.before_us == 0


def test_the_rollover_is_seen_even_when_the_old_window_expired_between_readings():
    """The common case: the last run read the old window minutes before it ended
    and the next run comes after. The old counter is pruned; the memory of its
    reset is not, so the new window still counts as seen opening."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=4000, reset=_RESET_A, now=_NOW)
    assert book.counters("tok", "core", _RESET_A + 1) == []
    opened = _read(book, _ON_A[0], remaining=4900, used=100,
                   reset=_RESET_A + 3600, now=_RESET_A + 900)
    assert opened is not None and opened.before_us == 99


def test_the_endpoints_rollover_opens_with_what_it_carried_and_no_request_of_ours():
    """The endpoint reporting a real counter sees it roll over too, and its
    opening reading was free: nothing is subtracted."""
    book = Ledger()
    _read(book, "/rate_limit", remaining=3000, reset=_RESET_B, now=_NOW)
    opened = _read(book, "/rate_limit", remaining=4960, used=40,
                   reset=_RESET_B + 3600, now=_RESET_B + 60)
    assert opened is not None and opened.before_us == 40


def test_a_304_that_opens_a_window_names_it_as_a_200_would_and_spends_nothing():
    """"A `304` … is recorded in the ledger as a reading of the window the path is
    charged to that spent nothing of it" (ADR-0011). The warm run after a rollover
    is the case: its first reads on both `core` windows come back `304`, and the
    ledger must still keep **two** windows, each knowing the paths it serves (ADR-0007,
    decision 2), with nothing of it counted as this process's spend — a `304`-opened
    window is not the endpoint's one-per-resource kind, and must neither replace
    the window the endpoint reported nor be replaced by the next `304`."""
    book = Ledger()
    _replay(book)                                    # the hour before, read in full
    after = _RESET_B + 3600                          # both windows have rolled over
    reset_a, reset_b = after + 34 * 60, after + 39 * 60
    # The budget check runs every minute and reported the second window first.
    _read(book, "/rate_limit", remaining=4990, used=10, reset=reset_b, now=after - 1)
    _read(book, _ON_A[0], remaining=4950, used=50, reset=reset_a, now=after,
          status=304)
    _read(book, _ON_B[1], remaining=4900, used=100, reset=reset_b, now=after + 1,
          status=304)
    again = _read(book, _ON_A[1], remaining=4949, used=51, reset=reset_a,
                  now=after + 2, status=304)
    tighter, wider = book.counters("tok", "core", after + 3)
    assert (tighter.reset, wider.reset) == (reset_b, reset_a)
    assert wider.paths == {"dependabot/alerts", "code-scanning/alerts"}
    assert tighter.paths == {"pulls"}
    assert book.counter_for("tok", "pulls", after + 3) == tighter
    assert again is not None and again.first_at == after     # merged, not reopened
    assert tighter.first_at == after - 1     # the endpoint's window, merged into too
    assert wider.attempts == 0 and wider.own_spend == 0      # nothing of it is ours
    assert wider.before_us == 50           # …so the whole of it was somebody else's


def test_a_path_moving_to_an_earlier_window_is_not_a_rollover():
    """The routing moved once in the logs: a path that lands on a window with an
    earlier reset than the one it was last seen on did not watch that window
    open."""
    book = Ledger()
    _read(book, _ON_A[0], remaining=4000, reset=_RESET_B, now=_NOW)
    moved = _read(book, _ON_A[0], remaining=2000, reset=_RESET_A, now=_NOW + 5)
    assert moved is not None and moved.before_us is None
