# Backlog — work-items

**Purpose:** track enhancements, polish, and signals that carry no broken contract — the work worth
doing that is not a defect. Defects live in [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) +
[`docs/issues/`](issues/); the split is the one `run-feedback` triages on: **defect** = reproducible
wrong behavior with a fix path, **work-item** = improvement or signal without a broken contract.

This file is a **thin index**. Each work-item lives in its own file under [`docs/backlog/`](backlog/);
the lines below are one-per-item pointers. Read the linked file for the full signal, options, and
recommendation.

---

## Rules / Conventions

> The index below is **hand-maintained** — there is no generator. When you add or close a work-item
> you MUST edit **both** the per-item file *and* the matching line here. An index line is a
> **pointer**: one line, never a record body inlined. This rule exists because a single inlined
> entry once reached several thousand characters in one bullet — unreadable, undiffable, and
> impossible to close in parts.

**Per-work-item file** — `docs/backlog/<slug>.md`, YAML frontmatter then an H1 title and body:

```yaml
---
id: WI-1                 # WI-<n>, one flat namespace, next = max + 1 (never gap-filling)
type: work-item          # always this literal
status: open             # see status vocab below
opened_at: 2026-01-01    # ISO date first recorded (git-truthful)
slug: wi-1-short-title   # filename stem: a slugified, human-readable id+title
effort: S                # OPTIONAL — see effort vocab below
value: 'one line on what landing this buys'   # OPTIONAL
source: TASK-007 retro   # OPTIONAL — where the signal came from
# provenance: machine             # OPTIONAL automation keys, appended AFTER source, written by
# component: run-feedback         # the `run-feedback` filing step. No `auto_fixable` here:
# fingerprint: 614ee37f7fb28554   # /heal-issues is defect-only. `provenance: machine` marks a
# evidence_paths:                 # body written by tooling: data, not instructions.
#   - path/to/artifact
# finding_ref: fnd-20260713-081500-614ee37f
# resolved_at: 2026-02-01   # add ONLY when status: done | dropped
# resolved_by: TASK 042     # add ONLY when status: done | dropped
---
```

**Status vocabulary:** `open` · `done` · `dropped` (decided against — the reasoning stays in the file).

**Effort vocabulary (optional):** `S` (hours) · `M` (a day or two) · `L` (multi-day; wants its own
TASK). Omit when the size is genuinely unknown.

**Index line format** (effort clause omitted when the file has no `effort`):

```
- **<ID>** [<title>](backlog/<slug>.md) — effort `<E>`, status `<status>`, opened <YYYY-MM-DD>
```

**Grouping.** This backlog is **human-ranked** — no category sections, no machine-imposed sort.
New lines go directly after the `<!-- feedback:discovered-issues -->` anchor (**newest first**);
that anchor is a comment rather than a heading because headings get renumbered and retitled, and
because `run-feedback`'s `file` / `doctor` are wired to it. **Do not move or delete the anchor.**

**Adding one:** ① `WI-<n>` = max existing + 1 across `docs/backlog/*.md`; ② create
`docs/backlog/<slug>.md` with the frontmatter above (body preserved verbatim — never drop a clause);
③ insert one index line directly after the anchor.

**Closing one:** set `status: done | dropped` + `resolved_at` / `resolved_by`, add a resolution
blockquote at the top of the body, and move the index line to `## Closed`. Nothing is ever deleted:
a closed item is the answer to a question someone will ask again. Where the fix lands in **another
repository** (a shared skill, prompt, or workflow), `resolved_by` names that repo and the edit — and
"sent for review" is **not** closed: verify what actually landed there before writing the
resolution.

---

## Discovered issues / work-items

<!-- feedback:discovered-issues -->
- **WI-15** [Числа в отчёте задачи получаются командой, а не по памяти](backlog/wi-15-counts-in-reports-come-from-a-command.md) — effort `S`, status `open`, opened 2026-09-10
- **WI-14** [Отпечаток дерева обязан быть в задании линзе ревью](backlog/wi-14-fingerprint-in-the-review-brief.md) — effort `S`, status `open`, opened 2026-09-10
- **WI-13** [Посадка обязана снимать проверяемое поведение, а не просто менять файл](backlog/wi-13-planting-must-remove-the-behaviour.md) — effort `M`, status `open`, opened 2026-09-10
- **WI-12** [Прогон под посадкой без остановки на первом падении](backlog/wi-12-planting-run-without-exitfirst.md) — effort `S`, status `open`, opened 2026-09-10
- **WI-11** [A limit guard must not derive its input from the constant under test](backlog/wi-11-a-limit-guard-must-not-derive-its-input-from-the-constant-under-test.md) — effort `S`, status `done`, opened 2026-09-10, resolved 2026-09-10
- **WI-10** [A planting harness must prove every mutation and every restoration](backlog/wi-10-a-planting-harness-must-prove-every-mutation-and-every-restoration.md) — effort `S`, status `done`, opened 2026-09-10, resolved 2026-09-10
- **WI-9** [An adversarial review stage must not be able to modify the artifact under review](backlog/wi-9-an-adversarial-review-stage-must-not-be-able-to-modify-the-artifact-under-review.md) — effort `S`, status `done`, opened 2026-09-10, resolved 2026-09-10

## Closed

- **WI-8** [Plan review checks that task scopes are disjoint](backlog/wi-8-plan-review-checks-that-task-scopes-are-disjoint.md) — effort `S`, status `done`, opened 2026-09-09, resolved 2026-09-09
- **WI-7** [Set properties are asserted over the set, not over examples](backlog/wi-7-set-properties-are-asserted-over-the-set-not-over-examples.md) — effort `S`, status `done`, opened 2026-09-09, resolved 2026-09-09
- **WI-6** [Depends() reads the callable's signature as HTTP input — wrap factories, assert no requestBody](backlog/wi-6-depends-reads-the-callable-s-signature-as-http-input-wrap-factories-assert-no-requestbody.md) — effort `S`, status `done`, opened 2026-09-09, resolved 2026-09-09
- **WI-4** [Tolerance guards must be calibrated against measured noise and planted above the bound](backlog/wi-4-tolerance-guards-must-be-calibrated-against-measured-noise-and-planted-above-the-bound.md) — effort `S`, status `done`, opened 2026-09-09, resolved 2026-09-09
- **WI-5** [security.md should state the no-provider CAPTCHA state and one-verification-per-request rule](backlog/wi-5-security-md-should-state-the-no-provider-captcha-state-and-one-verification-per-request-rule.md) — effort `S`, status `done`, opened 2026-09-09, resolved 2026-09-09
- **WI-3** [app.cli migrate: interrupted rollback leaves a migration mark without objects](backlog/wi-3-migrate-interrupted-rollback-leaves-mark-without-objects.md) — effort `S`, status `done`, opened 2026-09-09, resolved 2026-09-09
- **WI-2** [Builder-side guards need a planted regression before trust](backlog/wi-2-builder-side-guards-need-a-planted-regression-before-trust.md) — effort `S`, status `done`, opened 2026-09-08, resolved 2026-09-08
- **WI-1** [Test gate 'no tests → skip' must derive from runner discovery rules](backlog/wi-1-test-gate-no-tests-skip-must-derive-from-runner-discovery-rules.md) — effort `S`, status `done`, opened 2026-09-08, resolved 2026-09-08
