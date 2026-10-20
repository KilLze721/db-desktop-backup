"""同一配置目录只允许一个程序实例执行任务。"""

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.file = (root / "app.lock").open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close()
                raise RuntimeError("程序已经运行，请使用已打开的窗口") from None
        else:
            import fcntl
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                raise RuntimeError("程序已经运行") from None

    def close(self) -> None:
        self.file.close()
