# ADR-0002 — A read failure is not a finding about the repository

- **Status:** Accepted
- **Date:** 2026-08-13
- **Related:** [ADR-0001](0001-a-second-check-type-in-this-package.md) (which
  applied the *wording* half of this — "could not ask" — to `github-rate-limit`,
  and left the grading half open for this type by name),
  little-sister ADR-0042 (an entry carries its own code), little-sister ADR-0036
  (a keyed reason is a member), little-sister ADR-0040 (a failing check is
  all-or-nothing — the shape this record works around), little-sister ADR-0050
  (a slug is an identifier, never a position), little-sister **ADR-0058** (one
  transport policy, and any client — the vocabulary this record's machinery moved
  into)

> **Update (2026-08-15):** the machinery below is the library's now, and one of the
> classifications was **wrong in a way that mattered**. Every reading this record
> decided still holds; what changed is where the code lives, and that a throttle is
> no longer read as a refusal.
>
> **1. A rate limit is *not now*, and this shipped as *no*.** §2 sets `transient` to
> `500 <= code < 600`. GitHub answers a rate limit — primary or secondary — with
> **403 or 429**, so every throttle landed in the *answer* column: an amber
> `could not read` line naming a repository that was in perfect health, and no retry.
> ADR-0001's own Update predicted the shape of this
> ("the signature of a secondary limit") and closed on *GitHub's answer may carry a
> `retry-after` header, which nothing here reads yet*. It is read now.
>
> The status cannot decide it and never could, which is the whole reason this reading
> is **ours** rather than the library's: GitHub documents both 403 and 429 for both of
> its limits, and a bare 403 equally means *this token may not see it*. So the headers
> decide, in GitHub's own precedence — `retry-after` first, then
> `x-ratelimit-remaining: 0` with `x-ratelimit-reset` (an epoch stamp, read against
> the wall clock and not the run's monotonic one), and otherwise a 60-second floor.
> A **bare 429** takes that floor, because a rate limit is the only thing GitHub sends
> that status for. A **bare 403 does not**: with no throttle header on it, it is still
> the permission answer, and reading it as *not now* would retry every unreadable
> repository in the scope and turn a real permission problem grey.
>
> **Never from the body**, though GitHub's throttle bodies say so in prose. That is
> §2's rule, on the one classification most tempting to break it for.
>
> A throttle is `TRANSIENT` and carries the number GitHub named. What is done with the
> number is bounded by the run: a two-second secondary limit is absorbed, and a
> twenty-minute primary limit is **refused and re-raised**, because a wait outliving
> the check's budget is not a request layer's to take. The floor is deliberately
> longer than a normal run's remaining budget, so it mostly *prevents* a retry rather
> than scheduling one — pressing a service that has just complained about volume is
> how an integration gets itself blocked.
>
> **2. Three faults, not two, and the third is new information on a line.** The
> library's `Fault` is *transient* / *answered* / *malformed*, and §2's last row —
> "a malformed payload (no status)" — now has a name of its own instead of sharing the
> not-transient column with a 404. It still grades WARN, which is the same reading
> this record made; what changes is that a line can say *could not read* about an
> answer that arrived unusable and mean something distinct from *the thing is absent*.
> One case moved to it: a body that is not JSON used to be reported as transient and
> **retried**, spending a second request to be handed the same bytes.
>
> **3. §1's stated caveat is closed.** This record named a hole it did not take on —
> both budgets "hold against a slow or dead server and neither holds against a
> deliberately dribbling one", closing which "needs a read loop with a clock of its
> own". The library has that loop: a response body is read in bounded chunks against
> a byte cap *and* the run's deadline. This client passes its deadline in, so the hole
> is shut here.
>
> **4. What is left of this client is GitHub.** The request, the two budgets and the
> retry are all named by little-sister ADR-0058 and imported:
> `Deadline` replaces our `_Deadline`, `DeadlineExceeded` replaces
> `RunDeadlineExceeded` — still deliberately outside the error hierarchy, for exactly
> the reason §7 gives — `GitHubError` becomes a `RemoteError` subclass keeping its own
> messages and every `except` clause, `fetch` replaces the hand-rolled `urlopen` call,
> and `ask` replaces the retry loop with the same veto §3 describes. What stays is what
> only a GitHub client can know: the auth and API-version headers, the `Link` walk, and
> the throttle reader above.
>
> Two side effects worth naming. Requests now identify as `little-sister/<version>`
> rather than `Python-urllib/<x.y>` — a name a support thread can do something with,
> and one an outbound filter is less likely to answer with an uninterpretable 403. And
> the per-request clamp is the library's `Deadline.budget` rather than our `_budget`,
> so it is one implementation for every check in the family instead of one per package.
>
> **5. This raises the install floor.** The names above are promised by a
> little-sister release, and the floor in `pyproject.toml` has to name it — a floor
> that does not is an `ImportError` at startup for anybody who installs this package
> against an older library. The two releases go out together.

## Context

GitHub's SBOM endpoint fails intermittently with a 500 whose body is its own
explanation — `Failed to generate SBOM: Request timed out.` The aspect turned that
into a member line and lifted an otherwise green `sbom_check` to WARN:

```
warn — platform-a: could not read (HTTP 500 for …/dependency-graph/sbom:
{"message":"Failed to generate SBOM: Request timed out.", …})
```

Read as a monitoring statement that is wrong. `sbom_check` answers one question —
*does this repository have a dependency graph?* — and a 500 is not an answer to it.
What happened is that **this check could not ask**, which is a fact about the check
and its upstream, not about the repository. The operator sees a repository named in
amber and has to read to the end of a truncated JSON body to find out that the
repository is fine and GitHub was busy.

It is not one endpoint's problem: every aspect reads the API the same way, and the
same 500 has been seen from `actions`.

**And investigating it turned up a second, larger defect.** The obvious suspicion
was our own timeout, and it was wrong twice over. That 500 is a *completed HTTP
response* — GitHub's own backend giving up — so no client-side timeout affects it.
But `timeout:` — the check's one duration, described in the library only as "how
long to wait" — was being handed to `urlopen` as its **per-socket-operation**
timeout: spent afresh on each of the several hundred requests a run makes, and
therefore bounding nothing. The engine does not bound a run either — it calls
`run()` with no deadline of its own, and the comment beside that call ("a hanging
check that hit its timeout should show the full wait") says honouring the value is
the check's job. Reading it as the **run's** budget is this record's decision, not a
rule quoted from elsewhere: the library's own `http` check still spends it the other
way, on a check that makes exactly one request, where the two readings coincide. So a slow GitHub
could hold a run past its own frequency indefinitely, and a wedged check reports
*nothing at all*, which is worse than the amber line this record started from.

## Decision

### 1. The two budgets are two numbers

`request_timeout:` (new, default **15s**) bounds **one request**. `timeout:` is the
**whole run's** deadline, checked before every request — including every page of a
paginated read, which is where one call quietly becomes twenty.

A request's socket timeout is **clamped to what is left of the run**. Without the
clamp a 15-second request could start with two seconds of budget left and overrun
`timeout:` by thirteen, which would make the deadline a suggestion rather than a
bound.

This is the shape the library already arrived at in its link prober — and its module
docstring puts the reason more sharply than this record can: a pass carries a
deadline *"because `REQUEST_TIMEOUT` bounds one socket operation and not one
request: a server that dribbles a byte every four seconds never times out"*.

**That caveat applies here too, and neither budget repeals it.** `request_timeout`
is handed to `urlopen`, which bounds a socket operation; a response trickling bytes
below that rate is not cut off by it, and the run deadline is only consulted
*between* requests. So both bounds hold against a slow or dead server and neither
holds against a deliberately dribbling one. Closing that needs a read loop with a
clock of its own, which is a larger change than this record takes on — it is named
here so the next person does not have to rediscover it.

### 2. A failure is transient or it is an answer, and the status decides

Set on `GitHubError` at the point it is raised, **by status, never by message text**:

| what came back | transient | what it means |
|---|---|---|
| **5xx**, or a transport failure | yes | we could not ask |
| **404** | no | GitHub answered: the thing is absent |
| **401**, **403** | no | GitHub answered: this token may not see it |
| a malformed payload (no status) | no | asking again cannot change a shape |

The last row is why this cannot be inferred from `status is None`.

### 3. Only a transient failure is retried, once

One extra attempt, after a 1-second backoff, and **only** while the run has more
than that backoff left — a retry certain to be cut off before it is even made spends
the wait to learn nothing. The guard is deliberately that crude: it does not also
reserve time for the retried request, so a retry can still begin with almost no
budget and be clamped to a socket timeout it cannot meet. One retry, because it separates "GitHub
hiccupped" from "GitHub is having a bad ten minutes", which is the whole question
the reading has to answer; a second would buy a sharper line with the run's
remaining time.

So what *usually* reaches a dashboard is a failure GitHub gave **twice** — and the
reading must not claim more than that. Two paths produce a quiet line after a single
ask: a run too short to afford the backoff skips the retry, and a request clamped to
the run's last fraction of a second times out on a budget of our own making. That is
why the node's sentence in §6 says *GitHub did not answer* and stops there; the
stronger claim would be false exactly when the run is already in trouble.

### 4. A transient failure is a line that grades nothing

It becomes an `Entry` with `code=UNDEFINED` (little-sister ADR-0042): displayed,
keyed, pinnable — and skipped when the node's code is derived. That is exactly the
sentence this record needs, *"could not ask GitHub"* said out loud without claiming
anything about the repository.

`report` was the other candidate and is rejected: a line that never grades and
cannot be pinned is precisely the "second, quieter reason channel" that field's own
rule forbids.

**The cost, and it is the bulk of the change.** Declaring a node code *and* handing
over coded entries is refused by the library, so for an aspect to carry one
`UNDEFINED` line, **every** line it emits must carry its own code and the aspect's
code becomes derived. All seven aspects hand over `Entry` objects now. The derived
worst-of equals the previously declared code in every existing case, and the
existing suite pins that: every one of its verdicts survives unchanged, and the only
edit it needed was mechanical — a derived result carries its code in `stored_code`,
so the assertions moved from `.code` to the field that holds the answer either way.
One test was replaced rather than translated, because its claim is the one this
record overturns.

### 5. The aspect grades its own coverage gap

The repository lines grade nothing by decision 4, so an aspect that reached nothing
at all would derive `UNDEFINED` from an entry set of nothing but `UNDEFINED` — and
the roll-up **ignores** an `UNDEFINED`, which is how *I could not look* came to read
as silence rather than as trouble.

So an aspect that could not ask about anything emits one line of its own, and that
line is `WARN`: `GitHub did not answer for 1 of 40 repositories`. It appears **only**
when something could not be asked about, so a healthy aspect renders exactly as
before, with no line and no noise. Two properties are load-bearing:

- **The count is the could-not-ask kind, and the sentence names the cause.** A
  repository GitHub *refused* is already amber on its own line; counting it here
  would grade one condition twice and make the number mean two things. The
  denominator is every repository the aspect tried, so the refused one is not
  quietly written out of the scope either.
- **It is a claim about coverage, not about a repository.** That is what makes it
  honest on the aspect: coverage is the aspect's own business, and no repository is
  named.

This is also what fixes the **severity-band** aspects, and without changing what a
band means. Their read failures sit on the *container*, whose own code used to be
`UNDEFINED` and therefore skipped in favour of the watched bands — rendered `OK` when
empty, so that a band's silence stays visible. `security_advisories` and
`code_scanning_alerts` consequently showed green when they had read nothing at all.
The coverage line grades the container instead, so the bands never have to tell
*empty* from *unread* — a question this record deliberately does not open.

### 6. The outage is visible on the check's own node, once

If the repository lines grade nothing, something must, or an hour of 5xx reads
exactly like an hour of everything being fine.

That statement is about **coverage**, and coverage is already this node's job —
`expect_min_repos` lives there. So the check's node says
`3 repository reads could not be completed this run — GitHub did not answer`,
aggregated across aspects and stated **once**. The *run's* total belongs here and
nowhere else: on an aspect it would be written onto up to seven nodes for one outage.
Decision 5's line is a different sentence — what **this aspect** could not cover, in
this aspect's own denominator — and the two do not duplicate: one says how much of
the run was blind, the other says which question could not be answered.

Permission errors are **not** counted there. They are already amber on the line
where they happened; counting them here would report one condition twice and make
the node's number mean two different things.

### 7. The deadline keeps what finished

When the run's budget runs out, the aspects that completed report normally, the rest
are simply absent, and the node says
`run cut short after 63s of its 60s timeout — 4 of 7 aspects reported`.

Partial truth beats no truth, and the engine's own failure path is all-or-nothing
(little-sister ADR-0040), so letting the deadline escape `run()` would throw away
every aspect that read perfectly well before GitHub slowed down. The one exception
is a deadline hit during **discovery**: nothing was read, so there is nothing
partial to keep.

The deadline is checked in **two** places and both are load-bearing: in `run()`
before each aspect, so an aspect whose budget is gone is never started; and in the
client, so a long aspect is cut off partway rather than running to the end of forty
repositories past the deadline.

### 8. `pull_requests` is isolated per repository, like the others

Its whole loop sat inside one `try`, so the first repository's failure returned the
aspect as ERROR and the other repositories' open pull requests were never looked
for. That is the all-or-nothing shape this record rejects, one level down.

### 9. Discovery follows the same rule as everything under it

> Added 2026-08-15, with decision 5's rewrite. Discovery was the one read still
> graded by where it happened rather than by what came back.

A run that cannot list the repositories has the same two cases as a run that cannot
read one, and decision 2 already separates them. A **transient** discovery failure is
*we could not ask*: the check reports `WARN` on its own node and **no children at
all**, which leaves every aspect exactly as the last good run left it (little-sister
ADR-0007 does not prune omitted children). A working dashboard is not replaced by one
red line for GitHub's bad minute, and the node is the one place that says why.

A discovery failure GitHub **answered** stays `ERROR`: an owner or team that is not
there, or a token that may not look, is a defect in the deployment and somebody has
to fix it. So does a malformed payload, for decision 2's reason — asking again cannot
change a shape.

What this does *not* do is carry a reading forward. Nothing is re-emitted and nothing
is marked; the aspects simply are not written this run, and freshness
(little-sister ADR-0005) is what eventually says the readings are old. Re-publishing
half a scope is a different question and is not decided here.

## Consequences

- **No config file needs editing, but one existing key changed meaning.**
  `request_timeout:` is new and optional. `timeout:` is not new, and it now bounds
  the run rather than each request — so a value chosen for a single request will cut
  runs short, and a deployment should re-read it against its scope.
- **No node path moves**, and the per-repository slugs are unchanged — including the
  `…-unreadable` lines, which keep their key and change only their code and wording.
  One slug *does* change: `pull_requests`' failure line was prose, so its slug was
  derived from the wording; it is the keyed `<repo>-unreadable` now, like every other
  aspect's. A pin held against that derived slug does not carry over, and none could
  — a derived slug invalidates itself on a re-wording by design.
- **A dashboard gets quieter, deliberately.** A repository that GitHub could not be
  asked about is no longer amber. The compensating alarm is on the check's node, and
  it fires on a failure that survived a retry.
- **A test's claim was overturned**, not patched:
  `test_an_aspect_that_could_not_run_at_all_stays_prose` pinned the old
  `pull_requests` shape and is replaced by one that pins the new one.
- **This is the package's half of a rule that is not only ours.** `little-sister-wiz`
  reads a different API through the same shape, and *what a read failure is* belongs
  in the library's check-authoring guidance where a second check type can find it.
  Proposing it there is the follow-up this record leaves open — with the caveat that
  the library would also have to say something about `UNDEFINED` on an aspect, which
  is the one piece of this that a check author would not guess.
