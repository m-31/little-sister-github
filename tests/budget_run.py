"""One `github-rate-limit` run in a process of its own, for the test that reads
what it logged.

`test_rate_limit.py` runs this file on a machine in one zone with settings that name
another, and reads its stderr: the lines there are stamped by little-sister's default
logging, which a test inside the suite's own process never sees, because pytest
owns the logging there. No GitHub is asked — the client is a double that answers
the two reads a run makes, and keeps what the response's own headers said.

    python budget_run.py <the window's end, in epoch seconds>
"""
from __future__ import annotations

import sys

# An instance has read its settings before a check runs: the application's import
# does it, and hands the logger the zone its lines are stamped in (little-sister
# ADR-0120 decision 9). This process imports no application, so it reads them here.
import little_sister.config  # noqa: F401

from little_sister_github.github import RateLimitHeaders
from little_sister_github.rate_limit import (
    DEFAULT_ERROR_BELOW,
    DEFAULT_WARN_BELOW,
    Budget,
    GitHubRateLimitCheck,
)


class _Client:
    """Answers `GET /user` and `GET /rate_limit` as GitHub would for a personal
    access token whose `core` window has spend on it and ends at ``reset`` — in the
    endpoint's row and in the response's own headers alike."""

    def __init__(self, reset):
        self._reset = reset
        self.last_rate_limit = RateLimitHeaders(
            resource="core", limit=5000, remaining=4000, used=1000, reset=reset)

    def get(self, path, params=None):
        if path == "/user":
            return {"login": "example-user", "type": "User"}
        return {"resources": {"core": {"limit": 5000, "remaining": 4000,
                                       "used": 1000, "reset": self._reset}}}


def main():
    client = _Client(int(sys.argv[1]))
    check = GitHubRateLimitCheck(
        path="/github-rate-limit", token_ref="env://GITHUB_TOKEN",
        budgets=(Budget("core", DEFAULT_WARN_BELOW, DEFAULT_ERROR_BELOW),))
    check._make_client = lambda token, deadline=None: client
    check.measure()


if __name__ == "__main__":
    main()
