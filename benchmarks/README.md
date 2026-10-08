# Phase 11 controlled pipeline benchmark

This benchmark uses a **synthetic fixture**, not an authenticated coding agent
and not a replay of a real historical coding-agent session. It exercises the
existing adapters, failure/correction analysis, Lesson audit/freeze, independent
clones, persistence, deterministic evaluator, and measured Stats.

The case is a small CSV parser with deterministic unittest checks for quoted
delimiters and escaped quotes. The fixture makes the same deliberately limited
edit in both conditions. It accepts the injected Lesson but does not implement
learning behavior. A neutral result is expected; this cannot demonstrate that
Reflex improves real agents.

Each invocation requires a fresh output directory. Before measured execution it
records the exact base commit, task, required checks, frozen Lesson, runner,
timeouts, and three runs per condition. Setup and historical fixture observations
are separate from measured Replay trials. Existing Phase 6 ordering executes the
CONTROL baseline before REFLEX; clones and HOME/state directories are independent.

The runner has no network or provider dependency and receives no credentials.
Tokens and agent tool-call metrics remain unknown. Subprocess elapsed time,
exit status, and check outcomes are actually measured. Evaluation uses only the
persisted Replay report; no Lesson is automatically promoted or applied.

## Live integration validation

**LIVE CODEX VALIDATION: INFRASTRUCTURE BLOCKED**

Reason: The current parent sandbox prohibits nested sandbox-exec, and the
installed Codex CLI does not provide a help-documented configuration that
simultaneously guarantees authenticated network execution and filesystem
confinement sufficient to prove the Reflex repository cannot be modified.

Classification: Validation gap, not Reflex implementation failure.

Production changes caused by failed smoke attempts: None.

**LIVE CLAUDE VALIDATION: INFRASTRUCTURE BLOCKED**

Local help exposes options to reduce tools and persistent state, but does not
establish confinement of the entire CLI process. The same unavailable host
filesystem boundary prevents the required safety proof. No live Claude request
was attempted. Claude transcript import support is separate from live Replay;
no canonical Claude live runner has been validated.

No network smoke or nested sandbox retry is part of this benchmark.

## Recorded controlled result

The primary trial is **VALID NEUTRAL RESULT**: CONTROL 0/3, REFLEX 0/3.
The baseline and comparison passed integrity checks; Phase 7 returned `rejected`.
The frozen Lesson remains `candidate`; nothing was promoted or applied.
Token and tool-call metrics are unavailable. Runtime is measured, but no
efficiency improvement is claimed.

Base commit: `08f8f4c949d280984cf1b17551ae36f2df2f8020`.

[Preregistration](results/csv-fixture-neutral-20261008/preregistration.json),
[Replay](results/csv-fixture-neutral-20261008/replay.json),
[Evaluation](results/csv-fixture-neutral-20261008/evaluation.json),
[Stats](results/csv-fixture-neutral-20261008/stats.json), and
[summary](results/csv-fixture-neutral-20261008/summary.json) are retained with
the unchanged SQLite evidence, synthetic transcript, base Git bundle, and
artifact hashes. The original trial repository remains outside Reflex at
`/private/tmp/reflex-phase11-csv-20261008-run1/repository`.

To reproduce the offline pipeline in a new directory:

```sh
PYTHONDONTWRITEBYTECODE=1 uv run python -B tools/controlled_benchmark.py \
  --output /private/tmp/reflex-controlled-new-trial --execute
```

This result verifies the fixture pipeline, not authenticated provider execution
or real-agent learning. It does not satisfy release evidence of measured
improvement in a real coding agent.
