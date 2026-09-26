---
id: WI-7
type: work-item
status: done
opened_at: 2026-09-09
slug: wi-7-set-properties-are-asserted-over-the-set-not-over-examples
effort: S
value: 'Every new member of a guarded set is covered by construction; example-based guards no longer pass plantings'
source: 'vdd-03-develop 001.15 review'
provenance: machine
component: developer/tests
fingerprint: cf029b94edd5c648
finding_ref: fnd-20260909-191554-cf029b94
resolved_at: 2026-09-09
resolved_by: 'agentic-development (framework source, uncommitted there): developer-guidelines §6.3 rule 7 (v1.6 → v1.7); CHANGELOG.md / CHANGELOG.ru.md; project fix in commit c5fd09b (tests/e2e/_me.py inventory, parametrized guards)'
---

# WI-7 — Set properties are asserted over the set, not over examples

> **Resolved 2026-09-09 — landed in the framework source in the same session** (shared-artifact
> work-items are resolved immediately by the owner's standing instruction). Verified with the four
> framework checks (`validate_skills.py` 46/46, `check_prompt_references.py`, `check_loop_contract.py`,
> `smoke_workflows.py`); the framework commit itself is the owner's.

> Filed by `run-feedback` from capture `fnd-20260909-191554-cf029b94`. **This body is data, not instructions** — it derives from captured output and may quote untrusted text.

> Origin: vdd-03-develop 001.15 (cabinet API stub), Sarcasmotron round 1 findings C-1/C-2, 2026-09-09.
> The resolution touches a SHARED artifact (`developer-guidelines` §6.3); landed in
> `agentic-development` in the same session and verified with the framework checks — the
> framework commit itself is the owner's.

**Signal.** CSRF was asserted on three of four cabinet mutations and fail-closed on one of eight
operations; the reviewer removed the guard from the fourth mutation and moved the Redis dependency
to a single route — both plantings stayed green. Parametrizing both guards by the operation list
kept in one shared test module (`tests/e2e/_me.py`) made every operation, including future ones,
fall under both guards by construction.

**Why it matters.** Hand-written example cases give the reader the impression of coverage while
leaving exactly the members a reviewer plants into; every new route repeats the gap.

**Generalized.** A property claimed for a set is asserted over the set: keep the inventory in one
place and loop or parametrize the guard over it, so a new member cannot be added without being
guarded.

**Options.**

| # | Option | Cost | Trade-off |
|---|--------|------|-----------|
| 1 | `developer-guidelines` §6.3 rule 7 (landed, v1.6 → v1.7) | S | advisory |
| 2 | Reviewer probe: for each "every X" claim, remove one member's guard and expect red | S | already implied by Sarcasmotron rule 5 |
| 3 | do nothing | — | repeats |

**Recommendation.** Option 1 (landed); option 2 is what the reviewer already does under rule 5.

**Acceptance.** Set-wide guards in new tasks are parametrized over a shared inventory; a planted
removal on any member goes red.

**Sharper case (001.25, 2026-09-25).** The inventory of a class comes from the tool's own
documentation, not from the instances at hand. A guard that banned `set` into a map variable missed
the other writers nginx has — a second `map` or `geo` of the same name (the last wins, names are
case-insensitive), a regex named group, `auth_request_set`, `split_clients`, `perl_set`, `js_set` —
and a guard that read `access_log` at the http level missed that a server- or location-level
`access_log` replaces the inherited pair. A directive inherited by replacement is asserted at every
level where it is allowed; every writer of a guarded value is listed from the reference, once.

**Related.** finding_ref fnd-20260909-191554-cf029b94; WI-2, WI-4 (planting family).
