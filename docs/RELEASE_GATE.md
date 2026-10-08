# Reflex v0.1 Release Gate

This gate has exactly 14 items, in the order defined by the v0.1 specification.
Gates 1–4 and 9 require evidence from the selected real-agent release case.
Gates 5–8 and 10–14 currently assess only the retained controlled fixture's
properties; those PASS results do not automatically carry over to a future real
release case. All 14 items must be reassessed for that case. Synthetic fixtures
and parsing real saved sessions do not alone prove a real end-to-end release
case. A FAIL or EXTERNAL VALIDATION REQUIRED blocks release. No assumed PASS is permitted.

Current result: **9 local/controlled PASS, 5 EXTERNAL VALIDATION REQUIRED. Release is blocked.** PASS entries below are
limited to the retained controlled synthetic fixture and its artifacts; they do
not claim real-agent end-to-end validation. The fixture's Evaluation is
`rejected`, and its Lesson remains `candidate`.

Evidence paths are relative to this document. The [benchmark report](../benchmarks/README.md)
describes the live-provider infrastructure gaps and the controlled trial. The
[retained trial artifacts](../benchmarks/results/csv-fixture-neutral-20261008/)
include the preregistration, replay, evaluation, lesson, session, failure, calls,
repository-state, stats, summary, artifact hashes, SQLite database, and base Git
bundle.

| # | Release Gate | PASS condition | FAIL condition | Current | Evidence |
|---:|---|---|---|---|---|
| 1 | Real Session | The selected release case is traced to an actual coding-agent session, with provenance preserved through import. | The case relies only on a fixture, or its real-session provenance cannot be established. | **EXTERNAL VALIDATION REQUIRED** — retained case is synthetic. Real saved-session parsing does not establish this case's provenance. | [summary](../benchmarks/results/csv-fixture-neutral-20261008/summary.json), [session](../benchmarks/results/csv-fixture-neutral-20261008/session.json), [synthetic transcript](../benchmarks/results/csv-fixture-neutral-20261008/synthetic-session.jsonl) |
| 2 | Real Failure | The selected real session contains a supported, evidenced failure that is reproduced for the release case. | The failure is synthetic, unsupported, or not reproducible under the recorded baseline. | **EXTERNAL VALIDATION REQUIRED** — the retained failure evidence is synthetic. | [failure](../benchmarks/results/csv-fixture-neutral-20261008/failure.json), [replay](../benchmarks/results/csv-fixture-neutral-20261008/replay.json) |
| 3 | Real Correction | A real correction linked to the selected real failure and subsequent successful behavior is preserved as evidence. | The correction is synthetic, missing, or cannot be linked to the real failure and successful behavior. | **EXTERNAL VALIDATION REQUIRED** — the retained correction chain originates from synthetic evidence. | [session](../benchmarks/results/csv-fixture-neutral-20261008/session.json), [calls](../benchmarks/results/csv-fixture-neutral-20261008/calls.json), [failure](../benchmarks/results/csv-fixture-neutral-20261008/failure.json) |
| 4 | Generated Lesson | A candidate Lesson is generated from the selected real failure/correction evidence and its provenance, audit, and frozen hash are recorded. | No Lesson is generated, or the release-case Lesson lacks real evidence provenance. | **EXTERNAL VALIDATION REQUIRED** — a candidate Lesson exists, but it is derived from a synthetic session; real saved-session parsing elsewhere does not prove real release-case generation. | [lesson](../benchmarks/results/csv-fixture-neutral-20261008/lesson.json), [preregistration](../benchmarks/results/csv-fixture-neutral-20261008/preregistration.json) |
| 5 | Replay Isolation | Each run uses an independent clone and isolated state; original benchmark repository state is unchanged. This does not claim a perfect hostile-code sandbox. | Runs share mutable repository state, the original repository changes, or required isolation evidence is missing. | **PASS — controlled fixture evidence only.** | [replay](../benchmarks/results/csv-fixture-neutral-20261008/replay.json), [repository state](../benchmarks/results/csv-fixture-neutral-20261008/repository-state.json), [summary](../benchmarks/results/csv-fixture-neutral-20261008/summary.json) |
| 6 | Fair CONTROL vs REFLEX | Recorded case inputs and runner configuration match; the frozen Lesson injection is the sole intended condition difference. | A material condition mismatch exists or parity cannot be determined. This fixture result does not establish authenticated-provider parity. | **PASS — controlled fixture evidence only.** | [preregistration](../benchmarks/results/csv-fixture-neutral-20261008/preregistration.json), [replay](../benchmarks/results/csv-fixture-neutral-20261008/replay.json), [calls](../benchmarks/results/csv-fixture-neutral-20261008/calls.json) |
| 7 | Multiple Runs | At least two independent runs per condition are recorded; the configured default is three. | The minimum is not met or run evidence is missing/incomplete. | **PASS — 3 CONTROL and 3 REFLEX fixture runs.** | [preregistration](../benchmarks/results/csv-fixture-neutral-20261008/preregistration.json), [replay](../benchmarks/results/csv-fixture-neutral-20261008/replay.json) |
| 8 | Deterministic Evaluation | Repeated evaluation of identical recorded evidence yields the same result, with evidence persisted. | Repeated results differ or evaluation cannot be reproduced from persisted evidence. | **PASS — 10 identical fixture evaluations recorded.** | [summary](../benchmarks/results/csv-fixture-neutral-20261008/summary.json), [evaluation](../benchmarks/results/csv-fixture-neutral-20261008/evaluation.json), [SQLite evidence](../benchmarks/results/csv-fixture-neutral-20261008/reflex.db) |
| 9 | Measured Improvement | A valid real-agent comparison meets Phase 7 Condition A or B, without required-check regression. | No improvement is demonstrated, the comparison is invalid, or the evidence is not from the selected real-agent release case. | **EXTERNAL VALIDATION REQUIRED** — fixture result is neutral: 0/3 versus 0/3; Evaluation is `rejected`; no improvement is claimed. | [evaluation](../benchmarks/results/csv-fixture-neutral-20261008/evaluation.json), [summary](../benchmarks/results/csv-fixture-neutral-20261008/summary.json), [stats](../benchmarks/results/csv-fixture-neutral-20261008/stats.json) |
| 10 | No Benchmark Leakage | Preregistration and recorded runs show that no hidden answer or benchmark-specific answer material was supplied to REFLEX. | Answer-bearing material or post-result case changes contaminate the comparison. | **PASS — controlled fixture evidence only.** | [preregistration](../benchmarks/results/csv-fixture-neutral-20261008/preregistration.json), [calls](../benchmarks/results/csv-fixture-neutral-20261008/calls.json), [replay](../benchmarks/results/csv-fixture-neutral-20261008/replay.json) |
| 11 | No Lesson Leakage | The frozen Lesson passes the recorded mechanical audit, retains the same hash through Replay, and contains behavioral guidance rather than a copied answer. | The Lesson includes answer-bearing patch details, prohibited coordinates/code, unresolved leakage suspicion, or a changed frozen hash. | **PASS — controlled fixture Lesson audit only; not a general leakage guarantee.** | [lesson](../benchmarks/results/csv-fixture-neutral-20261008/lesson.json), [preregistration](../benchmarks/results/csv-fixture-neutral-20261008/preregistration.json) |
| 12 | No Fabricated Metrics | Reported values come from recorded measurements; unavailable values remain unavailable. | Missing values are represented as fabricated zeroes or other invented measurements. | **PASS — fixture evidence preserves unknown token/tool metrics as null/N/A.** | [replay](../benchmarks/results/csv-fixture-neutral-20261008/replay.json), [stats](../benchmarks/results/csv-fixture-neutral-20261008/stats.json), [summary](../benchmarks/results/csv-fixture-neutral-20261008/summary.json) |
| 13 | Repository Safe | The original benchmark repository remains unchanged, and run changes are confined to their independent clones. | The original repository is modified or evidence of its state is absent. | **PASS — controlled fixture repository-state evidence only.** | [repository state](../benchmarks/results/csv-fixture-neutral-20261008/repository-state.json), [summary](../benchmarks/results/csv-fixture-neutral-20261008/summary.json) |
| 14 | Privacy Safe | No credentials or unrelated private session/source data are included in retained benchmark artifacts or sent by the fixture runner. | Credentials or unrelated private data are exposed or retained. | **PASS — retained synthetic fixture artifacts only.** This does not certify future live-provider handling. | [benchmark report](../benchmarks/README.md), [preregistration](../benchmarks/results/csv-fixture-neutral-20261008/preregistration.json), [artifact hashes](../benchmarks/results/csv-fixture-neutral-20261008/artifact-sha256.json) |

## Status semantics

`replay_confirmed` means only that Replay evidence supports improvement under
the tested conditions. It does not establish correctness or generalization to
other tasks. In the retained Phase 11 trial, Phase 7 returned `rejected`; the
Lesson is still `candidate`. No promotion or Apply occurred.

The saved Codex and Claude session parser checks are a separate validation
category from authenticated live execution. Live Codex and live Claude execution
are both **INFRASTRUCTURE BLOCKED** in the current environment. This is a
validation gap, not evidence of an implementation failure, and it does not turn
any of Gates 1–4 or 9 into PASS. These are unvalidated requirements, not demonstrated implementation failures.

CI completion and authenticated live integration remain separate follow-up
work. A future release review must reassess every item against its real release
case evidence; this document does not approve a release or waive a failed gate.

## Local implementation closure

The existing components are now connected through Init/Learn/Replay CLI, with
explicit agent execution, unchanged Lesson lifecycle and deterministic fixture
coverage. [CLI regression tests](../tests/test_release_cli.py) exercise canonical
synthetic import through Stats, candidate provenance, isolation, baseline and
error handling. [CI](../.github/workflows/tests.yml) is configured locally; remote
execution and real provider validation remain separate. All real-case Gates must
be reassessed after the [external procedure](FOLLOW_UP.md); local tests do not
prove measured real-agent improvement.

Closure validation: 64 focused tests and 440 deterministic tests from a fresh
wheel installation passed. No concrete HIGH/BLOCKER remains in the bounded
independent CLI review. Authenticated Codex/Claude and remote CI were not run;
these test results do not close real-agent evidence or improvement requirements.
