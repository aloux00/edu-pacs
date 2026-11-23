# -*- coding: utf-8 -*-
"""EduPACS 备份与恢复核心库（CLI 与网页管理界面共用）

设计要点
--------
备份范围（数据，不含代码——代码用部署包还原）：
  1. app/data/app.db        —— SQLite 在线备份 API 快照（教学系统运行中可安全快照）
  2. app/data/secret.key    —— 登录 token 签名密钥（恢复后旧 token 仍有效）
  3. orthanc/orthanc.json   —— 含动态维护的远程 PACS 节点（Modalities/Peers）与服务账号
  4. orthanc/storage/index* —— Orthanc 索引库快照
  5. orthanc/storage/**     —— 全部 DICOM 影像文件

快照一致性（Orthanc 官方文档要求：Orthanc 假定自己是 SQLite 唯一访问者）：
  - 停机模式（默认，Windows 原生部署）：短暂停止本项目 Orthanc 进程 → 拷贝 → 自动重启；
    Docker / apt 部署无法从应用层停 Orthanc → 自动降级为热备份模式
  - 热备份模式：拷贝 index+index-wal → 在副本上重放 WAL(checkpoint) → 完整性校验
    (quick_check) + 实例集合与存储文件交叉核对；不一致自动重试 3 次，仍失败则中止
  - 任何模式下都做"索引实例集合 ⊆ 已拷贝文件"交叉校验，失败即放弃本次备份

异机 / 自定义端口部署（环境变量，全部可选）：
  EDUPACS_ORTHANC_STORAGE  指向 Orthanc 影像/索引目录（异机时用 NFS/共享挂载点）
  EDUPACS_APP_DATA         指向 app.db / secret.key 所在目录
  EDUPACS_ORTHANC_DIR      指向 Orthanc 可执行与配置目录（仅本机编排时需要）
  EDUPACS_ORTHANC_PORT     Orthanc HTTP 端口（默认 8042）
  EDUPACS_APP_PORT         教学系统端口（默认 8000）
  异机时本机无 Orthanc 二进制 → 自动降级热备份；影像须经共享目录对本机可见。

加密：AES-256-GCM 流式加密整个 tar.gz（GCM 自带完整性校验），密钥 PBKDF2-HMAC-SHA256
      600,000 轮派生；包头为明文元数据（创建时间/主机/备注/计数），并以 AAD 方式纳入
      GCM 认证域（包头被篡改 = 解密必失败）；包头含 kc=sha256(key)[:16] 用于快速
      判定密码错误。

归档格式 .edubak：
  [6B "EDUBAK"][1B 版本][2B 头长度 LE][头 JSON UTF-8]
  [密文流 = AES-256-GCM(明文流 = manifest.json + "\\n" + tar.gz)]
  [16B GCM tag]
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    CRYPTO_OK = True
except Exception:                                            # pragma: no cover
    CRYPTO_OK = False

# ---------------------------------------------------------------- 常量与路径
ROOT = Path(__file__).resolve().parents[2]                    # edu-pacs/

def _env_path(name: str, default: Path) -> Path:
    """允许异机部署/自定义布局时用环境变量覆盖数据目录"""
    v = os.environ.get(name, "").strip()
    return Path(v) if v else default

APP_DATA = _env_path("EDUPACS_APP_DATA", ROOT / "app" / "data")
ORTHANC_DIR = _env_path("EDUPACS_ORTHANC_DIR", ROOT / "orthanc")
STORAGE_DIR = _env_path("EDUPACS_ORTHANC_STORAGE", ORTHANC_DIR / "storage")
ORTHANC_JSON = _env_path("EDUPACS_ORTHANC_JSON", ORTHANC_DIR / "orthanc.json")
ORTHANC_PORT = int(os.environ.get("EDUPACS_ORTHANC_PORT", "8042") or 8042)
APP_PORT = int(os.environ.get("EDUPACS_APP_PORT", "8000") or 8000)
CONFIG_PATH = ROOT / "config" / "backup.json"
STATE_PATH = APP_DATA / "backup_state.json"

MAGIC = b"EDUBAK"
FORMAT_VERSION = 1
KDF_ITER = 600_000
PW_MIN_LEN = 8
ARCHIVE_SUFFIX = ".edubak"
CHUNK = 1024 * 1024
IS_WIN = sys.platform == "win32"

ProgressCb = Callable[[str, Optional[float], str], None]

DEFAULT_CONFIG = {
    "version": 1,
    "destinations": [
        {"id": "local", "label": "本机备份目录", "path": "backups", "keep": 7, "enabled": True},
        {"id": "offsite", "label": "异机备份（网络路径，请改成真实 UNC/NFS 后启用）",
         "path": "\\\\FILESERVER\\EduPACS备份", "keep": 14, "enabled": False},
    ],
}


class BackupError(Exception):
    """备份/恢复流程中可预期的错误（面向用户展示）"""


# ---------------------------------------------------------------- 配置
def load_config() -> dict:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = None
        if isinstance(cfg, dict) and isinstance(cfg.get("destinations"), list) and cfg["destinations"]:
            for d in cfg["destinations"]:
                d.setdefault("keep", 7)
                d.setdefault("enabled", False)
                d.setdefault("label", d.get("id", ""))
            return cfg
    save_config(DEFAULT_CONFIG)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_dest(cfg: dict, dest_id: str) -> Optional[dict]:
    for d in cfg.get("destinations", []):
        if d.get("id") == dest_id:
            return d
    return None


def resolve_dest_path(dest_cfg: dict) -> Path:
    p = str(dest_cfg.get("path", "")).strip()
    if not p:
        raise BackupError(f"目的地 {dest_cfg.get('id')} 未配置路径")
    path = Path(p)
    if not path.is_absolute():
        path = ROOT / path
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BackupError(f"备份目的地不可写：{path}（{e}）")
    return path


# ---------------------------------------------------------------- 归档清单
def _archive_stem() -> str:
    host = re.sub(r"[^A-Za-z0-9-]+", "-", socket.gethostname()).strip("-") or "host"
    return f"edupacs-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{host}"


def list_archives(dest_path: Path) -> list[dict]:
    out = []
    if not dest_path.exists():
        return out
    for f in sorted(dest_path.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not f.is_file() or f.suffix != ARCHIVE_SUFFIX:
            continue
        m = re.match(r"edupacs-backup-(\d{8}-\d{6})-(.+)$", f.stem)
        meta = {}
        if m:
            ts = m.group(1)
            meta = {"created": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}",
                    "host": m.group(2)}
        try:
            head = read_header(f)
            meta["note"] = head.get("note", "")
            meta["counts"] = head.get("counts", {})
        except Exception:
            meta.setdefault("note", "")
        st = f.stat()
        out.append({"name": f.name, "path": str(f), "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    **meta})
    return out


def prune_dest(dest_path: Path, keep: int) -> list[str]:
    archives = [f for f in dest_path.glob("*" + ARCHIVE_SUFFIX) if f.is_file()]
    archives.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    removed = []
    for f in archives[max(0, int(keep)):]:
        f.unlink()
        removed.append(f.name)
    return removed


# ---------------------------------------------------------------- 加密原语
def _derive_key(password: str, salt: bytes, iterations: int = KDF_ITER) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.b64decode(s)


def write_header_payload(out_path: Path, header: dict, plaintext_path: Path,
                         key: bytes, iv: bytes) -> None:
    """按 .edubak 格式写：明文头(AAD) + AES-256-GCM 流密文 + 16B tag"""
    header_bytes = json.dumps(header, ensure_ascii=False).encode("utf-8")
    blob = MAGIC + bytes([FORMAT_VERSION]) + len(header_bytes).to_bytes(2, "little") + header_bytes
    enc = Cipher(algorithms.AES(key), modes.GCM(iv)).encryptor()
    enc.authenticate_additional_data(blob)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    with open(tmp, "wb") as fo, open(plaintext_path, "rb") as fi:
        fo.write(blob)
        while True:
            chunk = fi.read(CHUNK)
            if not chunk:
                break
            fo.write(enc.update(chunk))
        fo.write(enc.finalize())
        fo.write(enc.tag)
    os.replace(tmp, out_path)


def read_header(path: Path) -> dict:
    with open(path, "rb") as f:
        head = f.read(9)
    if len(head) < 9 or head[:6] != MAGIC:
        raise BackupError("不是有效的 EduPACS 备份归档（魔数不匹配）")
    if head[6] != FORMAT_VERSION:
        raise BackupError(f"不支持的归档版本 {head[6]}（本工具支持版本 {FORMAT_VERSION}）")
    hlen = int.from_bytes(head[7:9], "little")
    with open(path, "rb") as f:
        f.seek(9)
        header = json.loads(f.read(hlen).decode("utf-8"))
    return header


def open_decryptor(path: Path, password: str):
    """返回 (decryptor, header, 密文)；密码错误立即抛 BackupError。"""
    header = read_header(path)
    kdf = header.get("kdf", {})
    salt = _b64d(kdf.get("salt", ""))
    iterations = int(kdf.get("iter", KDF_ITER))
    key = _derive_key(password, salt, iterations)
    kc = hashlib.sha256(key).hexdigest()[:16]
    if header.get("kc") and kc != header["kc"]:
        raise BackupError("备份密码错误")
    iv = _b64d(header["iv"])
    with open(path, "rb") as f:
        head = f.read(9)
        hlen = int.from_bytes(head[7:9], "little")
        aad = head + f.read(hlen)          # AAD 覆盖整个明文头（9+hlen 字节）
        rest = f.read()                    # 密文流 + 尾部 16B tag
    tag = rest[-16:]
    dec = Cipher(algorithms.AES(key), modes.GCM(iv, tag)).decryptor()
    dec.authenticate_additional_data(aad)
    return dec, header, rest[:-16]


def decrypt_to_file(path: Path, password: str, dest: Path) -> dict:
    """解密归档到 tar.gz 文件，返回头信息；任何篡改/密码错都会抛异常。"""
    dec, header, cipher_blob = open_decryptor(path, password)
    tmp = dest.with_suffix(".part")
    try:
        with open(tmp, "wb") as fo:
            for i in range(0, len(cipher_blob), CHUNK):
                fo.write(dec.update(cipher_blob[i:i + CHUNK]))
            fo.write(dec.finalize())          # GCM tag 校验；篡改/错密码在此抛异常
    except Exception:
        # Windows 下文件句柄未关闭时 unlink 会抛 PermissionError，
        # 因此清理必须放在 with 块之外，否则会掩盖真正的校验错误。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(sys.exc_info()[1], BackupError):
            raise
        raise BackupError("归档校验失败（数据被篡改或密码错误）")
    os.replace(tmp, dest)
    return header


# ---------------------------------------------------------------- 进程编排
def orthanc_running() -> bool:
    """Orthanc HTTP 端口探测（端口可用 EDUPACS_ORTHANC_PORT 覆盖）"""
    try:
        with socket.create_connection(("127.0.0.1", ORTHANC_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def app_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", APP_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def can_orchestrate() -> bool:
    """是否具备停/启本项目 Orthanc 的能力（Windows 原生部署布局）"""
    if IS_WIN:
        return (ORTHANC_DIR / "Orthanc.exe").is_file()
    return (ORTHANC_DIR / "orthanc").is_file() or (ORTHANC_DIR / "Orthanc").is_file()


def _orthanc_pids() -> list[int]:
    """枚举可证明属于本项目 orthanc 目录的进程 PID（绝不动其它 Orthanc）"""
    pids: list[int] = []
    marker = str(ORTHANC_DIR.resolve()).lower()
    if IS_WIN:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='Orthanc.exe'\" | "
                 "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=20).stdout.strip()
            if out:
                rows = json.loads(out)
                if isinstance(rows, dict):
                    rows = [rows]
                for r in rows:
                    exe = str(r.get("ExecutablePath") or "")
                    if exe and marker in exe.lower():
                        pids.append(int(r["ProcessId"]))
        except Exception:
            pass
    else:
        for pid in [p for p in os.listdir("/proc") if p.isdigit()]:
            try:
                cmd = (Path("/proc") / pid / "cmdline").read_bytes().decode("utf-8", "ignore")
                if marker in cmd.lower() and "orthanc" in cmd.lower():
                    pids.append(int(pid))
            except Exception:
                continue
    return pids


def stop_orthanc(timeout: int = 20) -> bool:
    """硬停本项目 Orthanc（仅限路径可证明属于本项目的进程）；返回是否确有停止动作"""
    pids = _orthanc_pids()
    if not pids:
        return False
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", *[str(p) for p in pids], "/T", "/F"],
                       capture_output=True, timeout=timeout)
    else:
        for pid in pids:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
    deadline = time.time() + timeout
    while time.time() < deadline and orthanc_running():
        time.sleep(0.5)
    if orthanc_running():
        raise BackupError("Orthanc 停止超时，请手动停止后重试")
    return True


def _orthanc_http_auth() -> tuple:
    """读 orthanc.json 的服务账号（用于 /tools/shutdown 优雅关停）"""
    try:
        import urllib.request
        cfg = json.loads(ORTHANC_JSON.read_text(encoding="utf-8"))
        users = cfg.get("RegisteredUsers", {})
        u, pw = next(iter(users.items()))
        return u, pw
    except Exception:
        return None


def stop_orthanc_clean(timeout: int = 30) -> bool:
    """优雅停止：POST /tools/shutdown —— SQLite 随进程干净退出自动合并 WAL 并删除，
    主库文件即为自洽快照（优于 taskkill 硬杀后手工重放 WAL 的各种边界情况）。
    失败时回退硬停。返回是否确有停止动作。"""
    if not orthanc_running():
        return False
    auth = _orthanc_http_auth()
    if auth:
        try:
            import urllib.request
            import base64
            req = urllib.request.Request(
                f"http://127.0.0.1:{ORTHANC_PORT}/tools/shutdown", method="POST")
            req.add_header("Authorization", "Basic " + base64.b64encode(
                f"{auth[0]}:{auth[1]}".encode()).decode())
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass  # HTTP 失败 → 回退硬停
    deadline = time.time() + timeout
    while time.time() < deadline and orthanc_running():
        time.sleep(0.5)
    if orthanc_running():
        return stop_orthanc(timeout)
    return True


def start_orthanc() -> None:
    exe = ORTHANC_DIR / ("Orthanc.exe" if IS_WIN else "orthanc")
    if not exe.is_file():
        raise BackupError(f"未找到 Orthanc 可执行文件：{exe}")
    flags = 0
    if IS_WIN:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen([str(exe), str(ORTHANC_JSON)], cwd=str(ORTHANC_DIR),
                     creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 60
    while time.time() < deadline:
        if orthanc_running():
            return
        time.sleep(1.0)
    raise BackupError("Orthanc 重启超时：请手动启动（scripts\\start_all.ps1）并检查 orthanc\\logs")


# ---------------------------------------------------------------- 快照
def snapshot_sqlite(src: Path, dst: Path) -> int:
    """SQLite 在线备份 API 快照（源库运行中可安全调用）"""
    s = sqlite3.connect(str(src), timeout=15)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
        d.close()
        s.close()
        return dst.stat().st_size
    except Exception:
        try:
            d.close(); s.close()
        except Exception:
            pass
        raise


def _wal_checkpoint_copy(index_src: Path, dst: Path) -> None:
    """拷贝 index(+wal) 后在副本上重放 WAL，产出单文件一致性快照"""
    shutil.copy2(index_src, dst)
    wal = index_src.parent / "index-wal"
    if wal.exists():
        shutil.copy2(wal, dst.parent / "index-wal")
    con = sqlite3.connect(str(dst))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        ok = con.execute("PRAGMA quick_check").fetchone()[0]
        if ok != "ok":
            raise BackupError(f"索引快照完整性校验失败：{ok}")
    finally:
        con.close()
    (dst.parent / "index-wal").unlink(missing_ok=True)
    (dst.parent / "index-shm").unlink(missing_ok=True)


def _index_levels(con) -> dict:
    """Orthanc 索引库 resourceType 层级自适应（库内存储为 枚举值+1：Patient=1…Instance=4，
    以 MAX(resourceType) 为实例层，兼容未来层级变化）"""
    mx = con.execute("SELECT MAX(resourceType) FROM Resources").fetchone()[0]
    mx = mx if mx is not None else 4
    return {"instance": mx, "series": mx - 1, "study": mx - 2, "patient": mx - 3}


def _index_stats(db_path: Path) -> dict:
    con = sqlite3.connect(str(db_path))
    try:
        lv = _index_levels(con)
        return {
            "instances": con.execute(
                "SELECT COUNT(*) FROM Resources WHERE resourceType=?", (lv["instance"],)).fetchone()[0],
            "series": con.execute(
                "SELECT COUNT(*) FROM Resources WHERE resourceType=?", (lv["series"],)).fetchone()[0],
            "studies": con.execute(
                "SELECT COUNT(*) FROM Resources WHERE resourceType=?", (lv["study"],)).fetchone()[0],
        }
    finally:
        con.close()


def _index_storage_file_ids(db_path: Path) -> set[str]:
    """索引中登记的存储文件名（AttachedFiles.uuid，与磁盘上两级扇出文件名一致）"""
    con = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in con.execute("SELECT uuid FROM AttachedFiles")}
    finally:
        con.close()


def _collect_storage_files() -> list[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(STORAGE_DIR):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.startswith("index"):
                continue
            files.append(Path(dirpath) / fn)
    return files


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 备份
def create_backup(dest_cfg: dict, password: str, note: str = "",
                  mode: str = "auto", progress: ProgressCb = lambda *a: None,
                  cancel: Callable[[], bool] = lambda: False) -> dict:
    """执行一次完整备份。返回结果 dict（写入状态文件）。

    mode: auto（默认：能编排则停机模式，否则热备份）/ hot（强制热备份）
    """
    if not CRYPTO_OK:
        raise BackupError("加密库不可用：请先 pip install cryptography（见 requirements.lock.txt）")
    if not password or len(password) < PW_MIN_LEN:
        raise BackupError(f"备份密码至少 {PW_MIN_LEN} 位")
    note = (note or "").strip()[:200]
    dest_path = resolve_dest_path(dest_cfg)
    t0 = time.time()
    tmpdir = ROOT / ".temp" / ("backup-" + datetime.now().strftime("%Y%m%d%H%M%S"))
    shutil.rmtree(tmpdir, ignore_errors=True)
    tmpdir.mkdir(parents=True, exist_ok=True)
    stopped = False
    try:
        # ---- 阶段 1：app.db / secret.key 快照（教学系统不停机） ----
        progress("snapshot", 5, "快照教学系统数据库（在线备份 API）")
        appdb_path = tmpdir / "app.db"
        snapshot_sqlite(APP_DATA / "app.db", appdb_path)
        secret_path = APP_DATA / "secret.key"
        has_secret = secret_path.exists()
        if has_secret:
            shutil.copy2(secret_path, tmpdir / "secret.key")

        # ---- 阶段 2：Orthanc 索引 + 存储快照 ----
        running = orthanc_running()
        hot = mode == "hot" or (running and not can_orchestrate())
        if running and not hot:
            progress("orthanc", 10, "优雅停止本项目 Orthanc（WAL 自动合并，官方推荐一致性窗口）")
            stop_orthanc_clean()
            stopped = True
        if hot and running:
            progress("orthanc", 10, "热备份模式：拷贝索引并重放 WAL（带完整性校验与重试）")

        idx_snapshot = tmpdir / "index"
        attempts = 3 if (hot and running) else 1
        for attempt in range(attempts):
            try:
                _wal_checkpoint_copy(STORAGE_DIR / "index", idx_snapshot)
                break
            except Exception as e:
                if attempt == attempts - 1:
                    raise BackupError(f"Orthanc 索引快照失败（重试 {attempts} 次后放弃）：{e}")
                progress("orthanc", 10, f"快照不一致，重试（{attempt + 2}/{attempts}）……")
                time.sleep(1.0)

        stats = _index_stats(idx_snapshot)
        file_ids = _index_storage_file_ids(idx_snapshot)

        # ---- 阶段 3：拷贝存储文件（并交叉校验） ----
        progress("storage", 30, "拷贝 DICOM 影像存储……")
        files = _collect_storage_files()
        def _instance_file(iid: str) -> Path:
            # Orthanc 文件存储两级扇出：<uuid[0:2]>/<uuid[2:4]>/<uuid>
            return STORAGE_DIR / iid[:2] / iid[2:4] / iid
        missing = [i for i in file_ids if not _instance_file(i).is_file()]
        if missing:
            raise BackupError(
                f"一致性校验失败：索引登记的 {len(missing)} 个存储文件在磁盘上缺失"
                "（备份窗口内发生了删除，请稍后重试）")
        disk_ids = {f.name for f in files}
        orphans = sorted(disk_ids - file_ids)
        storage_dir_tmp = tmpdir / "storage"
        storage_dir_tmp.mkdir()
        total = len(files)
        copied_bytes = 0
        for i, f in enumerate(files):
            if cancel():
                raise BackupError("已取消")
            rel = f.relative_to(STORAGE_DIR)
            target = storage_dir_tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            copied_bytes += f.stat().st_size
            if i % 25 == 0 or i == total - 1:
                progress("storage", 30 + 40 * (i + 1) / max(total, 1),
                         f"拷贝影像 {i + 1}/{total}")
        if stopped:
            progress("orthanc", 72, "重启本项目 Orthanc……")
            try:
                start_orthanc()
            finally:
                stopped = False

        # ---- 阶段 4：打包 tar.gz ----
        progress("pack", 75, "打包并压缩（gzip）……")
        manifest = {
            "v": 1,
            "tool": "edupacs-backup/1",
            "created": datetime.now().isoformat(timespec="seconds"),
            "host": socket.gethostname(),
            "note": note,
            "mode": "hot" if hot else "stopped",
            "sections": {
                "app_db": {"size": appdb_path.stat().st_size, "sha256": _sha256_file(appdb_path)},
                "secret_key": ({"present": True, "sha256": _sha256_file(tmpdir / "secret.key")}
                               if has_secret else {"present": False}),
                "orthanc_json": ({"sha256": _sha256_file(ORTHANC_JSON)}
                                 if ORTHANC_JSON.exists() else {"present": False}),
                "orthanc_index": {"size": idx_snapshot.stat().st_size,
                                  "sha256": _sha256_file(idx_snapshot), **stats},
                "storage": {"files": len(files), "bytes": copied_bytes, "orphans": len(orphans)},
            },
        }
        tar_path = tmpdir / "payload.tar.gz"
        with tarfile.open(tar_path, "w:gz", compresslevel=6) as tf:
            info = tarfile.TarInfo("manifest.json")
            data = (json.dumps(manifest, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
            info.size = len(data)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(data))
            tf.add(appdb_path, arcname="app/data/app.db")
            if has_secret:
                tf.add(tmpdir / "secret.key", arcname="app/data/secret.key")
            if ORTHANC_JSON.exists():
                tf.add(ORTHANC_JSON, arcname="orthanc/orthanc.json")
            tf.add(idx_snapshot, arcname="orthanc/storage/index")
            for f in files:
                tf.add(f, arcname="orthanc/storage/" + str(f.relative_to(STORAGE_DIR)).replace("\\", "/"))

        # ---- 阶段 5：AES-256-GCM 加密落盘 ----
        progress("encrypt", 88, "AES-256-GCM 加密……")
        salt = os.urandom(16)
        iv = os.urandom(12)
        key = _derive_key(password, salt)
        header = {
            "v": FORMAT_VERSION,
            "cipher": "AES-256-GCM",
            "kdf": {"name": "PBKDF2-SHA256", "iter": KDF_ITER, "salt": _b64e(salt)},
            "iv": _b64e(iv),
            "kc": hashlib.sha256(key).hexdigest()[:16],
            "created": manifest["created"],
            "host": manifest["host"],
            "note": note,
            "counts": {"instances": stats["instances"], "studies": stats["studies"],
                       "storage_files": len(files), "app_db_bytes": appdb_path.stat().st_size},
        }
        name = _archive_stem()
        final = dest_path / (name + ARCHIVE_SUFFIX)
        k = 1
        while final.exists():
            final = dest_path / f"{name}-{k}{ARCHIVE_SUFFIX}"
            k += 1
        write_header_payload(final, header, tar_path, key, iv)

        # ---- 阶段 6：写后校验（解密 + GCM tag + 结构） ----
        progress("verify", 94, "写后校验（解密回读 + 完整性）……")
        rep = verify_archive(final, password, progress=lambda s, p, m: progress("verify", 94, m))
        if not rep.get("ok"):
            final.unlink(missing_ok=True)
            raise BackupError(f"写后校验失败，已丢弃本次归档：{rep.get('error')}")

        # ---- 阶段 7：保留策略 + 状态 ----
        removed = prune_dest(dest_path, int(dest_cfg.get("keep", 7)))
        result = {
            "ok": True,
            "file": final.name,
            "path": str(final),
            "size": final.stat().st_size,
            "duration_s": round(time.time() - t0, 1),
            "mode": manifest["mode"],
            "counts": header["counts"],
            "pruned": removed,
        }
        try:
            STATE_PATH.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        progress("done", 100, f"备份完成：{final.name}（{final.stat().st_size // 1024} KB）")
        return result
    finally:
        if stopped:  # 异常路径也要尽力把 Orthanc 拉回来
            try:
                start_orthanc()
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- 校验 / 恢复
def _tmp_workspace() -> Path:
    d = ROOT / ".temp" / ("verify-" + datetime.now().strftime("%Y%m%d%H%M%S%f"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def verify_archive(path: Path, password: str,
                   progress: ProgressCb = lambda *a: None) -> dict:
    """完整校验：密码 → GCM 解密 → tar 结构 → 各分区 sha256 与 manifest 一致"""
    if not CRYPTO_OK:
        raise BackupError("加密库不可用：请先安装 cryptography")
    if not path.is_file():
        return {"ok": False, "error": f"归档不存在：{path}"}
    work = _tmp_workspace()
    try:
        progress("verify", 10, "读取归档头……")
        tar_path = work / "payload.tar.gz"
        header = decrypt_to_file(path, password, tar_path)
        progress("verify", 40, "GCM 校验通过，解压检查结构……")
        manifest = {}
        sections = {}
        got_storage = 0
        with tarfile.open(tar_path, "r:gz") as tf:
            for m in tf.getmembers():
                if m.name == "manifest.json":
                    manifest = json.loads(tf.extractfile(m).read().decode("utf-8"))
                elif m.name in ("app/data/app.db", "app/data/secret.key",
                                "orthanc/orthanc.json", "orthanc/storage/index"):
                    sections[m.name] = m
                elif m.name.startswith("orthanc/storage/"):
                    got_storage += 1
        if not manifest:
            raise BackupError("归档内缺少 manifest.json")
        sec = manifest.get("sections", {})
        checks = {}
        # 分区 sha 复核
        def _check(member, skey):
            with tarfile.open(tar_path, "r:gz") as tfx:
                mf = tfx.getmember(member)
                h = hashlib.sha256()
                with tfx.extractfile(mf) as ef:
                    for chunk in iter(lambda: ef.read(CHUNK), b""):
                        h.update(chunk)
                checks[skey] = (h.hexdigest() == sec.get(skey, {}).get("sha256"))
        if "app/data/app.db" in sections:
            _check("app/data/app.db", "app_db")
        if "app/data/secret.key" in sections:
            _check("app/data/secret.key", "secret_key")
        if "orthanc/orthanc.json" in sections:
            _check("orthanc/orthanc.json", "orthanc_json")
        if "orthanc/storage/index" in sections:
            _check("orthanc/storage/index", "orthanc_index")
        storage_ok = got_storage == sec.get("storage", {}).get("files", -1)
        ok = all(checks.values()) and storage_ok
        progress("verify", 95, "校验完成")
        return {
            "ok": ok,
            "header": header,
            "manifest": manifest,
            "checks": {**checks, "storage_files": storage_ok},
            "error": None if ok else "分区哈希或文件数与清单不一致",
        }
    except BackupError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"校验异常：{type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def restore_archive(path: Path, password: str, apply: bool = False,
                    root: Optional[Path] = None,
                    progress: ProgressCb = lambda *a: None) -> dict:
    """恢复归档。apply=False 为干跑分析；apply=True 实际写入 root（默认本项目根）。

    安全策略：
      - 只允许恢复到本项目根（或显式 --root 沙箱目录）
      - 恢复真实根时要求两个服务均已停止
      - 覆盖前把现有 app/data、orthanc/storage、orthanc/orthanc.json 移入
        .temp/pre-restore-<ts>/ 作为回滚保障
    """
    root = root or ROOT
    rep = verify_archive(path, password, progress=progress)
    if not rep.get("ok"):
        return {"ok": False, "stage": "verify", "error": rep.get("error")}
    manifest = rep["manifest"]
    header = rep["header"]

    # 提取到暂存区
    work = _tmp_workspace()
    try:
        tar_path = work / "payload.tar.gz"
        decrypt_to_file(path, password, tar_path)
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(work / "extract", filter="data")
        ex = work / "extract"
        planned = {
            "app/data/app.db": (ex / "app/data/app.db").is_file(),
            "app/data/secret.key": (ex / "app/data/secret.key").is_file(),
            "orthanc/orthanc.json": (ex / "orthanc/orthanc.json").is_file(),
            "orthanc/storage/index": (ex / "orthanc/storage/index").is_file(),
        }
        storage_count = sum(1 for p in (ex / "orthanc/storage").rglob("*")
                             if p.is_file() and p.name != "index")

        report = {
            "ok": True, "applied": False,
            "header": header, "manifest": manifest, "planned": planned,
            "storage_files": storage_count,
            "warnings": [],
        }

        if not apply:
            report["commands"] = _restore_commands(path)
            progress("done", 100, "干跑分析完成（未写入任何文件）")
            return report

        if root.resolve() == ROOT.resolve():
            if orthanc_running() or app_running():
                return {"ok": False, "stage": "precheck",
                        "error": f"检测到服务正在运行（{APP_PORT}/{ORTHANC_PORT}）。恢复前请先停止："
                                 "scripts\\stop_all.ps1 或 EduPACS启动器.exe --stop"}

        # ---- 实际恢复 ----
        progress("apply", 30, "现有数据移入回滚保障目录……")
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safeguard = root / ".temp" / f"pre-restore-{ts}"
        safeguard.mkdir(parents=True, exist_ok=True)
        for src, name in ((root / "app" / "data", "app-data"),
                          (root / "orthanc" / "storage", "orthanc-storage"),
                          (root / "orthanc" / "orthanc.json", "orthanc.json")):
            if src.exists():
                dst = safeguard / name
                if src.is_dir():
                    shutil.move(str(src), str(dst))
                else:
                    shutil.move(str(src), str(dst))

        progress("apply", 60, "写入恢复数据……")
        try:
            (root / "app" / "data").mkdir(parents=True, exist_ok=True)
            (root / "orthanc" / "storage").mkdir(parents=True, exist_ok=True)
            if (ex / "app/data/app.db").is_file():
                shutil.copy2(ex / "app/data/app.db", root / "app/data/app.db")
            if (ex / "app/data/secret.key").is_file():
                shutil.copy2(ex / "app/data/secret.key", root / "app/data/secret.key")
            if (ex / "orthanc/orthanc.json").is_file():
                shutil.copy2(ex / "orthanc/orthanc.json", root / "orthanc/orthanc.json")
            if (ex / "orthanc/storage/index").is_file():
                shutil.copy2(ex / "orthanc/storage/index", root / "orthanc/storage/index")
            for p in (ex / "orthanc/storage").rglob("*"):
                if p.is_file() and p.name != "index":
                    rel = p.relative_to(ex / "orthanc/storage")
                    target = root / "orthanc/storage" / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, target)
        except Exception as e:
            return {"ok": False, "stage": "apply",
                    "error": f"恢复写入失败：{e}；原数据已保存在 {safeguard}",
                    "safeguard": str(safeguard)}

        progress("apply", 90, "恢复后自检……")
        try:
            con = sqlite3.connect(str(root / "app/data/app.db"))
            users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            con.close()
        except Exception as e:
            return {"ok": False, "stage": "postcheck",
                    "error": f"恢复后 app.db 自检失败：{e}；原数据在 {safeguard}",
                    "safeguard": str(safeguard)}
        report["applied"] = True
        report["safeguard"] = str(safeguard)
        report["postcheck"] = {"users": users}
        report["next"] = ("启动服务（scripts\\start_all.ps1 / EduPACS启动器.exe --start），"
                          "然后运行冒烟测试 tools\\smoke_test.py 核对 26+ 项全部通过")
        progress("done", 100, "恢复完成")
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _restore_commands(path: Path) -> dict:
    py = sys.executable or "python"
    p = str(path)
    return {
        "windows": f'{py} tools\\backup.py restore "{p}" --password <备份密码> --apply',
        "linux": f'{py} tools/backup.py restore "{p}" --password <备份密码> --apply',
        "docker": (f"docker compose down && docker compose run --rm --no-deps app "
                   f"python tools/backup.py restore \"{p}\" --password <备份密码> --apply "
                   f"&& docker compose up -d"),
    }


def load_last_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
