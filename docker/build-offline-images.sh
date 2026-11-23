#!/usr/bin/env bash
# EduPACS Docker 离线镜像包制作脚本（在【有外网】且装有 Docker 的 Linux 机器上执行）
# ============================================================================
# 产出：deploy/EduPACS-Docker离线镜像包-<日期>.tar.gz
#       （含 images.tar 两个镜像 + compose + Orthanc 配置 + 内网部署脚本 + 说明）
# 内网 Ubuntu Server 拿到该包后：解压 → bash deploy-offline.sh 一条命令完成部署。
#
# 用法：
#   bash docker/build-offline-images.sh                 # 全默认
#   ORTHANC_IMAGE=orthancteam/orthanc:26.9.1 bash docker/build-offline-images.sh
#   PIP_INDEX=https://mirrors.aliyun.com/pypi/simple bash docker/build-offline-images.sh
#
# 说明：
#   - Orthanc 用官方 orthancteam/orthanc 镜像，tag = 发行批次号 26.9.1（对应 core 1.13.x，
#     与 Windows 包同源；注意 orthancteam 系列没有 1.13.x 这类 core 版本 tag，
#     jodogne/orthanc 才用 core 版本做 tag）
#   - app 镜像按 requirements.lock.txt 锁定依赖（与开发机版本完全一致）
set -euo pipefail

cd "$(dirname "$0")/.."          # 项目根目录 = docker build 的上下文

APP_IMAGE=${APP_IMAGE:-edupacs-app:1.0.0}
ORTHANC_IMAGE=${ORTHANC_IMAGE:-orthancteam/orthanc:26.9.1}
PIP_INDEX=${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}
DATE=$(date +%Y%m%d)
OUT_TGZ="deploy/EduPACS-Docker离线镜像包-${DATE}.tar.gz"
STAGE=".temp/docker-offline-${DATE}"

die() { echo "[错误] $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker"
docker compose version >/dev/null 2>&1 || die "未找到 docker compose 插件（docker compose version 失败）"

echo "==> 1/6 拉取 Orthanc 官方镜像：${ORTHANC_IMAGE}"
docker pull "${ORTHANC_IMAGE}"

echo "==> 2/6 构建教学系统镜像：${APP_IMAGE}（pip 源 ${PIP_INDEX}）"
docker build -f docker/Dockerfile \
      --build-arg PIP_INDEX="${PIP_INDEX}" \
      -t "${APP_IMAGE}" .

echo "==> 3/6 导出镜像为 images.tar"
rm -rf "${STAGE}"; mkdir -p "${STAGE}"
docker save -o "${STAGE}/images.tar" "${ORTHANC_IMAGE}" "${APP_IMAGE}"

echo "==> 4/6 组装离线包（compose / 配置 / 部署脚本 / 说明）"
cp docker/docker-compose.yml      "${STAGE}/docker-compose.yml"
cp docker/orthanc-docker.json     "${STAGE}/orthanc-docker.json"
cp docker/.env.example            "${STAGE}/.env.example"
cp docker/deploy-offline.sh       "${STAGE}/deploy-offline.sh"
cp docker/README-docker.md        "${STAGE}/README-Docker部署说明.md" 2>/dev/null || true
# 备份工具与配置模板随镜像分发，这里另附源码副本便于宿主机侧查阅/二次定制
cp tools/backup.py                "${STAGE}/tools-backup.py" 2>/dev/null || true
cp config/backup.json             "${STAGE}/backup-config.json" 2>/dev/null || true

cat > "${STAGE}/镜像清单.txt" <<EOF
EduPACS Docker 离线镜像包 ${DATE}
- ${ORTHANC_IMAGE}   （Orthanc 1.13 系列官方镜像，含 DicomWeb/Explorer2/GDCM；OHIF 插件在 plugins-available 中，已由 orthanc-docker.json 的 Plugins 字段显式启用）
- ${APP_IMAGE}            （教学系统：FastAPI 后端 + 纯静态前端 + 冒烟测试工具 + 加密备份 CLI）
镜像内已含：tools/backup.py、config/backup.json；Orthanc 索引/影像经 orthanc-storage 共享卷读取。
归档默认写宿主机 ./backups，异机备份在 .env 里设 BACKUP_DIR=/mnt/nfs/edupacs-backups。
制作环境：$(docker --version | head -n1) / $(docker compose version | head -n1)
EOF

echo "==> 5/6 压缩 ${OUT_TGZ}"
mkdir -p deploy
tar -C "${STAGE}" -czf "${OUT_TGZ}" .

echo "==> 6/6 校验和"
sha256sum "${OUT_TGZ}" | tee "${OUT_TGZ}.sha256"

echo ""
echo "完成：${OUT_TGZ}"
echo "拷贝到内网 Ubuntu Server 后执行："
echo "  tar xzf EduPACS-Docker离线镜像包-*.tar.gz -C edupacs-docker"
echo "  cd edupacs-docker && bash deploy-offline.sh"
