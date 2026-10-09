# Retryloom

Derive lessons from agent failures. Test them through replay.

**Measured synthetic benchmark: 0/3 → 0/3 (CONTROL → REFLEX).
VALID NEUTRAL RESULT. Improvement in a real coding agent has not been demonstrated.**

| Retained evidence | Result |
|---|---|
| CONTROL / REFLEX successes | 0/3 / 0/3 |
| Baseline / comparison integrity | Passed for the controlled fixture |
| Evaluation / current Lesson status | `rejected` / `candidate` |
| Token / tool-call metrics | Unknown (`N/A`), not zero |
| Authenticated live Codex / Claude | `INFRASTRUCTURE BLOCKED` |

See the [benchmark evidence](benchmarks/README.md). This development snapshot is
**not release-ready**: the [fixed 14-item Release Gate](docs/RELEASE_GATE.md)
has external validation requirements. The tagline describes the workflow, not a proven improvement guarantee.

Retryloom is an experimental local framework for deriving, replaying, evaluating,
and applying Lessons from agent failure evidence. The CLI connects the existing
components; real-provider end-to-end behavior and improvement remain unvalidated.

## Evidence Categories

| Category | What it establishes | Current status |
|---|---|---|
| Synthetic fixture | Offline behavior of the pipeline | Phase 11 exercised; no real-agent learning claim |
| Real saved session parsing | Read-only normalization of previously recorded agent logs | Exercised in earlier Codex/Claude adapter work; does not prove authenticated execution or improvement |
| Authenticated live execution | A fresh provider-backed agent run in a verified execution environment | Infrastructure blocked for both providers; no successful authenticated smoke is claimed |

The Phase 11 transcript is synthetic even though its local subprocess exit codes
and check outputs were observed. Its correction was author-supplied, not made by
a live coding agent. These evidence categories are not interchangeable.

## Current CLI

Python 3.12+, Git, and macOS or Linux are required. For example, use
`uv run retryloom --help` in the project environment. Help lists all six names;
appearing in help does not establish implementation.

| Command | Current behavior | Mutation |
|---|---|---|
| `retryloom init` | Initializes local storage/config in the selected Git root; preserves existing data; idempotent | Required Retryloom-owned files/directories only |
| `retryloom scan` | Imports selected-repository Codex/Claude saved sessions; skips existing IDs | Writes local `.retryloom/retryloom.db` |
| `retryloom learn` | Analyzes an imported `--session-id`, persists evidenced candidates with audit/frozen hash | Local failure/Lesson records; no promotion or Replay |
| `retryloom replay` | Validates `--case` JSON; `--execute` explicitly starts CodexRunner, existing Engine and Evaluator | Preflight is read-only; execution persists Replay/Evaluation, not Lesson status |
| `retryloom apply` | Lists eligible Lessons, previews a selected change, requires confirmation | Confirmed target and application persistence only |
| `retryloom stats` | Reads structured persisted counts and eligible comparison metrics | Read-only |

Implemented command examples; substitute your own paths and eligible Lesson ID:

```sh
uv run retryloom init --repository /path/to/project
uv run retryloom scan --agent codex --repository /path/to/project
uv run retryloom scan --agent claude --repository /path/to/project --source-root /path/to/transcripts
uv run retryloom learn --repository /path/to/project --session-id ID
uv run retryloom replay --repository /path/to/project --case /path/to/case.json
uv run retryloom stats --repository /path/to/project
uv run retryloom apply --repository /path/to/project --lesson-id ID --target AGENTS.md --preview
```

`replay --execute` can contact the provider and execute supplied checks; it is not
a read-only smoke. It does not solve the current host confinement/authentication
gap. Do not invoke it here to force a live PASS. Case JSON follows
[ReplayCase](src/retryloom/replay/models.py); repository, base_commit, task, checks,
lesson_id and lesson_hash are required. Runs default to 3 (minimum 2), agent to
Codex, and timeout to 1800 seconds. Use a fresh case ID for changed inputs.

Scan persists Sessions; it does not automatically detect failures, generate
Lessons, or run Replay. Existing IDs, including changed transcripts, are skipped;
automatic refresh is not implemented. Source history is read only. Both adapters
use the same canonical Session/Event model and database. A missing historical
base commit stays unavailable; current HEAD is not proof of a historical base.

Apply supports `AGENTS.md`, `CLAUDE.md`, and `--target skills` for `.retryloom/skills/`.
The default instruction file follows the configured agent. Only a recorded
`replay_confirmed` Lesson with matching frozen content and linked Evaluation is
eligible. Candidate/rejected Lessons cannot be applied. Preview is non-mutating;
declining confirmation leaves files and status unchanged. Existing UTF-8 content
is preserved and duplicate Apply is a no-op. Target content/identity are checked
again immediately before replacement and guarded rollback.

## How It Works

The commands connect the existing Python components. Scan, Learn, Replay, Stats,
and confirmed Apply are explicit separate operations:

1. Normalize selected session records, preserving source provenance and available metrics.
2. Detect failure signals and select a correction followed by matching success evidence.
3. Generate a scoped `candidate` behavioral rule. Failure confidence is not Lesson confidence.
4. Mechanically audit, canonicalize, and freeze the Lesson's SHA-256 hash before Replay.
5. Execute independent CONTROL runs and the baseline gate, then REFLEX runs receiving exactly the frozen Lesson. Execute required deterministic checks.
6. Persist Replay evidence and evaluate it with the deterministic Phase 7 evaluator. Evaluation does not rerun agents or modify Lesson state.
7. Read persisted evidence through Stats. Apply remains a separate confirmed operation for eligible Lessons.

### Lesson Safety and Status

Lessons are behavioral guidance, not stored answer patches. Mechanical auditing
rejects correction/final-answer copying, source coordinates, fenced code,
secrets, and unsupported generalization. Correction-only identifiers or word-trigram
overlap **strictly greater than 30%** are suspect and require explicit audit
resolution before Replay. Rewritten content must be audited again. A one-off
preference does not automatically become a universal instruction.

Historical evidence cannot silently rewrite frozen content. A changed hash/context
invalidates comparison. Public Lesson statuses are `candidate`, `replay_confirmed`,
`rejected`, and `applied`.

**`replay_confirmed` means improvement evidence under the tested Replay conditions
only. It does not establish universal correctness or cross-task generalization.**
Generation alone never establishes confirmation.

In the retained benchmark, **Evaluation is `rejected`, while the Lesson is still
`candidate`**. They are separate records: Evaluation persistence does not
implicitly change Lesson status. No promotion or Apply occurred.

## Replay Fairness

Each run uses an independent Git clone at the exact resolved base commit, with
separate working tree, index, and local Git state. No Git worktrees are shared.
History is restricted to the permitted base, and source remotes are removed.
Task, runner/provider, available model/version metadata, settings, timeouts,
checks, and baseline environment must match. Unknown metadata is not invented.

Every run receives temporary HOME/XDG/CODEX_HOME/state paths. The intentional
behavioral difference is one frozen Lesson for REFLEX and none for CONTROL.
Historical corrections/final patches, CONTROL output, and hidden answers are not
included in REFLEX instructions. Original repository state is checked before/after;
owned `.retryloom` database files are explicit administrative exclusions, not permission
for arbitrary source changes.

### Replay Engine Boundaries

Minimum runs are **2 per condition**; default **3**. Fewer than 2 are rejected.
CONTROL failures must form a strict majority. With three runs, 0 or 1 CONTROL
success is eligible; 2 or more yields `BASELINE_NOT_REPRODUCIBLE`.

The engine runs CONTROL first and REFLEX only after baseline eligibility. It does
not interleave/randomize trials; this order is recorded. Phase 6 produces
ReplayRun/ReplayReport data, not Condition A/B decisions or Lesson promotion.
Phase 7 evaluates recorded evidence.

Live Codex authentication inside isolated Replay remains unverified. Claude
saved-session import does not imply a working authenticated Claude Replay runner.

## Metric Gaming Protections

Required checks determine task success; agent prose or a normal process exit
alone does not. Invalid or baseline-ineligible comparisons cannot support improvement.
Given an eligible baseline and no required-check regression:

- **Condition A:** REFLEX gains at least one successful run.
- **Condition B:** Success counts are equal, at least one CONTROL succeeds, and REFLEX median tokens are at most 85% of CONTROL's positive median, using complete usable token evidence for all planned runs.

0/3 versus 0/3 cannot confirm through token savings. Missing metrics do not
automatically fail a run, but prevent decisions depending on those metrics.
Timeouts, crashes, unusable/truncated accounting, and cleanup problems cannot
masquerade as efficient completed runs. Timeout mismatch invalidates comparison.

Stats uses persisted report/Evaluation attestations, excludes invalid comparisons,
and keeps CONTROL/REFLEX separate. Unknown values are `N/A`; measured zero remains
zero. Medians use eligible observations and expose availability counts. No token
estimates from text length are presented as measurements.

Claude usage is deduplicated by API message ID; missing usage on a distinct
assistant message leaves aggregate usage unknown. Cache categories remain in
source metadata rather than fabricated totals. Claude exit codes require structured
status uniquely associated with the corresponding result; unknown stays unknown.

## Privacy

Retryloom has no account, telemetry, cloud sync, or automatic session/source upload.
Import, mechanical auditing, deterministic evaluation, and Stats run locally.
Sessions, commands, tool output, stdout/stderr, and local databases can contain
secrets; treat them as private and review them before sharing.

External agent execution is different from local parsing: an invoked CLI may
contact its provider and transmit task/context. No successful authenticated live
execution is claimed here. The retained controlled fixture uses an allowlisted
environment, synthetic records, and no provider/network calls. Credentials,
raw private home history, and unrelated private logs are not benchmark artifacts.

## Known Limitations

- Timeout/process-group cleanup is **best-effort**. Orphans may remain; no zero-orphan guarantee.
- Independent clones separate files/Git state; they are **not a complete hostile-code sandbox**. Pollution warnings are advisory, not isolation proof.
- Persistent state isolation depends on CLI redirect support. HOME changes do not prove keychain or every external state store is isolated.
- Live Codex/Claude are infrastructure blocked here. Nested macOS sandbox execution is unavailable; safety was not weakened to obtain a successful smoke. Provider/auth settings were not changed to bypass it.
- Historical commits are often absent; consulting today's HEAD cannot establish historical Replay provenance.
- Read-only workflows reject WAL/non-default SQLite journaling and active recovery state rather than creating sidecars or silently reading stale evidence. Default Retryloom storage uses DELETE journaling.
- Filesystem writes and SQLite commits are not one crash-atomic transaction. Guarded restoration handles ordinary failures. A crash may leave Lesson text without an application record; recovery requires a fresh preview/confirmation. Residual replacement races and hostile directory replacement are not completely prevented.
- The minimal Codex runner retains raw output and leaves unparsed metrics unknown. A canonical authenticated Claude live runner is not complete/verified.
- Lesson generalization, held-out evaluation, and multi-session automatic promotion are not v0.1 features.
- Replay CLI currently supports CodexRunner only; Claude saved-session import is supported, but Claude live Replay has no supported CLI runner.
- Replay requires an absolute matching repository root, an exact full commit OID, a managed candidate, required automatic checks, and `--execute` to launch the provider. Default preflight does not start agents. Promisor/partial repositories are rejected to avoid implicit Git fetch.
- `learn` requires an imported session with supported failure/correction/success evidence. Missing evidence is an error, not a guessed Lesson. Replay persists Evaluation without automatic Lesson promotion.

## Benchmark Methodology

The retained CSV case is an **offline synthetic pipeline benchmark**. Required
unittest checks cover quoted commas, escaped quotes, and whitespace. Its transcript
is author-constructed from observed local failure/correction/success outputs.
The fixture makes the same inadequate whitespace-only edit in both conditions;
it accepts but does not interpret Lesson guidance.

Before measurement, the exact task, base commit, checks, three runs per arm,
10-second timeouts, runner configuration, frozen Lesson/hash, implementation source
hashes, and ordering were recorded. Setup/debug runs were not counted. Neither
condition received the historical solution. Actual checks produced 0/3 versus 0/3.
The report is complete and Evaluation rejected. Ten evaluations of the same report
agree; Stats matches the database. Runtime is measured, but no efficiency gain is claimed.

See [preregistration](benchmarks/results/csv-fixture-neutral-20261008/preregistration.json),
[Replay](benchmarks/results/csv-fixture-neutral-20261008/replay.json),
[Evaluation](benchmarks/results/csv-fixture-neutral-20261008/evaluation.json),
[Stats](benchmarks/results/csv-fixture-neutral-20261008/stats.json), and
[artifact hashes](benchmarks/results/csv-fixture-neutral-20261008/artifact-sha256.json).
The [benchmark guide](benchmarks/README.md) describes reproduction in a fresh
repository outside Retryloom; live authentication is not required for that fixture.

## Development and Release Status

```sh
uv run retryloom --help
uv run pytest
```

Phase 11 baseline: **10 focused tests, 397 full-suite tests passed**.
Release closure validation: **64 focused tests passed; 440 full-suite tests passed
from the wheel installed into a fresh temporary virtual environment**.
These regression results do not demonstrate real-agent improvement.
The original Reflex release candidate was committed as `69d939757646bf12645e05d349539acd2eedca3c`
and published at [larhalt0515/reflex](https://github.com/larhalt0515/reflex).
The local Retryloom rename awaits commit, public repository rename and push approval.
The retained Phase 11 trial identifies its original implementation through source hashes.

All [14 Release Gates](docs/RELEASE_GATE.md) must pass. Required external validation
is not an assumed PASS. CLI wiring is implemented; the [CI workflow](.github/workflows/tests.yml)
is prepared for deterministic tests on Linux Python 3.12/3.13/3.14 and macOS 3.14.
[GitHub Actions passed all four jobs](https://github.com/larhalt0515/reflex/actions/runs/37793077910) for the original commit above.
That result does not validate this unpushed rename. Safe live integration, real
improvement evidence, and release preparation remain [separate approval checkpoints](docs/FOLLOW_UP.md).
This documentation does not authorize execution, promotion, production Apply,
push, release, publication, or external artifact transmission.

## Pre-v0.1 identity migration

Local rename validation: **67 focused tests and 443 full-suite tests passed**.
The full suite used the renamed wheel installed in a fresh Python 3.14 environment.
All seven help pages, Init/idempotency, empty Stats and expected user errors passed
outside the checkout. These are offline checks, not authenticated agent evidence.

Retryloom replaces the original Reflex distribution, import namespace and CLI.
Use a fresh environment: `import retryloom` and `retryloom --help`. New local
state is `.retryloom/retryloom.db`; old `.reflex/` state and previously applied
instructions are not automatically moved, rewritten or deleted. No old CLI or
import alias is supplied. Shared task coordination and model escalation are
development operating policies, not implemented Retryloom commands or automatic
routing features.

The immutable historical benchmark still uses `REFLEX` for the Lesson-injected
arm. This condition label and serialized `reflex_*` fields/SQL columns remain
record-format identifiers so existing Replay/Evaluation evidence stays readable.
They are not the current project brand. New public descriptions call this the
Lesson-assisted workflow. See [historical evidence](benchmarks/README.md).

The approved GitHub target is `larhalt0515/retryloom`; that public rename has not
been performed. The current public repository and origin remain
[larhalt0515/reflex](https://github.com/larhalt0515/reflex).
