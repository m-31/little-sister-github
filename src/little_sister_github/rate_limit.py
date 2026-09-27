"""The ``github-rate-limit`` check type: what is left of a token's API budget.

A second check type in this package, and deliberately not an eighth aspect of
``github``: a rate limit belongs to the **token**, not to an account. Two
``github`` checks sharing a credential share one budget, a token spent by other
tooling has a budget this package does not control — and ``GitHubCheck.measure``
*skips its whole run* when the budget is low, so an aspect inside it would go
quiet at exactly the moment the budget is the story. The reasoning is
``docs/adr/0001-a-second-check-type-in-this-package.md``.

The node is **flat**: one coded entry per watched resource (little-sister
ADR-0042), keyed by GitHub's own resource name, so one page shows every budget
and a maintenance pin on ``core`` survives a config that starts watching
``search`` next year.

A run is two halves (little-sister ADR-0086): :meth:`GitHubRateLimitCheck.measure`
asks GitHub and the ledger and hands back **one reading per watched resource** —
each about that account's budget for that resource, once GitHub has said whose token
it is — and
:meth:`GitHubRateLimitCheck.grade` writes each resource's line from its reading and
the instant it is given (ADR-0014).

Everything imported from little-sister below is part of its **check-authoring
surface** (architecture.md §11), which is what the ``require_api(3)`` in this
package's ``__init__`` pins.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from little_sister import values
from little_sister.checks import (
    Check,
    CheckError,
    CheckResult,
    Entry,
    Measurement,
    config_markdown,
    parse_secret_refs,
    plain,
    register,
)
from little_sister.reasons import clip, slug
from little_sister.status import StatusCode
from little_sister.transport import Deadline, DeadlineExceeded, Fault

from little_sister_github.budget import DEFAULT_UNIT as budget_default_unit
from little_sister_github.budget import RESOURCE_UNITS as budget_units
from little_sister_github.budget import Counter
from little_sister_github.budget import ledger as shared_ledger
from little_sister_github.github import (
    GITHUB_API,
    GitHubClient,
    GitHubError,
    _first_sight,
    _resets_in,
    budget_said,
)

logger = logging.getLogger(__name__)

#: The endpoint. Reading it **does not count against the budget it reports**,
#: which is what lets this check run every minute beside a `github` check that
#: runs every fifteen — and why it needs no rate-limit guard of its own. It is no
#: longer the line's *source* where the ledger has one (ADR-0007): it reports one
#: counter per resource where a token has two, and for some tokens one nothing
#: spends. It is still read every run, because it is free, because it is the
#: only source for a resource nothing in this process spends, and because where
#: it does report a real counter it samples that counter between runs.
RATE_LIMIT_PATH = "/rate_limit"

#: The clause a line carries when its numbers are the endpoint's and nothing in
#: this process has been charged to that resource — `graphql`, `search`, or
#: `core` before the first `github` run after a restart. Said, because the
#: endpoint's number is the weaker reading and a reader comparing it with the
#: `github` check's trace should know which one they are looking at.
ENDPOINT_SAYS = "— as /rate_limit reports it; nothing here has spent it"

#: Watched when a config names no `resources:` block. `core` is what this
#: package's other check type spends, and since ADR-0008 so is `graphql` — a few
#: points a run for the dependency-graph query; it was in the default set before
#: that because a token is rarely used by one tool alone, and a GraphQL budget
#: nobody watches is the one that runs out during an incident. Everything else
#: GitHub reports —
#: `search`, `code_search`, `dependency_snapshots`, `code_scanning_upload`,
#: `integration_manifest`, `actions_runner_registration`, `dependency_sbom`,
#: `scim` — is one line of config away.
DEFAULT_RESOURCES = ("core", "graphql")

#: The thresholds a resource takes when neither it nor the config's top level
#: names its own. The pair is the one the dashboard this was ported from used,
#: and it is stated in **calls**, not in a fraction of the limit: an operator
#: reasons about how many requests are left, and a percentage of a limit they
#: cannot see is not that number.
DEFAULT_WARN_BELOW = 1000
DEFAULT_ERROR_BELOW = 500

#: What a budget is counted in — the ledger's table, which the `github` guard
#: reads too, so the two lines about one window say one word.
RESOURCE_UNITS = budget_units
DEFAULT_UNIT = budget_default_unit


def _non_negative_int(value: object, field: str) -> int:
    """A configured threshold. ``0`` is allowed and switches its band off — a
    resource watched without being graded at that level — which is why this is not
    ``github``'s ``_positive_int``: there the floor is a backstop that must not be
    disabled, here it is a threshold whose absence is a legitimate thing to say.

    Note what the ordering rule below then implies: ``error_below: 0`` switches the
    red band off on its own, but switching only the *amber* one off means zeroing
    **both**, because an error threshold may not sit above a warning threshold."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CheckError(
            f"github-rate-limit '{field}' must be an integer of at least 0")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise CheckError(
            f"github-rate-limit '{field}' must be an integer of at least 0"
        ) from error
    if parsed < 0:
        raise CheckError(
            f"github-rate-limit '{field}' must be an integer of at least 0")
    return parsed


@dataclass(frozen=True)
class Budget:
    """One resource this check watches, and the two numbers it is graded at.

    A typed value built at the config seam rather than a bag the run reaches into
    (little-sister ADR-0026): a threshold that is a string, or a resource whose
    two thresholds contradict each other, is then a load-time refusal instead of
    a comparison that quietly always answers the same way.
    """

    name: str
    warn_below: int
    error_below: int

    @property
    def unit(self) -> str:
        return RESOURCE_UNITS.get(self.name, DEFAULT_UNIT)

    def code(self, remaining: int) -> StatusCode:
        """This resource's verdict for a remaining budget.

        Graded on ``remaining`` **alone**. The reset time is on the line so a
        reader can see that a red budget is about to refill, but it is not in the
        grade: a check that went green because relief was minutes away would be
        silent for the run that is failing right now.
        """
        if remaining < self.error_below:
            return StatusCode.ERROR
        if remaining < self.warn_below:
            return StatusCode.WARN
        return StatusCode.OK


@register("github-rate-limit")
class GitHubRateLimitCheck(Check):
    """Report what is left of this token's GitHub API budget, one line per
    resource.

    Point it at the **same token** a `github` check uses and it explains that
    check's skipped runs; point it at another and it reports another budget. A
    budget is per credential, so which token this one names is the whole scope of
    the check.
    """

    def __init__(self, *, budgets: tuple[Budget, ...],
                 api_url: str = GITHUB_API, token_ref: str,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Resolved **once here** from the reference the config names (little-sister
        # ADR-0023). An unresolvable reference leaves this empty and pins the check
        # to a visible ERROR without ever measuring.
        self.token = self.resolve_secret(token_ref)
        # In the order the config declared them, which is the order they are
        # reported in. A table whose rows keep their places is readable at a
        # glance; one sorted by severity moves the line you were watching.
        self.budgets = budgets
        self.api_url = api_url
        #: Whose budget this token spends, as GitHub names the account — asked once
        #: per process, because it does not change while the token does not
        #: (ADR-0014 §6). `None` while nobody has asked, or while asking has not
        #: been answered; `_account_settled` says which.
        self._account: str | None = None
        #: Whether the question is **settled**: answered with a login, or answered
        #: *no user here* — an installation token, which `/user` refuses. Only a
        #: failure GitHub did not answer leaves it open, to be asked again.
        self._account_settled = False

    @classmethod
    def _threshold(cls, block: dict[str, Any], key: str, fallback: int,
                   resource: str) -> tuple[int, str]:
        """One threshold, and **the config key it came from** — the resource's own
        or the top-level default it fell back to."""
        if key in block:
            return (_non_negative_int(block[key], f"resources.{resource}.{key}"),
                    f"resources.{resource}.{key}")
        return fallback, key

    @classmethod
    def _extra_from_config(cls, config: dict[str, Any],
                           base_dir: Path) -> dict[str, Any]:
        default_warn = _non_negative_int(
            config.get("warn_below", DEFAULT_WARN_BELOW), "warn_below")
        default_error = _non_negative_int(
            config.get("error_below", DEFAULT_ERROR_BELOW), "error_below")
        # **Absent** and **present but empty** are different statements, and YAML
        # makes the second one easy to write by accident: a `resources:` whose
        # every entry is commented out parses to `None`, not to `{}`. Falling back
        # to the default set there would quietly contradict the documented rule
        # that naming the key *replaces* the set — so the key that is present says
        # what it says, and an empty one is refused.
        if "resources" not in config:
            resources: object = dict.fromkeys(DEFAULT_RESOURCES)
        else:
            resources = config["resources"] if config["resources"] is not None else {}
        if not isinstance(resources, dict):
            # A list is the shape somebody reaches for first, and it cannot carry
            # per-resource thresholds — so it is refused with the spelling that
            # can, rather than accepted as a second syntax for half the feature.
            raise CheckError(
                "github-rate-limit 'resources' must be a mapping of resource "
                "name to its thresholds — write `core:` with nothing under it "
                "to take the defaults")
        if not resources:
            raise CheckError(
                "github-rate-limit 'resources' is empty — the check would read "
                "the rate limit and report nothing about it. Remove the key to "
                "watch " + " and ".join(DEFAULT_RESOURCES) + ", or name a "
                "resource.")
        budgets = []
        for name, block in resources.items():
            resource = str(name)
            if block is None:
                block = {}
            if not isinstance(block, dict):
                raise CheckError(
                    f"github-rate-limit 'resources.{resource}' must be a mapping "
                    f"of thresholds, or empty to take the defaults")
            # Each threshold is named by **where it was actually written**. The
            # pair below can be refused for a contradiction the operator built out
            # of two keys in two places, and a message that blamed
            # `resources.<name>` for a number they never typed there would send
            # them to the wrong line of the wrong file.
            warn, warn_key = cls._threshold(
                block, "warn_below", default_warn, resource)
            error, error_key = cls._threshold(
                block, "error_below", default_error, resource)
            if error > warn:
                # Not a preference: with the error threshold above the warning
                # one, no remaining budget can ever land in the warning band, and
                # the config would read as though a warning were possible.
                raise CheckError(
                    f"github-rate-limit resource '{resource}': "
                    f"{error_key} ({error}) is above {warn_key} ({warn}), so no "
                    f"budget could ever be a warning — error_below is the lower "
                    f"of the two")
            budgets.append(Budget(resource, warn, error))
        # No allow-list of resource names. GitHub's set is open — it has grown
        # several times — so a load-time refusal would reject a resource that
        # exists before it rejected a typo. A name GitHub does not report is a
        # WARN line at run time instead, which says the same thing in the place
        # that can tell the two apart.
        return {
            "budgets": tuple(budgets),
            "api_url": str(config.get("api_url", GITHUB_API)),
            # `secrets: {token: …}` — required, so a deployment can watch the
            # budget of each team's credential separately.
            "token_ref": parse_secret_refs(config, "token")["token"],
        }

    def config_summary(self) -> str:
        """What this check ran with — including **the grading in force**.

        The thresholds are a deployment's decision and a reader on the node's page
        has no other way to see them: the numbers live in a config file and a
        package default, neither of which is on a dashboard.
        """
        fields: dict[str, str | None] = {"API": self.api_url}
        for budget in self.budgets:
            fields[f"`{plain(budget.name)}` graded"] = (
                f"WARN below {budget.warn_below}, "
                f"ERROR below {budget.error_below} {budget.unit}")
        return config_markdown(fields)

    def _new_deadline(self) -> Deadline:
        """This run's budget, `timeout:`, read as the `github` type reads it
        (ADR-0002 §1). Overridden in tests, so a deadline can be
        spent without a test spending one."""
        return Deadline(self.timeout_seconds)

    def _make_client(self, token: str,
                     deadline: Deadline | None = None) -> GitHubClient:
        """Build the API client. Overridden in tests to avoid live calls.

        `timeout:` reaches it twice, as the `github` type's two budgets do
        (ADR-0002 §1): as the limit on each request, which this type
        has no `request_timeout:` to set apart, and as the run's ``deadline``. The
        deadline is what lets the client refuse a wait: without one it slept
        whatever GitHub asked — an exhausted window's hour, or, for a reset no clock
        holds, a `time.sleep` that raises out of the run.
        """
        return GitHubClient(token, api_url=self.api_url,
                            timeout=self.timeout_seconds, deadline=deadline)

    #: Every field a resource's reading has, in one shape whatever its source
    #: (little-sister ADR-0085 decision 3): what the check could not read stands as
    #: ``None``, and a reset it has no time for as ``{"at": None}``. `source` says
    #: where the numbers came from — the **ledger**, the **endpoint**'s row alone,
    #: or none: a resource GitHub did not report (`absent`), reported in a shape
    #: this check cannot read (`shape`), or reported without a structural field
    #: (`unreadable`) — and, for the run itself, `unanswered` when the asking
    #: failed and `malformed` when the answer had no `resources` object.
    _READING_FIELDS: tuple[str, ...] = (
        "account", "resource", "source", "limit", "remaining", "used", "reset",
        "own", "foreign", "before", "charged", "others", "error")

    def _reading(self, **fields: Any) -> Measurement:
        """One reading, in the one shape; about one resource's budget where it
        names a resource, and then — where the account is known — with that
        budget as its subject.

        It names **no identity** (ADR-0014 §6): every poll really is a new reading
        of that budget, so each one is a new point in the budget's series rather
        than a record an earlier poll wrote (little-sister ADR-0087 decision 3).
        """
        record: dict[str, Any] = dict.fromkeys(self._READING_FIELDS)
        record["account"] = self._account
        record.update(fields)
        # The window's reset is an instant, kept where little-sister reads one
        # (ADR-0014 §6): handed in as GitHub counts it, in epoch seconds, and kept
        # as `reset.at`.
        record["reset"] = _at(record["reset"])
        resource = record["resource"]
        subject = (self._budget_subject(str(resource))
                   if resource is not None else "")
        # No identity: what is left of a budget is read anew on every poll.
        return Measurement(record=record, subject=subject)

    def _budget_subject(self, resource: str) -> str:
        """The object one resource's reading is about: **that account's budget
        for that resource** (ADR-0014 §6).

        ``<login>;resource=<resource>``, and ``;host=<host>`` after it off
        GitHub's own API — the shape of the estate's subject, for the same reason:
        the object first, then what narrows it. Each resource is its own budget at
        GitHub, with its own limit and its own reset, so `core` and `graphql` of one
        account are two objects and never one named twice. A login holds neither
        `;` nor `=`, and GitHub's resource names are words.

        Empty where the account is not known — an installation token, whose budget
        is the installation's and which `/user` will not name, or a run whose
        question went unanswered: a reading that names no account has no subject
        and so no history, and a stand-in would name something that is not whose
        budget was read.
        """
        if self._account is None:
            return ""
        subject = f"{self._account};resource={resource}"
        if self.api_url.rstrip("/") != GITHUB_API:
            host = urllib.parse.urlsplit(self.api_url).hostname or self.api_url
            subject = f"{subject};host={host}"
        return subject

    def _learn_account(self, client: GitHubClient) -> None:
        """Ask GitHub whose token this is, **once per process**.

        `/rate_limit` never says: it answers with resources, not with an account.
        `GET /user` does, for a personal access token and for a user access token —
        and for those the budget is the user's (GitHub: *your personal rate limit
        of 5,000 requests per hour*). It costs one `core` request, spent here once
        and not every run. An installation token is refused there, being no user,
        and its budget is the **installation's**, which nothing at run time names;
        that answer is final, so it is not asked again. A failure GitHub did not
        answer — a throttle among them, whose wait the run does not take when it
        cannot afford it — leaves the question open for the next run, and so does a
        run whose `timeout:` runs out while it asks.
        """
        if self._account_settled:
            return
        try:
            answer = client.get("/user")
        except (GitHubError, DeadlineExceeded) as error:
            if isinstance(error, GitHubError) and error.fault is Fault.ANSWERED:
                self._account_settled = True
            logger.info("%s: GitHub did not name the account this token belongs "
                        "to (%s) — its budget readings carry no subject",
                        self.path, error)
            return
        login = values.text(answer, "login") if isinstance(answer, dict) else ""
        if login:
            self._account = login
            self._account_settled = True

    def _row(self, budget: Budget,
             row: object) -> tuple[int, int, int, int] | Measurement:
        """The endpoint's row for one resource as its four numbers — `limit`,
        `remaining`, `used`, `reset` — or the reading that says why it is not one.

        A resource this config watches and GitHub did not answer for is a typo,
        or a name this installation does not have; either way the watched line
        must not simply be absent — a missing line reads as a budget that is
        fine. The two shapes are worded apart because they send a reader to
        different places: one to their own config, the other to the payload.
        """
        if not isinstance(row, dict):
            return self._reading(resource=budget.name,
                                 source="absent" if row is None else "shape")
        try:
            # **Required**, both of them, and the asymmetry is the reason: an
            # absent `remaining` would default to 0 and grade red, which is
            # survivable, but an absent `limit` would default to 0 and take the
            # "no limit" path — an exhausted budget reading green because a key
            # went missing. A structural field's absence means the shape changed,
            # and that is a read failure, not a reading. `used` and `reset` are
            # not structural: a row without them is still a budget.
            limit = values.number(row, "limit", where=budget.name, required=True)
            remaining = values.number(row, "remaining", where=budget.name,
                                      required=True)
            used = values.number(row, "used", where=budget.name,
                                 default=limit - remaining)
            reset = values.number(row, "reset", where=budget.name)
        except CheckError as error:
            # Per-resource isolation, as every aspect of the `github` type does it:
            # one unreadable row must not cost the readings of the others. Letting
            # this escape `measure()` would replace every keyed line — and every
            # maintenance pin held against one — with a check-error traceback.
            return self._reading(resource=budget.name, source="unreadable",
                                 error=_kept(str(error)))
        return limit, remaining, used, reset

    def _line(self, budget: Budget, limit: int, remaining: int, reset: int,
              now: float, *, tail: str = "") -> Entry:
        """One resource's line, coded with that resource's own verdict on
        ``remaining`` — whichever source the numbers came from."""
        name = plain(budget.name)
        if limit <= 0:
            # A limit of zero is not a budget of nothing, it is the **absence** of
            # a budget — and grading it would paint a permanent red on an
            # installation that does not rate-limit at all. UNDEFINED says nothing
            # and is skipped when the node's code is derived, which is the honest
            # answer to a resource there is nothing to say about.
            return Entry(slug(budget.name),
                         f"{name}: GitHub reports no limit for this "
                         f"resource — nothing to grade",
                         code=StatusCode.UNDEFINED)
        text = f"{name}: {remaining} of {limit} {budget.unit} left"
        if reset:
            text = f"{text}, {_resets_in(reset, now)}"
        if tail:
            text = f"{text}{tail}"
        return Entry(slug(budget.name), text, code=budget.code(remaining))

    def _from_ledger(self, budget: Budget,
                     counters: list[Counter]) -> Measurement | None:
        """One resource's reading **taken from the ledger**, or ``None`` when the
        ledger holds no counter with numbers for it (ADR-0007, decision 2).

        The tightest counter grades the resource; the others are kept beside it
        as what they hold, because a token whose endpoint reports one window of
        two has been hiding the one spent twice as fast. The three numbers about
        who spent the window are the tightest counter's own: what this process
        spent, what something else spends an hour as the node would say it, and
        what the window carried before this process first read it. The ledger is
        read **here**, in the measuring half: it is state this process keeps about
        the world, and the grading may read nothing but the record.
        """
        gradable = [counter for counter in counters
                    if counter.limit is not None and counter.remaining is not None]
        if not gradable:
            return None
        tightest, *others = gradable          # tightest first: the ledger's order
        return self._reading(
            resource=budget.name, source="ledger", limit=tightest.limit,
            remaining=tightest.remaining, used=tightest.used,
            reset=tightest.reset, own=tightest.own_spend,
            foreign=tightest.foreign_rate_said(), before=tightest.before_us or 0,
            charged=any(counter.charged for counter in gradable),
            others=[{"remaining": counter.remaining,
                     "reset": _at(counter.reset)}
                    for counter in others])

    def _ledger_line(self, budget: Budget, record: Mapping[str, Any],
                     now: float) -> Entry:
        """One resource's line from a reading the ledger gave: the tightest
        window grades it and opens the line, and the clauses after it say whose
        spend it is and what the other windows hold."""
        tail = ""
        # **What this process spent**, said first because it is the one number on
        # this line the reader can act on directly, and because the three clauses
        # together are what makes a window add up: its own, somebody else's rate,
        # and what it carried before this process saw it. A count rather than a
        # rate, and with no tilde: it is what the ledger recorded, not an estimate
        # of anybody's pace. Said only when it is not zero — a window this process
        # has not spent is the endpoint's clause below, or nothing worth a word.
        ours = record["own"]
        if ours:
            tail += f"; {ours} of it this process's own"
        foreign = record["foreign"]
        before = record["before"] or 0
        # Two clauses about the same somebody: the rate, measured between this
        # process's own readings, and what the window carried before its first
        # reading, said only when this process saw the window open and then
        # without a floor — with the rollover seen it is a count, not an
        # estimate, and the hourly job it exists for spends forty-five.
        if foreign is not None and before:
            tail += (f"; ~{foreign}/h of it is spent by something else using "
                     f"this token, and {before} of it before this process first "
                     f"read this window")
        elif foreign is not None:
            tail += (f"; ~{foreign}/h of it is spent by something else using "
                     f"this token")
        elif before:
            tail += (f"; {before} of it were spent by something else before "
                     f"this process first read this window")
        others = record["others"] or []
        if not record["charged"]:
            tail += f" {ENDPOINT_SAYS}"
        elif others:
            held = ", and ".join(
                f"{other['remaining']} left, "
                f"{_resets_in(_epoch(other['reset']), now)}"
                for other in others)
            tail += (f" — the tightest of {len(others) + 1} windows GitHub keeps "
                     f"for this token; "
                     + ("the other has " if len(others) == 1 else
                        "the others have ") + held)
        return self._line(budget, record["limit"], record["remaining"],
                          _epoch(record["reset"]), now, tail=tail)

    def measure(self) -> list[Measurement]:
        """Ask GitHub for the budget, fold its answer into the ledger, and read
        each watched resource back out of it — one reading per resource, in the
        order the configuration declared them.

        A failed ask is **one** reading, since nothing was read about any
        resource: the attempt, and why it failed — GitHub's refusal or silence, or
        the run's `timeout:` spent before the budget could be asked.
        """
        # The run's budget starts here, before its first request, as the `github`
        # type's does (ADR-0002 §1).
        client = self._make_client(self.token, self._new_deadline())
        # Before the budget, so the one request it costs is in the numbers read.
        self._learn_account(client)
        try:
            payload = client.get(RATE_LIMIT_PATH)
        except (GitHubError, DeadlineExceeded) as error:
            return [self._reading(source="unanswered", error=_kept(str(error)))]
        resources = payload.get("resources") if isinstance(payload, dict) else None
        if not isinstance(resources, dict):
            return [self._reading(source="malformed")]
        now = time.time()
        book = shared_ledger()
        readings: list[Measurement] = []
        for budget in self.budgets:
            row = self._row(budget, resources.get(budget.name))
            if not isinstance(row, Measurement):
                # The body's row, merged as **one more reading** of whichever
                # counter its `reset` names — free, so it counts as no attempt.
                # A pristine row of a window nothing spent opens no counter; the
                # ledger says why.
                limit, remaining, used, reset = row
                _first_sight(book.record(
                    self.token, RATE_LIMIT_PATH, resource=budget.name,
                    limit=limit, remaining=remaining, used=used,
                    reset=reset or None, now=now), now, self.path)
            # The reading is the ledger's wherever the ledger has one: the
            # counters the reads of this process were charged to, or the
            # endpoint's own counter where that is all there is. Only a resource
            # the ledger holds nothing for — pristine on the endpoint, or
            # unreadable there — is taken from the row itself, and then says so.
            reading = self._from_ledger(
                budget, book.counters(self.token, budget.name, now))
            if reading is None:
                reading = row if isinstance(row, Measurement) else self._reading(
                    resource=budget.name, source="endpoint", limit=row[0],
                    remaining=row[1], used=row[2], reset=row[3])
            readings.append(reading)
        # The reading, and then the **same response's** own budget headers. This
        # check reads a bucket GitHub looked up by identity; the headers say which
        # bucket it charged for that very lookup and what is left of *that* one.
        # They normally restate each other, which is why the second half is in the
        # log and not on the node: it is worth nothing until it disagrees, and then
        # it is worth everything, because a node reporting a full budget while the
        # `github` check beside it spends hundreds of calls an hour is otherwise a
        # contradiction with no third number to settle it.
        logger.info("%s: %s | that response's own headers: %s", self.path,
                    "; ".join(self._said(reading.record)
                              for reading in readings),
                    budget_said(client.last_rate_limit, now))
        return readings

    def _said(self, record: Mapping[str, Any]) -> str:
        """One resource's reading, as the log line says it."""
        name = str(record["resource"])
        if record["limit"] is None or record["remaining"] is None:
            return f"{name}: {record['source']}"
        unit = RESOURCE_UNITS.get(name, DEFAULT_UNIT)
        return f"{name}: {record['remaining']} of {record['limit']} {unit} left"

    def grade(self, measurements: Sequence[Measurement],
              now: datetime) -> CheckResult:
        """Each watched resource's line, from its reading and the instant it is
        given — the one number a line reads the clock for is how long until its
        window resets."""
        at = now.timestamp()
        budgets = {budget.name: budget for budget in self.budgets}
        entries: list[Entry] = []
        for measurement in measurements:
            record = measurement.record
            source = record["source"]
            if source == "unanswered":
                # **What failed is the asking**, and the sentence says so rather
                # than making a claim about a budget nobody read. There is no other
                # finding to protect here — this check has exactly one source — so
                # it is the node's own code and not a line beside a reading.
                return CheckResult(
                    StatusCode.ERROR,
                    [f"could not ask GitHub for the rate limit: "
                     f"{plain(str(record['error']))}"])
            if source == "malformed":
                return CheckResult(
                    StatusCode.ERROR,
                    [f"GitHub answered {RATE_LIMIT_PATH} without a 'resources' "
                     f"object — nothing to read"])
            budget = budgets.get(str(record["resource"]))
            if budget is None:
                continue            # a reading of a resource no longer watched
            # **Each reading is one line, and the line carries it** (little-sister
            # ADR-0086 decision 2; ADR-0014 §6): the
            # measurement's record is the entry's `data` and its subject the
            # entry's `subject` — on every line, the failed ones included, which
            # are about the same budget a good reading would be. Carried here, in
            # the one place every line passes, so no sentence below can be the
            # one that forgot.
            entries.append(replace(self._resource_line(budget, record, at),
                                   subject=measurement.subject,
                                   data=dict(record)))
        # No `code`: every line carries its own, so the node's is the worst of
        # them (little-sister ADR-0042). Declaring both is refused.
        return CheckResult(reason=tuple(entries), entries=True)

    def _resource_line(self, budget: Budget, record: Mapping[str, Any],
                       at: float) -> Entry:
        """One watched resource's sentence and code, from its reading alone."""
        name = plain(budget.name)
        source = record["source"]
        if source == "absent":
            return Entry(slug(budget.name),
                         f"{name}: GitHub did not report this resource",
                         code=StatusCode.WARN)
        if source == "shape":
            return Entry(slug(budget.name),
                         f"{name}: GitHub reported this resource in a shape this "
                         f"check cannot read", code=StatusCode.WARN)
        if source == "unreadable":
            return Entry(slug(budget.name),
                         f"{name}: could not read this resource "
                         f"({plain(str(record['error']))})", code=StatusCode.WARN)
        if source == "ledger":
            return self._ledger_line(budget, record, at)
        return self._line(budget, record["limit"], record["remaining"],
                          _epoch(record["reset"]), at, tail=f" {ENDPOINT_SAYS}")


def _kept(text: str) -> str:
    """An error's text as the reading keeps it — clipped once, in characters and
    then in the bytes the seam weighs (little-sister ADR-0086 decision 7)."""
    return clip(text, chars=300, budget=600)


def _at(reset: int | None) -> dict[str, str | None]:
    """A window's reset as a reading keeps it: under `at`, the name little-sister
    reads as an instant at any depth (ADR-0014 §6), in UTC — or ``None`` where
    GitHub sent none, or a number of seconds no calendar holds. GitHub counts it
    in epoch seconds, a number every surface would show as a count."""
    if not reset:
        return {"at": None}
    try:
        moment = datetime.fromtimestamp(reset, UTC)
    except (OverflowError, OSError, ValueError):
        return {"at": None}
    return {"at": moment.isoformat().replace("+00:00", "Z")}


def _epoch(reset: Mapping[str, Any]) -> int:
    """A reset a reading kept, as the epoch seconds a line counts its minutes
    from — the grading's way back from :func:`_at`, reading nothing but the
    record; ``0`` where it kept no time, which says nothing about a reset."""
    at = reset.get("at")
    return int(datetime.fromisoformat(str(at)).timestamp()) if at else 0
