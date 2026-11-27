# Moodle + EduPACS + OHIF + LTI 1.3 + xAPI 集成指南

> 目标：学生在 Moodle 课程里点一次「EduPACS 病例」即可免密进入 EduPACS，病例可直接深链到 OHIF 阅片器；学习行为以 xAPI 语句送到 LRS，教师批阅分数可按 LTI Advantage AGS 回传 Moodle 成绩簿。
>
> 适用版本：Moodle 4.x（内置 `mod_lti` 支持 LTI 1.3 / LTI Advantage）；EduPACS 包19及以后。
>
> 默认状态：**集成关闭**。不配置时系统行为与包18完全一致。

---

## 1. 架构与数据流

```
Moodle（Platform）
   │ ① OIDC 第三方登录 /lti/login
   ▼
EduPACS（Tool）
   │ ② 浏览器 POST ID Token → /lti/callback
   │ ③ 验签 + 角色映射 + 自动建号 + LTI 会话 Cookie
   ▼
EduPACS SPA → /#/lti/landing → 病例详情 → OHIF（同源 /ohif 代理）
   │
   ├─ xAPI 语句（浏览/阅片/作答/批阅）→ LRS
   └─ AGS 成绩回传（可选）→ Moodle 成绩簿
```

关键点：

- EduPACS 是 **LTI Tool**，Moodle 是 **Platform**，不需要给 Moodle 装额外插件。
- 浏览器只需访问 EduPACS 的 `public_url`；OHIF 继续走 EduPACS 的 `/ohif`、`/dicom-web` 同源反向代理，患者影像不直接暴露 8042。
- xAPI actor 默认使用 LTI `sub` 的哈希，**不向 LRS 发送姓名、住院号、PatientAge 等身份或 PHI 信息**。
- LTI 工具私钥自动保存在 `app/data/lti_private.pem`，公钥通过 `/lti/jwks.json` 发布；请纳入备份，切勿提交到 Git。

---

## 2. 在 EduPACS 侧准备

### 2.1 确认 public_url

`config/lti.json` 的 `public_url` 必须是**浏览器能打开**的地址，且必须与 Moodle 中登记的 Redirection URI 同源，例如：

- 本机测试：`http://127.0.0.1:8000`
- 内网服务器：`http://172.16.21.132:8000`
- 推荐生产：`https://edu-pacs.your-school.edu.cn`

同时，**Moodle 服务器自身**也必须能访问这个地址的 `/lti/jwks.json`（平台要拉取工具公钥）。

> 若 Moodle 强制 HTTPS，请给 EduPACS 配 HTTPS 反代；纯 HTTP 内网需关闭 Moodle 工具配置里的 Force SSL。

### 2.2 填写 `config/lti.json`

首次交付文件是关闭状态。先在 Moodle 建好工具（下一节），拿到 **Platform ID / Client ID / Deployment ID** 后再回填：

```json
{
  "version": 1,
  "enabled": true,
  "public_url": "http://172.16.21.132:8000",
  "session_ttl_hours": 12,
  "cookie_secure": false,
  "auto_provision": true,
  "default_role": "student",
  "role_map": {
    "Administrator": "admin",
    "Instructor": "teacher",
    "TeachingAssistant": "teacher",
    "Learner": "student"
  },
  "grade_passback": { "enabled": true, "comment_on_grade": true },
  "registrations": [
    {
      "id": "moodle-main",
      "label": "Moodle 主站",
      "iss": "http://moodle.your-school.edu.cn",
      "client_id": "从 Moodle 配置详情复制",
      "deployment_id": "从 Moodle 配置详情复制",
      "discovery_url": "",
      "authorization_endpoint": "",
      "jwks_url": "",
      "token_endpoint": "",
      "target_link_uri": "http://172.16.21.132:8000/lti/callback"
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `iss` | 必须等于 Moodle 的 Platform ID（站点根 URL），末尾斜杠可省略 |
| `client_id` / `deployment_id` | 从 Moodle「View configuration details」复制 |
| `target_link_uri` | 必须与 Moodle 登记的 Redirection URI 完全一致 |
| `authorization_endpoint` / `jwks_url` / `token_endpoint` | 可留空，程序自动尝试 MOIDC discovery；自动发现失败时再显式填 |
| `auto_provision` | 首次从 Moodle 进入时自动建 EduPACS 账号，账号名为 `lti_<哈希>` |
| `role_map` | Moodle 角色 → EduPACS 角色；教师/助教映射为 teacher，学生映射为 student |
| `cookie_secure` | HTTPS 部署设为 `true`；HTTP 内网保持 `false` |
| `grade_passback.enabled` | 是否把教师批阅分数回传 Moodle 成绩簿（LTI AGS） |

修改配置后需**重启 EduPACS 后端服务**：

- Windows 服务：`net stop EduPACS-App && net start EduPACS-App`
- 启动器：托盘退出后重新启动
- Docker：`docker compose restart app`

### 2.3 可选：配置 xAPI / LRS

`config/xapi.json`：

```json
{
  "version": 1,
  "enabled": true,
  "endpoint": "https://lrs.your-school.edu.cn/xapi/statements",
  "auth_header": "",
  "platform_iri": "http://172.16.21.132:8000",
  "platform_name": "EduPACS 教学病例库",
  "actor_mode": "anonymous",
  "flush_interval_seconds": 5,
  "max_attempts": 5
}
```

- 强烈建议把 `auth_header` 放到环境变量，避免明文写进配置文件：
  - Windows PowerShell：`$env:EDUPACS_XAPI_AUTH_HEADER = "Basic <base64(user:pass)>"`
  - Linux：`export EDUPACS_XAPI_AUTH_HEADER='Basic <base64(user:pass)>'`
- 也可覆盖 `EDUPACS_XAPI_ENDPOINT`。
- `actor_mode` 推荐 `anonymous`（默认）；仅在合规允许时可改为 `lti` 使用 Moodle 姓名/邮箱。
- xAPI 队列落 SQLite `xapi_queue` 表，发送失败会重试 5 次；可用 `GET /api/xapi/queue` 查看状态。
- 语句类型：

| 动作 | xAPI verb |
| --- | --- |
| 打开病例详情 | experienced |
| 打开 OHIF 阅片器 | interacted |
| 提交题目答案 | answered |
| 教师批阅完成 | completed（含 result.score） |

---

## 3. 在 Moodle 侧注册 LTI 1.3 工具

以 Moodle 管理员登录：

1. **站点管理 → 插件 → 活动模块 → 外部工具 → 管理工具**
2. 点击 **Configure a tool manually**（手动配置工具）
3. 填写：

| Moodle 字段 | 填写内容 |
| --- | --- |
| Tool name | EduPACS 教学病例库 |
| Tool URL | `http(s)://<EduPACS>/lti/callback` |
| LTI version | LTI 1.3 |
| Public key type | Public keyset URL |
| Public keyset URL | `http(s)://<EduPACS>/lti/jwks.json` |
| Initiate login URL | `http(s)://<EduPACS>/lti/login` |
| Redirection URI(s) | `http(s)://<EduPACS>/lti/callback` |
| Content selection URL | `http(s)://<EduPACS>/lti/callback`（同一入口会识别 Deep Linking 消息） |
| Supports Deep Linking | 勾选 |
| Tool configuration usage | Show as pre-configured tool（推荐） |
| Default launch container | Embed（或 New window，取决于网络/浏览器策略） |

4. 隐私与成绩：
   - Share launcher's name with tool：按需（默认匿名即可）
   - Share launcher's email with tool：建议 Never
   - Accept grades from the tool：**Always**（启用 AGS 成绩回传时必须）
5. 保存后，在工具列表点击 **View configuration details**，复制：
   - Platform ID（即 Issuer，填到 EduPACS 的 `iss`）
   - Client ID（填 `client_id`）
   - Deployment ID（填 `deployment_id`）
6. 回到 EduPACS 更新 `config/lti.json` 并重启后端。

> 顺序很重要：Moodle 先生成 Client/Deployment ID，EduPACS 再填配置；反过来无法完成注册。

---

## 4. 课程内使用

### 4.1 教师添加病例（Deep Linking）

1. 进入课程 → **添加活动或资源 → 外部工具 → EduPACS 教学病例库**
2. 若弹出「从 EduPACS 选择教学病例」，勾选一个或多个已发布病例，点击「添加到课程」
3. Moodle 会生成对应的 LTI 活动；学生点击后直接进入指定病例

未使用 Deep Linking 时，LTI 活动默认进入 EduPACS 病例库首页。

### 4.2 学生上课

1. 在 Moodle 课程中点击 EduPACS 活动
2. 自动免密登录 EduPACS，若活动绑定了病例则直接进入病例详情页
3. 点「打开阅片器」加载 OHIF；作答、笔记、打卡流程与本地登录完全一致
4. 页面右上角出现「返回课程」链接，可回到 Moodle

### 4.3 教师批阅与成绩回传

1. 教师在 EduPACS 中批阅学生答案
2. 若 `grade_passback.enabled=true` 且 Moodle 允许接收成绩，分数通过 LTI AGS 写回 Moodle 成绩簿
3. 同时生成 xAPI `completed` 语句（含分数）送 LRS

---

## 5. 验证与排错

### 5.1 上线前自测

在部署机执行（不会影响正在运行的 8000 服务，使用 8123/8129 独立端口）：

```bash
.venv\Scripts\python tools\test_lti_xapi.py
```

当前应全部通过，覆盖：OIDC 登录、ID Token 验签、角色映射、LTI 会话、xAPI 入队/送达、Deep Linking JWT、AGS 回传、本地账号登录不受影响。

### 5.2 常见错误

| 现象 | 排查 |
| --- | --- |
| Moodle 报 `Invalid tool configuration` / 无法拉公钥 | Moodle 服务器能否访问 `<EduPACS>/lti/jwks.json`；`public_url` 是否为浏览器可达地址 |
| 回调 400 `LTI 登录状态已过期` | 从 Moodle 重新点击活动；浏览器禁用了第三方 Cookie 时检查 SameSite 设置 |
| 回调 401 `ID Token 校验失败` | `iss` 必须与 Moodle Platform ID 完全一致；Client ID / Deployment ID 是否复制正确；平台时间是否同步 |
| 进入后是本地登录页 | 浏览器是否拦截 Cookie；`/api/lti/session` 是否返回 token；查看 `app/data/integrations.log` |
| 学生看不到病人信息 | LTI 学生账号由 Moodle 角色映射而来，只有教师能看溯源；匿名化临床信息仍按病例权限显示 |
| xAPI 队列一直 failed | `endpoint` / `Authorization` 是否正确；LRS 是否接受 xAPI 1.0.3；查看 `/api/xapi/queue` 的 `last_error` |
| 成绩未回传 Moodle | Moodle 工具是否勾选 Accept grades；EduPACS `grade_passback.enabled` 是否为 true；LTI 启动是否含 AGS claim；日志 `integrations.log` |

### 5.3 日志位置

- `app/data/integrations.log`：LTI 校验、自动建号、xAPI 发送、AGS 回传日志
- `app/data/error.log`：未捕获异常堆栈
- `GET /api/xapi/queue`：队列状态（不返回学生答案正文）

---

## 6. 安全与隐私清单

- [ ] `config/lti.json` / `config/xapi.json` 未提交真实密钥；LRS 认证走环境变量
- [ ] `app/data/lti_private.pem` 已纳入备份且权限受限
- [ ] `public_url` 使用 HTTPS；HTTP 仅用于可信内网
- [ ] Moodle 侧「Share email」设为 Never；EduPACS `actor_mode=anonymous`
- [ ] 病例匿名化信息不包含姓名、住院号、身份证号、具体日期（与包18规则一致）
- [ ] 教师账号仅由 Moodle Instructor/Administrator 角色映射产生

---

## 7. 与现有功能的关系

- 不影响本地账号登录、强制改密、上传脱敏、备份恢复、OHIF 阅片、Windows 服务化。
- 匿名化临床信息（年龄/性别/主诉/病史/其他）仍存储在 EduPACS SQLite，不写回 DICOM，也不发送给 LRS。
- 未启用 LTI 时，`/lti/*` 返回 503，前端不显示「返回课程」；未启用 xAPI 时，所有埋点函数直接无操作。
