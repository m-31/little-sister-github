# ADR-0004 — A finding grades; the repository does not

- **Status:** Accepted
- **Date:** 2026-08-15 — the grading is the port's and is already in the code; this
  record is where it is written down for the people who receive it
- **Related:** [ADR-0003](0003-an-aspect-is-one-question-asked-of-the-whole-scope.md)
  (what an aspect is, which this record grades within),
  [ADR-0002](0002-a-read-failure-is-not-a-finding.md) (a line that grades nothing,
  and why), [ADR-0001](0001-a-second-check-type-in-this-package.md) (the API budget,
  and the run this check declines to make), little-sister **ADR-0042** (an entry
  carries its own code), little-sister **ADR-0036** (a keyed line is a member),
  little-sister **ADR-0050** (a slug is an identifier, never a position),
  little-sister **ADR-0043** (decision 7 — a subject is a virtual child)

A bare ADR number here is this repository's; a reference to one of little-sister's
is always written out, because the two numbering spaces overlap.

## Context

Every question a reader asks this check ends in the same place: *why is my
repository amber?* The README answers the mechanical half — which setting grades
what, and what its default is. It does not answer why the default is that, and it
is not the place to: a reader who disagrees with a grading needs the argument, and
a reader who agrees with it needs nothing.

The argument matters more here than the settings do, because two of the knobs
exist **in order to be overridden**. `severity_map` invites a deployment to
disagree about what a vendor's severity means in their estate; `severities` invites
it to disagree about which findings are worth reporting at all. Until this record
there was nothing shipped for either disagreement to be argued against — the
defaults were in this package's source, which is precisely where a reader on a
dashboard cannot look.

## Decision

### 1. The finding is what carries a status

There is no per-repository node and no per-repository status anywhere in this
check. A repository is a name inside a line, and the line is what grades.

In little-sister's vocabulary the line is a **virtual child** and the repository is
its **subject** (little-sister ADR-0043 decision 7; how such a reading folds without
moving a node's code is little-sister `architecture.md` §4.3). Several lines commonly
share one subject: a repository with a failed workflow *and* a run awaiting approval
has two coded lines under `actions`, and a reader may take the worst of them to ask
*how is that repository doing*. Nothing here changes if they do — no aspect grades
differently, nothing is stored, and nobody can act on it, because **a pin holds an
identifier, and only a line has one**. Which is this record's title: the finding is
what a status is declared on and held against.

One thing about this check in particular, so nobody looks for arithmetic that is not
there: under the two banded aspects the lines carry **no code of their own**
(decision 5 — the band carries it), so four `critical` alerts against one repository
read as `critical`'s code rather than as a fold of four.

Where that status is declared differs by aspect, and both shapes say the same
thing. The five flat aspects hand over lines that each carry **their own code**
(little-sister ADR-0042), and the aspect's code is derived as the worst of them.
The two alert aspects put their lines under severity bands, and the **band**
carries the code for the lines beneath it (decision 5) — which is what makes one
setting able to regrade forty findings at once.

Either way the line is a keyed member, and that is what makes one finding
separately actionable: an engineer who opens a ticket for one alert pins **that
line** and the other nineteen keep reporting (little-sister ADR-0036). The key is
built from something GitHub minted — the per-repository number of the pull
request, issue or alert where there is one, the finding's own API URL where there
is not, the workflow and branch for a workflow line, and the aspect for the aspects
that report at most one line per repository. What it is never built from is the
rendered text, or the line's position in a list (little-sister ADR-0050).

The repository half of every key is its **numeric id**, and that is the one place
this record had to be tightened rather than merely applied. An earlier wording
allowed *the repository* as a key component without saying which of its fields, and
a name is the one thing here GitHub did **not** mint: its owner may change it at
any time, and a rename then silently re-keys every line about that repository and
orphans every pin held on one. The id is the field a rename does not touch. The
cost is paid in legibility — `6304-pr-42` cannot be read the way `platform-a-pr-42`
could, and a `?reason=` value no longer names its repository — and it is paid in
the right currency: a slug is what a machine and a pin hold, and the rendered line
still opens with the name. **Pinning** the repository instead would mean one pin
silencing everything about it, which is how a maintenance window becomes a blind
spot — and is why a subject is something to read and never something to pin.

### 2. Amber is a queue; red is something to do now

The two codes mean different things about **time**, and every grading below is an
application of that:

| What the check found | Code | Why that one |
|---|---|---|
| an open pull request | `WARN` | review is normal work — it is on the dashboard because a queue nobody drains is a problem, not because a pull request is |
| an open issue | `WARN` | the same reading, from the other endpoint |
| issues turned off for a repository | `WARN` | the question cannot be answered for it, and an aspect that silently answered "none" would be wrong |
| a secret-scanning alert | `ERROR` | a committed credential is live until it is rotated, and the clock started when it was pushed |
| secret scanning not enabled | `ERROR` | see decision 3 |
| no dependency graph | `ERROR` | see decision 4 |
| a failed workflow run | `ERROR` | the build is the one signal that is supposed to be green |
| a workflow run awaiting approval | `WARN` | somebody has to press a button; nothing is broken |
| a dependency advisory, an analysis alert | banded | see decision 5 |

Nothing this check reports is graded on a repository's *importance*. A leaked
secret in a scratch repository is red, because the credential does not know which
repository it was committed to.

**These codes are fixed, and only the banded ones are a setting.** The five flat
aspects carry the codes above with no knob for them; what a deployment decides about
those is whether the aspect runs at all and which repositories or titles it skips.
That asymmetry is deliberate rather than an omission: a severity is a **vendor's**
word whose meaning differs by estate, which is what `severity_map` exists to
translate, while *an open pull request* means the same thing everywhere. A
deployment that wants one of the five graded differently is disagreeing with this
record, not missing a key — and a per-aspect code setting would be the way to say
so.

### 3. The absence of a watcher grades like what it would have watched for

A repository with secret scanning switched off is `ERROR`, the same as an open
alert, unless `secret_scanning.require_enabled` is set false.

The endpoint answers `404` when the feature is off, which is indistinguishable
from "no alerts" if you only look at the alert count — and that is exactly the
shape of a false green: the repository most likely to be leaking a credential is
the one nobody is watching. So the code is the one the finding it cannot make
would have carried. A deployment that has decided not to run secret scanning
everywhere turns the setting off, which is a statement about their estate rather
than a repository that stops being reported.

### 4. A missing dependency graph is red because it makes another aspect lie

`sbom_check` looks the least urgent thing on the dashboard: it reports paperwork,
and it reports it at `ERROR`.

The reason is one aspect over. `security_advisories` reads Dependabot, and
Dependabot has nothing to say about a repository whose dependency graph is empty —
so that repository is **green in the advisories aspect for the wrong reason**, and
will stay green through every vulnerability that is ever published. The missing
graph is not a documentation gap, it is the thing that makes another aspect's
silence meaningless, and it is graded as what it costs rather than as what it is.
A repository that genuinely has no dependencies to graph is named in
`sbom_check.ignore`, which is a deployment's call about a repository and could not
have traveled with this package.

### 5. The band grades, the alert does not

The two alert aspects group their findings into **severity-band children**, one
leaf per band, and the band's status is the whole of what those aspects assert
about an alert. A band with findings takes its mapped code; a band with none is
`OK`. The aspect above them declares no code of its own, so what it shows is its
bands — with one exception that is not about alerts at all: a repository the
aspect was refused (a 401 or a 403) is an amber line on the aspect itself, because
it belongs to no severity and must not be smuggled into one (ADR-0002).

An alert therefore never carries a status of its own — it is a line on the band
that owns it. That is what lets `severity_map` mean something: one setting regrades
every finding at that severity, in one place, without touching a line of text. It
also puts the severity on the *node* rather than burying it in the reason text,
so a band that fired is visible at a glance instead of being inferred by reading
forty lines.

### 6. A watched band reports while it is empty

A band renders when it is named in the grading map **or** present in the data. So a
band a deployment declared is on screen every run, green, with nothing in it.

The alternative — render a band only when it has findings — loses the difference
between *nothing critical today* and *the critical band stopped being produced*. A
green band is a reading; an absent one is a silence, and a monitoring tool that
answers a question by saying nothing has not answered it.

The corollary is that a severity **the data brings and nothing declared** still
gets a band where the aspect grades everything it is handed, and that band is
`WARN`. GitHub can add a severity without asking us, and a check that discarded
one would under-report by exactly the amount nobody would think to look for.
`WARN` is the deliberate middle — loud enough to be seen, not so loud that a
vendor's taxonomy change wakes somebody at night — and a deployment that knows what
the new band means names it in its own map, at which point the guess stops
applying. The advisories aspect is the exception on both counts, because it
selects before it grades (decision 7): a severity outside `severities` never
reaches a band at all, and `WARN` is instead what a *selected* severity gets when
the map has nothing to say about it.

The cost of decision 6 is worth stating, since it is what a typo looks like: a
severity misspelled into a deployment's own map is a band that renders every run,
permanently empty and permanently green. A name nothing can ever fill is
indistinguishable from a name nothing happens to be filling, which is the same
property that makes an empty band worth showing at all.

### 7. `severities` is what is looked at; `severity_map` is what it means

The advisories aspect has both, and they are not two ways of saying the same
thing.

- **`severities`** (default `critical`, `high`) selects the findings this check
  reports **at all**. A severity that is not selected produces no line and no
  band: it is not on the dashboard, and it is not in the JSON.
- **`severity_map`** grades what was selected. Mapping a severity to `OK` keeps its
  band and its lines on screen, green.

A deployment that wants to *see* medium advisories without being paged for them
sets both — selected, mapped to `OK` — and gets a green band with lines in it,
which is a fact on a page rather than an alarm. One that does not want them at all
leaves them out of `severities`. Collapsing the two into one setting would force
those two intentions into the same spelling, and the shipped default would then be
the loud one for everybody.

### 8. The defaults are strict, and they are a default rather than a verdict

Where the shipped map has an opinion it is the pessimistic one: for dependency
advisories, `critical` and `high` are `ERROR`, `medium` and `low` are `WARN`, and
what the code-scanning aspect declares is graded `ERROR`.

Strict is the right way for a **default** to be wrong. A default that is too loud
is discovered on the first run and turned down in one line; a default that is too
quiet is discovered by the finding nobody was shown. Neither of those is a claim
about anybody's estate — this package has never seen it — and the whole of the
banded grading is one setting away: `severity_map`, per aspect, per severity, in the
deployment's own configuration.

### 9. A workflow's verdict is the last one that said something

The `actions` aspect grades one entry per workflow and branch, and its code is the
newest run that said something useful: a completed run that failed or passed, or a
run held for approval. Four rules make that honest:

- **A canceled or skipped run does not erase the verdict beneath it.** It is not
  an outcome, and treating it as one would turn a red workflow green by canceling
  a run.
- **A run in flight is an additional flag on the same line, never a replacement**
  for the verdict. A retry must not hide the failure it is trying to fix.
- **An unrecognized state is not "running".** Only the states that positively mean
  work is in flight count as in flight (little-sister ADR-0032 rule 7); anything
  else contributes nothing rather than being guessed at.
- **A workflow that is passing and idle is not shown** unless `actions.show_healthy`
  says so, because a list of everything that is fine is where the one thing that is
  not gets lost. A run in flight is always shown.

Runs of a workflow that no longer exists are dropped: a deleted workflow's last
failure is not a fact about the repository today.

**What follows from those rules is that a workflow can be absent**, and it is
named here so the next reader meets it as a known limit rather than as a mystery.
The aspect makes two reads per repository — one page of the repository's workflows,
and one page of its newest hundred runs, on the default branch unless
`actions.all_branches` says otherwise or discovery could not learn which branch
that is. A workflow appears only if those pages hold something it has to say. So a
workflow is missing when its recent runs were all canceled or skipped, when it is
quiet enough to have been pushed off the runs page by busier neighbors, when it
sits past the hundredth workflow of a very large repository, when
`actions.ignore_workflow_name_patterns` matches its name, and when Actions is
switched off for the repository altogether. All of those are absent rather than
reported as unknown, which is the cost of the same rule that stops the healthy
workflows from crowding the leaf. Paging further back is real work for a bound no
scope here has reached; a repository that outgrows it loses its **quietest**
workflows first.

## Consequences

- **A disagreement about grading is supported and is one line.** A deployment that
  thinks `medium` should be amber rather than red says so in its own configuration,
  and this record is the case it is arguing against rather than a default it has to
  reverse-engineer from behavior.
- **A rewording is free and a re-keying is not.** Because a line's identity is
  GitHub's own number, the text of any line can be improved at any time; changing
  how a key is composed is a breaking change for every deployment holding a pin,
  whatever the code says, and belongs at the top of the release notes.
- **A quiet aspect is not always a healthy one**, and this record is where the two
  cases are separated: a band with nothing in it is green (decision 6), while a
  repository nobody could ask about grades nothing at all and is counted on the
  check's own node instead (ADR-0002). An aspect that was switched off leaves no
  node (ADR-0003, decision 5), and a run the budget could not afford grades `WARN`
  on the check's node and reports no aspect at all (ADR-0001).
- **What a band renders as is not settled here.** Which bands the code-scanning
  aspect declares, the order the bands appear in, and whether a band's title should
  carry its severity as a glyph rather than as a repeated word, are display and
  classification questions this record deliberately leaves open.
