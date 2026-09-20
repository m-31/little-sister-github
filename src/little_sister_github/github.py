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
surface** (architecture.md §11), which is what the ``require_api(2)`` in this
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
    plain,
    register,
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

from little_sister_github.budget import (
    FREE_PATHS,
    Counter,
    Ledger,
    reduce_path,
    unit_of,
)
from little_sister_github.budget import ledger as shared_ledger

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


# **Two scales, and GitHub keeps them apart — so this check does too** (ADR-0006).
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
#: rank orders the row (little-sister ADR-0055); the color says how bad it is.
#:
#: **Two scales share the ramp, and the repeats are the point** (ADR-0006). `error`
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
#: color must not borrow one — and this stays rare enough to mean something.
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
    """One configured source-severity → dashboard-code mapping.

    ``field`` is the **whole** key path, because not every map of this shape is
    spelled `<block>.severity_map`: the one that grades a disabled workflow is
    `actions.disabled_severity_map`, and a refusal has to name the key the
    deployment actually typed.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CheckError(f"github '{field}' must be a mapping")
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
#: (one check per team). It is **declared, not applied**: little-sister takes this
#: map as `subnode_defaults` and `_subnode_tokens()` as `label_tokens`, resolves a
#: deployment's `subnodes:` block over it — replacing any of these, or extending
#: one where the config writes `{default}` — and the engine writes the result per
#: aspect name. `nodes.yaml` still wins over both, per node path. `{owner}` /
#: `{team}` and the three `{…_link}` sentences expand from this check's own
#: configuration, the declared account kind included.
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
can be checked for known issues. A repository listed here has none, or none of
its manifests could be parsed, so Dependabot has nothing to check. Some repos
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

Each workflow is asked about on its own, so a workflow with nothing here does not
run on this branch — that is an answer rather than a gap (ADR-0005). `actions.branches`
replaces the default branch with the branches you name and asks each workflow about
each of them, exactly as it asks about the default one (ADR-0009); where none of them
matched a repository at all, one WARN line says so and names the default branch it saw.
Cases that still read one shared page of runs and cannot be exact:
`actions.all_branches`, a repository with more workflows than one page of the workflow
list, a budget too thin to pay for a read per workflow, and more than one named branch
once the budget has made it fall back. One WARN line then names those repositories,
because a workflow whose newest run falls outside a shared page has no state here and
would otherwise be indistinguishable from a passing one.

{pin_note}
""",
    },
}


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


def _security_overview_link(owner: str, team: str, is_org: bool,
                            family: str, text: str) -> str:
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
    if not is_org:
        return ""
    terms = ["is:open"]
    if team:
        terms.append(f"team:{team}")
    query = urllib.parse.urlencode({"query": " ".join(terms)})
    return (f"[{text}](https://github.com/orgs/{_path(owner)}"
            f"/security/alerts/{family}?{query}).")


def _subnode_tokens(*, owner: str, team: str, is_org: bool,
                    advisory_severity_map: dict[str, StatusCode],
                    dependabot_severities: tuple[str, ...],
                    code_scanning_security_map: dict[str, StatusCode],
                    code_scanning_quality_map: dict[str, StatusCode],
                    ) -> dict[str, str]:
    """Values a `subnodes:` `about` may reference as `{token}` — this check's
    own `owner` / `team`, the three security-overview link sentences, and the
    shared `{pin_note}` sentence. little-sister takes this map as
    `label_tokens` and expands it wherever the text ends up coming from (its
    ADR-0025).

    A **function of its arguments rather than of a check**, because the base
    constructor is where the library resolves these and a check has no
    attributes yet when it calls it. Every value here was a load-time fact
    already — the account kind is declared, not discovered — so nothing is lost
    by computing it a few lines earlier.

    There is no `{org}`: it was renamed with the key it named, and a config
    still writing it is refused at load rather than rendering the literal
    token on a dashboard (`_extra_from_config`)."""
    return {
        "owner": owner,
        "team": team,
        "pin_note": PIN_NOTE,
        "advisories_link": _security_overview_link(
            owner, team, is_org, "dependabot", "All open advisories"),
        "code_scanning_link": _security_overview_link(
            owner, team, is_org, "code-scanning", "All open alerts"),
        "secret_scanning_link": _security_overview_link(
            owner, team, is_org, "secret-scanning", "All open alerts"),
        "advisories_grading": _grading_sentence(
            advisory_severity_map, dependabot_severities),
        "code_scanning_security_grading": _grading_sentence(
            code_scanning_security_map),
        "code_scanning_quality_grading": _grading_sentence(
            code_scanning_quality_map),
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

    A payload with no number falls back to the finding's ``html_url``, which is unique
    per finding, and finally to ``<repo-id>-<kind>`` for the aspects that report at
    most one line per repository (a missing SBOM, secret scanning switched off). What it
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

    def gone(self, repo: Repo, what: str) -> None:
        """A repository the question no longer applies to: GitHub answered
        `NOT_FOUND` about it between discovery and the read (ADR-0008, decision 4).
        A line that grades nothing — `UNDEFINED`, displayed and pinnable — and a
        read, because it *was* answered: it is not a coverage gap."""
        self.read += 1
        self.notes.append(Entry(_entry_slug(repo, "unreadable"),
                                f"{plain(repo.name)}: {what}",
                                code=StatusCode.UNDEFINED))

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


#: The disabled workflow states GitHub's own schema names today
#: (`github/rest-api-description`, `components.schemas.workflow.properties.state`,
#: whose enum is `active`, `deleted` and these three). The documentation pages show
#: only `active` and `disabled_manually` in their examples, which is how a read
#: written from the prose alone misses two. This tuple is what the shipped map
#: grades and nothing more — whether a workflow *is* disabled is the `disabled_`
#: prefix (`_Workflow.disabled`), so a state added later is graded rather than
#: missed. `deleted` is handled where it always was: such a workflow is dropped, and
#: its last failure is not a fact about the repository today (ADR-0004 §9).
DISABLED_STATES = ("disabled_manually", "disabled_inactivity", "disabled_fork")

#: What a disabled workflow grades, before a deployment's `severity_map` (ADR-0010).
#:
#: **`disabled_manually` is WARN**: somebody switched a workflow off and the
#: repository still has it, which is the switched-off nightly job this line exists
#: to find. **`disabled_inactivity` is WARN** for the case it fires at all — GitHub
#: disables *scheduled* workflows after sixty days without activity **in a public
#: repository**, so on a private estate it is rare, and the line is worth its amber
#: where it does appear. **`disabled_fork` is OK**: GitHub disables scheduled
#: workflows on a fork by default, `include_forks` is true unless a deployment says
#: otherwise (ADR-0003 §3), so grading it would put one standing amber on the leaf
#: per fork — a state nobody chose, on repositories nobody is going to act on.
#:
#: A state this map does not name grades WARN, as an undeclared severity does
#: (ADR-0004 §6) — a state GitHub adds later is seen rather than silently green.
DEFAULT_ACTIONS_DISABLED_MAP: dict[str, StatusCode] = {
    "disabled_manually": StatusCode.WARN,
    "disabled_inactivity": StatusCode.WARN,
    "disabled_fork": StatusCode.OK,
}


@dataclass
class _Held:
    """One URL's answer, kept so the next run can ask whether it still holds.

    The `link` is here and not forgotten on purpose: `_attempt` returns
    ``(payload, Link header)`` and `get_paginated` walks that header, so a `304`
    that returned the held payload without the held `Link` would stop a paginated
    read after its first page and call the result complete.
    """

    etag: str
    payload: Any
    link: str
    #: Which reader asked for this URL — an aspect's name, or `discovery`. The
    #: sweep is keyed to it rather than to the run because an aspect is the
    #: engine's unit (little-sister ADR-0078): once aspects are paced separately, a
    #: run that exercised only `actions` must not age out every alert payload.
    reader: str
    #: Passes of *that reader* since this entry was last used. Dropped at two, not
    #: one: a run cut short by its deadline, its pause budget or a dead network
    #: touches only a prefix of the repositories, and sweeping on one pass would
    #: throw away exactly what the next run needs — the run least able to afford
    #: full reads, because it runs in whatever condition broke the last one.
    idle: int = 0


class _ConditionalCache:
    """ETags and payloads held on the **check**, so the next run can send
    `If-None-Match` (ADR-0011).

    Private transport state, and deliberately not a record in little-sister's
    sense (its ADR-0082): a record is published state on an entry — snapshotted,
    copied on every poll, serialized to every API client, rendered on the node
    page and capped at 2 KB — and whole API payloads are exactly what that cap
    exists to keep out of the tree. Nothing here reaches `data`.

    **Per check, and that is why a bare URL is a sufficient key.** GitHub's answers
    differ by credential, and a check resolves exactly one `self.token`, so two
    checks on two tokens keep two caches and no read can be served another
    credential's payload. The ledger next door is shared across tokens and keys by
    token internally; this is the other shape, and lifting it into the library
    later means putting the identity in the key first.
    """

    def __init__(self) -> None:
        self._held: dict[str, _Held] = {}
        self._reader = ""
        self._touched: set[str] = set()
        #: Entries dropped by the sweep since the last `started()`, for the report.
        self.dropped = 0

    def started(self) -> None:
        """A run is beginning: reset what this run will report."""
        self.dropped = 0

    def reading(self, reader: str) -> None:
        """The reader whose pass is beginning — an aspect's name, or `discovery`."""
        self._reader = reader
        self._touched = set()

    def etag_for(self, url: str) -> str | None:
        held = self._held.get(url)
        return held.etag if held is not None else None

    def hit(self, url: str) -> tuple[Any, str] | None:
        """The held answer for a URL GitHub says has not changed, or ``None``."""
        held = self._held.get(url)
        if held is None:
            return None
        held.idle = 0
        self._touched.add(url)
        return held.payload, held.link

    def store(self, url: str, etag: str, payload: Any, link: str) -> None:
        """Keep this answer against the reader whose pass is running."""
        self._touched.add(url)
        self._held[url] = _Held(etag=etag, payload=payload, link=link,
                                reader=self._reader)

    def sweep(self) -> None:
        """End the current reader's pass: age its untouched entries, drop the
        ones that have now missed two of its passes."""
        reader = self._reader
        for url, held in list(self._held.items()):
            if held.reader != reader or url in self._touched:
                continue
            held.idle += 1
            if held.idle >= 2:
                del self._held[url]
                self.dropped += 1
        self._reader = ""
        self._touched = set()

    def __len__(self) -> int:
        return len(self._held)


@dataclass(frozen=True)
class _Workflow:
    """One row of `/actions/workflows` — what the aspect reads off the list itself.

    The state is why this is a record rather than a name: it says whether this
    workflow's runs are worth a request at all, and that is known **before** the
    read it would pay for.
    """

    name: str
    state: str
    url: str

    @property
    def disabled(self) -> bool:
        """Whether this workflow is switched off, **by the prefix and not by the
        list**.

        GitHub names every such state `disabled_…`, and the three in
        `DISABLED_STATES` are the ones the shipped map grades. Matching the prefix
        rather than that tuple is what makes a state GitHub adds later arrive as an
        amber line naming it — an undeclared state grades WARN (ADR-0004 §6) — where
        an exact list would quietly treat it as active and spend a request reading
        the runs of a workflow that cannot run.
        """
        return self.state.startswith("disabled")


@dataclass(frozen=True)
class _WorkflowList:
    """One page of ``/actions/workflows``, and whether it was the whole answer.

    ``total`` is GitHub's own count for the query and ``listed`` is what this read
    carried, so they differ **exactly** when ``by_id`` is a cut of the repository's
    real workflow list. Carrying the pair rather than the set alone is what lets the
    aspect say so: a set that is quietly short is indistinguishable from a complete
    one, and every conclusion drawn from it inherits that silence.
    """

    #: Workflow id -> what the list said about it, for the ones that still exist.
    by_id: dict[int, _Workflow]
    listed: int
    total: int

    @property
    def partial(self) -> bool:
        return self.total > self.listed


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


def _resets_in(reset_epoch: int, now: float) -> str:
    """How long this window has left, in the words the source dashboard used.

    Lives here rather than beside the check that renders it because both check
    types say this sentence now — the budget check about the window it polled, and
    the client below about the window GitHub named on a response it had in its
    hand. One formatter, so the two lines a reader is comparing cannot drift into
    two vocabularies for the same minute.
    """
    seconds = reset_epoch - now
    if seconds <= 0:
        return "resetting now"
    minutes = int(seconds // 60)
    return f"resets in {minutes}min" if minutes else "resets in under a minute"


def _resets_at(reset_epoch: int, now: float) -> str:
    """The **trace's** form of the same clause: the minutes, and the window's end
    as a clock time beside them — `resets in 34min (21:50:07)`.

    A token has more than one window per resource (ADR-0007), and two trace lines
    saying `resets in 34min` and `resets in 39min` read as one window that moved
    until the clock time shows they are two. The clock is the log's own — the
    machine's local time, which the timestamp at the head of the line is written
    in — so the two are compared on one clock. The node keeps `_resets_in`: a
    reader there wants the wait, not the hour.
    """
    return (f"{_resets_in(reset_epoch, now)} "
            f"({time.strftime('%H:%M:%S', time.localtime(reset_epoch))})")


def _detail_of(body: str) -> str:
    """What of a refusal's body goes on the line: the first two hundred
    characters, or — when the body is an HTML page rather than an API answer,
    which is what GitHub's gateway sends for a request it ended at ten seconds —
    that page's title alone. Two hundred characters of nginx's markup on a
    repository's line said nothing its title does not."""
    stripped = body.lstrip()
    if stripped[:6].lower().startswith(("<html", "<!doct")):
        title = re.search(r"<title>(.*?)</title>", stripped, re.IGNORECASE | re.DOTALL)
        if title:
            return " ".join(title.group(1).split())[:200]
        return "an HTML page, not an API answer"
    return body[:200]


def _header_int(response: Response, name: str) -> int | None:
    """One integer budget header, or ``None`` when it is absent or unreadable.

    ``None`` and not ``0``: *GitHub did not say* and *nothing left* are opposite
    readings, and the zero would be the alarming one.
    """
    value = response.headers.get(name)
    if not isinstance(value, str):
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class RateLimitHeaders:
    """What GitHub said about the budget **on one response**.

    Every REST answer carries these, so a run making a hundred reads is told a
    hundred times what its own requests cost — and this package read them only when
    deciding whether a refusal was a throttle (:func:`_throttle_wait`), throwing the
    rest away. The ``/rate_limit`` endpoint was then the only budget anything here
    could see, and it answers a **different question**: the body reports a bucket
    looked up by identity, these headers report the bucket *the request in your hand*
    was charged to. Where an identity makes those two disagree, the endpoint reads a
    pristine budget while every response says otherwise, and nothing in a log can
    show it — which is the observation this class exists to make loggable.

    ``resource`` is the half no body can supply: ``x-ratelimit-resource`` **names**
    the bucket GitHub charged — ``core``, ``graphql``, ``search`` — so a read landing
    somewhere nobody expected says so itself.

    Every field is optional because every header is: an Enterprise Server
    installation, or anything sitting between us and the API, may send none of them.
    """

    resource: str = ""
    limit: int | None = None
    remaining: int | None = None
    used: int | None = None
    reset: int | None = None

    @classmethod
    def from_response(cls, response: Response) -> RateLimitHeaders | None:
        """This response's budget headers, or ``None`` when it carried none.

        ``None`` for *nothing was stated* rather than an instance of five absent
        fields, so the one caller that matters — the log line — can say **GitHub
        sent no budget headers**, which is itself a finding: a proxy that strips
        them reads exactly like a run that spends nothing.
        """
        numbers = {name: _header_int(response, f"x-ratelimit-{name}")
                   for name in ("limit", "remaining", "used", "reset")}
        stated = response.headers.get("x-ratelimit-resource")
        resource = stated.strip() if isinstance(stated, str) else ""
        if not resource and all(value is None for value in numbers.values()):
            return None
        return cls(resource=resource, **numbers)

    def text(self, now: float | None = None) -> str:
        """This reading as one clause, in the budget check's own vocabulary.

        Absent parts are **left out** rather than rendered as zeros or as "unknown":
        the line is read beside the budget check's, and a number that is not there
        must not look like a number that is.
        """
        parts: list[str] = []
        if self.remaining is not None and self.limit is not None:
            parts.append(f"{self.remaining} of {self.limit} left")
        elif self.remaining is not None:
            parts.append(f"{self.remaining} left")
        elif self.limit is not None:
            parts.append(f"limit {self.limit}")
        if self.used is not None:
            parts.append(f"{self.used} used")
        if self.reset:
            parts.append(_resets_at(self.reset,
                                    time.time() if now is None else now))
        return (f"{self.resource or 'budget'}: "
                f"{', '.join(parts) or 'no numbers stated'}")


def _first_sight(counter: Counter | None, now: float, where: str) -> None:
    """One INFO line the first time a window is seen with spend already on it.

    Unconditional on the log — a reader there has the timestamps, which say
    whether the spend was this deployment's own before a restart or somebody
    else's — and marked when this process saw the window open, the one case the
    node then states (`Counter.before_us`). Written by whoever recorded the
    reading, because the ledger keeps no logger of its own.
    """
    if counter is None or counter.read_at != now or counter.first_at != now:
        return                                  # not the reading that opened it
    if counter.before_us is not None:
        spent, seen = counter.before_us, " — this process saw it open"
    else:
        # Less the opening reading where GitHub charged for it — `first_charged`,
        # not `charged`: a `304` lands on the counter without spending (ADR-0011).
        spent = max(0, (counter.used or 0) - (1 if counter.first_charged else 0))
        seen = " — before a restart, or by something else"
    if spent:
        logger.info("%s: first reading of the %s window, %s — %d used before "
                    "this process read it%s", where, counter.resource or "budget",
                    _resets_at(counter.reset, now), spent, seen)


#: What the trace says when GitHub sent no budget headers at all. A sentence and
#: not a blank, for the reason :meth:`RateLimitHeaders.from_response` returns
#: ``None``: a missing header is a finding about the path to GitHub, and a line
#: that simply ended early would be read as a run that made no requests.
NO_BUDGET_HEADERS = "no budget headers on that read"


def budget_said(headers: RateLimitHeaders | None,
                now: float | None = None) -> str:
    """One log-ready clause for the last budget GitHub stated, or its absence."""
    return NO_BUDGET_HEADERS if headers is None else headers.text(now)


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
                 sleep: Callable[[float], None] = time.sleep,
                 ledger: Ledger | None = None,
                 cache: _ConditionalCache | None = None) -> None:
        self._token = token
        self._api = api_url.rstrip("/")
        self._timeout = timeout
        self._deadline = deadline
        self._retries = retries
        self._backoff = backoff
        self._max_pause = max_pause
        self._sleep = sleep
        #: Where every response's budget headers go (ADR-0007, decision 1). The
        #: process's own ledger unless a caller hands one in, which only a test
        #: does: the point of the ledger is that both check types read one
        #: memory, and a client keeping its own would be the `github` check
        #: reporting the budget itself — the design ADR-0001 refused.
        self._ledger = ledger if ledger is not None else shared_ledger()
        #: The conditional-request cache, or ``None`` for a client that makes
        #: none. Injected the way the ledger is and **not** defaulted to a shared
        #: one, which is the whole difference between them: the ledger is one
        #: memory for every token and keys by token inside, while a cache keyed by
        #: bare URL is only sound for as long as it belongs to a single
        #: credential (ADR-0011).
        self._cache = cache
        #: Attempts GitHub answered `304` — asked for, and charged for by nobody.
        #: `reads_made` counts them, because they are attempts and they took time;
        #: this says how many of those cost nothing, which is what a reader
        #: comparing the guard's estimate against the spend needs.
        self.free_reads = 0
        #: Seconds this client actually spent asleep, **by cause** (ADR-0007,
        #: decision 4): because GitHub asked — a `retry-after`, or an exhausted
        #: primary window with its reset, the throttle path of `_refusal` — and
        #: on the library's backoff after a failure GitHub did not explain, a 5xx
        #: or a connection that never answered. Measured where the waiting
        #: happens — `ask` decides to wait and calls the sleep injected here, so
        #: wrapping it is the only place that can tell a wait that happened from
        #: one the deadline refused. Two totals and not one, because the node
        #: says which happened: for two weeks one deployment's node said *rate
        #: limit* eighty-six times about waits that were every one of them our
        #: own one-second backoff after a 500.
        self.throttled_seconds = 0.0
        self.retried_seconds = 0.0
        #: Set by `_refusal` when the response it is reading is a throttle, read
        #: and cleared by the very next `_slept`, and cleared before every
        #: attempt: `ask` calls the sleep straight after the operation raised, so
        #: the flag set by that refusal is the flag the sleep reads, and a wait the
        #: deadline refused instead leaves nothing behind for a later sleep to
        #: misread.
        self._asked_to_wait = False
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
        #: What GitHub said the budget was on the **most recent attempt**, or
        #: ``None`` when it carried no budget headers — or never reached a status
        #: at all. Cleared before every attempt and set by its answer, never
        #: carried over (ADR-0007, decision 5): the question it exists for is
        #: *what is GitHub charging these reads to, right now*, and a read that
        #: got no answer must not trace as the previous response's numbers, which
        #: is how three aspects on a dead network once reported the fourth's
        #: window as their own. The history is the ledger's; this is one reading.
        self.last_rate_limit: RateLimitHeaders | None = None

    @property
    def paused_seconds(self) -> float:
        """Both kinds of wait together — what the pause budget is spent against
        and what the run's receipt states; the node states the two apart."""
        return self.throttled_seconds + self.retried_seconds

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

    def _path_of(self, url: str) -> str:
        """The API path of a URL this client built or was handed by a `Link`
        header — what a reader compares against `ASPECT_ENDPOINT`, and what the
        ledger reduces to the resource a read was of; stable across `api_url`."""
        return url[len(self._api):] if url.startswith(self._api) else url

    def _counted(self, url: str, seconds: float) -> None:
        """Add one attempt to what this client has spent."""
        self.reads_made += 1
        self.read_seconds += seconds
        if seconds > self.slowest_read:
            self.slowest_read = seconds
            self.slowest_path = self._path_of(url)

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        """What every request to this API carries. The ``User-Agent`` is
        `fetch`'s — ``little-sister/<version>`` — which is a name a GitHub support
        thread can do something with, unlike the ``Python-urllib`` this used to
        send. A request with a body says what the body is."""
        headers = {"Authorization": f"Bearer {self._token}",
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _attempt(self, url: str, *, method: str = "GET",
                 body: bytes | None = None) -> tuple[Any, str]:
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
        # Cleared **before** the request rather than in the `except` below, so
        # that every way out of this method — an answer, a refusal, a request
        # that never reached a status — leaves it saying what *this* attempt
        # read. For the last of those that is *no budget headers on that read*,
        # which is the truth about it (ADR-0007, decision 5).
        self.last_rate_limit = None
        self._asked_to_wait = False
        started = self._now()
        headers = self._headers(json_body=body is not None)
        # **The budget read is never held** (ADR-0011). `/rate_limit`'s *body* is a
        # budget reading — `Ledger.record` consumes one of its resource rows as if
        # it were a response's own headers — so serving a held body there would
        # feed the ledger a stale window as a current one, and the budget is what
        # the guard reasons from. Its headers on a `304` elsewhere are fine: those
        # come with the live response.
        cache = (self._cache if method == "GET" and body is None
                 and reduce_path(self._path_of(url)) not in FREE_PATHS else None)
        held_etag = cache.etag_for(url) if cache is not None else None
        if held_etag is not None:
            headers["If-None-Match"] = held_etag
        try:
            response = fetch(url, timeout=self._timeout, follow_redirects=True,
                             method=method, data=body,
                             headers=headers,
                             deadline=self._deadline)
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
        # **Before** the status is read, so a refusal is recorded too — and the
        # refusal is the one that matters most: a throttled 403 or 429 carries the
        # numbers that say whether the budget ran out or something else did. Not in
        # the `finally` above, where a request that never reached a status has no
        # response to read.
        self.last_rate_limit = stated = RateLimitHeaders.from_response(response)
        if stated is not None:
            # Every reading, to the ledger, with the path it was charged for: the
            # split between GitHub's counters is by path and the ledger observes
            # it here, one response at a time. The wall clock rather than `_now`,
            # because a window's `reset` is an epoch and a counter is compared
            # against it (ADR-0007, decision 1).
            read_at = time.time()
            _first_sight(self._ledger.record(
                self._token, self._path_of(url), resource=stated.resource,
                limit=stated.limit, remaining=stated.remaining,
                used=stated.used, reset=stated.reset, now=read_at,
                status=response.status),
                read_at, reduce_path(self._path_of(url)))
        # **Before the refusal**, or `fault_for(304)` reads *not modified* as an
        # answer and the repository gets a `could not read` line for not having
        # changed. A `304` is only ever received because this client sent the
        # `If-None-Match` the cache handed it, so the held answer is there; if a
        # sweep has taken it since, there is nothing to return and the refusal
        # below is the honest outcome rather than an empty payload.
        if response.status == 304 and self._cache is not None:
            answer = self._cache.hit(url)
            if answer is not None:
                self.free_reads += 1
                return answer
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
        link = response.headers.get("Link", "")
        etag = response.headers.get("ETag", "")
        # A read that carries no `ETag` is simply never held, which is what makes
        # "cache only what comes back unchanged" an outcome rather than a rule
        # somebody has to keep per endpoint.
        if cache is not None and etag:
            cache.store(url, etag, data, link)
        return data, link

    def _refusal(self, response: Response, url: str) -> GitHubError:
        """A status GitHub refused with, read as one of the three faults.

        The **throttle** is the one this package has to decide for itself, and
        getting it wrong is not cosmetic: a rate-limited ``403``/``429`` classified
        as *answered* is reported as though GitHub had said **no** about the
        repository, and is never retried.
        """
        detail = _detail_of(response.text())
        wait = _throttle_wait(response)
        self._asked_to_wait = wait is not None
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
        return self._ask(url)

    def _ask(self, url: str, *, method: str = "GET",
             body: bytes | None = None) -> tuple[Any, str]:
        """`ask`, with this client's policy — the one place the retry, the
        deadline and the counted sleep are wired, for both dialects."""
        return ask(lambda: self._attempt(url, method=method, body=body),
                   deadline=self._deadline, retries=self._retries,
                   backoff=self._backoff, sleep=self._slept)

    def _graphql_url(self) -> str:
        """`{api_url}/graphql` — and for a GitHub Enterprise Server, whose REST
        root is `…/api/v3`, its sibling `…/api/graphql` (ADR-0008, decision 3)."""
        if self._api.endswith("/api/v3"):
            return f"{self._api[:-len('/v3')]}/graphql"
        return f"{self._api}/graphql"

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        """One GraphQL query, answered as the JSON body GitHub sent — `data` and
        `errors` both, because a `200` carries either or both and the caller
        reads them the way ADR-0002 reads a status (ADR-0008, decision 4).

        A `POST` through the same `_attempt` every REST read takes: the headers,
        the deadline, the retry, the throttle reading and the ledger's feed are
        the client's and not the dialect's, which is why this is a method here and
        not a second client (ADR-0008, decision 3; ADR-0001's own argument).
        """
        payload = json.dumps({"query": query,
                              "variables": variables or {}}).encode("utf-8")
        data, _ = self._ask(self._graphql_url(), method="POST", body=payload)
        return data

    def _slept(self, wait: float) -> None:
        """`ask`'s sleep, counted and said out loud.

        `ask` logs its wait at *info*, which is off in most deployments — and a
        minute of silence with no visible line reads exactly like a hang. So the
        wait is logged **here**, at warning, with what is left of the run beside
        it, and added to :attr:`paused_seconds` so the check can put it on the
        node afterwards.
        """
        asked = self._asked_to_wait
        self._asked_to_wait = False
        if (self._max_pause is not None
                and self.paused_seconds + wait > self._max_pause):
            # Refused whole rather than trimmed to what is left: a wait shorter
            # than the one asked for does not satisfy the service, so taking it
            # would spend the rest of the budget and still be throttled.
            raise _PauseBudgetSpent(wait)
        left = (f"{self._deadline.remaining():.0f}s of the run left"
                if self._deadline is not None else "no run deadline")
        logger.warning("paused %.0fs before retrying (%s; %s)", wait,
                       "GitHub asked" if asked
                       else "our backoff after a failure GitHub did not explain",
                       left)
        if asked:
            self.throttled_seconds += wait
        else:
            self.retried_seconds += wait
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

    #: The per-repository endpoint each aspect reads, for the pre-run guard — and,
    #: less its leading slash, the reduced form the ledger keeps a path in
    #: (`budget.reduce_path`), which is how the guard asks which window this
    #: token's reads of an endpoint were charged to. **Two aspects share one**: the
    #: code-scanning split partitions a single `/code-scanning/alerts` payload, so
    #: counting aspects would claim a request per repository that the run never
    #: makes — and the guard would grow more cautious on a number that is not true.
    #:
    #: **`actions` names the workflow list**, the read 0.1.6 made the aspect's
    #: first; the per-workflow runs it then reads reduce to the same path. The
    #: guard prices it as one plus the workflows each repository had the last
    #: time the aspect read its list, times the branches it names (`1 + W × B`,
    #: `_workflow_counts` and `actions.branches`) — exact after the first run, a
    #: floor before it — and the aspect still prices the per-workflow read itself
    #: against the headers in hand (`_budget_covers`), degrading that repository
    #: to the wide read rather than walking through the guard.
    #:
    #: **`sbom_check` is the one aspect that reads through GraphQL**: one query
    #: per repository, and the ledger keeps that on the path `graphql` under the
    #: resource of the same name — so the guard prices it as its queries' points
    #: there (ADR-0008, decisions 1 and 5).
    ASPECT_ENDPOINT: ClassVar[dict[str, str]] = {
        "pull_requests": "/pulls",
        "security_advisories": "/dependabot/alerts",
        "code_scanning_security": "/code-scanning/alerts",
        "code_scanning_quality": "/code-scanning/alerts",
        "secret_scanning_alerts": "/secret-scanning/alerts",
        "sbom_check": "/graphql",
        "actions": "/actions/workflows",
        "issues": "/issues",
    }

    #: `sbom_check` asks the dependency graph, not the SBOM export (ADR-0008,
    #: decision 1): this many repositories per query, and this many manifests
    #: read per repository, which is where the cause a red line names comes
    #: from; a graph with more than that many has content whatever the first ten
    #: say. **One repository a query, because GitHub ends a GraphQL request at
    #: ten seconds and a query costs one point whatever it carries.** Measured
    #: live: nineteen repositories in one query were cut in twenty of
    #: twenty-nine runs, fourteen of them on the retry too; ten in one still
    #: took four to ten seconds, one chunk per organization twice the other,
    #: because a few heavy repositories dominate whichever chunk holds them.
    #: Alone, no query is slower than its own repository, a cut costs exactly
    #: that repository's line, and there is no chunk size left to move. The
    #: query builder still aliases, so a reader who ever wants more than one
    #: per query changes this number and nothing else.
    _GRAPH_REPOS_PER_QUERY: ClassVar[int] = 1
    _GRAPH_MANIFESTS: ClassVar[int] = 10
    #: What one such query costs on the `graphql` budget: **one point**, measured
    #: — `1 used` on every answer — and what GitHub's formula gives it: the
    #: requests the query stands for (a repository lookup and a manifest
    #: connection), divided by a hundred and rounded, is nothing, and the minimum
    #: is one. The guard prices the aspect at this per query (ADR-0008,
    #: decision 5): a point a repository, some eighty an hour on a budget of
    #: five thousand for a scope of nineteen.
    _GRAPH_QUERY_POINTS: ClassVar[int] = 1

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
                 actions_branches: tuple[str, ...] = (),
                 actions_disabled_map: dict[str, StatusCode] | None = None,
                 actions_show_healthy: bool = False,
                 issues_ignore: tuple[str, ...] = (),
                 disabled_aspects: tuple[str, ...] = (),
                 token_ref: str, **kwargs: Any) -> None:
        # The two declarations this type makes about its subnodes, computed
        # **before** the base constructor because that is where little-sister
        # resolves them against the deployment's `subnodes:` block (its
        # ADR-0025). They are functions of the arguments rather than of
        # `self` for the same reason — and can be, because every value in them is
        # a load-time fact: `kind` is declared, and the severity maps are the
        # defaults with the config's overrides on top, exactly as the attributes
        # below take them.
        advisories = {**DEFAULT_ADVISORY_SEVERITY_MAP,
                      **(advisory_severity_map or {})}
        scanning_security = {**DEFAULT_CODE_SCANNING_SECURITY_MAP,
                             **(code_scanning_security_map or {})}
        scanning_quality = {**DEFAULT_CODE_SCANNING_QUALITY_MAP,
                            **(code_scanning_quality_map or {})}
        disabled_map = {**DEFAULT_ACTIONS_DISABLED_MAP,
                        **(actions_disabled_map or {})}
        super().__init__(
            subnode_defaults=SUBNODES,
            label_tokens=_subnode_tokens(
                owner=owner, team=team, is_org=kind == "organization",
                advisory_severity_map=advisories,
                dependabot_severities=dependabot_severities,
                code_scanning_security_map=scanning_security,
                code_scanning_quality_map=scanning_quality),
            **kwargs)
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
        self.advisory_severity_map = advisories
        self.code_scanning_security_map = scanning_security
        self.code_scanning_quality_map = scanning_quality
        self.secret_scanning_require_enabled = secret_scanning_require_enabled
        self.sbom_ignore = sbom_ignore
        self.actions_ignore_patterns = actions_ignore_patterns
        self.actions_all_branches = actions_all_branches
        #: The branches `actions` asks about by name, in the order the config gave
        #: them, or empty for the default-branch mode (ADR-0009).
        self.actions_branches = actions_branches
        #: What each disabled workflow state grades — the shipped map with the
        #: deployment's own entries on top (ADR-0010).
        self.actions_disabled_map = disabled_map
        if actions_all_branches and actions_branches:
            # **A refusal, not a precedence rule.** The two keys ask different
            # questions — one watches every branch and says where it is incomplete,
            # the other asks exactly the branches it is given and is exact — so
            # picking a winner would silently answer a question this config did not
            # ask, and the deployment would read the mode it did not get off the
            # leaf. Here it is a startup error naming both keys instead.
            raise CheckError(
                "github 'actions.all_branches' and 'actions.branches' ask two "
                "different questions and cannot both be set: 'all_branches' "
                "watches every branch and reports where that answer is short, "
                "'branches' asks exactly the branches you name and is exact. "
                "Keep one of them")
        self.actions_show_healthy = actions_show_healthy
        self.issues_ignore = issues_ignore
        # Aspects this check does **not** run, by name. Stored as what is switched
        # off rather than what is on, so an aspect a later release adds is on by
        # default in every config written before it existed — the opposite of what
        # an allow-list would do, which is to exclude it silently.
        self.disabled_aspects = frozenset(disabled_aspects)
        # What kind of account `org` names, filled by `_discover` from GitHub
        # itself. `None` means "not asked yet": every reader of it runs during a
        # run, after discovery, and the organization answer is the one that keeps
        # a standalone caller rendering what it rendered before.
        # Whether discovery can see this account's private repositories. True
        # until a user account's token says otherwise (`_discover`): an
        # organization listing made with a member's token is whole, but
        # `/users/{login}/repos` is public-only however privileged the token is.
        self._sees_private = True
        # Where the next run's roster starts: the last aspect that **finished**, or
        # `None` before the first run (ADR-0002 §7). Run state
        # like the two below, and deliberately in memory only — a restart beginning
        # at the head again costs one cycle, and the engine never runs one check
        # twice at once, so a plain attribute is the whole of it.
        self._resume_after: str | None = None
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
        #: How many workflows the `actions` aspect watched in each repository the
        #: last time it read that repository's list, by full name — what the guard
        #: prices the per-workflow read from (`1 + W`). Run state that outlives the
        #: run on purpose: the count is free to remember, and a guard that priced
        #: the aspect as one read per repository grew looser with every workflow
        #: added. Unknown before the first run, which prices the floor.
        self._workflow_counts: dict[str, int] = {}
        #: This check's conditional-request cache (ADR-0011). Beside
        #: `_workflow_counts` because it is the same kind of memory: across runs,
        #: in this process only, and belonging to this check's one credential. A
        #: cache on the client would be empty every run — `_make_client` builds a
        #: fresh one inside `run` — and so would buy nothing at all.
        self._conditional = _ConditionalCache()
        #: What the pre-run guard priced this run at, for the report line that puts
        #: it beside what the run actually spent. The guard is **not** changed to
        #: account for conditional requests in this slice: it would be tuned
        #: against a hit rate nobody has measured, and this line is what makes that
        #: rate measurable (this package's backlog #6).
        self._priced_total = 0
        #: How long the last run that ran took, in seconds, or `None` before one
        #: has — the length the guard multiplies a window's foreign rate by, since
        #: a budget somebody else is spending is smaller by the end of this run
        #: than it reads at the start. A skipped run spent nothing and sets nothing.
        self._last_run_seconds: float | None = None

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
            security.get("severity_map"), "security_advisories.severity_map")
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
            security_scanning.get("severity_map"),
            "code_scanning_security.severity_map")
        quality_scanning = _block(config, "code_scanning_quality")
        code_scanning_quality_map = _severity_map(
            quality_scanning.get("severity_map"),
            "code_scanning_quality.severity_map")
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
        # **Named branches** (ADR-0009). The list *replaces* the default branch
        # rather than adding to it: the key says "ask about these", and resolving
        # each repository's own default branch into a list the deployment believes
        # it controls would make `main` against `master` this check's problem
        # instead of the config's. A repeated name is dropped rather than refused —
        # the intent is not ambiguous, and keeping it would buy an identical second
        # read of every workflow.
        actions_disabled_map = _severity_map(
            actions.get("disabled_severity_map"),
            "actions.disabled_severity_map")
        branches = actions.get("branches") or []
        if not isinstance(branches, list):
            raise CheckError("actions.branches must be a list")
        named_branches: list[str] = []
        for branch in branches:
            name = str(branch).strip()
            if not name:
                # Not skipped: an empty entry is a deployment that meant to name a
                # branch, and silently asking about one branch fewer than the config
                # lists is how a check answers a question nobody asked.
                raise CheckError(
                    "actions.branches: a branch name cannot be empty")
            if name not in named_branches:
                named_branches.append(name)
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
        # The block's **shape** is little-sister's to validate, and it already
        # has: `_parse_common` reads `subnodes:` for every check type now, and
        # resolves the labels against what this type declares (its
        # ADR-0025). What is left here is what only this type knows —
        # which names it answers to, and which token it retired.
        block = config.get("subnodes")
        for raw_name, texts in (block.items() if isinstance(block, dict) else ()):
            aspect = str(raw_name)
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
            for field_name, text in (texts.items()
                                     if isinstance(texts, dict) else ()):
                if "{org}" in str(text):
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
            "actions_branches": tuple(named_branches),
            "actions_disabled_map": actions_disabled_map,
            "actions_show_healthy": bool(actions.get("show_healthy", False)),
            "issues_ignore": tuple(str(r) for r in issues_ignore),
            "disabled_aspects": disabled,
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
            # Only when the default-branch mode was replaced: the branches a check
            # asks about are what decides whether an empty Actions leaf is an
            # estate with nothing to say or a list that matches no repository, and
            # that is the first thing to look at when the unmatched line appears.
            "Actions branches": (", ".join(self.actions_branches)
                                 if self.actions_branches else None),
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

    def run_order(self) -> tuple[str, ...]:
        """The roster this run walks: :meth:`active_aspects` rotated to start after
        the last aspect that **finished** (ADR-0002 §7).

        A run that never fits refreshed the same head and starved the same tail
        forever, while those nodes kept their last reading and looked answered. So
        the next run resumes where the last one got to: a run cut short after four
        of eight starts at the fifth, and every aspect is read once per cycle rather
        than the first four every time.

        The resume point is a **name**, not an index, so a config that switches an
        aspect off between runs shifts nothing; a name no longer in the roster starts
        the run at the head, which is what a roster that changed under us deserves.
        And it is only the *run* order: the row's order is `aspect_rank`, read from
        `ASPECTS` (little-sister ADR-0055), so nothing on the dashboard moves.
        """
        roster = self.active_aspects()
        if not roster or self._resume_after is None:
            return roster
        try:
            start = (roster.index(self._resume_after) + 1) % len(roster)
        except ValueError:
            return roster
        return roster[start:] + roster[:start]

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
        return GitHubClient(token, api_url=self.api_url, cache=self._conditional,
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
        return CheckResult(
            reason=(*entries, *coverage.lines()),
            entries=True,
            name=name, description=description, report=report)

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
        `UNDEFINED` — which the tree ignores in favor of the bands beneath it,
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
        return CheckResult(
            reason=coverage.lines(),
            entries=True,
            name=name,
            description=description,
            children=tuple(children),
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
        nothing ever wrote them (ADR-0006).

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

    @classmethod
    def _graph_query(cls, repos: list[Repo]) -> str:
        """The aliased query for one chunk of repositories: `r0`, `r1`, … in the
        chunk's order, each asking one repository for the count of its dependency
        manifests and the first ten of them (ADR-0008, decision 1). Owner and
        name are JSON-quoted, which is GraphQL's string syntax too."""
        fields = (f"dependencyGraphManifests(first: {cls._GRAPH_MANIFESTS}) "
                  "{ totalCount nodes { filename parseable exceedsMaxSize } }")
        parts = []
        for position, repo in enumerate(repos):
            owner, _, name = repo.full_name.partition("/")
            parts.append(f"r{position}: repository(owner: {json.dumps(owner)}, "
                         f"name: {json.dumps(name)}) {{ {fields} }}")
        return "query { " + " ".join(parts) + " }"

    @staticmethod
    def _graph_errors(answer: object) -> tuple[dict[str, tuple[str, str]],
                                               list[str]]:
        """The `errors` of a GraphQL answer, split by what they are about: those
        whose `path` names one alias, as ``{alias: (type, message)}``, and the
        rest, which are about the query as a whole, as their messages."""
        by_alias: dict[str, tuple[str, str]] = {}
        whole: list[str] = []
        for error in (values.rows(answer, "errors", where="graphql")
                      if isinstance(answer, dict) else []):
            kind = values.text(error, "type")
            message = values.text(error, "message")
            path = error.get("path") if isinstance(error, dict) else None
            alias = (path[0] if isinstance(path, list) and path
                     and isinstance(path[0], str) else None)
            if alias is not None:
                by_alias.setdefault(alias, (kind, message))
            elif kind or message:
                whole.append(f"{kind}: {message}" if kind else message)
        return by_alias, whole

    def _graph_verdict(self, repo: Repo, total: int,
                       manifests: list[Any]) -> Entry | None:
        """One repository's line, or nothing (ADR-0008, decision 2).

        No manifests is no dependency graph and grades **ERROR**, as a `404` and
        an empty SBOM did. Manifests that exist but none of them parseable grade
        **ERROR** too, and the line names the cause — Dependabot cannot alert from
        a manifest it could not parse, which is why this aspect is red at all
        (ADR-0004 §4). More manifests than were read is a graph with content,
        whatever the first ten say. At most one line per repository, so the
        repository and the aspect are the whole identity: the slug `sbom` a pin
        was held against before this record holds (decision 6).
        """
        name = plain(repo.name)
        network = f"https://github.com/{repo.full_name}/network/dependencies"
        if total <= 0:
            return Entry(_entry_slug(repo, "sbom"),
                         _link(f"{name}: no dependency graph (0 manifests)",
                               network),
                         code=StatusCode.ERROR)
        if total > self._GRAPH_MANIFESTS or not manifests:
            return None
        if any(values.flag(manifest, "parseable") for manifest in manifests):
            return None
        causes = "; ".join(
            plain(values.text(manifest, "filename", default="a manifest"))
            + (" exceeds the size limit"
               if values.flag(manifest, "exceedsMaxSize")
               else " could not be parsed")
            for manifest in manifests)
        count = (f"{total} manifests, none parseable" if total != 1
                 else "1 manifest, not parseable")
        return Entry(_entry_slug(repo, "sbom"),
                     _link(f"{name}: {count} ({causes})", network),
                     code=StatusCode.ERROR)

    def _read_graph_answer(self, chunk: list[Repo], answer: object,
                           entries: list[Entry], coverage: _Coverage) -> None:
        """One query's answer, read as ADR-0002 reads a REST one (ADR-0008,
        decision 4): a `200` whose `errors` name one alias is about that
        repository alone; a `200` with `errors` and no `data` is the query failing
        whole, which is *could not ask* for every repository it carried; an
        answer with neither is one this check cannot read."""
        try:
            by_alias, whole = self._graph_errors(answer)
        except CheckError as error:
            unreadable = GitHubError(
                f"GitHub answered the dependency-graph query with something "
                f"this check cannot read ({plain(str(error))})",
                fault=Fault.MALFORMED)
            for repo in chunk:
                coverage.failed(repo, unreadable)
            return
        data = answer.get("data") if isinstance(answer, dict) else None
        if not isinstance(data, dict):
            if whole or by_alias:
                failed = GitHubError(
                    "GitHub answered the dependency-graph query with errors and "
                    "no data (" + ("; ".join(whole) or "per-repository errors "
                                                        "only") + ")",
                    fault=Fault.TRANSIENT)
            else:
                failed = GitHubError(
                    "GitHub answered the dependency-graph query with neither "
                    "data nor errors", fault=Fault.MALFORMED)
            for repo in chunk:
                coverage.failed(repo, failed)
            return
        for position, repo in enumerate(chunk):
            alias = f"r{position}"
            if alias in by_alias:
                kind, message = by_alias[alias]
                if kind == "NOT_FOUND":
                    coverage.gone(repo, f"not found — gone since discovery "
                                        f"({plain(message)})")
                elif kind in ("FORBIDDEN", "INSUFFICIENT_SCOPES"):
                    # The token being told no about this repository: an answer,
                    # and it grades, as a 403 on the export did.
                    coverage.failed(repo, GitHubError(
                        f"{kind}: {message}", status=403, fault=Fault.ANSWERED))
                else:
                    coverage.failed(repo, GitHubError(
                        f"{kind or 'error'}: {message}", fault=Fault.MALFORMED))
                continue
            node = data.get(alias)
            try:
                if not isinstance(node, dict):
                    raise CheckError(f"{repo.full_name}: no repository object "
                                     f"under {alias} in the answer")
                total = values.number(node, "dependencyGraphManifests",
                                      "totalCount", where=repo.full_name,
                                      required=True)
                manifests = values.rows(node, "dependencyGraphManifests",
                                        "nodes", where=repo.full_name)
            except CheckError as error:
                coverage.failed(repo, GitHubError(plain(str(error)),
                                                  fault=Fault.MALFORMED))
                continue
            coverage.read_one()
            line = self._graph_verdict(repo, total, manifests)
            if line is not None:
                entries.append(line)

    def _sbom_check(self, client: GitHubClient,
                    repos: list[Repo]) -> CheckResult:
        """A repository with code but no dependency graph Dependabot can read →
        ERROR (ADR-0004 §4), asked of the graph itself rather than read off an
        SBOM export (ADR-0008): one GraphQL query per repository. Repositories in
        ``sbom_ignore`` are not asked. A query that fails
        as a request — a 5xx, a throttle, a 401 — is one fault for every
        repository it carried (ADR-0002); what a `200` says is read per alias."""
        entries: list[Entry] = []
        coverage = _Coverage()
        asked = [repo for repo in repos if repo.name not in self.sbom_ignore]
        for start in range(0, len(asked), self._GRAPH_REPOS_PER_QUERY):
            chunk = asked[start:start + self._GRAPH_REPOS_PER_QUERY]
            try:
                answer = client.graphql(self._graph_query(chunk))
            except GitHubError as error:
                for repo in chunk:
                    coverage.failed(repo, error)
                continue
            self._read_graph_answer(chunk, answer, entries, coverage)
        return self._finalize(
            "sbom_check",
            "Repositories without a dependency graph Dependabot can read",
            entries, coverage)

    #: Workflow-run conclusions that count as a failure.
    _ACTIONS_FAIL = ("failure", "timed_out", "startup_failure")
    #: Empty-conclusion states that positively mean work is in flight. Unknown
    #: states do not default to running (little-sister ADR-0032 rule 7).
    _ACTIONS_RUNNING = ("queued", "in_progress", "pending", "requested")

    @classmethod
    def _action_verdict(cls, run: object) -> tuple[StatusCode, str] | None:
        """The completed verdict a run contributes, or none when it contributes
        only an in-flight/neutral fact. Canceled and skipped runs deliberately do
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

    @staticmethod
    def _run_record(run: object) -> dict[str, Any]:
        """One run, as the fields a surface renders or grades on rather than as the
        sentence they were folded into (little-sister ADR-0082).

        ``started`` is one of the three names the library reads as an **instant**,
        and GitHub's own field says exactly that, so it is safe to claim.
        ``updated`` deliberately is **not** ``ended``: a workflow run carries no
        completion time at all, and ``updated_at`` is when anything about it last
        changed — near enough to be worth keeping, not near enough to publish under
        a name that means *finished*. An empty field becomes ``None`` rather than
        ``""``: the library refuses a timed name whose value is not a time, and an
        absent time is a fact of its own.
        """
        return {
            "run_number": values.number(run, "run_number") or None,
            "url": values.text(run, "html_url") or None,
            "status": values.text(run, "status") or None,
            "conclusion": values.text(run, "conclusion") or None,
            "started": values.text(run, "run_started_at") or None,
            "updated": values.text(run, "updated_at") or None,
        }

    @classmethod
    def _action_record(cls, repo: Repo, workflow: str, branch: str,
                       completed: object | None, running: object | None,
                       verdict: tuple[StatusCode, str] | None) -> dict[str, Any]:
        """What this run read about one workflow on one branch.

        **Two words for the outcome, and they are not the same field.**
        ``conclusion`` is GitHub's own — ``success``, ``failure``,
        ``action_required`` — and it is what a grading seam will map; ``verdict`` is
        the word this check chose for the line, which is what a line template will
        substitute. A template cannot compute *failed* from *failure*, and a grading
        map must not read a word we invented, so both ride and each says which job
        it has.

        The names a human reads are **raw** — escaping is a render-time step
        (little-sister ADR-0018), and this is the half of the change that lets the
        check stop escaping them itself.
        """
        record: dict[str, Any] = {
            "repository": repo.full_name,
            "workflow": workflow,
            "branch": branch,
            "verdict": verdict[1] if verdict is not None else None,
        }
        if completed is not None:
            record["completed"] = cls._run_record(completed)
        if running is not None:
            record["running"] = cls._run_record(running)
        return record

    #: How each disabled state reads on the line. GitHub's own wording, because an
    #: operator who wants to turn the workflow back on will meet these words in the
    #: Actions tab and nowhere else.
    _DISABLED_WORDS: ClassVar[dict[str, str]] = {
        "disabled_manually": "disabled manually",
        "disabled_inactivity": "disabled by GitHub after 60 days without "
                               "repository activity",
        "disabled_fork": "disabled by GitHub on this fork",
    }

    @classmethod
    def _disabled_text(cls, repo: Repo, workflow: _Workflow) -> str:
        """One disabled workflow's line: where it is, and **why it is off**.

        The cause is the whole value of the line — *somebody switched this off* and
        *GitHub switched this off* are different facts with different answers — and a
        state this package has not met is said as GitHub spelled it rather than
        flattened to `disabled`, so a new state arrives as a word to look up instead
        of as silence.
        """
        where = f"{plain(repo.name)} / {plain(workflow.name)}"
        word = cls._DISABLED_WORDS.get(
            workflow.state, f"disabled ({plain(workflow.state)})")
        return f"{_link(where, workflow.url)}: {word}, no runs read"

    #: Rows asked for per workflow. The scan needs the newest in-flight run **and**
    #: the newest useful completed verdict, which one row cannot carry and which a
    #: `status=` filter would need two reads to get — so it asks for a few and finds
    #: both in one. Ten covers "running now, canceled before that, green before
    #: that" with room; a workflow whose ten newest runs are all neutral reports no
    #: verdict, which is the same answer the one-page read gave and is rare enough
    #: to leave until it is seen.
    _ACTIONS_WORKFLOW_ROWS = 10

    def _actions(self, client: GitHubClient,
                 repos: list[Repo]) -> CheckResult:
        """One coded entry per workflow/branch that has something to say.

        The entry code is the newest useful completed verdict. A newer in-flight
        run is an additional flag and words on that same stable entry, so a retry
        cannot hide the failure it is trying to fix. Healthy idle workflows are
        optional; a run in flight is always emitted. Only runs of a currently
        existing workflow count.

        **It asks per workflow, and that is what makes it exact.**
        `/actions/workflows/<id>/runs?branch=` cannot go blind: an empty answer means
        this workflow does not run on this branch, which is a fact rather than a gap.
        The repository-wide `/actions/runs` it replaced returned the newest 100 runs
        across *all* workflows, so a workflow whose newest run fell below that cut
        contributed nothing and — with `show_healthy: false` — rendered exactly as
        one that passed. Nothing could tell those apart from that read, which is why
        the aspect briefly reported which workflows it had missed and was wrong to.

        Two places still use the wide read, and both say so on the leaf:
        `all_branches`, where "newest per (workflow, branch)" is unbounded and one
        page is the only bounded question there is; and a repository whose remaining
        budget will not cover a read per workflow, which degrades to it rather than
        reporting nothing.

        **`actions.branches` is the third mode** (ADR-0009): the named list replaces
        the default branch and every watched workflow is asked about every name,
        which keeps the construction above — nothing back means no run on that
        branch — at `W × B` reads. Where the budget will not cover that, the same
        degradation applies, and with more than one name the wide page is taken
        unfiltered and cut to the named branches, which is a cut by construction and
        is reported as one.
        """
        problem_entries: list[Entry] = []
        running_entries: list[Entry] = []
        healthy_entries: list[Entry] = []
        # Repositories this aspect answered about only partly. Keyed by id rather
        # than guarded on the way in: a repository can be short more than one way,
        # the line says *this answer is incomplete here* and not how many ways it
        # is, and a pair of `not in` checks leaves one of them dead.
        partial: dict[int, Repo] = {}
        # Repositories where the branches this config names matched nothing at all.
        # Only ever filled from an **exact** read, for the reason stated where it
        # is filled: a degraded page cannot tell an unmatched branch from a cut.
        unmatched: dict[int, Repo] = {}
        coverage = _Coverage()
        for repo in repos:
            full = repo.full_name
            try:
                workflows = self._existing_workflows(client, full)
            except GitHubError as error:
                if error.status == 404:
                    coverage.read_one()
                    self._workflow_counts[full] = 0   # next run: the list alone
                    continue                        # Actions not enabled
                coverage.failed(repo, error, "workflows-unreadable", "workflows")
                continue
            if workflows.partial:
                partial[repo.id] = repo
            # Filtered **before** the reads, not after: an ignored workflow should
            # not cost a request either. The one-page read could not do this — it
            # asked for runs, not for a workflow.
            kept = {
                workflow_id: workflow
                for workflow_id, workflow in workflows.by_id.items()
                if not any(p.search(workflow.name or str(workflow_id))
                           for p in self.actions_ignore_patterns)}
            # **A disabled workflow's runs are not read** (ADR-0010). Its newest run
            # is frozen at whatever it was when somebody switched the workflow off,
            # so the request buys a verdict that cannot change and says nothing the
            # list already in hand does not. It gets a line of its own below, naming
            # the state, and the saved read is one per disabled workflow per run.
            disabled = {workflow_id: workflow
                        for workflow_id, workflow in kept.items()
                        if workflow.disabled}
            watched = {workflow_id: workflow.name
                       for workflow_id, workflow in kept.items()
                       if not workflow.disabled}
            # What the next run's guard prices this repository's per-workflow
            # read at: the workflows whose runs will actually be read — after the
            # ignore patterns, because an ignored workflow costs no request, and
            # after the disabled ones, because a disabled workflow costs none
            # either. Counting `kept` here would over-price every run after this
            # change by the number of workflows it stopped reading.
            self._workflow_counts[full] = len(watched)
            # Which branches this repository is asked about, and the whole of the
            # difference between the three modes. `all_branches` names none and
            # reads the wide page; a configured list replaces the default branch
            # (ADR-0009); and a repository GitHub named no default branch for falls
            # to the wide read exactly as it did before there was a list.
            if self.actions_all_branches:
                asked: tuple[str, ...] = ()
            elif self.actions_branches:
                asked = self.actions_branches
            else:
                asked = (repo.default_branch,) if repo.default_branch else ()
            try:
                if asked and self._budget_covers(
                        client, len(watched) * len(asked)):
                    runs = self._workflow_runs(client, repo, watched, asked)
                    # **Only an exact read may claim this.** Nothing back from a
                    # read that asked per workflow per named branch means no run on
                    # any of them — the same construction ADR-0005 rests on. The
                    # degraded page below cannot say it: no row on a named branch
                    # there is as likely to be the cut as the branch.
                    if self.actions_branches and watched and not runs:
                        unmatched[repo.id] = repo
                else:
                    # The wide read filters **one** branch, and a named list may
                    # hold several: with one it asks GitHub for it, with more it
                    # takes the page unfiltered and keeps the rows on a named
                    # branch — which makes that page a cut by construction, so the
                    # repository is short whatever `total_count` says about it.
                    one = asked[0] if len(asked) == 1 else ""
                    rows, window_total = self._repository_runs(client, repo, one)
                    if window_total > len(rows):
                        partial[repo.id] = repo
                    if self.actions_branches and not one:
                        rows = [row for row in rows
                                if values.text(row, "head_branch")
                                in self.actions_branches]
                        partial[repo.id] = repo
                    runs = rows
            except GitHubError as error:
                if error.status == 404:
                    coverage.read_one()
                    continue                        # Actions not enabled
                coverage.failed(repo, error, "runs-unreadable")
                continue
            coverage.read_one()
            for workflow_id, switched_off in sorted(disabled.items()):
                code = self.actions_disabled_map.get(switched_off.state,
                                                     StatusCode.WARN)
                # An `OK` disabled line follows `show_healthy`, exactly as a
                # passing idle workflow does (ADR-0004 §9): with `disabled_fork`
                # graded `OK` by default and forks discovered by default, a leaf
                # that showed them all would fill with green lines nobody chose.
                if code is StatusCode.OK and not self.actions_show_healthy:
                    continue
                # **Keyed without a branch**, because this line is not about one:
                # the workflow is off everywhere. That is a different slug from the
                # frozen verdict this line replaces, so a pin held on that line
                # stops matching and has to be made again (PL10, ADR-0010).
                entry = Entry(
                    slug(repo.id, "workflow", workflow_id),
                    self._disabled_text(repo, switched_off),
                    code=code,
                    # The repository is what this line is **about**, as the id a
                    # rename cannot change — the same value the slug is keyed on
                    # (ADR-0004, little-sister ADR-0050). The name a human reads
                    # rides the record instead.
                    subject=str(repo.id),
                    # `state` is GitHub's own word and is free-form here: the
                    # library types three names and this is not one of them.
                    data={"repository": repo.full_name,
                          "workflow": switched_off.name,
                          "url": switched_off.url,
                          "state": switched_off.state})
                if code in (StatusCode.ERROR, StatusCode.WARN):
                    problem_entries.append(entry)
                else:
                    healthy_entries.append(entry)
            for (workflow_id, branch), state in self._run_states(
                    runs, watched).items():
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
                    subject=str(repo.id),
                    data=self._action_record(
                        repo, workflow, branch, completed, running, verdict),
                )
                if entry_code in (StatusCode.ERROR, StatusCode.WARN):
                    problem_entries.append(entry)
                elif running is not None:
                    running_entries.append(entry)
                else:
                    healthy_entries.append(entry)
        # **One line for the whole leaf, and it grades.** Not one per repository and
        # emphatically not one per workflow: what it reports is the same fact
        # everywhere it is true — this answer is short — and repeating a fact an
        # operator cannot act on differently is how a leaf teaches its reader to
        # skip the color. It is `WARN` rather than `UNDEFINED` because a leaf that
        # *knows* it is incomplete and renders green is the defect this whole item
        # exists to remove; `UNDEFINED` is for a repository GitHub would not answer
        # about, which is a wait-and-see, and this is not one.
        window_entries: list[Entry] = []
        if partial:
            window_entries.append(Entry(
                "runs-window-partial",
                f"not all runs read in {len(partial)} of {len(repos)} "
                + ("repository" if len(repos) == 1 else "repositories")
                + " — a workflow whose newest run falls outside the window has no "
                "state here and is not reported above: "
                + ", ".join(plain(repo.name)
                            for repo in partial.values()),
                code=StatusCode.WARN))
        # **The branches this config names matched nothing here.** A configuration
        # line rather than a coverage line, and the only thing the per-entry
        # rendering cannot show: a branch that produced no entry produces no line
        # either, so an estate asked about a branch none of its repositories runs
        # renders exactly like one with nothing to report. It names the default
        # branch it *did* see, because `main` against `master` is what this nearly
        # always is, and it says *no run on* rather than *no such branch*, which is
        # the half of it the read cannot support (ADR-0009).
        branch_entries: list[Entry] = []
        if unmatched:
            named = ", ".join(plain(branch) for branch in self.actions_branches)
            branch_entries.append(Entry(
                "branches-unmatched",
                f"no workflow run on any branch this check names ({named}) in "
                f"{len(unmatched)} of {len(repos)} "
                + ("repository" if len(repos) == 1 else "repositories")
                + " — the branch may not exist there, or nothing has run on it "
                "yet: "
                + ", ".join(
                    f"{plain(repo.name)} (default branch "
                    + (plain(repo.default_branch) if repo.default_branch
                       else "not named by GitHub") + ")"
                    for repo in unmatched.values()),
                code=StatusCode.WARN))
        # The unreadable lines go **last**, after the healthy ones: they are the
        # least actionable thing on the leaf, and one of them is not news. The
        # window line sits with them and above them, for the same reason in the
        # other direction: it is about this check's own reach rather than about
        # anybody's repository, but unlike them it is a standing defect.
        return self._finalize(
            "actions", "Latest completed and in-flight workflow-run state",
            [*problem_entries, *running_entries, *healthy_entries,
             *branch_entries, *window_entries], coverage)

    def _budget_covers(self, client: GitHubClient, reads: int) -> bool:
        """Whether the budget GitHub last stated covers this repository's exact
        read — one per watched workflow **per named branch** (ADR-0009), which is
        one per workflow in the default-branch mode.

        **Read from the headers of the response already in hand**, which cost
        nothing: `/rate_limit` would spend a request to ask a question this run has
        been told the answer to on every response it has had (the budget-header
        trace, 0.1.3). It is also the more truthful of the two — the headers report
        the bucket *the requests we are about to make* will be charged to.

        This is where the per-workflow cost is priced, and it has to be here rather
        than in the pre-run guard: that one runs before any aspect and multiplies
        repositories by endpoints, while the number of workflows in a repository is
        not known until this aspect has read its list. The guard stays a floor; this
        keeps the aspect from walking through it.

        No headers at all means **yes**. A path to GitHub that strips them is not a
        reason to degrade every repository behind it to a worse read, and the
        pre-run guard has already had its say.
        """
        stated = client.last_rate_limit
        if stated is None or stated.remaining is None:
            return True
        return stated.remaining >= self.rate_limit_safety_factor * reads

    def _repository_runs(self, client: GitHubClient, repo: Repo,
                         branch: str) -> tuple[list[Any], int]:
        """One page of the repository's runs, and what GitHub says it is a page *of*.

        The bounded question, and the only one `all_branches` has: the newest 100
        runs across every workflow. ``total_count`` above the rows returned is the
        page being a cut of the real answer — the aspect cannot say which workflow
        it then has no state for, only that it has none for somebody.
        """
        params: dict[str, Any] = {"per_page": 100}
        if branch:
            params["branch"] = branch
        data = client.get(f"/repos/{repo.full_name}/actions/runs", params)
        return (values.rows(data, "workflow_runs"),
                values.number(data, "total_count"))

    def _workflow_runs(self, client: GitHubClient, repo: Repo,
                       watched: dict[int, str],
                       branches: tuple[str, ...]) -> list[Any]:
        """The newest rows of each watched workflow on each of ``branches``, as one
        list.

        Returned in the shape the repository-wide read returns so that one scan
        serves both. A workflow with no rows contributes none and is **not** an
        omission: it does not run on that branch, and that is the whole difference
        between this read and the one it replaced.

        The cost is one read per pair, `W × B`, and both the pre-run guard and
        :meth:`_budget_covers` price it that way — a branch count left out of either
        is a guard that under-prices by exactly the factor this loop multiplies by.
        The order is workflow-major and does not matter: :meth:`_run_states` keys
        every row by `(workflow, branch)` and GitHub returns each read newest first.
        """
        rows: list[Any] = []
        for workflow_id in sorted(watched):
            for branch in branches:
                data = client.get(
                    f"/repos/{repo.full_name}/actions/workflows/"
                    f"{workflow_id}/runs",
                    {"per_page": self._ACTIONS_WORKFLOW_ROWS, "branch": branch})
                rows.extend(values.rows(data, "workflow_runs"))
        return rows

    def _run_states(self, runs: list[Any],
                    watched: dict[int, str]) -> dict[
                        tuple[int, str],
                        tuple[str, object | None, object | None,
                              tuple[StatusCode, str] | None]]:
        """The newest in-flight run and the newest useful completed verdict, kept
        **independently**, for each stable (workflow, branch) identity.

        GitHub returns newest first, and both reads preserve that. The old one-set
        loop let the in-flight run claim the key and made the verdict disappear, so
        a retry hid the failure it was trying to fix.
        """
        states: dict[
            tuple[int, str],
            tuple[str, object | None, object | None,
                  tuple[StatusCode, str] | None],
        ] = {}
        for run in runs:
            workflow_id = values.number(run, "workflow_id")
            if workflow_id not in watched:
                continue        # a deleted workflow's run, or an ignored one
            workflow = (values.text(run, "name")
                        or watched[workflow_id]
                        or str(workflow_id) or "workflow")
            branch = values.text(run, "head_branch", default="?")
            key = (workflow_id, branch)
            status = values.text(run, "status").lower()
            current = states.get(key, (workflow, None, None, None))
            current_workflow, completed, running, verdict = current
            if running is None and status in self._ACTIONS_RUNNING:
                running = run
            run_verdict = self._action_verdict(run)
            if completed is None and run_verdict is not None:
                completed, verdict = run, run_verdict
            states[key] = (current_workflow, completed, running, verdict)
        return states

    @staticmethod
    def _existing_workflows(client: GitHubClient,
                            full_name: str) -> _WorkflowList:
        """The repo's workflows that still exist (``state`` != ``deleted``), by id,
        so runs of a deleted workflow can be dropped — **and how much of the list
        this is**.

        Name as well as id, because the ids that never appear in the runs page are
        reported on their own entries and an operator cannot act on a number. The
        counts are what stops that report from being built on a set that is itself a
        cut: this reads one page like everything else here, and a repository with
        more workflows than fit in it would otherwise have the coverage line go
        blind in exactly the way the aspect it is there to catch does.
        """
        data = client.get(f"/repos/{full_name}/actions/workflows",
                          {"per_page": 100})
        listed = values.rows(data, "workflows")
        return _WorkflowList(
            by_id={values.number(workflow, "id"): _Workflow(
                name=values.text(workflow, "name"),
                state=values.text(workflow, "state"),
                url=values.text(workflow, "html_url"))
                for workflow in listed
                if values.text(workflow, "state") != "deleted"},
            listed=len(listed),
            total=values.number(data, "total_count"))

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

    def _with_cache_report(self, scope: str, client: GitHubClient) -> str:
        """The scope report, plus what the conditional cache holds and what this
        run cost against what the guard priced it at.

        **Display text, never a status claim** (little-sister ADR-0044): it is
        scope, it never alarms, and nothing derives a code from it. It is here
        because this cache is invisible to everything the library can attribute —
        it is not entries, so `entry_limit` never sees it, and ADR-0075's
        declarations cover worker seconds and entry counts, not held bytes. The
        engine's own observed limit is the backstop (`limits.py` measures resident
        size and speaks at 80 %); this line is so that a reader can see the thing
        that grew.

        The estimate beside the spend is the measurement this slice owes. The guard
        still prices a run at full cost while a run with a warm cache spends a
        fraction of it, which is conservative and refuses nothing it should not —
        but it is now a number somebody can read rather than a sentence in a
        backlog item (backlog #6).
        """
        spent = client.reads_made - client.free_reads
        # `sbom_check` is **not** among them: it reads `POST /graphql` (ADR-0008),
        # conditional requests are a `GET`/`HEAD` mechanism and GitHub's GraphQL
        # answers carry no `ETag`. Counted rather than assumed, so a config that
        # switches an aspect off says the smaller number.
        covered = sum(1 for name in self.active_aspects() if name != "sbom_check")
        line = (f"conditional cache: {len(self._conditional)} payload(s) held "
                f"across {covered} aspect(s) and discovery, "
                f"{self._conditional.dropped} dropped; this run made "
                f"{client.reads_made} request(s) of which {client.free_reads} were "
                f"free, so it spent {spent} against the "
                f"{self._priced_total} the guard priced it at")
        return f"{scope}\n\n{line}" if scope else line

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
        self._conditional.started()
        try:
            client = self._make_client(self.token, deadline)
            # Discovery is a reader of its own: it asks for the repository list and
            # nothing else does, so its entries age on its own passes rather than
            # on whichever aspect ran last.
            self._conditional.reading("discovery")
            repos = self._discover(client)
            self._conditional.sweep()
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
            short = self._budget_short(client, repos)
        except DeadlineExceeded as cut:
            # The one client call outside the aspect loop — the guard's endpoint
            # rung — so it needs its own catch: `DeadlineExceeded` is deliberately
            # not a `RemoteError`, and letting it out of `run` here would hand the
            # engine an all-or-nothing check error instead of the reading below.
            return CheckResult(StatusCode.WARN, [plain(str(cut)), scope_reason],
                               report=scope_report)
        if short:
            return CheckResult(StatusCode.WARN, [short, scope_reason],
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
        # order **moves**: it resumes after the last aspect that finished, so the
        # tail a short run misses is the head of the next one (`run_order`).
        roster = self.run_order()
        if roster and roster[0] != self.active_aspects()[0]:
            # The one line that says the rotation happened. Without it a reader of a
            # log has to reconstruct the order from the per-aspect lines below, and
            # the whole point of the rotation is that the order is not the one the
            # constant shows.
            logger.info("%s: roster resumes at %s", self.path, roster[0])
        for position, name in enumerate(roster, start=1):
            entered = deadline.elapsed()
            reads_before = client.reads_made
            started = False
            try:
                if deadline.expired():
                    raise DeadlineExceeded(name)
                started = True
                # **The cache's unit is the reader, not the run** (ADR-0011): the
                # aspect is the engine's unit (little-sister ADR-0078), and once
                # aspects are paced separately a run that exercised only `actions`
                # must not age out every alert payload it never asked for.
                self._conditional.reading(name)
                # The rank is set **here** and not in the seven builders: it is a
                # property of the aspect roster, not of anything a builder measured,
                # and seven copies of `ASPECTS.index(...)` is how a row ends up
                # disagreeing with the constant it claims to follow.
                children.append(replace(builders[name](client, repos),
                                        order=self.aspect_rank(name)))
                # Swept only where the pass **finished**: an aspect the deadline or
                # the pause budget cut off read a prefix of the repositories, and
                # ageing its entries on that would be the partial-run defect the
                # two-pass rule exists to avoid, arriving one level down.
                self._conditional.sweep()
                # Only a *finished* aspect moves the resume point: one cut off by
                # the deadline is where the next run has to start, not where it
                # has to start after.
                self._resume_after = name
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
            # One line per aspect that finished, and the numbers that say where a
            # run's budget went: which aspect, how long it took, how many requests
            # that cost, what was left afterwards — and, last, what **GitHub** says
            # those same requests cost. Our count and theirs on one line and in one
            # column, once per aspect, so a budget that moves by the reads we made,
            # moves by something else, or does not move at all is a column to read
            # down rather than an arithmetic exercise across two logs. A run that
            # always stops at the same aspect is answered here too.
            logger.info("%s: aspect %d/%d %s took %.1fs in %d read(s) — %.0fs of "
                        "the run left; GitHub says %s", self.path, position,
                        len(roster), name, deadline.elapsed() - entered,
                        client.reads_made - reads_before, deadline.remaining(),
                        budget_said(client.last_rate_limit))
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
        # For the next run's guard: how long a run of this scope takes is how
        # long somebody else's spend has to eat into a window before this run is
        # through with it.
        self._last_run_seconds = deadline.elapsed()
        code, reason = self._node_reading(scope_code, scope_reason, cut_short,
                                          client.throttled_seconds,
                                          client.retried_seconds)
        return CheckResult(
            code, reason,
            children=tuple(children),
            report=self._with_cache_report(scope_report, client),
        )

    def _priced_reads(self, repos: list[Repo]) -> dict[str, int]:
        """What this run will ask for, per path in the ledger's reduced form.

        One read per repository per endpoint — two aspects on one endpoint are one
        read (`ASPECT_ENDPOINT`) — and for `actions` the workflow list plus one
        read per workflow the last run saw in that repository per branch it is
        asked about (`1 + W × B`, and `B` is one in the default-branch mode), or
        plus the one page of the wide read in `all_branches` mode, which is charged
        wherever GitHub charges `/actions/runs`. ``max(1, …)`` so an empty scope
        still prices one read per endpoint: the guard is a floor, and a skip
        saying `need > 4×0` would be no sentence at all.
        """
        count = max(1, len(repos))
        reads: dict[str, int] = {}
        for name in self.active_aspects():
            served = self.ASPECT_ENDPOINT[name].lstrip("/")
            if name == "sbom_check":
                # One query per repository asked, priced at its point on the
                # `graphql` window (ADR-0008, decision 5); an ignored repository
                # is not asked and not priced.
                asked = sum(1 for repo in repos if repo.name not in self.sbom_ignore)
                queries = max(1, -(-asked // self._GRAPH_REPOS_PER_QUERY))
                reads[served] = queries * self._GRAPH_QUERY_POINTS
                continue
            reads[served] = count
            if name == "actions":
                if self.actions_all_branches:
                    reads["actions/runs"] = count
                else:
                    # A read per workflow **per named branch** (ADR-0009). With no
                    # list the one branch asked about is the repository's own
                    # default, so the factor is one and this stays the `1 + W` the
                    # guard has priced since ADR-0007.
                    branches = max(1, len(self.actions_branches))
                    reads[served] += branches * sum(
                        self._workflow_counts.get(repo.full_name, 0)
                        for repo in repos)
        return reads

    def _serves(self, counter: Counter) -> str:
        """The only name a window has: what it serves — the first segment of
        each aspect's path this token's reads were charged to it on, in the
        roster's order, `dependabot, code-scanning and actions` — and
        `discovery` for whatever else landed there, which is the account and
        team reads (`/orgs/…`, `/users/…`, and `/organizations/…`, the `Link`
        header's spelling of the same page). Empty for a window nothing here was
        charged to."""
        roster = [self.ASPECT_ENDPOINT[name].lstrip("/") for name in self.ASPECTS]
        words: list[str] = []
        for served in sorted(counter.paths, key=lambda path: (
                roster.index(path) if path in roster else len(roster), path)):
            word = served.split("/")[0] if served in roster else "discovery"
            if word not in words:
                words.append(word)
        if len(words) <= 1:
            return "".join(words)
        return ", ".join(words[:-1]) + " and " + words[-1]

    def _budget_short(self, client: GitHubClient,
                      repos: list[Repo]) -> str | None:
        """The pre-run guard: the sentence that skips this run, or ``None`` when
        every window the run will spend can afford it (ADR-0007, decision 3).

        Priced **per counter**, in three rungs. Each path this run will read is
        looked up in the ledger, which says which window this token's reads of it
        were charged to in the current hour; a path the ledger has not seen this
        window — the first run after a restart, an aspect switched on, the run
        after a rollover — is priced against the tightest window known **of the
        resource that path was last charged to** (`Ledger.resource_of`), or of
        any resource for a path never seen; and a path whose resource has no
        window open, like every path where nothing is known at all, is priced by
        the endpoint as it always was, so a fresh process is never *less* guarded
        than the one before it. Of the resource, because a token's two `core`
        windows can roll over in one minute, and the first run after that once
        priced every REST read against the one window still open — GraphQL's —
        which is a number about a different budget. A window somebody else is
        smaller by the end of this run than it reads at the start, so the foreign
        rate measured on it, over the length of the last run, comes off first.
        `rate_limit_safety_factor` keeps its meaning and its name; it is applied
        per window instead of once.

        **A window that ends before this run would is outside the guard** —
        neither priced nor the fallback for an unseen path. The guard exists for
        the lockout ADR-0001 describes: exhaust an hourly window and every aspect
        is blind until it rolls over, so a WARN now beats a 403 in twenty
        minutes. A window that resets inside the run cannot do that — exhausting
        it costs a wait the throttle path reads off the 403 and `max_pause`
        bounds, and at worst the run is cut short with what finished kept — while
        skipping every aspect to protect a minute of `dependency_sbom` reads
        would trade the whole run for nothing. *Before the run would end* is
        measured by the last run's length, and by `timeout:` before one has been
        measured, since that is the most a run can take. A run shorter than the
        minute such a window may still have ahead of it prices it with the
        factor; that is a scope small enough that `factor × repositories` sits
        under the window's hundred anyway, and if it ever bites, a window's
        period is the difference between two successive resets on one path,
        which the ledger sees.

        Every window is on the log whether or not it stops the run: the
        interesting case is the one that *just* cleared the factor, which is
        invisible when only the refusal is logged.
        """
        reads = self._priced_reads(repos)
        self._priced_total = sum(reads.values())
        factor = self.rate_limit_safety_factor
        now = time.time()
        book = shared_ledger()
        horizon = (self.timeout_seconds if self._last_run_seconds is None
                   else self._last_run_seconds)
        tightest = book.tightest(self.token, now, outlasting=horizon)
        if tightest is None:
            return self._budget_short_by_endpoint(client, repos,
                                                  sum(reads.values()))
        landing: dict[tuple[str, int], tuple[Counter, int]] = {}
        unpriced: set[tuple[str, int]] = set()
        by_endpoint = 0       # reads of a resource with no window open: rung three
        for served, count in reads.items():
            counter = book.counter_for(self.token, served, now)
            if counter is not None and counter.reset <= now + horizon:
                key = (counter.resource, counter.reset)
                if key not in unpriced and counter.remaining is not None:
                    unpriced.add(key)
                    logger.info("%s: %d API calls left on the window GitHub "
                                "charges the %s reads to (%s) — it ends before "
                                "this run would be through, so the run is not "
                                "priced against it", self.path,
                                counter.remaining, self._serves(counter),
                                _resets_in(counter.reset, now))
                continue
            if counter is None or counter.remaining is None:
                resource = book.resource_of(self.token, served)
                counter = (tightest if resource is None else book.tightest(
                    self.token, now, outlasting=horizon, resource=resource))
                if counter is None:
                    by_endpoint += count
                    continue
            key = (counter.resource, counter.reset)
            held, total = landing.get(key, (counter, 0))
            landing[key] = (held, total + count)
        for counter, needed in landing.values():
            assert counter.remaining is not None       # `tightest` has one
            rate = counter.foreign_rate()
            meanwhile = (0.0 if rate is None or self._last_run_seconds is None
                         else rate * self._last_run_seconds / 3600.0)
            serves = self._serves(counter)
            window = (f"the window GitHub charges the {serves} reads to"
                      if serves else "the tightest window known for this token")
            when = _resets_in(counter.reset, now)
            # The word the budget node uses for this resource: a GraphQL window
            # is counted in points, and "API calls" on it would be a number in
            # the wrong unit beside a node saying the right one.
            unit = ("points" if unit_of(counter.resource) == "points"
                    else "API calls")
            logger.info("%s: %d %s left on %s (%s)%s, this run needs "
                        "%d×%d = %d there", self.path, counter.remaining, unit,
                        window, when,
                        (f" — of which ~{meanwhile:.0f} will be spent by "
                         f"something else during this run"
                         if meanwhile else ""), factor, needed, factor * needed)
            if counter.remaining - meanwhile < factor * needed:
                said = counter.foreign_rate_said()
                return (f"skipped this run: {counter.remaining} {unit} left "
                        f"on {window} ({when}), need > {factor}×{needed} for "
                        f"{len(repos)} repo(s)"
                        + (f"; ~{said}/h of it is spent by something else "
                           f"using this token" if said is not None else ""))
        if by_endpoint:
            return self._budget_short_by_endpoint(client, repos, by_endpoint)
        return None

    def _budget_short_by_endpoint(self, client: GitHubClient, repos: list[Repo],
                                  needed: int) -> str | None:
        """The guard's bottom rung: nothing in the ledger, so `/rate_limit`, read
        and priced the way every release before ADR-0007 did it.

        Its answer is one counter of the token's — for some tokens one nothing
        spends — which is why it is the last rung and not the first. The read
        itself feeds the ledger like any other response, so a real counter it
        reports is known from here on.
        """
        try:
            _limit, remaining, _reset = client.rate_limit()
        except GitHubError:
            return None    # rate-limit endpoint unavailable — proceed rather than block
        logger.info("%s: %d API calls left, this run needs %d×%d = %d",
                    self.path, remaining, self.rate_limit_safety_factor,
                    needed, self.rate_limit_safety_factor * needed)
        # **The same response, read twice.** The line above is `/rate_limit`'s
        # body; this one is the `x-ratelimit-*` headers on the very response that
        # carried it. They agreed in every reading behind ADR-0007 — the
        # disagreement this line was built to catch is between the endpoint and
        # the ordinary reads, which is the ledger's to show — and the line stays
        # because one response has no seconds between its two halves to blame.
        logger.info("%s: the same response's own headers say %s", self.path,
                    budget_said(client.last_rate_limit))
        if remaining < self.rate_limit_safety_factor * needed:
            return (f"skipped this run: {remaining} API calls left, need > "
                    f"{self.rate_limit_safety_factor}×{needed} "
                    f"for {len(repos)} repo(s)")
        return None

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
                      cut_short: str, throttled: float = 0.0,
                      retried: float = 0.0) -> tuple[StatusCode, list[str]]:
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
        # A pause is a fact about the run, not a claim about anybody's
        # repository, so it sits here with the other two and grades nothing on
        # its own: waiting when a service asks is correct behavior, and so is a
        # second ask after a failure. What the lines stop is a paused run being
        # indistinguishable from a slow one — and each is **named by its cause**
        # (ADR-0007, decision 4): *rate limit* only when a throttle was read, the
        # backoff after an unexplained failure as what it is. A reason string,
        # not a slug or a key, so nothing a deployment stores moves with it.
        if throttled:
            reason.append(
                f"paused {throttled:.0f}s for a GitHub rate limit")
        if retried:
            reason.append(
                f"paused {retried:.0f}s retrying after GitHub did not answer")
        if cut_short:
            reason.append(cut_short)
            code = StatusCode.WARN if code is StatusCode.OK else code
        return code, reason
