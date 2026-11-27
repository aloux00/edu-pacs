# -*- coding: utf-8 -*-
"""EduPACS · Moodle LTI 1.3 + xAPI 集成模块

设计原则：
1. EduPACS 作为 LTI 1.3 Tool，Moodle 作为 Platform；无需给 Moodle 装额外插件，
   使用 Moodle 内置「外部工具（LTI 1.3）」即可。
2. 所有 URL / client_id / deployment_id / LRS 凭据均来自 config/*.json 或环境变量，
   代码内不硬编码任何站点地址或密钥。
3. 不向 xAPI / LRS 发送患者可识别信息；默认使用匿名 actor（LTI sub 的哈希）。
4. 所有新表均为增量迁移，旧库、旧部署、非 LTI 使用方式完全不受影响。
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests as http_client
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# ---------------------------------------------------------------- LTI 声明常量
LTI_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/"
DL_SETTINGS = "https://purl.imsglobal.org/spec/lti-dl/claim/deep_linking_settings"
DL_DATA = "https://purl.imsglobal.org/spec/lti-dl/claim/data"
DL_CONTENT = "https://purl.imsglobal.org/spec/lti-dl/claim/content_items"
AGS_ENDPOINT = "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"
AGS_SCORE_SCOPE = "https://purl.imsglobal.org/spec/lti-ags/scope/score"
COOKIE_SID = "edupacs_lti_sid"

router = APIRouter()

# ---------------------------------------------------------------- 由 main.py 注入
ROOT: Path = Path(".")
DB = None
MAKE_TOKEN = None
HASH_PASSWORD = None
ROW_DICT = None
SECRET: bytes = b""
LTICFG: Dict[str, Any] = {}
XAPICFG: Dict[str, Any] = {}
LTI_KEY_PATH: Optional[Path] = None
_discovery_cache: Dict[str, tuple] = {}
_jwks_cache: Dict[str, tuple] = {}
_ags_token_cache: Dict[str, tuple] = {}
_stop_event = threading.Event()
_worker_started = False


# ---------------------------------------------------------------- 基础工具
def _log(msg: str) -> None:
    try:
        p = Path(os.environ.get("EDUPACS_INTEGRATIONS_LOG", ROOT / "app" / "data" / "integrations.log"))
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _load_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return dict(default)
    except Exception as e:
        _log(f"配置文件读取失败 {path}: {e}")
        return dict(default)


def _b64u_enc(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/")


def _server_url(url: str) -> str:
    """服务端回调地址映射：浏览器用 localhost，容器内要走 compose 服务名。

    EDUPACS_LTI_SERVER_HOST_MAP 形如：
        http://localhost:8080=http://moodle,http://localhost:8000=http://app
    只影响后端主动发起的请求（JWKS / token / AGS），不影响 ID Token 的 iss 校验。
    """
    text = _norm_url(url)
    raw = os.environ.get("EDUPACS_LTI_SERVER_HOST_MAP", "")
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        external, internal = pair.split("=", 1)
        external = external.strip().rstrip("/")
        internal = internal.strip().rstrip("/")
        if not external or not internal:
            continue
        if text == external or text.startswith(external + "/"):
            text = internal + text[len(external):]
            break
    return text


def _hash_id(*parts: str) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _enabled() -> bool:
    return bool(LTICFG.get("enabled")) and bool(LTICFG.get("registrations"))


def _ttl() -> int:
    return max(300, int(LTICFG.get("session_ttl_hours", 12) or 12) * 3600)


def _public_url() -> str:
    return _norm_url(os.environ.get("EDUPACS_LTI_PUBLIC_URL") or LTICFG.get("public_url", "")) or "http://127.0.0.1:8000"


def _find_reg(iss: str, client_id: str = "") -> Optional[dict]:
    iss_n = _norm_url(iss)
    for reg in LTICFG.get("registrations", []):
        if _norm_url(reg.get("iss", "")) != iss_n:
            continue
        if client_id and str(reg.get("client_id", "")) != str(client_id):
            continue
        return reg
    return None


def _deployment_ok(reg: dict, deployment_id: str) -> bool:
    allowed = reg.get("deployment_id", "")
    if not allowed or allowed == "*":
        return True
    if isinstance(allowed, list):
        return str(deployment_id) in [str(x) for x in allowed]
    return str(deployment_id) == str(allowed)


def _platform_meta(reg: dict) -> dict:
    """从显式配置或 OIDC discovery 获取 authorization_endpoint / token_endpoint / jwks_uri。"""
    meta: Dict[str, Any] = {}
    required = ("authorization_endpoint", "jwks_uri", "token_endpoint")
    explicit = {
        "authorization_endpoint": reg.get("authorization_endpoint", ""),
        "token_endpoint": reg.get("token_endpoint", ""),
        "jwks_uri": reg.get("jwks_url", ""),
    }
    if all(explicit.values()):
        return explicit

    iss = _norm_url(reg.get("iss", ""))
    cache_key = iss + "|" + str(reg.get("id", ""))
    hit = _discovery_cache.get(cache_key)
    if hit and hit[0] > time.time():
        meta = dict(hit[1])
    else:
        candidates = []
        if reg.get("discovery_url"):
            candidates.append(reg["discovery_url"])
        candidates += [iss + "/.well-known/openid-configuration",
                       iss + "/mod/lti/.well-known/openid-configuration"]
        for url in candidates:
            try:
                r = http_client.get(url, timeout=6)
                if r.status_code == 200 and isinstance(r.json(), dict):
                    meta = r.json()
                    break
            except Exception as e:
                _log(f"OIDC discovery 失败 {url}: {str(e)[:120]}")
        if meta:
            _discovery_cache[cache_key] = (time.time() + 3600, meta)

    out = {
        "authorization_endpoint": explicit["authorization_endpoint"] or meta.get("authorization_endpoint", ""),
        "token_endpoint": explicit["token_endpoint"] or meta.get("token_endpoint", ""),
        "jwks_uri": explicit["jwks_uri"] or meta.get("jwks_uri", ""),
    }
    if not all(out.values()):
        raise RuntimeError("无法获取 LTI 平台元数据，请在 config/lti.json 显式填写 authorization_endpoint / jwks_url / token_endpoint")
    return out


# ---------------------------------------------------------------- JWT（仅依赖 cryptography）
def _jwt_unverify(token: str):
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT 格式错误")
    header = json.loads(_b64u_dec(parts[0]))
    payload = json.loads(_b64u_dec(parts[1]))
    return header, payload, (parts[0] + "." + parts[1]).encode(), _b64u_dec(parts[2])


def _fetch_jwks(url: str, force: bool = False) -> dict:
    hit = _jwks_cache.get(url)
    if hit and hit[0] > time.time() and not force:
        return hit[1]
    r = http_client.get(_server_url(url), timeout=8)
    r.raise_for_status()
    data = r.json()
    keys = {k["kid"]: k for k in data.get("keys", []) if k.get("kid")}
    _jwks_cache[url] = (time.time() + 3600, keys)
    return keys


def _jwk_public_key(jwk: dict):
    if jwk.get("kty") != "RSA":
        raise ValueError("仅支持 RSA 公钥")
    n = int.from_bytes(_b64u_dec(jwk["n"]), "big")
    e = int.from_bytes(_b64u_dec(jwk["e"]), "big")
    return rsa.RSAPublicNumbers(e, n).public_key()


def _verify_id_token(token: str, reg: dict, meta: dict, expected_nonce: str) -> dict:
    header, payload, signing_input, sig = _jwt_unverify(token)
    if header.get("alg") != "RS256":
        raise ValueError("ID Token 签名算法必须为 RS256")
    kid = header.get("kid", "")
    keys = _fetch_jwks(meta["jwks_uri"])
    jwk = keys.get(kid)
    if not jwk:
        keys = _fetch_jwks(meta["jwks_uri"], force=True)
        jwk = keys.get(kid)
    if not jwk:
        raise ValueError("平台 JWKS 中找不到对应 kid")
    _jwk_public_key(jwk).verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())

    now = int(time.time())
    iss = _norm_url(reg.get("iss", ""))
    if _norm_url(payload.get("iss", "")) != iss:
        raise ValueError("ID Token iss 不匹配")
    aud = payload.get("aud", "")
    aud_list = aud if isinstance(aud, list) else [aud]
    if str(reg.get("client_id", "")) not in [str(x) for x in aud_list]:
        raise ValueError("ID Token aud 不匹配")
    if payload.get("azp") and str(payload["azp"]) != str(reg.get("client_id", "")):
        raise ValueError("ID Token azp 不匹配")
    if int(payload.get("exp", 0)) <= now:
        raise ValueError("ID Token 已过期")
    if payload.get("nonce") != expected_nonce:
        raise ValueError("ID Token nonce 不匹配")
    dep = payload.get(LTI_CLAIM + "deployment_id", "")
    if not _deployment_ok(reg, dep):
        raise ValueError("deployment_id 不匹配")
    if payload.get(LTI_CLAIM + "version") not in (None, "1.3.0"):
        raise ValueError("不支持的 LTI 版本")
    return payload


def _tool_private_key():
    global LTI_KEY_PATH
    if LTI_KEY_PATH is None:
        env = os.environ.get("EDUPACS_LTI_KEY")
        LTI_KEY_PATH = Path(env) if env else (ROOT / "app" / "data" / "lti_private.pem")
    if LTI_KEY_PATH.exists():
        return serialization.load_pem_private_key(LTI_KEY_PATH.read_bytes(), password=None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    LTI_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    LTI_KEY_PATH.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    _log(f"已生成 LTI 工具私钥：{LTI_KEY_PATH}")
    return key


def _tool_kid() -> str:
    pub = _tool_private_key().public_key()
    der = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]


def _int_b64u(i: int) -> str:
    raw = i.to_bytes(max(1, (i.bit_length() + 7) // 8), "big")
    return _b64u_enc(raw)


def _tool_jwk() -> dict:
    pub = _tool_private_key().public_key()
    nums = pub.public_numbers()
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": _tool_kid(),
            "n": _int_b64u(nums.n), "e": _int_b64u(nums.e)}


def _sign_jwt(claims: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": _tool_kid()}
    seg1 = _b64u_enc(json.dumps(header, separators=(",", ":")).encode())
    seg2 = _b64u_enc(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode())
    signing_input = f"{seg1}.{seg2}".encode()
    sig = _tool_private_key().sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{seg1}.{seg2}.{_b64u_enc(sig)}"


# ---------------------------------------------------------------- 数据库
SCHEMA = """
CREATE TABLE IF NOT EXISTS lti_auth (
  state TEXT PRIMARY KEY, nonce TEXT NOT NULL, iss TEXT NOT NULL,
  client_id TEXT NOT NULL, target_link_uri TEXT DEFAULT '',
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lti_identities (
  iss TEXT NOT NULL, client_id TEXT NOT NULL, sub TEXT NOT NULL,
  user_id INTEGER NOT NULL, deployment_id TEXT DEFAULT '',
  display_name TEXT DEFAULT '', email TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY(iss, client_id, sub)
);
CREATE TABLE IF NOT EXISTS lti_sessions (
  sid TEXT PRIMARY KEY, user_id INTEGER NOT NULL, iss TEXT NOT NULL,
  client_id TEXT NOT NULL, deployment_id TEXT DEFAULT '', sub TEXT DEFAULT '',
  roles TEXT DEFAULT '', context_id TEXT DEFAULT '', resource_link_id TEXT DEFAULT '',
  case_id INTEGER, return_url TEXT DEFAULT '', name TEXT DEFAULT '', email TEXT DEFAULT '',
  ags_endpoint TEXT DEFAULT '', ags_lineitem TEXT DEFAULT '', ags_scope TEXT DEFAULT '',
  token_endpoint TEXT DEFAULT '',
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lti_deeplink (
  dl_id TEXT PRIMARY KEY, iss TEXT NOT NULL, client_id TEXT NOT NULL,
  deployment_id TEXT DEFAULT '', return_url TEXT NOT NULL, data TEXT DEFAULT '',
  target_link_uri TEXT DEFAULT '', user_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS xapi_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  statement_id TEXT UNIQUE NOT NULL, dedupe_key TEXT,
  statement TEXT NOT NULL, status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0, last_error TEXT DEFAULT '',
  created_at INTEGER NOT NULL, sent_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_xapi_dedupe
  ON xapi_queue(dedupe_key) WHERE dedupe_key IS NOT NULL;
"""


def ensure_tables() -> None:
    with DB() as conn:
        conn.executescript(SCHEMA)


def cleanup_expired() -> None:
    now = int(time.time())
    try:
        with DB() as conn:
            conn.execute("DELETE FROM lti_auth WHERE expires_at<?", (now,))
            conn.execute("DELETE FROM lti_sessions WHERE expires_at<?", (now,))
            conn.execute("DELETE FROM lti_deeplink WHERE expires_at<?", (now,))
    except Exception as e:
        _log(f"清理过期 LTI 记录失败: {e}")


# ---------------------------------------------------------------- 角色 / 用户
def _role_from_claims(claims: dict) -> str:
    roles = claims.get(LTI_CLAIM + "roles", []) or []
    if isinstance(roles, str):
        roles = [roles]
    text = " ".join(str(r) for r in roles).lower()
    order = {"admin": 0, "teacher": 1, "student": 2}
    for key, role in sorted(LTICFG.get("role_map", {}).items(),
                            key=lambda kv: order.get(kv[1], 9)):
        if str(key).lower() in text:
            return role
    return LTICFG.get("default_role", "student")


def _resolve_user(conn, reg: dict, claims: dict, issuer: str) -> Optional[int]:
    sub = str(claims.get("sub", ""))
    if not sub:
        raise ValueError("ID Token 缺少 sub")
    client_id = str(reg.get("client_id", ""))
    ident = conn.execute(
        "SELECT * FROM lti_identities WHERE iss=? AND client_id=? AND sub=?",
        (issuer, client_id, sub)).fetchone()
    if ident:
        conn.execute("UPDATE lti_identities SET updated_at=datetime('now','localtime') WHERE iss=? AND client_id=? AND sub=?",
                     (issuer, client_id, sub))
        return ident["user_id"]

    if not LTICFG.get("auto_provision", True):
        return None

    name = str(claims.get("name", "") or claims.get(LTI_CLAIM + "name", "") or "")
    email = str(claims.get("email", "") or "")
    role = _role_from_claims(claims)
    digest = _hash_id(issuer, client_id, sub)[:14]
    username = f"lti_{digest}"
    existing = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if existing:
        uid = existing["id"]
    else:
        full = name if LTICFG.get("use_lti_name", True) else f"LTI 用户 {digest[:4]}"
        cur = conn.execute(
            "INSERT INTO users(username,password,role,full_name,dept,must_change_pwd,active) "
            "VALUES(?,?,?,?,?,0,1)",
            (username, HASH_PASSWORD(secrets.token_urlsafe(32)), role, full[:60], "LTI"))
        uid = cur.lastrowid
    conn.execute(
        "INSERT INTO lti_identities(iss,client_id,sub,user_id,deployment_id,display_name,email) "
        "VALUES(?,?,?,?,?,?,?)",
        (issuer, client_id, sub, uid, str(claims.get(LTI_CLAIM + "deployment_id", "")),
         name[:120], email[:160]))
    _log(f"LTI 自动创建/绑定用户 uid={uid} iss={issuer} sub={digest}")
    return uid


def _case_id_from_claims(claims: dict, request: Request) -> Optional[int]:
    custom = claims.get(LTI_CLAIM + "custom", {}) or {}
    for key in ("case_id", "edupacs_case_id", "case"):
        v = custom.get(key) or custom.get(key.replace("_", "")) or ""
        if str(v).isdigit():
            return int(v)
    for key in ("case_id", "case"):
        v = request.query_params.get(key, "")
        if str(v).isdigit():
            return int(v)
    target = str(request.query_params.get("target_link_uri", "") or "")
    if not target:
        target = str(claims.get(LTI_CLAIM + "target_link_uri", "") or "")
    qs = parse_qs(urlparse(target).query)
    for key in ("case_id", "case"):
        v = (qs.get(key) or [""])[0]
        if str(v).isdigit():
            return int(v)
    return None


# ---------------------------------------------------------------- xAPI
def _xapi_actor(sess) -> dict:
    mode = XAPICFG.get("actor_mode", "anonymous")
    base = XAPICFG.get("platform_iri", _public_url())
    if mode == "lti" and sess and sess["email"]:
        return {"objectType": "Agent", "mbox": "mailto:" + sess["email"],
                "name": sess["name"] or ""}
    if mode == "lti" and sess and sess["name"]:
        return {"objectType": "Agent",
                "account": {"homePage": base, "name": _hash_id(sess["iss"], sess["sub"])[:16]},
                "name": sess["name"]}
    digest = _hash_id(sess["iss"], sess["sub"])[:16] if sess else "anonymous"
    return {"objectType": "Agent",
            "account": {"homePage": base, "name": "stu-" + digest},
            "name": "匿名学习者"}


def _make_statement(sess, verb_key: str, object_id: str, object_name: str,
                    object_type: str, result: Optional[dict] = None,
                    response: str = "") -> dict:
    verb_iri = XAPICFG.get("verbs", {}).get(verb_key, "http://adlnet.gov/expapi/verbs/experienced")
    display = XAPICFG.get("verb_display", {}).get(verb_key, verb_key)
    stmt: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "actor": _xapi_actor(sess),
        "verb": {"id": verb_iri, "display": {"zh-CN": display, "en-US": verb_key}},
        "object": {
            "id": object_id,
            "objectType": "Activity",
            "definition": {"name": {"zh-CN": object_name}, "type": object_type},
        },
        "context": {
            "registration": sess["sid"],
            "platform": XAPICFG.get("platform_name", "EduPACS"),
        },
        "timestamp": _now_iso(),
    }
    if sess["context_id"]:
        stmt["context"]["contextActivities"] = {
            "parent": [{"id": str(sess["context_id"]), "objectType": "Activity"}]
        }
    if response:
        result = dict(result or {})
        result["response"] = response[:4000]
    if result:
        stmt["result"] = result
    return stmt


def _enqueue(statement: dict, dedupe_key: Optional[str] = None) -> bool:
    with DB() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO xapi_queue(statement_id,dedupe_key,statement,created_at) VALUES(?,?,?,?)",
            (statement["id"], dedupe_key, json.dumps(statement, ensure_ascii=False), int(time.time())))
        return cur.rowcount > 0


def track_event(request: Request, verb_key: str, *, case_id: Optional[int] = None,
                question_id: Optional[int] = None, response: str = "",
                score: Optional[float] = None, max_score: Optional[float] = None,
                title: str = "", user_id: Optional[int] = None) -> bool:
    """由主程序在关键教学动作处调用；非 LTI 会话自动无操作。

    user_id 用于教师批阅等场景：此时请求来自教师，xAPI actor 应指向被批阅学生。
    """
    if not XAPICFG.get("enabled") or not XAPICFG.get("endpoint"):
        return False
    sid = request.cookies.get(COOKIE_SID, "") if request is not None else ""
    with DB() as conn:
        sess = None
        if sid:
            sess = conn.execute("SELECT * FROM lti_sessions WHERE sid=? AND expires_at>=?",
                                (sid, int(time.time()))).fetchone()
        if not sess and user_id is not None:
            sess = conn.execute(
                "SELECT * FROM lti_sessions WHERE user_id=? AND expires_at>=? "
                "ORDER BY last_seen DESC LIMIT 1", (user_id, int(time.time()))).fetchone()
        if not sess:
            return False
        if case_id and not title:
            c = conn.execute("SELECT title FROM cases WHERE id=?", (case_id,)).fetchone()
            title = c["title"] if c else f"病例 {case_id}"
        if question_id and not title:
            q = conn.execute("SELECT question FROM questions WHERE id=?", (question_id,)).fetchone()
            title = q["question"] if q else f"题目 {question_id}"

    base = _norm_url(XAPICFG.get("platform_iri", _public_url()))
    if question_id:
        object_id = f"{base}/lti/question/{question_id}"
        obj_type = "http://adlnet.gov/expapi/activities/cmi.interaction"
    elif case_id:
        object_id = f"{base}/lti/case/{case_id}"
        obj_type = "http://adlnet.gov/expapi/activities/lesson"
    else:
        object_id = f"{base}/lti/course/{sess['context_id'] or 'default'}"
        obj_type = "http://adlnet.gov/expapi/activities/course"

    result = None
    if score is not None:
        result = {"score": {"raw": float(score), "max": float(max_score) if max_score else 100.0}}
        if max_score:
            result["score"]["scaled"] = round(max(0.0, min(1.0, float(score) / float(max_score))), 4)

    dedupe = None
    if verb_key in ("viewed", "viewer_opened"):
        dedupe = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{sid}|{verb_key}|{object_id}"))

    stmt = _make_statement(sess, verb_key, object_id, title or "EduPACS 学习活动",
                           obj_type, result=result, response=response)
    queued = _enqueue(stmt, dedupe)
    if queued:
        _log(f"xAPI 入队 {verb_key} -> {object_id}")
    return queued


def flush_xapi_queue() -> None:
    endpoint = XAPICFG.get("endpoint", "")
    if not XAPICFG.get("enabled") or not endpoint:
        return
    headers = {
        "Content-Type": "application/json",
        "X-Experience-API-Version": "1.0.3",
    }
    auth = os.environ.get("EDUPACS_XAPI_AUTH_HEADER") or XAPICFG.get("auth_header", "")
    if auth:
        headers["Authorization"] = auth if auth.lower().startswith("basic ") else "Basic " + auth
    now = int(time.time())
    with DB() as conn:
        rows = conn.execute(
            "SELECT * FROM xapi_queue WHERE status IN ('pending','failed') AND attempts<? "
            "AND (last_error='' OR (id % 5)=?) ORDER BY id LIMIT 20",
            (int(XAPICFG.get("max_attempts", 5)), now // 5)).fetchall()
    for row in rows:
        try:
            r = http_client.post(endpoint, data=row["statement"].encode("utf-8"),
                                 headers=headers, timeout=10)
            if 200 <= r.status_code < 300:
                with DB() as conn:
                    conn.execute("UPDATE xapi_queue SET status='sent',sent_at=?,last_error='' WHERE id=?",
                                 (int(time.time()), row["id"]))
            else:
                raise RuntimeError(f"LRS 返回 {r.status_code}: {r.text[:160]}")
        except Exception as e:
            with DB() as conn:
                conn.execute("UPDATE xapi_queue SET status='failed',attempts=attempts+1,last_error=? WHERE id=?",
                             (str(e)[:500], row["id"]))
            _log(f"xAPI 发送失败 id={row['id']}: {str(e)[:200]}")


def _worker_loop() -> None:
    interval = max(2, int(XAPICFG.get("flush_interval_seconds", 5) or 5))
    while not _stop_event.wait(interval):
        try:
            cleanup_expired()
            flush_xapi_queue()
        except Exception as e:
            _log(f"集成后台任务异常: {e}")


# ---------------------------------------------------------------- AGS 成绩回传
def _ags_access_token(reg: dict, sess) -> Optional[str]:
    token_endpoint = _server_url(sess["token_endpoint"] or _platform_meta(reg).get("token_endpoint", ""))
    if not token_endpoint:
        return None
    scope = sess["ags_scope"] or AGS_SCORE_SCOPE
    cache_key = f"{sess['iss']}|{sess['client_id']}|{scope}"
    hit = _ags_token_cache.get(cache_key)
    if hit and hit[0] > time.time() + 30:
        return hit[1]
    now = int(time.time())
    assertion = _sign_jwt({
        "iss": str(reg.get("client_id", "")),
        "sub": str(reg.get("client_id", "")),
        "aud": token_endpoint,
        "iat": now, "exp": now + 300, "jti": secrets.token_hex(16),
    })
    r = http_client.post(token_endpoint, data={
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
        "scope": scope,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
    r.raise_for_status()
    token = r.json().get("access_token", "")
    if token:
        _ags_token_cache[cache_key] = (time.time() + int(r.json().get("expires_in", 3600)), token)
    return token


def post_grade_for_user(user_id: int, score: float, max_score: float,
                        feedback: str = "") -> bool:
    """教师批阅后可选回传 Moodle AGS；失败只记日志，不影响教学系统本身。"""
    if not LTICFG.get("grade_passback", {}).get("enabled"):
        return False
    with DB() as conn:
        sess = conn.execute(
            "SELECT * FROM lti_sessions WHERE user_id=? AND ags_lineitem!='' "
            "AND expires_at>=? ORDER BY last_seen DESC LIMIT 1",
            (user_id, int(time.time()))).fetchone()
    if not sess:
        return False
    reg = _find_reg(sess["iss"], sess["client_id"])
    if not reg:
        return False
    try:
        token = _ags_access_token(reg, sess)
        if not token:
            return False
        lineitem = _server_url(sess["ags_lineitem"])
        payload = {
            "userId": sess["sub"],
            "scoreGiven": float(score),
            "scoreMaximum": float(max_score) if max_score else 100.0,
            "activityProgress": "Completed",
            "gradingProgress": "FullyGraded",
            "timestamp": _now_iso(),
        }
        if LTICFG.get("grade_passback", {}).get("comment_on_grade", True):
            payload["comment"] = feedback[:1000]
        r = http_client.post(lineitem, json=payload, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.ims.lis.v2.score+json",
        }, timeout=10)
        ok = 200 <= r.status_code < 300
        _log(f"AGS 回传 {'成功' if ok else '失败'} uid={user_id} status={r.status_code}")
        return ok
    except Exception as e:
        _log(f"AGS 回传异常 uid={user_id}: {str(e)[:200]}")
        return False


# ---------------------------------------------------------------- LTI 路由
@router.get("/lti/jwks.json")
def lti_jwks():
    """Moodle 侧「公钥 JWK URL」填这个地址。"""
    return JSONResponse({"keys": [_tool_jwk()]},
                        headers={"Cache-Control": "public, max-age=3600"})


@router.api_route("/lti/login", methods=["GET", "POST"])
async def lti_login(request: Request):
    if not _enabled():
        return JSONResponse({"detail": "LTI 集成未启用"}, status_code=503)
    data: Dict[str, Any] = dict(request.query_params)
    if request.method == "POST":
        try:
            data.update(await request.form())
        except Exception:
            pass
    iss = str(data.get("iss", ""))
    client_id = str(data.get("client_id", ""))
    reg = _find_reg(iss, client_id)
    if not reg:
        return JSONResponse({"detail": "未注册的 LTI 平台 iss/client_id"}, status_code=400)
    try:
        meta = _platform_meta(reg)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    target = str(data.get("target_link_uri", "") or reg.get("target_link_uri", "")
                 or (_public_url() + "/lti/callback"))
    now = int(time.time())
    with DB() as conn:
        conn.execute("INSERT INTO lti_auth(state,nonce,iss,client_id,target_link_uri,created_at,expires_at) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (state, nonce, _norm_url(iss), client_id, target, now, now + 600))

    params = {
        "scope": "openid", "response_type": "id_token", "client_id": client_id,
        "redirect_uri": target, "login_hint": str(data.get("login_hint", "")),
        "state": state, "nonce": nonce, "response_mode": "form_post", "prompt": "none",
    }
    if data.get("lti_message_hint"):
        params["lti_message_hint"] = str(data["lti_message_hint"])
    sep = "&" if "?" in meta["authorization_endpoint"] else "?"
    return RedirectResponse(meta["authorization_endpoint"] + sep + urlencode(params), status_code=303)


def _deep_link_page(dl_id: str, cases: list) -> str:
    rows = "".join(
        f'<label class="dl-item"><input type="checkbox" name="case_id" value="{c["id"]}"> '
        f'<span>{html.escape(str(c["title"]))}</span>'
        f'<small>{html.escape(str(c.get("disease") or c.get("system") or ""))}</small></label>'
        for c in cases)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>从 EduPACS 添加病例</title>
<style>
body{{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f4f6fa;color:#1f2937}}
.wrap{{max-width:760px;margin:32px auto;padding:24px;background:#fff;border-radius:12px;box-shadow:0 4px 18px #0001}}
h1{{font-size:20px;margin:0 0 6px}} p{{color:#64748b;font-size:13px}}
.dl-item{{display:flex;gap:10px;align-items:center;padding:10px 12px;border:1px solid #e2e8f0;border-radius:8px;margin:8px 0;cursor:pointer}}
.dl-item:hover{{border-color:#2563eb;background:#f8fafc}} .dl-item small{{margin-left:auto;color:#94a3b8}}
button{{margin-top:16px;padding:10px 22px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-size:14px;cursor:pointer}}
</style></head><body><div class="wrap">
<h1>从 EduPACS 选择教学病例</h1><p>勾选要添加到 Moodle 课程的病例，提交后将返回课程。</p>
<form method="post" action="/lti/deeplink/submit"><input type="hidden" name="dl_id" value="{html.escape(dl_id)}">
{rows or '<div class="dl-item">暂无已发布病例</div>'}
<button type="submit">添加到课程</button></form></div></body></html>"""


@router.post("/lti/callback")
async def lti_callback(request: Request):
    if not _enabled():
        return JSONResponse({"detail": "LTI 集成未启用"}, status_code=503)
    form = await request.form()
    state = str(form.get("state", ""))
    id_token = str(form.get("id_token", ""))
    if not state or not id_token:
        return JSONResponse({"detail": "缺少 state 或 id_token"}, status_code=400)

    with DB() as conn:
        auth = conn.execute("SELECT * FROM lti_auth WHERE state=?", (state,)).fetchone()
        if auth:
            conn.execute("DELETE FROM lti_auth WHERE state=?", (state,))
    if not auth or auth["expires_at"] < int(time.time()):
        return JSONResponse({"detail": "LTI 登录状态已过期，请从 Moodle 重新进入"}, status_code=400)

    reg = _find_reg(auth["iss"], auth["client_id"])
    if not reg:
        return JSONResponse({"detail": "平台注册信息已变更"}, status_code=400)
    try:
        meta = _platform_meta(reg)
        claims = _verify_id_token(id_token, reg, meta, auth["nonce"])
    except Exception as e:
        _log(f"LTI ID Token 校验失败: {e}")
        return JSONResponse({"detail": f"ID Token 校验失败：{str(e)[:200]}"}, status_code=401)

    issuer = _norm_url(reg["iss"])
    message_type = str(claims.get(LTI_CLAIM + "message_type", ""))
    context = claims.get(LTI_CLAIM + "context", {}) or {}
    resource = claims.get(LTI_CLAIM + "resource_link", {}) or {}
    launch_presentation = claims.get(LTI_CLAIM + "launch_presentation", {}) or {}
    ags = claims.get(AGS_ENDPOINT, {}) or {}
    dl_settings = claims.get(DL_SETTINGS, {}) or {}

    with DB() as conn:
        uid = _resolve_user(conn, reg, claims, issuer)
    if not uid:
        return JSONResponse({"detail": "LTI 用户未绑定，且未开启自动创建账号"}, status_code=403)

    if message_type == "LtiDeepLinkingRequest":
        with DB() as conn:
            u = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if not u or u["role"] not in ("admin", "teacher"):
            return JSONResponse({"detail": "只有教师/管理员可在 Moodle 中添加病例"}, status_code=403)
        return_url = str(dl_settings.get("deep_link_return_url", ""))
        if not return_url:
            return JSONResponse({"detail": "平台未提供 deep_link_return_url"}, status_code=400)
        dl_id = secrets.token_urlsafe(24)
        with DB() as conn:
            conn.execute(
                "INSERT INTO lti_deeplink(dl_id,iss,client_id,deployment_id,return_url,data,target_link_uri,user_id,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (dl_id, issuer, str(reg.get("client_id", "")),
                 str(claims.get(LTI_CLAIM + "deployment_id", "")), return_url,
                 str(dl_settings.get("data", "")), auth["target_link_uri"], uid,
                 int(time.time()), int(time.time()) + 900))
            cases = conn.execute(
                "SELECT id,title,disease,system FROM cases WHERE status='published' ORDER BY id DESC LIMIT 200").fetchall()
        return HTMLResponse(_deep_link_page(dl_id, [dict(r) for r in cases]))

    if message_type != "LtiResourceLinkRequest":
        return JSONResponse({"detail": f"不支持的 LTI 消息类型：{message_type}"}, status_code=400)

    case_id = _case_id_from_claims(claims, request)
    if case_id:
        with DB() as conn:
            c = conn.execute("SELECT status FROM cases WHERE id=?", (case_id,)).fetchone()
            with DB() as conn2:
                u = conn2.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
            if not c or (u and u["role"] == "student" and c["status"] != "published"):
                case_id = None

    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    roles = claims.get(LTI_CLAIM + "roles", [])
    with DB() as conn:
        conn.execute(
            "INSERT INTO lti_sessions(sid,user_id,iss,client_id,deployment_id,sub,roles,context_id,resource_link_id,"
            "case_id,return_url,name,email,ags_endpoint,ags_lineitem,ags_scope,token_endpoint,created_at,expires_at,last_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, uid, issuer, str(reg.get("client_id", "")),
             str(claims.get(LTI_CLAIM + "deployment_id", "")), str(claims.get("sub", "")),
             json.dumps(roles, ensure_ascii=False), str(context.get("id", "")),
             str(resource.get("id", "")), case_id, str(launch_presentation.get("return_url", "")),
             str(claims.get("name", ""))[:120], str(claims.get("email", ""))[:160],
             str(ags.get("endpoint", "")), str(ags.get("lineitem", "")),
             str(ags.get("scope", AGS_SCORE_SCOPE)), meta.get("token_endpoint", ""),
             now, now + _ttl(), now))

    resp = RedirectResponse(_public_url() + "/#/lti/landing", status_code=303)
    resp.set_cookie(COOKIE_SID, sid, max_age=_ttl(), httponly=True,
                    samesite="lax", secure=bool(LTICFG.get("cookie_secure", False)), path="/")
    return resp


@router.post("/lti/deeplink/submit")
async def deeplink_submit(request: Request):
    form = await request.form()
    dl_id = str(form.get("dl_id", ""))
    case_ids = [str(x) for x in form.getlist("case_id") if str(x).isdigit()]
    with DB() as conn:
        dl = conn.execute("SELECT * FROM lti_deeplink WHERE dl_id=?", (dl_id,)).fetchone()
    if not dl or dl["expires_at"] < int(time.time()):
        return JSONResponse({"detail": "深链会话已过期，请从 Moodle 重新进入"}, status_code=400)

    reg = _find_reg(dl["iss"], dl["client_id"])
    if not reg:
        return JSONResponse({"detail": "平台注册信息不存在"}, status_code=400)

    base = dl["target_link_uri"]
    items = []
    for cid in case_ids:
        with DB() as conn:
            c = conn.execute("SELECT title FROM cases WHERE id=?", (int(cid),)).fetchone()
        if not c:
            continue
        sep = "&" if "?" in base else "?"
        url = f"{base}{sep}case_id={cid}"
        items.append({
            "type": "ltiResourceLink",
            "title": c["title"],
            "url": url,
            "custom": {"case_id": cid},
        })

    now = int(time.time())
    jwt_claims = {
        "iss": str(reg.get("client_id", "")),
        "aud": dl["iss"],
        "iat": now, "exp": now + 300,
        "nonce": secrets.token_urlsafe(16),
        LTI_CLAIM + "deployment_id": dl["deployment_id"],
        DL_DATA: dl["data"],
        DL_CONTENT: items,
    }
    jwt = _sign_jwt(jwt_claims)
    with DB() as conn:
        conn.execute("DELETE FROM lti_deeplink WHERE dl_id=?", (dl_id,))
    safe_url = html.escape(dl["return_url"], quote=True)
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>返回 Moodle</title></head>
<body onload="document.forms[0].submit()"><form method="post" action="{safe_url}">
<input type="hidden" name="JWT" value="{html.escape(jwt, quote=True)}">
<p>正在返回 Moodle…</p><button type="submit">如未自动跳转，请点击这里</button></form></body></html>""")


@router.get("/api/lti/session")
def lti_session(request: Request):
    sid = request.cookies.get(COOKIE_SID, "")
    if not sid:
        return JSONResponse({"detail": "非 LTI 会话"}, status_code=404)
    now = int(time.time())
    with DB() as conn:
        sess = conn.execute("SELECT * FROM lti_sessions WHERE sid=? AND expires_at>=?", (sid, now)).fetchone()
        if not sess:
            return JSONResponse({"detail": "LTI 会话已过期"}, status_code=401)
        conn.execute("UPDATE lti_sessions SET last_seen=? WHERE sid=?", (now, sid))
        u = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (sess["user_id"],)).fetchone()
    if not u:
        return JSONResponse({"detail": "LTI 用户已停用"}, status_code=403)
    token = MAKE_TOKEN(u["id"], u["role"], u["password"])
    user = ROW_DICT(u)
    user["password"] = None
    return {
        "token": token,
        "user": user,
        "lti": {
            "case_id": sess["case_id"],
            "context_id": sess["context_id"],
            "resource_link_id": sess["resource_link_id"],
            "return_url": sess["return_url"],
            "roles": sess["roles"],
            "platform": sess["iss"],
        },
        "xapi_enabled": bool(XAPICFG.get("enabled") and XAPICFG.get("endpoint")),
    }


@router.post("/api/lti/logout")
def lti_logout(request: Request):
    sid = request.cookies.get(COOKIE_SID, "")
    if sid:
        with DB() as conn:
            conn.execute("DELETE FROM lti_sessions WHERE sid=?", (sid,))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_SID, path="/")
    return resp


@router.post("/api/xapi/event")
async def xapi_event(request: Request):
    body = await request.json()
    queued = track_event(
        request, str(body.get("verb", "viewed")),
        case_id=int(body["case_id"]) if str(body.get("case_id", "")).isdigit() else None,
        question_id=int(body["question_id"]) if str(body.get("question_id", "")).isdigit() else None,
        response=str(body.get("response", "")),
        score=body.get("score"), max_score=body.get("max_score"),
        title=str(body.get("title", "")))
    return {"queued": queued}


@router.get("/api/xapi/queue")
def xapi_queue_view(request: Request):
    """本地调试用：查看 xAPI 发送队列状态（仅返回状态，不含学生答案正文）。"""
    with DB() as conn:
        rows = conn.execute(
            "SELECT id,status,attempts,last_error,created_at,sent_at FROM xapi_queue ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 初始化
def setup(root: Path, db_func, make_token_func, hash_password_func, row_dict_func, secret: bytes):
    global ROOT, DB, MAKE_TOKEN, HASH_PASSWORD, ROW_DICT, SECRET
    ROOT = Path(root)
    DB = db_func
    MAKE_TOKEN = make_token_func
    HASH_PASSWORD = hash_password_func
    ROW_DICT = row_dict_func
    SECRET = secret

    lti_path = Path(os.environ.get("EDUPACS_LTI_CONFIG", ROOT / "config" / "lti.json"))
    xapi_path = Path(os.environ.get("EDUPACS_XAPI_CONFIG", ROOT / "config" / "xapi.json"))
    LTICFG.clear()
    XAPICFG.clear()
    LTICFG.update(_load_json(lti_path, {"enabled": False, "registrations": []}))
    XAPICFG.update(_load_json(xapi_path, {"enabled": False}))
    if os.environ.get("EDUPACS_XAPI_ENDPOINT"):
        XAPICFG["endpoint"] = os.environ["EDUPACS_XAPI_ENDPOINT"]
    if os.environ.get("EDUPACS_XAPI_AUTH_HEADER"):
        XAPICFG["auth_header"] = os.environ["EDUPACS_XAPI_AUTH_HEADER"]

    ensure_tables()
    cleanup_expired()
    global _worker_started
    if XAPICFG.get("enabled") and not _worker_started:
        _worker_started = True
        t = threading.Thread(target=_worker_loop, name="edupacs-xapi", daemon=True)
        t.start()
    _log(f"LTI={'on' if _enabled() else 'off'} xAPI={'on' if XAPICFG.get('enabled') else 'off'} "
         f"public_url={_public_url()}")
    return router
