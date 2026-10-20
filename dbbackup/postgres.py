"""PostgreSQL 官方工具的调用与数据库操作。"""

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import quote

from .models import Connection
from .process import ProcessRunner


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class Postgres:
    def __init__(self, bin_path: str, runner: ProcessRunner | None = None):
        self.bin_path = Path(bin_path)
        self.runner = runner or ProcessRunner()

    def executable(self, name: str) -> str:
        path = self.bin_path / (name + (".exe" if os.name == "nt" else ""))
        if not path.is_file():
            raise ValueError(f"找不到 PostgreSQL 工具：{path}")
        return str(path)

    def tool_major(self) -> int:
        output = self.runner.run([self.executable("pg_dump"), "--version"], timeout=10)
        match = re.search(r"PostgreSQL\) (\d+)", output)
        if match is None:
            raise ValueError("无法识别 PostgreSQL 工具版本")
        self.executable("pg_restore")
        self.executable("psql")
        return int(match.group(1))

    @staticmethod
    def uri(connection: Connection) -> str:
        host = connection.host
        if ":" in host:
            host = f"[{host}]"
        return (f"postgresql://{quote(connection.user, safe='')}@{host}:{connection.port}/"
                f"{quote(connection.database, safe='')}?sslmode={connection.sslmode}")

    @staticmethod
    def environment(connection: Connection) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
        env.update(PGPASSWORD=connection.password, PGCONNECT_TIMEOUT="10", PGCLIENTENCODING="UTF8",
                   PGAPPNAME="db-desktop-backup")
        return env

    def sql(self, connection: Connection, query: str, cancel: threading.Event | None = None) -> str:
        return self.runner.run(
            [self.executable("psql"), "-X", "-w", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
             "--dbname", self.uri(connection)],
            env=self.environment(connection),
            input_text=("SET statement_timeout = '30s'; SET lock_timeout = '5s'; "
                        "SET standard_conforming_strings = on;\n" + query + "\n"),
            cancel=cancel, timeout=40,
        )

    def info(self, connection: Connection, cancel: threading.Event | None = None) -> dict:
        return json.loads(self.sql(connection, """
SELECT json_build_object(
    'version', current_setting('server_version_num')::int,
    'database', current_database(), 'user', current_user,
    'encoding', pg_encoding_to_char(d.encoding),
    'started_at', extract(epoch FROM pg_postmaster_start_time())::text,
    'server_port', current_setting('port')::int,
    'can_create', r.rolcreatedb OR r.rolsuper, 'superuser', r.rolsuper
)
FROM pg_database d JOIN pg_roles r ON r.rolname = current_user
WHERE d.datname = current_database();
""", cancel))

    def database(self, connection: Connection, name: str,
                 cancel: threading.Event | None = None) -> dict | None:
        result = self.sql(connection, f"""
SELECT json_build_object('oid', d.oid, 'owner', pg_get_userbyid(d.datdba),
 'sessions', (SELECT count(*) FROM pg_stat_activity a WHERE a.datid = d.oid),
 'allow_connections', d.datallowconn)
FROM pg_database d WHERE d.datname = {literal(name)};
""", cancel)
        return json.loads(result) if result else None

    def connections(self, connection: Connection, name: str,
                    cancel: threading.Event | None = None) -> list[dict]:
        result = self.sql(connection, f"""
SELECT coalesce(json_agg(activity), '[]'::json)
FROM (
 SELECT pid, application_name, usename, client_addr::text AS client_addr, state, backend_type
 FROM pg_stat_activity
 WHERE datname = {literal(name)} AND pid <> pg_backend_pid()
 ORDER BY pid LIMIT 10
) activity;
""", cancel)
        return json.loads(result)

    def create(self, maintenance: Connection, name: str, encoding: str) -> None:
        self.sql(maintenance, f"CREATE DATABASE {identifier(name)} TEMPLATE template0 "
                 f"ENCODING {literal(encoding)};")

    def drop(self, maintenance: Connection, name: str) -> None:
        self.sql(maintenance, f"DROP DATABASE {identifier(name)};")

    def dump(self, source: Connection, path: Path, cancel: threading.Event) -> None:
        self.runner.run([
            self.executable("pg_dump"), "--no-password", "--format=custom",
            "--quote-all-identifiers", "--no-tablespaces", "--no-publications", "--no-subscriptions",
            "--file", str(path), "--dbname", self.uri(source),
        ], env=self.environment(source), cancel=cancel)

    def restore(self, target: Connection, path: Path, cancel: threading.Event) -> None:
        self.runner.run([
            self.executable("pg_restore"), "--no-password", "--exit-on-error", "--single-transaction",
            "--no-owner", "--no-privileges", "--no-tablespaces", "--dbname", self.uri(target), str(path),
        ], env=self.environment(target), cancel=cancel)

    def validate_restore(self, target: Connection, cancel: threading.Event) -> dict:
        return json.loads(self.sql(target, """
SELECT json_build_object(
 'tables', (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            AND n.nspname !~ '^pg_toast'),
 'views', (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relkind IN ('v', 'm') AND n.nspname NOT IN ('pg_catalog', 'information_schema'))
);
""", cancel))

    def switch(self, maintenance: Connection, target: str, stage: str,
               previous: str, original: dict | None) -> None:
        expected = str(original["oid"]) if original else "NULL"
        rename_previous = (f"ALTER DATABASE {identifier(target)} RENAME TO {identifier(previous)};"
                           if original else "")
        # 两次改名放在同一事务中，第二次失败时回滚第一次改名。
        self.sql(maintenance, f"""
BEGIN;
SELECT 1 / CASE WHEN (SELECT oid FROM pg_database WHERE datname = {literal(target)})
 IS NOT DISTINCT FROM {expected}::oid THEN 1 ELSE 0 END;
{rename_previous}
ALTER DATABASE {identifier(stage)} RENAME TO {identifier(target)};
COMMIT;
""")

    @staticmethod
    def maintenance(target: Connection) -> Connection:
        return replace(target, database="postgres")
