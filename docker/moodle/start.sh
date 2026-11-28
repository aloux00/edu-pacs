#!/usr/bin/env bash
# EduPACS + Moodle 一键集成栈（Linux / macOS）
set -euo pipefail
cd "$(dirname "$0")"

PY="${PY:-python3}"
command -v docker >/dev/null || { echo "未找到 docker"; exit 1; }
docker version >/dev/null 2>&1 || { echo "Docker 引擎未启动"; exit 1; }

echo "[1/5] 生成配置与随机凭据"
"$PY" scripts/configure.py --init
if [ "${OFFLINE:-0}" = "1" ]; then
  echo "[2/5] 离线模式：跳过镜像拉取与构建（镜像应已通过 load_images.sh 导入）"
  docker compose -f docker-compose.moodle.yml up -d
else
  echo "[2/5] 拉取镜像并构建 EduPACS 应用镜像"
  docker compose -f docker-compose.moodle.yml pull mariadb moodle orthanc lrs
  docker compose -f docker-compose.moodle.yml up -d --build
fi
echo "[3/5] 在 Moodle 中注册 LTI 1.3 工具、创建课程与活动"
docker compose -f docker-compose.moodle.yml --profile tools run --rm bootstrap
echo "[4/5] 回写 Deployment ID 并重启 EduPACS 后端"
"$PY" scripts/configure.py --finalize
echo "[5/5] 验证服务端点"
"$PY" scripts/configure.py --status
