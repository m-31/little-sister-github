# ADR-0001 — The API budget is a check type of its own, not an eighth aspect

- **Status:** Accepted
- **Date:** 2026-08-13
- **Related:** [`../../README.md`](../../README.md) (how both types are configured),
  little-sister ADR-0042 (an entry carries its own code), little-sister ADR-0050 (a
  slug is an identifier, never a position), little-sister ADR-0051 (one bare type
  name, claimed once), little-sister ADR-0023 (secret references)

This is the **first ADR in this repository's sequence**. A reference to one of
little-sister's is always written `little-sister ADR-00NN`; a bare number here is ours.

> The port record for each type — the source-to-target mapping and what was
> deliberately changed — is a working note on the development branch, not part of
> what this package ships. This record carries the decision; that note carries the
> lineage.

> **Update (2026-08-15):** two limits this record left unstated, both of them things
> an operator meets while looking at these two nodes side by side. Nothing below
> changes; this names what the decision does **not** cover.
>
> **1. This is the *primary* budget, and the one a burst actually trips is
> unwatchable.** GitHub enforces *secondary* rate limits in addition to the primary
> ones — on concurrency and on request bursts — and its documentation is explicit
> that there is no way to check the status of a secondary rate limit: no header
> carries it, and `GET /rate_limit` does not report it. So this check cannot grow a
> line for it, and the gap is the API's rather than a decision taken here. What a
> deployment sees instead is worth knowing: GitHub answers a secondary limit with
> **403 or 429**, and ADR-0002 classifies a 4xx as an *answer* — not transient, so
> not retried — which means a burst throttle reaches a dashboard as `could not read`
> lines on the `github` check's aspects, at WARN, while this check's node typically
> stays green with plenty of budget left. **That combination — an untroubled budget
> beside a rash of unreadable repositories — is the signature of a secondary limit**,
> and reading it as a permission problem is the mistake this paragraph exists to
> prevent. GitHub's answer may carry a `retry-after` header, which nothing here reads
> yet.
>
> **2. The `github` check's guard reads `core`, and only `core`.** Its pre-run
> estimate consults `resources.core` from the same endpoint this check reads, which
> is the budget every REST call that check makes is charged to — so the guard is
> right today, and a `graphql` or `search` resource going red beside it correctly
> does not stop a `github` run, because that type issues neither kind of request.
> Two consequences follow. The number on this check's `core` line and the number the
> guard acted on are the **same reading taken at different moments**, so a dashboard
> can legitimately show a skipped run beside a budget that has since refilled. And if
> GitHub ever meters one of the endpoints those aspects call under a resource of its
> own, the guard would go on reading a budget that is no longer the one being spent —
> worth naming, because that failure would look like the guard not working rather
> than like the guard watching the wrong number.

> **Update (2026-08-15), later the same day:** the last sentence of point 1 above —
> *GitHub's answer may carry a `retry-after` header, which nothing here reads yet* —
> is no longer true. It is read, along with `x-ratelimit-remaining` /
> `x-ratelimit-reset`; see [ADR-0002](0002-a-read-failure-is-not-a-finding.md)'s own
> Update for the precedence and for why the reading has to live in this package.
>
> **The rest of point 1 stands, and it is the part that matters here.** A secondary
> limit is still unwatchable from `GET /rate_limit`, so this check still cannot grow a
> line for one. What changed is only the *other* node's reading: a burst throttle now
> reaches a dashboard as `could not ask GitHub` lines that grade nothing, rather than
> as amber `could not read` lines blaming the repositories. So the signature that
> paragraph describes has changed shape — an untroubled budget here beside a rash of
> **grey** lines on the `github` check, with the check's own node saying how many reads
> could not be completed.

## Context

A second overview check was ported from the same Ruby dashboard the `github` type came
from: it reads `GET /rate_limit` and reports what is left of the token's REST and
GraphQL budgets. The question the port raised is not how to read the endpoint — that is
four lines — but **where the reading belongs**, because this package already reads it.

`GitHubCheck.run` calls `client.rate_limit()` and, when the remaining budget is under
`rate_limit_safety_factor × (max(1, repos) × aspects)`, returns early with a WARN and
runs no aspect at all. So the number is already in the package — its own WARN line even
quotes it. What is missing is anywhere to see it *before* that guard fires, and any
history of it at all: the number exists only in the runs where the check has already
decided not to do its job.

The obvious placement is an eighth aspect of `github` — one more child, no new type
name, no new config file, and every deployment gets it for free on upgrade.

## Decision

### 1. A second check type, `github-rate-limit`, in the same package

Three things make the aspect wrong, and each of them is about scope rather than taste.

**A budget belongs to the token, not to an account.** The `github` node is *this
account's repositories*: it carries `expect_min_repos`, a repository roster, a
team filter. The rate limit is a property of the **credential**. Two `github` checks
sharing a token share one budget and would report the same number twice, on two nodes,
as though they were two facts; a token also spent by CI or by a bot has a budget this
package does not control and cannot explain. An aspect would be a per-account reading
of something that is not per-account, and the tree would say so wrongly.

**An aspect goes quiet at the moment it matters.** The safety-factor guard returns
*before* any aspect is built. A rate-limit aspect would therefore stop reporting
precisely when the budget is the story — the failure mode where an operator most needs
the number is the one where the aspect would be missing, and a node that is not written
has no history to look back through either. A separate check keeps reporting, at its
own cadence, and what it reports is the explanation of the other check's skipped run.

**Reading the endpoint is free, and the two checks want different frequencies.**
GitHub does not count `GET /rate_limit` against the budget it reports. A check that
costs nothing can run every minute; the `github` check runs every fifteen because it
costs `repos × aspects` calls. An aspect is tied to its check's `frequency:`, so
folding the cheap reading into the expensive check would either make the budget stale
or make the expensive check run too often.

The cost is a second `type:` name — claimed once, family-wide (little-sister ADR-0051)
— and a package whose name no longer describes exactly one type. It stays **one
package**: both types read one API, through one client, with one credential shape, and
they version together. Splitting them would duplicate `GitHubClient` and a release
pipeline for one endpoint.

**Spelled with hyphens.** `github-rate-limit`, matching the family's multi-word type
names (`ssh-command`, `host-metrics`) rather than inventing a second convention.

### 2. A flat node with one coded entry per resource

Each watched resource is an `Entry` carrying **its own code** (little-sister ADR-0042),
not a child node. The node's code is then derived as the worst of them, so a healthy
REST budget cannot hide an exhausted GraphQL one.

Entries rather than children because a resource is a **reading, not a domain**: it has
no sub-structure to grow into, and one page showing every budget at once is what an
operator opens this node for. The keyed form also means the identity is GitHub's own
resource name — `core`, `graphql` — so a maintenance pin held against `core` survives a
config that starts watching `search` next year, and never slides onto a neighbour
(little-sister ADR-0050).

What is given up is per-resource `about` text, since only a node can carry one. The
grading a reader needs instead goes into `config_summary()`, which the library renders
on the node.

### 3. Absolute thresholds, defaulting to 1000 / 500

`warn_below` and `error_below` are counts, at the config's top level as the default for
every resource and inside a resource's block as its own. A fraction of the reported
limit was the alternative and would survive GitHub changing a limit — but the number an
operator reasons about during an incident is *how many calls are left*, and a
percentage of a limit they cannot see is not that number. A resource with an unusual
budget (`search` is 30 a minute) states its own two numbers, which is where the
generality was actually needed.

`error_below` above `warn_below` is a **refusal**, not a preference: no remaining budget
could land in the warning band, so the config would read as though a warning were
possible. Because the two numbers can come from different places — one from a resource's
block, one from the top-level default — the refusal names **the key that was actually
written**, or a reader is sent to a line they never typed.

Equal is allowed and says "no warning band". Zero is allowed — unlike `github`'s
`expect_min_repos`, which is a backstop that must not be disabled, this is a threshold
whose absence is a legitimate thing to mean. Note what the ordering rule implies:
`error_below: 0` switches the red band off on its own, but switching only the amber one
off means zeroing **both**.

### 4. Graded on what is left, and only on that

The reset time is rendered on the line and is **not** in the grade. A check that turned
green because relief was ninety seconds away would be silent for the run that is failing
right now; the reader who wants to know whether to wait can see the clause and decide.

### 5. Four readings the check refuses to fake

- **A failed read says the asking failed.** `could not ask GitHub for the rate limit: …`
  at ERROR, never a claim about a budget nobody read. The same distinction is an open
  question for the `github` type, where a 5xx from one endpoint currently puts a
  repository's name in amber; it is easy here only because this check has exactly one
  source and no other finding to protect, so there is nothing for the failure to be
  mistaken for.
- **A watched resource GitHub did not report is a WARN line naming it**, not a missing
  line. A line that simply vanished would read as a budget that is fine. This is also
  where a mistyped resource name surfaces: the resource set is GitHub's and it is
  **open**, so a load-time allow-list would reject a resource that exists before it
  rejected a typo.
- **A row this check cannot read is a WARN line for that resource only.** `limit` and
  `remaining` are read as **required** structural fields, and a failure to read either
  is caught per resource, exactly as every aspect of the `github` type isolates a
  repository it could not read. Two things ride on this. An absent `limit` must not
  default to `0` and fall into the rule below — an exhausted budget would then read
  *green* because a key went missing, and the asymmetry is invisible: an absent
  `remaining` fails safe, an absent `limit` fails green. And one malformed row must not
  escape `run()`, because the engine's check-error path is all-or-nothing: every keyed
  line would be replaced by a traceback, and every maintenance pin held against one
  would stop matching.
- **A resource whose reported limit is not positive says nothing** — an `UNDEFINED`
  entry, which is skipped when the node's code is derived. A limit of zero is not a
  budget of nothing, it is the *absence* of a budget, and grading it would paint a
  permanent red on an installation that does not rate-limit at all.

## Consequences

- A deployment adds a second config file per token it wants watched, and nothing
  changes for one that does not: the `github` type is untouched, no node path moves, no
  maintenance pin is affected. The new type is **additive**.
- `GitHubCheck`'s safety-factor guard stays exactly as it is. It is a *decision* the
  check takes about its own run; this check is the *reading*. Merging them would put a
  monitor's threshold and a run's policy in one number.
- The package's rulebook now describes two types, and a third would have to argue
  against this record rather than beside it: what makes these two one package is a
  shared API, client and credential — not a shared vendor.
