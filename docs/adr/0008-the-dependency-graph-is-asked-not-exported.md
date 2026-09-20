# ADR-0008 — The dependency graph is asked, not exported

- **Status:** Accepted
- **Date:** 2026-09-19
- **Related:** [ADR-0003](0003-an-aspect-is-one-question-asked-of-the-whole-scope.md)
  (what `sbom_check` asks — this record changes how it asks, not what),
  [ADR-0004](0004-a-finding-grades-the-repository-does-not.md) (§4, why an empty
  dependency graph is red), [ADR-0002](0002-a-read-failure-is-not-a-finding.md) (the
  three faults, read here off a GraphQL answer), [ADR-0007](0007-the-budget-is-read-where-it-is-spent.md)
  (the ledger, which the GraphQL budget joins), [ADR-0001](0001-a-second-check-type-in-this-package.md)
  (one client and one credential — this record adds a second dialect behind them, and
  says why that is not a second package)

A bare ADR number here is this repository's; a reference to one of little-sister's
is always written out, because the two numbering spaces overlap.

## Context

`sbom_check` asks one question of every repository in scope: *can the dependencies of
this repository be checked at all?* A repository whose dependency graph is empty makes
`security_advisories` green for the wrong reason — Dependabot has nothing to say about it,
and never will — which is why an empty graph is graded red rather than amber (ADR-0004
§4). The SBOM export was only ever the **instrument**: the aspect downloaded the whole
SPDX document of each repository, `GET /repos/{owner}/{repo}/dependency-graph/sbom`, and
looked at one thing, whether `relationships` was non-empty; a `404` read as no graph.

That instrument is going away, and it was never a good one. GitHub computed the export
synchronously under a ten-second limit, and large dependency trees hit it: the 500s that
made this aspect's amber lines routine were *Failed to generate SBOM: Request timed out*,
one such answer per large repository per run, each retried once. GitHub moved SBOM
exports to asynchronous computation, deprecated the synchronous endpoint on `2026-05-12`
and removes it on `2026-11-13`; from that day the aspect reads nothing.

The replacement GitHub points to is a pair: `POST …/dependency-graph/sbom/generate-report`
answers `201` with a URL, and `GET …/sbom/fetch-report/{uuid}` answers `202` while the
report is being computed and `302` to a temporary download URL when it is done, the report
retained for up to a week. For a yes-or-no question about the graph that pair costs three
things this package does not otherwise pay. The `POST` is a **content-generating
request**, which GitHub's secondary limits cap at 80 a minute and 500 an hour **per
user**, not per token — one deployment measured in September 2026 spends one token from
two instances, and nineteen repositories at their cadence would have put some 320 of
those 500 into SBOM generation alone before the pipelines that share the token add
theirs; so generation could not happen every run, and the aspect would have needed a
per-repository memory of what was requested when and where to fetch it. The `302` leads
to another host, and `urllib` forwards the `Authorization` header on a redirect, so
following it as every other read here does would hand the token to a download host; the
client would have to read the `Location` itself and fetch it unauthenticated. And the
`202` is a read that answers *not yet*, a shape no other read in this package has, inside
a run that has a deadline and a pause budget.

The question the aspect asks has a direct answer. GitHub's GraphQL API exposes a
repository's dependency manifests as `Repository.dependencyGraphManifests`, a connection
with `totalCount` and, per manifest, `filename`, `parseable` and `exceedsMaxSize` — in the
current schema, with no preview or deprecation notice on the field or the objects. One
request can carry the question for every repository in scope, aliased; GraphQL's primary
budget is 5,000 points an hour per user, the point value of a query is the requests it
would take as REST divided by a hundred and rounded, at least one, and the answer arrives
with the same `x-ratelimit-*` headers every REST response carries, under the resource
`graphql` — a budget the `github-rate-limit` node watches by default. The endpoint is
`POST https://api.github.com/graphql`, authenticated as the REST reads are. That the
deployment's classic tokens may read the field was measured before this record was
accepted.

## Decision

1. **The aspect asks GraphQL whether the repository has dependency manifests.** One
   query **per repository**,
   `repository(owner:, name:) { dependencyGraphManifests(first: 10) { totalCount nodes {
   filename parseable exceedsMaxSize } } }`, built as an aliased query so that a
   reader who ever wants more than one repository in a query changes one number. Each
   query is **one point** by GitHub's own formula — the requests it stands for, a
   lookup and a connection, divided by a hundred and rounded, is nothing, and the
   minimum is one — and one point is what every answer measured: `1 used`. A scope of
   nineteen is nineteen points a run, some eighty an hour on a budget of five thousand,
   in place of one REST read per repository on a hundred-a-minute budget plus a retried
   500 for every large tree.

   One and not fifty, and the number is two measurements, not a taste. This record
   first said fifty, chosen for the size of the answer; the first evening it ran, in one
   deployment's log of `2026-09-16`, said otherwise twice. GitHub ends any GraphQL
   request it has not answered in ten seconds with a `502` or `504` — its documentation
   says so — and the time it takes is the repositories': a query for nineteen answered
   in nine to eleven seconds and was cut in twenty of twenty-nine runs, the retry too in
   fourteen, which left the aspect blind for the run — a worse reading than the
   export's 500 per large repository it replaced. Ten a query, the first correction,
   ended the blind runs but not the risk: a query of ten still took four to ten seconds,
   and in each organization one chunk took twice as long as the other, so a few heavy
   repositories dominate whichever chunk holds them and one query in twenty still
   touched the limit. Since a query costs one point whatever it carries, sharing one
   saves nothing; alone, no query is slower than its own repository, a cut costs exactly
   that repository's line, every repository stands on its own as it does in every other
   aspect, and there is no chunk size left to move when the scope grows. The price is
   round trips: about one more per repository, some six seconds on a run with hundreds
   to spend.

2. **What grades stays what it was, and says more.** `totalCount` of zero is no
   dependency graph and grades **ERROR**, as a `404` and an empty `relationships` did —
   and, measured, stricter than the export ever was in fact: an SPDX document carries
   the relationship that describes itself, so `relationships` was never empty for a
   repository the export could be generated for, and the old line fired on a `404`
   alone. In one deployment two repositories read green under the export and red under
   the graph the first evening it ran, and red is what ADR-0004 §4 meant.
   Manifests that exist but none of them parseable — `parseable: false` on every one read,
   when the count is within the ten asked for — grade **ERROR** too, and the line names
   the cause: `platform-api: 2 manifests, none parseable (package-lock.json exceeds the
   size limit)`. Dependabot cannot alert from a manifest it could not parse, and that is
   the reason this aspect is red at all (ADR-0004 §4); the SBOM export said the same thing
   as an empty `relationships` and could not say why. A repository with more than ten
   manifests has a graph, whatever the first ten say, and grades on `totalCount` alone.
   `sbom_check.ignore` keeps its meaning: a repository named there is not asked.

3. **One client, a second dialect.** `GitHubClient` grows `graphql(query, variables)`: a
   `POST` through the library's `fetch` to the API's GraphQL endpoint — `{api_url}/graphql`
   for GitHub, and for a GitHub Enterprise Server `api_url` ending in `/api/v3`, its
   sibling `/api/graphql` — with the headers, the deadline, the retry and the throttle
   reading every REST request here has. The response's budget headers feed the ledger as
   any response's do (ADR-0007, decision 1), under the resource GitHub names, `graphql`,
   on the reduced path `graphql`. Nothing else in the package learns GraphQL; the aspect
   hands the client a query and reads a JSON answer.

4. **A GraphQL answer is read as ADR-0002 reads a REST one.** An HTTP status that is not
   a success is the three faults as for every read: a 5xx or a connection failure is
   *could not ask*, a throttle is *not now*, a `401` is *may not*. A `200` whose `errors`
   name one alias is about **that repository alone**: `FORBIDDEN` or an insufficient-scope
   error is the token being told no about it, and grades as a permission answer grades
   today; `NOT_FOUND` is a repository that left between discovery and the query, a line
   that grades nothing. A `200` with `errors` and no `data` is the query failing whole,
   and the aspect says it could not ask — the coverage line, never a claim about any
   repository. What a repository whose dependency graph is **switched off** answers
   with was measured before the change shipped, against a personal account of
   fifty-eight repositories, several of them with the graph switched off by their
   owner's word: every alias answered, fifty-two with `totalCount: 0` and not one with
   an error of its own. A switched-off graph is a zero count, the same reading as a
   graph with no manifests, and it grades red as decision 2 says — which is right,
   because Dependabot can alert from neither. An alias error of a type this record does
   not name has therefore not been seen; the code reads one as *could not read* with
   the type on the line, and that sentence is what would turn it into the red line the
   day GitHub answers with one.

5. **The guard prices the aspect as what it costs.** The run's pre-run pricing (ADR-0007,
   decision 3) names the path `graphql` for `sbom_check`, with the point value of the
   queries the scope needs — one each — against the `graphql` window the ledger knows,
   in points, instead of one `core` read per repository that this aspect no longer
   makes.

6. **Every stored key stays.** The aspect is `sbom_check`, its ignore list is
   `sbom_check.ignore`, its lines keep the slug `sbom`; a maintenance pin held against any
   of them holds. What changes is prose — the line's sentence, the README's row — which is
   a *Changed* entry, not a break.

## Consequences

- The synchronous SBOM export leaves this package before GitHub removes it, and with it
  the 500s that were most of this aspect's amber and most of the `github` node's pauses;
  `dependency_sbom` stops appearing in the ledger, because nothing here spends it.
- **`graphql` becomes a budget this package spends** — a few points a run — so the
  `github-rate-limit` node's default set, which watched it because *a token is rarely
  used by one tool alone*, now watches it for this package's own sake too, and the guard
  prices it.
- **The token needs no new permission.** On a classic token the dependency graph reads
  under the same `repo` scope the SBOM export needed; on a fine-grained token the export
  needed *Contents* (read), and GraphQL requires for a field what the REST read of the
  same data requires. The README's account of what the token needs holds unchanged.
- The line changes its words: *missing SBOM* becomes *no dependency graph* with the
  count and the cause; the README's row names the query rather than the export.
- ADR-0001 said what makes two types one package is *a shared API, client and
  credential*; it now reads one host, one client, one credential, with two dialects
  behind the client. The argument it made — a second package would duplicate the client
  and a release pipeline for one endpoint — is exactly the argument against a second
  client for one query.

## Alternatives considered

- **The asynchronous SBOM pair.** Rejected for the three costs in the context: a
  content-generating `POST` per repository against a per-user cap of 500 an hour that the
  deployment's pipelines already draw on; a redirect that must be followed without the
  token; a *not yet* answer that needs memory across runs. All of it to learn whether a
  list is empty.
- **Drop the aspect and let `security_advisories` carry the warning.** Rejected: the
  Dependabot endpoint can say the feature is off, but it cannot see an *empty* graph on a
  repository with alerts enabled, and that silent case is the one ADR-0004 §4 exists for.
- **Keep the synchronous endpoint until it fails.** Rejected: it fails on `2026-11-13`
  for every repository at once, and it fails today for every large one.
- **Read the repository object's `security_and_analysis`.** Rejected: it carries the
  scanning switches, not the dependency graph, and nothing about whether the graph has
  content.
- **A second client for GraphQL.** Rejected by ADR-0001's own argument: one host, one
  credential, one retry and throttle policy — a query is a request like the others, and
  the ledger wants to see it where it sees the rest.

## Sources

- <https://docs.github.com/en/graphql/reference/repos> and
  <https://docs.github.com/en/graphql/reference/dependency-graph> —
  `Repository.dependencyGraphManifests` and the manifest objects.
- <https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api>
  — the point formula, the node limit and the `graphql` budget.
- <https://docs.github.com/en/graphql/guides/forming-calls-with-graphql> — the endpoint,
  the method and which tokens authenticate.
- <https://docs.github.com/en/rest/dependency-graph/sboms> — the export, the asynchronous
  pair, and the *Contents* (read) permission.
- <https://github.blog/changelog/2026-05-12-synchronous-sbom-api-deprecated/> — the
  deprecation and the removal date.
- <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api> —
  content-generating requests, 80 a minute and 500 an hour.

