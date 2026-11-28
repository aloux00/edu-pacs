#!/usr/bin/env python3
"""EduPACS + Moodle 一键集成栈 离线镜像包制作工具（在有外网的机器上执行）。

产出可在纯内网部署的离线包目录：

    <output>/EduPACS-Moodle离线包-<日期>/
        README-离线包-先看我.txt          # 内网部署三步说明
        一键导入镜像.bat                  # Windows 内网机：导入 images.tar
        load_images.sh                    # Linux 内网机：导入 images.tar
        启动Moodle集成栈.bat              # 离线模式启动（自动 OFFLINE=1）
        edupacs/                          # 完整源码与配置（不含 .env/私钥/数据）
            docker/moodle/offline/images.tar   # docker save 归档

内网机器流程：解压 -> 一键导入镜像 -> 启动Moodle集成栈。
启动脚本检测到 OFFLINE=1 或本地镜像齐全时，自动跳过 pull/build，全程不需要外网。

用法（在已装 Docker 且联网的机器上）：
    python scripts/build_offline.py                     # 输出到 docker/moodle/deploy/
    python scripts/build_offline.py --output D:/离线包   # 指定输出目录
    python scripts/build_offline.py --no-build          # 不重新构建 app 镜像
    python scripts/build_offline.py --skip-pull         # 用本地已有镜像，不重新 pull

不依赖第三方库，Python 3.8+ 可用。
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]          # docker/moodle
SRC_ROOT = BASE.parents[1]                          # 项目根
ENV_FILE = BASE / ".env"
ENV_EXAMPLE = BASE / ".env.example"

IMAGE_ENV_KEYS = (
    "MARIADB_IMAGE",
    "MOODLE_IMAGE",
    "ORTHANC_IMAGE",
    "PYTHON_IMAGE",
    "EDUPACS_APP_IMAGE",
)
DEFAULT_IMAGES = {
    "MARIADB_IMAGE": "docker.io/library/mariadb:11.2",
    "MOODLE_IMAGE": "docker.io/bitnamilegacy/moodle:4.5",
    "ORTHANC_IMAGE": "orthancteam/orthanc:26.9.1",
    "PYTHON_IMAGE": "python:3.12-slim",
    "EDUPACS_APP_IMAGE": "edupacs-moodle-app:1.0.0",
}
BUNDLE_NAME = "EduPACS-Moodle离线包-{date}"
IGNORE_PATTERNS = shutil.ignore_patterns(
    ".git", ".venv", "venv", "__pycache__", "*.pyc", ".pytest_cache",
    ".temp", "deploy", "node_modules", "runtime", ".env", "app.db",
    "*.log", ".workbuddy", "backups", "images.tar",
)

ROOT_LAUNCHER_BAT = (
    "@echo off\r\n"
    "chcp 65001 >nul\r\n"
    "rem 离线包专用启动入口：OFFLINE=1 让一键启动跳过镜像拉取与构建\r\n"
    "set OFFLINE=1\r\n"
    "cd /d \"%~dp0edupacs\\docker\\moodle\"\r\n"
    "call \"%~dp0edupacs\\docker\\moodle\\一键启动.bat\"\r\n"
)

README_TXT = """EduPACS + Moodle 一键集成栈 离线包 {date}
================================================
本包面向【纯内网】环境，全程不需要访问互联网。

第一步：确认内网机器已装好 Docker
  - Windows：Docker Desktop（Linux 容器模式），安装器可在有网机器下载后拷入。
  - Ubuntu：docker.io + docker-compose-v2（内网源或离线 deb）。
  - 已装好可跳过。

第二步：导入镜像（只需一次）
  - Windows：双击本目录「一键导入镜像.bat」。
  - Linux：  bash load_images.sh
  - 镜像清单见 edupacs/docker/moodle/offline/镜像清单.txt。

第三步：按内网地址启动
  1) 首次启动前，把 edupacs/docker/moodle/.env.example 复制为 .env，
     修改两行（端口不变，只改主机部分）：
         MOODLE_PUBLIC_HOST=<服务器IP>:8080
         EDUPACS_PUBLIC_HOST=<服务器IP>:8000
     其余 CHANGE_ME 保持不动，启动脚本会自动生成随机密码。
  2) 双击「启动Moodle集成栈.bat」（Linux：bash edupacs/docker/moodle/start.sh）。
     脚本以离线模式启动：跳过镜像拉取与构建，其余流程与在线版完全一致。
  3) 启动完成按提示访问 Moodle / EduPACS，详见
     edupacs/docker/moodle/docs/Moodle一键集成说明.md 与 docs/纯内网离线部署.md。

已用 localhost 启动过一次？把 .env 的两个 PUBLIC_HOST 改成服务器 IP 后重跑
「启动Moodle集成栈.bat」即可，Moodle 侧工具注册会自动更新（幂等）。

停止：edupacs/docker/moodle/一键停止.bat（数据保留在 Docker 卷中）。
"""


def log(msg):
    print(msg, flush=True)


def die(msg):
    log(f"[错误] {msg}")
    sys.exit(1)


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


def run(cmd, **kw):
    log("  $ " + " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def docker_images(env):
    """按保存顺序返回镜像名列表（外部镜像在前，app 最后）。"""
    return [env.get(k, "").strip() or DEFAULT_IMAGES[k] for k in IMAGE_ENV_KEYS]


def image_exists(name):
    return subprocess.run(
        ["docker", "image", "inspect", name],
        capture_output=True).returncode == 0


def human(nbytes):
    value = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}"
        value /= 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(BASE / "deploy"), help="离线包输出目录")
    ap.add_argument("--no-build", action="store_true", help="不重新构建 app 镜像")
    ap.add_argument("--skip-pull", action="store_true", help="跳过 pull，用本地镜像")
    args = ap.parse_args()

    if shutil.which("docker") is None:
        die("未找到 docker，请在装有 Docker 且联网的机器上执行本脚本")
    try:
        subprocess.run(["docker", "version"], capture_output=True, check=True)
    except subprocess.CalledProcessError:
        die("Docker 引擎未启动，请先启动 Docker Desktop / Docker Engine")

    env = parse_env(ENV_EXAMPLE)
    env.update(parse_env(ENV_FILE))  # .env 优先，镜像名可被用户覆盖
    images = docker_images(env)
    ext_images = images[:-1]
    app_image = images[-1]

    log("==> 1/6 准备镜像")
    if args.no_build:
        if not image_exists(app_image):
            die(f"镜像 {app_image} 不存在且指定了 --no-build，请先构建或去掉该参数")
        log(f"  跳过构建，使用已有 {app_image}")
    else:
        log(f"  构建教学系统镜像 {app_image} ...")
        run(["docker", "compose", "-f", "docker-compose.moodle.yml", "build", "app"],
            cwd=str(BASE))

    for img in ext_images:
        if args.skip_pull and image_exists(img):
            log(f"  跳过 pull，使用已有 {img}")
            continue
        log(f"  拉取 {img} ...")
        run(["docker", "pull", img])

    missing = [i for i in images if not image_exists(i)]
    if missing:
        die("以下镜像不存在，无法打包：" + ", ".join(missing))

    stage = Path(args.output) / BUNDLE_NAME.format(date=date.today().isoformat())
    if stage.exists():
        log(f"==> 2/6 清理已有输出目录 {stage}")
        shutil.rmtree(stage)
    edupacs_stage = stage / "edupacs"
    log(f"==> 2/6 复制源码到 {stage}")
    shutil.copytree(SRC_ROOT, edupacs_stage, ignore=IGNORE_PATTERNS)
    (edupacs_stage / "backups").mkdir(exist_ok=True)

    # 包根目录：启动入口 / 导入脚本 / 说明（bat 用 CRLF + GBK 可读的 ASCII 安全写法）
    (stage / "启动Moodle集成栈.bat").write_bytes(
        ROOT_LAUNCHER_BAT.encode("utf-8"))
    shutil.copyfile(BASE / "offline" / "一键导入镜像.bat", stage / "一键导入镜像.bat")
    shutil.copyfile(BASE / "offline" / "load_images.sh", stage / "load_images.sh")
    (stage / "README-离线包-先看我.txt").write_text(README_TXT.format(date=date.today()),
                                                    encoding="utf-8")

    offline_dir = edupacs_stage / "docker" / "moodle" / "offline"
    offline_dir.mkdir(exist_ok=True)
    tar_path = offline_dir / "images.tar"
    log(f"==> 3/6 导出镜像（约几分钟，产物 {tar_path.name}）")
    run(["docker", "save", "-o", str(tar_path), *images])

    sizes = {}
    for img in images:
        out = subprocess.run(
            ["docker", "image", "inspect", img, "--format", "{{.Size}}"],
            capture_output=True, text=True, check=True).stdout.strip()
        sizes[img] = int(out)
    manifest = {
        "打包日期": date.today().isoformat(),
        "镜像": [{"名称": k, "解压后大小": human(v)} for k, v in sizes.items()],
        "说明": "images.tar 为 docker save 归档，导入用 docker load，无需解压。",
    }
    (offline_dir / "镜像清单.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"EduPACS-Moodle 离线镜像清单 {date.today()}", ""]
    for k, v in sizes.items():
        lines.append(f"  {k}  解压后 {human(v)}")
    lines += ["", "内网导入：docker load -i images.tar（或用包根目录的一键导入脚本）"]
    (offline_dir / "镜像清单.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    log("==> 4/6 计算 SHA-256 校验和")
    sha = hashlib.sha256()
    with tar_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            sha.update(chunk)
    (offline_dir / "images.tar.sha256").write_text(
        f"{sha.hexdigest()}  images.tar\n", encoding="utf-8")
    log(f"  images.tar  {human(tar_path.stat().st_size)}  sha256={sha.hexdigest()[:16]}...")

    log("==> 5/6 自检离线包关键文件")
    for rel in ("edupacs/docker/moodle/docker-compose.moodle.yml",
                "edupacs/docker/moodle/bootstrap/bootstrap.php",
                "edupacs/docker/moodle/lrs/lrs.py",
                "edupacs/docker/moodle/scripts/configure.py",
                "edupacs/docker/orthanc-docker.json",
                "一键导入镜像.bat", "启动Moodle集成栈.bat",
                "edupacs/docker/moodle/offline/images.tar"):
        if not (stage / rel).exists():
            die(f"离线包缺少关键文件 {rel}")
    log("  关键文件齐全")

    log("==> 6/6 完成")
    total = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file())
    log(f"  离线包目录：{stage}")
    log(f"  总大小：{human(total)}")
    log("  拷贝到内网机器后：一键导入镜像 → 启动Moodle集成栈")
    return 0


if __name__ == "__main__":
    sys.exit(main())
