"""在独立实例验证真实导出、恢复、切换和中断。"""

from dataclasses import replace
import json
import threading
import time
import uuid

import pytest

from dbbackup.backup import BackupEngine
from dbbackup.models import BackupJob
from dbbackup.postgres import identifier
from dbbackup.process import ProcessError

pytestmark = pytest.mark.integration


def prepare(databases):
    pg, maintenance, names = databases
    source, target, unrelated = [replace(maintenance, database=name) for name in names]
    pg.sql(source, """
CREATE TABLE sample(id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, content text NOT NULL);
INSERT INTO sample(content) VALUES ('中文数据'), ('服务器内容');
CREATE VIEW sample_view AS SELECT content FROM sample;
CREATE FUNCTION sample_count() RETURNS bigint LANGUAGE SQL AS 'SELECT count(*) FROM sample';
""")
    pg.sql(target, "CREATE TABLE obsolete(id int); INSERT INTO obsolete VALUES (99);")
    pg.sql(unrelated, "CREATE TABLE untouched(value text); INSERT INTO untouched VALUES ('保留');")
    return pg, maintenance, BackupJob("实际备份", source, target), unrelated


def test_full_replacement_preserves_other_databases_and_keeps_one_copy(databases, tmp_path):
    pg, maintenance, job, unrelated = prepare(databases)
    engine = BackupEngine(pg, tmp_path)
    for _ in range(2):
        result = engine.run(job, threading.Event())
        assert result.status == "success", result
        assert not result.warnings, result.warnings
    assert pg.sql(job.target, "SELECT string_agg(content, ',' ORDER BY id) FROM sample;") == "中文数据,服务器内容"
    assert pg.sql(job.target, "SELECT sample_count();") == "2"
    assert pg.sql(job.target, "SELECT to_regclass('obsolete') IS NULL;") == "t"
    assert pg.sql(unrelated, "SELECT value FROM untouched;") == "保留"
    assert pg.sql(maintenance, "SELECT count(*) FROM pg_database WHERE datname ~ '^_dbbackup_';") == "0"
    assert not list(tmp_path.rglob("*.dump"))
    assert not list((tmp_path / "working").glob("*/journal.json"))
    assert json.loads((tmp_path / "state" / f"{job.id}.json").read_text(encoding="utf-8"))["last_success"]


def test_cancel_after_export_preserves_previous_database(databases, tmp_path):
    pg, maintenance, job, unrelated = prepare(databases)
    event = threading.Event()

    def progress(message):
        if message == "恢复到本地临时数据库":
            event.set()

    result = BackupEngine(pg, tmp_path).run(job, event, progress)
    assert result.status == "cancelled", result
    assert not result.warnings
    assert pg.sql(job.target, "SELECT id FROM obsolete;") == "99"
    assert pg.sql(unrelated, "SELECT value FROM untouched;") == "保留"
    assert pg.sql(maintenance, "SELECT count(*) FROM pg_database WHERE datname ~ '^_dbbackup_';") == "0"


def test_restore_failure_does_not_replace_previous_database(databases, tmp_path, monkeypatch):
    pg, maintenance, job, unrelated = prepare(databases)
    original_restore = pg.restore

    def broken_restore(target, path, cancel):
        path.write_bytes(b"broken backup")
        original_restore(target, path, cancel)

    monkeypatch.setattr(pg, "restore", broken_restore)
    result = BackupEngine(pg, tmp_path).run(job, threading.Event())
    assert result.status == "failed"
    assert pg.sql(job.target, "SELECT id FROM obsolete;") == "99"
    assert pg.sql(maintenance, "SELECT count(*) FROM pg_database WHERE datname ~ '^_dbbackup_';") == "0"


def test_rename_transaction_rolls_back_when_second_rename_fails(databases):
    pg, maintenance, names = databases
    target = names[1]
    old = pg.database(maintenance, target)
    previous = "test_old_" + uuid.uuid4().hex
    with pytest.raises(ProcessError):
        pg.switch(maintenance, target, "missing_" + uuid.uuid4().hex, previous, old)
    assert pg.database(maintenance, target)["oid"] == old["oid"]
    assert pg.database(maintenance, previous) is None


def test_first_backup_creates_missing_target(databases, tmp_path):
    pg, maintenance, job, unrelated = prepare(databases)
    pg.drop(maintenance, job.target.database)
    result = BackupEngine(pg, tmp_path).run(job, threading.Event())
    assert result.status == "success", result
    assert not result.warnings
    assert pg.sql(job.target, "SELECT sample_count();") == "2"
    assert pg.sql(unrelated, "SELECT value FROM untouched;") == "保留"


def test_occupied_target_is_preserved_without_forcing_disconnect(databases, tmp_path):
    pg, maintenance, job, unrelated = prepare(databases)
    release = threading.Event()
    errors = []

    def occupy():
        try:
            pg.sql(job.target, "SELECT pg_sleep(15);", release)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=occupy)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not pg.database(maintenance, job.target.database)["sessions"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        result = BackupEngine(pg, tmp_path).run(job, threading.Event())
        assert result.status == "failed"
        assert "其他程序" in result.message
        assert job.target.database in result.message
        assert "PID=" in result.message
        assert "db-desktop-backup" in result.message
        assert "idle 也表示连接仍存在" in result.message
        assert pg.sql(job.target, "SELECT id FROM obsolete;") == "99"
        assert pg.database(maintenance, job.target.database)["sessions"] >= 1
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def test_names_with_quotes_and_dollar_delimiters(isolated_pg):
    pg, maintenance = isolated_pg
    name = "测试'\"$check$" + uuid.uuid4().hex[:6]
    stage = "test_stage_" + uuid.uuid4().hex
    previous = "test_old_" + uuid.uuid4().hex
    pg.create(maintenance, name, "UTF8")
    pg.create(maintenance, stage, "UTF8")
    old = pg.database(maintenance, name)
    stage_oid = pg.database(maintenance, stage)["oid"]
    try:
        pg.switch(maintenance, name, stage, previous, old)
        assert pg.database(maintenance, name)["oid"] == stage_oid
        assert pg.database(maintenance, previous)["oid"] == old["oid"]
    finally:
        for current in [name, stage, previous]:
            if pg.database(maintenance, current):
                pg.drop(maintenance, current)
