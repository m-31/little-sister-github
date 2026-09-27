"""One run of a check, the way the engine does it — and one aspect of one, and
what a record it hands back carries as a time.

The engine calls :meth:`~little_sister.checks.Check.measure` and then
:meth:`~little_sister.checks.Check.grade` (little-sister ADR-0086), and a test that
wants *the result of running this check* wants both. Written once here rather than
in every test, and deliberately **not** a method on the check: a convenience that
ran the two halves together would be the thing every author reached for, and the
seam would quietly stop being a seam. The library's own suite keeps the same helper
for the same reason; its tests do not ship, so this one is ours.

``now`` defaults to the clock as :func:`time.time` reads it, rather than to
:func:`datetime.now`, so a test that fixed the one clock the check reads while
measuring hands the grading the same instant.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from little_sister.checks import Check, CheckResult, Measurement

from little_sister_github.github import GitHubCheck, Repo


def measured(check: Check) -> tuple[Measurement, ...]:
    """What one ``measure()`` handed back, normalized the way the engine does."""
    answer = check.measure()
    if isinstance(answer, Measurement):
        return (answer,)
    return tuple(answer)


def run_check(check: Check, *, now: datetime | None = None,
              measurements: Sequence[Measurement] | None = None) -> CheckResult:
    """Measure, then grade, and answer the result the engine would store.

    ``measurements`` replaces the measuring half, for a test that grades a reading
    it wrote itself — which is the whole point of the split.
    """
    taken = measured(check) if measurements is None else tuple(measurements)
    when = now or datetime.fromtimestamp(time.time(), UTC)
    return check.grade(taken, when)


#: What the last :func:`run_aspect` added to the run's could-not-ask count — the
#: number ``measure()`` puts on the estate reading for that aspect, and the one
#: the check's own node reports. Kept here rather than on the check because the
#: check keeps no such state any more: the count is a reading.
_LAST = {"unreachable": 0}


def unreachable() -> int:
    """The could-not-ask count of the aspect :func:`run_aspect` last read."""
    return _LAST["unreachable"]


def aspect_readings(check: GitHubCheck, name: str, client: Any,
                    repos: Sequence[Repo]) -> tuple[list[Measurement], int]:
    """One aspect's readings as ``measure()`` collects them, and its read count."""
    if name.startswith("code_scanning_"):
        found, reach = check._read_code_scanning(client, list(repos), name)
    else:
        found, reach = getattr(check, f"_read_{name}")(client, list(repos))
    _LAST["unreachable"] = reach.unreachable
    return [*found, *reach.readings_for(name)], reach.read


def run_aspect(check: GitHubCheck, name: str, client: Any,
               repos: Sequence[Repo]) -> CheckResult:
    """One aspect of a ``github`` check through both halves: its readings, then
    the child its grading builds out of them, with ``repos`` as the roster."""
    readings, read = aspect_readings(check, name, client, repos)
    result: CheckResult = getattr(check, f"_grade_{name}")(
        readings, read, list(repos))
    return result


def record_times(record: object, path: str = "",
                 name: str = "") -> list[tuple[str, str]]:
    """Every time a record carries **as text**, each as ``(dotted path, name)``:
    a string that reads as an ISO-8601 instant with a time of day in it, at any
    depth, and the name it sits under — for an item of a list, the list's.

    little-sister reads three names in a record as instants, at any depth
    (``RECORD_TIMESTAMP_KEYS``, little-sister ADR-0082); a time under any other
    name is a string to every surface that shows the record. What this cannot
    find is an instant kept as a **number**: epoch seconds look like any other
    count, so a test names such a field itself.
    """
    if isinstance(record, Mapping):
        found: list[tuple[str, str]] = []
        for key, value in record.items():
            found += record_times(value, f"{path}.{key}" if path else str(key),
                                  str(key))
        return found
    if isinstance(record, (list, tuple)):
        return [each for index, item in enumerate(record)
                for each in record_times(item, f"{path}[{index}]", name)]
    if isinstance(record, str) and "T" in record:
        try:
            datetime.fromisoformat(record)
        except ValueError:
            return []
        return [(path, name)]
    return []
