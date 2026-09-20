"""Fixtures every module of this suite shares. There is one.

The ledger is process-wide by design (ADR-0007): one object both check types feed
and read for the life of the process, so that a `github` check's reads and the
`github-rate-limit` node beside it describe the same windows. A test suite is one
process, so without this a window one test opened would still be open in the next,
and a test would pass or fail by what ran before it.
"""
from __future__ import annotations

import pytest

from little_sister_github import budget


@pytest.fixture(autouse=True)
def _fresh_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with an empty ledger of its own; `budget.ledger()` — the
    name the shipped code reads at call time — answers with it."""
    monkeypatch.setattr(budget, "_LEDGER", budget.Ledger())
