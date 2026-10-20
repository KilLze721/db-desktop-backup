"""配置、执行记录及 Windows 密码加密。"""

import base64
import ctypes
from ctypes import wintypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

from .models import AppConfig, BackupJob, Connection, discover_pg_bin


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("当前版本使用 Windows 加密，只支持 Windows")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if decrypt:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1,
                                        ctypes.byref(target))
    else:
        ok = crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 1,
                                      ctypes.byref(target))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def protect_password(password: str) -> str:
    return base64.b64encode(_crypt(password.encode("utf-8"))).decode("ascii") if password else ""


def unprotect_password(password: str) -> str:
    return _crypt(base64.b64decode(password), decrypt=True).decode("utf-8") if password else ""


def write_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".write-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump(content, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ConfigStore:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "config.json"

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig(pg_bin=discover_pg_bin())
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("version") != 1:
            raise ValueError("配置文件版本不受支持")
        jobs = []
        for item in raw.get("jobs", []):
            item = dict(item)
            for side in ("source", "target"):
                connection = dict(item[side])
                connection["password"] = unprotect_password(connection.get("password", ""))
                item[side] = Connection(**connection)
            jobs.append(BackupJob(**item))
        config = AppConfig(pg_bin=raw.get("pg_bin", ""), auto_start=raw.get("auto_start", False),
                           jobs=jobs)
        config.validate()
        return config

    def save(self, config: AppConfig) -> None:
        config.validate()
        raw = asdict(config)
        raw["version"] = 1
        for job in raw["jobs"]:
            for side in ("source", "target"):
                job[side]["password"] = protect_password(job[side]["password"])
        write_json(self.path, raw)
