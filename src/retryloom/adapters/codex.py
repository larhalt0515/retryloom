"""Normalize Codex rollout JSON/JSONL without discarding source records."""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from retryloom.models import Session, SessionEvent

_EXIT_CODE = re.compile(
    r"(?:process\s+)?(?:exited?\s+with\s+code|exit(?:ed)?\s+code|return\s+code)\s*[:=]?\s*(-?\d+)",
    re.IGNORECASE,
)


def _output_text(output: object) -> str | None:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(
            item["text"] for item in output
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return None


class CodexAdapter:
    def __init__(self, sessions_root: Path | None = None):
        self.sessions_root = sessions_root or Path.home() / ".codex" / "sessions"

    def discover(self) -> list[Path]:
        return sorted(set(self.sessions_root.rglob("*.jsonl")) | set(self.sessions_root.rglob("*.json")))

    def parse(self, path: Path) -> Session:
        raw = path.read_bytes()
        if path.suffix == ".jsonl":
            records = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        else:
            data = json.loads(raw)
            records = data if isinstance(data, list) else [data]
        if not records or any(not isinstance(record, dict) for record in records):
            raise ValueError("Codex session must contain object records")
        meta = next((r.get("payload", {}) for r in records if r.get("type") == "session_meta"), {})
        if not meta.get("cwd") or not meta.get("timestamp"):
            raise ValueError("Codex session requires session_meta cwd and timestamp")
        events = []
        task = result = model = None
        token_input = token_output = None
        calls = 0
        for record in records:
            payload = record.get("payload") or {}
            kind = payload.get("type", record.get("type", "unknown"))
            timestamp = record.get("timestamp")
            content = payload.get("content")
            text = payload.get("message")
            if isinstance(content, list):
                text = "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
            elif isinstance(content, str):
                text = content
            if kind == "custom_tool_call_output":
                text = _output_text(payload.get("output")) or text
            if kind == "user_message" or (kind == "message" and payload.get("role") == "user"):
                task = task or text
            if kind == "agent_message" or (kind == "message" and payload.get("role") == "assistant"):
                result = text
            if record.get("type") == "turn_context":
                model = payload.get("model", model)
            command = None
            if kind in {"function_call", "custom_tool_call"}:
                calls += 1
                arguments = payload.get("arguments", payload.get("input"))
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        pass
                if isinstance(arguments, dict):
                    command = arguments.get("cmd", arguments.get("command"))
            if kind == "token_count":
                usage = (payload.get("info") or {}).get("total_token_usage") or {}
                token_input = usage.get("input_tokens", token_input)
                token_output = usage.get("output_tokens", token_output)
            exit_code = payload.get("exit_code", payload.get("return_code"))
            if exit_code is None and kind == "custom_tool_call_output" and isinstance(text, str):
                match = _EXIT_CODE.search(text)
                if match:
                    exit_code = int(match.group(1))
            events.append(SessionEvent(
                type=kind, timestamp=timestamp, command=command, exit_code=exit_code, content=text,
                metadata={"source_record": record},
            ))
        git = meta.get("git") or {}
        return Session(
            id=meta.get("id") or hashlib.sha256(raw).hexdigest(), agent="codex",
            agent_version=meta.get("cli_version"), model=model,
            repository=meta["cwd"], base_commit=git.get("commit_hash"),
            started_at=datetime.fromisoformat(meta["timestamp"].replace("Z", "+00:00")),
            task=task, result=result, token_input=token_input, token_output=token_output,
            tool_call_count=calls, events=events,
        )
