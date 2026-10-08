"""Deterministic statistics from verified, persisted replay evidence."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from statistics import median

from reflex.replay.evaluator import evaluate
from reflex.storage.repository import load_replay_report


def _rows(connection, query):
    cursor = connection.execute(query)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _eligible(run):
    result = run.result
    return (result.efficiency_usable and not result.timed_out
            and not result.agent_error and not result.cleanup_error
            and result.exit_code in (None, 0)
            and not {"TIMEOUT", "AGENT_ERROR", "CLEANUP_WARNING"}.intersection(
                [run.status, *run.statuses])
            and not any(check.required and (check.timed_out or check.error or check.cleanup_error)
                        for check in run.checks))


def _metric(run, name):
    value = getattr(run.result, name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if name in {"token_total", "tool_calls"} and type(value) is not int:
        return None
    if name == "token_total":
        parts = (run.result.token_input, run.result.token_output)
        if any(part is not None and (type(part) is not int or part < 0 or part > value)
               for part in parts):
            return None
        if all(part is not None for part in parts) and sum(parts) != value:
            return None
    return value


def collect_stats(connection: sqlite3.Connection) -> dict:
    """Use an injected connection (normally mode=ro); perform SELECTs only.

    The latest evaluation must attest the unchanged report. Efficiency medians
    describe available eligible observations, not a new improvement judgment.
    """
    counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("sessions", "failures")}
    lessons = {status: 0 for status in ("candidate", "replay_confirmed", "rejected", "applied")}
    for row in _rows(connection, "SELECT status,COUNT(*) AS count FROM lessons GROUP BY status"):
        if row["status"] in lessons:
            lessons[row["status"]] = row["count"]
    result = {"sessions_analyzed": counts["sessions"], "failures_detected": counts["failures"],
              "lessons": lessons, "replay_comparisons": 0, "valid_comparisons": 0,
              "invalid_comparisons": 0, "unevaluated_comparisons": 0,
              "improvement_confirmed": 0, "arms": {}}
    observations = {arm: {name: [] for name in ("token_total", "tool_calls", "elapsed_seconds")}
                    for arm in ("CONTROL", "REFLEX")}
    for arm in observations:
        result["arms"][arm] = {"success_count": 0, "run_count": 0}
    reports = _rows(connection, "SELECT id FROM replay_reports ORDER BY id")
    result["replay_comparisons"] = len(reports)
    for row in reports:
        cursor = connection.execute("SELECT * FROM evaluations WHERE report_id=? ORDER BY id DESC LIMIT 1",
                                    (row["id"],))
        stored = cursor.fetchone()
        if stored is None:
            result["unevaluated_comparisons"] += 1
            continue
        saved = dict(zip([column[0] for column in cursor.description], stored))
        try:
            report = load_replay_report(connection, row["id"])
            evaluation = evaluate(report)
            encoded = json.dumps(report.model_dump(mode="json"), sort_keys=True, allow_nan=False)
            fields = {"replay_case_id": report.case_id, "status": evaluation.status,
                      "condition": evaluation.condition, "reason": evaluation.reason,
                      "control_success": evaluation.control_success_count,
                      "reflex_success": evaluation.reflex_success_count,
                      "control_median_tokens": evaluation.control_median_tokens,
                      "reflex_median_tokens": evaluation.reflex_median_tokens,
                      "token_reduction_ratio": evaluation.token_reduction_ratio,
                      "required_check_regression": int(evaluation.required_check_regression),
                      "manifest_hash": report.metadata.get("manifest_hash"),
                      "report_hash": hashlib.sha256(encoded.encode()).hexdigest()}
            if (any(saved.get(key) != value for key, value in fields.items())
                    or json.loads(saved["evaluation_json"]) != evaluation.model_dump(mode="json")
                    or json.loads(saved["evidence_json"]) != evaluation.evidence):
                raise ValueError("evaluation attestation differs")
            complete = (evaluation.status in {"replay_confirmed", "rejected"}
                        or (evaluation.status == "INSUFFICIENT_EVIDENCE"
                            and evaluation.reason.startswith("condition B unavailable:")))
            if not complete:
                raise ValueError("comparison integrity unavailable")
        except (ValueError, TypeError, KeyError, OverflowError):
            result["invalid_comparisons"] += 1
            continue
        result["valid_comparisons"] += 1
        result["improvement_confirmed"] += evaluation.status == "replay_confirmed"
        for arm, count in (("CONTROL", evaluation.control_success_count),
                           ("REFLEX", evaluation.reflex_success_count)):
            runs = [run for run in report.runs if run.condition == arm]
            result["arms"][arm]["success_count"] += count
            result["arms"][arm]["run_count"] += len(runs)
            if report.metadata.get("efficiency_usable") is not True or report.warnings:
                continue
            for run in runs:
                if not _eligible(run):
                    continue
                for name in observations[arm]:
                    value = _metric(run, name)
                    if value is not None:
                        observations[arm][name].append(value)
    for arm, metrics in observations.items():
        values = result["arms"][arm]
        values["success_rate"] = (values["success_count"] / values["run_count"]
                                  if values["run_count"] else None)
        for name, available in metrics.items():
            values[f"median_{name}"] = median(available) if available else None
            values[f"{name}_available_count"] = len(available)
    return result
