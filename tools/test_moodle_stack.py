#!/usr/bin/env python3
"""Moodle 一键集成栈静态自测：不依赖 Docker 引擎或 Moodle 实例。"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOODLE_DIR = ROOT / "docker" / "moodle"
sys.path.insert(0, str(ROOT))

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [OK]   {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


print("Moodle 一键集成栈自测")
print("-" * 50)

# 1. 配置生成
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td) / "stack"
    shutil.copytree(MOODLE_DIR, tmp, ignore=shutil.ignore_patterns("runtime", ".env"))
    cfg = load_module("edupacs_configure", tmp / "scripts" / "configure.py")
    cfg.BASE = tmp
    cfg.ENV_FILE = tmp / ".env"
    cfg.ENV_EXAMPLE = tmp / ".env.example"
    cfg.RUNTIME = tmp / "runtime"
    cfg.CONFIG_DIR = cfg.RUNTIME / "config"
    env = cfg.init_env()
    cfg.write_configs(env)

    check(".env 已生成", cfg.ENV_FILE.exists())
    check("所有 CHANGE_ME 已替换", "CHANGE_ME" not in cfg.ENV_FILE.read_text(encoding="utf-8"))
    lti = json.loads((cfg.CONFIG_DIR / "lti.json").read_text(encoding="utf-8"))
    xapi = json.loads((cfg.CONFIG_DIR / "xapi.json").read_text(encoding="utf-8"))
    reg = lti["registrations"][0]
    check("LTI 已启用且只有一条注册", lti["enabled"] and len(lti["registrations"]) == 1)
    check("client_id 已生成", reg["client_id"].startswith("edupacs-"), reg["client_id"])
    check("初始 deployment_id 为空", reg["deployment_id"] == "")
    check("iss 来自 Moodle 公开地址", reg["iss"] == "http://" + env["MOODLE_PUBLIC_HOST"])
    check("xAPI 指向内置 LRS", xapi["endpoint"] == "http://lrs:8001/statements")
    check("xAPI 认证头格式正确", xapi["auth_header"].lower().startswith("basic "))

    # 模拟 Moodle 引导结果回写
    cfg.write_configs(env, "42")
    lti2 = json.loads((cfg.CONFIG_DIR / "lti.json").read_text(encoding="utf-8"))
    check("deployment_id 可回写", lti2["registrations"][0]["deployment_id"] == "42")

# 2. compose 编排内容
compose = (MOODLE_DIR / "docker-compose.moodle.yml").read_text(encoding="utf-8")
for service in ("mariadb:", "moodle:", "orthanc:", "app:", "lrs:", "bootstrap:"):
    check(f"compose 含服务 {service.strip()}", service in compose)
check("AGS 主机映射已注入", "EDUPACS_LTI_SERVER_HOST_MAP" in compose)
check("Orthanc 8042 不对外发布", "8042:8042" not in compose)
check("Bootstrap 为按需 profile", 'profiles: ["tools"]' in compose)

# 3. Moodle 引导脚本关键点
bootstrap = (MOODLE_DIR / "bootstrap" / "bootstrap.php").read_text(encoding="utf-8")
for token in ("lti_add_type", "lti_update_type", "LTI_VERSION_1P3", "LTI_JWK_KEYSET",
              "lti_publickeyset", "lti_initiatelogin", "add_moduleinfo", "lti-result.json"):
    check(f"bootstrap 使用 {token}", token in bootstrap)

# 4. 后端容器地址映射
os.environ["EDUPACS_LTI_SERVER_HOST_MAP"] = "http://localhost:8080=http://moodle"
from app.backend import lti_xapi  # noqa: E402

check("AGS 地址映射外部→内部",
      lti_xapi._server_url("http://localhost:8080/mod/lti/score.php") == "http://moodle/mod/lti/score.php")
check("无映射地址保持不变",
      lti_xapi._server_url("http://other:9000/x") == "http://other:9000/x")

print("-" * 50)
if failures:
    print(f"失败 {len(failures)} 项：" + ", ".join(failures))
    sys.exit(1)
print("全部通过")
