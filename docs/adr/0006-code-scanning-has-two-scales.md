# ADR-0006 — A code-scanning alert has two severities, and the check keeps them apart

- **Status:** Accepted
- **Date:** 2026-08-16 (the split shipped in 0.1.1; this record is where its reasoning
  lives for the people who receive it, which the release notes were the only place
  for until now)
- **Related:** [ADR-0003](0003-an-aspect-is-one-question-asked-of-the-whole-scope.md)
  (what an aspect is — this record makes one of its seven into two),
  [ADR-0004](0004-a-finding-grades-the-repository-does-not.md) (how a banded aspect
  grades; the defaults below are its decision 8 read per scale),
  [ADR-0001](0001-a-second-check-type-in-this-package.md) (the API budget the
  single-read split leaves untouched)

A bare ADR number here is this repository's; a reference to one of little-sister's
is always written out, because the two numbering spaces overlap.

## Context

GitHub gives a code-scanning alert **two** severities, and files them under two
headings of its own: *Security* for the alerts whose rule carries a security severity
(`critical`, `high`, `medium`, `low`), *Other* for the rest, graded by the rule's own
analysis severity (`error`, `warning`, `note`). The first `code_scanning_alerts` aspect
read one field and rendered one eight-band row from it, with two consequences worth
a break. **Three of the eight bands could never be non-zero** — `error`, `warning` and
`note` were watched, permanently green and permanently unreachable, because nothing
ever classified an alert into them. And **one `severity_map` forced one answer onto
both scales**: the shipped default answered `ERROR` for everything, a `note` included,
so a dashboard went red for a lint finding.

## Decision

1. **Two aspects, one per scale.** `code_scanning_security` holds the alerts GitHub
   gave a security severity, as bands `critical` / `high` / `medium` / `low`;
   `code_scanning_quality` holds everything else, by analysis severity, as bands
   `error` / `warning` / `note`. Each declares only the bands it can fill, and carries
   a `severity_map` and a default of its own. The two scales never appear in one row.
2. **One read serves both.** Both aspects are built from a single
   `/code-scanning/alerts` read per repository, and the pre-run rate estimate counts
   distinct endpoints rather than aspects, so the eighth aspect reserves no call the
   run never makes.
3. **The defaults differ, on purpose.** `code_scanning_security` grades every band
   **ERROR**: an alert GitHub gave a security severity is a vulnerability in your own
   code, and the mildest one is still that. `code_scanning_quality` grades `error` and
   `warning` **WARN** and `note` **OK**: red on a status dashboard means *act now*, and a
   non-security finding is not that, however CodeQL grades its rule. A deployment that
   wants the old answer says so in the new block.
4. **The old key is refused, not migrated.** A `code_scanning_alerts:` block stops the
   check at load, naming both halves, rather than starting with a guess or silently
   dropping a grading. The node paths moved with the aspects, so every maintenance pin
   held on `…/code_scanning_alerts/<band>` stopped matching; an unmatched pin is
   suspended, never deleted, and the release notes told deployments to re-pin.

## Consequences

- The check has **eight aspects**: five flat ones (`pull_requests`,
  `secret_scanning_alerts`, `sbom_check`, `actions`, `issues`) and **three banded**
  ones (`security_advisories` and the two above). ADR-0003's table and ADR-0004's
  counts are read with that in mind; both records say so at their head.
- The severity rows share one glyph ramp across two scales, and the repeats are the
  point: `error` and `high` are comparable rungs of scales GitHub keeps apart, and
  since the split they never sit in one row (`BAND_GLYPHS` in `github.py`).
- The run trace shows `code_scanning_quality` with `0 read(s)`, which is decision 2
  made visible rather than a defect.

## Alternatives considered

- **Keep one aspect and grade by the security severity when it is there.** Rejected:
  the unreachable bands stay, and one map still answers for two scales.
- **Migrate `code_scanning_alerts:` automatically.** Rejected: a grading a deployment
  wrote for one scale cannot be split into two honestly without asking, and a check
  that starts on a guess about what red means is the failure this package exists to
  avoid.
