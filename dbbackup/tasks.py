"""全局串行队列与程序运行期间的定时调度。"""

from collections import deque
from copy import deepcopy
import threading
import time
from typing import Callable

from .backup import BackupResult
from .models import BackupJob


class TaskManager:
    def __init__(self, execute: Callable, notify: Callable):
        self.execute = execute
        self.notify = notify
        self._condition = threading.Condition()
        self._queue = deque()
        self._active: BackupJob | None = None
        self._cancel = threading.Event()
        self._closing = False
        self._thread = threading.Thread(target=self._work, name="数据库备份队列", daemon=False)
        self._thread.start()

    def submit(self, job: BackupJob) -> bool:
        job.validate()
        with self._condition:
            if self._closing or self.busy(job.id):
                return False
            self._queue.append(deepcopy(job))
            self.notify(job.id, "queued", "等待执行")
            self._condition.notify()
            return True

    def busy(self, job_id: str) -> bool:
        with self._condition:
            return ((self._active is not None and self._active.id == job_id)
                    or any(job.id == job_id for job in self._queue))

    def stop_current(self) -> None:
        with self._condition:
            if self._active is not None:
                self._cancel.set()
                self.notify(self._active.id, "stopping", "正在停止，请等待清理完成")

    def cancel_pending(self, job_id: str | None = None) -> None:
        with self._condition:
            remaining = deque()
            for job in self._queue:
                if job_id is None or job.id == job_id:
                    self.notify(job.id, "cancelled", "已取消排队")
                else:
                    remaining.append(job)
            self._queue = remaining

    def stop_all(self) -> None:
        with self._condition:
            self.cancel_pending()
            self.stop_current()

    def close(self) -> None:
        with self._condition:
            self._closing = True
            self.stop_all()
            self._condition.notify_all()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _work(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closing)
                if self._closing:
                    return
                job = self._queue.popleft()
                self._active = job
                self._cancel = threading.Event()
            try:
                self.notify(job.id, "running", "正在开始")
                result = self.execute(job, self._cancel,
                                      lambda message: self.notify(job.id, "running", message))
                self.notify(job.id, result.status, result)
            except Exception as error:
                self.notify(job.id, "failed", str(error))
            finally:
                with self._condition:
                    self._active = None


class Scheduler:
    def __init__(self, submit: Callable[[BackupJob], bool], clock: Callable[[], float] = time.monotonic):
        self.submit = submit
        self.clock = clock
        self._due: dict[str, tuple[int, float]] = {}

    def tick(self, jobs: list[BackupJob]) -> None:
        now = self.clock()
        enabled = {job.id for job in jobs if job.enabled and job.scheduled}
        self._due = {key: value for key, value in self._due.items() if key in enabled}
        for job in jobs:
            if job.id not in enabled:
                continue
            interval = job.interval_minutes * 60
            previous = self._due.get(job.id)
            if previous is None or previous[0] != interval:
                self._due[job.id] = (interval, now + interval)
            elif now >= previous[1]:
                self.submit(job)
                self._due[job.id] = (interval, now + interval)
