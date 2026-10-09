# ADR-0014 — A run is its readings, and the estate is the check's object

- **Status:** Accepted
- **Date:** 2026-10-10 (accepted 2026-09-20)
- **Related:** [ADR-0013](0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md)
  (the one reading besides the estate that has a subject),
  [ADR-0002](0002-a-read-failure-is-not-a-finding.md) (the coverage the readings
  carry),
  [ADR-0016](0016-an-aspect-asks-the-whole-scope-a-finding-grades-and-a-workflow-is-a-node.md)
  (§4, the node's own reading; §18, the workflow's file a run's record carries),
  [ADR-0007](0007-the-budget-is-read-where-it-is-spent.md) (the ledger
  `github-rate-limit` reads), little-sister **ADR-0086** (the split, and the purity this
  is written against), little-sister **ADR-0085** (what a subject is for, decision 2),
  [ADR-0012](0012-the-actions-line-carries-what-it-read.md) (the first line to carry
  what it read, and the other aspects it left to their own surfaces), little-sister
  **ADR-0082** (a line's `data` and `subject`, and the three names it reads as an
  instant), little-sister **ADR-0087** (a series, which finds the line a reading became
  by the record it carries, and the identity a budget's reading does not name),
  little-sister **ADR-0075** (what a type declares it costs, a record's weight among
  it), [ADR-0008](0008-the-dependency-graph-is-asked-not-exported.md) (decision 7, a
  graph that stands on its last answer, and the time it carries)
- **Register:** [`../decisions.md`](../decisions.md)

A bare ADR number here is this repository's; a reference to one of little-sister's is
always written out, because the two numbering spaces overlap.

## Context

little-sister ADR-0086 replaced `run()` with two halves: `measure()` reads the world
and hands back `Measurement`s, and `grade(measurements, now)` builds the tree out of
them and out of nothing else — not the world again, not the clock, not an attribute
the measuring half left behind. This package is the first to meet that seam with a
check that reads many objects: a `github` run reads an estate of repositories through
up to eight aspects, and writes a line per finding.

ADR-0013 settled what an `actions` line is about. This record settles the rest: what
the other readings are, which of them carry a subject, what the estate is, and how
free text stays inside the 2 KB `record_limit` a reading is weighed against.

## Decision

### 1. One reading per thing the run read

A `github` run hands back, in this order:

- **the estate** — one reading about the run as a whole (§3);
- **the roster** — one reading per repository discovery found, which is what the
  node's report and the Advanced Security scope line are written from;
- **each finished aspect's readings**, in the order it read them — one per pull
  request, issue, Dependabot alert, code-scanning alert, secret-scanning alert,
  dependency graph and workflow, and one per repository the aspect could not read or
  was told is gone.

A dependency graph that **stands** for a repository GitHub did not answer for — its last
answer, which the check holds
([ADR-0008](0008-the-dependency-graph-is-asked-not-exported.md), decision 7) — is a
dependency graph's reading, with no subject (§2), in the place of the one that would say
the repository could not be read.

Every record names the **`aspect`** it was read for — `null` for the estate and the
roster — and its **`kind`**, the shape the rest of it has; a reading about one
repository names it as `repository`, the full name a human reads, and
`repository_id`, the one a rename cannot move. The grading sorts the run by those
fields and writes each aspect's child from its own readings.

**An aspect the deadline or the pause budget cut off contributes none.** It read a
prefix of the repositories, and the grading must not present a prefix as the
aspect's answer — so that child is absent, exactly as it was, and the tree keeps its
last reading (little-sister ADR-0007).

### 2. A reading has a subject only where it is to have a history

A measurement is two things: the grading's input, because the grading may read only
records, and the stuff of a series. Its subject serves only the second
(little-sister ADR-0085 decision 2). A pull request has to be a
reading so that its line can be built purely; it needs no subject until somebody wants
its history, which is its own decision against its own surface.

So the findings, the roster and the unreadable repositories carry **no subject**.
Three kinds do: the **estate**, an **`actions`** run or disabled line (ADR-0013), and a
**`github-rate-limit`** reading of one resource (§6).

### 3. The estate is the object the check watches

Not the owner's login. Two checks can watch one account through two filters — a
prefix each, a team each — and those are two estates; a subject naming both as the
login would say two different things are one, which is the quiet untruth a subject
exists to prevent. The subject is the estate **as its configuration draws it**:

```
<owner>[;team=<team>][;prefix=<prefix>][;archived=yes][;forks=no][;host=<host>]
```

The login first, then only the filters a configuration set away from their defaults,
in that fixed order. `archived` and `forks` belong to it for the reason `prefix` does —
they change which repositories are the estate — and `host` separates two GitHubs,
written only off GitHub's own API. A login, a team slug and a repository name can hold
neither `;` nor `=`, so the parts stay apart. It is declared **at construction**, out
of the configuration (little-sister ADR-0086 decision 4), so a run that raises is
recorded against the estate it failed to reach.

The estate reading carries what the run could reach and what it cost: whether
discovery failed and how, how many repositories were in scope and whether private ones
could be seen, whether the guard skipped the run and why, which aspects finished and
how many reads each got answered, the run's could-not-ask total, the cut-short
sentence, the seconds paused by cause, and the requests made, the free ones, what the
guard priced the run at and what the conditional cache holds — and, beside the cache,
what the `actions` hold keeps and how often this run's answers went back on it
([ADR-0015](0015-a-workflow-line-holds-the-newest-run-it-has-read.md) §4). It has **one
shape on every run**: a field the run did not come to stands as `null` or zero.

### 4. Free text is clipped once, in the measuring half

A title, a summary, a rule, an error: at most 300 characters and then 600 of the bytes
the seam weighs a record in, by the library's `clip` (little-sister ADR-0086 decision
7). A field that shares its record gets less. A workflow's name gets 150 bytes, and its
file as many, since the record also holds a branch and two run blocks. Every record's
copy of a branch — a run line's, and the default branch the line names when none of the
configured branches has a run — gets 300, cut at the end like the rest: no plain ASCII
branch GitHub allows reaches it, and one past 49 letters of Cyrillic or 24 emoji does.
The subject keeps the branch as it did (ADR-0013 §2), and a run line's slug is built
from what the subject spells, so the clip moves no pin but one: a branch too long for a
subject as well, which the subject spells as its digest, has its slug built from that
digest — moved once, where a slug built from the clipped copy would put two branches
alike up to the clip on one line. The line is written from the text the reading kept, so
the two never disagree.

A dependency manifest's path gets 70 bytes, since ten of them share a record, and it is
cut **from the front**, behind a `…` weighed with what it keeps, at a code point as
`clip` cuts. Manifests in one repository differ near their end —
`…/api/requirements.txt` beside `…/worker/requirements.txt` — and the whole path is one
click away, on the graph's page the line links to; the mark is there because a path that
lost its start would read as a whole path that starts somewhere else. The cost is on the
red line, which names each unparseable manifest: a path past 68 characters loses its
start there, and real paths do, not only pathological ones. The cut lives in this
package, beside its one use; if a second type ever needs it, it moves to the library
then.

**The heaviest reading is a workflow run's, and the type declares it past the library's
warning line.** With the longest owner and repository names GitHub allows, ten-digit
repository and workflow ids, the workflow's name and its file at their clips, a branch
of 255 `"` — which git allows, and which the seam's JSON writes in 512 bytes — at its
300, `waiting`, two run blocks at their longest — eleven-digit run ids, a two-digit
attempt, a six-digit run number, `startup_failure` and `in_progress`, the completed
one's duration across every instant an ISO date can hold — and the record's own time,
the start of the run it names ([ADR-0012](0012-the-actions-line-carries-what-it-read.md)
§3), it weighs 1773 bytes. A dependency graph that stands on its last answer comes next:
with the longest names, a ten-digit repository id, a manifest count at a GraphQL `Int`'s
largest, ten manifests with neither flag set, each path at its 70 bytes, and the time
GitHub answered with it, to the second
([ADR-0008](0008-the-dependency-graph-is-asked-not-exported.md), decision 7; §8), it
weighs 1635, and 1617 on the run that read it, its time `null`; the heaviest pull
request is 1090. 1773 is 87 % of the default `record_limit` of 2048, past the 80 % at
which every start says that a declared record is close to its limit — a warning in the
log and a fact on the engine's report, never a line (little-sister ADR-0075 decision 5).
The declaration stands there on purpose, and the clips are not tightened to bring it
under: a deployment that wants a quiet start sets its `record_limit` to 2217 or more,
which is its own to decide (little-sister ADR-0082). `expected_record()` answers the
1773, so startup holds it against a deployment's `record_limit` and refuses one set
lower by name, before any run meets that reading (little-sister ADR-0075). What makes a
reading heavy is bounded by GitHub or clipped here, but for its ids and counters, which
are as long as GitHub's are today, and for one field: a disabled workflow's link, which
GitHub writes with the default branch and the workflow file's name in it, and which
nothing here shortens. One test rebuilds the run and holds the declaration to it, one
rebuilds the graph and holds it under the run, and another holds the declaration past
the library's line and inside the limit, and says what a limit of 2217 changes,
importing the library's `AMBER_SHARE` and `RECORD_LIMIT` from `little_sister.limits` — a
module the library does not promise to keep, on purpose, so that the day the line moves
there, the test says so here.

### 5. The configuration is split by what it spares

A filter that **spares a request** stays in the measuring half, because the reading
it saves is never taken: an aspect switched off, `sbom_check.ignore`, `issues.ignore`,
`actions.ignore_workflow_name_patterns` and `advanced_security_on_private`. A filter
that only **chooses what is said** moves to the grading, because a reading that left
something out would be a reading of the configuration rather than of GitHub:
`pull_requests.ignore_title_prefixes`, the Dependabot `severities`,
`secret_scanning.require_enabled`, and every severity and disabled-state map.

What the measuring half keeps on the check between runs — where the roster resumes, how
many workflows each repository had, how long the last run took, the conditional cache,
the runs [ADR-0015](0015-a-workflow-line-holds-the-newest-run-it-has-read.md) holds, and
the last answer `sbom_check` holds for each repository
([ADR-0008](0008-the-dependency-graph-is-asked-not-exported.md), decision 7) — is for
the **next run's measuring**, and the grading reads none of it: what stands is handed to
it as a reading.

### 6. `github-rate-limit` is one reading per watched resource, about that budget

In the order the configuration declared them. The ledger is state this process keeps
about the world, so it is read in the measuring half: each reading carries the
tightest window's numbers — its reset as a time, `reset.at` (§8) — what this process
spent of it, what something else spends as the line says it, what the window carried
before this process read it, and what the other windows hold. The grading writes the
line from that and from `now`, which is the one thing a line reads the clock for — how
long until a window resets. A failed ask for the budget is one reading, the attempt and
its error, about no resource.

**The object of a reading is one account's budget for one resource.** Each resource is
its own budget at GitHub, with its own limit and its own reset, and this check already
treats it as its own object — a line per resource, a pin on `core` that survives a
configuration watching more. The account alone would put `core`, `graphql` and
`search` of one run under one named object, which little-sister ADR-0086 decision 8
forbids. The subject has the estate's shape, the object first and then what narrows
it:

```
<login>;resource=<resource>[;host=<host>]
```

`host` only off GitHub's own API, since one login on two GitHubs is two accounts. A
failed reading of a resource — GitHub did not report it, or not in a shape this check
reads — is still about that budget and carries the subject.

**Each reading is one line, and the line carries it**, as an `actions` line does
(ADR-0013 §3, little-sister ADR-0086 decision 2): the line's
`data` is the reading's record and its `subject` the reading's, on every line the
grading writes for a resource, the failed ones included. With no account the line
still carries the record, and its subject is empty with the reading's. The two
node-level failures — the ask unanswered, the answer without a `resources` object —
are the node's own `ERROR` and write no line. A record is one resource's reading, so
watching more resources adds lines, not bytes: with the longest login GitHub allows,
the longest resource name and three windows held for it, one weighs 417 bytes against
the default 2 KB `record_limit`.

**The account is asked, once per process.** `GET /rate_limit` answers with resources
and never says whose token it is, so the object is not in that answer. `GET /user` says
it: for a personal access token the budget is the user's — GitHub's own words are
*your personal rate limit of 5,000 requests per hour* — so the login names the budget
read. It is asked before the budget, so the one `core` request it costs is in the
numbers, and never again once answered. **An installation token has no subject.**
GitHub refuses it `/user`, and its budget is the installation's — *GitHub Apps
authenticating with an installation access token use the installation's minimum rate
limit* — which nothing at run time names; that refusal is final and not asked again. A
stand-in such as the host would give two installations one object and name something
that is not whose budget was read, so there is none: no subject, no history. A failure
GitHub did not answer leaves the question open, and that run's readings go without a
subject.

**A reading names no identity** (little-sister ADR-0087 decision 3). What is left of a
budget is read anew on every poll, so each poll's reading is a new point in the
budget's series and never a record an earlier poll wrote — the course of the budget
between two resets is what a history of it is for. Nothing here is an event that could
be read twice, the way an `actions` run is (ADR-0013 §4).

### 7. Every line one reading became carries it

A line the grading writes out of **one** reading carries that reading: its record,
whole, as the line's `data`, and its subject as the line's. That is every finding — a
pull request, an issue, a Dependabot, code-scanning or secret-scanning alert, a
dependency graph — every *secret scanning not enabled* and *issues are disabled* line,
and every note about a repository that could not be read or is gone, as the `actions`
lines (ADR-0013) and a budget's (§6) already did. One helper writes it for every
aspect, so a line carries what it read in one way. A line written out of many readings
— the coverage count, `runs-window-partial`, `branches-unmatched` — carries none, since
no one record is what it read. A finding names no subject (§2), so its line's is empty.

This is the step [ADR-0012](0012-the-actions-line-carries-what-it-read.md) left to each
aspect's own surface, and §1 and §4 have since designed those surfaces: every finding
is a reading with a record of its own, clipped to fit, so carrying it costs no design
and only its bytes. It is also what the library reads a line by. A reading kept in a
series finds the line it became by the record the line carries and the subject it
names (little-sister ADR-0087, decision 8), so a finding that is given a history later
keeps the code it stood with from its first record, instead of from the release that
would otherwise have had to add this.

Neither the sentence nor the slug moves. A banded aspect's lines carry no code of their
own — the band does — so the band says `entries=True` to keep each line a member a pin
can hold, which the `(slug, text)` pairs they were said by their shape.

### 8. A time is kept where the library reads one

little-sister reads three names in a record as instants, at any depth — `at`, `started`
and `ended` (little-sister ADR-0082) — and a time under any other name is a string to
every surface that shows the record. So every time a reading carries sits under one of
them: a run block's `started`, and GitHub's `updated_at` beside it as `updated.at`
([ADR-0012](0012-the-actions-line-carries-what-it-read.md) decision 3);
a disabled workflow's `updated_at` as `updated.at`
([ADR-0013](0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md) §5); a
secret-scanning alert's `created_at` as `created.at`; and a budget's reset, and each
other window's, as `reset.at` (§6). None is claimed as `ended`, and one that is the
instant the record is of is carried a second time, as the record's own top-level `at`,
which places a kept reading in its series.

Three kinds of record carry a top-level `at`, because that time is the instant the
record is of: a run line's, the start of the run it names
([ADR-0012](0012-the-actions-line-carries-what-it-read.md) §3); a disabled line's, the
instant its `updated.at` keeps
([ADR-0013](0013-the-object-of-an-actions-line-is-the-workflow-on-its-branch.md) §5);
and a dependency graph's own time — `null` on the run that read the graph and, on a
graph that stands for a repository GitHub did not answer for, when GitHub answered with
it, in UTC and to the second
([ADR-0008](0008-the-dependency-graph-is-asked-not-exported.md), decision 7). A run's
reading and a disabled workflow's each name a subject, so their `at` places each in its
series, and one that is `null` leaves the record where it was first seen; a graph's
names none (§2), so nothing keeps it and the `at` places it in no series, and the line's
*as of* is written from it.

**GitHub's text is read as a time or `null`.** A value under one of those names that is
not ISO-8601 with an offset has the library refuse the whole result, every aspect of
the run with it, so each time GitHub sends is parsed first: one it did not send, sent as
something that is not an instant, or sent at an edge of the calendar that UTC cannot
reach is kept as `null`, and the field is lost where the run was. A reset, which GitHub
counts in epoch seconds, is converted the same way — a number no calendar holds is
`null` too — and the grading reads the time back for the minutes its line counts. An
absent time keeps the field's shape, `{"at": null}`. A disabled workflow's identity is
the string its `updated.at` keeps, and GitHub's text as sent only where that keeps no
time (ADR-0013 §5).

Every kind of record the type writes is held to this by one run that writes them all;
`reset` is named there, because epoch seconds look like any other count.

## Consequences

- **A large estate hands the grading a few thousand small readings per run**, each
  validated at the seam — milliseconds — and none of them held beyond its node, so
  they cost the series' runtime ceiling nothing.
- **`github-rate-limit` spends one `core` request per process**, on its first run,
  where it spent none; every later run reads only the free endpoint. The line counts it
  as this process's own spend like any other.
- **The engine places the estate on the check's node only when it is the one reading
  the run handed back**, which is a run whose discovery failed. Every other run hands
  back several, and then the engine places none (little-sister ADR-0086 decision 2).
- **Free text past 300 characters is shorter on its line than it was.** The error
  text a failed read quotes is the case that shows it.
- **The grading ignores `now`** for the `github` type: nothing it says depends on when
  it is said.
- **Every line one reading became weighs its record beside its sentence** (§7), in the
  tree and in every envelope a client polls. With ordinary titles and URLs a pull
  request's or an alert's record is about 330 bytes of JSON and an issue's under 200;
  the heaviest pull request is 1090 (§4). Two hundred open findings are some sixty
  kilobytes more per poll.
- **The field names of those records are keys now**, as an `actions` line's were
  (ADR-0012): a deployment's line template and its grading map will read them, so
  renaming one breaks a deployment this package cannot see. They are listed in
  [`../architecture.md`](../architecture.md) §3.7.
- **Every time a record carries reads as a time** on a surface that shows a record's
  times (§8), and one GitHub got wrong costs its field and not the run.
- **A deployment whose `record_limit` is below 1773 is refused at startup**, naming the
  `github` check, where a run would have failed on its heaviest reading, a workflow
  run's (§4), and at the default limit every start says the declared record is close,
  until a deployment sets 2217 or more. A workflow name past 148 ASCII characters or 12
  emoji, a branch past 49 letters of Cyrillic or 24 emoji, and a manifest path past 68
  characters are shorter on their lines than they were — the path by its start.
- **A run line's pin moves once on a branch too long for its subject**, some 180
  characters with non-ASCII letters among them: its slug is built from the subject's
  digest now, where it was built from the branch (§4).

## Alternatives considered

- **One reading per repository per aspect.** Refused for the reason ADR-0013 refused
  it for workflows: a busy repository's open issues, pull requests or alerts pass
  `record_limit` in any one of them.
- **A subject on every finding.** It fits the limit, and it decides now that a pull
  request or an alert is an object with a history — the decision this record leaves
  to its own surface.
- **The owner's login as the estate's subject.** Two estates of one login would be one
  object.
- **The account alone as a budget's subject.** One run would name one object once per
  resource.
- **The window's `reset` as a budget reading's identity**, one record per window. Each
  poll would replace the last, and the series would keep one reading of every window
  and lose the budget's course inside it.
- **`reset` kept as GitHub counts it**, in epoch seconds. The grading would read a
  number without a conversion, and every surface would show a window's end as a
  count. Refused in §8.
- **GitHub's times passed through as sent**, as `started` was. No code, and it rests on
  GitHub's documented format; but one malformed time would cost every aspect of the run
  it came in, and nesting `updated` and `created` would have put two more fields in that
  position. Refused in §8.
- **No declared record size**, as ADR-0012 had it while a record was a few hundred
  bytes. At 1773 of 2048 a lowered `record_limit` would first be met by a run.
- **A branch kept verbatim in the record**, as the subject keeps it. The run reading
  would weigh 1985, 63 bytes under the default limit: a deployment could lower its
  `record_limit` by no more than that, and the next field the record gained would pass
  it.
- **The workflow's name and its file clipped at 81 bytes**, which brings the run down to
  the graph's 1635, under the line. Refused in §4: a name is clipped on every line it
  is written on, where the declaration past the line costs a warning at start and never
  a line.
- **A `record_limit` the type sets for itself.** Refused by little-sister ADR-0082: the
  limit is the deployment's, and a limit its subject can raise is not one.
- **A run line's slug built from the record's copy of its branch.** It would move the
  pin of every line whose branch the clip cuts, and put two branches alike up to the
  clip on one line.
- **Manifest paths at 100 bytes**, as they were. The graph that stands on its last
  answer would weigh 1935, more than any run.
- **A manifest path cut at its end**, as every other clip here is. Two long paths in
  one repository would read alike on the red line, where what tells them apart is their
  end.
- **Eight manifests instead of ten**, at 100 bytes each: 1609. But the grading reads a
  graph of ten manifests or fewer as none parseable only when it has read all of them
  (ADR-0008), and a repository of nine or ten unparseable manifests would go silent.
- **The library's line moved for a declared record**, or its share raised. The line is
  the library's to draw, and this package does not ask it to move for one type.
- **The API host, or the token's digest, as the object of a budget GitHub will not
  name.** The host would make two installations one object; the digest is an identity
  `budget.py` keeps precisely because it is never published, and a new token would
  start a new history for the same account.
- **Letting the grading read the check's run state** — the roster, the counts, the
  cache — as `run()` did. It works today and answers something quietly wrong over a
  reading this process did not take, which is the one failure the split exists to
  make impossible.
- **A record on a line only where its reading has a subject**, the findings bare until
  one of them is given a history. Rejected in §7: the day a finding got a history would
  also be the day its line began to say what it read, and the second has nothing to do
  with the first — a node's page and a line template read `data` whether or not
  anything keeps it.
