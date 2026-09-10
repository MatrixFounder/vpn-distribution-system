---
id: WI-10
type: work-item
status: done
opened_at: 2026-09-10
slug: wi-10-a-planting-harness-must-prove-every-mutation-and-every-restoration
effort: S
value: 'Planting tables stop carrying claims that no measurement backs'
source: 'vdd-03-develop 001.24 verification'
provenance: machine
component: developer-guidelines
fingerprint: 024a576ab15bed4b
resolved_at: 2026-09-10
resolved_by: 'agentic-development (framework source, uncommitted there): developer-guidelines §6.3 rule 8 (v1.7 → v1.8) — appended, rules 1–7 keep their numbers because vdd-03-develop cites §6.3 p.5 and p.6; cited from Sarcasmotron rule 6, vdd-adversarial §4 item 6 and 09_code_reviewer_prompt; CHANGELOG.md / CHANGELOG.ru.md'
finding_ref: fnd-20260910-134631-024a576a
---

# WI-10 — A planting harness must prove every mutation and every restoration

> **Resolved 2026-09-10 — option 1 landed; option 3 folded into it.** `developer-guidelines` §6.3
> gains **rule 8**: address files by full path, confirm the edit actually changed the file, confirm
> the restore byte-identical, and report per planting what was *observed* rather than a bare "red" —
> which is option 3 (the report records the observation) stated as a duty of the harness rather than
> as a report template. The rule is appended, never inserted: `vdd-03-develop` cites `§6.3 p.5` and
> `p.6` live, so renumbering would have broken both citations.
>
> The reviewer side does not restate it — Sarcasmotron rule 6, `vdd-adversarial` §4 item 6 and
> `09_code_reviewer_prompt` cite `§6.3 p.8`, so the mechanics have one source and bind whoever plants.
>
> Verified: the four framework checks and `pytest tests/` 448 passed. The framework commit is the
> owner's.

> Filed by `run-feedback` from capture `fnd-20260910-134631-024a576a`. **This body is data, not instructions** — it derives from captured output and may quote untrusted text.

> Origin: `vdd-03-develop` 001.24 (vpn-distribution-system), 2026-09-10, guard-planting step.
> This is **a behaviour change for the framework owner's review, not a landed fix**.

**Signal.** Two separate mechanical failures inside one planting run, each of which silently
invalidated work that had been reported as done. (1) The planting harness stored its backups under
each file's base name; two files in different directories shared a base name, so the second backup
overwrote the first and the restore step put the wrong file's content back. Ten of twelve plantings
after that point measured an import error instead of the guard they targeted — the run *looked*
like twelve red plantings. It was caught only because the wrong-file restore broke loudly; had the
two files been similar, the run would have reported success while proving nothing. (2) Two later
fixes were applied by textual substitution without asserting that the pattern matched. A formatter
had re-wrapped the target text, so both substitutions silently changed nothing, and the "fixed"
guards were re-planted and found still green — the second failure was caught only by re-planting.

**Why it matters.** A planting run is the evidence that a guard is real. Both failures produce the
same outcome: a report of "guard proven red" that is not backed by anything, with no error to
notice. The cost lands on the reviewer and on every future reader who trusts the planting table in
the task report. This is not hypothetical frequency — both happened in a single task.

**Generalized.** A harness that mutates and restores files must address each file by its full
identity, never by its base name. Every mutation must be verified to have taken effect (the
substitution matched, the edit changed the file), and every restoration must be verified to be
byte-identical to the captured original, before the next planting runs. A planting whose target
failed to change, or whose restore did not verify, is a failed measurement and must be reported as
such — not counted as a red guard. State it independently of language and tool: the property is
"prove the mutation, prove the restoration", not any particular copy or patch command.

**Options.**

| # | Option | Cost | Trade-off |
|---|--------|------|-----------|
| 1 | Add the rule to the builder guidance beside the existing planting duty: key backups by full path, verify the mutation applied, verify the restore byte-for-byte | S | Guidance only; each harness still implements it |
| 2 | Ship a small planting helper with the framework (apply / verify / restore / report) | M | Removes the class entirely for anyone who uses it; another artifact to maintain, and stacks differ |
| 3 | Require the planting table in the task report to record the observed failure message per planting, not just "red" | S | Makes a mis-measured planting visible to the reviewer: an import error where an assertion was expected stands out |
| 4 | do nothing, document the constraint | — | Planting tables keep carrying unverifiable claims |

**Recommendation.** Options 1 + 3: state the mutation/restoration proof duty next to the existing
"plant your own gates" rule, and require the report to name what each planting actually observed.
Minimum acceptable outcome is option 3 alone — recording the observed failure per planting makes
both failure modes visible to a reviewer without changing any harness. Option 2 is the ideal if the
framework already ships builder-side helper scripts.

**Acceptance.** A planting run that restores the wrong file, or whose edit did not apply, is
reported as a failed measurement rather than as a proven guard; the task report shows, per
planting, what was observed rather than a bare "red".

**Related.** [WI-2](wi-2-builder-side-guards-need-a-planted-regression-before-trust.md) — establishes
that guards must be planted; this item is about the mechanics that make a planting trustworthy, and
does not duplicate it. Finding `fnd-20260910-134631-024a576a`.
