"""The ledger: GitHub's budget, read where it is spent and kept per counter.

Every REST answer carries ``x-ratelimit-*`` headers that say which budget the
request in hand was charged to and what is left of it. This module keeps those
readings for the life of the process — per token, per resource, one record per
**counter** — so that both check types report the budget the reads actually
spend, rather than the one ``GET /rate_limit`` reports, which for some tokens is
a counter nothing spends and for the rest is one of two (ADR-0007).

A **counter** is whatever GitHub answered with a distinct ``reset`` epoch. Nothing
here says which request path lands on which counter: the split is GitHub's
routing, it moved once inside the logs the record was written from, and a table
would have been wrong from that day on. The ledger *observes* the split — a
counter carries the paths GitHub **routed** to it, which is not the same set as
the paths it was charged for: a `304` names its counter and spends none of it
(ADR-0011) — and nothing declares it.

The ledger is one object for the process, behind a lock, because the engine runs
checks on a thread pool and two checks sharing a credential feed the same
counters. It is not persisted: an hour's memory is refilled by the first run
after a restart, and the state layer would be paid for nothing. It is keyed by a
digest of the token's value — never the value, and the digest is never logged.
"""
from __future__ import annotations

import hashlib
import threading
import urllib.parse
from dataclasses import dataclass, replace

#: How long two of this process's own readings on one counter must stand apart
#: before the difference between them is read as a **rate**. Five minutes: a
#: counter's readings arrive in bursts — a run of the `github` check reads it a
#: hundred times inside a minute and then not for a quarter of an hour — and the
#: spend of somebody else's whole run landing inside a ten-second burst would
#: otherwise read as tens of thousands an hour. A resource whose window is a
#: minute long (`dependency_sbom`) never gets a rate at all, which is correct:
#: nothing measured across a minute is a rate.
FOREIGN_RATE_MIN_SECONDS = 300.0

#: The share of a counter's limit, per hour, below which a foreign rate is not
#: worth a clause on the node: one percent — fifty an hour on a budget of five
#: thousand. Below that the estimate is noise from a few requests landing on the
#: wrong side of a reading; above it, it is somebody's job.
FOREIGN_RATE_SAID_SHARE = 0.01

#: What a budget is counted in. GitHub's REST budgets are requests; the GraphQL
#: budget is **points**, and one query can cost many of them — so "500 left"
#: means something different there, and the word is the only thing on a line
#: that says so. Kept here because both check types write the word: the node
#: for every watched resource, the guard for the window it prices.
RESOURCE_UNITS: dict[str, str] = {"graphql": "points"}
DEFAULT_UNIT = "requests"


def unit_of(resource: str) -> str:
    """The word a budget of ``resource`` is counted in."""
    return RESOURCE_UNITS.get(resource, DEFAULT_UNIT)


#: Reads GitHub does **not** charge to the budget they report, in reduced form.
#: `/rate_limit` says so in its documentation and in every reading behind ADR-0007
#: (a pristine token's endpoint reads stayed at zero used while ordinary reads
#: climbed). A reading from one of these is merged as one more sample of the
#: counter its `reset` names and counts as no attempt, so it never inflates what
#: this process is believed to have spent.
FREE_PATHS = frozenset({"rate_limit"})


def token_key(token: str) -> str:
    """The ledger's key for a token: a digest of its value, never the value.

    Sixteen hex characters of SHA-256 — plenty to keep two tokens apart, and
    short enough that a debugger showing the ledger's keys shows nothing anybody
    could use. It is never written into a log line either; the ledger has no
    line of its own to write.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def reduce_path(path: str) -> str:
    """A request path in the form the ledger keeps: what the read was *of*.

    ``/repos/{owner}/{repo}/dependabot/alerts?state=open`` becomes
    ``dependabot/alerts``; ``/repos/{owner}/{repo}/actions/workflows/12/runs``
    becomes ``actions/workflows`` — the resource under the repository, its first
    two segments, with the repository, the ids and the query gone. Anything not
    under a repository keeps its first segment alone: ``/orgs/…`` is ``orgs``,
    ``/user/repos`` is ``user``, ``/rate_limit`` is ``rate_limit``. A full URL is
    read the same way as a bare path, because GitHub's ``Link`` header names the
    next page as one.

    Two segments and not the whole tail, so that a counter's path set names what
    the counter *serves* and stays the size of the API rather than growing by one
    entry per workflow id — and so that it matches the endpoint an aspect is
    priced by (`GitHubCheck.ASPECT_ENDPOINT`) once that loses its leading slash.
    """
    bare = urllib.parse.urlsplit(path).path
    parts = [part for part in bare.split("/") if part]
    if len(parts) >= 4 and parts[0] == "repos":
        return "/".join(parts[3:5])
    return parts[0] if parts else ""


@dataclass(frozen=True)
class Counter:
    """One window of one budget, as this process has seen it.

    Frozen, and replaced rather than mutated on every reading, so that a counter
    handed out of the ledger is a consistent snapshot however many threads are
    feeding the ledger meanwhile.
    """

    #: What GitHub named on `x-ratelimit-resource` — `core`, `dependency_sbom` —
    #: or the empty string when it named nothing.
    resource: str
    #: The epoch second the window ends at. **This is the counter's identity**: two
    #: readings with the same resource and the same `reset` are two readings of one
    #: counter, and a window whose `reset` has passed is gone.
    reset: int
    limit: int | None
    remaining: int | None
    used: int | None
    #: When the last reading was taken (wall clock, epoch seconds).
    read_at: float
    #: When the first reading was taken, and what `used` said then — the two
    #: numbers the foreign rate is measured from.
    first_at: float
    first_used: int | None
    #: How many **charged** readings this process recorded here after the first
    #: one — its own attempts since then, each of which raised `used` by one.
    attempts: int
    #: The reduced request paths (`reduce_path`) this process's reads landed on
    #: this counter with — charged, or answered `304` (ADR-0011), since either
    #: says where the path is charged. Empty for a counter the endpoint alone
    #: reports.
    paths: frozenset[str]
    #: What the window already carried when this process first read it, **when
    #: this process saw the window open** — the previous window on the opening
    #: path had ended and this one appeared — and ``None`` otherwise. Only then
    #: is the number about somebody else: a process started at half past reads a
    #: window with thirteen hundred used, most of it this deployment's own
    #: requests before the restart, and must not call that foreign. With the
    #: rollover seen it is `first_used` less the opening request (none for a
    #: free reading), a measurement rather than an estimate — the hourly job that
    #: opens every window on one deployment's token is in this number and in no
    #: rate, because it spends before this process's first reading.
    before_us: int | None = None
    #: Whether the reading that **opened** this counter was a charged one of this
    #: process's. `attempts` deliberately counts only the readings after the first,
    #: because the first one's request is already inside `first_used` and the
    #: foreign rate subtracts `attempts` from what `used` has risen by since — so
    #: without this bit the counter cannot say how much of the window is its own
    #: without being wrong by one exactly when this process opened the window.
    #: Read by :attr:`own_spend` and by nothing else: feeding it into
    #: :meth:`foreign_rate` would subtract that opening request twice and make the
    #: line under-report what somebody else spends.
    first_charged: bool = False

    @property
    def own_spend(self) -> int:
        """Requests **this process** has had charged to this window.

        `attempts` plus the opening reading when that was charged. The scope is
        the counter's own: a window this process opened, it has spent all of; a
        window that was already open when this process first read it carries
        spend this process cannot have made, which is what `before_us` and the
        foreign rate are for.
        """
        return self.attempts + (1 if self.first_charged else 0)

    @property
    def charged(self) -> bool:
        """Whether this process's own reads have landed here — a response on a
        path, whether GitHub charged it or answered `304` — as opposed to a
        counter known only from `/rate_limit`, which nothing here has read on a
        path. What this process *spent* here is `own_spend`."""
        return bool(self.paths)

    def foreign_rate(self) -> float | None:
        """Requests an hour spent on this counter by something that is not this
        process, or ``None`` while the ledger cannot say.

        Between this process's first and last readings, `used` rose by this
        process's own attempts plus everybody else's; the difference over the
        interval is their rate — under one assumption, that they keep the pace
        they kept, which is why every rendering of it carries a tilde. Never
        below zero: a difference smaller than the attempts is two readings landing
        either side of somebody's request, not a refund. ``None`` until the
        interval is `FOREIGN_RATE_MIN_SECONDS` long, because nothing shorter is
        a rate.
        """
        if self.used is None or self.first_used is None:
            return None
        interval = self.read_at - self.first_at
        if interval < FOREIGN_RATE_MIN_SECONDS:
            return None
        foreign = max(0, self.used - self.first_used - self.attempts)
        return foreign * 3600.0 / interval

    def foreign_rate_said(self) -> int | None:
        """The foreign rate as the node says it — rounded to tens, and only when
        it is at least `FOREIGN_RATE_SAID_SHARE` of the limit an hour."""
        rate = self.foreign_rate()
        if rate is None or self.limit is None or self.limit <= 0:
            return None
        if rate < self.limit * FOREIGN_RATE_SAID_SHARE:
            return None
        return int(round(rate, -1))


class Ledger:
    """Every counter this process has seen, per token and resource, while its
    window lasts. One instance serves the process (`ledger()`); the class is
    public so a test can hold one of its own."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (token digest, resource, reset epoch) → the counter as last read.
        self._counters: dict[tuple[str, str, int], Counter] = {}
        # (token digest, resource, reduced path) → the latest `reset` a reading on
        # that path has named. Outlives the counter it names, which is pruned
        # when its window ends: the next run's reading on the path then carries a
        # later `reset`, and that is how the ledger knows it watched the new
        # window open rather than arriving in the middle of it (`Counter.before_us`).
        self._last_reset: dict[tuple[str, str, str], int] = {}

    def _prune(self, now: float) -> None:
        """Forget every window whose end has passed. Called under the lock by
        every reader and writer, so the ledger never answers from a window GitHub
        has already replaced — the next reading on that path opens the new one."""
        for key in [key for key, counter in self._counters.items()
                    if counter.reset <= now]:
            del self._counters[key]

    def record(self, token: str, path: str, *, resource: str,
               limit: int | None, remaining: int | None, used: int | None,
               reset: int | None, now: float,
               status: int | None = None) -> Counter | None:
        """One reading — the budget headers of one response, or one resource row
        of a `/rate_limit` body given as if it were one — merged into the counter
        its ``reset`` names, opening it when it is new.

        ``status`` is the HTTP status the reading came with, where the caller knows
        it, and it decides one thing: a `304` is a reading **of the window the
        path is charged to** — it names the counter as a `200` does — that did
        not spend any of it.

        Returns the counter as it now stands, or ``None`` when the reading opened
        nothing: a reading with no ``reset`` names no window and cannot be kept;
        and a reading from a **free** path (`FREE_PATHS`) that finds no counter
        with its ``reset`` and reports nothing used is the endpoint describing a
        counter nothing spent this hour — on the tokens measured the endpoint
        answered `5000 of 5000, 0 used` with a ``reset`` an hour out on every
        read, so keeping each of those would have made a new counter a minute.
        The tightest rule ignores such a counter, and so, here, does the ledger.
        The endpoint reports one counter per resource, so at most one counter it
        alone reports stands per resource: a second replaces the first.
        """
        if reset is None:
            return None
        if used is None and limit is not None and remaining is not None:
            used = limit - remaining
        served = reduce_path(path)
        # **Two questions about a reading, and they are not the same one.**
        # *Routed*: did this process's own request land on this counter — a
        # response on a path says where the path is charged, `200` or `304`
        # alike, and that is what `paths`, `counter_for` and the node's window
        # count are built on; the endpoint's row is not routed, it describes a
        # counter by identity. *Spent*: did GitHub charge for it — a `FREE_PATHS`
        # read costs nothing by its endpoint, a `304` costs nothing by its
        # answer (GitHub does not charge an authorized conditional request it
        # answers *not modified*; measured on this deployment's token,
        # 2026-09-19), and `attempts` is what `foreign_rate` subtracts from the
        # rise in `used`, so a free reading counted as spent makes this process
        # look busier than it was and somebody else quieter. Keeping the two
        # apart is what stops a `304` that opens a window from being taken for
        # the endpoint's one-counter-per-resource kind below: the warm run after
        # a rollover opens each `core` window with a `304`, and read as
        # endpoint-only those two windows would replace each other, the node
        # showing one where there are two and the guard pricing against
        # whichever was read last.
        routed = served not in FREE_PATHS
        spent = routed and status != 304
        key = (token_key(token), resource, reset)
        seen = (key[0], resource, served)
        with self._lock:
            self._prune(now)
            previous = self._last_reset.get(seen)
            if previous is None or reset > previous:
                self._last_reset[seen] = reset
            current = self._counters.get(key)
            if current is None:
                if not routed and not used:
                    return None
                if not routed:
                    for other_key, other in list(self._counters.items()):
                        if other_key[:2] == key[:2] and not other.charged:
                            del self._counters[other_key]
                # Seen opening: this path's previous window ended before this
                # one, so what the window carries is what others spent between
                # its start and this reading — less this reading itself when it
                # was charged. A path that moved to an *earlier* window did not
                # watch it open.
                opened = previous is not None and previous < reset
                counter = Counter(
                    resource=resource, reset=reset, limit=limit,
                    remaining=remaining, used=used, read_at=now, first_at=now,
                    first_used=used, attempts=0,
                    paths=frozenset({served}) if routed else frozenset(),
                    before_us=(max(0, used - (1 if spent else 0))
                               if opened and used is not None else None),
                    first_charged=spent)
            else:
                counter = replace(
                    current, limit=limit, remaining=remaining, used=used,
                    read_at=now,
                    attempts=current.attempts + (1 if spent else 0),
                    paths=current.paths | {served} if routed else current.paths)
            self._counters[key] = counter
            return counter

    def counters(self, token: str, resource: str, now: float) -> list[Counter]:
        """This token's live counters for one resource, **tightest first** — the
        one with the least remaining grades the resource (ADR-0007, decision 2)."""
        digest = token_key(token)
        with self._lock:
            self._prune(now)
            found = [counter for (key, res, _), counter in self._counters.items()
                     if key == digest and res == resource]
        return sorted(found, key=lambda counter: (
            counter.remaining is None, counter.remaining or 0, counter.reset))

    def counter_for(self, token: str, served: str, now: float) -> Counter | None:
        """The counter this token's reads of ``served`` — a path in reduced form —
        were last charged to, or ``None`` when no such read has been seen in a
        window still open. Where two windows both served the path (the routing
        moved between them), the one read most recently answers."""
        digest = token_key(token)
        with self._lock:
            self._prune(now)
            found = [counter for (key, _, _), counter in self._counters.items()
                     if key == digest and served in counter.paths]
        return max(found, key=lambda counter: counter.read_at, default=None)

    def resource_of(self, token: str, served: str) -> str | None:
        """The resource this token's reads of ``served`` were last charged to —
        `core`, `graphql` — or ``None`` for a path never seen. Remembered with
        the last reset per path, so it outlives the window it was seen on: the
        first run after a rollover knows no window for a path, but it knows
        which budget the path is charged to, and that is what its fallback is
        priced against rather than whichever window happens to be open."""
        digest = token_key(token)
        with self._lock:
            seen = [(reset, resource)
                    for (key, resource, path), reset in self._last_reset.items()
                    if key == digest and path == served]
        return max(seen, default=(0, None))[1]

    def tightest(self, token: str, now: float, *, outlasting: float = 0.0,
                 resource: str | None = None) -> Counter | None:
        """The live counter of this token with the least remaining — across
        every resource, or of one ``resource`` — what the guard prices a path
        against when the ledger has not yet seen where that path lands.
        ``outlasting`` narrows it to the windows still open that many seconds
        from now: the guard passes the length of a run, because a window that
        ends before the run would cannot be the one it is guarding against,
        whatever it has left."""
        digest = token_key(token)
        with self._lock:
            self._prune(now)
            found = [counter for (key, res, _), counter in self._counters.items()
                     if key == digest and counter.remaining is not None
                     and counter.reset > now + outlasting
                     and (resource is None or res == resource)]
        return min(found, key=lambda counter: (counter.remaining or 0,
                                               counter.reset), default=None)


_LEDGER = Ledger()


def ledger() -> Ledger:
    """The process-wide ledger: the one object both check types feed and read.
    A function rather than the name itself so that the tests can hand every test
    a ledger of its own without one test's windows leaking into the next."""
    return _LEDGER
