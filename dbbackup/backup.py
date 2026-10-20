"""单库备份流程，先恢复临时库，成功后替换对应目标库。"""

from dataclasses import dataclass, field, replace
from datetime import datetime
import json
from pathlib import Path
import threading
import uuid
from typing import Callable

from .models import BackupJob
from .postgres import Postgres
from .process import Cancelled
from .storage import write_json


@dataclass
class BackupResult:
    status: str
    message: str
    finished_at: str
    warnings: list[str] = field(default_factory=list)


class BackupEngine:
    def __init__(self, postgres: Postgres, root: Path):
        self.pg = postgres
        self.root = root

    def run(self, job: BackupJob, cancel: threading.Event,
            progress: Callable[[str], None] = lambda message: None) -> BackupResult:
        job.validate()
        run_id = uuid.uuid4().hex
        stage = f"_dbbackup_stage_{run_id}"
        previous = f"_dbbackup_old_{run_id}"
        work = self.root / "working" / run_id
        dump_path = work / "backup.dump"
        journal_path = work / "journal.json"
        maintenance = self.pg.maintenance(job.target)
        created = False
        promoted = False
        stage_oid = None
        original = None
        warnings = []
        status = "failed"
        message = "备份未完成"
        work.mkdir(parents=True)
        journal = {"job_id": job.id, "target": job.target.database, "stage": stage,
                   "previous": previous, "phase": "checking", "stage_oid": None, "original_oid": None}

        def checkpoint(phase: str) -> None:
            journal["phase"] = phase
            write_json(journal_path, journal)

        def check_cancel() -> None:
            if cancel.is_set():
                raise Cancelled("任务已停止，保留上一份成功备份")

        try:
            checkpoint("checking")
            progress("检查连接与版本")
            tool_major = self.pg.tool_major()
            source_info = self.pg.info(job.source, cancel)
            local_info = self.pg.info(maintenance, cancel)
            if source_info["version"] // 10000 > tool_major:
                raise ValueError("服务器版本高于备份工具，请选择更新的 PostgreSQL 工具")
            if local_info["version"] // 10000 < tool_major:
                raise ValueError("本地 PostgreSQL 版本低于备份工具，不支持向旧版本恢复")
            if not local_info["can_create"]:
                raise ValueError("本地账号需要 CREATEDB 权限，并拥有目标数据库")
            if (source_info["started_at"] == local_info["started_at"]
                    and source_info["server_port"] == local_info["server_port"]
                    and job.source.database == job.target.database):
                raise ValueError("检测到源数据库和目标数据库属于同一实例中的同一个库")
            original = self.pg.database(maintenance, job.target.database, cancel)
            if original:
                if original["owner"] != local_info["user"] and not local_info["superuser"]:
                    raise ValueError("本地账号必须拥有目标数据库，或具有超级用户权限")
                if original["sessions"]:
                    try:
                        connections = self.pg.connections(maintenance, job.target.database, cancel)
                        details = "；".join(
                            f"PID={item['pid']}，应用={item.get('application_name') or '未标注'}，"
                            f"用户={item.get('usename') or '未知'}，"
                            f"地址={item.get('client_addr') or '本机或内部进程'}，"
                            f"状态={item.get('state') or '不可见'}，"
                            f"类型={item.get('backend_type') or '不可见'}"
                            for item in connections
                        ) or "详情为空，连接可能已退出，或当前账号无法查看详情"
                    except Cancelled:
                        raise
                    except Exception as error:
                        details = f"读取连接详情失败：{error}"
                    raise ValueError(
                        f"本地目标数据库“{job.target.database}”正在被其他程序使用，"
                        f"检查时发现 {original['sessions']} 个连接。{details}。"
                        "请关闭对应程序的数据库连接后重试；idle 也表示连接仍存在。"
                        "程序未强制断开连接，未替换本地数据库。"
                    )
            journal["original_oid"] = original["oid"] if original else None
            progress("从服务器导出数据库")
            checkpoint("dumping")
            self.pg.dump(job.source, dump_path, cancel)
            check_cancel()
            if not dump_path.is_file() or dump_path.stat().st_size == 0:
                raise ValueError("未生成有效的备份文件")
            progress("创建本地临时数据库")
            checkpoint("creating")
            # 创建动作不在半途取消，确保随后能够记录并清理准确的临时库。
            self.pg.create(maintenance, stage, source_info["encoding"])
            created = True
            stage_info = self.pg.database(maintenance, stage)
            stage_oid = stage_info["oid"]
            journal["stage_oid"] = stage_oid
            checkpoint("restoring")
            check_cancel()
            progress("恢复到本地临时数据库")
            temporary_target = replace(job.target, database=stage)
            self.pg.restore(temporary_target, dump_path, cancel)
            progress("验证恢复结果")
            stats = self.pg.validate_restore(temporary_target, cancel)
            check_cancel()
            checkpoint("switching")
            progress("切换目标数据库（此步骤结束后处理停止请求）")
            # 切换事务执行期间不终止子进程，避免提交结果不明确。
            self.pg.switch(maintenance, job.target.database, stage, previous, original)
            promoted = True
            checkpoint("promoted")
            status = "success"
            message = f"备份成功：{stats['tables']} 张表，{stats['views']} 个视图或物化视图"
        except Cancelled as error:
            status = "cancelled"
            message = str(error)
        except Exception as error:
            message = str(error)
            # 子进程异常退出可能发生在提交之后，先按数据库标识核实结果。
            if stage_oid is not None and journal["phase"] in {"switching", "promoted"}:
                try:
                    current = self.pg.database(maintenance, job.target.database)
                    promoted = current is not None and current["oid"] == stage_oid
                    if promoted:
                        status = "success"
                        message = "备份已切换成功，后续步骤出现异常"
                        warnings.append(str(error))
                except Exception as inspection_error:
                    warnings.append(f"无法核实切换结果，请保留临时库并检查执行记录：{inspection_error}")
                    created = False
        finally:
            cleanup_ok = True
            if promoted and original:
                progress("清理上一份备份")
                try:
                    old = self.pg.database(maintenance, previous)
                    if old and old["oid"] == original["oid"]:
                        self.pg.drop(maintenance, previous)
                    elif old:
                        raise RuntimeError("旧库标识发生变化，未进行删除")
                except Exception as error:
                    cleanup_ok = False
                    warnings.append(f"新备份可用，但旧临时库 {previous} 清理失败：{error}")
            elif created:
                try:
                    temporary = self.pg.database(maintenance, stage)
                    if temporary and (stage_oid is None or temporary["oid"] == stage_oid):
                        self.pg.drop(maintenance, stage)
                    elif temporary:
                        raise RuntimeError("临时库标识发生变化，未进行删除")
                except Exception as error:
                    cleanup_ok = False
                    warnings.append(f"临时库 {stage} 清理失败：{error}")
            try:
                dump_path.unlink(missing_ok=True)
            except OSError as error:
                cleanup_ok = False
                warnings.append(f"临时备份文件清理失败：{error}")
            finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            result = BackupResult(status, message, finished_at, warnings)
            record = {"status": status, "message": message, "finished_at": finished_at,
                      "warnings": warnings, "last_success": None}
            state_path = self.root / "state" / f"{job.id}.json"
            try:
                if state_path.exists():
                    record["last_success"] = json.loads(state_path.read_text(encoding="utf-8")).get("last_success")
                if status == "success":
                    record["last_success"] = finished_at
                write_json(state_path, record)
                if cleanup_ok and not any("无法核实" in warning for warning in warnings):
                    journal_path.unlink(missing_ok=True)
                    work.rmdir()
                else:
                    journal.update(status=status, warnings=warnings)
                    checkpoint("needs_attention")
            except Exception as error:
                warnings.append(f"执行记录写入或清理失败：{error}")
        return result
