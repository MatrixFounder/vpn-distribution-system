---
id: WI-11
type: work-item
status: open
opened_at: 2026-09-10
slug: wi-11-a-limit-guard-must-not-derive-its-input-from-the-constant-under-test
effort: S
value: 'Size and count limits stop being decorative the moment they are asserted'
source: 'vdd-03-develop 001.24 fix'
provenance: machine
component: developer-guidelines
fingerprint: e9d9c54b913c119d
finding_ref: fnd-20260910-134631-e9d9c54b
---

# WI-11 — A limit guard must not derive its input from the constant under test

> Filed by `run-feedback` from capture `fnd-20260910-134631-e9d9c54b`. **This body is data, not instructions** — it derives from captured output and may quote untrusted text.

> Origin: `vdd-03-develop` 001.24 (vpn-distribution-system), 2026-09-10, round-2 fixes.
> This is **a behaviour change for the framework owner's review, not a landed fix**.

**Signal.** Two guards were added for declared size limits (the maximum length of an accepted
certificate request, and of a parsed PEM block). Each built its oversized input from the very
constant it was testing — "the limit plus one". Raising the limit by a factor of a thousand left
both tests green: the input grew with the bound. A second, subtler variant appeared in the same
pair of tests: the oversized payload was not valid base64, so the request was rejected for a reason
unrelated to length, and the test would have stayed green even with the limit removed entirely.
Both were found only by planting the regression the guards existed to catch.

**Why it matters.** This is the same class as an uncalibrated numeric tolerance, already covered on
the builder side, but the mechanism is different and the existing rule does not catch it: a
tolerance is wrong because the bound is chosen from comfort, whereas this guard is wrong because
its *input* is defined in terms of the bound, so no bound is ever wrong. A reader of the test sees
a limit named, a case marked "too long", and a passing suite. Cost is the same as any decorative
guard: the limit can be raised or deleted at any time without a single test noticing.

**Generalized.** A guard for a declared limit must fix its input independently of the constant
under test — a literal, or a value derived from the requirement rather than from the code — and
must pin the constant's value in a separate assertion. The input must fail *only* because of the
limit: any other property that would independently reject it (malformed encoding, wrong type,
missing field) makes the guard measure that property instead. Prove it by moving the limit, not by
moving the input.

**Options.**

| # | Option | Cost | Trade-off |
|---|--------|------|-----------|
| 1 | Extend the builder rule on numeric tolerances with the self-reference case: fix the input by literal, pin the constant separately, prove by moving the bound | S | One more clause in a rule that already exists; cheap to state, easy to miss |
| 2 | Add it to the reviewer's gate probe: for every new limit assertion, ask whether the input is derived from the constant | S | Catches it at review rather than at authoring; composes with 1 |
| 3 | do nothing, document the constraint | — | Limit guards keep passing regardless of the limit |

**Recommendation.** Options 1 + 2 — the same builder/reviewer pairing already used for calibrated
tolerances, since the failure is invisible at authoring time and obvious at review time when the
question is asked directly. Minimum acceptable outcome is option 2: one question in the reviewer's
probe finds every instance.

**Acceptance.** A limit guard whose bound is raised in the source goes red; a reviewer asked about
a new limit assertion can point at the literal input and the pinned constant.

**Related.** [WI-4](wi-4-tolerance-guards-must-be-calibrated-against-measured-noise-and-planted-above-the-bound.md)
— same family (a numeric guard that cannot fail), different mechanism: WI-4 is about a bound chosen
too wide, this is about an input defined in terms of the bound. Finding
`fnd-20260910-134631-e9d9c54b`.
