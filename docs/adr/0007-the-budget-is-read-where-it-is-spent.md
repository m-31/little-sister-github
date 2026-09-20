# ADR-0007 — The budget is read where it is spent, and there is more than one of it

- **Status:** Accepted
- **Date:** 2026-09-20 (accepted 2026-09-13)
- **Related:** [ADR-0001](0001-a-second-check-type-in-this-package.md) (the budget is
  the token's and a type of its own — this record keeps that and corrects what it says
  about `core` and about the guard), [ADR-0002](0002-a-read-failure-is-not-a-finding.md)
  (a throttle is *not now*; decision 4 here is about the sentence that follows one),
  [ADR-0005](0005-the-actions-aspect-asks-per-workflow.md) (the per-workflow read, which
  is charged to a different counter than the read it replaced), little-sister ADR-0042
  (an entry carries its own code), little-sister ADR-0050 (a slug is an identifier,
  never a position), little-sister ADR-0058 (one transport policy; the sleep this record
  names is the library's)

A bare ADR number here is this repository's; a reference to one of little-sister's
is always written out, because the two numbering spaces overlap.

## Context

Both check types in this package describe GitHub's primary rate limit, and both took the
same thing for granted: that a token has **one** `core` budget of 5,000 requests an hour,
that `GET /rate_limit` reports it, and that the `x-ratelimit-*` headers on an ordinary
response restate the same number a moment later. ADR-0001 built the `github-rate-limit`
type on the endpoint and left the `github` type's pre-run guard reading the same
endpoint; the header trace of 0.1.3 was added to catch the one disagreement anybody
expected, the body of `/rate_limit` against the headers of that very response.

Two weeks of one deployment's logs — two tokens, one process, one organization — and a
third token on a personal deployment say otherwise, and they say it the same way every
time. The numbers below were read off those logs in September 2026; the raw readings
stay in the working notes.

**One token, two `core` counters, split by endpoint.** Every token seen has two counters
that both call themselves `core`, both carry `limit: 5000`, and each keeps its own
`used`, its own `remaining` and its own `reset`. Which counter a request is charged to
follows the **path**: `/repos/…/dependabot/alerts`, `/repos/…/code-scanning/alerts`,
`/repos/…/actions/workflows` and `/repos/…/actions/workflows/{id}/runs` were charged to
one; `/repos/…/secret-scanning/alerts`, `/repos/…/pulls`, `/repos/…/issues` and the
discovery reads under `/orgs/…` and `/users/…` to the other. The split is GitHub's
routing, not a documented rule, and it moved once inside the logs: the one-page
`/repos/…/actions/runs` read that preceded 0.1.6 was charged to the second counter, the
per-workflow read that replaced it
([ADR-0005](0005-the-actions-aspect-asks-per-workflow.md)) to the first. Within one
counter the arithmetic is exact — an aspect's `used` rose by exactly the requests it
made in forty-six of forty-seven checked readings, the exception a window that rolled
over between them — and across some 870 readings on four counters no window
ended earlier than the minute its `reset` named, and `used` never fell inside a window.
What *looks* like a reset that came early is two windows read as one: the `github` type's
trace names whichever counter answered the **last** read of an aspect, so consecutive
lines say `resets in 34min` and `resets in 39min` about the same token, and when the
first window rolls over the line jumps to `5000 left, resets in 59min` while the other
window has minutes to go. On the shared token the two windows stood anywhere from a few
seconds to twenty minutes apart; on the token one process uses alone they coincide,
because one run starts both.

**`/rate_limit` reports one counter, and for some tokens one nothing spends.** For one
token the endpoint's `core` object restated the second counter exactly — its `used` was
the `used` on the last `/issues` read of the run before it in eighty-two of ninety
readings, the rest straddling a window boundary, and rose by exactly the discovery reads
of the run after it in forty-one of forty-four. For
the other two tokens it reported `5000 of 5000, 0 used`, with a `reset` always sixty
minutes out, in 389 of 389 readings, while ordinary reads on the same
token in the same process were reporting `used` in the thousands. One of those two
tokens is spent by a single process, so this is not an artifact of two instances sharing
a credential. The body and the headers of the `/rate_limit` response itself agreed in
347 of 347 readings — the disagreement the 0.1.3 trace was built to catch does not
exist; the one that does is between the endpoint and the reads. GitHub's own guidance
points the same way: *when possible, you should use the rate limit response headers
instead of calling the API to check your rate limit*, and the endpoint *can count
against your secondary rate limit*.

**Every token was shared, and the sharing is measurable.** On the token two instances
spend, the second instance's reads showed up in this one's readings as `used` rising by
more than this process's own requests: about 1,230 an hour on the first counter and
about 520 on the second, the shape of one more run of the same check at the same
cadence. On the token thought to be this process's alone, every window began at the same
minute past the hour — a different minute in the two stretches of log — with thirty to
forty-five requests already spent on each counter before this process had read anything:
a scheduled job somewhere, running hourly with the same credential, and it is the job,
not this process, that opens each window. The personal token showed nothing foreign. None
of this is visible on the `github-rate-limit` node today, and the guard cannot see it
either.

**The pause sentence names a cause it never checked.** `GitHubClient._slept` is the
sleep the library's retry calls for **every** transient failure, and it adds each wait to
`paused_seconds`; `_node_reading` renders the total as *paused Ns for a GitHub rate
limit*. In the two weeks of logs GitHub throttled this deployment **zero** times — not
one 403 with a throttle header, not one 429 — and the node said *rate limit* eighty-six
times, each after a 500 from the SBOM endpoint or a connection that failed or timed out,
the wait being the library's own one-second backoff.

Two smaller things the same logs show. The `dependency_sbom` resource, which the SBOM
read spends and ADR-0001 believed was `core`, has a `limit` of 100 and a `reset` under a
minute away on every reading — a window of a minute, not an hour, so a window's length is
a fact to read from `reset`, never to assume. And an aspect whose every read failed
without a response reports the previous aspect's headers as its own, because
`last_rate_limit` is only ever overwritten by an answer.

## Decision

1. **The budget is read from the headers of every response, and kept per counter.** The
   client already parses `RateLimitHeaders` off every answer and throws all but the
   last away. It now hands each one to a **ledger** the package keeps for the life of
   the process: per token (keyed by a digest of the token's value, never the value and
   never logged), per `x-ratelimit-resource`, one record per **counter**, a counter
   being whatever GitHub answered with a distinct `reset` epoch. The record holds the
   last `limit`, `remaining` and `used`, when they were read, the request paths GitHub
   **routed** to it — reduced to what the read was *of*: the two segments under
   `/repos/{owner}/{repo}/` with ids and query gone (`dependabot/alerts`,
   `actions/workflows`, never `actions/workflows/12/runs`), or the first segment for
   anything else — so a counter's path set stays the size of the API and matches the
   endpoint an aspect is priced by — and this process's own attempts against it since
   the previous reading. A counter whose `reset` has passed is gone; the next reading on
   that path opens the counter of the new window. A reading that reports **nothing
   used** opens no counter: a window exists from the first request charged to it, and
   `/rate_limit` on a token whose counter it cannot see answers `0 used` with a fresh
   `reset` an hour out on every call — kept literally, that is a new "window" a minute
   and a node saying *the tightest of 47 windows* by the end of the hour. The ledger is
   a module of this
   package behind a lock, because the engine runs checks on a thread pool; it is not
   persisted, because an hour's memory is refilled by the first run after a restart and
   the state layer would be paid for nothing. Nothing about *which* path lands on
   *which* counter is written into code: the split is GitHub's, it moved once already,
   and a table would be wrong the next time it does.

   **Routed and spent are two facts about one reading, and the record holds both.**
   Where a response *landed* is what `paths`, `counter_for` and the node's window count
   stand on — a response on a path names that path's counter, `200` and `304` alike,
   while the endpoint's own row is not routed at all and describes a counter by
   identity. What a reading *spent* is `attempts`, `first_charged` and the `before_us`
   subtraction. A `304` is routed and spends nothing, so it opens a window exactly as a
   `200` would; answering both questions with one flag made such an opening look like
   the endpoint's one-counter-per-resource kind, and the warm run after a rollover
   opens each `core` window with a `304`
   ([ADR-0011](0011-conditional-requests-and-the-cache-that-holds-them.md) has the
   reasoning).

2. **`github-rate-limit` says the tightest counter, and how many there are.** One entry
   per watched resource, keyed and graded as today — `core` stays `core`, and a
   maintenance pin held against it holds (little-sister ADR-0050) — but the line is
   written from the ledger: the counter with the least `remaining` grades the resource,
   and the sentence says it is one of several when it is —
   `core: 2441 of 5000 requests left, resets in 1min — the tightest of 2 windows GitHub
   keeps for this token; the other has 3932 left, resets in 6min`. The `/rate_limit`
   body is still read every run and is merged into the ledger as **one more reading**,
   of whichever counter its `reset` names: it is free, it is the only source for a
   resource nothing in this process spends — `graphql`, `search` — and where it does
   report a real counter it samples that counter between runs. A resource the ledger
   has heard nothing about from a response is reported from the endpoint and says so:
   `graphql: 5000 of 5000 points left, resets in 59min — as /rate_limit reports it;
   nothing here has spent it`. A counter the endpoint alone reports with spend on it
   stands as one counter per resource — the next such reading replaces it — and one it
   reports at zero used opens nothing (decision 1), so the endpoint can never multiply
   the windows the node counts.

   The line also carries what the ledger can measure and nothing else can: the spend
   **this process did not make**. Between two of its own readings on one counter,
   `used` rose by this process's attempts plus everybody else's; the difference over
   the interval is a rate, and when it is not negligible the line says `~1250/h of it
   is spent by something else using this token`. It is an estimate under one stated
   assumption — that the others keep the pace they kept — and the sentence says so with
   the tilde.

   A rate measured between this process's own readings cannot show a consumer that
   spends **before** the first of them: the hourly job that opens every window on one
   deployment's token, whose thirty to forty-five requests are in the first reading's
   `used` and in no difference. So the line also says what the window carried before
   this process first read it — `45 of it were spent by something else before this
   process first read this window` — but only when this process **saw the window open**:
   the ledger remembers, per path, the last `reset` a reading named, outliving the
   counter it pruned, and a window whose predecessor on that path ended before it counts
   as seen opening. Then the number is `used` at the first reading less that reading's
   own request, a measurement and not an estimate, and it has no floor but zero; a window
   this process opened itself reads one used and says nothing. Without the rollover seen
   the number is not claimed at all: a process started at half past reads a window with
   thirteen hundred used, most of it this deployment's own before the restart, and a line
   calling that foreign would be false. The log gets a line at the first sight of any
   window with spend on it, condition or none, because there the timestamps tell the two
   apart.

   Last, the line says what **this process** spent, and the counter keeps one more bit
   so that it can. Everything above names what somebody else spends and what a window
   carried before this process read it; the one number a reader can act on is its own,
   so the sentence opens with `; 269 of it this process's own` — a count and not a
   rate, said whenever it is not zero. The bit is `Counter.first_charged`, and it
   exists because `attempts` counts only the readings *after* the first: the opening
   request is already inside `first_used`, so a window this process opened had spent
   one more than `attempts` says. `own_spend` is `attempts` plus that opening request
   where it was charged — `/rate_limit` is free, so a window the endpoint opened claims
   none of it. **Nothing feeds it into the foreign rate**, which subtracts `attempts`
   precisely because the opening request is inside `first_used`; subtracting
   `own_spend` there would take that request off twice and make this node under-report
   what somebody else spends.

3. **The `github` guard prices the run against the counters it will spend, not against
   the endpoint.** Today `run()` reads `/rate_limit` once and compares its `core`
   `remaining` with `rate_limit_safety_factor × repositories × endpoints`; on two of the
   three tokens that number was 5,000 every time, so the guard could not have fired at
   all. It now prices per counter: the run's aspects map to request paths
   (`ASPECT_ENDPOINT`), the ledger says which counter each path was charged to in the
   current window, and the run is skipped when any counter it will touch has less than
   the factor times the reads that will land on it — less the foreign rate of decision 2
   over the length of the last run, since a budget that is being spent by somebody else
   is smaller than it reads. The WARN names the counter by what it serves, which is the
   only name it has: `skipped this run: 310 API calls left on the window GitHub charges
   the dependabot, code-scanning and actions reads to (resets in 12min), need >
   4×57`. Where the ledger does not yet know a path — the first run after a restart, an
   aspect switched on, the run after a rollover — that path is priced against the
   tightest counter known **of the resource it was last charged to**, which the ledger
   remembers beyond the window (a path never seen, against the tightest of any); a path
   whose resource has no counter open is priced by the endpoint, as is everything where
   nothing is known at all, so a fresh process is never *less* guarded than the current
   one. Of the resource, and not of any: a token's two `core` windows can roll over in
   the same minute, and the first run after that once priced every REST read against
   the one window still open, GraphQL's — a number about a different budget, harmless
   while that window was full and a skipped run naming the wrong window the day it is
   not. `rate_limit_safety_factor` keeps its meaning and its name; it is applied per
   counter instead of once.

   A counter whose `reset` comes before this run would end — measured by the last
   run's length, and by `timeout:` before one has been measured — is **outside the
   guard**: neither priced nor the fallback for a path the ledger has not seen. The
   guard exists for the lockout ADR-0001 describes, an hourly window exhausted and every
   aspect blind until it rolls over; a window that ends inside the run cannot do that —
   exhausting it costs a wait the throttle path reads off the 403 and `max_pause`
   bounds, and at worst the run is cut short with what finished kept — while skipping
   the whole run to protect a minute of `dependency_sbom` reads would trade every aspect
   for nothing. A run shorter than the minute such a window may still have ahead of it
   prices it with the factor; that is a scope small enough that the factor times its
   repositories sits under the window's hundred, and if it ever bites, a window's period
   is the difference between two successive resets on one path, which the ledger sees.

4. **A pause is named by its cause.** The client keeps two totals instead of one: the
   seconds it slept because GitHub **asked** — a `retry-after`, or `x-ratelimit-remaining:
   0` with a reset, the throttle path of [ADR-0002](0002-a-read-failure-is-not-a-finding.md)
   — and the seconds it slept on the library's backoff after a failure GitHub did not
   explain. The node's sentence says whichever happened, or both: `paused 61s for a
   GitHub rate limit` only when a throttle was read, `paused 3s retrying after GitHub
   did not answer` for the rest. It is a reason string, not a slug, a `type:` name or a
   configuration key, so nothing a deployment stores against this package moves; it is
   a change in what a node claims, which is why it is here and not in a commit message.
   The test is the sentence: a run whose only transient failure is a 500 does not say
   *limit* on its node, and it fails against the code as it stands.

5. **A read that got no answer leaves no budget claim.** `last_rate_limit` is cleared
   before every attempt, so a request that never reached a status reads on the trace as
   *no budget headers on that read* rather than as the previous response's numbers. And
   every trace line that names a window names its end as a clock time beside the
   minutes — `resets in 34min (21:50:07)` — so two lines about two windows read as two
   windows, which `resets in Nmin` alone cannot show; the node keeps the minutes, since
   a reader there wants the wait.

## Consequences

- **What the `github-rate-limit` node says changes, and what it grades changes with
  it.** A token whose `/rate_limit` reports a pristine counter stops reading green at
  5,000 while the `github` check beside it spends 2,500 an hour; a token whose endpoint
  reports one of two counters stops hiding the other, which on the deployment measured
  is spent about twice as heavily. Thresholds keep their meaning —
  `warn_below` and `error_below` are compared with the tightest counter — and a
  deployment that tuned them against the endpoint's number will see the node go amber
  sooner, which is the node telling the truth.
- **The guard can fire where it never could.** A deployment on a token with a pristine
  endpoint has been running unguarded; after this it is guarded per counter, and a run
  that is skipped says which window and what it serves. The factor is unchanged, so a
  deployment that never neared the limit sees nothing.
- **The per-aspect estimate in the README and in `examples/github.yaml` is priced per
  counter** when this ships, and the README's sample line for the budget node changes
  shape. Both are updated in the change that ships decision 2.
- **ADR-0001 stays the record for what it decided** — the budget is the token's, the
  type is its own, the thresholds are counts — and names the two sentences this record
  replaces: that every call the `github` check makes is charged to `core`, and that the
  guard reads the budget the run spends.
- **`dependency_sbom` gets the same treatment for free.** The ledger keys by resource,
  so the SBOM read's counter — 100 a minute, not 5,000 an hour — is graded by its own
  numbers when a deployment watches that resource; the guard reads its `reset` and,
  finding the window ends before the run would, leaves it alone (decision 3).
- **Nothing here changes a stored key.** No slug, `type:` name or configuration key
  moves; the CHANGELOG entry that ships this is *Changed*, not a break.
- The `RateLimitHeaders` trace of 0.1.3 has done its work: the disagreement it was built
  to catch does not exist, and the one it found instead is this record. The trace stays,
  because it is the ledger's source.

## Alternatives considered

- **Keep `/rate_limit` as the truth and find out what is wrong with the tokens.**
  Rejected. The endpoint's view is not the defect that matters: even where it reports a
  real counter it reports one of two, and the reads themselves are split. Whether the
  pristine reading was a property of a token's kind was worth asking, and it is not: the
  deployment's two tokens are both classic tokens, one reads pristine and one does not, so
  what separates them is something else and unknown — and the design does not change with
  the answer.
- **One entry per counter, each with its own slug.** Rejected: a counter has no
  identifier. Its `reset` moves every hour and the set of paths it serves is GitHub's
  routing, which moved once inside the logs; a slug built on either would slide onto
  a neighbor, the failure little-sister ADR-0050 exists to refuse. The resource name is
  the stable identity, and the resource's line says how many counters stand behind it.
- **A table of which path lands on which counter.** Rejected for the same reason: it
  would have been wrong from 0.1.6 on. The ledger observes the split; nothing declares it.
- **Let the `github` check report the budget itself, from the headers it already
  holds.** Rejected by ADR-0001's first two arguments, which stand: the budget is the
  token's, and a node inside the `github` check goes quiet exactly when the guard
  fires. The ledger is what lets the two types share one reading without either
  owning it.
- **Persist the ledger in the state layer.** Rejected: an hour's memory, refilled by the
  first run, is not worth a file and a keeper's bytes; a restart costs one run of
  reading the endpoint as today.
- **Fold the foreign rate into the grade rather than the line.** Rejected: the grade is
  on what is left, and ADR-0001 decision 4's reason holds against the projection too —
  a check that graded on an estimate would be wrong in the direction nobody can check.
  The estimate belongs in the guard, where the decision it informs is *whether to spend*.

## Sources

- <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api> —
  the headers, the recommendation to read them, and that `/rate_limit` can count against
  the secondary limit.
- <https://docs.github.com/en/rest/rate-limit/rate-limit> — the resource objects the
  endpoint reports, `dependency_sbom` among them.

