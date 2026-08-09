# Changelog

All notable changes to little-sister-github, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/). These notes are for the
deployments that install this package, not a work log.

Treat **a new or renamed `type:` name, a changed slug shape, or a removed config
key as a breaking change** and say so at the top: every deployment with
maintenance pins or dashboards built on those is affected, even though nothing in
the code says so.

## [Unreleased]

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
