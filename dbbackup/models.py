"""配置模型与输入检查。"""

from dataclasses import dataclass, field
from pathlib import Path
import uuid


@dataclass
class Connection:
    host: str = "127.0.0.1"
    port: int = 5432
    user: str = "postgres"
    database: str = ""
    password: str = field(default="", repr=False)
    sslmode: str = "prefer"

    def validate(self, *, local: bool = False) -> None:
        if not self.host.strip() or not self.user.strip() or not self.database.strip():
            raise ValueError("地址、用户名和数据库名不能为空")
        if not 1 <= self.port <= 65535:
            raise ValueError("端口必须在 1 至 65535 之间")
        if self.sslmode not in {"disable", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError("不支持的 SSL 模式")
        if any("\x00" in value or "\n" in value or "\r" in value
               for value in (self.host, self.user, self.database)):
            raise ValueError("连接信息不能包含换行或空字符")
        if len(self.database.encode("utf-8")) > 63:
            raise ValueError("数据库名称不能超过 63 个 UTF-8 字节")
        if local:
            if self.host.lower() not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("本地目标地址只允许 localhost、127.0.0.1 或 ::1")
            if self.database.lower() in {"postgres", "template0", "template1"}:
                raise ValueError("不能将 postgres、template0 或 template1 作为备份目标")
            if self.database.startswith("_dbbackup_"):
                raise ValueError("目标数据库不能使用程序临时库前缀 _dbbackup_")


@dataclass
class BackupJob:
    name: str
    source: Connection
    target: Connection
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    enabled: bool = True
    scheduled: bool = False
    interval_minutes: int = 60

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("任务名称不能为空")
        if len(self.id) != 32 or any(c not in "0123456789abcdef" for c in self.id):
            raise ValueError("任务标识无效")
        self.source.validate()
        self.target.validate(local=True)
        if self.interval_minutes < 1:
            raise ValueError("备份间隔至少为 1 分钟")
        if (self.source.host.lower() in {"localhost", "127.0.0.1", "::1"}
                and self.source.port == self.target.port
                and self.source.database == self.target.database):
            raise ValueError("源数据库和本地目标数据库不能是同一个库")


@dataclass
class AppConfig:
    pg_bin: str = ""
    auto_start: bool = False
    jobs: list[BackupJob] = field(default_factory=list)

    def validate(self) -> None:
        ids = set()
        targets = set()
        for job in self.jobs:
            job.validate()
            target = (job.target.port, job.target.database)
            if job.id in ids:
                raise ValueError("存在重复的任务标识")
            if target in targets:
                raise ValueError("多个任务不能写入同一个本地目标数据库")
            ids.add(job.id)
            targets.add(target)


def discover_pg_bin() -> str:
    roots = [Path("D:/Program Files/PostgreSQL"), Path("C:/Program Files/PostgreSQL")]
    for root in roots:
        if root.is_dir():
            versions = sorted(root.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else -1,
                              reverse=True)
            for version in versions:
                if (version / "bin" / "pg_dump.exe").is_file():
                    return str(version / "bin")
    return ""
