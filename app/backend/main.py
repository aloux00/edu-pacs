# -*- coding: utf-8 -*-
"""
教学病例库系统后端（EduPACS Teaching Case Library）
基座：Orthanc（DICOM 存储/DICOMweb）+ OHIF（阅片）
职责：账号权限、病例管理、教学问答、学习记录、统计看板
"""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import time
import traceback
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import List, Optional

import numpy as np
import pydicom
import requests as http_client
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
from pydantic import BaseModel

from . import lti_xapi  # LTI 1.3 + xAPI 集成（默认关闭，配置见 config/lti.json / xapi.json）

ROOT = Path(__file__).resolve().parent.parent.parent          # edu-pacs/
DATA = ROOT / "app" / "data"
DATA.mkdir(parents=True, exist_ok=True)
# 测试/多实例隔离：可用环境变量改写数据库与密钥位置（默认不变；旧部署行为完全兼容）
_env_db = os.environ.get("EDUPACS_DB_PATH")
_env_sec = os.environ.get("EDUPACS_SECRET_FILE")
DB_PATH = Path(_env_db) if _env_db else DATA / "app.db"
SECRET_FILE = Path(_env_sec) if _env_sec else DATA / "secret.key"
if _env_db:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
if _env_sec:
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
UPLOAD_TMP = ROOT / ".temp" / "uploads"
UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
STATIC_DIR = ROOT / "app" / "static"
ORTHANC_URL = os.environ.get("ORTHANC_URL", "http://127.0.0.1:8042")
ORTHANC_TIMEOUT = int(os.environ.get("ORTHANC_TIMEOUT", "180"))


def _load_orthanc_auth():
    """Orthanc HTTP 认证凭据：环境变量优先，否则读 orthanc.json 的 RegisteredUsers 服务账号。
    返回 None 表示 Orthanc 未启用认证（兼容旧部署）。"""
    u = os.environ.get("ORTHANC_AUTH_USER")
    if u is not None:
        return (u, os.environ.get("ORTHANC_AUTH_PASS", ""))
    try:
        cfg = json.loads((ROOT / "orthanc" / "orthanc.json").read_text(encoding="utf-8"))
        users = cfg.get("RegisteredUsers", {})
        if users:
            name = next(iter(users))
            return (name, users[name])
    except Exception:
        pass
    return None


ORTHANC_AUTH = _load_orthanc_auth()

app = FastAPI(title="EduPACS 教学病例库系统", docs_url="/api/docs")


# 前端静态资源禁用缓存：避免升级后浏览器继续使用旧版 app.js（导航/路由缺失的根因）
@app.middleware("http")
async def _no_cache_frontend(request: Request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith(".html") or p.endswith(".js") or p.endswith(".css") or p.endswith(".ico"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# 未捕获异常：记录完整 traceback 到日志并把错误原因返回前端，避免裸 500 无法排查
@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    detail = f"{type(exc).__name__}: {exc}"
    log_path = DATA / "error.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {request.method} {request.url.path} =====\n")
            f.write(f"{detail}\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass
    # 上传类接口把细节回传，便于前端 toast 显示具体失败原因；其它接口仅回通用提示
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=500, content={"detail": detail})
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


# ---------------------------------------------------------------- 基础设施
def _secret() -> bytes:
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes().strip()
    s = secrets.token_hex(32).encode()
    SECRET_FILE.write_bytes(s)
    return s


SECRET = _secret()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','teacher','student')),
    full_name TEXT DEFAULT '',
    dept TEXT DEFAULT '',
    class_name TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    must_change_pwd INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    study_uid TEXT UNIQUE NOT NULL,
    orthanc_study_id TEXT DEFAULT '',
    patient_id TEXT DEFAULT '',
    patient_name TEXT DEFAULT '',
    patient_age TEXT DEFAULT '',
    patient_sex TEXT DEFAULT '',
    chief_complaint TEXT DEFAULT '',
    medical_history TEXT DEFAULT '',
    clinical_info TEXT DEFAULT '',
    modality TEXT DEFAULT '',
    body_part TEXT DEFAULT '',
    department TEXT DEFAULT '放射科',
    system TEXT DEFAULT '',
    disease TEXT DEFAULT '',
    difficulty INTEGER DEFAULT 1,
    tags TEXT DEFAULT '',
    teaching_points TEXT DEFAULT '',
    diagnosis TEXT DEFAULT '',
    status TEXT DEFAULT 'draft' CHECK(status IN ('draft','published')),
    series_count INTEGER DEFAULT 0,
    image_count INTEGER DEFAULT 0,
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    reference_answer TEXT DEFAULT '',
    score REAL DEFAULT 10,
    sort_no INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES users(id),
    answer TEXT DEFAULT '',
    score REAL,
    feedback TEXT DEFAULT '',
    status TEXT DEFAULT 'submitted' CHECK(status IN ('submitted','graded')),
    submitted_at TEXT DEFAULT (datetime('now','localtime')),
    graded_at TEXT,
    graded_by TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id),
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    note TEXT DEFAULT '',
    view_count INTEGER DEFAULT 0,
    first_viewed TEXT DEFAULT (datetime('now','localtime')),
    last_viewed TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(student_id, case_id)
);
CREATE TABLE IF NOT EXISTS case_origin (
    case_id INTEGER PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
    source_patient_id TEXT DEFAULT '',
    source_patient_name TEXT DEFAULT '',
    source_patient_birth TEXT DEFAULT '',
    source_accession TEXT DEFAULT '',
    source_study_uid TEXT DEFAULT '',
    source_institution TEXT DEFAULT '',
    teaching_code TEXT DEFAULT '',
    deid_note TEXT DEFAULT '上传时自动脱敏',
    deid_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS dict_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL CHECK(category IN ('system','modality','difficulty','disease')),
    code TEXT DEFAULT '',
    label TEXT NOT NULL,
    sort_no INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(category, label)
);
"""

# 字典预置数据（首次初始化自动写入，可自行增改）
DICT_PRESETS = {
    "system": ["呼吸系统", "消化系统", "神经系统", "循环系统", "运动系统", "泌尿系统",
               "生殖系统", "内分泌系统", "血液系统", "头颈部", "乳腺", "五官（眼/耳鼻喉）",
               "儿科影像", "介入放射", "其他"],
    "modality": ["CT", "MR", "DR", "CR", "DX", "US", "MG", "PT", "NM", "OT"],
    "difficulty": [("1", "基础"), ("2", "进阶"), ("3", "疑难")],
    "disease": [],
}


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        # 增量迁移：盲读模式列（旧库兼容）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cases)").fetchall()]
        if "blind_mode" not in cols:
            conn.execute("ALTER TABLE cases ADD COLUMN blind_mode INTEGER DEFAULT 1")
        # 增量迁移：教学用匿名化临床信息（旧库兼容）
        for col in ("patient_age", "patient_sex", "chief_complaint", "medical_history", "clinical_info"):
            if col not in cols:
                conn.execute(f"ALTER TABLE cases ADD COLUMN {col} TEXT DEFAULT ''")
        # 增量迁移：首次登录强制改密标记（旧库兼容）
        ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "must_change_pwd" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN must_change_pwd INTEGER DEFAULT 0")
        # 字典：首次初始化预置 + 回填已有病例实际用到的值（保证旧库升级后下拉不缺失）
        if not conn.execute("SELECT 1 FROM dict_items LIMIT 1").fetchone():
            for cat, items in DICT_PRESETS.items():
                for i, it in enumerate(items):
                    code, label = (it if isinstance(it, tuple) else ("", it))
                    conn.execute("INSERT OR IGNORE INTO dict_items(category,code,label,sort_no) VALUES(?,?,?,?)",
                                 (cat, code, label, i + 1))
        for col, cat in (("system", "system"), ("modality", "modality")):
            for r in conn.execute(f"SELECT DISTINCT {col} v FROM cases WHERE {col}!=''").fetchall():
                conn.execute("INSERT OR IGNORE INTO dict_items(category,code,label) VALUES(?,?,?)",
                             (cat, "", r["v"]))
        for r in conn.execute("SELECT DISTINCT disease v FROM cases WHERE disease!=''").fetchall():
            conn.execute("INSERT OR IGNORE INTO dict_items(category,code,label) VALUES('disease','',?)",
                         (r["v"],))
        if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            conn.execute(
                "INSERT INTO users(username,password,role,full_name,dept,must_change_pwd) VALUES(?,?,?,?,?,1)",
                ("admin", hash_password("admin123"), "admin", "系统管理员", "教务"),
            )


def hash_password(pw: str, salt: str = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 120000).hex()
    return f"{salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    calc = hash_password(pw, salt).split("$", 1)[1]
    return hmac.compare_digest(calc, h)


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _unb64(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def make_token(uid: int, role: str, pwd_hash: str = "") -> str:
    # pd = 密码哈希指纹：改密后旧 token 全部失效（撤销机制）
    payload = _b64(json.dumps({"uid": uid, "role": role, "pd": pwd_hash[:8],
                               "exp": int(time.time()) + 14 * 86400}))
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def parse_token(token: str) -> Optional[dict]:
    try:
        payload, sig = token.split(".", 1)
        expect = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        data = json.loads(_unb64(payload))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def current_user(request: Request) -> sqlite3.Row:
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else request.query_params.get("token", "")
    data = parse_token(token)
    if not data:
        raise HTTPException(401, "未登录或登录已过期")
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (data["uid"],)).fetchone()
    if not row:
        raise HTTPException(401, "账号不存在或已停用")
    # 密码指纹校验：改密后旧 token 立即失效
    if data.get("pd") != str(row["password"])[:8]:
        raise HTTPException(401, "密码已变更，请重新登录")
    return row


def require_roles(*roles):
    def dep(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
        if user["role"] not in roles:
            raise HTTPException(403, "权限不足")
        return user
    return dep


def row_dict(row) -> dict:
    return {k: row[k] for k in row.keys()} if row is not None else None


def rows_list(rows) -> list:
    return [row_dict(r) for r in rows]


# 集成模块初始化：读取外部配置、建增量表、启动 xAPI 发送线程（未启用时零副作用）
lti_xapi.setup(ROOT, db, make_token, hash_password, row_dict, SECRET)


def sanitize_filename(name: str) -> str:
    name = os.path.basename(name or "")
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name or "file.dcm"


# 教学用匿名化临床信息：仅存 SQLite 病例元数据，绝不写回 DICOM PHI 标签
ANON_TEXT_LIMITS = {
    "patient_age": 40,
    "patient_sex": 20,
    "chief_complaint": 500,
    "medical_history": 4000,
    "clinical_info": 4000,
}


def clean_anon_text(value, key: str) -> str:
    """清理匿名化字段：去控制字符、去首尾空白、限长。"""
    s = "" if value is None else str(value)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s.strip()[:ANON_TEXT_LIMITS[key]]


def anon_fields_from_meta(meta: dict) -> dict:
    """从上传/导入 meta 中抽取匿名化字段（缺失留空，兼容旧客户端）。"""
    meta = meta or {}
    return {k: clean_anon_text(meta.get(k, ""), k) for k in ANON_TEXT_LIMITS}


# ---------------------------------------------------------------- 认证接口
class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginBody):
    with db() as conn:
        u = conn.execute("SELECT * FROM users WHERE username=?", (body.username.strip(),)).fetchone()
    if not u or not u["active"] or not verify_password(body.password, u["password"]):
        raise HTTPException(401, "用户名或密码错误")
    return {"token": make_token(u["id"], u["role"], u["password"]),
            "user": {"id": u["id"], "username": u["username"], "role": u["role"],
                      "full_name": u["full_name"], "dept": u["dept"],
                      "must_change_pwd": bool(u["must_change_pwd"]) if "must_change_pwd" in u.keys() else False}}


@app.post("/api/auth/change-password")
def change_password(payload: dict, user=Depends(current_user)):
    """自助改密（含首次登录强制改密）；改密后当前 token 自动失效，需重新登录"""
    old_pwd = str(payload.get("old_password", ""))
    new_pwd = str(payload.get("new_password", ""))
    if len(new_pwd) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    if not verify_password(old_pwd, user["password"]):
        raise HTTPException(400, "原密码不正确")
    with db() as conn:
        conn.execute("UPDATE users SET password=?, must_change_pwd=0 WHERE id=?",
                     (hash_password(new_pwd), user["id"]))
    return {"ok": True, "hint": "密码已修改，请重新登录"}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return row_dict(user) | {"password": None}


# ---------------------------------------------------------------- 用户管理
class UserBody(BaseModel):
    username: str
    password: Optional[str] = None
    role: str = "student"
    full_name: str = ""
    dept: str = ""
    class_name: str = ""
    active: bool = True


@app.get("/api/users")
def list_users(q: str = "", role: str = "", user=Depends(require_roles("admin", "teacher"))):
    sql = "SELECT id,username,role,full_name,dept,class_name,active,created_at FROM users WHERE 1=1"
    args = []
    if q:
        sql += " AND (username LIKE ? OR full_name LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if role:
        sql += " AND role=?"
        args += [role]
    sql += " ORDER BY role DESC, id"
    with db() as conn:
        return rows_list(conn.execute(sql, args).fetchall())


@app.post("/api/users")
def create_user(body: UserBody, user=Depends(require_roles("admin"))):
    if body.role not in ("admin", "teacher", "student"):
        raise HTTPException(400, "角色不合法")
    if not body.password:
        raise HTTPException(400, "必须提供初始密码")
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users(username,password,role,full_name,dept,class_name) VALUES(?,?,?,?,?,?)",
                (body.username.strip(), hash_password(body.password), body.role,
                 body.full_name, body.dept, body.class_name))
        except sqlite3.IntegrityError:
            raise HTTPException(400, "用户名已存在")
        return {"id": cur.lastrowid}


@app.put("/api/users/{uid}")
def update_user(uid: int, payload: dict, user=Depends(require_roles("admin"))):
    with db() as conn:
        old = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not old:
            raise HTTPException(404, "用户不存在")
        fields = {}
        for k in ("role", "full_name", "dept", "class_name"):
            if k in payload and payload[k] is not None:
                if k == "role" and payload[k] not in ("admin", "teacher", "student"):
                    raise HTTPException(400, "角色不合法")
                fields[k] = payload[k]
        if "active" in payload:
            fields["active"] = int(bool(payload["active"]))
        if payload.get("password"):
            fields["password"] = hash_password(payload["password"])
            fields["must_change_pwd"] = 0
        if fields:
            sets = ",".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE users SET {sets} WHERE id=?", list(fields.values()) + [uid])
        return {"ok": True}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, user=Depends(require_roles("admin"))):
    if uid == user["id"]:
        raise HTTPException(400, "不能删除自己")
    with db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.execute("DELETE FROM progress WHERE student_id=?", (uid,))
        conn.execute("DELETE FROM submissions WHERE student_id=?", (uid,))
    return {"ok": True}


def _gen_password() -> str:
    """批量导入自动生成随机密码（大写字母+小写字母+数字）"""
    alphabet = string.ascii_letters + string.digits
    return "Edu" + "".join(secrets.choice(alphabet) for _ in range(9))


@app.post("/api/users/import")
async def import_users(content: str = Form(...), user=Depends(require_roles("admin"))):
    """CSV 文本导入，每行: 用户名,密码,角色,姓名,科室,班级；密码列留空则自动生成随机密码"""
    ok, fail, generated = [], [], []
    with db() as conn:
        for line in content.replace("\r\n", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                fail.append({"line": line, "reason": "列数不足"})
                continue
            username, password = parts[0], parts[1]
            auto_pwd = False
            if not password:
                password = _gen_password()
                auto_pwd = True
            role = parts[2] if len(parts) > 2 and parts[2] in ("admin", "teacher", "student") else "student"
            full_name = parts[3] if len(parts) > 3 else ""
            dept = parts[4] if len(parts) > 4 else ""
            class_name = parts[5] if len(parts) > 5 else ""
            try:
                conn.execute(
                    "INSERT INTO users(username,password,role,full_name,dept,class_name) VALUES(?,?,?,?,?,?)",
                    (username, hash_password(password), role, full_name, dept, class_name))
                ok.append(username)
                if auto_pwd:
                    generated.append({"username": username, "password": password})
            except sqlite3.IntegrityError:
                fail.append({"line": username, "reason": "用户名已存在"})
    return {"imported": len(ok), "failed": fail, "generated": generated,
            "hint": "generated 为自动生成密码的账号（仅本次返回，请导出发放后妥善保存）"}


def _user_template_path() -> Path:
    """生成用户导入 xlsx 模板（含示例与说明页）"""
    path = UPLOAD_TMP / "user_template.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "用户清单"
    ws.append(["用户名", "密码", "角色", "姓名", "科室/系", "班级"])
    rows = [
        ("st101", "st101@pass", "student", "张三", "医学影像系", "影像2401班"),
        ("st102", "", "student", "李四", "医学影像系", "影像2401班"),
        ("teacher02", "", "teacher", "王老师", "放射科", ""),
    ]
    for r in rows:
        ws.append(list(r))
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0F766E")
        c.alignment = Alignment(horizontal="center")
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 10
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 14
    ws.column_dimensions["F"].width = 14
    tip = wb.create_sheet("填写说明")
    tips = [
        "1. 用户名必填且全局唯一（重复行导入时自动跳过，不覆盖已有账号）。",
        "2. 角色可填：student（学生）/ teacher（教师）/ admin（管理员）；留空或不识别默认 student。",
        "3. 密码可留空：导入时自动生成随机密码（Edu 开头 + 9 位），导入结果中一次性展示，请复制保存后发放。",
        "4. 姓名 / 科室系 / 班级可留空；学生建议填班级，便于统计。",
        "5. 与 CSV 粘贴导入等价：每列顺序一致（用户名,密码,角色,姓名,科室,班级）。",
        "6. 批量删除请到「用户列表」勾选后点「批量删除」（不能删除自己，且系统始终保留至少一名管理员）。",
    ]
    for t in tips:
        tip.append([t])
    tip.column_dimensions["A"].width = 110
    wb.save(path)
    return path


@app.get("/api/users/template")
def user_template(user=Depends(require_roles("admin"))):
    return FileResponse(_user_template_path(), filename="EduPACS用户导入模板.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/users/import-xlsx")
async def import_users_xlsx(file: UploadFile = File(...),
                            user=Depends(require_roles("admin"))):
    """xlsx 导入用户：Sheet「用户清单」列=用户名/密码/角色/姓名/科室/班级；密码留空自动生成随机密码"""
    raw = await file.read()
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise HTTPException(400, "无法解析 xlsx 文件，请使用「下载模板」生成的模板填写")
    ws = None
    for name in ("用户清单", "Sheet1", "Sheet"):
        if name in wb.sheetnames:
            ws = wb[name]
            break
    if ws is None:
        ws = wb[wb.sheetnames[0]]
    ok, fail, generated = [], [], []
    with db() as conn:
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not any(v is not None and str(v).strip() for v in row):
                continue
            username = str(row[0] or "").strip()
            if not username:
                fail.append({"line": [str(v) for v in row], "reason": "用户名为空"})
                continue
            password = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
            auto_pwd = False
            if not password:
                password = _gen_password()
                auto_pwd = True
            role = "student"
            if len(row) > 2 and row[2] is not None and str(row[2]).strip() in ("admin", "teacher", "student"):
                role = str(row[2]).strip()
            full_name = str(row[3] or "").strip() if len(row) > 3 else ""
            dept = str(row[4] or "").strip() if len(row) > 4 else ""
            class_name = str(row[5] or "").strip() if len(row) > 5 else ""
            try:
                conn.execute(
                    "INSERT INTO users(username,password,role,full_name,dept,class_name) VALUES(?,?,?,?,?,?)",
                    (username, hash_password(password), role, full_name, dept, class_name))
                ok.append(username)
                if auto_pwd:
                    generated.append({"username": username, "password": password})
            except sqlite3.IntegrityError:
                fail.append({"line": username, "reason": "用户名已存在"})
    return {"imported": len(ok), "failed": fail, "generated": generated,
            "hint": "generated 为自动生成密码的账号（仅本次返回，请复制发放后妥善保存）"}


@app.post("/api/users/batch-delete")
def batch_delete_users(payload: dict, user=Depends(require_roles("admin"))):
    """批量删除用户：{ids: [1,2,3]}；不可删除自己；不可删光全部管理员；学习记录一并清理"""
    ids = payload.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "未选择要删除的用户")
    ids = [int(i) for i in ids if str(i).isdigit()]
    if user["id"] in ids:
        raise HTTPException(400, "不能删除自己")
    deleted, skipped = 0, []
    with db() as conn:
        for uid in ids:
            row = conn.execute("SELECT id,role,username FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                skipped.append({"id": uid, "reason": "不存在"})
                continue
            if row["role"] == "admin":
                admins = conn.execute(
                    "SELECT COUNT(*) c FROM users WHERE role='admin' AND id NOT IN (%s)"
                    % ",".join("?" * len(ids)), ids).fetchone()["c"]
                if admins < 1:
                    skipped.append({"id": uid, "username": row["username"], "reason": "不能删除最后一个管理员"})
                    continue
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
            conn.execute("DELETE FROM progress WHERE student_id=?", (uid,))
            conn.execute("DELETE FROM submissions WHERE student_id=?", (uid,))
            deleted += 1
    return {"deleted": deleted, "skipped": skipped}


# ---------------------------------------------------------------- 字典管理（系统/模态/难度/疾病名称）
DICT_CATEGORIES = {
    "system": "系统",
    "modality": "模态",
    "difficulty": "难度",
    "disease": "疾病名称",
}
DICT_LABEL_TO_CAT = {v: k for k, v in DICT_CATEGORIES.items()}


class DictItemBody(BaseModel):
    category: str
    code: str = ""
    label: str
    sort_no: int = 0
    active: bool = True


def _dict_template_path() -> Path:
    """生成 xlsx 导入模板（含示例与说明页）"""
    path = UPLOAD_TMP / "dict_template.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "字典清单"
    ws.append(["类别", "代码", "名称", "排序", "是否启用"])
    rows = [
        ("系统", "", "呼吸系统", 1, "是"),
        ("系统", "", "消化系统", 2, "是"),
        ("模态", "CT", "CT", 1, "是"),
        ("模态", "OT", "其他（图片/论文截图）", 10, "是"),
        ("难度", "1", "基础", 1, "是"),
        ("难度", "2", "进阶", 2, "是"),
        ("难度", "3", "疑难", 3, "是"),
        ("疾病名称", "", "肺结节", 1, "是"),
        ("疾病名称", "", "脑膜瘤", 2, "是"),
    ]
    for r in rows:
        ws.append(list(r))
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0F766E")
        c.alignment = Alignment(horizontal="center")
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 26
    ws.column_dimensions["D"].width = 8
    ws.column_dimensions["E"].width = 10
    tip = wb.create_sheet("填写说明")
    tips = [
        "1. 类别只能填以下四种之一：系统 / 模态 / 难度 / 疾病名称（也接受英文 system、modality、difficulty、disease）。",
        "2. 名称必填；同一类别下名称不能重复，重复行导入时自动跳过（不覆盖已有）。",
        "3. 代码：模态建议填标准缩写（如 CT、MR、DR、US、OT）；难度必须填 1/2/3；系统与疾病名称可留空。",
        "4. 难度属于固定三档：代码 1=基础、2=进阶、3=疑难，名称可按教学口径自定义（如“入门/提高/疑难”）。",
        "5. 是否启用：填“是/停用”，留空默认启用。停用的项不再出现在上传/筛选下拉中。",
        "6. 导入完成后可在字典管理页继续维护；导出功能可将当前全部字典下载为 xlsx 备份。",
    ]
    for t in tips:
        tip.append([t])
    tip.column_dimensions["A"].width = 110
    wb.save(path)
    return path


@app.get("/api/dicts/template")
def dict_template(user=Depends(require_roles("admin", "teacher"))):
    return FileResponse(_dict_template_path(), filename="EduPACS字典导入模板.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/dicts/export")
def dict_export(user=Depends(require_roles("admin", "teacher"))):
    path = UPLOAD_TMP / "dict_export.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "字典清单"
    ws.append(["类别", "代码", "名称", "排序", "是否启用"])
    with db() as conn:
        rows = conn.execute(
            "SELECT category,code,label,sort_no,active FROM dict_items ORDER BY category,sort_no,id").fetchall()
    for r in rows:
        ws.append([DICT_CATEGORIES.get(r["category"], r["category"]), r["code"], r["label"],
                    r["sort_no"], "是" if r["active"] else "停用"])
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["C"].width = 26
    wb.save(path)
    return FileResponse(path, filename="EduPACS字典导出.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/dicts/import")
async def dict_import(file: UploadFile = File(...),
                      user=Depends(require_roles("admin", "teacher"))):
    """xlsx 模板导入：Sheet「字典清单」列=类别/代码/名称/排序/是否启用；同名同类别跳过"""
    raw = await file.read()
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise HTTPException(400, "无法解析 xlsx 文件，请使用「下载模板」生成的模板填写")
    ws = None
    for name in ("字典清单", "Sheet1", "Sheet"):
        if name in wb.sheetnames:
            ws = wb[name]
            break
    if ws is None:
        ws = wb[wb.sheetnames[0]]
    ok, skip, bad = 0, 0, []
    with db() as conn:
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not any(v is not None and str(v).strip() for v in row):
                continue
            cat_raw = str(row[0] or "").strip()
            category = DICT_LABEL_TO_CAT.get(cat_raw, cat_raw if cat_raw in DICT_CATEGORIES else "")
            label = str(row[2] or "").strip() if len(row) > 2 else ""
            code = str(row[1] or "").strip() if len(row) > 1 else ""
            if not category or not label:
                bad.append({"line": [str(v) for v in row], "reason": "类别或名称缺失/类别不合法"})
                continue
            if category == "difficulty" and str(code).strip() not in ("1", "2", "3"):
                bad.append({"line": [str(v) for v in row], "reason": "难度的代码必须为 1/2/3"})
                continue
            sort_no = 0
            try:
                sort_no = int(row[3]) if len(row) > 3 and row[3] is not None else 0
            except (TypeError, ValueError):
                pass
            active = 1
            if len(row) > 4 and str(row[4] or "").strip() in ("停用", "否", "0", "false", "False"):
                active = 0
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO dict_items(category,code,label,sort_no,active) VALUES(?,?,?,?,?)",
                    (category, code, label, sort_no, active))
                if cur.rowcount:
                    ok += 1
                else:
                    skip += 1
            except sqlite3.IntegrityError:
                skip += 1
    return {"imported": ok, "skipped": skip, "failed": bad,
            "hint": "同名同类别的行已自动跳过（不覆盖），如需修改请在字典管理页编辑"}


@app.post("/api/dicts/import-text")
def dict_import_text(payload: dict, user=Depends(require_roles("admin", "teacher"))):
    """粘贴导入（CSV 文本，每行：类别,代码,名称,排序,是否启用）——不依赖文件选择框的兜底通道"""
    content = str(payload.get("content", ""))
    ok, skip, bad = 0, 0, []
    with db() as conn:
        for ln, line in enumerate(content.splitlines(), start=1):
            line = line.strip().strip("\ufeff")
            if not line or line.startswith("#") or line.startswith("类别") or line.startswith("类别,"):
                continue
            cells = [c.strip() for c in line.split(",")]
            if len(cells) < 3:
                bad.append({"line": ln, "text": line, "reason": "至少需要 3 列：类别,代码,名称"})
                continue
            cat_raw = cells[0]
            category = DICT_LABEL_TO_CAT.get(cat_raw, cat_raw if cat_raw in DICT_CATEGORIES else "")
            code, label = cells[1], cells[2]
            if not category or not label:
                bad.append({"line": ln, "text": line, "reason": "类别或名称缺失/类别不合法（系统/模态/难度/疾病名称）"})
                continue
            if category == "difficulty" and code not in ("1", "2", "3"):
                bad.append({"line": ln, "text": line, "reason": "难度的代码必须为 1/2/3"})
                continue
            sort_no = 0
            try:
                sort_no = int(cells[3]) if len(cells) > 3 and cells[3] else 0
            except ValueError:
                pass
            active = 1
            if len(cells) > 4 and cells[4] in ("停用", "否", "0", "false", "False"):
                active = 0
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO dict_items(category,code,label,sort_no,active) VALUES(?,?,?,?,?)",
                    (category, code, label, sort_no, active))
                if cur.rowcount:
                    ok += 1
                else:
                    skip += 1
            except sqlite3.IntegrityError:
                skip += 1
    return {"imported": ok, "skipped": skip, "failed": bad,
            "hint": "同名同类别自动跳过；类别支持中文（系统/模态/难度/疾病名称）"}


@app.get("/api/dicts")
def list_dicts(category: str = "", q: str = "", user=Depends(require_roles("admin", "teacher"))):
    sql = "SELECT * FROM dict_items WHERE 1=1"
    args: list = []
    if category:
        sql += " AND category=?"
        args.append(category)
    if q:
        sql += " AND (label LIKE ? OR code LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY category, CASE WHEN sort_no=0 THEN 9999 ELSE sort_no END, id"
    with db() as conn:
        rows = rows_list(conn.execute(sql, args).fetchall())
    for r in rows:
        r["category_label"] = DICT_CATEGORIES.get(r["category"], r["category"])
    return {"items": rows}


@app.post("/api/dicts")
def create_dict_item(body: DictItemBody, user=Depends(require_roles("admin", "teacher"))):
    if body.category not in DICT_CATEGORIES:
        raise HTTPException(400, "类别不合法，只能为：系统/模态/难度/疾病名称")
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "名称不能为空")
    if body.category == "difficulty":
        body.code = str(body.code or "").strip() or label
        if body.code not in ("1", "2", "3"):
            raise HTTPException(400, "难度的代码必须为 1/2/3")
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO dict_items(category,code,label,sort_no,active) VALUES(?,?,?,?,?)",
                (body.category, (body.code or "").strip(), label, body.sort_no, int(body.active)))
        except sqlite3.IntegrityError:
            raise HTTPException(400, "该类别下已存在同名项")
        return {"id": cur.lastrowid}


@app.put("/api/dicts/{item_id}")
def update_dict_item(item_id: int, payload: dict, user=Depends(require_roles("admin", "teacher"))):
    fields = {}
    for k in ("code", "label", "sort_no", "active"):
        if k in payload and payload[k] is not None:
            if k == "label" and not str(payload[k]).strip():
                raise HTTPException(400, "名称不能为空")
            fields[k] = int(bool(payload[k])) if k == "active" else (
                int(payload[k]) if k == "sort_no" else str(payload[k]))
    if not fields:
        raise HTTPException(400, "没有需要更新的字段")
    sets = ",".join(f"{k}=?" for k in fields)
    with db() as conn:
        cur = conn.execute(f"UPDATE dict_items SET {sets} WHERE id=?", list(fields.values()) + [item_id])
        if cur.rowcount == 0:
            raise HTTPException(404, "字典项不存在")
    return {"ok": True}


@app.delete("/api/dicts/{item_id}")
def delete_dict_item(item_id: int, user=Depends(require_roles("admin", "teacher"))):
    with db() as conn:
        row = conn.execute("SELECT * FROM dict_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "字典项不存在")
        # 已被病例引用的项不允许硬删（可停用），避免筛选/上传下拉与既有病例脱节
        col = {"system": "system", "modality": "modality", "disease": "disease"}.get(row["category"])
        if col:
            n = conn.execute(f"SELECT COUNT(*) FROM cases WHERE {col}=?", (row["label"],)).fetchone()[0]
            if n:
                raise HTTPException(400, f"已有 {n} 个病例正在使用「{row['label']}」，请改为停用而非删除")
        if row["category"] == "difficulty":
            used = conn.execute("SELECT COUNT(*) FROM cases WHERE difficulty=?", (int(row["code"] or 0),)).fetchone()[0]
            if used:
                raise HTTPException(400, "该难度档已有病例使用，请改为停用而非删除")
        conn.execute("DELETE FROM dict_items WHERE id=?", (item_id,))
    return {"ok": True}


# ---------------------------------------------------------------- Orthanc 代理
def orthanc_get(path: str):
    r = http_client.get(ORTHANC_URL + path, timeout=ORTHANC_TIMEOUT, auth=ORTHANC_AUTH)
    r.raise_for_status()
    return r.json()


@app.get("/api/orthanc/health")
def orthanc_health():
    try:
        info = orthanc_get("/system")
        return {"ok": True, "name": info.get("Name"), "version": info.get("Version")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------- 远程 PACS / 设备节点（Orthanc Modalities & Peers）
def _orthanc_request(method: str, path: str, json_body=None, timeout=None):
    r = http_client.request(method, ORTHANC_URL + path, json=json_body,
                            timeout=timeout or ORTHANC_TIMEOUT, auth=ORTHANC_AUTH)
    return r


def _orthanc_json(method: str, path: str, json_body=None):
    r = _orthanc_request(method, path, json_body)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:200]
        raise HTTPException(502, f"Orthanc 返回 {r.status_code}：{detail}")
    return r.json() if r.content else {}


@app.get("/api/orthanc/local-aet")
def orthanc_local_aet(user=Depends(require_roles("admin", "teacher"))):
    """本机接收信息（影像设备直推用）"""
    try:
        sysinfo = orthanc_get("/system")
        return {"aet": sysinfo.get("DicomAet", ""), "port": sysinfo.get("DicomPort", 4242)}
    except Exception as e:
        return {"aet": "", "port": "", "error": str(e)[:200]}


@app.get("/api/orthanc/modalities")
def list_modalities(user=Depends(require_roles("admin", "teacher"))):
    names = _orthanc_json("GET", "/modalities")
    items = []
    for n in names:
        # 注意：GET /modalities/{name} 返回的是该节点支持的操作列表；
        # 配置详情（AET/Host/Port）在 GET /modalities/{name}/configuration
        try:
            cfg = _orthanc_json("GET", f"/modalities/{n}/configuration")
        except HTTPException:
            cfg = {}
        items.append({"name": n, "aet": cfg.get("AET", ""), "host": cfg.get("Host", ""),
                      "port": cfg.get("Port", ""),
                      "capabilities": _orthanc_json("GET", f"/modalities/{n}") or []})
    return {"items": items}


class ModalityBody(BaseModel):
    aet: str
    host: str
    port: int
    comment: str = ""


@app.put("/api/orthanc/modalities/{name}")
def upsert_modality(name: str, body: ModalityBody, user=Depends(require_roles("admin", "teacher"))):
    if not re.fullmatch(r"[\w\-.]{1,32}", name):
        raise HTTPException(400, "节点名称只能包含字母/数字/下划线/中划线/点")
    # Orthanc 1.12+ 支持通过 PUT 动态注册（无需重启，重启后失效——需要写入配置文件持久化见下）
    _orthanc_json("PUT", f"/modalities/{name}",
                  {"AET": body.aet, "Host": body.host, "Port": body.port, "Comment": body.comment})
    _persist_modality(name, body)
    return {"ok": True}


@app.delete("/api/orthanc/modalities/{name}")
def delete_modality(name: str, user=Depends(require_roles("admin", "teacher"))):
    try:
        _orthanc_json("DELETE", f"/modalities/{name}")
    except HTTPException as e:
        if "404" not in str(e.detail):
            raise
    _persist_modality(name, None)
    return {"ok": True}


_MODALITY_CFG_PATH = ROOT / "orthanc" / "orthanc.json"


def _persist_modality(name: str, body):
    """同步写入 orthanc.json 的 DicomModalities 段，重启后节点仍生效（动态注册重启即失效）"""
    try:
        cfg = json.loads(_MODALITY_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    mods = cfg.setdefault("DicomModalities", {})
    if body is None:
        mods.pop(name, None)
    else:
        mods[name] = [body.aet, body.host, int(body.port)]
        if body.comment:
            mods[name].append(body.comment)
    _MODALITY_CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


@app.post("/api/orthanc/modalities/{name}/echo")
def modality_echo(name: str, user=Depends(require_roles("admin", "teacher"))):
    """C-ECHO 连通性测试"""
    try:
        _orthanc_json("POST", f"/modalities/{name}/echo")
        return {"ok": True}
    except HTTPException:
        return {"ok": False, "error": "C-ECHO 失败：对方未开机 / AET·地址·端口不符 / 网络不通"}


@app.post("/api/orthanc/modalities/{name}/query")
def modality_query(name: str, body: dict, user=Depends(require_roles("admin", "teacher"))):
    """C-FIND 远程查询（Study 级）：body={"PatientID":"","PatientName":"","StudyDate":"","StudyDescription":""}
    返回紧凑列表（含 StudyInstanceUID / 患者信息 / 日期 / 描述 / 序列数与图像数）"""
    filters = {k: str(v).strip() for k, v in (body or {}).items()
               if str(v or "").strip() and k not in ("Level",)}
    # C-FIND 默认精确匹配，用户输入自动转模糊（不含通配符时包裹 *）
    for k in ("PatientName", "PatientID", "StudyDescription"):
        if k in filters and "*" not in filters[k] and "?" not in filters[k]:
            filters[k] = f"*{filters[k]}*"
    # 空键 = 匹配任意 + 请求该字段返回（C-FIND 只返回查询中出现的键）
    query = {"PatientName": "", "PatientID": "", "StudyDate": "", "StudyDescription": "",
             "ModalitiesInStudy": "", "NumberOfStudyRelatedSeries": "",
             "NumberOfStudyRelatedInstances": "", "StudyInstanceUID": ""}
    query.update(filters)
    payload = {"Level": "Study", "Query": query}
    try:
        resp = _orthanc_json("POST", f"/modalities/{name}/query", payload)
        # Orthanc 1.13 返回 {"ID":..., "Path":...}；老版本返回字符串
        qid = resp["ID"] if isinstance(resp, dict) else resp
    except HTTPException as e:
        raise HTTPException(502, f"C-FIND 失败：{e.detail}")
    try:
        count = _orthanc_json("GET", f"/queries/{qid}/answers")
        if isinstance(count, dict):
            count = []
    except Exception:
        count = []
    results = []
    for idx in range(len(count)):
        try:
            # Orthanc 1.13：答案内容在 /answers/{idx}/content（simplify 键名）
            ans = _orthanc_json("GET", f"/queries/{qid}/answers/{idx}/content?simplify=true")
        except Exception:
            continue
        results.append({
            "patient_id": ans.get("PatientID", ""),
            "patient_name": ans.get("PatientName", ""),
            "study_date": ans.get("StudyDate", ""),
            "study_desc": ans.get("StudyDescription", ""),
            "modality": ans.get("Modality", "") or ans.get("ModalitiesInStudy", ""),
            "series_count": ans.get("NumberOfStudyRelatedSeries", ""),
            "image_count": ans.get("NumberOfStudyRelatedInstances", ""),
            "study_uid": ans.get("StudyInstanceUID", ""),
        })
    try:
        _orthanc_json("DELETE", f"/queries/{qid}")
    except Exception:
        pass
    return {"items": results}


@app.get("/api/orthanc/peers")
def list_peers(user=Depends(require_roles("admin", "teacher"))):
    # GET /peers/{name} 只返回操作列表，配置详情（Url/用户名）从 orthanc.json 读取
    names = _orthanc_json("GET", "/peers")
    cfg_from_file = {}
    try:
        cfg_from_file = json.loads(_MODALITY_CFG_PATH.read_text(encoding="utf-8")).get("Peers", {})
    except Exception:
        pass
    items = []
    for n in names:
        v = cfg_from_file.get(n, [])
        url = v[0] if len(v) > 0 else ""
        username = v[1] if len(v) > 1 else ""
        has_pw = len(v) > 2 and bool(v[2])
        items.append({"name": n, "url": url, "username": username, "has_password": has_pw})
    return {"items": items}


class PeerBody(BaseModel):
    url: str
    username: str = ""
    password: str = ""


@app.put("/api/orthanc/peers/{name}")
def upsert_peer(name: str, body: PeerBody, user=Depends(require_roles("admin", "teacher"))):
    if not re.fullmatch(r"[\w\-.]{1,32}", name):
        raise HTTPException(400, "节点名称只能包含字母/数字/下划线/中划线/点")
    payload = {"Url": body.url}
    if body.username:
        payload["Username"] = body.username
    if body.password:
        payload["Password"] = body.password
    _orthanc_json("PUT", f"/peers/{name}", payload)
    try:
        cfg = json.loads(_MODALITY_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    peers = cfg.setdefault("Peers", {})
    peers[name] = [body.url] + ([body.username, body.password] if body.username else [])
    _MODALITY_CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


@app.delete("/api/orthanc/peers/{name}")
def delete_peer(name: str, user=Depends(require_roles("admin", "teacher"))):
    try:
        _orthanc_json("DELETE", f"/peers/{name}")
    except HTTPException:
        pass
    try:
        cfg = json.loads(_MODALITY_CFG_PATH.read_text(encoding="utf-8"))
        cfg.get("Peers", {}).pop(name, None)
        _MODALITY_CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/orthanc/peers/{name}/system")
def peer_test(name: str, user=Depends(require_roles("admin", "teacher"))):
    try:
        info = _orthanc_json("POST", f"/peers/{name}/system")
        return {"ok": True, "version": info.get("Version", ""), "name": info.get("Name", "")}
    except HTTPException:
        return {"ok": False, "error": "连接失败：URL / 认证信息不正确或对方不可达"}


# ---------------------------------------------------------------- 从 Orthanc / 远程 PACS 导入病例（统一脱敏管线）
def _download_study_instance_ids(sid: str) -> list:
    series_list = orthanc_get(f"/studies/{sid}/series")
    ids = []
    for s in series_list:
        for iid in s.get("Instances", []):
            ids.append(iid)
    return ids


def _create_case_record(sid: str, m: dict, user: sqlite3.Row, teaching_code: str,
                        origin: dict, deid_note: str) -> tuple:
    """按已入库 Orthanc study 创建病例记录（upload/import 共用）"""
    study = orthanc_get(f"/studies/{sid}")
    series_list = orthanc_get(f"/studies/{sid}/series")
    image_count = sum(len(s.get("Instances", [])) for s in series_list)
    tags = study.get("MainDicomTags", {})
    pat = study.get("PatientMainDicomTags", {})
    modality = (m.get("modality") or tags.get("Modality")
                or (series_list[0].get("MainDicomTags", {}).get("Modality", "") if series_list else ""))
    title = m.get("title") or tags.get("Description") or f"病例_{time.strftime('%Y%m%d_%H%M%S')}"
    with db() as conn:
        exists = conn.execute("SELECT id FROM cases WHERE study_uid=?",
                              (tags.get("StudyInstanceUID", ""),)).fetchone()
        if exists:
            raise HTTPException(400, f"该检查已导入过（病例 #{exists['id']}），请勿重复导入")
        anon = anon_fields_from_meta(m)
        cur = conn.execute(
            """INSERT INTO cases(title,study_uid,orthanc_study_id,patient_id,patient_name,modality,
               body_part,department,system,disease,difficulty,tags,teaching_points,diagnosis,
               status,series_count,image_count,created_by,blind_mode,
               patient_age,patient_sex,chief_complaint,medical_history,clinical_info)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title, tags.get("StudyInstanceUID", ""), sid, pat.get("PatientID", ""),
             pat.get("PatientName", ""), modality, m.get("body_part", ""),
             m.get("department", "放射科"), m.get("system", ""), m.get("disease", ""),
             int(m.get("difficulty") or 1), m.get("tags", ""), m.get("teaching_points", ""),
             m.get("diagnosis", ""), m.get("status", "draft"),
             len(series_list), image_count, user["username"],
             1 if m.get("blind_mode", True) else 0,
             anon["patient_age"], anon["patient_sex"], anon["chief_complaint"],
             anon["medical_history"], anon["clinical_info"]))
        case_id = cur.lastrowid
        conn.execute(
            """INSERT INTO case_origin(case_id,source_patient_id,source_patient_name,
               source_patient_birth,source_accession,source_study_uid,source_institution,
               teaching_code,deid_note) VALUES(?,?,?,?,?,?,?,?,?)""",
            (case_id, origin.get("source_patient_id", ""), origin.get("source_patient_name", ""),
             origin.get("source_patient_birth", ""), origin.get("source_accession", ""),
             origin.get("source_study_uid", ""), origin.get("source_institution", ""),
             teaching_code, deid_note))
    return case_id, {"title": title, "modality": modality, "series_count": len(series_list),
                     "image_count": image_count}


def _import_orthanc_study_as_case(sid: str, m: dict, user: sqlite3.Row, deid_note: str) -> dict:
    """把本地 Orthanc 中已存在的 study 走脱敏管线重建并入库为教学病例（远程拉取/本地未入库通用）"""
    instance_ids = _download_study_instance_ids(sid)
    if not instance_ids:
        raise HTTPException(400, "该检查没有任何实例可导入")
    # 该 study 的原始 UID（脱敏前）——用于判断是否已被病例引用
    src_uid = ""
    try:
        src_uid = orthanc_get(f"/studies/{sid}").get("MainDicomTags", {}).get("StudyInstanceUID", "")
    except Exception:
        pass
    with db() as conn:
        dup = conn.execute("SELECT id FROM cases WHERE study_uid=? OR orthanc_study_id=?",
                           (src_uid, sid)).fetchone()
    if dup:
        raise HTTPException(400, f"该检查已是系统病例（#{dup['id']}），请勿重复导入")
    teaching_code = _next_teaching_code()
    study_map: dict = {}
    series_map: dict = {}
    phi_pool: dict = {}
    scrubbed: list = []
    origin = {}
    for iid in instance_ids:
        try:
            raw = http_client.get(f"{ORTHANC_URL}/instances/{iid}/file",
                                  timeout=ORTHANC_TIMEOUT, auth=ORTHANC_AUTH).content
            data_bytes, origin = _deid_dataset(raw, teaching_code, study_map, series_map, phi_pool)
            scrubbed.append(data_bytes)
        except HTTPException:
            raise
        except Exception:
            continue
    if not scrubbed:
        raise HTTPException(400, "该检查的实例均无法解析，导入中止")
    study_ids: dict = {}
    failed = 0
    for data_bytes in scrubbed:
        try:
            r = http_client.post(ORTHANC_URL + "/instances", data=data_bytes,
                                 headers={"Content-Type": "application/dicom"},
                                 timeout=ORTHANC_TIMEOUT, auth=ORTHANC_AUTH)
            if r.status_code == 200:
                study_ids[r.json()["ParentStudy"]] = True
            else:
                failed += 1
        except Exception:
            failed += 1
    if len(study_ids) != 1:
        raise HTTPException(400, "脱敏重建异常：实例不属于同一检查，导入中止")
    new_sid = next(iter(study_ids))
    # 删除本地原始 study：Orthanc 内只保留脱敏后的数据
    # （仅在该 study 未被其他病例引用时；fetch 重复拉取已有病例的场景已在前面拦截）
    try:
        with db() as conn:
            still_used = conn.execute("SELECT 1 FROM cases WHERE orthanc_study_id=?", (sid,)).fetchone()
        if not still_used:
            http_client.delete(f"{ORTHANC_URL}/studies/{sid}", timeout=ORTHANC_TIMEOUT, auth=ORTHANC_AUTH)
    except Exception:
        pass
    case_id, info = _create_case_record(new_sid, m, user, teaching_code, origin, deid_note)
    return {"id": case_id, "uploaded": len(scrubbed), "failed": failed,
            "teaching_code": teaching_code, **info}


class ImportFromOrthancBody(BaseModel):
    study_uid: Optional[str] = None
    study_id: Optional[str] = None
    meta: dict = {}


@app.post("/api/cases/import-from-orthanc")
def import_case_from_orthanc(body: ImportFromOrthancBody,
                             user=Depends(require_roles("admin", "teacher"))):
    """把本地 Orthanc 已有检查（如影像设备直推）导入为教学病例：自动脱敏重建"""
    sid = body.study_id
    if not sid and body.study_uid:
        found = _orthanc_json("POST", "/tools/find", {"Level": "Study",
                                                      "Query": {"StudyInstanceUID": body.study_uid}})
        if not found:
            raise HTTPException(404, "Orthanc 中未找到该检查")
        sid = found[0]
    if not sid:
        raise HTTPException(400, "缺少 study_uid / study_id")
    return _import_orthanc_study_as_case(sid, body.meta, user, "本地检查导入，自动脱敏重建")


@app.get("/api/orthanc/orphan-studies")
def orphan_studies(user=Depends(require_roles("admin", "teacher"))):
    """Orthanc 中存在但尚未关联病例的检查（如设备直推数据）——注意：未脱敏，导入时自动脱敏"""
    studies = _orthanc_json("GET", "/studies?expand")
    with db() as conn:
        known = {r[0] for r in conn.execute("SELECT orthanc_study_id FROM cases WHERE orthanc_study_id!=''")}
        known_uids = {r[0] for r in conn.execute("SELECT study_uid FROM cases")}
    items = []
    for s in studies:
        uid = s.get("MainDicomTags", {}).get("StudyInstanceUID", "")
        if s.get("ID") in known or uid in known_uids:
            continue
        pat = s.get("PatientMainDicomTags", {})
        n_series = len(s.get("Series", [])) if isinstance(s.get("Series"), list) else 0
        items.append({
            "study_id": s.get("ID"), "study_uid": uid,
            "patient_id": pat.get("PatientID", ""), "patient_name": pat.get("PatientName", ""),
            "study_date": s.get("MainDicomTags", {}).get("StudyDate", ""),
            "study_desc": s.get("MainDicomTags", {}).get("StudyDescription", ""),
            "series_count": n_series,
            "arrival": s.get("LastUpdate", ""),
        })
    items.sort(key=lambda x: x["arrival"], reverse=True)
    return {"items": items}


class FetchCaseBody(BaseModel):
    study_uid: str
    patient_id: str = ""
    patient_name: str = ""
    meta: dict = {}


@app.post("/api/orthanc/modalities/{name}/fetch-case")
def fetch_case_from_remote(name: str, body: FetchCaseBody,
                           user=Depends(require_roles("admin", "teacher"))):
    """从远程 PACS C-MOVE 拉取指定 study 到本地 → 自动脱敏 → 入库为教学病例"""
    try:
        resp = _orthanc_json("POST", f"/modalities/{name}/query",
                             {"Level": "Study", "Query": {"StudyInstanceUID": body.study_uid}})
        qid = resp["ID"] if isinstance(resp, dict) else resp
    except HTTPException as e:
        raise HTTPException(502, f"C-FIND 定位失败：{e.detail}")
    try:
        aet = orthanc_get("/system").get("DicomAet", "EDUPACS")
    except Exception:
        aet = "EDUPACS"
    # Orthanc 1.13：按答案索引 C-MOVE；兼容旧式 /queries/{qid}/retrieve
    retrieved = False
    last_err = None
    try:
        answers = _orthanc_json("GET", f"/queries/{qid}/answers")
    except Exception:
        answers = []
    if answers:
        for aidx in range(len(answers)):
            try:
                _orthanc_json("POST", f"/queries/{qid}/answers/{aidx}/retrieve", {"TargetAet": aet})
                retrieved = True
            except HTTPException as e:
                last_err = e
    if not retrieved:
        try:
            _orthanc_json("POST", f"/queries/{qid}/retrieve", {"TargetAet": aet})
            retrieved = True
        except HTTPException as e:
            last_err = e
            try:
                _orthanc_json("DELETE", f"/queries/{qid}")
            except Exception:
                pass
            raise HTTPException(502, f"C-MOVE 拉取失败：{e.detail}")
    # 等待数据到达本地（最长 120s）
    deadline = time.time() + 120
    sid = None
    while time.time() < deadline:
        try:
            found = _orthanc_json("POST", "/tools/find",
                                  {"Level": "Study", "Query": {"StudyInstanceUID": body.study_uid}})
        except Exception:
            found = []
        if found:
            sid = found[0]
            break
        time.sleep(3)
    try:
        _orthanc_json("DELETE", f"/queries/{qid}")
    except Exception:
        pass
    if not sid:
        raise HTTPException(504, "拉取超时：数据 120 秒内未到达本地，请检查远程节点网络与 C-MOVE 权限")
    result = _import_orthanc_study_as_case(sid, body.meta, user,
                                           "远程 PACS 拉取导入，自动脱敏重建")
    result["source"] = {"modality_node": name, "patient_id": body.patient_id,
                        "patient_name": body.patient_name}
    return result


# ---------------------------------------------------------------- 病例管理
@app.get("/api/cases")
def list_cases(q: str = "", department: str = "", system: str = "", modality: str = "",
               disease: str = "", difficulty: int = 0, status: str = "", page: int = 1, page_size: int = 12,
               user=Depends(current_user)):
    sql = "SELECT * FROM cases WHERE 1=1"
    args: list = []
    if user["role"] == "student":
        sql += " AND status='published'"
    elif status:
        sql += " AND status=?"
        args += [status]
    if q:
        sql += (" AND (title LIKE ? OR disease LIKE ? OR tags LIKE ? OR patient_id LIKE ?"
                " OR patient_age LIKE ? OR patient_sex LIKE ? OR chief_complaint LIKE ?"
                " OR medical_history LIKE ? OR clinical_info LIKE ?")
        args += [f"%{q}%"] * 9
        if user["role"] in ("admin", "teacher"):
            # 教师可用原始住院号/检查号/源UID反查（溯源检索）
            sql += " OR EXISTS(SELECT 1 FROM case_origin o WHERE o.case_id=cases.id " \
                    "AND (o.source_patient_id LIKE ? OR o.source_accession LIKE ? OR o.source_study_uid LIKE ?))"
            args += [f"%{q}%"] * 3
        sql += ")"
    if department:
        sql += " AND department=?"
        args += [department]
    if system:
        sql += " AND system=?"
        args += [system]
    if modality:
        sql += " AND modality=?"
        args.append(modality)
    if disease:
        sql += " AND disease LIKE ?"
        args.append(f"%{disease}%")
    if difficulty:
        sql += " AND difficulty=?"
        args += [difficulty]
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?",
                            args + [page_size, (page - 1) * page_size]).fetchall()
        # 学生视角补充个人进度
        learned = {}
        if user["role"] == "student":
            for r in conn.execute("SELECT case_id FROM progress WHERE student_id=?",
                                  (user["id"],)).fetchall():
                learned[r["case_id"]] = True
        items = []
        for r in rows:
            d = row_dict(r)
            d["learned"] = bool(learned.get(d["id"]))
            items.append(d)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@app.get("/api/meta")
def get_meta(user=Depends(current_user)):
    """筛选/上传表单的选项源：字典表（active=1）∪ 病例实际已有值（兼容旧库），科室仍取病例 distinct"""
    with db() as conn:
        depts = [r[0] for r in conn.execute(
            "SELECT DISTINCT department FROM cases WHERE department!='' ORDER BY department")]
        def dict_of(cat: str) -> list:
            return [r[0] for r in conn.execute(
                "SELECT label FROM dict_items WHERE category=? AND active=1 "
                "ORDER BY CASE WHEN sort_no=0 THEN 9999 ELSE sort_no END, id", (cat,))]
        def merged(cat: str, col: str) -> list:
            vals = dict_of(cat)
            extra = [r[0] for r in conn.execute(
                f"SELECT DISTINCT {col} v FROM cases WHERE {col}!='' ORDER BY v")]
            return vals + [v for v in extra if v not in vals]
        diff_labels = {}
        for r in conn.execute(
                "SELECT code,label FROM dict_items WHERE category='difficulty' AND active=1 ORDER BY sort_no,id").fetchall():
            if str(r[0]) in ("1", "2", "3"):
                diff_labels[str(r[0])] = r[1]
        for k in ("1", "2", "3"):
            diff_labels.setdefault(k, {"1": "基础", "2": "进阶", "3": "疑难"}[k])
    return {"departments": depts, "systems": merged("system", "system"),
            "modalities": merged("modality", "modality"), "diseases": dict_of("disease"),
            "difficulty_labels": diff_labels}


@app.get("/api/cases/{case_id}")
def get_case(case_id: int, request: Request, user=Depends(current_user)):
    with db() as conn:
        c = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not c:
            raise HTTPException(404, "病例不存在")
        if user["role"] == "student" and c["status"] != "published":
            raise HTTPException(403, "病例未发布")
        qs = rows_list(conn.execute(
            "SELECT * FROM questions WHERE case_id=? ORDER BY sort_no,id", (case_id,)).fetchall())
    data = row_dict(c)
    if user["role"] == "student":
        with db() as conn:
            mine = conn.execute(
                "SELECT s.* FROM submissions s JOIN questions q ON q.id=s.question_id "
                "WHERE q.case_id=? AND s.student_id=?", (case_id, user["id"])).fetchall()
            by_q = {m["question_id"]: row_dict(m) for m in mine}
            p = conn.execute("SELECT * FROM progress WHERE student_id=? AND case_id=?",
                             (case_id, user["id"])).fetchone()
        data["note"] = p["note"] if p else ""
        # 盲读模式：作答完成前隐藏教学要点/参考诊断/参考答案，避免锚定依赖
        blind = bool(data.get("blind_mode", 1))
        if qs:
            answered_all = all(q["id"] in by_q for q in qs)
        else:
            answered_all = p is not None and (p["view_count"] or 0) >= 1
        if blind and not answered_all:
            data["teaching_points"] = None
            data["diagnosis"] = None
            data["locked"] = True
            data["locked_reason"] = ("盲读模式：完成本病例全部题目作答后解锁教学要点与参考诊断"
                                      if qs else "盲读模式：请先打开阅片器学习后解锁")
            qs = [{"id": q["id"], "question": q["question"], "score": q["score"],
                  "my_submission": by_q.get(q["id"])} for q in qs]
        else:
            qs = [{"id": q["id"], "question": q["question"], "score": q["score"],
                  "my_submission": by_q.get(q["id"]),
                  "reference_answer": q["reference_answer"]} for q in qs]
    else:
        with db() as conn:
            counts = {}
            for m in conn.execute(
                    "SELECT q.id, COUNT(s.id) n FROM questions q LEFT JOIN submissions s "
                    "ON s.question_id=q.id WHERE q.case_id=? GROUP BY q.id", (case_id,)).fetchall():
                counts[m["id"]] = m["n"]
        for q in qs:
            q["submission_count"] = counts.get(q["id"], 0)
    if user["role"] == "student":
        try:
            lti_xapi.track_event(request, "viewed", case_id=case_id, title=data.get("title", ""))
        except Exception:
            pass  # 学习记录不能因为 LRS 故障而中断阅课
    return data | {"questions": qs}


# ---------------------------------------------------------------- 脱敏引擎
DEID_UID_ROOT = "1.2.826.0.1.3680043.10.9999"

# 脱敏行为开关（教学价值 vs 隐私最小化的取舍点，可按院内安全要求调整）
DEID_CLEAR_AGE = True          # 清除 PatientAge（年龄教学价值低、辨识度中等）
DEID_KEEP_SEX = True           # 保留 PatientSex（乳腺等教学场景有诊断意义；置 False 删除）
DEID_KEEP_STUDY_DATE = True    # 保留 StudyDate（教学需要）；置 False 则抹平为固定日期

# 自由文本字段：设备常把患者姓名/住院号写进描述，需做"已知 PHI 字符串替换"
_DEID_TEXT_TAGS = [
    (0x0008, 0x1030),  # StudyDescription
    (0x0008, 0x103E),  # SeriesDescription
    (0x0018, 0x1030),  # ProtocolName
    (0x0008, 0x1080),  # AdmittingDiagnosesDescription
    (0x0008, 0x1010),  # StationName（设备名，可含院内信息）
    (0x0032, 0x4000),  # StudyComments
]

# 删除的 PHI 标签（患者隐私与机构/人员信息）
_DEID_DELETE_TAGS = [
    (0x0010, 0x0030),  # PatientBirthDate
    (0x0010, 0x1000),  # OtherPatientIDs
    (0x0010, 0x1001),  # OtherPatientNames
    (0x0010, 0x1002),  # OtherPatientIDsSequence
    (0x0010, 0x1040),  # PatientAddress
    (0x0010, 0x1050),  # InsurancePlanIdentification
    (0x0010, 0x1080),  # MilitaryRank
    (0x0010, 0x2000),  # MedicalAlerts
    (0x0010, 0x2110),  # Allergies
    (0x0010, 0x2150),  # CountryOfResidence
    (0x0010, 0x2152),  # RegionOfResidence
    (0x0010, 0x2154),  # PatientTelephoneNumbers
    (0x0010, 0x2201),  # EthnicGroup
    (0x0008, 0x0081),  # InstitutionAddress
    (0x0008, 0x1050),  # PerformingPhysicianName
    (0x0008, 0x1060),  # NameOfPhysiciansReadingStudy
    (0x0008, 0x1070),  # OperatorsName
    (0x0018, 0x1000),  # DeviceSerialNumber
]


def _next_teaching_code() -> str:
    """生成下一个教学编号，如 EDU26-0007"""
    yy = time.strftime("%y")
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] + 1
        used = {r[0] for r in conn.execute("SELECT patient_id FROM cases").fetchall()}
        used |= {r[0] for r in conn.execute("SELECT teaching_code FROM case_origin").fetchall()}
    code = f"EDU{yy}-{n:04d}"
    while code in used:
        n += 1
        code = f"EDU{yy}-{n:04d}"
    return code


def _strip_private(ds):
    """递归删除私有标签（可能隐藏院内私有 PHI）"""
    for elem in list(ds):
        if elem.tag.is_private:
            del ds[elem.tag]
        elif elem.VR == "SQ" and elem.value:
            for item in elem.value:
                _strip_private(item)


def _deid_dataset(raw: bytes, teaching_code: str,
                  study_map: dict, series_map: dict, phi_pool: dict = None) -> tuple:
    """读取原始 DICOM，返回（脱敏后字节, 溯源信息 dict）。
    phi_pool 跨文件共享：同一检查内已知 PHI 值（姓名/住院号等）用于清洗自由文本字段。"""
    if phi_pool is None:
        phi_pool = {}
    ds = pydicom.dcmread(io.BytesIO(raw), force=True)
    origin = {
        "source_patient_id": str(getattr(ds, "PatientID", "") or ""),
        "source_patient_name": str(getattr(ds, "PatientName", "") or ""),
        "source_patient_birth": str(getattr(ds, "PatientBirthDate", "") or ""),
        "source_accession": str(getattr(ds, "AccessionNumber", "") or ""),
        "source_study_uid": str(getattr(ds, "StudyInstanceUID", "") or ""),
        "source_institution": str(getattr(ds, "InstitutionName", "") or ""),
    }
    # 收集已知 PHI 值（供本检查所有文件的自由文本清洗）
    for v in (origin["source_patient_name"], origin["source_patient_id"],
              origin["source_patient_birth"], origin["source_accession"],
              origin["source_institution"]):
        if v and v not in phi_pool.get("_seen", set()):
            phi_pool.setdefault("_seen", set()).add(v)
    # 1) 替换患者标识为教学编号
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.PatientName = f"教学病例^{teaching_code}"
    ds.PatientID = teaching_code
    ds.AccessionNumber = teaching_code
    ds.StudyID = teaching_code
    ds.InstitutionName = "教学病例库（已脱敏）"
    ds.ReferringPhysicianName = "教学系统"
    # 1b) 自由文本字段清洗：已知 PHI 字符串替换（完整姓名 + 姓名^分段）
    name_variants = set()
    full_name = origin["source_patient_name"]
    if full_name:
        name_variants.add(full_name)
        for seg in full_name.split("^"):
            if seg.strip():
                name_variants.add(seg.strip())
    def _scrub_text(val) -> object:
        s = str(val)
        if not s.strip():
            return val
        for nm in name_variants:
            if nm and nm in s:
                s = s.replace(nm, "教学病例")
        for key in ("pid", "acc", "birth", "inst"):
            v = origin.get({"pid": "source_patient_id", "acc": "source_accession",
                            "birth": "source_patient_birth", "inst": "source_institution"}[key], "")
            pool_v = phi_pool.get(key)
            if pool_v:
                v = pool_v if len(str(pool_v)) > len(v) else v
            if v:
                s = s.replace(str(v), teaching_code if key in ("pid", "acc") else "")
        # 兜底：phi_pool 中其他已知 PHI 值（同检查其他文件带来的）
        for v in list(phi_pool.get("_seen", set())):
            if v and str(v) in s and str(v) != teaching_code:
                s = s.replace(str(v), "教学病例")
        return s
    for tag in _DEID_TEXT_TAGS:
        if tag in ds and ds[tag].VR in ("LO", "LT", "ST", "PN", "UT", "CS", "SH", "UN"):
            try:
                cur = ds[tag].value
                new = _scrub_text(cur)
                if new != cur:
                    ds[tag].value = new
            except Exception:
                pass
    # 1c) 年龄/性别按开关处理
    if DEID_CLEAR_AGE and (0x0010, 0x1010) in ds:
        del ds[(0x0010, 0x1010)]
    if not DEID_KEEP_SEX and (0x0010, 0x0040) in ds:
        del ds[(0x0010, 0x0040)]
    if not DEID_KEEP_STUDY_DATE and (0x0008, 0x0020) in ds:
        ds.StudyDate = "19000101"
    # 2) 删除 PHI 标签
    for tag in _DEID_DELETE_TAGS:
        if tag in ds:
            del ds[tag]
    # 3) 重写 UID（防止与院内 PACS 交叉比对），同批次保持映射一致
    old_study = str(getattr(ds, "StudyInstanceUID", "") or "")
    if old_study not in study_map:
        study_map[old_study] = generate_uid(prefix=DEID_UID_ROOT + ".2.")
    ds.StudyInstanceUID = study_map[old_study]
    old_series = str(getattr(ds, "SeriesInstanceUID", "") or "")
    if old_series not in series_map:
        series_map[old_series] = generate_uid(prefix=DEID_UID_ROOT + ".3.")
    ds.SeriesInstanceUID = series_map[old_series]
    ds.SOPInstanceUID = generate_uid(prefix=DEID_UID_ROOT + ".1.")
    # 4) 删除私有标签
    _strip_private(ds)
    # 5) 同步文件头
    if ds.file_meta is None:
        ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = str(getattr(ds, "SOPClassUID", ""))
    ds.file_meta.MediaStorageSOPInstanceUID = str(ds.SOPInstanceUID)
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue(), origin


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff"}


def _images_to_sc_dicoms(images: list, teaching_code: str, base_study_uid, m: dict) -> list:
    """把 jpg/png 等图片合成 Secondary Capture DICOM（同一检查、同一序列、每图一实例）。
    来源常见于教学论文/网络少见病例截图。"""
    from PIL import Image
    out: list = []
    study_uid = base_study_uid or generate_uid(prefix=DEID_UID_ROOT + ".4.")
    series_uid = generate_uid(prefix=DEID_UID_ROOT + ".3.")
    title = m.get("title") or f"教学图片病例_{time.strftime('%Y%m%d_%H%M%S')}"
    modality = m.get("modality") or "OT"
    now_date = time.strftime("%Y%m%d")
    now_time = time.strftime("%H%M%S")
    for idx, (fname, raw) in enumerate(images, start=1):
        try:
            im = Image.open(io.BytesIO(raw))
            im.load()
            if im.mode not in ("RGB", "L", "RGBA", "P"):
                im = im.convert("RGB")
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            if im.mode == "L":
                samples, photo, arr = 1, "MONOCHROME2", np.asarray(im, dtype=np.uint8)
            else:
                samples, photo, arr = 3, "RGB", np.asarray(im.convert("RGB"), dtype=np.uint8)
        except Exception as e:
            raise HTTPException(400, f"图片「{fname}」无法解码：{e}")
        h, w = arr.shape[0], arr.shape[1]
        if not (0 < w <= 65535 and 0 < h <= 65535):
            raise HTTPException(400, f"图片「{fname}」尺寸异常（{w}x{h}）")
        ds = Dataset()
        ds.SOPClassUID = SecondaryCaptureImageStorage
        ds.SOPInstanceUID = generate_uid(prefix=DEID_UID_ROOT + ".1.")
        ds.PatientName = f"教学病例^{teaching_code}"
        ds.PatientID = teaching_code
        ds.PatientSex = "O"
        ds.SpecificCharacterSet = "ISO_IR 192"
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.StudyDate, ds.StudyTime = now_date, now_time
        ds.SeriesDate, ds.SeriesTime = now_date, now_time
        ds.AccessionNumber = teaching_code
        ds.StudyID = teaching_code
        ds.Modality = modality
        ds.ReferringPhysicianName = "教学系统"
        ds.StudyDescription = title[:60]
        ds.SeriesDescription = f"教学图片{len(images)}张" if len(images) > 1 else "教学图片"
        ds.PatientBirthDate = ""
        ds.ImageType = ["DERIVED", "SECONDARY"]
        ds.InstanceNumber = idx
        ds.SeriesNumber = 1
        ds.ConversionType = "WSD"
        ds.SamplesPerPixel = samples
        ds.PhotometricInterpretation = photo
        ds.Rows, ds.Columns = h, w
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
        ds.PixelRepresentation = 0
        if samples == 3:
            ds.PlanarConfiguration = 0
        if im.mode == "RGB":
            # 常规窗宽窗位便于 OHIF 直接显示
            ds.WindowCenter = 128
            ds.WindowWidth = 256
        ds.SmallestImagePixelValue = int(arr.min())
        ds.LargestImagePixelValue = int(arr.max())
        ds.PixelData = arr.tobytes()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
        ds.file_meta.MediaStorageSOPInstanceUID = str(ds.SOPInstanceUID)
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        out.append(buf.getvalue())
    return out


@app.post("/api/cases/upload")
async def upload_case(
    meta: str = Form("{}"),
    files: List[UploadFile] = File(...),
    user=Depends(require_roles("admin", "teacher")),
):
    try:
        m = json.loads(meta or "{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "meta 格式错误")
    session = uuid.uuid4().hex
    workdir = UPLOAD_TMP / session
    workdir.mkdir(parents=True, exist_ok=True)
    dicom_files: List[Path] = []
    image_files: list = []   # [(文件名, 字节)] 支持 jpg/png 等图片（少见病例论文/网络截图）
    try:
        for f in files:
            safe = sanitize_filename(f.filename)
            p = workdir / safe
            with p.open("wb") as out:
                while chunk := await f.read(4 * 1024 * 1024):
                    out.write(chunk)
            if zipfile.is_zipfile(p):
                zdir = workdir / (safe + "_unz")
                with zipfile.ZipFile(p) as zf:
                    zf.extractall(zdir)
                for cand in zdir.rglob("*"):
                    if not cand.is_file():
                        continue
                    if cand.suffix.lower() in IMAGE_EXTS:
                        image_files.append((cand.name, cand.read_bytes()))
                    elif cand.suffix.lower() in (".dcm", ".dicom", ""):
                        dicom_files.append(cand)
            elif p.suffix.lower() in IMAGE_EXTS:
                image_files.append((p.name, p.read_bytes()))
            else:
                dicom_files.append(p)
        if not dicom_files and not image_files:
            raise HTTPException(400, "未找到可上传的文件（支持 .dcm / .zip / jpg / png 等图片）")

        # 入库即脱敏：清洗 PHI、替换教学编号、重写 UID；图片先合成 SC DICOM 再走同一管线
        teaching_code = _next_teaching_code()
        study_map: dict = {}
        series_map: dict = {}
        phi_pool: dict = {}
        scrubbed: list = []   # [(bytes, origin_info)]
        parse_failed = []
        for p in dicom_files:
            try:
                data_bytes, origin = _deid_dataset(p.read_bytes(), teaching_code, study_map, series_map, phi_pool)
                scrubbed.append((data_bytes, origin))
            except Exception:
                parse_failed.append(p.name)
        base_study_uid = next(iter(study_map)) if study_map else None
        if image_files:
            # burned-in PHI 责任确认：论文/网络截图可能把患者信息烧录在像素内，需上传者确认
            if not m.get("confirm_no_phi"):
                raise HTTPException(400, "图片病例需先勾选“确认图像内无患者可识别信息（burned-in PHI）”")
            sc_list = _images_to_sc_dicoms(image_files, teaching_code, base_study_uid, m)
            for sc in sc_list:
                try:
                    scrubbed.append(_deid_dataset(sc, teaching_code, study_map, series_map, phi_pool))
                except Exception:
                    parse_failed.append("合成图片")
        if not scrubbed:
            raise HTTPException(400, "无法解析任何文件（脱敏前置校验失败），请确认文件为标准 DICOM 或常见图片格式")

        study_ids: dict = {}
        uploaded, failed = 0, 0
        for data_bytes, _origin in scrubbed:
            try:
                r = http_client.post(ORTHANC_URL + "/instances", data=data_bytes,
                                     headers={"Content-Type": "application/dicom"},
                                     timeout=ORTHANC_TIMEOUT, auth=ORTHANC_AUTH)
                if r.status_code == 200:
                    resp = r.json()
                    # 部分 Orthanc 版本/重复实例响应的 JSON 里没有 ParentStudy，
                    # 直接下标访问会抛 KeyError → 裸 500。改为健壮解析。
                    parent = resp.get("ParentStudy") if isinstance(resp, dict) else None
                    if parent:
                        study_ids[parent] = True
                        uploaded += 1
                    elif resp.get("AlreadyExists") and isinstance(resp["AlreadyExists"], dict):
                        # 重复上传已存在实例：Orthanc 返回 {"AlreadyExists": {"ID": ..., "ParentStudy": ...}}
                        parent2 = resp["AlreadyExists"].get("ParentStudy")
                        if parent2:
                            study_ids[parent2] = True
                            uploaded += 1
                        else:
                            failed += 1
                    else:
                        failed += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        if not study_ids:
            raise HTTPException(400, f"上传失败：Orthanc 未接收任何文件（成功0，失败{failed}），请确认已启动 Orthanc")
        if len(study_ids) > 1:
            raise HTTPException(400, f"一次请只上传一个检查（检测到 {len(study_ids)} 个不同研究）")

        sid = next(iter(study_ids))
        origin = scrubbed[0][1]
        deid_note = (("上传时自动脱敏（清洗 PHI 标签与描述文本、重写 UID）") if dicom_files
                     else "图片合成 Secondary Capture DICOM 后入库（上传者已确认无 burned-in PHI）")
        case_id, info = _create_case_record(sid, m, user, teaching_code, origin, deid_note)
        return {"id": case_id, "uploaded": uploaded, "failed": failed,
                "parse_failed": parse_failed, "teaching_code": teaching_code,
                "series_count": info["series_count"], "image_count": info["image_count"],
                "image_import": bool(image_files)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class CaseBody(BaseModel):
    title: Optional[str] = None
    department: Optional[str] = None
    system: Optional[str] = None
    disease: Optional[str] = None
    modality: Optional[str] = None
    difficulty: Optional[int] = None
    tags: Optional[str] = None
    teaching_points: Optional[str] = None
    diagnosis: Optional[str] = None
    status: Optional[str] = None
    blind_mode: Optional[bool] = None
    patient_age: Optional[str] = None
    patient_sex: Optional[str] = None
    chief_complaint: Optional[str] = None
    medical_history: Optional[str] = None
    clinical_info: Optional[str] = None


@app.put("/api/cases/{case_id}")
def update_case(case_id: int, body: CaseBody, user=Depends(require_roles("admin", "teacher"))):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "没有需要更新的字段")
    if "status" in fields and fields["status"] not in ("draft", "published"):
        raise HTTPException(400, "状态不合法")
    if "difficulty" in fields:
        fields["difficulty"] = max(1, min(int(fields["difficulty"]), 3))
    if "blind_mode" in fields:
        fields["blind_mode"] = int(bool(fields["blind_mode"]))
    for k in ANON_TEXT_LIMITS:
        if k in fields:
            fields[k] = clean_anon_text(fields[k], k)
    sets = ",".join(f"{k}=?" for k in fields)
    args = list(fields.values()) + [case_id]
    with db() as conn:
        conn.execute(f"UPDATE cases SET {sets}, updated_at=datetime('now','localtime') WHERE id=?", args)
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "病例不存在")
    return row_dict(row)


@app.get("/api/cases/{case_id}/origin")
def get_origin(case_id: int, user=Depends(require_roles("admin", "teacher"))):
    """溯源信息：仅教师/管理员可见；学生端无此接口权限"""
    with db() as conn:
        row = conn.execute("SELECT * FROM case_origin WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            return {"exists": False, "note": "该病例早于脱敏功能入库（演示样例）或未记录溯源信息"}
        return {"exists": True} | row_dict(row)


@app.delete("/api/cases/{case_id}")
def delete_case(case_id: int, user=Depends(require_roles("admin", "teacher"))):
    with db() as conn:
        c = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not c:
            raise HTTPException(404, "病例不存在")
        conn.execute("DELETE FROM cases WHERE id=?", (case_id,))
    if c["orthanc_study_id"]:
        try:
            http_client.delete(f"{ORTHANC_URL}/studies/{c['orthanc_study_id']}",
                               timeout=ORTHANC_TIMEOUT, auth=ORTHANC_AUTH)
        except Exception:
            pass
    return {"ok": True}


# ---------------------------------------------------------------- 教学问答
class QuestionBody(BaseModel):
    question: str
    reference_answer: str = ""
    score: float = 10
    sort_no: int = 0


@app.post("/api/cases/{case_id}/questions")
def add_question(case_id: int, body: QuestionBody, user=Depends(require_roles("admin", "teacher"))):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone():
            raise HTTPException(404, "病例不存在")
        cur = conn.execute(
            "INSERT INTO questions(case_id,question,reference_answer,score,sort_no) VALUES(?,?,?,?,?)",
            (case_id, body.question, body.reference_answer, body.score, body.sort_no))
        return {"id": cur.lastrowid}


@app.delete("/api/questions/{qid}")
def delete_question(qid: int, user=Depends(require_roles("admin", "teacher"))):
    with db() as conn:
        conn.execute("DELETE FROM questions WHERE id=?", (qid,))
    return {"ok": True}


class AnswerBody(BaseModel):
    answer: str


@app.post("/api/questions/{qid}/submit")
def submit_answer(qid: int, body: AnswerBody, request: Request, user=Depends(require_roles("student"))):
    with db() as conn:
        q = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if not q:
            raise HTTPException(404, "题目不存在")
        old = conn.execute(
            "SELECT id FROM submissions WHERE question_id=? AND student_id=?", (qid, user["id"])).fetchone()
        if old:
            conn.execute(
                "UPDATE submissions SET answer=?, status='submitted', score=NULL, feedback='', "
                "submitted_at=datetime('now','localtime') WHERE id=?", (body.answer, old["id"]))
            sid = old["id"]
        else:
            cur = conn.execute(
                "INSERT INTO submissions(question_id,student_id,answer) VALUES(?,?,?)",
                (qid, user["id"], body.answer))
            sid = cur.lastrowid
    try:
        lti_xapi.track_event(request, "answered", case_id=q["case_id"], question_id=qid,
                             response=body.answer, title=q["question"])
    except Exception:
        pass
    return {"id": sid}


@app.get("/api/cases/{case_id}/submissions")
def list_submissions(case_id: int, user=Depends(require_roles("admin", "teacher"))):
    with db() as conn:
        rows = conn.execute(
            """SELECT s.id, s.question_id, s.answer, s.score, s.feedback, s.status, s.submitted_at,
                      u.id student_id, u.username, u.full_name, u.class_name,
                      q.question, q.reference_answer, q.score max_score
               FROM submissions s
               JOIN questions q ON q.id=s.question_id
               JOIN users u ON u.id=s.student_id
               WHERE q.case_id=? ORDER BY q.sort_no, u.id""", (case_id,)).fetchall()
    return rows_list(rows)


class GradeBody(BaseModel):
    score: float
    feedback: str = ""


@app.post("/api/submissions/{sid}/grade")
def grade_submission(sid: int, body: GradeBody, request: Request, user=Depends(require_roles("admin", "teacher"))):
    with db() as conn:
        s = conn.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "提交不存在")
        q = conn.execute("SELECT score FROM questions WHERE id=?", (s["question_id"],)).fetchone()
        score = max(0.0, min(float(body.score), float(q["score"]) if q else body.score))
        conn.execute(
            "UPDATE submissions SET score=?, feedback=?, status='graded', graded_at=datetime('now','localtime'), "
            "graded_by=? WHERE id=?", (score, body.feedback, user["username"], sid))
    # 成绩回传 Moodle（AGS）与 xAPI：失败只记日志，不影响批阅成功
    try:
        max_score = float(q["score"]) if q else score
        lti_xapi.post_grade_for_user(s["student_id"], score, max_score, body.feedback)
        lti_xapi.track_event(request, "graded", user_id=s["student_id"],
                             question_id=s["question_id"], score=score,
                             max_score=max_score, title="教师批阅")
    except Exception:
        pass
    return {"ok": True, "score": score}


# ---------------------------------------------------------------- 学习记录
class ProgressBody(BaseModel):
    case_id: int
    note: str = ""


@app.post("/api/progress")
def touch_progress(body: ProgressBody, request: Request, user=Depends(require_roles("student"))):
    with db() as conn:
        row = conn.execute("SELECT * FROM progress WHERE student_id=? AND case_id=?",
                           (user["id"], body.case_id)).fetchone()
        if row:
            conn.execute(
                "UPDATE progress SET view_count=view_count+1, last_viewed=datetime('now','localtime'), note=? "
                "WHERE id=?", (body.note or row["note"], row["id"]))
        else:
            conn.execute("INSERT INTO progress(student_id,case_id,note,view_count) VALUES(?,?,?,1)",
                         (user["id"], body.case_id, body.note))
        p = conn.execute("SELECT * FROM progress WHERE student_id=? AND case_id=?",
                         (user["id"], body.case_id)).fetchone()
    try:
        lti_xapi.track_event(request, "viewer_opened", case_id=body.case_id)
    except Exception:
        pass
    return row_dict(p)


@app.get("/api/my/progress")
def my_progress(user=Depends(require_roles("student"))):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM cases WHERE status='published'").fetchone()[0]
        learned = conn.execute("SELECT COUNT(*) FROM progress WHERE student_id=?", (user["id"],)).fetchone()[0]
        rows = conn.execute(
            """SELECT p.case_id, c.title, c.system, c.difficulty, p.view_count, p.last_viewed, p.note
               FROM progress p JOIN cases c ON c.id=p.case_id
               WHERE p.student_id=? ORDER BY p.last_viewed DESC""", (user["id"],)).fetchall()
    return {"total": total, "learned": learned, "items": rows_list(rows)}


# ---------------------------------------------------------------- 统计
@app.get("/api/stats/overview")
def stats_overview(user=Depends(current_user)):
    with db() as conn:
        case_total = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        published = conn.execute("SELECT COUNT(*) FROM cases WHERE status='published'").fetchone()[0]
        by_dept = rows_list(conn.execute(
            "SELECT department name, COUNT(*) value FROM cases GROUP BY department ORDER BY value DESC").fetchall())
        by_modality = rows_list(conn.execute(
            "SELECT modality name, COUNT(*) value FROM cases WHERE modality!='' GROUP BY modality ORDER BY value DESC").fetchall())
        by_difficulty = rows_list(conn.execute(
            "SELECT difficulty name, COUNT(*) value FROM cases GROUP BY difficulty ORDER BY name").fetchall())
        student_total = conn.execute("SELECT COUNT(*) FROM users WHERE role='student' AND active=1").fetchone()[0]
        learned_pairs = conn.execute("SELECT COUNT(*) FROM progress").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM submissions WHERE status='submitted'").fetchone()[0]
        graded = conn.execute("SELECT COUNT(*) FROM submissions WHERE status='graded'").fetchone()[0]
        avg_score = conn.execute("SELECT AVG(score) FROM submissions WHERE status='graded'").fetchone()[0]
        top_students = rows_list(conn.execute(
            """SELECT u.full_name, u.username, u.class_name, COUNT(p.case_id) learned
               FROM users u LEFT JOIN progress p ON p.student_id=u.id
               WHERE u.role='student' AND u.active=1
               GROUP BY u.id ORDER BY learned DESC LIMIT 10""").fetchall())
    return {"case_total": case_total, "published": published, "student_total": student_total,
            "by_department": by_dept, "by_modality": by_modality, "by_difficulty": by_difficulty,
            "learned_pairs": learned_pairs, "pending_grade": pending, "graded": graded,
            "avg_score": round(avg_score, 1) if avg_score is not None else None,
            "top_students": top_students}


@app.get("/api/stats/students")
def stats_students(user=Depends(require_roles("admin", "teacher"))):
    with db() as conn:
        total_cases = conn.execute("SELECT COUNT(*) FROM cases WHERE status='published'").fetchone()[0]
        rows = conn.execute(
            """SELECT u.id, u.username, u.full_name, u.class_name, u.dept,
                      (SELECT COUNT(*) FROM progress p WHERE p.student_id=u.id) learned,
                      (SELECT COUNT(*) FROM submissions s WHERE s.student_id=u.id) answers,
                      (SELECT AVG(s.score) FROM submissions s JOIN questions q ON q.id=s.question_id
                        WHERE s.student_id=u.id AND s.status='graded') avg_score
               FROM users u WHERE u.role='student' AND u.active=1 ORDER BY u.class_name, u.id""").fetchall()
    items = []
    for r in rows:
        d = row_dict(r)
        d["avg_score"] = round(d["avg_score"], 1) if d["avg_score"] is not None else None
        d["progress_pct"] = round(100 * d["learned"] / total_cases, 1) if total_cases else 0
        items.append(d)
    return {"total_cases": total_cases, "items": items}


# ---------------------------------------------------------------- 前端静态资源
init_db()  # 模块导入时确保数据库就绪（幂等）


@app.get("/api/config")
def front_config():
    return {"orthanc_origin": ORTHANC_URL, "viewer_base": ORTHANC_URL + "/ohif",
            "lti_enabled": lti_xapi._enabled(),
            "xapi_enabled": bool(lti_xapi.XAPICFG.get("enabled") and lti_xapi.XAPICFG.get("endpoint"))}


@app.get("/favicon.ico")
def favicon():
    path = STATIC_DIR / "favicon.ico"
    if path.exists():
        return FileResponse(path)
    raise HTTPException(404)


# ---------------------------------------------------------------- OHIF / DICOMweb 反向代理（方案 B）
# 浏览器只访问 8000：/ohif/* 与 /dicom-web/* 由本代理转发到本机 Orthanc（自动附带服务账号认证）。
# Orthanc 侧 RemoteAccessAllowed=false + AuthenticationEnabled=true，8042 不再对外暴露。
# 安全约束：代理仅放行 GET/HEAD 只读请求（QIDO/WADO 阅片所需）；POST/PUT/DELETE 一律 405，
# 防止未登录客户端借服务账号凭据向 Orthanc 写入/删除（STOW-RS 等）；写入只由教学系统上传通道在服务端执行。
_PROXY_FORWARD_HEADERS = ("Accept", "Content-Type", "Range", "If-None-Match",
                          "If-Modified-Since", "Origin", "Referer", "User-Agent")
_PROXY_RESP_HEADERS = ("Content-Type", "Content-Length", "Content-Encoding",
                       "Last-Modified", "ETag", "Cache-Control", "Content-Range")


async def _proxy_to_orthanc(request: Request, target_path: str):
    url = ORTHANC_URL + target_path
    if request.url.query:
        url += "?" + str(request.url.query)
    fwd = {k: request.headers.get(k) for k in _PROXY_FORWARD_HEADERS if request.headers.get(k)}
    body = await request.body()
    try:
        up = http_client.request(request.method, url, headers=fwd,
                                 data=body if body else None, stream=True,
                                 auth=ORTHANC_AUTH, timeout=ORTHANC_TIMEOUT)
    except Exception as e:
        raise HTTPException(502, f"Orthanc 不可达：{str(e)[:150]}")

    def gen():
        try:
            for chunk in up.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            up.close()

    out = {k: up.headers[k] for k in _PROXY_RESP_HEADERS if k in up.headers}
    # 安全响应头：阅片页面在 iframe 中运行，禁止被第三方站点嵌套/引用
    out.setdefault("X-Content-Type-Options", "nosniff")
    out.setdefault("X-Frame-Options", "SAMEORIGIN")
    # 去掉会阻止 iframe 嵌入/触发浏览器认证弹窗的头（认证由代理透明完成）
    return StreamingResponse(gen(), status_code=up.status_code, headers=out,
                             media_type=up.headers.get("Content-Type"))


@app.api_route("/ohif", methods=["GET", "HEAD"])
async def proxy_ohif_root(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/ohif/", status_code=307)


# OHIF 3.13 的 bundle 使用了 ES2022 静态初始化块（static { }），Chrome/Edge 85~90
# 会在此处抛 SyntaxError: Unexpected token '{'。在同源代理层降级为静态字段 IIFE，
# 语法等价且兼容 Chrome/Edge 80+。同时屏蔽 HTTP 环境下 serviceWorker 未定义的报错。
_OHIF_JS_CACHE: dict = {}


def _downlevel_ohif_js(text: str) -> tuple:
    out, i, n = [], 0, 0
    while True:
        j = text.find("static {", i)
        if j < 0:
            out.append(text[i:])
            break
        k, depth, quote, esc = j + 8, 1, None, False   # len("static {") == 8
        while depth and k < len(text):
            ch = text[k]
            if quote:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    quote = None
            else:
                if ch in "'\"`":
                    quote = ch
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            k += 1
        body = text[j + 8:k - 1]
        n += 1
        out.append(text[i:j])
        out.append(f"static __ohifStaticInit{n} = (() => {{{body}}})();")
        i = k
    patched = "".join(out)
    if "navigator.serviceWorker.getRegistrations" in patched:
        patched = patched.replace(
            "navigator.serviceWorker.getRegistrations()",
            "(navigator.serviceWorker ? navigator.serviceWorker.getRegistrations() : Promise.resolve([]))",
            1,
        )
    return patched, n


async def _proxy_ohif_js(request: Request, path: str):
    url = ORTHANC_URL + "/ohif/" + path
    if request.url.query:
        url += "?" + str(request.url.query)
    try:
        up = http_client.get(url, auth=ORTHANC_AUTH, timeout=ORTHANC_TIMEOUT)
    except Exception as e:
        raise HTTPException(502, f"Orthanc 不可达：{str(e)[:150]}")
    key = (path, up.headers.get("ETag"), len(up.content))
    cached = _OHIF_JS_CACHE.get(key)
    if cached is None:
        patched, _ = _downlevel_ohif_js(up.content.decode("utf-8", "replace"))
        cached = patched.encode("utf-8")
        _OHIF_JS_CACHE.clear()
        _OHIF_JS_CACHE[key] = cached
    return Response(content=b"" if request.method == "HEAD" else cached,
                    status_code=up.status_code,
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.api_route("/ohif/{path:path}", methods=["GET", "HEAD"])
async def proxy_ohif(path: str, request: Request):
    # 同 dicom-web 代理：拒绝 .. 与百分号编码穿越，防止折叠后越过 /ohif 前缀
    decoded = urllib.parse.unquote(path)
    if any(seg == ".." for seg in decoded.lower().split("/")):
        raise HTTPException(404, "ohif 代理禁止路径穿越")
    if path.endswith(".js"):
        return await _proxy_ohif_js(request, path)
    return await _proxy_to_orthanc(request, f"/ohif/{path}")


@app.api_route("/dicom-web", methods=["GET", "HEAD"])
async def proxy_dicomweb_root(request: Request):
    return await _proxy_to_orthanc(request, "/dicom-web")


@app.api_route("/dicom-web/{path:path}", methods=["GET", "HEAD"])
async def proxy_dicomweb(path: str, request: Request):
    # 只读路径白名单：仅放行 QIDO/WADO 的 studies/series/instances 子路径。
    # 安全：必须先解码百分号编码再检查 .. 段，防止 %2e%2e / ..%2f 绕过；
    # 并拒绝任何 .. 段，防止 HTTP 客户端/代理把 /dicom-web/studies/../../system
    # 折叠成 Orthanc 管理端点 /system（白名单形同虚设）。
    decoded = urllib.parse.unquote(path)
    segments = decoded.lower().split("/")
    if any(seg == ".." for seg in segments):
        raise HTTPException(404, "dicom-web 代理禁止路径穿越")
    if not re.match(r"^(studies|series|instances)(/|$)", decoded.strip().lower()):
        raise HTTPException(404, "dicom-web 代理仅放行 studies/series/instances 只读子路径")
    return await _proxy_to_orthanc(request, f"/dicom-web/{path}")


# ---------------------------------------------------------------- 备份与恢复（admin）
import threading

try:
    from app.backend import backup_core as bc
    BACKUP_AVAILABLE = bc.CRYPTO_OK
except Exception:                                        # pragma: no cover
    BACKUP_AVAILABLE = False

_bk_job = {"id": None, "running": False, "stage": "", "pct": 0.0, "msg": "",
           "error": None, "result": None, "cancel": False}
_bk_lock = threading.Lock()


def _bk_public_job():
    """任务快照（不含任何密码）"""
    return {k: _bk_job.get(k) for k in ("id", "running", "stage", "pct", "msg", "error", "result")}


def _bk_safe_archive(dest_id: str, name: str) -> Path:
    """把（目的地 id, 文件名）安全解析为归档绝对路径，防目录穿越"""
    if BACKUP_AVAILABLE:
        cfg = bc.load_config()
        d = bc.get_dest(cfg, dest_id)
        if not d:
            raise HTTPException(404, f"未知备份目的地：{dest_id}")
        dest_path = bc.resolve_dest_path(d)
        if not re.fullmatch(r"[\w\-.]+\.edubak", name or ""):
            raise HTTPException(400, "非法归档文件名")
        p = (dest_path / name).resolve()
        if p.parent != dest_path.resolve():
            raise HTTPException(400, "非法归档路径")
        if not p.is_file():
            raise HTTPException(404, "归档不存在")
        return p
    raise HTTPException(503, "备份模块不可用（缺少 cryptography 依赖）")


@app.get("/api/backup/state")
def backup_state(user: sqlite3.Row = Depends(require_roles("admin"))):
    if not BACKUP_AVAILABLE:
        raise HTTPException(503, "备份模块不可用：请安装 cryptography（pip install -r requirements.lock.txt）")
    cfg = bc.load_config()
    return {
        "available": True,
        "config": cfg,
        "capability": {"can_orchestrate": bc.can_orchestrate(),
                       "orthanc_running": bc.orthanc_running()},
        "last": bc.load_last_state(),
        "job": _bk_public_job(),
    }


@app.post("/api/backup/run")
def backup_run(payload: dict, user: sqlite3.Row = Depends(require_roles("admin"))):
    if not BACKUP_AVAILABLE:
        raise HTTPException(503, "备份模块不可用（缺少 cryptography 依赖）")
    with _bk_lock:
        if _bk_job["running"]:
            raise HTTPException(409, "已有备份任务在运行，请等待完成或取消")
        dest_id = (payload or {}).get("dest", "local")
        password = (payload or {}).get("password", "")
        note = (payload or {}).get("note", "")
        mode = (payload or {}).get("mode", "auto")
        if mode not in ("auto", "hot"):
            mode = "auto"
        cfg = bc.load_config()
        dest = bc.get_dest(cfg, dest_id)
        if not dest:
            raise HTTPException(404, f"未知备份目的地：{dest_id}")
        job = {"id": f"bk-{int(time.time())}", "running": True, "stage": "init",
               "pct": 0.0, "msg": "准备备份……", "error": None, "result": None, "cancel": False}
        _bk_job.update(job)

        def _cb(stage, pct, msg):
            _bk_job.update(stage=stage, pct=float(pct or 0), msg=msg)

        def _worker():
            try:
                r = bc.create_backup(dest, password, note=note, mode=mode,
                                      progress=_cb, cancel=lambda: _bk_job["cancel"])
                _bk_job.update(running=False, stage="done", pct=100.0,
                               msg=f"备份完成：{r['file']}", result=r)
            except Exception as e:
                _bk_job.update(running=False, stage="error", error=str(e),
                               msg="备份失败")
        threading.Thread(target=_worker, daemon=True).start()
        return {"job_id": _bk_job["id"]}


@app.post("/api/backup/cancel")
def backup_cancel(user: sqlite3.Row = Depends(require_roles("admin"))):
    if _bk_job["running"]:
        _bk_job["cancel"] = True
        _bk_job["msg"] = "正在取消（将在当前阶段边界停止）……"
    return {"job": _bk_public_job()}


@app.get("/api/backup/progress")
def backup_progress(user: sqlite3.Row = Depends(require_roles("admin"))):
    return {"job": _bk_public_job()}


@app.get("/api/backup/archives")
def backup_archives(dest: str = "local", user: sqlite3.Row = Depends(require_roles("admin"))):
    if not BACKUP_AVAILABLE:
        raise HTTPException(503, "备份模块不可用（缺少 cryptography 依赖）")
    cfg = bc.load_config()
    d = bc.get_dest(cfg, dest)
    if not d:
        raise HTTPException(404, f"未知备份目的地：{dest}")
    dest_path = bc.resolve_dest_path(d)
    return {"dest": dest, "path": str(dest_path), "archives": bc.list_archives(dest_path)}


@app.post("/api/backup/verify")
def backup_verify(payload: dict, user: sqlite3.Row = Depends(require_roles("admin"))):
    p = _bk_safe_archive((payload or {}).get("dest", "local"), (payload or {}).get("name", ""))
    r = bc.verify_archive(p, (payload or {}).get("password", ""))
    return r


@app.post("/api/backup/analyze")
def backup_analyze(payload: dict, user: sqlite3.Row = Depends(require_roles("admin"))):
    p = _bk_safe_archive((payload or {}).get("dest", "local"), (payload or {}).get("name", ""))
    r = bc.restore_archive(p, (payload or {}).get("password", ""), apply=False)
    return r


@app.post("/api/backup/prune")
def backup_prune(payload: dict, user: sqlite3.Row = Depends(require_roles("admin"))):
    if not BACKUP_AVAILABLE:
        raise HTTPException(503, "备份模块不可用（缺少 cryptography 依赖）")
    cfg = bc.load_config()
    d = bc.get_dest(cfg, (payload or {}).get("dest", "local"))
    if not d:
        raise HTTPException(404, "未知备份目的地")
    keep = payload.get("keep") or d.get("keep", 7)
    removed = bc.prune_dest(bc.resolve_dest_path(d), int(keep))
    return {"removed": removed, "keep": int(keep)}


@app.post("/api/backup/config")
def backup_config_save(payload: dict, user: sqlite3.Row = Depends(require_roles("admin"))):
    if not BACKUP_AVAILABLE:
        raise HTTPException(503, "备份模块不可用（缺少 cryptography 依赖）")
    dests = (payload or {}).get("destinations")
    if not isinstance(dests, list) or not dests:
        raise HTTPException(400, "destinations 不能为空")
    seen = set()
    for d in dests:
        if not isinstance(d, dict):
            raise HTTPException(400, "目的地格式错误")
        did = str(d.get("id", "")).strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", did) or did in seen:
            raise HTTPException(400, f"目的地 id 非法或重复：{did!r}")
        seen.add(did)
        if not str(d.get("path", "")).strip():
            raise HTTPException(400, f"目的地 {did} 缺少路径")
        try:
            keep = int(d.get("keep", 7))
            assert keep >= 1 and keep <= 365
        except Exception:
            raise HTTPException(400, f"目的地 {did} 的 keep 需为 1-365")
        d["id"], d["keep"], d["enabled"] = did, keep, bool(d.get("enabled", False))
        d.setdefault("label", did)
        d.setdefault("type", "path")
    bc.save_config({"version": 1, "destinations": dests})
    return {"ok": True, "config": bc.load_config()}


# LTI 1.3 / xAPI 路由必须在静态目录挂载之前注册，否则会被 SPA 兜底路由吞掉
app.include_router(lti_xapi.router)

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    init_db()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
