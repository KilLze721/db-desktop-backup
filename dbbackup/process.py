"""可停止的子进程执行，不通过命令行传递密码。"""

import os
import subprocess
import tempfile
import threading
import time


class Cancelled(Exception):
    pass


class ProcessError(RuntimeError):
    pass


class ProcessRunner:
    def run(self, command: list[str], *, env: dict[str, str] | None = None,
            input_text: str | None = None, cancel: threading.Event | None = None,
            timeout: float | None = None) -> str:
        if cancel is not None and cancel.is_set():
            raise Cancelled("任务已停止")
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=output, stderr=errors, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            started = time.monotonic()
            try:
                if input_text is not None:
                    process.stdin.write(input_text.encode("utf-8"))
                    process.stdin.close()
                while process.poll() is None:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled("任务已停止")
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise ProcessError("操作超时")
                    time.sleep(0.05)
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise
            finally:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
            output.seek(0)
            result = output.read().decode("utf-8", errors="replace").strip()
            if process.returncode:
                errors.seek(0, 2)
                errors.seek(max(0, errors.tell() - 12000))
                detail = errors.read().decode("utf-8", errors="replace").strip()
                password = (env or {}).get("PGPASSWORD", "")
                if password:
                    detail = detail.replace(password, "***")
                raise ProcessError(detail or f"进程退出，错误码 {process.returncode}")
            return result
