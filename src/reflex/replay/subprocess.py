"""Bounded POSIX execution. Escaped descendants are not a sandbox guarantee."""
from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool = False
    error: str | None = None
    cleanup_error: str | None = None
    output_truncated: bool = False

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


def execute_process(command: list[str], cwd: Path, env: dict[str, str],
                    timeout_seconds: float, input_text: str | None = None,
                    output_limit: int = 1024 * 1024) -> ProcessResult:
    if timeout_seconds <= 0 or output_limit <= 0:
        raise ValueError("timeout and output limit must be positive")
    start = time.monotonic()
    try:
        process = subprocess.Popen(command, cwd=cwd, env=dict(env),
                                   stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
    except (OSError, ValueError) as error:
        return ProcessResult(None, "", str(error), time.monotonic() - start, error=str(error))
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    pending = memoryview((input_text or "").encode("utf-8"))
    timed_out = truncated = False
    cleanup_errors: list[str] = []
    execution_error = None
    deadline = start + timeout_seconds
    drain_deadline: float | None = None

    def kill_group() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            cleanup_errors.append(str(error))
            try:
                process.kill()
            except OSError as fallback_error:
                cleanup_errors.append(str(fallback_error))

    with selectors.DefaultSelector() as selector:
        def close_pipe(pipe) -> None:
            selector.unregister(pipe)
            pipe.close()

        for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        if process.stdin is not None:
            if pending:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
        try:
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if drain_deadline is None and now >= deadline:
                    timed_out = True
                    kill_group()
                    drain_deadline = now + 1.0
                if drain_deadline is not None and now >= drain_deadline:
                    if selector.get_map():
                        cleanup_errors.append("output pipes remained open after process-group termination")
                    break
                for key, _ in selector.select(timeout=0.05):
                    pipe = key.fileobj
                    if key.data == "stdin":
                        try:
                            written = os.write(pipe.fileno(), pending[:65536])
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = pending[:0]
                        except BlockingIOError:
                            continue
                        if not pending:
                            close_pipe(pipe)
                        continue
                    try:
                        data = os.read(pipe.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        close_pipe(pipe)
                        continue
                    buffer = buffers[key.data]
                    available = max(0, output_limit - len(buffer))
                    buffer.extend(data[:available])
                    truncated |= len(data) > available
        except (OSError, ValueError) as error:
            execution_error = str(error)
            kill_group()
        finally:
            # A leader may exit while background descendants are still alive.
            kill_group()
            for key in list(selector.get_map().values()):
                close_pipe(key.fileobj)
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                cleanup_errors.append("process did not exit after termination")
    return ProcessResult(process.returncode,
                         buffers["stdout"].decode("utf-8", errors="replace"),
                         buffers["stderr"].decode("utf-8", errors="replace"),
                         time.monotonic() - start, timed_out, execution_error,
                         "; ".join(cleanup_errors) or None, truncated)
