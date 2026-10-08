# Release Closure and External Validation

## Local closure

`init`, `scan`, `learn`, `replay`, `apply`, and `stats` are implemented. Init is
repository-bound and idempotent; Learn uses imported canonical evidence and the
existing analysis/audit/freeze APIs. Replay requires a canonical case JSON and
explicit `--execute`, uses the existing Engine/Evaluator, and never automatically
changes Lesson status. The supported CLI runner is CodexRunner; Claude support
currently means saved-session parsing, not authenticated live Replay.

The [CI configuration](../.github/workflows/tests.yml) installs the package,
runs the deterministic suite, and checks help without provider authentication.
Local syntax/command validation is distinct from a passing GitHub Actions run.
No GitHub repository URL has been invented. Remote CI and all publication remain
unexecuted. Wheel/sdist installation is tested locally; private state and retained
benchmark databases/bundles are excluded from package archives.

The [Release Gate](RELEASE_GATE.md) has 9 controlled/local PASS entries and 5
**EXTERNAL VALIDATION REQUIRED** entries. None of those five is release PASS.
The historical synthetic trial remains unchanged: CONTROL 0/3, REFLEX 0/3,
Evaluation `rejected`, Lesson `candidate`. Live providers are infrastructure
blocked here. CLI wiring does not remove that host confinement gap.

Local validation: **64 focused tests passed** and **440 full-suite tests passed**
using the wheel in a fresh Python 3.14 virtual environment. Global help and all
six command help pages passed outside the checkout; installed Init/Stats also
worked in a fresh disposable repository. CI YAML/matrix and wheel/sdist contents
were checked locally. An independent Sol review found no concrete HIGH/BLOCKER.
Residual concurrent-replacement races, cross-filesystem/SQLite crash atomicity,
and host-level provider confinement remain explicit limits. WAL/recovery-mode
storage is refused before read-only SQLite opens; Stats aggregation is unchanged.
Public build/runtime dependencies were fetched only with approval into temporary
build/install paths; no provider request, authentication change or upload occurred.

## Remaining approval checkpoints

| ID | Work remaining | Approval / evidence |
|---|---|---|
| CI-1 | Execute the prepared GitHub Actions matrix on the selected remote repository. | Approve repository selection/push separately. Retain commit SHA and workflow run URL; do not add provider secrets or live tests. |
| LIVE-1 | Exercise authenticated Codex in a suitable confined host; separately establish a Claude live runner/execution path before claiming that capability. | Approve the exact command, provider/network exposure and host isolation first. Do not retry nested sandbox-exec or weaken safety here. |
| EVIDENCE-1 | Establish a real failure/correction/Lesson chain, then measure fair CONTROL/REFLEX outcomes. | Use the preregistration procedure below. Neutral/negative/invalid results remain evidence; never replace them to rescue a claim. |
| COMMIT-1 | Create a reviewed source commit from the proposed file manifest. | Explicit commit approval required. Reflex has an initialized local Git root but no tracked commit. No commit/tag has been made by closure work. |
| RELEASE-1 | Reassess all 14 Gates against the selected real release case; review package/privacy/source state. | Every Gate must PASS before release. Tag/push/package publication/artifact upload each needs explicit approval. |

## Reproducible external procedure — not execution authorization

1. **Approve an environment and exact Reflex commit.** Use a dedicated host/container
   with an enforced filesystem boundary for the provider CLI and descendants.
   Reflex source must be read-only and never the child cwd. Make a tracked source
   commit after reviewing the proposed manifest; record its full SHA. Until that
   approval, there is no exact Reflex release SHA and measured runs are blocked.
   Source hashes identify the local candidate, not a committed release.
2. **Restore one predeclared small CSV repository.** The retained
   [base bundle](../benchmarks/results/csv-fixture-neutral-20261008/base.bundle)
   contains exact base `08f8f4c949d280984cf1b17551ae36f2df2f8020`.
   Restore it into a new disposable repository outside Reflex, disable hooks,
   and verify the exact commit and parser/tests before any provider execution.
   The task is: “Correct parse_row to handle quoted commas, escaped quotes, and
   trim field whitespace; verify with unittest.” The required check is a pinned
   Python executable with `-B -m unittest -v`, run in each clone. Record the
   interpreter/version and argv. This is a **controlled real-provider benchmark**,
   not a historical replay of a previously imported session.
3. **Obtain real evidence without using the synthetic correction.** Record the
   base before an actual provider task. Preserve its real failure, actual
   correction, and matching successful check. If no supported real failure and
   correction occur, this case cannot close Gates 1–4: record that result rather
   than inserting author-generated evidence. Import only this repository's log
   via `scan`, then use `learn --session-id`. Do not feed historical patches into
   either measured arm.
4. **Freeze and preregister before measured runs.** Record repository root, exact
   Reflex/source and benchmark commits, task, required check, candidate ID/rule,
   audit decision and hash, provider, installed CLI version, explicitly selected
   model and observed identity, settings, run order, timeout and environment.
   Default plan: **3 CONTROL + 3 REFLEX**, common agent timeout **1800 seconds**,
   required check timeout **60 seconds**, CodexRunner with empty model_settings.
   Fix the actual model ID from supported CLI configuration; it is not known or
   invented in this document. For the unchanged test-failure behavioral template,
   the expected rule hash is
   `5aac97fdcf1d7df3b51e419d9191751c6bf6ce7b064e3bcbff05b19c52afe509`;
   verify the real newly generated Lesson hash rather than reusing the synthetic
   Lesson's provenance. Any different Lesson must pass audit and be frozen anew
   **before** measured execution. There is no executable preregistration until
   the exact commit, real evidence/candidate and provider/model fields are set.
5. **Preflight and explicitly approve execution.** Create a fresh-ID canonical
   ReplayCase JSON, then run `reflex replay --repository REPOSITORY --case CASE`.
   It must pass without writes or agent execution. Review the exact task/check
   and provider data exposure. Approve `--execute` separately. Authentication
   must use an approved normal mechanism without copying credentials into the
   repository/logs/artifacts. If the existing isolated runner cannot authenticate
   within confinement, classify INFRASTRUCTURE BLOCKED and stop; do not weaken
   isolation or treat it as real-agent evidence.
6. **Run the existing Engine and Evaluator.** CONTROL executes first; REFLEX runs
   only if CONTROL failures form a strict majority. Each run has a new clone,
   exact base, independent HOME/XDG/CODEX_HOME and paired seed. Task, provider,
   model/version, settings, timeout, check, tools and baseline environment match.
   The only intended instruction difference is exactly one frozen Lesson in
   REFLEX. No hidden answer, correction diff, CONTROL output, reused worktree or
   credential artifact is permitted. Snapshot the original repository including
   tracked/untracked content and HEAD/index before/after, excluding only owned
   SQLite administrative writes. Host confinement must supplement clone isolation.
7. **Retain and audit all outcomes.** Keep preregistration, real-session provenance,
   canonical events, failure/correction references, frozen Lesson/audit/hash,
   case and run IDs, raw local outputs with privacy review, deterministic check
   results, known token/tool/time/exit metrics, Replay report, linked Evaluation,
   original snapshots and source commit. Unknown remains null, never zero.
   Do not edit stored history. Classification: baseline-ineligible is
   BASELINE_NOT_REPRODUCIBLE; asymmetry/corruption is INVALID_COMPARISON;
   CLI/auth/provider restrictions are INFRASTRUCTURE BLOCKED; valid neutral or
   negative outcomes do not close Measured Improvement. Only accepted Phase 7
   A/B evidence can support the limited improvement claim.
8. **Verify Stats and all Gates.** `reflex stats` must agree with recorded counts,
   arm success, eligible medians and unavailable metrics and must be read-only.
   Re-evaluate identical saved evidence ten times. Audit all 14 Gate conditions
   on this real case, not just the previous fixture. Review any Lesson lifecycle
   transition separately; Evaluation does not automatically promote or Apply.
   Current candidate cannot be applied. An eligible replay-confirmed Lesson
   requires visible preview and explicit confirmation in its own repository.

No action above is approved merely by being documented. No current live request,
authentication change, commit, tag, push, publication or artifact transmission is
part of the local closure.

## Proposed commit scope (approval required)

Reflex now has its own initialized Git root. The index is empty and there is no
commit, tag, remote, push or publication. All files listed below are intended for
one reviewed local commit; the rest of the parent workspace is excluded.
The synthetic evidence directory deliberately includes its small audited SQLite
database and Git base bundle to retain the linked neutral trial. These are
synthetic audit fixtures, not private state; neither ships in wheel/sdist.

Excluded: `.git/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.reflex/`,
`dist/`, `build/`, `*.egg-info/`, all temporary validation/build/cache/smoke paths,
authentication files, personal sessions, and all unrelated parent-workspace files.
No excluded evidence or cache was deleted by closure. The worktree is not clean: it
contains the proposed untracked source files awaiting commit approval.

Suggested commit: `Complete Reflex v0.1 local release candidate validation`.
Suggested eventual tag: `v0.1.0`, only after every Gate passes and tag approval.

Exact proposed paths:

```text
.github/workflows/tests.yml
.gitignore
LICENSE
README.md
benchmarks/README.md
benchmarks/results/csv-fixture-neutral-20261008/artifact-sha256.json
benchmarks/results/csv-fixture-neutral-20261008/base.bundle
benchmarks/results/csv-fixture-neutral-20261008/calls.json
benchmarks/results/csv-fixture-neutral-20261008/case.json
benchmarks/results/csv-fixture-neutral-20261008/evaluation.json
benchmarks/results/csv-fixture-neutral-20261008/failure.json
benchmarks/results/csv-fixture-neutral-20261008/lesson.json
benchmarks/results/csv-fixture-neutral-20261008/preregistration.json
benchmarks/results/csv-fixture-neutral-20261008/reflex.db
benchmarks/results/csv-fixture-neutral-20261008/replay.json
benchmarks/results/csv-fixture-neutral-20261008/repository-state.json
benchmarks/results/csv-fixture-neutral-20261008/session.json
benchmarks/results/csv-fixture-neutral-20261008/stats-cli.txt
benchmarks/results/csv-fixture-neutral-20261008/stats.json
benchmarks/results/csv-fixture-neutral-20261008/summary.json
benchmarks/results/csv-fixture-neutral-20261008/synthetic-session.jsonl
benchmarks/results/csv-fixture-neutral-20261008/trial.json
docs/FOLLOW_UP.md
docs/RELEASE_GATE.md
pyproject.toml
src/reflex/__init__.py
src/reflex/adapters/__init__.py
src/reflex/adapters/base.py
src/reflex/adapters/claude.py
src/reflex/adapters/codex.py
src/reflex/analysis/__init__.py
src/reflex/analysis/corrections.py
src/reflex/analysis/failures.py
src/reflex/analysis/leakage.py
src/reflex/analysis/lessons.py
src/reflex/analysis/signals.py
src/reflex/application.py
src/reflex/cli_workflows.py
src/reflex/config/__init__.py
src/reflex/config/loader.py
src/reflex/config/models.py
src/reflex/git/__init__.py
src/reflex/git/clone.py
src/reflex/git/repository.py
src/reflex/main.py
src/reflex/models.py
src/reflex/output/__init__.py
src/reflex/output/stats.py
src/reflex/replay/__init__.py
src/reflex/replay/checks.py
src/reflex/replay/engine.py
src/reflex/replay/evaluator.py
src/reflex/replay/injection.py
src/reflex/replay/isolation.py
src/reflex/replay/models.py
src/reflex/replay/runner.py
src/reflex/replay/subprocess.py
src/reflex/storage/__init__.py
src/reflex/storage/database.py
src/reflex/storage/lifecycle.py
src/reflex/storage/repository.py
src/reflex/storage/schema.py
tests/fixtures/claude/session.jsonl
tests/fixtures/codex_rollout_format.jsonl
tests/test_apply.py
tests/test_apply_replacement_guards.py
tests/test_apply_review.py
tests/test_claude.py
tests/test_claude_exit_provenance.py
tests/test_claude_review.py
tests/test_cli.py
tests/test_clone.py
tests/test_codex.py
tests/test_entry_gate.py
tests/test_evaluation_storage.py
tests/test_evaluator.py
tests/test_failures.py
tests/test_lesson_storage.py
tests/test_lessons.py
tests/test_phase1.py
tests/test_phase10_cli.py
tests/test_phase11_controlled.py
tests/test_phase6.py
tests/test_release_cli.py
tests/test_replay_storage.py
tests/test_stats.py
tools/controlled_benchmark.py
uv.lock
```
