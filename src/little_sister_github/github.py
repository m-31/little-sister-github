"""The ``github`` check type: a team's repositories, one child per aspect.

Discovers the repositories of a GitHub account — an organization, optionally
narrowed to one team, or a personal account — and reports **one child per
aspect** — aspect-first. Which kind of account `owner:` names is **declared** by
the config (`kind:`) and verified against GitHub on every run. Most aspect children
are leaves listing the repositories they flag; the two severity-carrying security
aspects split once more into source-severity bands. Ported from a Ruby
overview-check dashboard.

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
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar

from little_sister import values
from little_sister.checks import (
    Check,
    CheckError,
    CheckResult,
    Entry,
    coerce_code,
    config_markdown,
    parse_duration,
    parse_secret_refs,
    parse_subnodes,
    plain,
    register,
    resolve_text,
)
from little_sister.fetch import Response, fault_for, fetch, retry_after
from little_sister.reasons import slug
from little_sister.status import StatusCode
from little_sister.transport import (
    Deadline,
    DeadlineExceeded,
    Fault,
    RemoteError,
    ask,
)

#: This package's own logger. little-sister does not promise its ``logger`` to
#: check authors and does not need to: the library configures the root handlers,
#: so an ordinary module logger's records land in the same place, under a name
#: that says which package emitted them.
logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

#: How long **one request** may take, when a config does not say (`request_timeout:`).
#: Not the same budget as `timeout:`, which is the whole run's — see
#: :class:`~little_sister.transport.Deadline`. Well under the library's 30-second
#: default `timeout:`, because a run makes one request per repository per aspect and
#: then pages on top of that: a per-request value at the run's own size cannot bound
#: anything.
DEFAULT_REQUEST_TIMEOUT = 15.0

#: How many extra attempts a **transient** failure gets. One: it separates "GitHub
#: hiccupped" from "GitHub is having a bad ten minutes", which is the whole question
#: the reading has to answer, and a second retry would only buy a sharper line at
#: the cost of the run's remaining time.
TRANSIENT_RETRIES = 1

#: The wait before that one retry. Long enough that an overloaded endpoint is not
#: asked again instantly, short enough that forty repositories retrying once cannot
#: dominate a run — and it is spent only when the deadline can still afford it.
RETRY_BACKOFF_SECONDS = 1.0

#: What fraction of a run's own `timeout:` may be spent asleep waiting out a
#: throttle, when a config does not say (`max_pause:`). Half: a run that spends
#: more than half its budget waiting is not doing the job it was scheduled for,
#: and the number has to be derived rather than fixed because `timeout:` is what
#: a deployment sizes to its own account — a constant would be a no-op at the
#: library's 30-second default and far too loose at ten minutes.
DEFAULT_MAX_PAUSE_FRACTION = 0.5


class _PauseBudgetSpent(Exception):
    """A throttle wait this run cannot afford — raised instead of taking it.

    It ends the run the way an exhausted deadline does, and for the same reason:
    the aspects that finished are kept and reported and the rest are absent, which
    beats both sleeping through the check's own frequency and pressing a service
    that has just asked us to wait. It carries the wait it refused so the node can
    say what would have happened.

    Not a `RemoteError` and not `DeadlineExceeded`, deliberately: an aspect's
    per-repository `except RemoteError` would otherwise swallow it into a finding
    line about a repository that has done nothing wrong.
    """

    def __init__(self, wait: float) -> None:
        super().__init__(f"a {wait:.0f}s wait is more than this run may pause")
        self.wait = wait


# **Two scales, and GitHub keeps them apart — so this check does too** (ADR-0005).
# A code-scanning alert carries a *security* severity when its rule has one, and an
# *analysis* severity always; GitHub's own filter files the first under **Security**
# and the second under **Other**. They used to be one eight-band row here, which is
# what let three bands render that no alert could ever land in.
#
# Worst first in each. A deployment may grade every band independently; the band
# shape is what makes overriding one a one-line change (little-sister ADR-0042).
SECURITY_SEVERITY_ORDER = ("critical", "high", "medium", "low")
ANALYSIS_SEVERITY_ORDER = ("error", "warning", "note")

DEFAULT_ADVISORY_SEVERITY_MAP = {
    "critical": StatusCode.ERROR,
    "high": StatusCode.ERROR,
    "medium": StatusCode.WARN,
    "low": StatusCode.WARN,
}
#: Every security-severity band is red: a code-scanning alert GitHub gave a security
#: severity to is a vulnerability in this account's own code, and the mildest one is
#: still that.
DEFAULT_CODE_SCANNING_SECURITY_MAP = dict.fromkeys(
    SECURITY_SEVERITY_ORDER, StatusCode.ERROR)
#: And none of the quality bands is. **Red on this dashboard means act now**, and a
#: non-security finding is not that however CodeQL grades its own rule — which is the
#: distinction one shared map could not make, and why the shipped default used to
#: answer `ERROR` for a `note`. A deployment that wants its lint errors red says so in
#: one line.
DEFAULT_CODE_SCANNING_QUALITY_MAP = {
    "error": StatusCode.WARN,
    "warning": StatusCode.WARN,
    "note": StatusCode.OK,
}
#: A severity band's title: a colored circle, **by name and never by rank**.
#:
#: The band's name sits directly beside the title on every chip, so the circle costs a
#: chip's width less than the word *Critical* and says the same thing faster — and where
#: a surface draws the title *instead of* the name, little-sister now draws both
#: (little-sister ADR-0061), so the word is never lost.
#:
#: **By name**, and this package is the reason. A rank here is a *deployment's* tuple:
#: `security_advisories` is built with `order=self.dependabot_severities`, so an
#: operator who watches `high` and `medium` gives `high` rank 1 in that aspect while
#: it is rank 2 under code scanning — and a rank-derived circle would make one `high`
#: 🔴 and the other 🟠 on one dashboard. Nobody reads a red circle as *where this sits
#: in this row*. The
#: rank orders the row (little-sister ADR-0055); the colour says how bad it is.
#:
#: **Two scales share the ramp, and the repeats are the point** (ADR-0005). `error`
#: and `high` are comparable rungs of two scales GitHub itself keeps apart, and since
#: the split they never appear in one row: `code_scanning_security` draws the security
#: four, `code_scanning_quality` the analysis three, and `security_advisories`
#: whichever of the security four a deployment watches. A row with two 🟠 in it would
#: be a bug; two aspects that each have one is the model.
BAND_GLYPHS = {
    # security severity — Dependabot's, and code scanning's
    # `security_severity_level`
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    # analysis severity — a code-scanning rule's own `severity`
    "error": "🟠",
    "warning": "🟡",
    "note": "🔵",
}

#: What a severity this package does not name gets. The band list is **open**: GitHub
#: may add a severity, an operator may mistype one into a `severity_map`, and an alert
#: carrying neither severity lands in `code_scanning_quality`'s `none`. A band with no
#: colour must not borrow one — and this stays rare enough to mean something.
UNKNOWN_BAND_GLYPH = "❓"


def band_glyph(severity: str) -> str:
    """The circle this severity wears, or `❓` where this package does not name it."""
    return BAND_GLYPHS.get(severity, UNKNOWN_BAND_GLYPH)

#: Aspect names this check used to emit, and what to write instead. Kept so a
#: `subnodes:` block naming one is told what happened rather than merely that the
#: name is unknown — the text under it is a deployment's own policy paragraph, and
#: *this key is wrong* is not enough to reconstruct where it should go.
RETIRED_ASPECTS = {
    "code_scanning_alerts": (
        "it split into 'code_scanning_security' (the alerts GitHub gave a security "
        "severity) and 'code_scanning_quality' (everything else, by the rule's own "
        "analysis severity). Put your text under whichever half it is about, or "
        "under both"),
}


def _severity_map(value: object, field: str) -> dict[str, StatusCode]:
    """One configured source-severity → dashboard-code mapping."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CheckError(f"github '{field}.severity_map' must be a mapping")
    return {str(severity).lower(): coerce_code(code)
            for severity, code in value.items()}


def _block(config: dict[str, Any], name: str) -> dict[str, Any]:
    """One aspect's configuration block, defaulted to empty.

    Every aspect's knobs live under its own key, so this is where a block that is
    not a mapping is caught **once** — four of the seven used to be read with a
    bare ``.get`` and answered a scalar with an ``AttributeError`` naming neither
    the check nor the key.
    """
    value = config.get(name) or {}
    if not isinstance(value, dict):
        raise CheckError(f"github '{name}' must be a mapping")
    return value


def _flag(value: object, field: str) -> bool:
    """A configuration boolean that must actually be one.

    ``bool("false")`` is ``True``, so a quoted YAML boolean — ``enabled: "false"``
    — would switch an aspect **on** while its config says off, and nothing
    downstream could notice: the aspect would simply report. A switch is worth
    less than nothing if it can silently mean its opposite.
    """
    if not isinstance(value, bool):
        raise CheckError(f"github '{field}' must be true or false")
    return value


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


def _positive_seconds(value: object, key: str, default: int) -> float:
    """A configured duration, in the **same spelling** `timeout:` and `frequency:`
    take — `15s`, `2m`, or a bare number of seconds.

    Read through the library's own `parse_duration` rather than a local `float()`,
    because a surface where one duration key accepts `15s` and the next refuses it
    is a trap the operator falls into exactly once per key.

    It must be positive: zero makes ``urlopen`` non-blocking and every request fail
    instantly, and a negative one raises from inside the socket layer with a message
    naming neither this check nor this key.
    """
    if isinstance(value, bool):
        raise CheckError(f"github '{key}' must be a positive duration")
    try:
        parsed = parse_duration(value, default)
    except CheckError as error:
        raise CheckError(f"github '{key}' must be a positive duration: "
                         f"{error}") from error
    if parsed <= 0:
        raise CheckError(f"github '{key}' must be a positive duration")
    return float(parsed)


#: The sentence every aspect's `about` ends with, referenced as `{pin_note}` so it
#: is written once (little-sister ADR-0025). GitHub numbers each finding per
#: repository, so every line here is separately addressable — which is the part an
#: operator needs to know before they open a ticket for one of twenty.
PIN_NOTE = ("Each line is one finding and can be put into maintenance on its own — "
            "pin the line you are working on and the rest keeps reporting.")


#: Built-in display text for the aspect leaves this check emits (little-sister
#: ADR-0025) — **type-inherent**, so it is written once here rather than copied into
#: every deployment config, which matters as soon as the type runs more than once
#: (one check per team). `{owner}` / `{team}` and the three
#: `{…_link}` sentences expand from the check's own config and from the account
#: kind discovery resolved (`_subnode_tokens`). A check config's `subnodes:` block
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
dependencies of the repositories in scope.
{advisories_link}

{advisories_grading}

{pin_note}
""",
    },
    "code_scanning_security": {
        "title": "Code-scanning security alerts",
        "about": """\
GitHub Code Scanning found potential **vulnerabilities** in the code of the
repositories in scope — the alerts GitHub itself assigned a security severity,
which is the *Security* half of its own alert filter.
{code_scanning_link}

{code_scanning_security_grading}

{pin_note}
""",
    },
    "code_scanning_quality": {
        "title": "Code-scanning quality alerts",
        "about": """\
Everything else GitHub Code Scanning found — correctness and quality findings
with no security severity, banded by the analysis severity the rule itself
carries. GitHub files these under *Other*, and they are graded more gently than
the security half on purpose: a lint finding is worth knowing, not worth waking
somebody.
{code_scanning_link}

{code_scanning_quality_grading}

{pin_note}
""",
    },
    "secret_scanning_alerts": {
        "title": "Secret-scanning alerts",
        "about": """\
GitHub Secret Scanning found secrets committed to the repositories in scope.
{secret_scanning_link}

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
Open issues across the repositories in scope, one line per issue. **Pull requests are
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


def _path(segment: str) -> str:
    """One configured value as a **single** URL path segment.

    GitHub logins cannot contain a slash, so this changes nothing for a well-formed
    config — it is here so that a typo in `org:` becomes a 404 for the name that was
    typed rather than a request against a path the config did not describe.
    """
    return urllib.parse.quote(segment, safe="")


def _entry_slug(repo: Repo, kind: str, number: int = 0, url: str = "") -> str:
    """The slug for one per-repo finding — ``<repo-id>-<kind>-<number>``.

    GitHub numbers pull requests, issues and every alert family **per repository**,
    and the number is minted with the finding and retired with it. That is the
    identity little-sister ADR-0036 asks for.

    The repository half is its **numeric id, not its name**. A slug is what a
    maintenance pin holds, and a pin has to survive everything except the finding
    it is about — but a repository name is a display value its owner may change at
    any time, and a rename would silently re-key every line about that repository,
    orphaning every pin held on one. The id is what GitHub minted and the one field
    a rename does not touch (little-sister ADR-0050: a key comes from what the
    provider minted, never from what is rendered).

    The cost, stated because it is real: `6304-pr-42` cannot be read the way
    `platform-a-pr-42` could, and a `?reason=` value no longer names its repository.
    The **rendered line** still opens with the name, which is where a person reads
    it; a slug is for machines and pins.

    A payload with no number falls back to the API's own URL, which is unique per
    finding, and finally to ``<repo-id>-<kind>`` for the aspects that report at most
    one line per repository (a missing SBOM, secret scanning switched off). What it
    never falls back to is the line's position: an entry above it closing would slide
    the pin onto somebody else's finding.
    """
    if number:
        return slug(repo.id, kind, number)
    return slug(repo.id, kind, url) if url else slug(repo.id, kind)


@dataclass
class _Coverage:
    """What one aspect managed to look at, and what it could not.

    This is where ADR-0002's rule lives: **a read failure that is about GitHub is
    not a finding about the repository.** A `TRANSIENT` failure — a 5xx, a dropped
    connection, a throttle — becomes an `UNDEFINED` line, which is displayed and
    pinnable and which the node's derived code skips, so a repository nobody could
    ask about is not painted amber for it. Anything else keeps grading `WARN`:
    `ANSWERED`, because a token that may not read this repository is a true statement
    about this repository and somebody must act on it, and `MALFORMED`, because an
    unreadable answer is a defect in this check or in the API and not something to
    wait out.

    The `read` count is what keeps :meth:`lines` honest: it is the denominator of
    the one line here that **grades**, the aspect's own coverage gap.
    """

    #: Repositories whose payload actually arrived (a 404 counts: "not enabled" is
    #: an answer). Only ever compared against `missed`, never displayed alone.
    read: int = 0
    #: One line per repository that could not be read, coded by kind.
    notes: list[Entry] = field(default_factory=list)
    #: How many of those were the *we could not ask* kind. The check's own node
    #: reports this; the graded kind is already amber where it happened.
    unreachable: int = 0
    #: Whether the run has already added `unreachable` to the check's own total.
    #: **One read, one count**, however many aspects it feeds: the two code-scanning
    #: aspects share a payload and would otherwise make one outage say `2 repository
    #: reads could not be completed` where one was attempted — which is the exact
    #: double-count ADR-0002 kept off the aspects in the first place.
    counted: bool = False

    def read_one(self) -> None:
        self.read += 1

    def failed(self, repo: Repo, error: GitHubError,
               kind: str = "unreadable", subject: str = "") -> None:
        """One repository this aspect could not read.

        ``subject`` names *what* could not be read where an aspect reads more than
        one thing per repository — `actions` reads the workflow list and then the
        runs, and a line that said only "could not read" for either left the two
        distinguishable by slug alone, which is not on the rendered line.
        """
        name = plain(repo.name)
        what = f" {subject}" if subject else ""
        if error.fault is Fault.TRANSIENT:
            self.unreachable += 1
            self.notes.append(Entry(
                _entry_slug(repo, kind),
                f"{name}: could not ask GitHub{what} ({plain(str(error))})",
                code=StatusCode.UNDEFINED))
            return
        self.notes.append(Entry(
            _entry_slug(repo, kind),
            f"{name}: could not read{what} ({plain(str(error))})",
            code=StatusCode.WARN))

    @property
    def missed(self) -> int:
        return len(self.notes)

    def lines(self) -> tuple[Entry, ...]:
        """The notes, and — whenever anything could not be asked about — one
        `WARN` line stating the gap.

        This is the line that grades. The per-repository notes deliberately do not:
        a transient failure is `UNDEFINED`, which the derivation skips, so without
        this an aspect that reached nothing at all would derive `UNDEFINED`, and a
        banded aspect would render **green** — its watched bands being `OK` when
        empty. The gap is a fact about this aspect's *coverage*, which the aspect
        may honestly grade itself amber for, and stating it here needs no band to
        tell *empty* from *unread*.

        The count is the **could-not-ask** kind only, and the sentence names the
        cause so it cannot be read as a total. A repository GitHub *refused* is
        already amber on its own line; counting it here would grade one condition
        twice.
        """
        if not self.unreachable:
            return tuple(self.notes)
        total = self.read + self.missed
        return (*self.notes, Entry(
            "read",
            f"GitHub did not answer for {self.unreachable} of "
            f"{total} repositories",
            code=StatusCode.WARN))


@dataclass(frozen=True)
class Repo:
    """One repository from discovery — the fields every aspect reads.

    Built once at the seam (:meth:`from_api`) so the aspects work on typed values
    rather than an ``Any`` bag whose wrong types only surface at runtime
    (little-sister ADR-0026). ``name`` / ``full_name`` are **raw**: Markdown escaping
    is a render-time step (``plain``), and an escaped value can no longer be
    interpolated into a URL.
    """

    #: GitHub's own numeric id for the repository — **the only field here a
    #: rename cannot change**, which is why the entry slugs are built from it
    #: (:func:`_entry_slug`) while everything a human reads uses `name`.
    id: int
    name: str
    full_name: str
    archived: bool = False
    fork: bool = False
    #: Read for one reason: GitHub Advanced Security — code scanning and secret
    #: scanning — is free on a public repository and paid on a private one, so the
    #: two aspects that read it are narrowed by visibility rather than by account
    #: kind (`advanced_security_on_private`).
    private: bool = False
    default_branch: str = ""

    @classmethod
    def from_api(cls, row: object) -> Repo:
        """Read one ``/repos`` row. ``id``, ``name`` and ``full_name`` are
        **structural** — the slugs are keyed on the first and every aspect addresses
        the repo by the other two — so their absence means the payload is not what we
        think and is raised, not defaulted. The rest are display or filter fields and
        default quietly."""
        try:
            return cls(
                id=values.number(row, "id", required=True, where="repository"),
                name=values.text(row, "name", required=True, where="repository"),
                full_name=values.text(row, "full_name", required=True,
                                      where="repository"),
                archived=values.flag(row, "archived"),
                fork=values.flag(row, "fork"),
                private=values.flag(row, "private"),
                default_branch=values.text(row, "default_branch"))
        except CheckError as error:
            # It arrived and it cannot be used: `id`, `name` and `full_name` are
            # structural, so a row without them means the payload is not what we
            # think it is. Asking again returns the same shape.
            raise GitHubError(f"unexpected repository payload: {error}",
                              fault=Fault.MALFORMED) from error


#: The two account kinds `kind:` may name, spelled as GitHub spells them in its own
#: UI when you pick an owner for a new repository.
ACCOUNT_KINDS = ("organization", "user")

#: What GitHub's own `type` field calls each of them, which is what the run-time
#: guard compares a config's claim against.
_REPORTED_KIND = {"Organization": "organization", "User": "user"}


class GitHubError(RemoteError):
    """A GitHub API request failed (``status`` is the HTTP code, when known).

    A :class:`~little_sister.transport.RemoteError` subclass, which is what that
    class is for: the vocabulary is the library's and the messages, the prefixes and
    every ``except GitHubError`` in this package stay ours. ``fault`` is inherited
    and **required** — a default would be the one decision this package must not
    take by accident (little-sister ADR-0058).

    :class:`~little_sister.transport.Fault` says which of three things happened, and
    it is set **by status and by header, never by message text** (ADR-0002):
    `TRANSIENT` means *we could not ask* — a 5xx, a transport failure, or GitHub
    throttling us; `ANSWERED` means GitHub answered and the answer was no —
    ``404`` that the thing is absent, ``401``/``403`` that this token may not see it;
    `MALFORMED` means it answered with something this check cannot read. Only
    `TRANSIENT` is retried, and only `TRANSIENT` refuses to grade the repository it
    is about.

    ``retry_after`` carries the seconds GitHub asked for, and only this package can
    fill it in: no status identifies a throttle here (see :func:`_throttle_wait`).
    """


#: The statuses GitHub documents for a rate limit — **both** of them, for both its
#: primary and its secondary limits. Neither identifies a throttle on its own, which
#: is why the headers below decide and why this list is not a classification.
_THROTTLE_STATUSES = (403, 429)

#: What to wait when GitHub says it is throttling and names no end. GitHub's own
#: guidance ("wait at least one minute before retrying"), and it is deliberately
#: longer than any run's remaining budget usually is: `ask` refuses a wait the
#: deadline cannot afford, so this number mostly *prevents* a retry rather than
#: scheduling one. Pressing a service that has just complained about volume is how
#: an integration gets itself blocked.
THROTTLE_FLOOR_SECONDS = 60.0


def _throttle_wait(response: Response, *,
                   now: Callable[[], float] | None = None) -> float | None:
    """Seconds GitHub asked us to wait, or ``None`` if it was not asking.

    **This is the dialect, and it is why it lives here.** The library reads the
    standard ``Retry-After`` and stops (little-sister ADR-0058): a throttle has no
    status of its own — GitHub documents ``403`` *and* ``429`` for both its primary
    and its secondary limits — and a bare ``403`` equally means *this token may not
    see it*. Only code that knows GitHub's headers can tell those apart, so only
    this package can.

    GitHub's own precedence, in order:

    1. **``retry-after``** — the standard header, which the secondary limit sends.
    2. **``x-ratelimit-remaining: 0``** — the primary limit is exhausted, and
       ``x-ratelimit-reset`` says when the window rolls over. That is an **epoch
       timestamp**, so it is read against the wall clock and not the run's monotonic
       one; a reset already past yields ``0.0``, which is the honest answer.
    3. Otherwise a **``429``** is still a throttle — it is the only thing GitHub
       sends that status for — and gets :data:`THROTTLE_FLOOR_SECONDS`. A **``403``**
       is not: with no throttle header on it, it is the permission answer, and
       reading it as *not now* would retry every unreadable repository in the scope.

    Never from the body. GitHub's throttle bodies do say so in prose, and matching on
    it is exactly what ADR-0002 forbids — the same rule that stopped a 500's message
    from deciding whether a repository grades.
    """
    if response.status not in _THROTTLE_STATUSES:
        return None
    asked = retry_after(response.headers)
    if asked is not None:
        return asked
    remaining = response.headers.get("x-ratelimit-remaining")
    if isinstance(remaining, str) and remaining.strip() == "0":
        return _seconds_until_reset(response, now=now)
    return THROTTLE_FLOOR_SECONDS if response.status == 429 else None


def _seconds_until_reset(response: Response, *,
                         now: Callable[[], float] | None = None) -> float:
    """How long the exhausted primary window has left, from ``x-ratelimit-reset``.

    Falls back to the floor when the header is absent or unreadable rather than to
    ``0.0``: we already know from ``x-ratelimit-remaining`` that the budget is gone,
    and *retry immediately* is the one answer that cannot be right.

    ``now`` is resolved at **call** time and not captured as a default, so a test can
    drive the wall clock — and so can a caller that would rather measure against the
    response's own ``Date`` than trust two machines' clocks to agree.
    """
    reset = response.headers.get("x-ratelimit-reset")
    if not isinstance(reset, str):
        return THROTTLE_FLOOR_SECONDS
    try:
        when = float(reset.strip())
    except ValueError:
        return THROTTLE_FLOOR_SECONDS
    return max(0.0, when - (now or time.time)())


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
    """A minimal GitHub REST client with Link pagination.

    Two budgets, and they are not the same one (ADR-0002). ``timeout`` bounds **one
    request**; ``deadline``, when given, bounds **the whole run** and is checked
    before every request — including every page of a paginated read, which is where
    a single call can quietly become twenty. ``max_pause`` is a third and narrower
    one: how much of the run may be spent *asleep*, which the deadline alone cannot
    say, because a run that sleeps through its whole budget never overruns it.

    **What is ours here is GitHub, and nothing else.** The request, the budgets and
    the retry are the library's — :func:`~little_sister.fetch.fetch`,
    :class:`~little_sister.transport.Deadline` and
    :func:`~little_sister.transport.ask`. What stays is what only a GitHub client can
    know: the auth and API-version headers, the ``Link`` walk, and reading a throttle
    out of GitHub's own headers (:func:`_throttle_wait`) — none of which any other
    package could have written for us, and all of which used to sit around a
    hand-rolled ``urlopen`` call and a retry loop of our own.
    """

    def __init__(self, token: str, *, api_url: str = GITHUB_API,
                 timeout: float = DEFAULT_REQUEST_TIMEOUT,
                 deadline: Deadline | None = None,
                 retries: int = TRANSIENT_RETRIES,
                 backoff: float = RETRY_BACKOFF_SECONDS,
                 max_pause: float | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._token = token
        self._api = api_url.rstrip("/")
        self._timeout = timeout
        self._deadline = deadline
        self._retries = retries
        self._backoff = backoff
        self._max_pause = max_pause
        self._sleep = sleep
        #: Seconds this client actually spent waiting on a throttle. Measured
        #: where the waiting happens — `ask` decides to wait and calls the sleep
        #: injected here, so wrapping it is the only place that can tell a wait
        #: that happened from one the deadline refused. Read by the check for the
        #: node's sentence; a paused run is otherwise indistinguishable from a
        #: slow one.
        self.paused_seconds = 0.0
        #: What this client has spent, for the run's own trace. **Attempts**, not
        #: logical reads: a retried request counts twice, because the question
        #: these answer is where the run's seconds went and a retry spends them
        #: like anything else. A page of a paginated read is one of these too.
        self.reads_made = 0
        self.read_seconds = 0.0
        #: The single slowest attempt, as ``(seconds, path)`` — the one fact that
        #: says whether a run ran out of budget because *everything* was slow or
        #: because one endpoint sat at the request timeout. The path is the API
        #: path rather than the URL: it is what a reader compares against
        #: `ASPECT_ENDPOINT`, and it is stable across `api_url`.
        self.slowest_read = 0.0
        self.slowest_path = ""

    def _now(self) -> float:
        """The clock this run measures with.

        The **deadline's**, when there is one, and for the reason that class
        injects one at all: a run reads one clock, so a test can drive the trace
        and the budget together and neither can drift from the other. Without a
        deadline there is nothing to agree with, and this is the monotonic clock
        `Deadline` itself defaults to.
        """
        return (self._deadline.clock() if self._deadline is not None
                else time.monotonic())

    def _counted(self, url: str, seconds: float) -> None:
        """Add one attempt to what this client has spent."""
        self.reads_made += 1
        self.read_seconds += seconds
        if seconds > self.slowest_read:
            self.slowest_read = seconds
            self.slowest_path = (url[len(self._api):] if url.startswith(self._api)
                                 else url)

    def _headers(self) -> dict[str, str]:
        """What every request to this API carries. The ``User-Agent`` is
        `fetch`'s — ``little-sister/<version>`` — which is a name a GitHub support
        thread can do something with, unlike the ``Python-urllib`` this used to
        send."""
        return {"Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _attempt(self, url: str) -> tuple[Any, str]:
        """One request, as the ``(payload, Link header)`` pair the readers want.

        The socket timeout is the request's own, **clamped by `fetch` to what is
        left of the run** — without that clamp a 15-second request could start with
        two seconds of budget left and overrun `timeout:` by thirteen, which would
        make the deadline a suggestion rather than a bound.

        The attempt is **timed and counted in a `finally`**, so the one that failed
        and the one the deadline cut off are on the run's trace beside the ones that
        answered. Counting only what succeeded would leave a run that spent its
        whole budget on timeouts looking like a run that made no requests.
        """
        started = self._now()
        try:
            response = fetch(url, timeout=self._timeout, follow_redirects=True,
                             headers=self._headers(), deadline=self._deadline)
        except RemoteError as error:
            # `fetch` raises only for a request that never reached a status, and
            # attaches urllib's own exception as `__cause__` so the sentence a
            # reader sees is ours rather than a list of proxy paths and certificate
            # directories. `DeadlineExceeded` is **not** a `RemoteError` and is not
            # caught here on purpose: it is about the run, not about this request.
            raise GitHubError(f"request failed for {url}: {error.__cause__ or error}",
                              status=error.status, fault=error.fault) from error
        finally:
            self._counted(url, self._now() - started)
        if not 200 <= response.status < 300:
            raise self._refusal(response, url)
        try:
            data = json.loads(response.body) if response.body else None
        except ValueError as error:
            # It arrived and it cannot be used, which is neither *we could not ask*
            # nor *GitHub said no*. Before the shared vocabulary this was reported as
            # transient and retried, which spent a second request to be handed the
            # same bytes.
            raise GitHubError(f"GitHub answered {url} with something that is not "
                              f"JSON: {error}", status=response.status,
                              fault=Fault.MALFORMED) from error
        return data, response.headers.get("Link", "")

    def _refusal(self, response: Response, url: str) -> GitHubError:
        """A status GitHub refused with, read as one of the three faults.

        The **throttle** is the one this package has to decide for itself, and
        getting it wrong is not cosmetic: a rate-limited ``403``/``429`` classified
        as *answered* is reported as though GitHub had said **no** about the
        repository, and is never retried.
        """
        detail = response.text()[:200]
        wait = _throttle_wait(response)
        if wait is not None:
            return GitHubError(
                f"HTTP {response.status} for {url} — GitHub asked us to wait "
                f"{wait:.0f}s: {detail}",
                status=response.status, fault=Fault.TRANSIENT, retry_after=wait)
        # `fault_for` is the library's reading: a 5xx is GitHub failing to answer,
        # anything else *is* an answer and asking again would only get it faster.
        return GitHubError(f"HTTP {response.status} for {url}: {detail}",
                           status=response.status,
                           fault=fault_for(response.status))

    def _request(self, path_or_url: str,
                 params: dict[str, Any] | None = None) -> tuple[Any, str]:
        """One request, retried by the library's policy and nothing of our own.

        `ask` retries only a `TRANSIENT` fault, only while attempts remain, and only
        while the deadline can still afford the wait — and it spends GitHub's own
        ``retry_after`` in place of the backoff when GitHub named one. A wait longer
        than the run has left is refused and the error re-raised: **pausing past the
        check's budget is not a request layer's decision** (little-sister ADR-0058).
        """
        url = (path_or_url if path_or_url.startswith("http")
               else f"{self._api}{path_or_url}")
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return ask(lambda: self._attempt(url), deadline=self._deadline,
                   retries=self._retries, backoff=self._backoff,
                   sleep=self._slept)

    def _slept(self, wait: float) -> None:
        """`ask`'s sleep, counted and said out loud.

        `ask` logs its wait at *info*, which is off in most deployments — and a
        minute of silence with no visible line reads exactly like a hang. So the
        wait is logged **here**, at warning, with what is left of the run beside
        it, and added to :attr:`paused_seconds` so the check can put it on the
        node afterwards.
        """
        if (self._max_pause is not None
                and self.paused_seconds + wait > self._max_pause):
            # Refused whole rather than trimmed to what is left: a wait shorter
            # than the one asked for does not satisfy the service, so taking it
            # would spend the rest of the budget and still be throttled.
            raise _PauseBudgetSpent(wait)
        left = (f"{self._deadline.remaining():.0f}s of the run left"
                if self._deadline is not None else "no run deadline")
        logger.warning("paused %.0fs before retrying (%s)", wait, left)
        self.paused_seconds += wait
        self._sleep(wait)

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
                # `MALFORMED` and not transient: another identical request returns
                # the same shape.
                raise GitHubError(f"expected a list from {path}",
                                  fault=Fault.MALFORMED)
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
    Discovery is scoped to the account ``org`` names — team-scoped when ``team`` is
    set, which only an organization can be — and filtered by ``name_prefix`` /
    ``include_archived`` / ``include_forks``.
    """

    #: Every aspect this check type can run, whether or not this check runs it —
    #: **and the order it is read in**, worst-first by what a finding costs, with the
    #: hygiene aspects after the security ones. One constant answers three questions
    #: on purpose: the roster `enabled:` is read against, the sequence the run asks
    #: in (so a run that loses its budget loses the cheapest aspects), and the rank
    #: each aspect node declares (`aspect_rank`, little-sister ADR-0055). A second
    #: tuple existing only to be a display order is the thing this avoids — the
    #: previous sequence was written for the rate estimate, not for a reader, and the
    #: dashboard sorted it alphabetically anyway.
    ASPECTS = ("secret_scanning_alerts", "security_advisories",
               "code_scanning_security", "actions", "sbom_check",
               "code_scanning_quality", "pull_requests", "issues")

    #: Which configuration block each aspect's knobs live under. Identical to the
    #: aspect name for six of the seven — `secret_scanning_alerts` reads
    #: `secret_scanning:`, because the block was named for the *feature* and the
    #: node for what it reports. Renaming either is a breaking change for somebody
    #: (a config key, or a node path every maintenance pin is held against), so the
    #: mismatch is written down here rather than repaired.
    ASPECT_CONFIG_KEY: ClassVar[dict[str, str]] = {
        "pull_requests": "pull_requests",
        "security_advisories": "security_advisories",
        "code_scanning_security": "code_scanning_security",
        "code_scanning_quality": "code_scanning_quality",
        "secret_scanning_alerts": "secret_scanning",
        "sbom_check": "sbom_check",
        "actions": "actions",
        "issues": "issues",
    }

    #: The per-repository endpoint each aspect reads, for the pre-run rate estimate
    #: alone. **Two aspects share one**: the code-scanning split partitions a single
    #: `/code-scanning/alerts` payload, so counting aspects would claim a request per
    #: repository that the run never makes — and the guard would grow more cautious
    #: on a number that is not true. `actions` reads twice per repository and is
    #: counted once, which is the approximation this estimate already made.
    ASPECT_ENDPOINT: ClassVar[dict[str, str]] = {
        "pull_requests": "/pulls",
        "security_advisories": "/dependabot/alerts",
        "code_scanning_security": "/code-scanning/alerts",
        "code_scanning_quality": "/code-scanning/alerts",
        "secret_scanning_alerts": "/secret-scanning/alerts",
        "sbom_check": "/dependency-graph/sbom",
        "actions": "/actions/runs",
        "issues": "/issues",
    }

    def __init__(self, *, owner: str, kind: str = "organization",
                 advanced_security_on_private: bool = True,
                 team: str = "", name_prefix: str = "",
                 include_archived: bool = False, include_forks: bool = True,
                 api_url: str = GITHUB_API,
                 request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
                 max_pause: float | None = None,
                 rate_limit_safety_factor: int = 4,
                 expect_min_repos: int = 1,
                 pr_ignore_prefixes: tuple[str, ...] = (),
                 dependabot_severities: tuple[str, ...] = ("critical", "high"),
                 advisory_severity_map: dict[str, StatusCode] | None = None,
                 code_scanning_security_map: dict[str, StatusCode] | None = None,
                 code_scanning_quality_map: dict[str, StatusCode] | None = None,
                 secret_scanning_require_enabled: bool = True,
                 sbom_ignore: tuple[str, ...] = (),
                 actions_ignore_patterns: tuple[re.Pattern[str], ...] = (),
                 actions_all_branches: bool = False,
                 actions_show_healthy: bool = False,
                 issues_ignore: tuple[str, ...] = (),
                 disabled_aspects: tuple[str, ...] = (),
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
        self.owner = owner
        # **Declared**, not discovered — and every load-time decision is made from
        # it: whether `team:` is legal, what `advanced_security_on_private`
        # defaults to, which link the security aspects may offer. `_verify_kind`
        # checks it against the account on every run, so the claim cannot rot.
        self.kind = kind
        self.is_org = kind == "organization"
        # Whether the two GitHub Advanced Security aspects apply to this account's
        # **private** repositories. Not a kind question: code scanning and secret
        # scanning are free on public repositories and paid on private ones, for
        # organizations and personal accounts alike. So the axis is visibility, and
        # the kind only decides the default (off for a personal account, on for an
        # organization) — which a config that pays for either overrides in one line.
        self.advanced_security_on_private = advanced_security_on_private
        self.team = team
        self.name_prefix = name_prefix
        self.include_archived = include_archived
        self.include_forks = include_forks
        self.api_url = api_url
        # One request's budget, NOT the run's — see `_make_client`.
        self.request_timeout = request_timeout
        # How much of the run may be spent asleep waiting out GitHub's throttle.
        # The deadline does not cover this: a run that sleeps for its whole budget
        # never overruns it, and reports nothing — which is exactly the wedged
        # check a throttle is the easiest way to build.
        self.max_pause_seconds = (self.timeout_seconds * DEFAULT_MAX_PAUSE_FRACTION
                                  if max_pause is None else max_pause)
        if self.max_pause_seconds >= self.timeout_seconds:
            # Refused rather than accepted-and-ignored, on the same argument as
            # `error_below` above `warn_below` in the rate-limit type: the deadline
            # would always bite first, so no run could ever reach this cap and the
            # config summary would state a bound that does nothing.
            raise CheckError(
                f"github 'max_pause' ({self.max_pause_seconds:g}s) must be less "
                f"than 'timeout' ({self.timeout_seconds:g}s): the run's own "
                f"deadline would end it first, so the cap could never apply")
        self.rate_limit_safety_factor = rate_limit_safety_factor
        self.expect_min_repos = _positive_int(expect_min_repos, "expect_min_repos")
        self.pr_ignore_prefixes = pr_ignore_prefixes
        self.dependabot_severities = dependabot_severities
        self.advisory_severity_map = {
            **DEFAULT_ADVISORY_SEVERITY_MAP,
            **(advisory_severity_map or {}),
        }
        self.code_scanning_security_map = {
            **DEFAULT_CODE_SCANNING_SECURITY_MAP,
            **(code_scanning_security_map or {}),
        }
        self.code_scanning_quality_map = {
            **DEFAULT_CODE_SCANNING_QUALITY_MAP,
            **(code_scanning_quality_map or {}),
        }
        self.secret_scanning_require_enabled = secret_scanning_require_enabled
        self.sbom_ignore = sbom_ignore
        self.actions_ignore_patterns = actions_ignore_patterns
        self.actions_all_branches = actions_all_branches
        self.actions_show_healthy = actions_show_healthy
        self.issues_ignore = issues_ignore
        # Aspects this check does **not** run, by name. Stored as what is switched
        # off rather than what is on, so an aspect a later release adds is on by
        # default in every config written before it existed — the opposite of what
        # an allow-list would do, which is to exclude it silently.
        self.disabled_aspects = frozenset(disabled_aspects)
        # Per-aspect display text (title/about) declared in this check's own config
        # (`subnodes:`), carried onto each aspect child (little-sister
        # ADR-0025). nodes.yaml still overrides per path.
        self.subnodes = subnodes or {}
        # What kind of account `org` names, filled by `_discover` from GitHub
        # itself. `None` means "not asked yet": every reader of it runs during a
        # run, after discovery, and the organization answer is the one that keeps
        # a standalone caller rendering what it rendered before.
        # Whether discovery can see this account's private repositories. True
        # until a user account's token says otherwise (`_discover`): an
        # organization listing made with a member's token is whole, but
        # `/users/{login}/repos` is public-only however privileged the token is.
        self._sees_private = True
        # Run state, like `_sees_private` above: how many repository reads this run
        # could not be *completed* — the "we could not ask GitHub" class only, since
        # the graded kind is already amber on the line where it happened. Every
        # aspect adds to it through `_finalize` / `_severity_bands`, and `run` reads
        # it once for the node's coverage line and resets it at the top of the run.
        self._unreachable = 0
        #: One run's per-repository reads, keyed by (endpoint, repository set), so
        #: two aspects built from one payload cost one request. Cleared at the top
        #: of every run — a cache that outlived a run would be a check that reports
        #: yesterday.
        self._collected: dict[
            tuple[str, tuple[str, ...]],
            tuple[list[tuple[Repo, list[Any]]], _Coverage, list[Repo]]] = {}

    @classmethod
    def _extra_from_config(cls, config: dict[str, Any],
                           base_dir: Path) -> dict[str, Any]:
        if config.get("org") is not None:
            # A hard cut, deliberately: the key took an account login all along and
            # `org` said something the value need not mean, which is how `org: m-31`
            # came to be written for a person. Accepting both spellings forever
            # would keep the misleading one alive in every config somebody copies.
            # A **refusal** is what makes the rename safe — the alternative fails
            # by discovering the wrong scope, or none.
            raise CheckError(
                "github 'org' has been renamed to 'owner' — it is an account "
                "login, which GitHub lets be a person or an organization. "
                "Rename the key; the value does not change.")
        owner = config.get("owner")
        if not owner:
            raise CheckError(
                "github check requires an 'owner' — the login of the "
                "organization or user account whose repositories are in scope")
        kind = config.get("kind")
        if kind not in ACCOUNT_KINDS:
            # Required, with no default. A default here would be a guess about
            # somebody's account made by this package, and it is the value every
            # other decision below is taken from — `team:`, the security link, the
            # Advanced Security default. GitHub asks the same question when you
            # create a repository, in the same two words.
            raise CheckError(
                "github check requires a 'kind' of "
                + " or ".join(repr(k) for k in ACCOUNT_KINDS)
                + f" — what {str(owner)!r} is on GitHub"
                + (f", not {kind!r}" if kind is not None else ""))
        team = str(config.get("team", ""))
        if team and kind != "organization":
            # A load-time refusal, which is what declaring the kind buys: this used
            # to be a discovery failure, found on the first run, by a token.
            raise CheckError(
                f"github check declares 'kind: {kind}' and a team {team!r}, but "
                f"only an organization has teams — remove one of the two")
        pull_requests = _block(config, "pull_requests")
        ignore = pull_requests.get("ignore_title_prefixes") or []
        if not isinstance(ignore, list):
            raise CheckError("pull_requests.ignore_title_prefixes must be a list")
        security = _block(config, "security_advisories")
        severities = security.get("severities")
        if severities is None:
            severities = ["critical", "high"]
        if not isinstance(severities, list):
            raise CheckError("security_advisories.severities must be a list")
        advisory_severity_map = _severity_map(
            security.get("severity_map"), "security_advisories")
        if config.get("code_scanning_alerts") is not None:
            # Refused at load rather than ignored, in the same shape as `org:` →
            # `owner:`. A silently dropped block would take a deployment's whole
            # grading with it and say so only as bands that suddenly read red.
            raise CheckError(
                "github 'code_scanning_alerts' is now two blocks: "
                "'code_scanning_security' (critical / high / medium / low, the "
                "severities GitHub assigns a security rule) and "
                "'code_scanning_quality' (error / warning / note, the analysis "
                "severities of everything else). Split your 'severity_map' "
                "between them; an 'enabled: false' belongs on whichever half you "
                "do not want")
        security_scanning = _block(config, "code_scanning_security")
        code_scanning_security_map = _severity_map(
            security_scanning.get("severity_map"), "code_scanning_security")
        quality_scanning = _block(config, "code_scanning_quality")
        code_scanning_quality_map = _severity_map(
            quality_scanning.get("severity_map"), "code_scanning_quality")
        secret_scanning = _block(config, "secret_scanning")
        sbom = _block(config, "sbom_check")
        sbom_ignore = sbom.get("ignore") or []
        if not isinstance(sbom_ignore, list):
            raise CheckError("sbom_check.ignore must be a list")
        actions = _block(config, "actions")
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
        issues = _block(config, "issues")
        issues_ignore = issues.get("ignore") or []
        if not isinstance(issues_ignore, list):
            raise CheckError("issues.ignore must be a list")
        # Which aspects this check does not run. `enabled:` sits in the aspect's
        # own block, beside the knobs that shape it, so a config is read top to
        # bottom — and an aspect that says nothing is on, which is what every
        # config written before this key existed says.
        disabled = tuple(
            aspect for aspect in cls.ASPECTS
            if not _flag(
                _block(config, cls.ASPECT_CONFIG_KEY[aspect]).get("enabled", True),
                f"{cls.ASPECT_CONFIG_KEY[aspect]}.enabled"))
        if len(disabled) == len(cls.ASPECTS):
            # The node would still carry the coverage backstop and the roster, so
            # this is not literally nothing — but a check whose every aspect is off
            # reports no finding about any repository it discovers, while looking
            # from the dashboard exactly like one that does. Deleting the check
            # says that out loud; this config whispers it.
            raise CheckError(
                "github check has every aspect disabled — it would report no "
                "finding about any repository. Remove the check instead.")
        subnodes = parse_subnodes(config)
        for aspect, texts in subnodes.items():
            if aspect not in cls.ASPECTS:
                # A key naming nothing was accepted and did nothing — a paragraph a
                # deployment wrote, loaded without complaint, and never drawn. The
                # roster here is **closed**, which is what makes refusing safe: this
                # check emits exactly these eight children and no more. (The sister
                # package's band list is open — a `severity_map` may legitimately
                # name a severity it does not declare — so the same refusal would be
                # wrong there, and is deliberately not made.)
                if aspect in RETIRED_ASPECTS:
                    raise CheckError(
                        f"github subnodes.{aspect} names an aspect that no longer "
                        f"exists: {RETIRED_ASPECTS[aspect]}")
                raise CheckError(
                    f"github subnodes.{aspect} names nothing this check reports. "
                    f"`subnodes:` addresses the aspects — "
                    f"{', '.join(sorted(cls.ASPECTS))} — and not the severity "
                    f"bands beneath them; a band's own title or about is set per "
                    f"node path in the deployment's nodes.yaml")
            for field_name, text in texts.items():
                if "{org}" in text:
                    # An unknown token is left as-is rather than raising, so this
                    # would otherwise reach a dashboard as a literal `{org}`. The
                    # key's rename can refuse; the token has to be refused here or
                    # it fails silently.
                    raise CheckError(
                        f"github subnodes.{aspect}.{field_name} writes the "
                        f"retired "
                        f"token '{{org}}' — it is '{{owner}}' now")
        return {
            "owner": str(owner),
            "kind": kind,
            # Default from the kind, because that is the likely plan — and a
            # deployment that pays for Advanced Security on a personal account, or
            # runs a free organization, says so in one line.
            "advanced_security_on_private": _flag(
                config.get("advanced_security_on_private",
                           kind == "organization"),
                "advanced_security_on_private"),
            "team": team,
            "name_prefix": str(config.get("name_prefix", "")),
            "include_archived": bool(config.get("include_archived", False)),
            "include_forks": bool(config.get("include_forks", True)),
            "api_url": str(config.get("api_url", GITHUB_API)),
            "request_timeout": _positive_seconds(
                config.get("request_timeout"), "request_timeout",
                int(DEFAULT_REQUEST_TIMEOUT)),
            # Absent means "derive it from `timeout:`", which is not a number this
            # classmethod can reach: the base class is what reads `timeout:`. So
            # None travels to `__init__` and the default is resolved there.
            "max_pause": (None if config.get("max_pause") is None else
                          _positive_seconds(config["max_pause"], "max_pause", 0)),
            "rate_limit_safety_factor": int(
                config.get("rate_limit_safety_factor", 4)),
            "expect_min_repos": _positive_int(
                config.get("expect_min_repos", 1), "expect_min_repos"),
            "pr_ignore_prefixes": tuple(str(p) for p in ignore),
            "dependabot_severities": tuple(str(s).lower() for s in severities),
            "advisory_severity_map": advisory_severity_map,
            "code_scanning_security_map": code_scanning_security_map,
            "code_scanning_quality_map": code_scanning_quality_map,
            "secret_scanning_require_enabled": bool(
                secret_scanning.get("require_enabled", True)),
            "sbom_ignore": tuple(str(r) for r in sbom_ignore),
            "actions_ignore_patterns": action_patterns,
            "actions_all_branches": bool(actions.get("all_branches", False)),
            "actions_show_healthy": bool(actions.get("show_healthy", False)),
            "issues_ignore": tuple(str(r) for r in issues_ignore),
            "disabled_aspects": disabled,
            "subnodes": subnodes,
            # `secrets: {token: …}` — required, so two checks of this type can
            # each carry their own team's credential (little-sister ADR-0023).
            "token_ref": parse_secret_refs(config, "token")["token"],
        }

    def config_summary(self) -> str:
        scope = f"{self.owner}/{self.team}" if self.team else self.owner
        scope = f"{scope} ({self.kind})"
        return config_markdown({
            "scope": scope,
            "name prefix": self.name_prefix or None,
            "include archived": "yes" if self.include_archived else "no",
            "expected repositories": str(self.expect_min_repos),
            "pause budget": f"{self.max_pause_seconds:.0f}s",
            "show healthy Actions": "yes" if self.actions_show_healthy else "no",
            # Only when there are any: a disabled aspect leaves no node, so without
            # this line the difference between "that aspect is off" and "somebody
            # broke the check" is invisible on the very page an operator opens to
            # find out which.
            "aspects switched off": (
                ", ".join(sorted(self.disabled_aspects))
                if self.disabled_aspects else None),
        })

    def active_aspects(self) -> tuple[str, ...]:
        """The aspects this check runs, in `ASPECTS` order — every aspect its
        config did not switch off."""
        return tuple(name for name in self.ASPECTS
                     if name not in self.disabled_aspects)

    @classmethod
    def aspect_rank(cls, name: str) -> int:
        """Where this aspect sorts among its siblings (little-sister ADR-0055).

        `ASPECTS`' own sequence, from 1. **Not from 0**, which is not a neutral value
        here: `0` is the rank the unranked carry and sorts *before* every positive
        one (little-sister ADR-0055 decision 4), so a rank of `0` would put an aspect
        at the front of the row rather than leave it where it was.

        Ranks stay dense across a config that switches an aspect off: the gap is a
        node that is not there, and nothing sorts into it.
        """
        return cls.ASPECTS.index(name) + 1

    @staticmethod
    def _grading_sentence(severity_map: dict[str, StatusCode],
                          watched: tuple[str, ...] | None = None) -> str:
        """The grading **in force**, as a sentence for the aspect's `about`.

        The shipped text used to name the key that sets the mapping, which is the
        one place a reader on a dashboard cannot look — the defaults live in this
        package's source. This renders the map the check is actually using, so a
        deployment that overrode it sees *its* answer and not ours.
        """
        names = watched if watched is not None else tuple(severity_map)
        if not names:
            return "Findings are grouped into severity-band children."
        codes = [severity_map.get(name, StatusCode.WARN) for name in names]
        if len(set(codes)) == 1:
            # One answer for every band is worth saying once. Eight identical
            # arrows are a wall a reader skips, and skipping is how the setting
            # stays invisible — which is the whole complaint this text answers.
            graded = f"**{codes[0].name}** for every band"
        else:
            graded = ", ".join(f"`{name}` → **{code.name}**"
                               for name, code in zip(names, codes, strict=True))
        return ("Findings are grouped into severity-band children, graded "
                f"{graded}. A band with no findings reads `OK`, so a watched "
                "band's silence is visible.")

    def _subnode_tokens(self) -> dict[str, str]:
        """Values a `subnodes:` `about` may reference as `{token}` — this check's
        own `owner` / `team`, the three security-overview link sentences, and the
        shared `{pin_note}` sentence.

        There is no `{org}`: it was renamed with the key it named, and a config
        still writing it is refused at load rather than rendering the literal
        token on a dashboard (`_extra_from_config`)."""
        return {
            "owner": self.owner,
            "team": self.team,
            "pin_note": PIN_NOTE,
            "advisories_link": self._security_overview_link(
                "dependabot", "All open advisories"),
            "code_scanning_link": self._security_overview_link(
                "code-scanning", "All open alerts"),
            "secret_scanning_link": self._security_overview_link(
                "secret-scanning", "All open alerts"),
            "advisories_grading": self._grading_sentence(
                self.advisory_severity_map, self.dependabot_severities),
            "code_scanning_security_grading": self._grading_sentence(
                self.code_scanning_security_map),
            "code_scanning_quality_grading": self._grading_sentence(
                self.code_scanning_quality_map),
        }

    def _security_overview_link(self, family: str, text: str) -> str:
        """One aspect's link to the organization-wide security overview — or
        **nothing at all**.

        `https://github.com/orgs/<login>/security/alerts/…` is an organization page:
        a personal account does not have one, and GitHub answers 404. So for a
        personal account this expands to the empty string and the aspect ships its
        text without a link rather than with a dead one — which is the whole reason
        the sentence is a token instead of a line in `SUBNODES`.

        The team clause is written **only when there is a team**. `team:` with an
        empty value is a filter that matches nothing, not an absent filter, so the
        no-team form of this link used to lead to an empty overview page.
        """
        if not self.is_org:
            return ""
        terms = ["is:open"]
        if self.team:
            terms.append(f"team:{self.team}")
        query = urllib.parse.urlencode({"query": " ".join(terms)})
        return (f"[{text}](https://github.com/orgs/{_path(self.owner)}"
                f"/security/alerts/{family}?{query}).")

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

    def _new_deadline(self) -> Deadline:
        """This run's budget. Overridden in tests, so a deadline can be spent
        without a test spending one."""
        return Deadline(self.timeout_seconds)

    def _make_client(self, token: str,
                     deadline: Deadline | None = None) -> GitHubClient:
        """Build the API client. Overridden in tests to avoid live calls.

        The two budgets are handed over separately, because they are not the same
        one: ``request_timeout`` bounds a single request, ``deadline`` the whole
        run. Before that split, ``timeout:`` was passed here as the *per-request*
        value — so the documented per-run budget was spent afresh on every one of
        the hundreds of requests a run makes, and bounded nothing (ADR-0002).
        """
        return GitHubClient(token, api_url=self.api_url,
                            timeout=self.request_timeout, deadline=deadline,
                            max_pause=self.max_pause_seconds)

    def _verify_kind(self, client: GitHubClient) -> None:
        """Check the account against what this config **declared** it to be.

        `kind:` is the config's claim and it is what every load-time decision is
        made from — whether `team:` is legal, what `advanced_security_on_private`
        defaults to. A claim nobody checks decays, and the two ways it decays are
        both silent: a login mistyped into the other kind's name discovers the
        wrong scope, and GitHub lets a personal account **convert** to an
        organization, at which point a correct config becomes a wrong one without
        anybody touching it.

        ``GET /users/{login}`` answers for both kinds — an organization comes back
        from it with ``type: Organization`` — so one call settles it, and it is the
        only endpoint that does: asking `/orgs/{login}` would mean reading a 404 as
        an answer, and a 404 is equally what a misspelled name produces.

        A disagreement names **both** claims, because either one can be the thing
        that is wrong.
        """
        reported = _REPORTED_KIND.get(values.text(client.get(
            f"/users/{_path(self.owner)}"), "type"), "")
        if not reported:
            # GitHub answered, and the answer names a kind this check has never
            # heard of — the reading is unusable, not a statement about the account.
            raise GitHubError(
                f"{self.owner!r} is neither a user nor an organization",
                fault=Fault.MALFORMED)
        if reported != self.kind:
            # An answer, and a true one: the config is wrong, and asking again gets
            # the same reply faster.
            raise GitHubError(
                f"this check declares 'kind: {self.kind}', but GitHub reports "
                f"{self.owner!r} as a {reported} — correct the config, or point "
                f"'owner' at the account you meant",
                fault=Fault.ANSWERED)

    def _is_own_account(self, client: GitHubClient) -> bool:
        """Is ``owner`` the account this check's own token belongs to?

        It decides whether discovery can see private repositories at all, because
        ``/users/{login}/repos`` returns **public repositories only** however
        privileged the token is; a personal account's private repositories are
        listed by exactly one endpoint, ``/user/repos``, and only for its owner.

        A token that cannot read its own account answers "no" rather than failing
        the run: the public listing is still a reading, and the smaller scope is
        reported on the node.
        """
        try:
            login = values.text(client.get("/user"), "login")
        except GitHubError:
            logger.info("%s: cannot read the token's own account; "
                        "discovery is limited to public repositories", self.path)
            return False
        return login.casefold() == self.owner.casefold()

    def _discover(self, client: GitHubClient) -> list[Repo]:
        """The in-scope repositories (team-scoped when ``team`` is set), as typed
        :class:`Repo` values — this is the seam where the API's ``Any`` stops."""
        self._verify_kind(client)
        if self.is_org:
            repos = self._discover_org(client)
        else:
            # The only thing about the account that is *not* declared: whether this
            # token can see its private repositories. It is not a property of the
            # kind, so it cannot be a config key — it is a fact about this token
            # and this account, and only GitHub can answer it.
            self._sees_private = self._is_own_account(client)
            repos = self._discover_user(client)
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

    def _discover_org(self, client: GitHubClient) -> list[Any]:
        """An organization's repositories, narrowed to one team when asked."""
        if not self.team:
            return client.get_paginated(f"/orgs/{_path(self.owner)}/repos")
        teams = client.get_paginated(f"/orgs/{_path(self.owner)}/teams")
        match = next((t for t in teams
                      if self.team in (values.text(t, "slug"),
                                       values.text(t, "name"))), None)
        if match is None:
            # The team list arrived and the team is not in it — an answer, and the
            # config is what needs correcting.
            raise GitHubError(
                f"team {self.team!r} not found in org {self.owner!r}",
                fault=Fault.ANSWERED)
        # NOT `slug`: that name holds the imported slug builder, and rebinding
        # it here would shadow the function for the rest of this scope — a trap
        # for the next aspect that needs it.
        team_slug = values.text(match, "slug", required=True, where="team")
        return client.get_paginated(
            f"/orgs/{_path(self.owner)}/teams/{_path(team_slug)}/repos")

    def _discover_user(self, client: GitHubClient) -> list[Any]:
        """A personal account's repositories.

        Two endpoints, and which one is reachable is not a preference: only
        ``/user/repos`` shows a personal account's **private** repositories, and it
        exists only for the token's own account. ``affiliation=owner`` is what keeps
        it a listing of *this* account rather than of everything the token can
        reach — without it the endpoint also returns repositories the account merely
        collaborates on or reaches through an organization, which no `owner:` in any
        config asked for.

        ``team`` cannot be set here: the config declaring `kind: user` alongside a
        team is refused at load (`_extra_from_config`), and a config declaring
        `kind: organization` never reaches this method.
        """
        if self._sees_private:
            return client.get_paginated("/user/repos", {"affiliation": "owner"})
        return client.get_paginated(f"/users/{_path(self.owner)}/repos")

    # --- aspects -------------------------------------------------------------

    def _advanced_security_scope(
            self, repos: list[Repo]) -> tuple[list[Repo], str]:
        """The repositories the two Advanced Security aspects may read, and a run
        fact naming the ones they may not.

        Code scanning and secret scanning are **free on a public repository and
        paid on a private one**, for organizations and personal accounts alike. So
        when `advanced_security_on_private` is off, the private repositories drop
        out of these two aspects entirely — rather than every one of them being
        reported as "scanning not enabled", which is true and useless.

        The skipped ones are named on the node (little-sister ADR-0044): an aspect
        that quietly reads half its scope and reports OK is the shape of a monitor
        that has stopped monitoring.
        """
        if self.advanced_security_on_private:
            return repos, ""
        scanned = [repo for repo in repos if not repo.private]
        skipped = [repo for repo in repos if repo.private]
        if not skipped:
            return scanned, ""
        return scanned, (
            f"{len(skipped)} private "
            + ("repository" if len(skipped) == 1 else "repositories")
            + " not read: Advanced Security is paid on private repositories and "
            "`advanced_security_on_private` is off — "
            + ", ".join(plain(repo.name) for repo in skipped))

    def _pull_requests(self, client: GitHubClient,
                       repos: list[Repo]) -> CheckResult:
        """WARN per open pull request (excluding ignored titles).

        **Per repository, like every other aspect.** This one used to wrap its whole
        loop in a single `try` and return the aspect as ERROR on the first failure,
        so one repository's bad minute cost the findings about all the others — the
        all-or-nothing shape ADR-0002 rejects, applied one level down.
        """
        entries: list[Entry] = []
        coverage = _Coverage()
        for repo in repos:
            try:
                prs = client.get_paginated(
                    f"/repos/{repo.full_name}/pulls", {"state": "open"})
            except GitHubError as error:
                coverage.failed(repo, error)
                continue
            coverage.read_one()
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
                entries.append(Entry(
                    _entry_slug(repo, "pr", number, url),
                    f"{_link(f'{name}: {plain(subject)}', url)} "
                    f"[{plain(user)}]",
                    code=StatusCode.WARN))
        return self._finalize("pull_requests",
                              "Open pull requests awaiting attention",
                              entries, coverage)

    def _collect(self, client: GitHubClient, repos: list[Repo],
                 suffix: str) -> tuple[
                     list[tuple[Repo, list[Any]]], _Coverage, list[Repo]]:
        """Fetch an open-alerts list per repo. A **404** means the feature is not
        enabled for that repo; those repos are returned separately (as
        ``not_enabled``) so a caller can skip them quietly *or* flag them. Every
        other failure goes to the :class:`_Coverage`, which decides from the status
        whether it is a claim about this repository or a note about the run.

        The notes are keyed like the findings, whichever kind they are: "this
        repository could not be read" is a condition somebody may well be working
        on — a missing scope, an archived repo, an endpoint having a bad hour — and
        it should be pinnable without silencing the alerts that *did* come back."""
        key = (suffix, tuple(repo.full_name for repo in repos))
        cached = self._collected.get(key)
        if cached is not None:
            # The code-scanning split partitions one payload into two aspects, so
            # the second asks for a read the run has already made. Keyed by the
            # repository set as well as the endpoint, because the two GHAS aspects
            # run against a scope `advanced_security_on_private` may have narrowed
            # — a hit must mean *the same question*, not merely the same URL.
            return cached
        results: list[tuple[Repo, list[Any]]] = []
        coverage = _Coverage()
        not_enabled: list[Repo] = []
        for repo in repos:
            try:
                alerts = client.get_paginated(
                    f"/repos/{repo.full_name}{suffix}", {"state": "open"})
            except GitHubError as error:
                if error.status == 404:
                    not_enabled.append(repo)
                    coverage.read_one()
                    continue
                coverage.failed(repo, error)
                continue
            coverage.read_one()
            results.append((repo, alerts))
        collected = (results, coverage, not_enabled)
        self._collected[key] = collected
        return collected

    def _finalize(self, name: str, description: str,
                  entries: list[Entry], coverage: _Coverage,
                  report: str = "") -> CheckResult:
        """One aspect leaf from its findings and what it managed to look at.

        Every line is an :class:`Entry` **carrying its own code**, and this result
        declares none: the node's code is derived as the worst of them (little-sister
        ADR-0042). That is what lets a "could not ask GitHub" line exist at all —
        it is `UNDEFINED`, which the derivation skips, so it is displayed and
        pinnable without grading anybody's repository.

        The lines are members either way (little-sister ADR-0036): each is an
        independent condition, so an operator who opens a ticket for one can pin
        that line and leave the rest of the aspect reporting.
        """
        # One read, one count. The two code-scanning aspects are built from one
        # payload, and an outage that stopped one read must not say it stopped two.
        if not coverage.counted:
            coverage.counted = True
            self._unreachable += coverage.unreachable
        title, about = self._meta(name)
        return CheckResult(
            reason=(*entries, *coverage.lines()),
            entries=True,
            name=name, description=description,
            title=title, about=about, report=report)

    def _severity_bands(
        self, name: str, description: str,
        groups: dict[str, list[tuple[str, str]]],
        severity_map: dict[str, StatusCode],
        coverage: _Coverage,
        *, declared_order: tuple[str, ...] = SECURITY_SEVERITY_ORDER,
        report: str = "",
    ) -> CheckResult:
        """One aspect branch with one uncoded leaf per source-severity band.

        The grouping carries severity onto the node rather than burying it in the
        reason text. Empty configured bands still render as OK, making it visible that
        they were watched. Read failures stay on the aspect **container**: they have
        no honest source severity and must not be smuggled into one. The container
        declares no code of its own, so an unreadable repository leaves it
        `UNDEFINED` — which the tree ignores in favour of the bands beneath it,
        exactly as "I have nothing to say" should behave.
        """
        seen = set(declared_order)
        # Three tiers, and the third is why this is not simply `band_order.index`.
        # The first two were **stated** — by this aspect's declared tuple, then by a
        # `severity_map` (this package's default or a deployment's) — so their
        # sequence is somebody's decision and each keeps its own rank. The third
        # arrives from the data in whatever order the payload had, which is nobody's
        # decision at all: those share one rank, after every stated band, and the
        # name half of little-sister's sort key orders them (ADR-0055).
        stated = [*declared_order,
                  *(severity for severity in severity_map if severity not in seen)]
        ranks = {severity: index + 1 for index, severity in enumerate(stated)}
        unstated_rank = len(stated) + 1
        band_order = [*stated,
                      *(severity for severity in groups
                        if severity not in seen and severity not in severity_map)]
        children: list[CheckResult] = []
        for severity in band_order:
            if severity not in severity_map and severity not in groups:
                continue
            entries = groups.get(severity, [])
            code = (severity_map.get(severity, StatusCode.WARN) if entries
                    else StatusCode.OK)
            mapped = severity_map.get(severity)
            children.append(CheckResult(
                code, entries, name=severity,
                description=f"{severity.capitalize()} {description}",
                title=band_glyph(severity),
                order=ranks.get(severity, unstated_rank),
                config=config_markdown({
                    "graded": (f"`{mapped.name}` when this band has findings"
                               if mapped is not None else
                               "`WARN` when this band has findings — no "
                               "`severity_map` entry, so the fallback applies"),
                    "when empty": "`OK`, so a watched band's silence is visible",
                })))
        # One read, one count. The two code-scanning aspects are built from one
        # payload, and an outage that stopped one read must not say it stopped two.
        if not coverage.counted:
            coverage.counted = True
            self._unreachable += coverage.unreachable
        title, about = self._meta(name)
        return CheckResult(
            reason=coverage.lines(),
            entries=True,
            name=name,
            description=description,
            children=tuple(children),
            title=title,
            about=about,
            report=report,
        )

    def _security_advisories(self, client: GitHubClient,
                             repos: list[Repo]) -> CheckResult:
        """Open Dependabot alerts, grouped into configured severity bands."""
        results, coverage, _ = self._collect(client, repos, "/dependabot/alerts")
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
            severity_map, coverage,
            declared_order=self.dependabot_severities)

    def _code_scanning_groups(
        self, client: GitHubClient, repos: list[Repo]
    ) -> tuple[dict[str, dict[str, list[tuple[str, str]]]], _Coverage, str]:
        """One `/code-scanning/alerts` read, **partitioned by which scale grades it**.

        GitHub gives a code-scanning alert two severities and files them under two
        headings of its own: a *security* severity, when the rule has one, and the
        rule's *analysis* severity always. This check reported one eight-band row
        built from the first field alone, defaulting it to `none` — so `error`,
        `warning` and `note` were rendered, and watched, and unreachable, because
        nothing ever wrote them (ADR-0005).

        **One field per alert, so the two aspects partition rather than double-count.**
        The security severity where GitHub assigned one; the analysis severity
        otherwise. An alert with neither lands in a `none` band under quality, which
        no default map names — so it renders only if it actually happens, and says so
        as a band nobody declared rather than as a silent zero.

        Returns `{"security": groups, "quality": groups}`, the shared coverage, and
        the scope report. The read behind it is memoized for the run
        (:meth:`_collect`), so the second aspect costs no request.
        """
        repos, report = self._advanced_security_scope(repos)
        results, coverage, _ = self._collect(
            client, repos, "/code-scanning/alerts")
        groups: dict[str, dict[str, list[tuple[str, str]]]] = {
            "security": {}, "quality": {}}
        for repo, alerts in results:
            for alert in alerts:
                security = values.text(
                    alert, "rule", "security_severity_level").lower()
                scale = "security" if security else "quality"
                severity = security or values.text(
                    alert, "rule", "severity", default="none").lower()
                detail = (values.text(alert, "rule", "description")
                          or values.text(alert, "rule", "id")
                          or "alert")
                url = values.text(alert, "html_url")
                number = values.number(alert, "number")
                name = plain(repo.name)
                groups[scale].setdefault(severity, []).append((
                    _entry_slug(repo, "codescan", number, url),
                    _link(f"{name}: {plain(detail)}", url)))
        return groups, coverage, report

    def _code_scanning_security(self, client: GitHubClient,
                                repos: list[Repo]) -> CheckResult:
        """Code-scanning alerts GitHub gave a **security** severity."""
        groups, coverage, report = self._code_scanning_groups(client, repos)
        return self._severity_bands(
            "code_scanning_security", "code-scanning security alerts",
            groups["security"], self.code_scanning_security_map, coverage,
            declared_order=SECURITY_SEVERITY_ORDER, report=report)

    def _code_scanning_quality(self, client: GitHubClient,
                               repos: list[Repo]) -> CheckResult:
        """Code-scanning alerts with no security severity, by the rule's own."""
        groups, coverage, report = self._code_scanning_groups(client, repos)
        return self._severity_bands(
            "code_scanning_quality", "code-scanning quality alerts",
            groups["quality"], self.code_scanning_quality_map, coverage,
            declared_order=ANALYSIS_SEVERITY_ORDER, report=report)

    def _secret_scanning_alerts(self, client: GitHubClient,
                                repos: list[Repo]) -> CheckResult:
        """Any open secret-scanning alert → ERROR. Unless
        ``secret_scanning.require_enabled`` is false, a repo with secret scanning
        **not enabled** is flagged too (also → ERROR): the alerts endpoint 404s
        when scanning is disabled for the repo, which would otherwise read as
        'no alerts'."""
        repos, report = self._advanced_security_scope(repos)
        results, coverage, not_enabled = self._collect(
            client, repos, "/secret-scanning/alerts")
        entries: list[Entry] = []
        for repo, alerts in results:
            for alert in alerts:
                secret = (values.text(alert, "secret_type_display_name")
                          or values.text(alert, "secret_type")
                          or "secret")
                created = values.text(alert, "created_at")
                url = values.text(alert, "html_url")
                number = values.number(alert, "number")
                name = plain(repo.name)
                entries.append(Entry(
                    _entry_slug(repo, "secret", number, url),
                    _link(f"{name}: {plain(secret)} detected {plain(created)}",
                          url),
                    code=StatusCode.ERROR))
        if self.secret_scanning_require_enabled:
            for repo in not_enabled:
                name = plain(repo.name)
                settings = (f"https://github.com/{repo.full_name}"
                            "/settings/security_analysis")
                # A distinct kind, not `secret`: "scanning is off" is a different
                # condition from "an alert fired", and pinning the one must not
                # need the other's number.
                entries.append(Entry(
                    _entry_slug(repo, "secret-scanning-off"),
                    _link(f"{name}: secret scanning not enabled", settings),
                    code=StatusCode.ERROR))
        return self._finalize(
            "secret_scanning_alerts",
            "Open secret-scanning alerts (and repos with it disabled)",
            entries, coverage, report)

    def _sbom_check(self, client: GitHubClient,
                    repos: list[Repo]) -> CheckResult:
        """A repo with code but no dependency graph (SBOM) → ERROR. Repos in
        ``sbom_ignore`` are skipped; a 404 counts as missing, a permission error
        is surfaced."""
        entries: list[Entry] = []
        coverage = _Coverage()
        for repo in repos:
            if repo.name in self.sbom_ignore:
                continue
            name = plain(repo.name)
            network = f"https://github.com/{repo.full_name}/network/dependencies"
            # At most one line per repository, so the repo and the aspect are the
            # whole identity — no number to hang it on and none needed.
            missing = Entry(_entry_slug(repo, "sbom"),
                            _link(f"{name}: missing SBOM", network),
                            code=StatusCode.ERROR)
            try:
                sbom = client.get(
                    f"/repos/{repo.full_name}/dependency-graph/sbom")
            except GitHubError as error:
                if error.status == 404:
                    entries.append(missing)
                    coverage.read_one()
                    continue
                # The endpoint this whole rule was written for: it 500s with
                # `Failed to generate SBOM: Request timed out.` often enough that
                # the amber repository line it used to produce was routine.
                coverage.failed(repo, error)
                continue
            coverage.read_one()
            if not values.rows(sbom, "sbom", "relationships", where="sbom"):
                entries.append(missing)
        return self._finalize("sbom_check",
                              "Repositories missing an SBOM (dependency graph)",
                              entries, coverage)

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
        coverage = _Coverage()
        for repo in repos:
            full = repo.full_name
            try:
                existing = self._existing_workflow_ids(client, full)
            except GitHubError as error:
                if error.status == 404:
                    coverage.read_one()
                    continue                        # Actions not enabled
                coverage.failed(repo, error, "workflows-unreadable", "workflows")
                continue
            params: dict[str, Any] = {"per_page": 100}
            if not self.actions_all_branches and repo.default_branch:
                params["branch"] = repo.default_branch
            try:
                data = client.get(f"/repos/{full}/actions/runs", params)
            except GitHubError as error:
                if error.status == 404:
                    coverage.read_one()
                    continue                        # Actions not enabled
                coverage.failed(repo, error, "runs-unreadable")
                continue
            coverage.read_one()
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
                    slug(repo.id, "workflow", workflow_id, branch),
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
        # The unreadable lines go **last**, after the healthy ones: they are the
        # least actionable thing on the leaf, and one of them is not news.
        return self._finalize(
            "actions", "Latest completed and in-flight workflow-run state",
            [*problem_entries, *running_entries, *healthy_entries], coverage)

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
        entries: list[Entry] = []
        coverage = _Coverage()
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
                    entries.append(Entry(
                        _entry_slug(repo, "issues-off"),
                        _link(f"{name}: issues are disabled", issues_url),
                        code=StatusCode.WARN))
                    coverage.read_one()
                    continue
                coverage.failed(repo, error)
                continue
            coverage.read_one()
            for row in rows:
                if _is_pull_request(row):
                    continue
                # a real int, so it can be put in the URL — an escaped value could not
                number = values.number(row, "number")
                subject = values.text(row, "title", default="unknown")
                entries.append(Entry(
                    _entry_slug(repo, "issue", number),
                    f"{_link(f'{name}: has issue {number}', f'{issues_url}/{number}')}"
                    f": {plain(subject)}",
                    code=StatusCode.WARN))
        return self._finalize("issues", "Open issues per repository",
                              entries, coverage)


    # --- run -----------------------------------------------------------------

    def _scope_reading(self, repos: list[Repo]) -> tuple[StatusCode, str]:
        """The coverage backstop on the check's owned container
        (little-sister ADR-0043)."""
        count = len(repos)
        noun = "repository" if count == 1 else "repositories"
        # What discovery could not see belongs on the reading, not in the log: a
        # personal account listed publicly is a *smaller scope than the config
        # asked for*, and a count that says nothing about it looks the same as a
        # complete one until somebody wonders where a repository went.
        limited = "" if self._sees_private else " (public only)"
        if count >= self.expect_min_repos:
            return StatusCode.OK, f"{count} {noun} in scope{limited}"
        if count:
            return (StatusCode.WARN,
                    f"{count} {noun} in scope{limited}, expected at least "
                    f"{self.expect_min_repos}")
        filters = [f"{self.kind} {plain(self.owner)}"]
        if self.team:
            filters.append(f"team {plain(self.team)}")
        if self.name_prefix:
            filters.append(f'prefix "{plain(self.name_prefix)}"')
        if limited:
            filters.append("public repositories only")
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
        # `timeout:` is the whole run's budget and this is where it starts ticking
        # — discovery included, since a run that cannot discover has spent it too.
        deadline = self._new_deadline()
        self._unreachable = 0
        self._collected = {}
        # The budgets **in force**, before any of them is spent. Three numbers that
        # decide how far a run gets, two of which are derived when a config does not
        # say them, so a reader asking "why did it stop at five aspects" cannot
        # answer it from the config file alone. Rendered with `:g`, which prints the
        # configured value rather than a rounded one: `0.5s` and `1s` are a
        # difference worth seeing here.
        logger.info("%s: run starting — timeout %gs, request timeout %gs, "
                    "max pause %gs, %d aspect(s)", self.path, self.timeout_seconds,
                    self.request_timeout, self.max_pause_seconds,
                    len(self.active_aspects()))
        try:
            client = self._make_client(self.token, deadline)
            repos = self._discover(client)
        except DeadlineExceeded as cut:
            # Nothing was read, so there is nothing partial to keep: this is the one
            # place the deadline is the whole answer rather than a footnote.
            return CheckResult(StatusCode.WARN, [plain(str(cut))])
        except GitHubError as error:
            # ADR-0002 §2, one level up. A **transient** discovery failure is *we
            # could not ask*, and grading the whole tree for GitHub's bad minute is
            # the thing this record exists to stop. Returning no children leaves
            # every aspect exactly as the last good run left it (little-sister
            # ADR-0007 does not prune), so the tree keeps its last reading and this
            # node is the one place that says why. Anything GitHub **answered** —
            # an owner or team that is not there, a token that may not look — is a
            # real defect in this deployment, and stays `ERROR`.
            if error.fault is Fault.TRANSIENT:
                return CheckResult(
                    StatusCode.WARN,
                    [f"could not ask GitHub for the repository list "
                     f"({plain(str(error))}) — every aspect keeps its last "
                     f"reading"])
            return CheckResult(StatusCode.ERROR,
                               [f"discovery failed: {plain(str(error))}"])
        # names only — the payloads are large and one line per run is enough to see
        # what the discovery filter actually selected. The account kind is on the
        # line because it is resolved rather than configured: when a scope surprises
        # somebody, the first question is which endpoint was read.
        logger.info("%s: %d repositories in scope for %s %s%s: %s", self.path,
                    len(repos), self.kind, self.owner,
                    "" if self._sees_private else " (public only)",
                    ", ".join(repo.name for repo in repos) or "(none)")
        # Discovery is inside the budget and pages like anything else, so a run that
        # never reaches its last aspects may have spent the difference here — which
        # the aspect lines below cannot show, because none of them has run yet.
        logger.info("%s: discovery took %.1fs in %d read(s) — %.0fs of the run "
                    "left", self.path, deadline.elapsed(), client.reads_made,
                    deadline.remaining())
        scope_code, scope_reason = self._scope_reading(repos)
        scope_report = self._scope_report(repos)

        try:
            _limit, remaining, _reset = client.rate_limit()
            # Distinct **endpoints**, not aspects: see `ASPECT_ENDPOINT`.
            reads = {self.ASPECT_ENDPOINT[name]
                     for name in self.active_aspects()}
            needed = max(1, len(repos)) * len(reads)
            # What the run is about to ask for against what the token has, on the
            # line whether or not it stops the run: the interesting case is the one
            # that *just* cleared the factor, which is invisible when only the
            # refusal is logged.
            logger.info("%s: %d API calls left, this run needs %d×%d = %d",
                        self.path, remaining, self.rate_limit_safety_factor,
                        needed, self.rate_limit_safety_factor * needed)
            if remaining < self.rate_limit_safety_factor * needed:
                return CheckResult(
                    StatusCode.WARN,
                    [f"skipped this run: {remaining} API calls left, need > "
                     f"{self.rate_limit_safety_factor}×{needed} "
                     f"for {len(repos)} repo(s)", scope_reason],
                    report=scope_report)
        except GitHubError:
            pass   # rate-limit endpoint unavailable — proceed rather than block
        except DeadlineExceeded as cut:
            # The one client call outside the aspect loop, so it needs its own
            # catch: `DeadlineExceeded` is deliberately not a `RemoteError`,
            # and letting it out of `run` here would hand the engine an
            # all-or-nothing check error instead of the reading below.
            return CheckResult(StatusCode.WARN, [plain(str(cut)), scope_reason],
                               report=scope_report)

        builders: dict[str, Callable[[GitHubClient, list[Repo]], CheckResult]] = {
            "pull_requests": self._pull_requests,
            "security_advisories": self._security_advisories,
            "code_scanning_security": self._code_scanning_security,
            "code_scanning_quality": self._code_scanning_quality,
            "secret_scanning_alerts": self._secret_scanning_alerts,
            "sbom_check": self._sbom_check,
            "actions": self._actions,
            "issues": self._issues,
        }
        # A disabled aspect is skipped whole: no node, and none of its calls. It is
        # not a hidden node — a node that exists and is not drawn would still be in
        # the JSON and would still hold maintenance pins.
        #
        # Built one at a time rather than in a comprehension, because the deadline
        # can end the loop: the aspects already finished are kept and reported, and
        # the rest are simply absent. Partial truth beats no truth — and the
        # engine's own failure path is all-or-nothing (little-sister ADR-0040), so
        # letting the deadline out of here would throw away every aspect that read
        # perfectly well before GitHub slowed down.
        # The deadline is checked in **two** places, and both are load-bearing.
        # Here, so an aspect whose budget is already gone is never started at all —
        # cheaper, and it does not depend on that aspect happening to make a
        # request early. And inside the client, so a long aspect is cut off partway
        # rather than running to the end of forty repositories past the deadline.
        #
        # The **pause** budget is checked in one place and not two, which is not an
        # oversight: paused seconds accrue only where the sleeping happens, so a
        # check here could never see a budget the client had not already refused.
        # Time passes whether or not this check is running; sleep does not.
        children: list[CheckResult] = []
        cut_short = ""
        # Named once and walked by position, because **which** aspects a cut-short
        # run never reached is a fact about this roster and this order — and the
        # order is fixed, so it is the same tail every time until somebody looks.
        roster = self.active_aspects()
        for position, name in enumerate(roster, start=1):
            entered = deadline.elapsed()
            reads_before = client.reads_made
            started = False
            try:
                if deadline.expired():
                    raise DeadlineExceeded(name)
                started = True
                # The rank is set **here** and not in the seven builders: it is a
                # property of the aspect roster, not of anything a builder measured,
                # and seven copies of `ASPECTS.index(...)` is how a row ends up
                # disagreeing with the constant it claims to follow.
                children.append(replace(builders[name](client, repos),
                                        order=self.aspect_rank(name)))
            except DeadlineExceeded:
                cut_short = (
                    f"run cut short after {deadline.elapsed():.0f}s of its "
                    f"{self.timeout_seconds:.0f}s timeout — "
                    f"{self._aspects_reported(children)}")
                # The **node** says how many reported; the log says which one the
                # budget died in and which never got a turn. Both halves are here
                # rather than on the node because they are about this run's
                # ordering and not about anybody's repository (ADR-0002) — and
                # without them a reader has to reconstruct the roster by hand from
                # `ASPECTS` minus their own `disabled_aspects`.
                logger.warning("%s: %s — %s; never reached: %s", self.path,
                               cut_short, self._where_it_stopped(
                                   name, position, roster, started,
                                   deadline.elapsed() - entered,
                                   client.reads_made - reads_before),
                               self._never_reached(roster, position))
                break
            except _PauseBudgetSpent as spent:
                cut_short = (
                    f"run cut short after {self._aspects_reported(children)} — a "
                    f"further {spent.wait:.0f}s wait would pass its "
                    f"{self.max_pause_seconds:.0f}s pause budget")
                logger.warning("%s: %s — %s; never reached: %s", self.path,
                               cut_short, self._where_it_stopped(
                                   name, position, roster, started,
                                   deadline.elapsed() - entered,
                                   client.reads_made - reads_before),
                               self._never_reached(roster, position))
                break
            # One line per aspect that finished, and the four numbers that say
            # where a run's budget went: which aspect, how long it took, how many
            # requests that cost, and what was left afterwards. A run that always
            # stops at the same aspect is answered by reading this column.
            logger.info("%s: aspect %d/%d %s took %.1fs in %d read(s) — %.0fs of "
                        "the run left", self.path, position, len(roster), name,
                        deadline.elapsed() - entered,
                        client.reads_made - reads_before, deadline.remaining())
        # The run's own receipt, and the line that answers *why* it ran out: how
        # many requests it made, how much of the budget those requests were, and
        # whether one endpoint sat at the request timeout or everything was merely
        # slow. Without the slowest read that difference is unanswerable from a log.
        slowest = (f"slowest read {client.slowest_read:.1f}s "
                   f"({client.slowest_path})" if client.slowest_path
                   else "no read took measurable time")
        logger.info("%s: run ended after %.1fs of its %gs timeout — %d read(s) in "
                    "%.1fs, %s, %.0fs paused, %s", self.path, deadline.elapsed(),
                    self.timeout_seconds, client.reads_made, client.read_seconds,
                    slowest, client.paused_seconds,
                    self._aspects_reported(children))
        code, reason = self._node_reading(scope_code, scope_reason, cut_short,
                                          client.paused_seconds)
        return CheckResult(
            code, reason,
            children=tuple(children),
            report=scope_report,
        )

    @staticmethod
    def _where_it_stopped(name: str, position: int, roster: tuple[str, ...],
                          started: bool, seconds: float, reads: int) -> str:
        """Which aspect the run stopped in, and whether it had begun.

        The two are different findings and the numbers cannot tell them apart: an
        aspect the loop refused to start because the budget was already gone reads
        as *0 reads in 0.0s*, and so does one that was cut off on its first request
        before anything came back. The first says the aspects before it were too
        slow; the second says this one is.
        """
        where = f"{name} ({position} of {len(roster)})"
        if not started:
            return f"{where} was never started"
        return f"{where} was cut off after {seconds:.1f}s and {reads} read(s)"

    @staticmethod
    def _never_reached(roster: tuple[str, ...], position: int) -> str:
        """The aspects after the one the run stopped in — the starving tail."""
        return ", ".join(roster[position:]) or "(none)"

    def _aspects_reported(self, children: list[CheckResult]) -> str:
        """`4 of 7 aspects reported` — what both cut-short sentences end with."""
        return (f"{len(children)} of {len(self.active_aspects())} aspects "
                f"reported")

    def _node_reading(self, scope_code: StatusCode, scope_reason: str,
                      cut_short: str, paused: float = 0.0
                      ) -> tuple[StatusCode, list[str]]:
        """The check's own node: coverage, and only coverage.

        Two facts live here rather than on the aspects, because both are about
        **this run** and not about anybody's repository (ADR-0002). One GitHub
        outage would otherwise write the same sentence onto up to seven nodes, and
        each of those nodes is supposed to be answering a question about
        repositories.

        The unreachable count is what keeps an outage from reading green: the
        repository lines it produced are `UNDEFINED` by design and grade nothing,
        so if nothing said so here, an hour of 5xx would look exactly like an hour
        of everything being fine.
        """
        reason = [scope_reason]
        code = scope_code
        unreachable = self._unreachable
        if unreachable:
            reason.append(
                f"{unreachable} repository read"
                f"{'' if unreachable == 1 else 's'} could not be completed this "
                f"run — GitHub did not answer")
            code = StatusCode.WARN if code is StatusCode.OK else code
        if paused:
            # A fact about the run, not a claim about anybody's repository, so it
            # sits here with the other two and grades nothing on its own: waiting
            # when a service asks is correct behavior. What it stops is a paused
            # run being indistinguishable from a slow one.
            reason.append(
                f"paused {paused:.0f}s for a GitHub rate limit")
        if cut_short:
            reason.append(cut_short)
            code = StatusCode.WARN if code is StatusCode.OK else code
        return code, reason
