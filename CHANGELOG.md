# Changelog

All notable changes to little-sister-github, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/). These notes are for the
deployments that install this package, not a work log.

Treat **a new or renamed `type:` name, a changed slug shape, or a removed config
key as a breaking change** and say so at the top: every deployment with
maintenance pins or dashboards built on those is affected, even though nothing in
the code says so.

## [Unreleased]

## [0.1.1] - 2026-08-16

**Breaking, and every existing config is affected: `org:` is now `owner:`, and a
new `kind:` is required.** Both are one-line edits and the check refuses at load
until they are made — it does not start with a guess.

**Breaking: the `code_scanning_alerts` aspect is now two, `code_scanning_security`
and `code_scanning_quality`.** Node paths move, so every maintenance pin and every
dashboard built on `…/code_scanning_alerts/<band>` stops matching, and the
configuration key splits. **The check refuses to load** while a
`code_scanning_alerts:` block is present, naming both halves — it does not start with
a guess, and it does not silently drop your grading.

*What to do:* rename the block to `code_scanning_security:` and move `critical`,
`high`, `medium` and `low` from its `severity_map` there. If you had graded `error`,
`warning` or `note`, those belong in a new `code_scanning_quality:` block — and read
the defaults below before you copy them across, because they changed. An
`enabled: false` goes on whichever half you do not want. Then re-place your pins:
an unmatched pin is **suspended, not deleted**, so the old ones sit in your
maintenance file doing nothing.

*Why:* GitHub gives a code-scanning alert **two** severities and files them under two
headings of its own — *Security* for the alerts whose rule has a security severity,
*Other* for the rest, graded by the rule's own analysis severity. This check read one
field and rendered one eight-band row from it, which had two consequences worth the
break. **Three of those eight bands could never be non-zero**: `error`, `warning` and
`note` were watched, permanently green and permanently unreachable, because nothing
ever classified an alert into them. And **one `severity_map` forced one answer onto
both scales** — the shipped default answered `ERROR` for everything, a `note`
included. Each aspect now declares only the bands it can fill, and carries its own
map and its own default.

*What it costs you at runtime:* **nothing.** Both aspects are built from a single
`/code-scanning/alerts` read per repository, and the pre-run rate estimate counts
distinct endpoints rather than aspects, so an eighth aspect does not reserve an
eighth call that the run never makes.

*New defaults.* `code_scanning_security` grades every band **ERROR** — an alert
GitHub gave a security severity is a vulnerability in your own code, and the mildest
one is still that. `code_scanning_quality` grades `error` and `warning` **WARN** and
`note` **OK**: red on a status dashboard means act now, and a non-security finding is
not that, however CodeQL grades its rule. **If your dashboard was red for lint
findings it will go amber**; if you want the old answer, say so in the new block.

**Breaking, and every existing maintenance pin on a repository line is affected:
the slug shape changed from `<repo-name>-…` to `<repo-id>-…`.** A pin is keyed by
slug, so every pin held on a pull request, issue, alert, workflow or SBOM line stops
matching on upgrade. Nothing is lost loudly and that is the awkward part: an
unmatched pin is **suspended, not deleted**, and a pin with no expiry is never
reaped while its node still exists — so it sits in the deployment's maintenance
file doing nothing, invisibly. **Re-pin what you still want silenced, and clear the
rest.** The reason for the break is in
[ADR-0004](docs/adr/0004-a-finding-grades-the-repository-does-not.md) decision 1: a
repository *name* is not something GitHub minted, so a rename used to orphan those
same pins with no upgrade and no warning at all.

### Fixed

- **An aspect that could not look was showing green.** A repository this check could
  not ask GitHub about produces a line that grades nothing (by design), so an aspect
  that reached *nothing at all* derived `UNDEFINED` — which the roll-up ignores. For
  the two severity-band aspects it was worse: their bands render `OK` when empty, so
  `security_advisories` and `code_scanning_alerts` showed **green** during a total
  outage. Every aspect now carries one amber line of its own when it could not ask —
  `GitHub did not answer for 1 of 40 repositories` — so *could not look* never reads
  as *nothing to report*
  ([ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md) decision 5).
  - **Replaces the `39 of 40 repositories read` line**, which was `OK` and said the
    complement. Same slug (`read`), so a pin on it carries over; new wording and a
    new code.
  - The count is the *could-not-ask* kind only. An aspect whose only trouble was a
    permission error is as quiet as before — that repository already grades amber on
    its own line.
- **A GitHub outage during discovery turned the whole tree red.** Failing to list the
  repositories returned a flat `ERROR`, which is precisely the outage-grades-us that
  this check exists to avoid, and it replaced every aspect's reading with one line. A
  **transient** discovery failure is now `WARN` with no children, which leaves every
  aspect showing what the last good run found. A failure GitHub *answered* — an owner
  or team that is not there, a token that may not look — is still `ERROR`, because it
  is a defect somebody has to fix (ADR-0002 decision 9).

- **A rate limit was being reported as though GitHub had said *no*.** GitHub answers a
  rate limit — primary or secondary — with **403 or 429**, and the failure split read
  anything below 500 as *GitHub answered*. So a throttled request became an amber
  `could not read` line naming a repository that was in perfect health, and it was
  never retried. The reading is *not now* now: it grades nothing, and it is retried
  once, after the wait GitHub itself named
  ([ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md)).
  - **The status cannot tell you**, which is why this lives here and not in the
    library: GitHub documents both 403 and 429 for both of its limits, and a bare 403
    equally means *this token may not see it*. So the headers decide, in GitHub's own
    order — `retry-after`, then `x-ratelimit-remaining: 0` with `x-ratelimit-reset`,
    and otherwise 60 seconds. A **bare 429** is a throttle, because a rate limit is
    the only thing GitHub sends that status for. A **bare 403 is not**, and stays the
    permission answer it always was; reading it as *not now* would retry every
    repository your token cannot see and then paint the real problem grey.
  - **The wait stays inside `timeout:`.** A short secondary limit is absorbed and the
    run carries on. A long primary limit — a reset twenty minutes out — is *not* slept
    through: the request is not retried, the line says how long GitHub asked for, and
    the run reports what it has. When to ask again is the check's schedule, and a
    request is not the place to hold a run past its budget.
  - If you have a dashboard where a burst of activity produced amber lines across
    several repositories at once, **that is what this was** — and those lines are grey
    now, with the count on the check's own node.
- **An answer that is not JSON is no longer retried.** A proxy login page, or an error
  page where a payload was expected, used to be classified as *could not ask* and asked
  again — a second request spent to be handed the same bytes. It now reports what it
  is: an answer this check cannot read, which grades and says so on the line.

### Changed

- **A read failure is no longer a finding about the repository**
  ([ADR-0002](docs/adr/0002-a-read-failure-is-not-a-finding.md)). GitHub's SBOM
  endpoint 500s often enough that `warn — platform-a: could not read (HTTP 500 …)`
  was routine, and read as a monitoring statement it was wrong: `sbom_check` asks
  *does this repository have a dependency graph?*, and a 500 is not an answer to
  it. What happened is that the check **could not ask**.
  - A **5xx or a transport failure** is now a line that grades nothing — displayed,
    keyed and pinnable, but skipped when the node's code is derived. Your dashboard
    gets quieter here on purpose: a repository nobody could ask about is no longer
    amber.
  - The split is **by status, never by message text**. A **404** still means the
    thing is absent, and **401 / 403** still grade — a token that may not read a
    repository is a true statement about that repository.
  - **The check's own node is where an outage becomes visible**, once:
    `3 repository reads could not be completed this run — GitHub did not answer`. Beside the `expect_min_repos` reading, because both are coverage. On an
    aspect it would be the same sentence on up to seven nodes for one outage.
  - **An aspect that missed something states what it did read** —
    `39 of 40 repositories read`. Only when a repository was *unreachable*, which is
    the case where the quiet lines would otherwise erase the clean ones; a healthy
    aspect is as quiet as before. An aspect that read *nothing* codes itself
    UNDEFINED rather than OK. (The two severity-band aspects are the exception and
    still render green in that case, because a container defers to its bands — the
    check's own node is where it shows.)
  - **`pull_requests` is isolated per repository**, like every other aspect. Its
    whole loop was inside one `try`, so the first repository's failure returned the
    aspect as ERROR and the others' open pull requests were never looked for.
  - Node paths are unchanged and so are the per-repository slugs, so **maintenance
    pins still match** — with one exception: `pull_requests`' failure line was prose,
    so its slug was derived from the wording; it is a keyed `<repo>-unreadable` now,
    and a pin held against the old derived slug does not carry over.
- **A `subnodes:` key that names nothing is refused at load**, where it used to be
  accepted and quietly do nothing. The keys are aspect names; a key that is not one
  is a paragraph a deployment wrote and no page ever draws, which is worse than a
  config error because nothing anywhere says so.
  - **This is why it surfaced now.** The aspect split above renames a key, and a
    config that split its `code_scanning_alerts:` block but left the matching
    `subnodes:` entry alone would have loaded cleanly and silently lost that text.
    Naming the retired aspect there now says what it became and where the paragraph
    should go, rather than only that the key is unknown.
  - Two other keys are worth knowing about: a **band** name (`critical`) is refused,
    because `subnodes:` addresses aspects and not the bands beneath them — set a
    band's title or about per node path in `nodes.yaml`; and `secret_scanning`, which
    is the *configuration block* for the `secret_scanning_alerts` aspect and not the
    aspect's own name.
  - **A switched-off aspect may still carry its text.** The check validates against
    the aspect roster, not against what is enabled, so `enabled: false` does not also
    require deleting the paragraph.
- **A severity band wears a colored circle instead of its own name again.** Every
  band's title was the name back with a capital letter — `critical Critical` — twice
  the width of a chip for no second fact. The rows now read 🔴 🟠 🟡 🔵 beside the
  names.
  - **The colour is by name, never by rank**, and this package is the reason. A rank
    here is *your* tuple: `security_advisories` reports the severities
    `dependabot_severities` names, so if you watch `high` and `medium`, `high` is
    first there and second under code scanning. A rank-derived circle would put one
    `high` 🔴 and the other 🟠 on one dashboard. Same severity, same circle.
  - **Two scales share the ramp** — `error` 🟠, `warning` 🟡, `note` 🔵 for the
    analysis severities, matching their security counterparts rung for rung. Since
    the split above they never appear in one row, so nothing repeats where you can
    see it.
  - **A severity this package does not name gets `❓`**, never a borrowed colour —
    including the `none` band an alert with no severity of either kind lands in.
  - **The word is not lost.** The name is beside the title on every chip, and the two
    surfaces that draw a title *instead of* a name — the `/copy` hand-off and the
    hover card — now draw both, so a pasted ticket reads `critical 🔴`. That is the
    library's rule (little-sister ADR-0061), and it is why the floor below matters
    for more than an import.
  - **To get the word back**, set `title:` per node path in your deployment's
    `nodes.yaml`. A `subnodes:` block in the check's own config reaches the *aspect*
    nodes here, not the bands beneath them.
- **Both rows are in an order somebody chose.** Siblings sort by name, and a name
  sort destroys the one order a severity scale has. The band row read
  `critical high low medium` — **`low` before `medium`**, which is not a cosmetic
  complaint but a wrong statement about severity — and the aspect row was pure
  alphabet: `actions` first, `sbom_check` wedged between `pull_requests` and
  `secret_scanning_alerts`. Both now declare a rank (little-sister ADR-0055).
  - **The aspect row is now** `secret_scanning_alerts`, `security_advisories`,
    `code_scanning_alerts`, `actions`, `sbom_check`, `pull_requests`, `issues` —
    worst-first by what a finding costs, with the hygiene aspects after the security
    ones. **This is also the order the run asks in**, because it is one constant and
    not a display copy: a run that loses its budget to a slow GitHub now loses the
    cheapest aspects rather than whichever the alphabet put last.
  - **A band row follows its own aspect's declared severities**, not a constant in
    this package — so if you configured `dependabot_severities`, the sequence you
    wrote is the sequence you get. A band only a `severity_map` names ranks after
    those, and a severity that arrived in the data and nobody declared shares one
    rank at the end, sorted by name rather than by whatever order the payload had.
  - **Your `nodes.yaml` still wins**, per node, as it already does for `title` and
    `about`. One thing to know before writing one: `0` is the rank the *unranked*
    carry, so `order: 0` on one child of a ranked row moves it to the **front**, not
    back to its alphabetical place.
  - **The JSON `children` order moves with it** — little-sister ADR-0055 decision 5
    accepts that as a small incompatible change. No node path, no slug and no
    grading changes, so maintenance pins are untouched.
- **The two severity-band aspects now state the grading in force** instead of
  naming the key that sets it. The shipped text said *graded by
  `<aspect>.severity_map`* — which withholds the answer and points a dashboard
  reader at a default written in this package's source, the one place they cannot
  look. The text is expanded per deployment, so a config that overrode the map
  sees its own mapping; when every band grades the same it collapses to
  `**ERROR** for every band` rather than repeating itself eight times.
- **A throttle wait is logged at warning, and says what is left of the run.**
  little-sister logs the wait at info, and a minute of silence with no visible
  line reads exactly like a hang. This check now logs its own line —
  `paused 47s before retrying (312s of the run left)` — so a throttled run is
  visible without turning info on.
- **`timeout:` now bounds the run it always claimed to.** It is documented as the
  per-run budget and was being handed to the socket layer as the *per-request*
  timeout — spent afresh on each of the several hundred requests a run makes, and
  bounding nothing. Nothing else bounded it either: the engine runs a check without
  a deadline of its own. A slow GitHub could therefore hold a run past its own
  frequency indefinitely, and a wedged check reports nothing at all.
  - When the budget runs out, the aspects that finished are **kept and reported**,
    the rest are absent, and the node says
    `run cut short after 63s of its 60s timeout — 4 of 7 aspects reported`.
  - **Check your `timeout:`** if you set it near a single request's length: it is
    now the whole run's, and a run makes at least one request per repository per
    aspect (`actions` makes two) plus pagination.

### Added

- **A run traces itself at `INFO`, so a run that does not finish can be read
  rather than guessed at.** Roughly a dozen lines per run, on the logger this
  package already uses (`little_sister_github.github`), and every one of them
  answers a question the node deliberately does not:
  - `run starting — timeout 60s, request timeout 15s, max pause 30s, 7 aspect(s)`,
    before anything is spent. Two of those three are **derived** when your config
    does not name them, so they cannot be read off the config file.
  - `discovery took 4.1s in 3 read(s) — 56s of the run left`, because discovery
    spends the same budget the aspects do and no aspect line can show that.
  - `900 API calls left, this run needs 4×14 = 56`, whether or not it stops the
    run: the interesting run is the one that *just* cleared the factor.
  - One line per aspect — `aspect 4/8 actions took 8.4s in 12 read(s) — 31s of the
    run left`. **This is the column that says where a run's budget went.** A run
    that always stops at the same aspect is answered by reading it, and
    `code_scanning_quality` showing `0 read(s)` is the single-payload split above,
    visible for the first time.
  - `run ended after 61.0s of its 60s timeout — 96 read(s) in 58.2s, slowest read
    14.9s (/repos/…/code-scanning/alerts), 0s paused, 5 of 7 aspects reported`. The
    slowest read is what tells *one endpoint sitting at the request timeout* apart
    from *everything is slow*, which are different problems with the same node
    sentence.
- **The cut-short warning now names the aspect the run stopped in and the ones it
  never reached** — `… — code_scanning_quality (6 of 7) was cut off after 12.3s and
  9 read(s); never reached: pull_requests, issues`. The node's sentence is
  unchanged and still counts rather than names; what is new is that a reader no
  longer has to rebuild the roster by hand from the aspect order minus their own
  `disabled_aspects` to find out **which** aspects a starving run is starving.
  - **The order is fixed, so it is the same tail every run.** An aspect that is
    never reached keeps its last reading and stays on the dashboard looking
    answered; the check's own node says the run was cut short, and now the log says
    who paid for it.
  - *Never started* and *cut off partway* are told apart on the line. Both read as
    `0 read(s)` in `0.0s` and they are different findings: the first says the
    aspects **before** it were too slow, the second says **this one** is.
- **`request_timeout:`** — how long **one request** may take, default `15s`, in the
  same spelling `timeout:` takes (`15s`, `2m`, or a bare number of seconds). A
  request is additionally clamped to whatever is left of `timeout:`, so neither
  bound can be talked past.
- **One retry for a transient failure.** A 5xx or a transport failure is asked
  again once, after a second, and only while the run has more than that second left.
  So what *usually* reaches a dashboard is a failure GitHub gave twice — which is
  what makes the quiet line above trustworthy. Not always: a run short of budget
  skips the retry, which is why the node's sentence claims only that GitHub did not
  answer.
- **A second check type: `github-rate-limit`** — what is left of a token's GitHub
  API budget, one coded line per resource. Additive: nothing about the `github`
  type changes, no node path moves, and a deployment that does not want it writes
  no config. The one import line already in your `wsgi.py` registers both types.
  - **It is a type of its own, not an eighth aspect of `github`**, because a rate
    limit belongs to the **token** and not to an account: two `github` checks
    sharing a credential share one budget, and a token also spent by CI has a
    budget this package does not control. It also keeps reporting when the
    `github` check's rate-limit guard *skips a whole run* — which is exactly when
    the number is wanted — and reading `GET /rate_limit` does not count against the
    budget it reports, so it can run every minute beside a check that runs every
    fifteen. The reasoning is
    [ADR-0001](docs/adr/0001-a-second-check-type-in-this-package.md).
  - **`warn_below` / `error_below`** default to 1000 and 500, at the config's top
    level for every resource and inside a resource's own block for that one. They
    are counts, not a fraction of the limit — the number that matters during an
    incident is how many calls are left. `error_below` above `warn_below` is
    refused at load: no budget could ever land in the warning band.
  - **`resources:`** defaults to `core` and `graphql` and takes any key GitHub
    returns; write a resource with nothing under it to take the shared thresholds.
    Names are deliberately **not** validated at load — GitHub's set is open, so an
    allow-list would reject a resource that exists before it rejected a typo. A
    name GitHub does not report becomes a warning line instead of a missing one.
    Leave the key out to take the default set: a `resources:` that is *present and
    empty* is refused, because naming the key replaces the set and YAML cannot tell
    "empty" from "nothing" any other way.
  - Each line is keyed by GitHub's own resource name, so a maintenance pin held
    against `core` survives a config that starts watching `search` later.
  - **Readings it will not fake:** an unreadable endpoint reports that the *asking*
    failed rather than claiming a budget; a watched resource GitHub did not report
    is a warning line naming it; a row this check cannot read is a warning line for
    that resource alone, so one malformed budget never costs the others their
    reading; and a resource whose reported limit is not positive says nothing rather
    than going permanently red on an installation that does not rate-limit at all.
  - The token needs no scopes, but it has to resolve and it has to be one GitHub
    accepts: a reference that resolves to nothing pins the check to a visible ERROR
    before it runs, and a token GitHub rejects is reported as a failed read. What
    nothing catches is a token that authenticates as somebody else — the budget on
    the line is then plausible and belongs to the wrong credential, and the `limit`
    is the only tell.
  - Grading is on what is left and only on that. The reset time is on the line so a
    red budget that is about to refill is visible, but it does not soften the
    verdict.
  - **Two limits are named in the record**
    ([ADR-0001](docs/adr/0001-a-second-check-type-in-this-package.md), the update
    note). This is the **primary** budget: GitHub's *secondary* rate limits — the
    ones a burst trips — are reported by no endpoint and no header, and they arrive
    instead as `could not read` lines on your `github` check while this node stays
    green. An untroubled budget beside a rash of unreadable repositories is that
    signature, not a permission problem. And the `github` check's own pre-run guard
    reads `core` alone, which is the budget its calls are charged to — so its number
    and this check's `core` line are the same reading taken at different moments, and
    may legitimately disagree on a dashboard.
- **`owner:` may name a personal account, not only an organization.** The key
  always meant "the account whose repositories are in scope", but discovery only
  ever read `/orgs/{login}/repos`, which is a 404 for a person: the check reported
  `discovery failed: HTTP 404` and nothing else. A personal account is now
  discovered through its own listing.
  - A personal account's **private** repositories are listed by `/user/repos`
    (with `affiliation=owner`) when the check's own token belongs to that account.
    `/users/{login}/repos` returns public repositories only however privileged the
    token is, so when the private ones are out of reach the check's reading says
    `(public only)` rather than reporting a smaller scope as though it were whole.
- **`kind:` — `organization` or `user` — is required, and verified.** Declared,
  because it is what every load-time decision is taken from; verified against
  `GET /users/{login}` on every run, because a claim nobody checks decays and
  GitHub lets a personal account convert to an organization. A disagreement is a
  refusal naming both the config's claim and GitHub's answer.
  - **`team:` with `kind: user` is a config error**, refused at load rather than
    on the first run: only an organization has teams.
- **`advanced_security_on_private:`** — whether this account has GitHub Advanced
  Security on its private repositories. Code scanning and secret scanning are free
  on a public repository and paid on a private one, **for both kinds of account**,
  so the switch is about visibility and the kind only picks its default: `true`
  for an organization, `false` for a personal account. With it off, private
  repositories drop out of those two aspects entirely — and are **named on the
  aspect's node** — rather than every one of them being reported as "scanning not
  enabled". Every other aspect reads private repositories either way.
- **Any aspect can be switched off** with `enabled: false` in its own block. A
  switched-off aspect is skipped whole: no node, none of its API calls, and the
  rate-limit estimate shrinks with it. An aspect that says nothing is on, so an
  aspect a later release adds arrives switched on in configs written before it
  existed. Switching every aspect off is a config error. The one block not named
  after its aspect is `secret_scanning:`, which switches `secret_scanning_alerts`.
  - **A disabled aspect leaves no node, so any maintenance pin held against that
    node or a line under it is orphaned.** Clear them before switching one off.
  - The check's node names the switched-off aspects in its config summary: without
    it, "that aspect is off" and "the check is broken" look the same on the page an
    operator opens to find out which.
- **A severity band's leaf page says how it is graded.** Each band child now
  carries a `config` card naming the code its findings get and what an empty band
  reads as — `WARN when this band has findings` where no `severity_map` entry
  covers it, and it says so. A band's page previously showed a Time card and
  nothing about the grading that produced what was on it.
- **`max_pause:`** — how much of a run may be spent **asleep** waiting out a GitHub
  rate limit, in the same duration spelling `timeout:` takes. **Default: half of
  `timeout:`**, derived rather than fixed because `timeout:` is the number a
  deployment sizes to its own account.
  - **`timeout:` does not cover this.** A run that sleeps for its entire budget
    never overruns it — it just reports nothing, which is the wedged check a
    throttle is the easiest way to build.
  - A wait the budget cannot afford is **refused whole, not trimmed**: a wait
    shorter than the one GitHub asked for does not satisfy it, so taking it would
    spend the rest of the budget and still be throttled.
  - At the cap the run ends the way an exhausted `timeout:` ends it — the aspects
    that finished are kept and reported, the rest are absent, and the node says
    `run cut short after 4 of 7 aspects reported — a further 45s wait would pass
    its 30s pause budget`. The rejected alternative is worth naming because it is
    what a naive fix does: simply not passing GitHub's `retry-after` on does not
    stop the retry, it drops it to a one-second backoff — so a throttled run with
    thirty-nine repositories left would press a service that had just asked it to
    wait, thirty-nine more times.
  - **A `max_pause:` at or above `timeout:` is refused at load**, on the same
    argument as `error_below` above `warn_below`: the deadline would always end the
    run first, so the cap could never apply and the config summary would state a
    bound that does nothing.
- **A run that had to wait says so on the check's node** —
  `paused 47s for a GitHub rate limit`, beside the coverage and cut-short
  sentences. It grades nothing on its own: waiting when a service asks is correct
  behavior. What it stops is a paused run being indistinguishable from a slow one.

- **The reasoning behind the `github` type now ships, as two records.**
  [ADR-0003](docs/adr/0003-an-aspect-is-one-question-asked-of-the-whole-scope.md)
  is why the tree has seven children named after aspects rather than after your
  repositories, what one aspect is, and what the check's own node claims.
  [ADR-0004](docs/adr/0004-a-finding-grades-the-repository-does-not.md) is what a
  finding asserts and why each grading is the code it is — why a leaked secret and
  a missing dependency graph are both red, what a severity band means while it is
  empty, and the difference between `severities` (what is looked at) and
  `severity_map` (what it means). No behavior changes. If you have ever wanted to
  overrule one of these defaults, these are the records to overrule.

### Changed

- **`org:` → `owner:`, a hard cut.** A config still using `org:` is refused with a
  message naming the new key. The old name said something the value need not mean —
  which is how `org: m-31` came to be written for a person — and accepting both
  spellings forever would keep the misleading one alive in every config anybody
  copies. The value does not change.
- **The `{org}` token is `{owner}`.** A `subnodes:` text still writing `{org}` is
  refused at load: an unknown token is left as-is by the library, so it would
  otherwise have reached a dashboard as a literal `{org}`.
- **The organization security overview is no longer linked on a personal
  account.** `github.com/orgs/{login}/security/alerts/…` is an organization page
  and 404s for a person, so the three security aspects' shipped `about` text now
  carries the link as a token (`{advisories_link}`, `{code_scanning_link}`,
  `{secret_scanning_link}`) that expands to nothing there. A deployment's own
  `subnodes:` text may use the tokens.
- **That link no longer writes an empty team clause.** With no `team:` it used to
  render `team:` with nothing after it — a filter that matches nothing rather than
  an absent filter — so the "all open alerts" page opened empty. The three links
  are also now built the same way; one of them spelled the query parameter `q=`
  where the security overview reads `query=`.
- The shipped aspect text says **"the repositories in scope"** where it used to
  say "this team's repositories": there may be no team, and now there may be no
  organization either.
- The check's own node names the account kind — `scope: m-31 (user)` in the config
  summary, `no repositories in scope (user m-31)` in the coverage reading — and
  the discovery log line says which endpoint the scope came from.
- An aspect configuration block that is not a mapping is now a `CheckError` naming
  the key. Four of the seven were read with a bare `.get` and answered a scalar
  with an `AttributeError` naming neither the check nor the key.
- **The request, the budgets and the retry are the library's now**, and only what is
  actually about GitHub stays here — the auth and API-version headers, the `Link`
  page walk, and the throttle reader above. No behavior changes from the move itself;
  the two effects you can see are that requests identify themselves as
  `little-sister/<version>` rather than `Python-urllib/<x.y>`, which is a name a
  support thread can do something with and one an outbound filter is less likely to
  answer with an uninterpretable 403 — and that a response body is now read in bounded
  chunks against the run's clock, which closes the one hole `timeout:` and
  `request_timeout:` both left open: a server dribbling a byte at a time never trips a
  socket timeout, so neither budget stopped it.
- **A read failure can now say three things rather than two.** *We could not ask*,
  *GitHub answered no*, and — new — *GitHub answered with something this check cannot
  read*. The third used to be indistinguishable from a 404 on the line. All three grade
  exactly as before; the wording is what got more specific.

### Requires

- **`little-sister >= 0.3.12`**, up from 0.3.11, now for **two** reasons. This package
  imports `little_sister.transport` and `little_sister.fetch`, and 0.3.11 has neither —
  so a floor still naming it would let this install against a library missing every
  name it needs, and would say so only as an `ImportError` at startup.
  - The second reason is quieter and worth stating, because nothing would crash: the
    band glyphs above depend on 0.3.12 knowing that **a title with no word in it cannot
    stand in for a name** (little-sister ADR-0061). On an older library the check would
    run perfectly and every `/copy` would paste a bare 🔴. A floor that only ever
    guards imports would have missed this one.

  The two releases go out together, the library first. Still a **floor, never a pin**,
  and the check API epoch is still **1**: adding names to the surface does not move it.

## [0.1.0] - 2026-08-09

### Added

- **First release: the `github` check type**, extracted from the private
  deployment it grew up in. The behavior is unchanged — same tree, same aspect
  codes, same entry slugs — but the shipped per-aspect prose was rewritten to be
  deployment-neutral: remediation deadlines and internal practices are a
  deployment's policy and are now added with `{default}` (see below) rather than
  shipped. One node per configured team, with a child per aspect:
  open pull requests, Dependabot advisories, code-scanning alerts, secret-scanning
  alerts, SBOM presence, workflow runs and open issues. Discovery is org- or
  team-scoped and filtered by `name_prefix`, `include_archived` and
  `include_forks`.
- Every finding is an **individually addressable line**, slugged from GitHub's own
  per-repository numbering, so one alert can be put into maintenance while the
  rest of its aspect keeps reporting.
- The **per-aspect display text ships with the type** and expands `{org}` /
  `{team}` from the check's own config, so it is not copied per team. A deployment
  replaces a label in its own `subnodes:` block, or extends the shipped text by
  writing `{default}` into its own.
- A **rate-limit guard** (`rate_limit_safety_factor`) skips a run with a WARN
  rather than exhausting the API budget, and a **coverage backstop**
  (`expect_min_repos`) refuses to report a quiet green over an empty scope.
- The credential is a little-sister **secret reference** in the config's
  `secrets:` block, so each team's check carries its own token.

### Requires

- `little-sister >= 0.3.11` — the floor is 0.3.11, not the 0.3.0 that first
  promised the check-authoring surface this package imports: 0.3.0 was tagged but
  never uploaded, so it names no version a resolver can fetch, and it is 0.3.11
  that lowered the library's own Python floor to the one below.
  A **floor, never a pin**. The package declares
  check API epoch **1**; a library that has moved past that surface refuses at
  startup rather than failing as an import error from inside this package.
- **Python 3.11 or newer** — the library's floor, not a higher one of this
  package's own: a plugin that asked for more would mean somebody installs
  little-sister and then cannot install the package they came for. The claim is
  checked rather than declared — the type check runs against 3.11 on every commit
  and the whole suite against a real 3.11 on every release.
