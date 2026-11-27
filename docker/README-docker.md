# EduPACS Docker 部署（Ubuntu Server 内网，离线）

> 配套文档：《部署与使用手册》4.5 节（完整版，含排错与数据迁移）；本文件为离线包内随附的快速版。

## 一、适用场景

- 目标：Ubuntu Server（推荐 24.04 LTS），**内网无外网**。
- 交付物：`EduPACS-Docker离线镜像包-<日期>.tar.gz`（含两个镜像 + compose + 配置 + 部署脚本）。
- Windows 原生部署包（zip）与 Docker 方案**双轨并存**：同一套应用代码与数据结构，可互相迁移。

## 二、前置条件（宿主机一次性准备）

1. Ubuntu Server 已安装 Docker Engine + compose 插件。
   - 内网 apt 镜像源可装：`sudo apt install -y docker.io docker-compose-v2`
   - 纯离线：用官方 `.deb` 离线安装（需 `containerd`/`runc` 等依赖链），或直接选
     **Windows 双轨 zip / Ubuntu 原生 apt 方案**（见手册 4.4 节，不依赖 Docker）。
2. 解压离线包：
   ```bash
   tar xzf EduPACS-Docker离线镜像包-*.tar.gz -C edupacs-docker && cd edupacs-docker
   ```

## 三、部署（一条命令）

```bash
sudo bash deploy-offline.sh
```

脚本自动完成：导入镜像 → 生成随机 Orthanc 服务账号密码（写入 `.env`，权限 600）→
`docker compose up -d` → 等待就绪 → 打印访问地址、账号、冒烟测试命令。

浏览器打开 `http://<服务器IP>:8000`，`admin / admin123`（首登强制改密）。

## 四、部署后必做：冒烟测试（32 项）

```bash
U=$(grep ^EDUPACS_ORTHANC_USER= .env | cut -d= -f2-)
P=$(grep ^EDUPACS_ORTHANC_PASS= .env | cut -d= -f2-)
docker compose exec app python tools/smoke_test.py \
  --base http://127.0.0.1:8000 --orthanc http://orthanc:8042 \
  --orthanc-user "$U" --orthanc-pass "$P"
```

32/32 全部通过再交付师生使用（覆盖：上传脱敏零泄漏、盲读锁、权限隔离、反代只读保护、备份加密与沙箱恢复等）。

## 五、安全模型（与 Windows 原生版对齐）

| 项 | Docker 方案 | Windows 原生方案 |
|---|---|---|
| 师生入口 | 仅 8000（app 容器） | 仅 8000 |
| Orthanc 8042 | 不发布端口，仅 compose 内部网络，跨容器访问仍需 Basic 认证 | `RemoteAccessAllowed=false`，仅本机回环 + 认证 |
| DICOM 4242 | 按需发布（设备直推） | 按需开放防火墙 |
| 凭据 | `.env` 单点维护（600 权限） | `orthanc\orthanc.json` |
| 数据 | volume `edupacs_orthanc-storage` / `edupacs_app-data`；加密归档落 `BACKUP_DIR` | `orthanc\storage\` / `app\data\`；归档落 `backups\` |

## 六、日常运维

```bash
docker compose ps                       # 状态
docker compose logs -f app              # 应用日志
docker compose down                     # 停止（数据保留）
docker compose up -d                    # 再启动
docker compose restart                 # 重启（改 .env 后用 up -d 重建）

# 内置加密备份：app 镜像已含 tools/backup.py，compose 已挂 orthanc-storage
# （影像+索引）与宿主机 ./backups，因此容器内可直接做完整加密备份
docker compose exec app python tools/backup.py backup --password '<备份密码>' --note '每日备份'
docker compose exec app python tools/backup.py list
docker compose exec app python tools/backup.py verify backups/<归档> --password '<备份密码>'

# 异机备份：宿主机先挂载 NFS/共享目录，在 .env 设 BACKUP_DIR=/mnt/nfs/edupacs-backups
# 然后 docker compose up -d 重建 app 容器，执行同样的 backup 命令即写入异机目录。

# 恢复：先停服务，再用 --no-deps 避免依赖把 orthanc 拉起来写共享卷
docker compose down
docker compose run --rm --no-deps app python tools/backup.py restore backups/<归档> --password '<备份密码>' --apply
docker compose up -d
```

升级：拿到新离线包 → `docker load` → `.env` 不动 → `docker compose up -d`（volume 数据自动沿用）。

## 七、常见问题

| 现象 | 处理 |
|---|---|
| 部署后 8000 打不开 | `docker compose ps` 看容器状态；`docker compose logs --tail=100 app` |
| OHIF 阅片空白 | 容器内探测：`docker compose exec app python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/api/orthanc/health',timeout=8).read().decode())"` 应含 `"ok": true`；否则查 `.env` 凭据与 orthanc 容器日志 |
| 病例列表可见但阅片器全黑 | 实际根因多为 OHIF 3.13 bundle 的 ES2022 静态块在 Chrome/Edge 85~90 报 `Unexpected token '{'`，脚本中断导致无图像。包15 已在代理层降级静态块；旧容器可临时 `docker cp app/backend/main.py edupacs-app:/edu-pacs/app/backend/main.py && docker compose restart app`，永久修复请重建 app 镜像 |
| `/frames/1` 返回 400、`Couldn't retrieve ... frames/1` | Orthanc 26.9.1 的 RetrieveFrames **只接受** `multipart/related; type=application/octet-stream; transfer-syntax=*`。不要把 `app-config.js` 的 `acceptHeader` 改成 `application/dicom`（那是 RetrieveInstance 的类型，会破坏 frames）。保持 Orthanc 原生配置即可 |
| 控制台 `getRegistrations of undefined` | HTTP 非 localhost 下 `navigator.serviceWorker` 不存在，OHIF 脚本未判空。包15 已在代理层加三元守卫；该报错本身不影响阅片 |
| 改了 `.env` 没生效 | `docker compose up -d`（会按新环境重建容器） |
| 设备直推收不到 | 检查 `ufw allow 4242/tcp` 与设备端 AET 配置 `EDUPACS@<IP>:4242` |
| 从远程 PACS 拉片（C-MOVE）失败 | 对端 PACS 上 EDUPACS 节点地址必须配成**宿主机 IP:4242**（发布端口）。bridge 网络下容器对外连接经 NAT 源地址为宿主机 IP，多数 PACS 可正常回连；个别 PACS 拿容器内地址回连失败时改用 host 网络：把 compose 里两个服务的 `ports` 段删掉，`orthanc` 与 `app` 都加 `network_mode: host`，`app` 环境变量 `ORTHANC_URL` 改 `http://127.0.0.1:8042`，`orthanc-docker.json` 的 `RemoteAccessAllowed` 改 `false`（8042 仍仅本机可达，安全不变） |
| 想直接看 Orthanc 后台 | 临时诊断：`docker compose exec orthanc python /probes/test-aliveness.py`；或临时给 ports 加 `127.0.0.1:8042:8042`（仅绑定回环，用完移除） |

## 附录：离线镜像包制作（开发/有网机器）

```bash
bash docker/build-offline-images.sh     # 详见脚本头注释；产物在 deploy/ 下
```

> AI生成
