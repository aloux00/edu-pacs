# EduPACS + Moodle + OHIF + LTI 1.3 + xAPI 一键集成栈

把本目录整体放到任意位置，双击 `一键启动.bat`（Linux/macOS 用 `bash start.sh`）即可。脚本会自动完成：

1. 生成 `.env` 与全部随机密码/凭据；
2. 启动 MariaDB、Moodle、Orthanc、EduPACS、内置 LRS；
3. 等 Moodle 安装完成后，自动创建课程、演示师生账号、LTI 1.3 工具与「影像阅片」活动；
4. 把 Moodle 生成的 **Deployment ID** 回写 EduPACS 配置并重启后端；
5. 输出访问地址、账号和端点自检结果。

## 一、环境与端口

| 组件 | 浏览器地址 | 说明 |
| --- | --- | --- |
| Moodle | http://localhost:8080 | 管理员默认 `admin`，密码见启动后输出 |
| EduPACS | http://localhost:8000 | 含 `/ohif` 阅片反代；账号体系独立于 Moodle |
| LRS | 容器内部 `http://lrs:8001/statements` | 不对外发布，避免 xAPI 数据外泄 |
| Orthanc | 不发布 8042；DICOM 直推端口 4242 | 与 Windows 版安全边界一致 |

前置条件：Docker Desktop（Linux 容器）或 Docker Engine + Compose v2；Python 3.8+（仅宿主脚本使用）。首次启动需要联网拉镜像与 pip 依赖。

**实测状态**：本集成栈已在 Docker Desktop 29.9.2 上完整跑通并验证：Moodle 登录、课程与 LTI 活动创建、教师/学生 OIDC 启动、EduPACS 会话落地、病例上传与图片脱敏、xAPI 语句送达 LRS、AGS 成绩写入 Moodle 成绩簿（ gradebook 中可见 `EduPACS 教学作答` 分数）。

局域网使用时，把 `.env` 中 `MOODLE_PUBLIC_HOST` / `EDUPACS_PUBLIC_HOST` 改成 `服务器IP:8080` / `服务器IP:8000`，再执行一次一键启动。

## 二、启动后怎么用

启动脚本最后会输出类似：

```
Moodle   http://localhost:8080   管理员 admin / xxxxxxxx
演示教师 teacher1 / xxxxxxxx
演示学生 student1 / xxxxxxxx
EduPACS  http://localhost:8000
LTI 课程 http://localhost:8080/course/view.php?id=2
阅片活动 http://localhost:8080/mod/lti/view.php?id=3
```

流程：

1. 用 `teacher1` 登录 Moodle → 进入课程 → 点击「EduPACS 影像阅片（LTI 1.3）」；
2. 浏览器经 OIDC 跳转到 EduPACS，自动建号并以教师身份进入；
3. 发布病例后，学生用 `student1` 从同一活动进入，查看病例并打开 OHIF 阅片器；
4. 学生作答、教师批阅后，成绩按 AGS 写回 Moodle 成绩簿；浏览/作答/批阅事件写入内置 LRS。

## 三、自动完成的关键配置

Moodle 侧（`lti_types` + `lti_types_config`）：

| 项 | 值 |
| --- | --- |
| LTI 版本 | `1.3.0` |
| Client ID | `.env` 的 `LTI_CLIENT_ID` |
| Deployment ID | Moodle 工具类型 ID，由引导脚本回写 |
| Initiate login URL | `http://<EduPACS>/lti/login` |
| Redirection URI | `http://<EduPACS>/lti/callback` |
| Public keyset URL | `http://app:8000/lti/jwks.json`（容器内部地址） |
| Deep Linking | 开启，教师可在新增活动时勾选病例 |
| AGS | 开启，成绩回传 |
| 姓名/邮箱传递 | 关闭，符合匿名化原则 |

EduPACS 侧（`runtime/config/lti.json` / `xapi.json`）：

- `iss` 与 `authorization_endpoint` = Moodle 的 `$CFG->wwwroot`（浏览器可见地址），用于 ID Token 校验和 OIDC 跳转；
- `jwks_url` / `token_endpoint` 用 compose 服务名 `http://moodle:8080/...`（Bitnami 容器内监听 8080），由 EduPACS 后端直接访问；
- `EDUPACS_LTI_SERVER_HOST_MAP` 把 AGS lineitem 从 `localhost:8080` 映射到 `http://moodle:8080`；同时用 `EDUPACS_LTI_SERVER_HOST_HEADER` 带公开 Host 头，绕过 Moodle 4.5 的 wwwroot 主机校验重定向；
- AGS：Moodle 只下发 `lineitems` 集合，EduPACS 首条成绩自动创建 lineitem 并缓存；提交时先尝试标准 v2，失败自动回退 Moodle 4.x 的 `/lineitem/scores` + v1 类型；
- 引导容器以 `daemon:1`（Moodle Web 同用户）运行，避免 `moodledata` 下出现 root 属主缓存目录导致 Web 500；
- 引导脚本放行 Moodle cURL 安全策略对 `app:8000` 的封锁（仅 compose 隔离网络内生效）并刷新缓存；
- xAPI 指向内置 LRS，发送匿名 actor，不含患者 PHI。

## 四、查看 xAPI 语句

```bash
docker compose -f docker-compose.moodle.yml exec -T lrs \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8001/statements?limit=5').read().decode())"
```

语句持久化在 `lrs-data` 卷（`docker/moodle/runtime/lrs` 未直接挂载，数据由 compose 卷管理）。如需接入正式 LRS，把 `runtime/config/xapi.json` 的 `endpoint` / `auth_header` 改为正式地址后 `docker compose restart app`。

## 五、常用命令

```bash
# 查看状态
docker compose -f docker-compose.moodle.yml ps
python scripts/configure.py --status

# 查看日志
docker compose -f docker-compose.moodle.yml logs -f moodle
docker compose -f docker-compose.moodle.yml logs -f app

# 重新执行 Moodle 侧引导（幂等，不会重复建课）
docker compose -f docker-compose.moodle.yml --profile tools run --rm bootstrap
python scripts/configure.py --finalize

# 停止（数据保留）
docker compose -f docker-compose.moodle.yml down
```

## 六、排错

| 现象 | 处理 |
| --- | --- |
| `Docker 引擎未启动` | 先启动 Docker Desktop，确认右下角鲸鱼图标正常 |
| 拉 `bitnami/moodle` 失败 | 默认已使用可拉的 `docker.io/bitnamilegacy/moodle:4.5` + `docker.io/library/mariadb:11.2`；如镜像源受限，把 `.env` 中 `MOODLE_IMAGE` / `MARIADB_IMAGE` 改为本地已缓存镜像后重跑 |
| Moodle 首次安装很慢 | 引导脚本最多等 9 分钟；可 `logs -f moodle` 看进度，之后重跑 bootstrap |
| LTI 启动报 `iss 不匹配` | 确认 `MOODLE_PUBLIC_HOST` 与浏览器地址栏完全一致（含端口），改后重跑一键启动 |
| LTI 启动报 `kid 找不到` | `docker compose restart app` 让工具重新生成 JWKS，再检查 `http://localhost:8000/lti/jwks.json` |
| Moodle 页面报「试图创建一个目录时检测到权限无效」 | 说明 `moodledata` 中有 root 属主文件（多因手工用 root 跑 CLI）。在 moodle 容器执行 `find /bitnami/moodledata -user root -exec chown daemon:daemon {} +`，之后不要再以 root 跑 bootstrap |
| 成绩未回传 | 查看 `app/data/integrations.log` 中 `AGS 回传` / `AGS token 请求失败` 行；脚本已自动处理 lineitem 创建与 v1/v2 兼容 |
| ID Token 校验失败，报 `certs.php` 连接被拒 | 确认 `jwks_url` 为 `http://moodle:8080/mod/lti/certs.php` 且 `EDUPACS_LTI_SERVER_HOST_HEADER` 已注入 |
| 阅片器黑屏 | 见主部署手册 OHIF 兼容章节；本栈已保留 JS 降级与 serviceWorker 守卫 |
| 端口被占用 | 改 `.env` 的 `MOODLE_PORT` / `EDUPACS_PORT` / `DICOM_PORT` 以及对应 PUBLIC_HOST |

## 七、安全提示

- `.env` 含数据库、Moodle 管理员与 LRS 凭据，请勿提交到公开仓库；
- 本栈面向内网教学，默认 HTTP；公网部署请在前面加 TLS 反向代理，并把 `MOODLE_PUBLIC_HOST` / `EDUPACS_PUBLIC_HOST` 改成 HTTPS 地址；
- LRS 为教学演示用途，正式审计场景请替换为合规 LRS。
