"""Read-only normalization of Claude Code transcripts, including split messages."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from retryloom.models import Session, SessionEvent


def _time(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else None
    except ValueError:
        return None


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(x['text'] for x in value if isinstance(x, dict) and isinstance(x.get('text'), str)) or None
    return None


def _number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _result_metadata(record, block, results):
    """Use record metadata only for a uniquely identified result."""
    extra = record.get('toolUseResult')
    if not isinstance(extra, dict):
        return {}
    identifiers = [source[key] for source in (record, extra)
                   for key in ('tool_use_id', 'toolUseId', 'call_id')
                   if key in source and source[key] is not None]
    if identifiers:
        if any(not isinstance(value, str) or not value for value in identifiers):
            return {}
        if len(set(identifiers)) != 1:
            return {}
        target = identifiers[0]
        matches = [item for item in results if item.get('tool_use_id') == target]
        return extra if len(matches) == 1 and matches[0] is block else {}
    return extra if len(results) == 1 and results[0] is block else {}


def _structured_exit_code(block, extra):
    """Conflicting or absent structured status is unknown, never text-derived."""
    values = [source[key] for source in (block, extra)
              for key in ('exit_code', 'exitCode')
              if isinstance(source.get(key), int) and not isinstance(source[key], bool)]
    return values[0] if values and len(set(values)) == 1 else None


class ClaudeAdapter:
    def __init__(self, sessions_root: Path | None = None):
        self.sessions_root = sessions_root or Path.home() / '.claude' / 'projects'

    def discover(self) -> list[Path]:
        if self.sessions_root.is_symlink():
            raise ValueError('Claude discovery root must not be a symlink')
        return sorted(p for p in self.sessions_root.rglob('*') if p.is_file() and not p.is_symlink() and p.suffix in {'.json', '.jsonl'})

    def parse(self, path: Path) -> Session:
        raw = path.read_bytes()
        records = []
        source_lines = []
        issues = []
        if path.suffix == '.jsonl':
            for index, line in enumerate(raw.decode('utf-8', errors='replace').splitlines()):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    issues.append({'line': index + 1, 'reason': 'malformed JSON'})
                    continue
                if isinstance(item, dict):
                    records.append(item)
                    source_lines.append(index + 1)
                else:
                    issues.append({'line': index + 1, 'reason': 'non-object record'})
        else:
            data = json.loads(raw)
            items = data if isinstance(data, list) else data.get('messages', [data]) if isinstance(data, dict) else []
            if not isinstance(items, list):
                raise ValueError('Claude JSON wrapper messages must be a list')
            records = [x for x in items if isinstance(x, dict)]
            issues.extend({'record': i, 'reason': 'non-object record'} for i, x in enumerate(items) if not isinstance(x, dict))
        timestamps = [_time(r.get('timestamp')) for r in records]
        known = [t for t in timestamps if t is not None]
        if not known or not any(isinstance(r.get('type'), str) and r.get('type') in {'user', 'assistant', 'result', 'system'} for r in records):
            raise ValueError('Claude transcript requires usable message records and a source timestamp')
        events = []
        calls = {}
        usage = {}
        cumulative = None
        cumulative_index = -1
        last_assistant_index = -1
        assistant_records = 0
        unidentified_message = False
        task = model = version = repository = base = result = None
        session_id = None
        for index, record in enumerate(records):
            kind = record.get('type', 'unknown')
            if not isinstance(kind, str):
                issues.append({'record': index, 'reason': 'invalid record type'})
                kind = 'unknown'
            timestamp = timestamps[index]
            metadata = {'source_record': record, 'source_index': index}
            if source_lines:
                metadata['source_line'] = source_lines[index]
            if record.get('timestamp') is not None and timestamp is None:
                issues.append({'record': index, 'reason': 'invalid timestamp'})
            identifier = record.get('sessionId', record.get('session_id'))
            if isinstance(identifier, str):
                if session_id is not None and identifier != session_id:
                    raise ValueError('Claude transcript contains conflicting session IDs')
                session_id = session_id or identifier
            repository = repository or record.get('cwd')
            version = version or record.get('version') or record.get('claude_code_version')
            git = record.get('git')
            if isinstance(git, dict):
                base = base or git.get('commit_hash') or git.get('base_commit')
            base = base or record.get('base_commit')
            message = record.get('message')
            message = message if isinstance(message, dict) else {}
            model = message.get('model') or record.get('model') or model
            metrics = message.get('usage')
            if kind == 'assistant' and message:
                last_assistant_index = index
                assistant_records += 1
                if not isinstance(message.get('id'), str) or not message.get('id'):
                    unidentified_message = True
            if (kind == 'assistant' and message) or isinstance(metrics, dict):
                key = message.get('id') or record.get('uuid')
                if not isinstance(key, str):
                    key = f'record-{index}'
                    if message.get('id') is not None:
                        issues.append({'record': index, 'reason': 'invalid message ID'})
                # Split streamed blocks repeat usage for the same API message.
                old = usage.setdefault(key, {})
                for name in ('input_tokens', 'output_tokens'):
                    value = _number(metrics.get(name)) if isinstance(metrics, dict) else None
                    if value is not None:
                        old[name] = max(old.get(name, 0), value)
            if kind == 'cost-state':
                # A newer incomplete checkpoint invalidates the older checkpoint.
                cumulative = None
                cumulative_index = -1
                entries = list(record['modelUsage'].values()) if isinstance(record.get('modelUsage'), dict) else []
                if entries and all(isinstance(u, dict) and _number(u.get('inputTokens')) is not None and _number(u.get('outputTokens')) is not None for u in entries):
                    cumulative = (sum(u['inputTokens'] for u in entries), sum(u['outputTokens'] for u in entries))
                    cumulative_index = index
            if kind == 'result':
                if record.get('is_error') is True:
                    result = 'error'
                elif record.get('subtype') == 'success' or record.get('result') == 'success':
                    result = 'success'
                elif isinstance(record.get('result'), str) and record['result'] in {'failure', 'error'}:
                    result = record['result']
            content = message.get('content', record.get('content'))
            blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content}] if isinstance(content, str) else []
            request_text = '\n'.join(block['text'] for block in blocks
                                    if isinstance(block, dict) and block.get('type') == 'text'
                                    and isinstance(block.get('text'), str))
            tool_results = [block for block in blocks
                            if isinstance(block, dict) and block.get('type') == 'tool_result']
            if not blocks:
                events.append(SessionEvent(type=str(kind), timestamp=timestamp, metadata=metadata))
            block_event_start = len(events)
            for block in blocks:
                if not isinstance(block, dict):
                    issues.append({'record': index, 'reason': 'non-object content block'})
                    continue
                block_type = block.get('type', 'unknown')
                meta = dict(metadata, source_block=block)
                if block_type == 'tool_use':
                    call_id = block.get('id')
                    if call_id is not None and not isinstance(call_id, str):
                        issues.append({'record': index, 'reason': 'invalid tool ID'})
                        call_id = None
                    payload = block.get('input') if isinstance(block.get('input'), dict) else {}
                    name = block.get('name')
                    name = name if isinstance(name, str) else None
                    if call_id:
                        calls.setdefault(call_id, (name, payload))
                    else:
                        calls[f'missing-{index}-{len(events)}'] = (name, payload)
                    events.append(SessionEvent(type='tool_call', timestamp=timestamp, command=payload.get('command') if isinstance(payload.get('command'), str) else None, metadata=dict(meta, tool_name=name, tool_call_id=call_id, tool_input=payload)))
                elif block_type == 'tool_result':
                    call_id = block.get('tool_use_id')
                    if not isinstance(call_id, str):
                        issues.append({'record': index, 'reason': 'invalid tool result ID'})
                        call_id = None
                    name, payload = calls.get(call_id, (None, {}))
                    text = _text(block.get('content'))
                    extra = _result_metadata(record, block, tool_results)
                    exit_code = _structured_exit_code(block, extra)
                    events.append(SessionEvent(type='tool_result', timestamp=timestamp, command=payload.get('command') if isinstance(payload.get('command'), str) else None, exit_code=exit_code, content=text, metadata=dict(meta, tool_call_id=call_id, tool_name=name, is_error=block.get('is_error'), result_metadata=extra)))
                    if name in {'Edit', 'Write', 'MultiEdit'} and block.get('is_error') is not True and (extra.get('structuredPatch') or extra.get('oldString') is not None):
                        # Source supplies selected edit data; no reconstructed answer/diff.
                        events.append(SessionEvent(type='file_edit', timestamp=timestamp, file_path=extra.get('filePath') if isinstance(extra.get('filePath'), str) else payload.get('file_path') if isinstance(payload.get('file_path'), str) else None, content=json.dumps(extra.get('structuredPatch'), sort_keys=True) if extra.get('structuredPatch') else extra.get('newString') if isinstance(extra.get('newString'), str) else None, metadata=dict(meta, source_before=extra.get('originalFile') if isinstance(extra.get('originalFile'), str) else extra.get('oldString') if isinstance(extra.get('oldString'), str) else '')))
                elif block_type == 'text':
                    text = _text(block.get('text'))
                    canonical = 'user_message' if kind == 'user' else 'agent_message' if kind == 'assistant' else str(kind)
                    events.append(SessionEvent(type=canonical, timestamp=timestamp, content=text, metadata=meta))
                    if kind == 'user' and task is None and request_text and not record.get('isMeta') and not record.get('isSidechain') and not re.match(r'^\s*(?:<local-command|<command-name|<system-reminder|(?:yes|ok|approved|continue)[.!]?\s*$)', request_text, re.I):
                        task = request_text
                else:
                    events.append(SessionEvent(type=str(block_type), timestamp=timestamp, metadata=meta))
            if blocks and len(events) == block_event_start:
                events.append(SessionEvent(type=str(kind), timestamp=timestamp, metadata=metadata))
        tokens = cumulative if cumulative_index >= last_assistant_index else None
        if tokens is None and usage:
            tokens = tuple(sum(u[name] for u in usage.values()) if all(name in u for u in usage.values()) else None for name in ('input_tokens', 'output_tokens'))
            if unidentified_message and assistant_records > 1:
                tokens = None
        if issues:
            # A damaged record can conceal usage after a checkpoint.
            tokens = None
        if issues:
            events.append(SessionEvent(type='parse_warning', metadata={'issues': issues}))
        identity = str(session_id or hashlib.sha256(raw).hexdigest())
        if 'subagents' in path.parts:
            identity += ':' + path.stem
        return Session(id=identity, agent='Claude', agent_version=version if isinstance(version, str) else None, model=model if isinstance(model, str) else None, repository=repository if isinstance(repository, str) else '', base_commit=base if isinstance(base, str) else None, started_at=known[0], ended_at=known[-1] if result is not None else None, task=task, result=result, token_input=tokens[0] if tokens else None, token_output=tokens[1] if tokens else None, tool_call_count=len(calls), events=events)
