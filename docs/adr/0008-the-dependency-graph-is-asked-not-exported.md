# ADR-0008 — The dependency graph is asked, not exported

- **Status:** Accepted
- **Date:** 2026-10-10 (accepted 2026-09-19)
- **Related:**
  [ADR-0016](0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
  (§2, what `sbom_check` asks — this record changes how it asks, not what; §11, why an
  empty dependency graph is red), [ADR-0002](0002-a-read-failure-is-not-a-finding.md)
  (the three faults, read here off a GraphQL answer),
  [ADR-0007](0007-the-budget-is-read-where-it-is-spent.md) (the ledger, which the
  GraphQL budget joins), [ADR-0001](0001-a-second-check-type-in-this-package.md) (one
  client and one credential — this record adds a second dialect behind them, and says
  why that is not a second package),
  [ADR-0015](0015-a-workflow-line-holds-the-newest-run-it-has-read.md) and
  [ADR-0011](0011-conditional-requests-and-the-cache-that-holds-them.md) (the two
  memories the last answers are held like),
  [ADR-0014](0014-a-run-is-its-readings-and-the-estate-is-its-object.md) (§4, the record
  a held graph is weighed in), little-sister ADR-0113 (the read of a check's kept
  readings, which is not where the last answers are held)

A bare ADR number here is this repository's; a reference to one of little-sister's
is always written out, because the two numbering spaces overlap.

## Context

`sbom_check` asks one question of every repository in scope: *can the dependencies of
this repository be checked at all?* A repository whose dependency graph is empty makes
`security_advisories` green for the wrong reason — Dependabot has nothing to say about it,
and never will — which is why an empty graph is graded red rather than amber (ADR-0016
§11). The SBOM export was only ever the **instrument**: the aspect downloaded the whole
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
   the graph the first evening it ran, and red is what ADR-0016 §11 means.
   Manifests that exist but none of them parseable — `parseable: false` on every one read,
   when the count is within the ten asked for — grade **ERROR** too, and the line names
   the cause: `platform-api: 2 manifests, none parseable (package-lock.json exceeds the
   size limit)`. Dependabot cannot alert from a manifest it could not parse, and that is
   the reason this aspect is red at all (ADR-0016 §11); the SBOM export said the same
   thing as an empty `relationships` and could not say why. A repository with more than
   ten manifests has a graph, whatever the first ten say, and grades on `totalCount`
   alone. `sbom_check.ignore` keeps its meaning: a repository named there is not asked.

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
   not name had therefore not been seen when the decision was made; the code reads
   one as *could not read* with the type on the line, and that sentence is what
   would turn it into the red line the day GitHub answers with one.

   **The alias error `timedout` is *could not ask*.** One deployment's instance met it
   for a day and a half, on every one of its twenty repositories at some hour. What its
   log shows of the answer is the line the code wrote for it, *could not read (error:
   timedout)*: a `200` whose `data` is an object, and whose `errors` hold an error with
   the message `timedout`, no `type`, and a `path` that begins with the one alias the
   query carries — under which GraphQL answers `null`, for the repository or for the
   connection beneath it, as it does for a field it errs at. The message says what it is:
   GitHub did not answer in time for one repository's part of a query. Where its time
   limit (decision 1) reaches the request whole, the answer is the `502` or `504` that
   was *could not ask* already. This decision first had the code read an alias error of a
   type the record does not name as *could not read*, so this one was an amber line on
   its repository, and that line, coming or going, is what 80 of the aspect's 81 changes
   of status in that day and a half were — the other was the repository of decision 7
   gaining a dependency graph. It is `TRANSIENT`: a line that grades nothing and that the
   coverage line counts, never a claim about the repository (ADR-0002 §4 and §5). **The
   error is matched as it was seen** — that message, whole, and no `type` — wherever its
   path goes on to after the alias. An alias error of any other type the record does not
   name, and one without a type whose message is anything else, still reads as *could not
   read* with what it said on the line. ADR-0002 §2 has a fault decided by status and
   header and never by message text. A GraphQL alias error has neither, this decision
   reads its `type` in their place, and this one has none: its message is one bare word,
   which stands in for the code GitHub did not send, and it is compared whole, never
   searched for in a sentence.

5. **The guard prices the aspect as what it costs.** The run's pre-run pricing (ADR-0007,
   decision 3) names the path `graphql` for `sbom_check`, with the point value of the
   queries the scope needs — one each — against the `graphql` window the ledger knows,
   in points, instead of one `core` read per repository that this aspect no longer
   makes.

6. **Every stored key stays.** The aspect is `sbom_check`, its ignore list is
   `sbom_check.ignore`, its lines keep the slug `sbom`; a maintenance pin held against any
   of them holds. What changes is prose — the line's sentence, the README's row — which is
   a *Changed* entry, not a break.

7. **A repository GitHub did not answer for keeps its last answer.** A failed read
   forgot a finding: the one repository in that scope without a dependency graph was
   red on a run that read it (decision 2) and was not on a run GitHub did not answer
   for it — nine times one way and eight the other in that day and a half. And *could
   not ask* still turns the aspect amber for a run, on its coverage line, for a fact
   that changes rarely and is asked every few minutes. So the check holds, for every
   repository the aspect asks, **the last dependency graph GitHub answered with and
   when** — the count and the manifests as a reading keeps them (ADR-0014 §4), and the
   time. A repository whose query fails as *could not ask* on a run — the cut above, a
   5xx, a throttle, a query answered with errors and no `data` — is graded on that
   answer while it is younger than **`sbom_check.max_answer_age`**, a duration in the
   aspect's block, **an hour** where a deployment does not say:

   - **its reading is the held graph**, with the time it was read as the record's own,
     `at` — which is `null` on a reading of this run;
   - **its line, where the graph has one, is the line it had** — the same slug, so a
     pin holds, and the same code — ending *as of* that time, written by the library in
     the configured zone and format;
   - **it counts as read**: no *could not ask* line, nothing in the coverage line, and
     nothing in the count on the check's own node (ADR-0002 §6).

   A repository whose last answer is that old or older, or that has none, is *could not
   ask* as decision 4 has it. **Standing does not renew an answer**: the time is the
   answer's own, so an hour after GitHub last answered for a repository its line gives
   way, however often it stood in between. The age is read off the wall clock, since
   how old an answer is is a fact about the world. By the next run an answer is about
   as old as the time between the two, so a value that is not longer than that leaves
   none young enough, and the aspect reads as it did before this decision. That is how
   a deployment goes without the hold: the key takes a positive duration, and zero is
   refused at load.

   **Why an hour, and why the name.** The hour is read off a log. One deployment's log
   of `2026-10-06` and `2026-10-07` has 170 runs of the check in twenty hours, one every
   seven minutes, by six instances, each replaced after about three and a half hours,
   with the error still read as decision 4 first had it. In 67 of those runs the aspect
   was amber for at least one repository GitHub did not answer for — the cut of
   decision 4, on every line the events show — in 27 stretches of consecutive runs:
   nine of one run, six of two, five of three, five of four, one of five and one of
   six. Which repository was cut on the later runs of a stretch the log does not say;
   that the run before a stretch answered for all of them it does, so the oldest answer
   a repository can have stood on is the one from that run, on the stretch's last — in
   the longest, 42 minutes. An hour leaves one of the 67 runs amber, an instance's
   first, which has no last answer; half an hour leaves between one and four, by which
   repositories the two longest stretches cut. One repository's every run the events
   do show, for the fifteen hours it was without a dependency graph and red wherever it
   was read: it was cut nine times in some 125 runs and read on the next run each time.
   So a repository's own silence is mostly one run long, and half an hour would likely
   have done; an hour is the shortest round value that covers the worst the log allows,
   with two runs to spare, and the fact it holds changed once in that day and a half.
   The key says what it bounds, **the age of an answer** — counted from the answer, not
   from the first run that stood on it — and is spelled like `max_pause`, this type's
   other upper bound on a duration. **The hour does not grow with `frequency`**: a check
   that runs hourly or slower keeps no answer until its config says more. It is left
   so because the hour is what the log argues and nothing runs this check slower than
   every fifteen minutes, the library's default; a default that scales — an hour, or a
   few runs where that is longer — is worth building when a deployment runs it so.

   **What an answer is, and when it goes.** Every dependency graph that arrives is the
   repository's last answer, on the first asking or the second (decision 8), and at
   once, whatever becomes of the pass. An answer that is not a graph — `NOT_FOUND`,
   `FORBIDDEN` or the scope error on the repository's alias, a `401` or a `403` without
   a throttle for the query itself — lets the held one go: the last answer is then
   that one, and it is on its own line. An answer this check cannot read leaves it
   where it was. A repository the aspect did not ask on a pass that finished — gone
   from the scope, or named under `sbom_check.ignore` — is forgotten with that pass,
   and an answer too old to stand is forgotten where it is met.

   **The hold is the check's memory**, as ADR-0015 holds a run and ADR-0011 a payload:
   on the check, in this process, keyed by the repository's id, and empty at every
   start — the first run after one has no last answer, and a repository GitHub does
   not answer for on it is *could not ask*. The grading stays pure (little-sister
   ADR-0086 decision 6): the measuring half reads the hold and the clock and hands
   over a reading, and the grading writes *as of* from the time in the record and
   reads nothing else. The other home was the check's kept readings, which a restart
   keeps (little-sister ADR-0113), and it was refused: decision 3 of that record has a
   type use what it kept to decide what to ask its source for and for nothing else,
   and has nothing a type derived from its history written into a new record. Holding
   there would have asked for that decision to be amended, for a subject and a state
   on this reading — stored keys, and one more series for every repository in every
   deployment that keeps series — and for `series_keep` to be set before the hold
   existed at all. Memory leaves that road open; a subject, once shipped, is not taken
   back. A reading that stands is itself what one instance happens to hold, as a held
   run is (ADR-0015); it names no subject, so nothing keeps it.

8. **A repository cut once is asked once more.** At the end of the aspect, when
   every repository has been asked, each one whose query came back with the alias
   error of decision 4 is asked again, in the order they were asked. **A second asking
   is one request**: it is not retried, and no wait is taken for it — so it costs at
   most a request's timeout, and it is made **only while the run has more than one
   request's timeout left**. Only that error is asked again. A 5xx or a connection
   failure has had its second attempt, from the client (ADR-0002 §3); a throttle, or
   errors about the query whole, is GitHub asking for less — and so a second asking
   that GitHub answers with a throttle is the last of the pass: the repositories
   behind it are left unasked. What the second asking says is the repository's reading
   for the run: a graph, another of decision 4's answers, or *could not ask* again,
   where decision 7 takes over. What the second askings spend of the run, the aspects
   behind this one do not have: a run that is short of time is cut short as it is by
   any read, and resumes where it stopped (ADR-0002 §7). The guard does not price them
   (decision 5). Each is one more query, a point where GitHub answers it; what GitHub
   charges for a query it cut has not been measured here, and its changelog of
   `2025-07-21` has a request that timed out count against the primary limit. What a
   cut costs in time is in the log decision 7 reads: the aspect took a median of 22
   seconds on a run with no cut, twenty queries, and each cut added three to six — so
   a second asking costs about a second where GitHub answers it and about five where
   it cuts it again, well inside the request's timeout the run is asked to have left.

   **The trace counts it**: a pass that met a cut writes one `INFO` line with the
   fixed phrase *asked once more* — how many repositories were cut, how many were
   asked again, how many of those GitHub answered — with a graph or with any other
   answer of decision 4's — and how many were left unasked. It is written whatever
   ends the asking. A pass GitHub did not answer everything on writes a second, with
   the phrase *on the last answer*: each repository that stands on one and since when,
   and how many had none to stand on. Whether the second asking earns its requests is
   read off the first line, against a bar fixed before any such line was written: **it
   stays if GitHub answered at least half of what it asked**, summed over the first
   week of the log of the deployment decision 7 reads, once that has taken the release;
   under half it goes in the release after. Half, and not higher, because what it costs
   is small at any share — about a second where it is answered and five where it is cut
   again — so the bar has only to catch an asking that does nothing: under half it buys
   more second cuts than answers. What it is for has narrowed with decision 7. On a run
   with a last answer to stand on, an answered second asking makes the reading the
   run's own and nothing else; on an instance's first run it is all there is between a
   cut and an amber run.

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
- **What the hold costs to see.** For as long as answers stand, GitHub not answering
  this aspect shows on the node only as *as of* on the lines there are — a repository
  with a graph has none — and in the log (decision 8). The key,
  `sbom_check.max_answer_age`, bounds how long that can be.
- **A held graph's record is thirty bytes heavier.** At its heaviest it carries its
  time, to the second: 1635 bytes, where a graph's record weighed 1605 before it carried
  a time — second to a workflow run's 1773, which the type declares (ADR-0014 §4). The
  key is new, and nothing else a deployment stored moves: no `type:` name, no slug, no
  existing key (decision 6). What moves is prose, as decision 6 has it: the held line's
  ending, the README's row, and the aspect's shipped text, which says what *as of* means
  where the line is read.

## Alternatives considered

- **The asynchronous SBOM pair.** Rejected for the three costs in the context: a
  content-generating `POST` per repository against a per-user cap of 500 an hour that the
  deployment's pipelines already draw on; a redirect that must be followed without the
  token; a *not yet* answer that needs memory across runs. All of it to learn whether a
  list is empty.
- **Drop the aspect and let `security_advisories` carry the warning.** Rejected: the
  Dependabot endpoint can say the feature is off, but it cannot see an *empty* graph on a
  repository with alerts enabled, and that silent case is what ADR-0016 §11 exists for.
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
- <https://github.blog/changelog/2025-07-21-including-timeouts-in-primary-rate-limits>
  — a request that timed out counts against the primary rate limit.
- <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api> —
  content-generating requests, 80 a minute and 500 an hour.

