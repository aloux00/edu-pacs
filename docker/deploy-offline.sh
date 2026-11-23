#!/usr/bin/env bash
# EduPACS 内网 Ubuntu Server 部署脚本（离线，需 root）
# ============================================================================
# 前置：已解压离线镜像包（本目录含 images.tar / docker-compose.yml /
#       orthanc-docker.json），宿主机已安装 docker + compose 插件。
# 功能：导入镜像 → 生成 .env（首次随机 Orthanc 服务账号密码）→ 启动 →
#       健康等待 → 打印访问地址/账号/冒烟测试命令。
# 幂等：重复执行安全（.env 已存在则复用原凭据，up -d 不重建健康容器）。
set -euo pipefail

cd "$(dirname "$0")"

SVC_USER_DEFAULT="edupacs_service"
die() { echo "[错误] $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root（或 sudo bash deploy-offline.sh）执行"
command -v docker >/dev/null 2>&1 || die "未找到 docker；内网离线安装见 README-Docker部署说明.md 附录"
docker compose version >/dev/null 2>&1 || die "未找到 docker compose 插件"

# 1) 导入离线镜像
if [ -f images.tar ]; then
  echo "==> 1/5 导入离线镜像（docker load）"
  docker load -i images.tar
else
  echo "==> 1/5 未发现 images.tar（跳过导入，假定镜像已导入）"
fi

# 2) 生成/复用 .env
if [ -f .env ]; then
  echo "==> 2/5 复用已有 .env（Orthanc 服务账号不变）"
else
  echo "==> 2/5 生成 .env：随机 Orthanc 服务账号密码"
  RAND_PASS=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24)
  cat > .env <<EOF
# EduPACS Docker 部署配置（deploy-offline.sh 生成）
# Orthanc 服务账号：app 反代认证用。如需修改，改完执行 docker compose up -d 重建生效。
EDUPACS_ORTHANC_USER=${SVC_USER_DEFAULT}
EDUPACS_ORTHANC_PASS=${RAND_PASS}
EOF
  chmod 600 .env
  echo "    用户名：${SVC_USER_DEFAULT}   密码：${RAND_PASS}（已写入 .env，妥善保管）"
fi

# 3) 启动
echo "==> 3/5 启动容器（docker compose up -d）"
docker compose up -d

# 4) 等待就绪（最长 120 秒；探测在 app 容器内执行，不依赖宿主机 curl）
echo "==> 4/5 等待服务就绪（最长 120 秒）"
OK=""
for i in $(seq 1 60); do
  if docker compose exec -T app python -c \
    "import urllib.request,json;exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/orthanc/health',timeout=5)).get('ok') else 1)" \
    2>/dev/null; then
    OK=1; break
  fi
  sleep 2
done
[ -n "${OK}" ] || { echo "等待超时。排查：docker compose ps / docker compose logs --tail=100 app orthanc"; exit 1; }
echo "    就绪。"

# 5) 打印交付信息
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
U=$(grep '^EDUPACS_ORTHANC_USER=' .env | cut -d= -f2-)
P=$(grep '^EDUPACS_ORTHANC_PASS=' .env | cut -d= -f2-)

cat <<EOF

============================================================
 EduPACS 部署完成
   教学系统入口   ：http://${LAN_IP:-<服务器IP>}:8000
                     （本机访问 http://127.0.0.1:8000）
   默认账号       ：admin / admin123（首次登录强制改密）
                     teacher01 / teach123
   DICOM 设备直推 ：EDUPACS@${LAN_IP:-<服务器IP>}:4242
   8042           ：未对宿主机发布，仅容器网络内部可达（安全边界）

 防火墙（如启用 ufw）：
   ufw allow 8000/tcp && ufw allow 4242/tcp

 部署后必做冒烟（32 项全通过再交付）：
   docker compose exec app python tools/smoke_test.py \\
     --base http://127.0.0.1:8000 --orthanc http://orthanc:8042 \\
     --orthanc-user '${U}' --orthanc-pass '${P}'

 日常运维：
   看日志 docker compose logs -f app | 停止 docker compose down
   加密备份 docker compose exec app python tools/backup.py backup --password '<备份密码>'
             （归档落 ./backups；异机在 .env 设 BACKUP_DIR 后 docker compose up -d 重建）
   手工卷备 docker run --rm -v edupacs_orthanc-storage:/d -v edupacs_app-data:/a -v \$PWD:/b alpine tar czf /b/edupacs-backup-\$(date +%F).tgz /d /a
============================================================
EOF
