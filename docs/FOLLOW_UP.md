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
The original candidate is public at [larhalt0515/reflex](https://github.com/larhalt0515/reflex).
[Remote CI passed all four jobs](https://github.com/larhalt0515/reflex/actions/runs/37793077910) on `69d939757646bf12645e05d349539acd2eedca3c`.
The Retryloom rename is local only; its CI has not run remotely. No tag, GitHub
Release or PyPI publication exists. Wheel/sdist installation is tested locally; private state and retained
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

The local Retryloom rename passed 67 focused tests and 443 full-suite tests with
the renamed wheel in a fresh environment. The sdist/wheel build, new import/CLI,
temporary repository Init/Stats/error smoke and retained artifact hashes passed.
This does not change the nine controlled/local and five external Release Gates.

| ID | Work remaining | Approval / evidence |
|---|---|---|
| CI-1 | Verify CI again after the approved rename commit/push. Original candidate CI already passed all four jobs. | Rename commit, GitHub rename, origin reconciliation and push have separate approval gates; no provider secrets or live tests. |
| LIVE-1 | Exercise authenticated Codex in a suitable confined host; separately establish a Claude live runner/execution path before claiming that capability. | Approve the exact command, provider/network exposure and host isolation first. Do not retry nested sandbox-exec or weaken safety here. |
| EVIDENCE-1 | Establish a real failure/correction/Lesson chain, then measure fair CONTROL/REFLEX outcomes. | Use the preregistration procedure below. Neutral/negative/invalid results remain evidence; never replace them to rescue a claim. |
| COMMIT-1 | Create a reviewed local rename commit. | Gate B approval required. The original candidate is already committed; no rename commit or tag has been created. |
| RELEASE-1 | Reassess all 14 Gates against the selected real release case; review package/privacy/source state. | Every Gate must PASS before release. Tag/push/package publication/artifact upload each needs explicit approval. |

## Reproducible external procedure — not execution authorization

1. **Approve an environment and exact Retryloom commit.** Use a dedicated host/container
   with an enforced filesystem boundary for the provider CLI and descendants.
   Retryloom source must be read-only and never the child cwd. The original candidate
   commit is `69d939757646bf12645e05d349539acd2eedca3c`; it does not include this rename.
   After rename commit approval, record that exact new SHA for real-provider
   validation. No provider execution is authorized by this document.
2. **Restore one predeclared small CSV repository.** The retained
   [base bundle](../benchmarks/results/csv-fixture-neutral-20261008/base.bundle)
   contains exact base `08f8f4c949d280984cf1b17551ae36f2df2f8020`.
   Restore it into a new disposable repository outside Retryloom, disable hooks,
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
   Retryloom/source and benchmark commits, task, required check, candidate ID/rule,
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
   ReplayCase JSON, then run `retryloom replay --repository REPOSITORY --case CASE`.
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
8. **Verify Stats and all Gates.** `retryloom stats` must agree with recorded counts,
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

The original 88-file candidate is already committed and public. This local rename
changes package/source paths, CLI/runtime identity, tests, CI and documentation.
The retained synthetic database, bundle and other trial artifacts remain unchanged.
No rename commit, public repository rename, origin change or push has occurred.

Excluded: `.git/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.retryloom/`,
legacy `.reflex/`, `dist/`, `build/`, `*.egg-info/`, temporary build/cache/smoke
paths, authentication files, personal sessions and unrelated workspace files.
No runtime state is automatically migrated or deleted.

Suggested rename commit: `Rename Reflex to Retryloom before v0.1 release`.
An eventual `v0.1.0` tag requires all Gates to pass and separate approval.

Current source layout (historical evidence paths intentionally retain their names):

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
src/retryloom/__init__.py
src/retryloom/adapters/__init__.py
src/retryloom/adapters/base.py
src/retryloom/adapters/claude.py
src/retryloom/adapters/codex.py
src/retryloom/analysis/__init__.py
src/retryloom/analysis/corrections.py
src/retryloom/analysis/failures.py
src/retryloom/analysis/leakage.py
src/retryloom/analysis/lessons.py
src/retryloom/analysis/signals.py
src/retryloom/application.py
src/retryloom/cli_workflows.py
src/retryloom/config/__init__.py
src/retryloom/config/loader.py
src/retryloom/config/models.py
src/retryloom/git/__init__.py
src/retryloom/git/clone.py
src/retryloom/git/repository.py
src/retryloom/main.py
src/retryloom/models.py
src/retryloom/output/__init__.py
src/retryloom/output/stats.py
src/retryloom/replay/__init__.py
src/retryloom/replay/checks.py
src/retryloom/replay/engine.py
src/retryloom/replay/evaluator.py
src/retryloom/replay/injection.py
src/retryloom/replay/isolation.py
src/retryloom/replay/models.py
src/retryloom/replay/runner.py
src/retryloom/replay/subprocess.py
src/retryloom/storage/__init__.py
src/retryloom/storage/database.py
src/retryloom/storage/lifecycle.py
src/retryloom/storage/repository.py
src/retryloom/storage/schema.py
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
tests/test_identity.py
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
