# -*- coding: utf-8 -*-
"""EduPACS LTI 1.3 + xAPI 集成自测（独立端口，不影响正在运行的 8000 服务）

用法：
    .venv\\Scripts\\python tools\\test_lti_xapi.py

覆盖：
  - OIDC 登录跳转 / state+nonce 校验
  - ID Token 验签、角色映射、用户自动创建、LTI 会话 Cookie
  - 前端落地接口 /api/lti/session 换取本地 token
  - xAPI 入队与发送到模拟 LRS
  - Deep Linking 选病例并返回 JWT
  - AGS 成绩回传到模拟 Moodle
"""
import base64
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".temp" / "lti_selftest"
TMP.mkdir(parents=True, exist_ok=True)
DB_PATH = TMP / "app.db"
SECRET_PATH = TMP / "secret.key"

BASE = "http://127.0.0.1:8123"
PLATFORM = "http://127.0.0.1:8129/moodle"
ISS = PLATFORM
CLIENT_ID = "test-client"
DEPLOYMENT = "dep-1"
COOKIE_NAME = "edupacs_lti_sid"

os.environ["EDUPACS_DB_PATH"] = str(DB_PATH)
os.environ["EDUPACS_SECRET_FILE"] = str(SECRET_PATH)
os.environ["EDUPACS_LTI_PUBLIC_URL"] = BASE
os.environ["EDUPACS_LTI_CONFIG"] = str(TMP / "lti.json")
os.environ["EDUPACS_XAPI_CONFIG"] = str(TMP / "xapi.json")
os.environ["EDUPACS_LTI_KEY"] = str(TMP / "lti_private.pem")
os.environ["EDUPACS_INTEGRATIONS_LOG"] = str(TMP / "integrations.log")

# 每次自测从干净数据库开始（仅清理 .temp/lti_selftest 内的测试库）
for _p in (DB_PATH, SECRET_PATH, TMP / "lti_private.pem",
           Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
    try:
        _p.unlink()
    except FileNotFoundError:
        pass

import requests  # noqa: E402
import uvicorn  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- 模拟平台 / LRS
platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
lrs_received = []
ags_received = []
token_requests = []


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def jwk_pub(key):
    n = key.public_key().public_numbers()
    return {
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": "platform-1",
        "n": b64u(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
        "e": b64u(n.e.to_bytes((n.e.bit_length() + 7) // 8, "big")),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/moodle/jwks":
            body = json.dumps({"keys": [jwk_pub(platform_key)]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.path == "/lrs":
            lrs_received.append(json.loads(raw))
        elif self.path == "/moodle/lineitem":
            ags_received.append(json.loads(raw))
        elif self.path == "/moodle/token":
            token_requests.append(raw.decode())
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        body = b'{"access_token":"fake-access-token","expires_in":3600,"token_type":"Bearer"}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def write_configs():
    (TMP / "lti.json").write_text(json.dumps({
        "version": 1, "enabled": True, "public_url": BASE,
        "session_ttl_hours": 1, "cookie_secure": False,
        "auto_provision": True, "default_role": "student",
        "role_map": {"Administrator": "admin", "Instructor": "teacher",
                     "TeachingAssistant": "teacher", "Learner": "student"},
        "grade_passback": {"enabled": True, "comment_on_grade": True},
        "registrations": [{
            "id": "selftest", "label": "自测平台", "iss": ISS, "client_id": CLIENT_ID,
            "deployment_id": DEPLOYMENT,
            "authorization_endpoint": PLATFORM + "/authorize",
            "jwks_url": PLATFORM + "/jwks",
            "token_endpoint": PLATFORM + "/token",
            "target_link_uri": BASE + "/lti/callback",
        }],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (TMP / "xapi.json").write_text(json.dumps({
        "version": 1, "enabled": True,
        "endpoint": "http://127.0.0.1:8129/lrs",
        "auth_header": "c2VsZnRlc3Q6c2VsZnRlc3Q=",
        "platform_iri": BASE, "platform_name": "EduPACS 自测",
        "actor_mode": "anonymous", "flush_interval_seconds": 1, "max_attempts": 3,
        "verbs": {"viewed": "http://adlnet.gov/expapi/verbs/experienced",
                  "viewer_opened": "http://adlnet.gov/expapi/verbs/interacted",
                  "answered": "http://adlnet.gov/expapi/verbs/answered",
                  "graded": "http://adlnet.gov/expapi/verbs/completed"},
        "verb_display": {"viewed": "浏览病例", "viewer_opened": "打开阅片器",
                         "answered": "作答", "graded": "完成批阅"},
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# 必须先写配置，再导入 main（main 导入时会调用 lti_xapi.setup 读取配置）
write_configs()
from app.backend.main import app  # noqa: E402
from app.backend import lti_xapi  # noqa: E402


def sign_platform_token(claims: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": "platform-1"}
    seg1 = b64u(json.dumps(header, separators=(",", ":")).encode())
    seg2 = b64u(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode())
    sig = platform_key.sign(f"{seg1}.{seg2}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{seg1}.{seg2}.{b64u(sig)}"


def lti_login(message_type="LtiResourceLinkRequest"):
    r = requests.get(BASE + "/lti/login", params={
        "iss": ISS, "client_id": CLIENT_ID, "login_hint": "selftest",
        "target_link_uri": BASE + "/lti/callback",
    }, allow_redirects=False, timeout=10)
    assert r.status_code == 303, (r.status_code, r.text[:300])
    redirect = urlparse(r.headers["Location"])
    params = parse_qs(redirect.query)
    state = params["state"][0]
    nonce = params["nonce"][0]
    assert "response_mode=form_post" in redirect.query
    return state, nonce


def make_id_token(nonce, message_type="LtiResourceLinkRequest", roles=None,
                  sub="student-1", custom=None, ags=None, deep_settings=None):
    now = int(time.time())
    claims = {
        "iss": ISS, "aud": CLIENT_ID, "sub": sub, "nonce": nonce,
        "iat": now - 5, "exp": now + 600, "name": "自测用户", "email": "",
        "https://purl.imsglobal.org/spec/lti/claim/version": "1.3.0",
        "https://purl.imsglobal.org/spec/lti/claim/deployment_id": DEPLOYMENT,
        "https://purl.imsglobal.org/spec/lti/claim/message_type": message_type,
        "https://purl.imsglobal.org/spec/lti/claim/roles": roles or [
            "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"],
        "https://purl.imsglobal.org/spec/lti/claim/context": {"id": "course-1", "label": "测试课程"},
        "https://purl.imsglobal.org/spec/lti/claim/resource_link": {"id": "link-1"},
        "https://purl.imsglobal.org/spec/lti/claim/launch_presentation": {
            "return_url": PLATFORM + "/course/view.php?id=1"},
        "https://purl.imsglobal.org/spec/lti/claim/custom": custom or {},
    }
    if ags:
        claims["https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"] = ags
    if deep_settings:
        claims["https://purl.imsglobal.org/spec/lti-dl/claim/deep_linking_settings"] = deep_settings
    return sign_platform_token(claims)


def callback(id_token, state):
    return requests.post(BASE + "/lti/callback", data={"state": state, "id_token": id_token},
                         allow_redirects=False, timeout=10)


def seed_case():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO cases(title,study_uid,status,patient_sex,patient_age) "
            "VALUES(?,?,?,?,?)", ("自测脑动脉瘤病例", "1.2.826.0.1.999.1", "published", "女", "55岁"))
        return conn.execute("SELECT id FROM cases WHERE study_uid=?",
                            ("1.2.826.0.1.999.1",)).fetchone()[0]


def wait_for(predicate, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.25)
    return False


def main():
    write_configs()
    platform_server = ThreadingHTTPServer(("127.0.0.1", 8129), Handler)
    threading.Thread(target=platform_server.serve_forever, daemon=True).start()

    uvicorn.run(app, host="127.0.0.1", port=8123, log_level="error", access_log=False)


def _alive():
    try:
        requests.get(BASE + "/api/config", timeout=1)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    # 以子线程方式启动，便于在同一进程内断言
    write_configs()
    platform_server = ThreadingHTTPServer(("127.0.0.1", 8129), Handler)
    threading.Thread(target=platform_server.serve_forever, daemon=True).start()

    server_thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=8123,
                                   log_level="error", access_log=False),
        daemon=True)
    server_thread.start()
    assert wait_for(lambda: _alive(), 15), "测试服务未能启动"

    case_id = seed_case()
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        print(("  [OK] " if ok else "  [FAIL] ") + name + (" | " + str(detail) if detail else ""))

    cfg = requests.get(BASE + "/api/config", timeout=10).json()
    check("配置开关暴露给前端", cfg.get("lti_enabled") and cfg.get("xapi_enabled"), cfg)

    jwks = requests.get(BASE + "/lti/jwks.json", timeout=10).json()
    check("工具 JWKS 可获取", len(jwks.get("keys", [])) == 1 and jwks["keys"][0]["kty"] == "RSA")

    local_login = requests.post(BASE + "/api/auth/login",
                                json={"username": "admin", "password": "admin123"}, timeout=10)
    check("本地账号登录未受影响", local_login.status_code == 200, local_login.status_code)

    # 学生 Launch
    state, nonce = lti_login()
    resp = callback(make_id_token(nonce, custom={"case_id": str(case_id)}), state)
    check("学生 LTI 启动回调", resp.status_code == 303 and "/#/lti/landing" in resp.headers.get("Location", ""),
          (resp.status_code, resp.text[:200]))
    cookies = {COOKIE_NAME: resp.cookies.get(COOKIE_NAME)}
    sess = requests.get(BASE + "/api/lti/session", cookies=cookies, timeout=10).json()
    check("落地接口换本地 token", bool(sess.get("token")) and sess.get("lti", {}).get("case_id") == case_id,
          {"user": sess.get("user", {}).get("role"), "case": sess.get("lti", {}).get("case_id")})
    token = sess["token"]
    headers = {"Authorization": "Bearer " + token}

    detail = requests.get(f"{BASE}/api/cases/{case_id}", headers=headers, cookies=cookies, timeout=10)
    check("LTI 学生可读取已发布病例", detail.status_code == 200, detail.status_code)

    check("xAPI 浏览语句入队", wait_for(
        lambda: requests.get(BASE + "/api/xapi/queue", timeout=5).json(), 5)
        and any(q["status"] in ("pending", "sent", "failed")
                for q in requests.get(BASE + "/api/xapi/queue", timeout=5).json()), "")
    check("xAPI 语句送达模拟 LRS", wait_for(lambda: len(lrs_received) > 0, 12),
          len(lrs_received))

    # 教师 Deep Linking
    state2, nonce2 = lti_login()
    token2 = make_id_token(
        nonce2, message_type="LtiDeepLinkingRequest",
        roles=["http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"],
        sub="teacher-1",
        deep_settings={"deep_link_return_url": PLATFORM + "/lti/deeplink-return", "data": "opaque-1"})
    dl = callback(token2, state2)
    m = re.search(r'name="dl_id" value="([^"]+)"', dl.text)
    check("教师 Deep Linking 选课页", dl.status_code == 200 and bool(m), dl.status_code)
    if m:
        submit = requests.post(BASE + "/lti/deeplink/submit",
                               data=[("dl_id", m.group(1)), ("case_id", str(case_id))],
                               timeout=10)
        mt = re.search(r'name="JWT" value="([^"]+)"', submit.text)
        check("Deep Linking 返回 JWT", submit.status_code == 200 and bool(mt), submit.status_code)
        if mt:
            jwt = mt.group(1)
            payload = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
            items = payload.get("https://purl.imsglobal.org/spec/lti-dl/claim/content_items", [])
            check("Deep Linking 内容项含病例", len(items) == 1 and items[0].get("custom", {}).get("case_id") == str(case_id), items)

    # AGS 成绩回传（教师批阅路径的底层函数）
    state3, nonce3 = lti_login()
    ags = {"endpoint": PLATFORM + "/lineitem", "lineitem": PLATFORM + "/lineitem",
           "scope": ["https://purl.imsglobal.org/spec/lti-ags/scope/score"],
           "service_versions": ["1.0"]}
    resp3 = callback(make_id_token(nonce3, sub="student-ags", ags=ags,
                                   custom={"case_id": str(case_id)}), state3)
    cookies3 = {COOKIE_NAME: resp3.cookies.get(COOKIE_NAME)}
    sess3 = requests.get(BASE + "/api/lti/session", cookies=cookies3, timeout=10).json()
    uid = sess3["user"]["id"]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO questions(case_id,question,score) VALUES(?,?,?)",
                     (case_id, "自测题", 10))
        qid = conn.execute("SELECT id FROM questions WHERE question='自测题'").fetchone()[0]
        conn.execute("INSERT INTO submissions(question_id,student_id,answer) VALUES(?,?,?)",
                     (qid, uid, "自测答案"))
    ok_ags = lti_xapi.post_grade_for_user(uid, 8, 10, "批阅通过")
    check("AGS 成绩回传成功", ok_ags and len(ags_received) == 1 and ags_received[0].get("scoreGiven") == 8,
          ags_received[:1])
    check("AGS OAuth client_credentials 已调用", len(token_requests) == 1)

    # 学生身份自动映射为 student、教师为 teacher
    with sqlite3.connect(DB_PATH) as conn:
        roles = dict(conn.execute("SELECT username,role FROM users").fetchall())
    check("角色映射正确", "teacher" in roles.values() and "student" in roles.values(), roles)

    failed = [n for n, ok, _ in checks if not ok]
    print("\n结果：%d/%d 通过" % (len(checks) - len(failed), len(checks)))
    if failed:
        print("失败项：" + ", ".join(failed))
        sys.exit(1)
