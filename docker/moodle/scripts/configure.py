#!/usr/bin/env python3
"""EduPACS + Moodle 一键集成栈的宿主侧配置与验证工具。

用法：
    python configure.py --init        # 生成 .env、runtime/config（幂等）
    python configure.py --finalize    # 读取 Moodle 引导结果，回写 deployment_id 并重启 app
    python configure.py --status      # 打印访问地址与服务端点状态
    python configure.py --reset-secrets  # 重新生成凭据（需重新启动整栈）

不依赖第三方库，Python 3.8+ 可用。
"""
import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
ENV_FILE = BASE / ".env"
ENV_EXAMPLE = BASE / ".env.example"
RUNTIME = BASE / "runtime"
CONFIG_DIR = RUNTIME / "config"
COMPOSE_FILE = BASE / "docker-compose.moodle.yml"
RESULT_IN_CONTAINER = "/bitnami/moodledata/edupacs-bootstrap/lti-result.json"

GENERATED_KEYS = {
    "MOODLE_ADMIN_PASS": lambda: secrets.token_urlsafe(18),
    "MOODLE_DB_PASS": lambda: secrets.token_urlsafe(24),
    "MARIADB_ROOT_PASS": lambda: secrets.token_urlsafe(24),
    "DEMO_TEACHER_PASS": lambda: "Tea" + secrets.token_urlsafe(9),
    "DEMO_STUDENT_PASS": lambda: "Stu" + secrets.token_urlsafe(9),
    "EDUPACS_ORTHANC_PASS": lambda: secrets.token_urlsafe(24),
    "LTI_CLIENT_ID": lambda: "edupacs-" + secrets.token_hex(12),
}

XAPI_KEYS = ("XAPI_BASIC_TOKEN", "XAPI_AUTH_HEADER")


def log(msg):
    print(msg, flush=True)


def parse_env(path):
    data = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip()
    return data


def make_xapi_header(token=None):
    token = token or secrets.token_urlsafe(24)
    encoded = base64.b64encode(("lrs:" + token).encode("utf-8")).decode("ascii")
    return token, "Basic " + encoded


def init_env(force=False):
    env = parse_env(ENV_EXAMPLE)
    existing = parse_env(ENV_FILE) if ENV_FILE.exists() else {}
    if existing:
        env.update(existing)  # 保留用户已有配置

    changed = False
    for key, gen in GENERATED_KEYS.items():
        val = env.get(key, "")
        if force or not val or val == "CHANGE_ME":
            env[key] = gen()
            changed = True

    token = env.get("XAPI_BASIC_TOKEN", "")
    header = env.get("XAPI_AUTH_HEADER", "")
    if force or not token or not header or header == "CHANGE_ME" or not header.lower().startswith("basic "):
        token, header = make_xapi_header()
        env["XAPI_BASIC_TOKEN"] = token
        env["XAPI_AUTH_HEADER"] = header
        changed = True

    if not ENV_FILE.exists() or changed:
        lines = ["# 由 configure.py 自动生成；修改后请重启整栈", ""]
        for k, v in env.items():
            lines.append(f"{k}={v}")
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"[init] 已写入 {ENV_FILE}")
    else:
        log(f"[init] 沿用已有 {ENV_FILE}")
    return env


def render_template(name, env, deployment_id=""):
    tmpl = (BASE / "templates" / name).read_text(encoding="utf-8")
    mapping = {
        "__EDUPACS_PUBLIC_HOST__": env.get("EDUPACS_PUBLIC_HOST", "localhost:8000"),
        "__MOODLE_PUBLIC_HOST__": env.get("MOODLE_PUBLIC_HOST", "localhost:8080"),
        "__LTI_CLIENT_ID__": env.get("LTI_CLIENT_ID", ""),
        "__LTI_DEPLOYMENT_ID__": deployment_id,
        "__XAPI_AUTH_HEADER__": env.get("XAPI_AUTH_HEADER", ""),
    }
    for k, v in mapping.items():
        tmpl = tmpl.replace(k, str(v))
    return json.loads(tmpl)


def write_configs(env, deployment_id=""):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lti_path = CONFIG_DIR / "lti.json"
    if lti_path.exists() and deployment_id:
        try:
            current = json.loads(lti_path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
        current.update(render_template("lti.json.tmpl", env, deployment_id))
        lti = current
    else:
        lti = render_template("lti.json.tmpl", env, deployment_id)
    lti_path.write_text(json.dumps(lti, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    xapi_path = CONFIG_DIR / "xapi.json"
    xapi_path.write_text(
        json.dumps(render_template("xapi.json.tmpl", env), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    log(f"[init] 已写入 {lti_path} / {xapi_path}")
    return lti_path, xapi_path


def compose(*args, check=True, capture=False):
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(cmd, cwd=str(BASE), check=check,
                          capture_output=capture, text=capture, encoding="utf-8" if capture else None)


def finalize():
    env = parse_env(ENV_FILE)
    if not env:
        log("[finalize] 错误：.env 不存在，请先运行 --init")
        return 1
    log("[finalize] 等待 Moodle 引导结果 ...")
    result = None
    for attempt in range(60):
        proc = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T", "moodle", "cat", RESULT_IN_CONTAINER],
            cwd=str(BASE), capture_output=True, text=True, encoding="utf-8")
        if proc.returncode == 0:
            try:
                result = json.loads(proc.stdout.strip())
                break
            except Exception:
                pass
        time.sleep(2)
    if result is None:
        log("[finalize] 超时：未取得 lti-result.json，请检查 bootstrap 容器日志")
        return 1

    lti_path, _ = write_configs(env, str(result.get("deployment_id", "")))
    (RUNTIME).mkdir(parents=True, exist_ok=True)
    (RUNTIME / "bootstrap.ok").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log("[finalize] LTI 注册信息已回写：" + json.dumps(result, ensure_ascii=False))
    log("[finalize] 重启 EduPACS 后端使配置生效 ...")
    compose("restart", "app")
    return 0


def probe(url, label):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=6) as resp:
            log(f"  [OK]   {label}: {resp.status} {url}")
            return True
    except urllib.error.HTTPError as exc:
        log(f"  [WARN] {label}: HTTP {exc.code} {url}")
        return exc.code < 500
    except Exception as exc:
        log(f"  [FAIL] {label}: {str(exc)[:120]} {url}")
        return False


def status():
    env = parse_env(ENV_FILE)
    if not env:
        log("[status] .env 不存在，请先运行 --init")
        return 1
    moodle = "http://" + env.get("MOODLE_PUBLIC_HOST", "localhost:8080")
    edupacs = "http://" + env.get("EDUPACS_PUBLIC_HOST", "localhost:8000")
    log("一键集成栈状态：")
    probe(moodle + "/login/index.php", "Moodle 登录页")
    probe(edupacs + "/api/config", "EduPACS 配置接口")
    probe(edupacs + "/lti/jwks.json", "EduPACS 工具 JWKS")
    probe(edupacs + "/ohif/", "OHIF 阅片器")
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T", "app",
         "python", "-c", "import urllib.request;print(urllib.request.urlopen('http://lrs:8001/health',timeout=5).read().decode())"],
        cwd=str(BASE), capture_output=True, text=True, encoding="utf-8")
    log("  " + ("[OK]   " if proc.returncode == 0 else "[FAIL] ") + "LRS: " +
        (proc.stdout.strip() or proc.stderr.strip()[:160]))

    log("")
    log("访问地址：")
    log(f"  Moodle   {moodle}   管理员 {env.get('MOODLE_ADMIN_USER','admin')} / {env.get('MOODLE_ADMIN_PASS','')}")
    log(f"  演示教师 {env.get('DEMO_TEACHER_USER','teacher1')} / {env.get('DEMO_TEACHER_PASS','')}")
    log(f"  演示学生 {env.get('DEMO_STUDENT_USER','student1')} / {env.get('DEMO_STUDENT_PASS','')}")
    log(f"  EduPACS  {edupacs}")
    marker = RUNTIME / "bootstrap.ok"
    if marker.exists():
        data = json.loads(marker.read_text(encoding="utf-8"))
        log(f"  LTI 课程 {data.get('course_url','')}")
        log(f"  阅片活动 {data.get('activity_url','')}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="生成 .env 与 runtime/config")
    ap.add_argument("--finalize", action="store_true", help="回写 LTI deployment_id 并重启 app")
    ap.add_argument("--status", action="store_true", help="显示状态与访问地址")
    ap.add_argument("--reset-secrets", action="store_true", help="重新生成随机凭据")
    args = ap.parse_args()
    if not (args.init or args.finalize or args.status or args.reset_secrets):
        args.init = True

    if args.init or args.reset_secrets:
        env = init_env(force=args.reset_secrets)
        write_configs(env)
        if args.reset_secrets:
            log("[reset] 凭据已更新，请重新运行一键启动.bat")
    if args.finalize:
        rc = finalize()
        if rc:
            return rc
    if args.status:
        return status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
