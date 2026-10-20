"""数据库配置、串行备份及停止操作的桌面界面。"""

import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .backup import BackupEngine, BackupResult
from .instance import InstanceLock
from .models import AppConfig, BackupJob, Connection
from .postgres import Postgres
from .storage import ConfigStore
from .tasks import Scheduler, TaskManager


class JobDialog(tk.Toplevel):
    def __init__(self, app: "Application", job: BackupJob | None = None):
        super().__init__(app)
        self.app = app
        self.job = job
        self.title("编辑数据库配置" if job else "添加数据库配置")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        self.name = tk.StringVar(value=job.name if job else "")
        ttk.Label(frame, text="任务名称").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.name, width=32).grid(row=0, column=1, sticky="ew", pady=6)
        self.fields = {}
        for column, (side, title, connection) in enumerate([
            ("source", "服务器源数据库（只读取）", job.source if job else Connection()),
            ("target", "本地目标数据库（完整替换）", job.target if job else Connection()),
        ]):
            group = ttk.LabelFrame(frame, text=title, padding=12)
            group.grid(row=1, column=column, sticky="nsew", padx=(0, 12) if column == 0 else 0, pady=10)
            values = {}
            for row, (key, label) in enumerate([
                ("host", "地址"), ("port", "端口"), ("user", "用户名"),
                ("password", "密码"), ("database", "数据库名"), ("sslmode", "SSL 模式"),
            ]):
                values[key] = tk.StringVar(value=str(getattr(connection, key)))
                ttk.Label(group, text=label).grid(row=row, column=0, sticky="w", pady=5)
                if key == "sslmode":
                    widget = ttk.Combobox(group, textvariable=values[key], state="readonly", width=24,
                                          values=["disable", "prefer", "require", "verify-ca", "verify-full"])
                else:
                    widget = ttk.Entry(group, textvariable=values[key], width=27,
                                       show="●" if key == "password" else "")
                widget.grid(row=row, column=1, padx=(10, 0), pady=5)
            self.fields[side] = values
        options = ttk.Frame(frame)
        options.grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
        self.enabled = tk.BooleanVar(value=job.enabled if job else True)
        self.scheduled = tk.BooleanVar(value=job.scheduled if job else False)
        self.interval = tk.StringVar(value=str(job.interval_minutes if job else 60))
        ttk.Checkbutton(options, text="参与启动自动备份和全部备份", variable=self.enabled).pack(side="left")
        ttk.Checkbutton(options, text="运行期间定时备份", variable=self.scheduled).pack(side="left", padx=10)
        ttk.Spinbox(options, from_=1, to=100000, textvariable=self.interval, width=7).pack(side="left")
        ttk.Label(options, text="分钟").pack(side="left", padx=5)
        ttk.Label(frame, text="仅替换指定目标 Database；其他数据库不变。请关闭目标库的其他连接。",
                  foreground="#9a4d13").grid(row=3, column=0, columnspan=2, sticky="w", pady=8)
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="保存配置", command=self.save).pack(side="right", padx=8)

    def save(self) -> None:
        try:
            connections = {}
            for side, fields in self.fields.items():
                data = {key: value.get() if key == "password" else value.get().strip()
                        for key, value in fields.items()}
                data["port"] = int(data["port"])
                connections[side] = Connection(**data)
            options = dict(name=self.name.get().strip(), source=connections["source"],
                           target=connections["target"], enabled=self.enabled.get(),
                           scheduled=self.scheduled.get(), interval_minutes=int(self.interval.get()))
            updated = replace(self.job, **options) if self.job else BackupJob(**options)
            jobs = [updated if item.id == updated.id else item for item in self.app.config.jobs]
            if self.job is None:
                jobs.append(updated)
            config = replace(self.app.config, jobs=jobs)
            self.app.store.save(config)
            self.app.config = config
            self.app.refresh()
            self.destroy()
        except Exception as error:
            messagebox.showerror("无法保存配置", str(error), parent=self)


class Application(tk.Tk):
    def __init__(self, root: Path, store: ConfigStore, config: AppConfig):
        super().__init__()
        self.root_path = root
        self.store = store
        self.config = config
        self.events = queue.Queue()
        self.statuses = {}
        self.closing = False
        self.schedule_paused = False
        self.test_thread = None
        self.test_cancel = threading.Event()
        self.title("db-desktop-backup · 数据库备份")
        self.geometry("1160x760")
        self.minsize(940, 640)
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=34)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        self.manager = TaskManager(self.execute, lambda *event: self.events.put(event))
        self.scheduler = Scheduler(self.manager.submit)
        self.build()
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.poll)
        self.after(1000, self.tick)
        self.after(300, self.startup)

    def build(self) -> None:
        main = ttk.Frame(self, padding=24)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="db-desktop-backup", style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, text="服务器 → 本地 · 指定 Database 完整替换 · 全局逐个执行 · 每库保留最新成功副本",
                  foreground="#62676d").pack(anchor="w", pady=(6, 18))
        settings = ttk.LabelFrame(main, text="程序设置", padding=12)
        settings.pack(fill="x")
        self.pg_bin = tk.StringVar(value=self.config.pg_bin)
        ttk.Label(settings, text="PostgreSQL 工具目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.pg_bin).grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Button(settings, text="选择目录", command=self.choose_bin).grid(row=0, column=2)
        self.auto_start = tk.BooleanVar(value=self.config.auto_start)
        ttk.Checkbutton(settings, text="下次打开程序时，自动依次备份已启用配置",
                        variable=self.auto_start).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(settings, text="保存设置", command=self.save_settings).grid(row=1, column=2, pady=(10, 0))
        settings.columnconfigure(1, weight=1)
        toolbar = ttk.Frame(main)
        toolbar.pack(fill="x", pady=16)
        for text, command in [
            ("添加配置", lambda: JobDialog(self)), ("编辑", self.edit), ("删除", self.delete),
            ("测试连接", self.test_connection), ("一键备份选中项", self.backup_selected),
            ("全部依次备份", self.backup_all), ("停止当前", self.manager.stop_current),
            ("取消选中排队", self.cancel_selected), ("停止全部", self.stop_all),
        ]:
            ttk.Button(toolbar, text=text, command=command).pack(side="left", padx=(0, 6))
        table_frame = ttk.Frame(main)
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_frame, columns=("name", "source", "target", "schedule", "success", "status"),
                                  show="headings", selectmode="browse")
        for key, label, width in [
            ("name", "任务", 140), ("source", "服务器源库", 200), ("target", "本地目标库", 170),
            ("schedule", "定时 / 启用", 110), ("success", "最近成功", 150), ("status", "状态", 210),
        ]:
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=70)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(12, 6))
        self.schedule_label = tk.StringVar(value="定时调度：运行中（仅本程序打开期间生效）")
        ttk.Label(bottom, textvariable=self.schedule_label).pack(side="left")
        ttk.Button(bottom, text="暂停 / 恢复定时", command=self.toggle_schedule).pack(side="right")
        ttk.Label(main, text="执行记录").pack(anchor="w", pady=(8, 4))
        self.log = tk.Text(main, height=9, wrap="word", state="disabled", relief="solid", borderwidth=1,
                           background="#f7f8fa", foreground="#24292f")
        self.log.pack(fill="x")

    def choose_bin(self) -> None:
        selected = filedialog.askdirectory(parent=self, initialdir=self.pg_bin.get() or None)
        if selected:
            self.pg_bin.set(selected)

    def save_settings(self) -> None:
        try:
            config = replace(self.config, pg_bin=self.pg_bin.get().strip(), auto_start=self.auto_start.get())
            Postgres(config.pg_bin).tool_major()
            self.store.save(config)
            self.config = config
            self.append_log("程序设置已保存；启动模式将在下次打开时生效")
        except Exception as error:
            messagebox.showerror("无法保存设置", str(error), parent=self)

    def selected(self) -> BackupJob | None:
        selection = self.table.selection()
        return next((job for job in self.config.jobs if selection and job.id == selection[0]), None)

    def editable(self) -> BackupJob | None:
        job = self.selected()
        if job is not None and self.manager.busy(job.id):
            messagebox.showinfo("任务正在执行或排队", "请先停止任务或取消排队，再修改配置", parent=self)
            return None
        return job

    def edit(self) -> None:
        job = self.editable()
        if job:
            JobDialog(self, job)

    def delete(self) -> None:
        job = self.editable()
        if job and messagebox.askyesno("删除配置", f"删除“{job.name}”的配置？本地数据库不会被删除。", parent=self):
            try:
                config = replace(self.config, jobs=[item for item in self.config.jobs if item.id != job.id])
                self.store.save(config)
                self.config = config
                self.refresh()
            except Exception as error:
                messagebox.showerror("无法删除配置", str(error), parent=self)

    def refresh(self) -> None:
        selection = self.table.selection()
        self.table.delete(*self.table.get_children())
        for job in self.config.jobs:
            last_success = "尚无成功记录"
            state_path = self.root_path / "state" / f"{job.id}.json"
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if state.get("last_success"):
                        last_success = datetime.fromisoformat(state["last_success"]).strftime("%m-%d %H:%M:%S")
                except (ValueError, OSError):
                    last_success = "记录读取失败"
            schedule = f"每 {job.interval_minutes} 分钟" if job.scheduled and job.enabled else "手动"
            if not job.enabled:
                schedule = "未启用 / 可手动"
            self.table.insert("", "end", iid=job.id, values=(
                job.name, f"{job.source.host}:{job.source.port}/{job.source.database}",
                f"{job.target.port}/{job.target.database}", schedule, last_success,
                self.statuses.get(job.id, "空闲"),
            ))
        if selection and self.table.exists(selection[0]):
            self.table.selection_set(selection)

    def append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        if int(self.log.index("end-1c").split(".")[0]) > 500:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def execute(self, job, cancel, progress) -> BackupResult:
        return BackupEngine(Postgres(self.config.pg_bin), self.root_path).run(job, cancel, progress)

    def backup_selected(self) -> None:
        job = self.selected()
        if job and not self.closing:
            if not self.manager.submit(job):
                self.append_log(f"{job.name} 已在执行或排队，未重复添加")

    def backup_all(self) -> None:
        if not self.closing:
            for job in self.config.jobs:
                if job.enabled:
                    self.manager.submit(job)

    def cancel_selected(self) -> None:
        job = self.selected()
        if job:
            self.manager.cancel_pending(job.id)

    def stop_all(self) -> None:
        self.manager.stop_all()
        self.schedule_paused = True
        self.update_schedule_label()
        self.append_log("已请求停止所有任务并暂停定时调度；可手动备份或恢复定时")

    def toggle_schedule(self) -> None:
        self.schedule_paused = not self.schedule_paused
        self.scheduler = Scheduler(self.manager.submit)
        self.update_schedule_label()

    def update_schedule_label(self) -> None:
        self.schedule_label.set("定时调度：已暂停" if self.schedule_paused else
                                "定时调度：运行中（仅本程序打开期间生效）")

    def test_connection(self) -> None:
        job = self.selected()
        if job is None or self.closing:
            return
        if self.test_thread is not None and self.test_thread.is_alive():
            self.append_log("已有连接测试正在执行，请等待完成")
            return
        self.test_cancel = threading.Event()
        pg = Postgres(self.config.pg_bin)
        self.append_log(f"{job.name}：正在测试连接")

        def test():
            try:
                major = pg.tool_major()
                source = pg.info(job.source, self.test_cancel)
                target = pg.info(pg.maintenance(job.target), self.test_cancel)
                self.events.put((job.id, "test", f"连接成功：服务器 PG {source['version'] // 10000}，"
                                 f"本地 PG {target['version'] // 10000}，备份工具 {major}；"
                                 f"本地建库权限：{'有' if target['can_create'] else '无'}"))
            except Exception as error:
                self.events.put((job.id, "test", f"连接测试失败：{error}"))
        self.test_thread = threading.Thread(target=test, name="连接测试", daemon=False)
        self.test_thread.start()

    def startup(self) -> None:
        pending = list((self.root_path / "working").glob("*/journal.json"))
        if pending:
            self.append_log(f"发现 {len(pending)} 份未清理的执行记录，请查看 data/working；程序不会自动删除不确定的数据库")
            for path in pending:
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    phase = record.get("phase", "未知")
                    if phase in {"checking", "dumping"} and record.get("stage_oid") is None:
                        description = "按记录尚未创建本地临时库，可能留下未完成的导出文件"
                    else:
                        description = "请核实临时库和目标库状态，未自动清理"
                    self.append_log(f"未完成记录：目标={record.get('target', '未知')}，阶段={phase}；"
                                    f"{description}；记录={path}")
                except (ValueError, OSError) as error:
                    self.append_log(f"无法读取未完成记录 {path}：{error}")
        if self.config.auto_start:
            self.backup_all()

    def tick(self) -> None:
        if self.closing:
            return
        if not self.schedule_paused:
            self.scheduler.tick(self.config.jobs)
        self.after(1000, self.tick)

    def poll(self) -> None:
        while True:
            try:
                job_id, state, detail = self.events.get_nowait()
            except queue.Empty:
                break
            job = next((item for item in self.config.jobs if item.id == job_id), None)
            name = job.name if job else job_id
            if isinstance(detail, BackupResult):
                message = detail.message
                for warning in detail.warnings:
                    self.append_log(f"{name}：{warning}")
            else:
                message = str(detail)
            if state != "test":
                self.statuses[job_id] = message
            self.append_log(f"{name}：{message}")
            self.refresh()
        test_alive = self.test_thread is not None and self.test_thread.is_alive()
        if self.closing and not self.manager.alive and not test_alive:
            self.destroy()
            return
        self.after(100, self.poll)

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.test_cancel.set()
        self.manager.close()
        self.title("正在停止任务并清理，请稍候…")
        self.append_log("正在关闭：停止当前任务、取消等待任务并完成清理")


def main() -> None:
    parser = argparse.ArgumentParser(prog="db-desktop-backup",
                                     description="数据库桌面备份程序，目前支持 PostgreSQL")
    parser.add_argument("--data-dir", type=Path,
                        default=Path(__file__).resolve().parent.parent / "data")
    args = parser.parse_args()
    lock = None
    try:
        lock = InstanceLock(args.data_dir)
        store = ConfigStore(args.data_dir)
        config = store.load()
        app = Application(args.data_dir, store, config)
        app.mainloop()
    except Exception as error:
        error_root = tk.Tk()
        error_root.withdraw()
        messagebox.showerror("程序启动失败", str(error), parent=error_root)
        error_root.destroy()
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    main()
