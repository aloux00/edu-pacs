#!/usr/bin/env bash
# EduPACS + Moodle 离线镜像导入（Linux 内网机，约 6 GB，需数分钟）
set -euo pipefail
cd "$(dirname "$0")"   # edupacs/docker/moodle/offline

command -v docker >/dev/null || { echo "[错误] 未找到 docker，请先安装 Docker Engine + compose 插件"; exit 1; }
docker version >/dev/null 2>&1 || { echo "[错误] Docker 引擎未启动"; exit 1; }
[ -f images.tar ] || { echo "[错误] 未找到 images.tar，请确认离线包完整"; exit 1; }

echo "============================================================"
echo " EduPACS + Moodle 离线镜像导入"
echo "============================================================"

echo "[1/2] 校验镜像包完整性 ..."
if command -v sha256sum >/dev/null; then
  actual=$(sha256sum images.tar | cut -d' ' -f1)
  expected=$(cut -d' ' -f1 images.tar.sha256)
  echo "  实际: $actual"
  echo "  期望: $expected"
  if [ "$actual" = "$expected" ]; then
    echo "  [OK] SHA-256 校验通过。"
  else
    echo "  [警告] SHA-256 不匹配！文件可能损坏。"
    read -r -p "是否仍要继续导入? [y/N] " ans
    [ "${ans,,}" = "y" ] || { echo "已取消。"; exit 1; }
  fi
else
  echo "  [跳过] 未找到 sha256sum，跳过校验。"
fi

echo "[2/2] 导入镜像 ..."
docker load -i images.tar

echo ""
echo "完成！已导入镜像："
docker images --format "  {{.Repository}}:{{.Tag}}" | grep -E "mariadb|bitnamilegacy|orthancteam|python|edupacs" || true
echo ""
echo "下一步：bash edupacs/docker/moodle/start.sh 启动教学平台（离线模式自动跳过拉取/构建）。"
echo "提示：首次启动前请按 README 编辑 .env 中的两个 PUBLIC_HOST。"
