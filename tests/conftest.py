"""仅在显式启用时启动项目目录内的独立 PostgreSQL 测试实例。"""

import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import uuid

import pytest

from dbbackup.models import Connection, discover_pg_bin
from dbbackup.postgres import Postgres, identifier


def pytest_configure(config):
    if config.option.basetemp is None:
        # 每轮使用项目内的独立目录，避免沙箱内外用户的系统临时目录权限冲突。
        config.option.basetemp = str(Path(__file__).resolve().parents[1] / ".test_runtime" /
                                     ("pytest-" + uuid.uuid4().hex))


@pytest.fixture(scope="session")
def isolated_pg():
    if os.environ.get("RUN_PG_INTEGRATION") != "1":
        pytest.skip("设置 RUN_PG_INTEGRATION=1 后才启动独立 PostgreSQL 测试实例")
    bin_path = os.environ.get("PGBACKUP_TEST_BIN") or discover_pg_bin()
    if not bin_path:
        pytest.fail("未找到测试所需的 PostgreSQL 工具")
    base = Path(__file__).resolve().parents[1] / ".test_runtime"
    root = base / uuid.uuid4().hex
    cluster = root / "cluster"
    root.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    suffix = ".exe" if os.name == "nt" else ""

    def run(tool, *args):
        # 服务进程会继承句柄，使用文件而非等待管道 EOF。
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                [str(Path(bin_path) / (tool + suffix)), *map(str, args)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode:
                output.seek(0)
                pytest.fail(output.read().decode("utf-8", errors="replace"))

    started = False
    try:
        run("initdb", "-D", cluster, "-U", "postgres", "-A", "trust", "--no-locale", "--encoding=UTF8")
        run("pg_ctl", "-D", cluster, "-l", root / "server.log", "-w", "-t", "30", "-o",
            f"-h 127.0.0.1 -p {port} -c max_connections=20 -c shared_buffers=16MB", "start")
        started = True
        pg = Postgres(bin_path)
        connection = Connection(host="127.0.0.1", port=port, user="postgres", database="postgres", sslmode="disable")
        yield pg, connection
    finally:
        if started or (cluster / "postmaster.pid").exists():
            run("pg_ctl", "-D", cluster, "-m", "immediate", "-w", "stop")
        # 只删除本次测试创建且位于项目测试目录下的实例目录。
        if root.resolve().is_relative_to(base.resolve()) and root != base:
            shutil.rmtree(root)


@pytest.fixture
def databases(isolated_pg):
    pg, maintenance = isolated_pg
    names = [f"test_{uuid.uuid4().hex}" for _ in range(3)]
    for name in names:
        pg.create(maintenance, name, "UTF8")
    try:
        yield pg, maintenance, names
    finally:
        for name in names:
            if pg.database(maintenance, name):
                pg.sql(maintenance, f"DROP DATABASE {identifier(name)} WITH (FORCE);")
