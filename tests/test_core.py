"""验证配置保护、取消进程、串行执行和定时去重。"""

from dataclasses import replace
import json
import os
import sys
import threading
import time

import pytest

from dbbackup.backup import BackupResult
from dbbackup.instance import InstanceLock
from dbbackup.models import AppConfig, BackupJob, Connection
from dbbackup.postgres import Postgres, identifier, literal
from dbbackup.process import Cancelled, ProcessRunner
from dbbackup.storage import ConfigStore
from dbbackup.tasks import Scheduler, TaskManager


def job(name="测试"):
    return BackupJob(name, Connection(host="remote.example", database="source", password="远程密码"),
                     Connection(database="local", password="本地密码"))


def test_forbid_system_database_remote_target_and_duplicate_target():
    item = job()
    for target in [replace(item.target, database="postgres"),
                   replace(item.target, host="remote.example")]:
        with pytest.raises(ValueError):
            replace(item, target=target).validate()
    with pytest.raises(ValueError, match="同一个本地目标"):
        AppConfig(jobs=[item, job("另一任务")]).validate()


def test_quote_and_connection_password_not_in_command():
    connection = Connection(host="::1", user="用户@名", database="库 / ?='", password="秘密")
    uri = Postgres.uri(connection)
    assert "秘密" not in uri
    assert "[::1]" in uri
    assert "库 /" not in uri
    assert identifier('库"名') == '"库""名"'
    assert literal("库'名") == "'库''名'"
    assert "秘密" not in repr(connection)


@pytest.mark.skipif(os.name != "nt", reason="密码保护使用 Windows DPAPI")
def test_encrypted_configuration_roundtrip(tmp_path):
    config = AppConfig(pg_bin="工具目录", auto_start=True, jobs=[job()])
    store = ConfigStore(tmp_path)
    store.save(config)
    raw_text = store.path.read_text(encoding="utf-8")
    assert "远程密码" not in raw_text and "本地密码" not in raw_text
    assert json.loads(raw_text)["version"] == 1
    assert store.load() == config


def test_only_one_application_instance_per_data_directory(tmp_path):
    first = InstanceLock(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            InstanceLock(tmp_path)
    finally:
        first.close()
    second = InstanceLock(tmp_path)
    second.close()


def test_cancel_running_process():
    event = threading.Event()
    timer = threading.Timer(0.2, event.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            ProcessRunner().run([sys.executable, "-c", "import time; time.sleep(20)"], cancel=event)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5


def test_queue_serializes_and_suppresses_duplicates():
    started = threading.Event()
    release = threading.Event()
    done = threading.Event()
    calls = []
    events = []

    def execute(item, cancel, progress):
        calls.append(item.name)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
        if len(calls) == 2:
            done.set()
        return BackupResult("success", "成功", "现在")

    manager = TaskManager(execute, lambda *event: events.append(event))
    first = job("一")
    second = replace(job("二"), target=Connection(database="local2"))
    try:
        assert manager.submit(first)
        assert started.wait(2)
        assert not manager.submit(first)
        assert manager.submit(second)
        assert calls == ["一"]
        release.set()
        assert done.wait(2)
        assert calls == ["一", "二"]
    finally:
        release.set()
        manager.close()
        manager.join(3)
    assert not manager.alive


def test_stop_all_cancels_active_and_waiting():
    started = threading.Event()
    calls = []

    def execute(item, cancel, progress):
        calls.append(item.name)
        started.set()
        assert cancel.wait(3)
        return BackupResult("cancelled", "停止", "现在")

    manager = TaskManager(execute, lambda *event: None)
    try:
        manager.submit(job("一"))
        assert started.wait(2)
        manager.submit(replace(job("二"), target=Connection(database="local2")))
        manager.stop_all()
    finally:
        manager.close()
        manager.join(3)
    assert calls == ["一"]


def test_scheduler_waits_one_interval_and_does_not_replay_missed_runs():
    now = [0]
    submitted = []
    item = replace(job(), scheduled=True, interval_minutes=1)
    scheduler = Scheduler(lambda item: submitted.append(item.id), lambda: now[0])
    scheduler.tick([item])
    assert not submitted
    now[0] = 61
    scheduler.tick([item])
    assert submitted == [item.id]
    now[0] = 3600
    scheduler.tick([item])
    scheduler.tick([item])
    assert submitted == [item.id, item.id]
    scheduler.tick([replace(item, enabled=False)])
    now[0] = 7200
    scheduler.tick([item])
    assert len(submitted) == 2
