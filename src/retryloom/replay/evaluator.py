"""Pure, conservative evaluation of recorded paired replay evidence."""
from __future__ import annotations

import hashlib
import json

from .models import Evaluation, ReplayCheck, ReplayReport, ReplayRun


def evaluate(report: ReplayReport) -> Evaluation:
    """Evaluate only frozen report evidence; never run an agent or change a lesson."""
    arms = {name: sorted((run for run in report.runs if run.condition == name),
                         key=lambda run: run.run_number) for name in ("CONTROL", "REFLEX")}
    counts = {name: sum(_success(run) for run in runs) for name, runs in arms.items()}
    evidence = {
        "report_id": report.id, "case_id": report.case_id,
        "manifest_hash": report.metadata.get("manifest_hash"),
        "resolved_base_commit": report.metadata.get("resolved_base_commit"),
        "run_ids": {name: [run.id for run in runs] for name, runs in arms.items()},
        "optional_check_regressions": [], "required_check_regressions": [],
        "efficiency_available": False, "limitations": [],
    }
    values = dict(control_success_count=counts["CONTROL"],
                  reflex_success_count=counts["REFLEX"], evidence=evidence)

    def finish(status: str, reason: str, condition: str | None = None) -> Evaluation:
        return Evaluation(status=status, reason=reason, condition=condition, **values)

    if report.status == "BASELINE_NOT_REPRODUCIBLE":
        return finish("BASELINE_NOT_REPRODUCIBLE", report.reason or "baseline was not reproducible")
    if report.status.startswith("INVALID"):
        return finish("INVALID_COMPARISON", report.reason or "replay integrity failed")
    if report.status != "COMPLETED":
        return finish("INSUFFICIENT_EVIDENCE", "report is not a completed comparison")
    if any(any(status.startswith("INVALID") for status in [run.status, *run.statuses])
           for run in report.runs):
        return finish("INVALID_COMPARISON", "run integrity or fairness failed")

    manifest = report.metadata.get("manifest")
    if not isinstance(manifest, dict):
        return finish("INSUFFICIENT_EVIDENCE", "frozen manifest is missing")
    if not isinstance(manifest.get("task"), str) or not manifest["task"].strip():
        return finish("INVALID_COMPARISON", "manifest task is missing or empty")
    if type(manifest.get("timeout_seconds")) is not int or manifest["timeout_seconds"] <= 0:
        return finish("INVALID_COMPARISON", "manifest timeout must be a positive integer")
    if not isinstance(manifest.get("model_settings", {}), dict):
        return finish("INVALID_COMPARISON", "manifest model settings are malformed")
    planned = manifest.get("runs")
    if type(planned) is not int or planned < 2:
        return finish("INSUFFICIENT_EVIDENCE", "manifest requires at least two runs per arm")
    if any(len(runs) != planned for runs in arms.values()):
        return finish("INVALID_COMPARISON", "complete equal planned run counts are required")
    if len({run.id for run in report.runs}) != len(report.runs):
        return finish("INVALID_COMPARISON", "run identities are duplicated")
    if any([run.run_number for run in runs] != list(range(1, planned + 1))
           for runs in arms.values()):
        return finish("INVALID_COMPARISON", "run numbers are not complete numbered pairs")
    manifest_hash = report.metadata.get("manifest_hash")
    base = report.metadata.get("resolved_base_commit")
    lesson_hash = manifest.get("lesson_hash")
    if not all(isinstance(value, str) and value for value in (manifest_hash, base, lesson_hash)):
        return finish("INSUFFICIENT_EVIDENCE", "manifest hash, base commit or lesson hash is missing")
    if len(lesson_hash) != 64 or any(c not in "0123456789abcdef" for c in lesson_hash):
        return finish("INVALID_COMPARISON", "lesson hash is not a SHA256 digest")
    try:
        computed_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True,
                                                allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        return finish("INVALID_COMPARISON", "manifest is not deterministic JSON")
    if computed_hash != manifest_hash or manifest.get("base_commit") != base:
        return finish("INVALID_COMPARISON", "frozen manifest hash or base commit differs")
    if len(base) not in (40, 64) or any(c not in "0123456789abcdef" for c in base):
        return finish("INVALID_COMPARISON", "base commit is not a resolved full commit ID")
    try:
        definitions = [ReplayCheck.model_validate(check) for check in manifest["checks"]]
    except (KeyError, TypeError, ValueError):
        return finish("INSUFFICIENT_EVIDENCE", "frozen check definitions are missing or malformed")
    if not definitions or not any(check.required for check in definitions):
        return finish("INSUFFICIENT_EVIDENCE", "at least one required check definition is needed")
    if any(not check.command or any(not part or "\0" in part for part in check.command)
           for check in definitions):
        return finish("INVALID_COMPARISON", "frozen check commands are invalid")

    runner = report.metadata.get("runner", {})
    if not isinstance(runner, dict):
        return finish("INVALID_COMPARISON", "runner metadata is malformed")
    observed = report.metadata.get("observed_agent_metadata", {})
    if not isinstance(observed, dict):
        return finish("INVALID_COMPARISON", "observed agent metadata is malformed")
    for name in ("agent", "agent_version", "model"):
        recorded = [getattr(run.result, name) for run in report.runs]
        available = [manifest.get(name), runner.get(name), observed.get(name), *recorded]
        if any(value is not None and (not isinstance(value, str) or not value.strip())
               for value in available):
            return finish("INVALID_COMPARISON", f"{name} metadata must be a nonempty string or null")
        known = {value for value in available if value is not None}
        if len(known) > 1:
            return finish("INVALID_COMPARISON", f"{name} metadata differs across comparison")
        if name == "agent" and not known:
            return finish("INSUFFICIENT_EVIDENCE", f"{name} parity is not attested")
        if name != "agent" and (not known or report.metadata.get(f"{name}_parity_verified") is False):
            evidence["limitations"].append(f"{name} parity is unknown; available metadata has no mismatch")
    if manifest.get("model_settings", {}) != runner.get("model_settings", {}):
        return finish("INVALID_COMPARISON", "runner settings differ from frozen manifest")

    environment_keys = None
    known_statuses = {"COMPLETED", "TIMEOUT", "AGENT_ERROR", "REQUIRED_CHECK_FAILED", "CLEANUP_WARNING"}
    for run in report.runs:
        if run.status not in known_statuses or any(s not in known_statuses for s in run.statuses):
            return finish("INSUFFICIENT_EVIDENCE", "run completion status is unknown")
        if ("timeout_seconds" in run.metadata and
                run.metadata["timeout_seconds"] != manifest.get("timeout_seconds")):
            return finish("INVALID_COMPARISON", "run timeout differs from frozen manifest")
        for key, expected in (("manifest_hash", manifest_hash), ("base_commit", base)):
            if key not in run.metadata:
                return finish("INSUFFICIENT_EVIDENCE", f"run {key} evidence is missing")
            if run.metadata[key] != expected:
                return finish("INVALID_COMPARISON", f"run {key} differs from frozen report")
        if run.result.lesson_hash is None:
            return finish("INSUFFICIENT_EVIDENCE", "run lesson hash is missing")
        if run.result.lesson_hash != lesson_hash or run.seed != run.run_number - 1:
            return finish("INVALID_COMPARISON", "run lesson hash or paired seed differs")
        keys = run.metadata.get("tool_environment_keys")
        if not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys):
            return finish("INSUFFICIENT_EVIDENCE", "tool environment evidence is missing")
        if len(set(keys)) != len(keys):
            return finish("INVALID_COMPARISON", "tool environment keys are duplicated")
        if environment_keys is None:
            environment_keys = set(keys)
        elif set(keys) != environment_keys:
            return finish("INVALID_COMPARISON", "tool environment keys differ across comparison")
        if len(run.checks) != len(definitions):
            return finish("INSUFFICIENT_EVIDENCE", "run check evidence is incomplete")
        for check, definition in zip(run.checks, definitions):
            if (check.name, check.command, check.required) != (definition.name, definition.command, definition.required):
                return finish("INVALID_COMPARISON", "recorded check differs from frozen definition")
            if check.success and (check.timed_out or check.error or
                                  check.exit_code not in (None, 0)):
                return finish("INVALID_COMPARISON", "check success contradicts recorded failure")
        if run.result.success and not _success(run):
            return finish("INVALID_COMPARISON", "run success contradicts recorded failure")

    if planned - counts["CONTROL"] <= planned // 2:
        return finish("BASELINE_NOT_REPRODUCIBLE", "control failures did not form a strict majority")

    for control, enhanced in zip(arms["CONTROL"], arms["REFLEX"]):
        for number, (before, after) in enumerate(zip(control.checks, enhanced.checks), 1):
            if before.success and not after.success:
                key = "required_check_regressions" if before.required else "optional_check_regressions"
                evidence[key].append({"run_number": control.run_number,
                                      "check_number": number, "name": before.name})
    values["required_check_regression"] = bool(evidence["required_check_regressions"])

    efficiency_reason = _efficiency_unavailable(report)
    evidence["efficiency_unavailable_reason"] = efficiency_reason
    control_twice_median = enhanced_twice_median = None
    if efficiency_reason is None:
        control_twice_median = _twice_median([run.result.token_total for run in arms["CONTROL"]])
        enhanced_twice_median = _twice_median([run.result.token_total for run in arms["REFLEX"]])
        values.update(control_median_tokens=control_twice_median / 2,
                      reflex_median_tokens=enhanced_twice_median / 2,
                      token_reduction_ratio=((control_twice_median - enhanced_twice_median) / control_twice_median
                                             if control_twice_median > 0 else None))
        evidence["efficiency_available"] = True
    if values["required_check_regression"]:
        return finish("rejected", "paired required check regression blocks confirmation")
    if counts["REFLEX"] >= counts["CONTROL"] + 1:
        return finish("replay_confirmed", "REFLEX gained at least one success without required check regression", "A")
    if counts["REFLEX"] == counts["CONTROL"] and counts["CONTROL"] >= 1:
        if efficiency_reason is not None:
            return finish("INSUFFICIENT_EVIDENCE", "condition B unavailable: " + efficiency_reason)
        if control_twice_median > 0 and enhanced_twice_median * 100 <= control_twice_median * 85:
            return finish("replay_confirmed", "equal successes with at least 15 percent median token reduction", "B")
    return finish("rejected", "neither success improvement nor eligible token improvement was demonstrated")


def _twice_median(totals: list[int]) -> int:
    # Retain the exact half-integer median for threshold decisions at any token scale.
    ordered = sorted(totals)
    middle = len(ordered) // 2
    return 2 * ordered[middle] if len(ordered) % 2 else ordered[middle - 1] + ordered[middle]


def _success(run: ReplayRun) -> bool:
    result = run.result
    failures = {"TIMEOUT", "AGENT_ERROR", "REQUIRED_CHECK_FAILED"}
    return bool(result.success and not result.timed_out and not result.agent_error
                and result.exit_code in (None, 0)
                and not failures.intersection([run.status, *run.statuses])
                and all(check.success for check in run.checks if check.required))


def _efficiency_unavailable(report: ReplayReport) -> str | None:
    if report.metadata.get("efficiency_usable") is not True or report.warnings:
        return "report efficiency audit is unavailable"
    for run in report.runs:
        result = run.result
        if any(check.required and (check.timed_out or check.error or check.cleanup_error)
               for check in run.checks):
            return "a required check has incomplete or unusable efficiency evidence"
        if (not result.efficiency_usable or result.timed_out or result.agent_error
                or result.cleanup_error or result.exit_code not in (None, 0)
                or {"TIMEOUT", "AGENT_ERROR", "CLEANUP_WARNING"}.intersection([run.status, *run.statuses])):
            return "a planned run did not complete with usable efficiency evidence"
        total = result.token_total
        if type(total) is not int or total < 0:
            return "a planned run has missing or negative token totals"
        parts = (result.token_input, result.token_output)
        if any(value is not None and (type(value) is not int or value < 0 or value > total)
               for value in parts):
            return "token components contradict the recorded total"
        if all(value is not None for value in parts) and sum(parts) != total:
            return "token components contradict the recorded total"
    return None
