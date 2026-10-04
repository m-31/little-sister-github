"""What every module of this suite shares: one setting, and one fixture.

**The setting is the library's settings file.** A log line that names a budget
window writes the window's end as a clock time, and the library writes it, in the
zone its settings name (ADR-0007 decision 5). With no settings file named, the
library looks for a configuration root — a `config/` directory, which a deployment
has and this repository does not — and refuses, so a run that came to such a line
would end there. It reads its settings once, the first time it needs them, so the
suite names its own file here, before anything imports the library: every run can
write its lines, and a test states a clock time as the text it is and reads the same
one on every machine. The siblings' suites replace the library's writer with one
that shows the instant it was handed; this one keeps the writer, because the clock
is what these lines are about. Set, and not defaulted: a deployment's own settings
in the environment would change what every such test reads.

**The fixture is the ledger's.** The ledger is process-wide by design (ADR-0007):
one object both check types feed and read for the life of the process, so that a
`github` check's reads and the `github-rate-limit` node beside it describe the same
windows. A test suite is one process, so without this a window one test opened
would still be open in the next, and a test would pass or fail by what ran before
it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ["LITTLE_SISTER_CONFIG"] = str(Path(__file__).with_name("settings.yaml"))

from little_sister_github import budget


@pytest.fixture(autouse=True)
def _fresh_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with an empty ledger of its own; `budget.ledger()` — the
    name the shipped code reads at call time — answers with it."""
    monkeypatch.setattr(budget, "_LEDGER", budget.Ledger())
