# ADR-0011 — Conditional requests, and the cache that holds them

- **Status:** Accepted
- **Date:** 2026-09-19
- **Related:** [ADR-0007](0007-the-budget-is-read-where-it-is-spent.md) (the ledger a
  `304` is a reading of, and the guard this does not change),
  [ADR-0002](0002-a-read-failure-is-not-a-finding.md) (the three faults — a `304` is
  none of them), [ADR-0005](0005-the-actions-aspect-asks-per-workflow.md) (the
  per-workflow read this makes cheap), [ADR-0008](0008-the-dependency-graph-is-asked-not-exported.md)
  (`sbom_check` reads GraphQL and is outside this), little-sister ADR-0078 (the unit
  is what the engine releases — why the sweep is keyed to a reader), little-sister
  ADR-0082 (a record is published state — why this is not one), little-sister ADR-0044
  (display text is not a status claim)

A bare ADR number here is this repository's; a reference to one of little-sister's
is always written out, because the two numbering spaces overlap.

## Context

Between two runs fifteen minutes apart, most of what this check reads has not changed:
no new pull request, no new alert, no workflow run. It asked for all of it again anyway.
GitHub answers a conditional request — `If-None-Match` against an `ETag` it gave earlier
— with `304 Not Modified`, and **does not charge it against the primary rate limit when
the request was correctly authorized**. Measured on this deployment's token
(2026-09-19): three `304`s left `used` at 120 and `remaining` at 4880 untouched, the
plain `200` after them moved `used` by one, and `x-ratelimit-reset` was identical on all
five responses, so it was one window rather than two of the token's `core` counters
taking turns. The same probe run unauthenticated has a `304` charged like a `200`, which
is the excluded case and not a contradiction.

Every `GET` this check makes carries an `ETag` and answers `304` to `If-None-Match`:
discovery, `/pulls`, `/issues`, `/actions/workflows`, `/actions/runs`, and the
per-workflow runs read that dominates the cost. The client sent neither conditional
header, and `_attempt` would have turned a `304` into a refusal — `fault_for(304)` reads
it as an answer, so a repository that had not changed would have been reported as one
this check could not read.

## Decision

**The cache lives on the check, not on the client.** `_make_client` builds a fresh
`GitHubClient` inside every run, so a cache held there would start empty each time and
buy nothing. It sits beside `_workflow_counts`, which is the same kind of memory —
across runs, in this process only — and is injected into the client the way the ledger
is.

**It is per check, and that is what makes a bare URL a sufficient key.** GitHub's
answers differ by credential and a check resolves exactly one token, so two checks on
two tokens keep two caches and no read can be served another credential's payload. The
ledger next door is deliberately the other shape — shared across tokens, keyed by token
inside — and lifting this into the library later means putting the identity into the key
first.

**It is not a record.** little-sister ADR-0082's record is published state on an entry:
snapshotted, copied on every poll, serialized to every API client, rendered on the node
page and capped at 2 KB with *a record is what the check read about one subject, never a
payload dump*. Whole API payloads are what that cap exists to keep out of the tree.
Nothing here reaches `data`, and nothing here is sized by that limit. A `304` also
creates no carried reading: the check asked this run and was told the answer still holds,
so the observation time is now.

**The held value is `(etag, payload, link)`.** `_attempt` returns `(payload, Link
header)` and `get_paginated` walks that header, so returning the held payload without
the held `Link` would stop a paginated read after its first page and call the result
complete.

**A `304` is handled in `_attempt` before `_refusal`**, and is recorded in the ledger as
a reading **of the window the path is charged to** that spent nothing of it. Those are
two facts, and the ledger keeps them apart. The answer *names the counter* exactly as a
`200` does — its `x-ratelimit-resource` and `reset` say where the path lands, which is
what the guard's first rung, the node's window count and the *before this process first
read this window* clause stand on — so the path is recorded against the counter. The
charge is nil, so `attempts` does not move: `Ledger.record` computed both from
`FREE_PATHS` alone and now also takes the status, because `attempts` is what
`foreign_rate` subtracts from the rise in `used` — a free reading counted as spent makes
this process look busier than it was and somebody else quieter, and takes the node's
*spent by something else* clause with it. Collapsing the two into one *charged* flag
would make a `304` that opens a window look like the endpoint's one-counter-per-resource
kind, and the warm run after a rollover — the run this record exists for — opens each
`core` window with a `304`: read as endpoint-only, the two would replace each other and
the node would show one window flapping between two. The reading itself is kept either
way: a `304` carries the whole `x-ratelimit-*` set and is a true sample of the window.

**The budget read is never held.** `/rate_limit`'s *body* is a budget reading — the
ledger consumes one of its resource rows as if it were a response's own headers — so
serving a held body there would feed a stale window in as a current one, and the budget
is what the guard reasons from. Budget *headers* on a `304` elsewhere are fine; they
arrive with the live response.

**Eviction is by entries, at two missed passes, keyed to the reader.** Two rather than
one because a run cut short by its deadline, its pause budget or a dead network touches
only a prefix of the repositories, and sweeping on one pass would throw away exactly what
the next run needs — the run least able to afford full reads, because it runs in whatever
condition broke the last one. Keyed to the reader — an aspect, or `discovery` — rather
than to the run because the aspect is the engine's unit (little-sister ADR-0078): once
aspects are paced separately, a run that exercised only `actions` must not age out every
alert payload it never asked for. A pass the deadline cut off is not swept at all.

**No byte cap.** A limit the engine can observe beats one it predicts: little-sister's
`limits.py` measures resident size and speaks at 80 %. A kilobyte number for a cache is
one nobody can derive, and if it ever hurts, a cap is an addition on top rather than a
different design.

**The guard is not changed here, and the run says why it need not be yet.** The pre-run
estimate prices a run at full cost while a warm run spends a fraction, which is
conservative — it refuses no run that would have fit — but it is now a standing state
rather than a temporary one. Changing a budget guard against a hit rate nobody has
measured is the wrong order, so this slice buys the measurement instead: the check's
`report` carries what the cache holds, what it dropped, how many of the run's requests
were free, and what the guard priced the run at. Display text, never a status claim
(little-sister ADR-0044). The guard decision is its own item, with that number in hand.

## Consequences

A second run against an unchanged organization spends a fraction of the requests it used
to, and no promise changes: a `304` returns exactly the payload the `200` returned.

The cache is invisible to everything the library can attribute — it is not entries, so
`entry_limit` never sees it, and ADR-0075's declarations cover worker seconds and entry
counts rather than held bytes. The observed limit is the backstop and the report line is
so that a reader can see the thing that grew.

`sbom_check` is outside this: it reads `POST /graphql`, conditional requests are a
`GET`/`HEAD` mechanism, and the answer carries no `ETag`. Seven aspects and discovery,
not eight, and the report line says so.

## Alternatives

**A cache on the client.** Where the sketch put it, and it cannot work: the client is
built per run.

**A module-level cache shared like the ledger.** One memory for every check, and a bare
URL key would then serve one credential's payload under another's read. Sound only with
the token in the key, which is a cost for a sharing nobody has asked for.

**Sweep what one run did not touch.** Simpler to state and wrong exactly when it is
expensive: the runs that fail to touch everything are the ones that could least afford to
re-read it.

**A byte cap now.** A number nobody can derive, defended against a cost nobody has
measured, in a process that already measures its own resident size.

**Change the guard in this slice.** It would be tuned against a hit rate that does not
exist yet. The rate becomes measurable the moment this lands.
