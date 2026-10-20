"""桌面窗口、配置表单和正常退出的基本验证。"""

import time

from dbbackup.models import AppConfig, discover_pg_bin
from dbbackup.storage import ConfigStore
from dbbackup.ui import Application, JobDialog


def test_configuration_dialog_and_clean_shutdown(tmp_path, monkeypatch):
    def unexpected_error(title, message, **kwargs):
        raise AssertionError(f"{title}：{message}")

    monkeypatch.setattr("dbbackup.ui.messagebox.showerror", unexpected_error)
    store = ConfigStore(tmp_path)
    app = Application(tmp_path, store, AppConfig(pg_bin=discover_pg_bin()))
    app.withdraw()
    try:
        app.update_idletasks()
        dialog = JobDialog(app)
        dialog.withdraw()
        dialog.name.set("界面测试配置")
        dialog.fields["source"]["host"].set("remote.example")
        dialog.fields["source"]["database"].set("source")
        dialog.fields["target"]["database"].set("backup")
        dialog.save()
        assert len(app.config.jobs) == 1
        assert len(app.table.get_children()) == 1
        assert store.load().jobs[0].name == "界面测试配置"
        assert app.config.auto_start is False
        assert app.config.jobs[0].scheduled is False
    finally:
        app.close()
        app.manager.join(3)
        # 等待窗口自身处理退出，避免后台工作线程在窗口关闭后残留。
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                app.update()
                if not app.winfo_exists():
                    break
            except Exception:
                break
            time.sleep(0.02)
    assert not app.manager.alive
