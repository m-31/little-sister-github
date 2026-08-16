# ADR-0003 — An aspect is one question asked of the whole scope

- **Status:** Accepted
- **Date:** 2026-08-15 — the shape is the port's and is already in the code; this
  record is where it is written down for the people who receive it
- **Related:** [ADR-0004](0004-a-finding-grades-the-repository-does-not.md) (what
  each aspect then asserts, which is the other half of this),
  [ADR-0002](0002-a-read-failure-is-not-a-finding.md) (what an aspect does with a
  read it could not complete), little-sister **ADR-0043** (a coverage reading on the
  check's own node), little-sister **ADR-0044** (a node's report — presence without
  a status claim), little-sister **ADR-0025** (per-node display text, and a
  deployment's right to replace it), little-sister **ADR-0036** (a keyed line is a
  member)

A bare ADR number here is this repository's; a reference to one of little-sister's
is always written out, because the two numbering spaces overlap.

## Context

A `github` check reads a **whole account** — every repository an organization,
a team or a personal account holds — rather than one target. Nothing in the
configuration names a repository: the check discovers its scope and then has to
decide what shape to report it in.

That shape is a decision, and it is the first one a reader of a dashboard meets.
The README says which endpoint each aspect reads and what it grades. What it does
not say is why the tree has seven children called `pull_requests` and `sbom_check`
rather than forty called after repositories, why an aspect that is switched off
leaves no node behind, and why two of the aspects read a smaller scope than the
other five. A deployment that wants to change any of those has to be able to argue
with something.

## Decision

### 1. The tree is aspect-first, and a repository is a line

The check's node is a container with one child per aspect (`/<path>/<aspect>`).
Every repository the aspect flags is a **keyed line** on that aspect — or on one of
its severity bands, for the two aspects that have them (ADR-0004, decision 5) —
and never a node of its own (little-sister ADR-0036).

The repository-first tree is the obvious alternative and answers a different
question. What an operator opens this dashboard for is *which domain needs
attention across everything we own* — are there secrets committed anywhere, is
anything missing a dependency graph. Repository-first turns that into forty nodes
that each have to be opened, and the glance at the top becomes a per-repository
roll-up nobody acts on: "four repositories are amber" says nothing about whether
to be worried.

Aspect-first costs the reading in the other direction — *what is wrong with this
one repository* is now spread across seven nodes. That is the deliberate trade,
and it is the reading the operators of the dashboard this was ported from already
had. It costs nothing in addressability: each line is keyed with the repository in
it, so a pin held for one repository's finding is exactly as precise either way
(ADR-0004, decision 1).

### 2. One aspect is one question, and the endpoint follows the question

The seven are seven questions, not seven endpoints that happened to exist:

| Aspect | The question it answers |
|---|---|
| `pull_requests` | what is waiting to be reviewed |
| `security_advisories` | which known-vulnerable dependencies are we shipping |
| `code_scanning_alerts` | what did static analysis find in the code |
| `secret_scanning_alerts` | is a credential committed anywhere |
| `sbom_check` | can the dependencies of this repository be checked at all |
| `actions` | is the build passing |
| `issues` | what is open against these repositories |

Where the two would disagree, **the question wins**. `issues` and
`pull_requests` are two aspects reading endpoints that overlap — GitHub's REST API
counts every pull request as an issue and returns both from the issues endpoint —
and the aspect drops the pull-request rows rather than reporting each open pull
request twice under two headings. It follows that where one endpoint answers two
different questions, that is two aspects and not one node with two kinds of thing
on it.

The number seven is therefore not a property of the API and not fixed. An aspect
added later is additive: it arrives switched on, because a config that says
nothing about an aspect runs it (decision 5).

### 3. Scope is discovered once, and every aspect starts from that one set

One discovery per run — the declared account kind verified against GitHub, then
the organization, team or user repository listing, filtered by `name_prefix`,
`include_archived` and `include_forks` — producing typed values every aspect
works from (little-sister ADR-0026).

An aspect may then narrow it, and two do: the Advanced Security pair by visibility
(decision 6), and `sbom_check` and `issues` by their own ignore lists. What no
aspect does is *widen* it or resolve its own. So a repository that appears under one
aspect and not another is saying something about **that aspect**, never about
discovery. Had each aspect resolved its own
scope, the two readings would drift the first time a filter was applied in one
place and not another, and the difference would look exactly like a finding.

It also bounds the cost. A run makes at least one request per repository per
aspect — `actions` makes two, and every paginated read makes as many as it takes —
so a per-aspect discovery would add a listing per aspect to a budget this check
already has to estimate before it dares spend it (ADR-0001).

**One filter default is worth naming here**, because it decides what an aspect is
reading before any grading applies: `include_forks` is **true**. A busy fork can
therefore dominate an aspect, and the default still does not presume — a fork is a
repository the account holds, its pull requests are real, and a package that has
never seen the estate is not the right party to decide that somebody's forks do not
count. The filter is one line for a deployment that disagrees. `include_archived`
defaults the other way, because an archived repository is one nobody can act on.

### 4. The check's own node says only how much was looked at

Everything the container writes for itself is about **coverage**: `14 repositories
in scope`, `WARN` below `expect_min_repos`, and the discovered roster as its report
— presence without a status claim (little-sister ADR-0043, little-sister ADR-0044).
The two run-level facts ADR-0002 put here — reads that could not be completed, and
a run cut short by its deadline — are the same kind of statement. The node still
takes the worst of its aspects, as any container does; what is decided here is what
it *says*, and it says nothing about a repository's contents.

An empty scope is the case this exists for. No repositories discovered is `WARN`,
naming the account, the team and the prefix it looked with, because a check that
found nothing to look at must not read like a check that looked and found nothing
wrong. That distinction is invisible one level down: every aspect is perfectly
green when the scope is empty — nothing was found because nothing was examined,
and an aspect has no way to tell those apart.

### 5. An aspect is switched off whole, or not at all

`enabled: false` in the aspect's own configuration block skips it entirely: no
node, none of its API calls, and the rate-limit estimate shrinks with it.

It is deliberately not a *hidden* node. A node that existed and was merely not
drawn would still be in the JSON, still hold maintenance pins, and still be graded
by anything reading the tree rather than the page — so "off" would mean four
different things depending on where you looked.

Two consequences follow from storing the setting as what is *off* rather than what
is on. An aspect a later release adds is on in every config written before it
existed, which is the opposite of what an allow-list does — an allow-list would
silently exclude it, and nobody would find out from their dashboard. And a config
that switches off all seven is **refused at load**: the node would still carry the
coverage reading and the roster, so it would look from the dashboard exactly like
a check that reports on repositories, while reporting no finding about any of
them. Deleting the check says that out loud.

### 6. Two aspects read a narrower scope, and say so where they narrow it

GitHub Advanced Security — code scanning and secret scanning — is free on a public
repository and paid on a private one, for organizations and personal accounts
alike. So `advanced_security_on_private` is a switch about **visibility**, and the
account kind only picks its default.

With it off, private repositories drop out of those two aspects rather than being
reported as "scanning not enabled" — which would be true, useless, and repeated on
every private repository forever. The repositories that dropped out are **named on
the aspect that dropped them** (little-sister ADR-0044): an aspect that quietly
reads half its scope and reports OK is the shape of a monitor that has stopped
monitoring, and the check's own coverage reading cannot see this one, because
discovery found those repositories perfectly well.

### 7. Where a name is a stored key, a mismatch is recorded rather than repaired

Six aspects read a configuration block named after them. `secret_scanning_alerts`
reads `secret_scanning:`, because the block was named for the GitHub feature and
the node for what it reports.

Both spellings are keys held somewhere else — one in every deployment's
configuration, the other in every maintenance pin and dashboard built on the node
path — so making them agree would break one set to tidy the other. The mismatch is
written down here and in the README instead. This is the general rule arriving in
its smallest possible case: a name this package has published is not ours to
improve, and the value of consistency is not zero but it is much smaller than the
cost of a pin that stops matching.

## Consequences

- **A deployment's disagreement has somewhere to go.** Wanting a repository-first
  dashboard, or an eighth aspect, or one of the seven switched off for a team, are
  all arguments against a decision recorded here rather than against a shape whose
  reasoning was never shipped.
- **Adding an aspect is additive and cannot be silent.** It appears on every
  dashboard at the next upgrade, which is the intended behavior and belongs in the
  release notes as a change to what a check reports.
- **Removing or renaming one is breaking**, whatever the code says: an aspect name
  is a node-path segment, and every maintenance pin held against it is stored in
  somebody else's deployment.
- **What each aspect then asserts is not decided here.** Why an open pull request
  is amber while a leaked secret is red, and how the two alert aspects turn a
  vendor's severity into a status, are ADR-0004's.
