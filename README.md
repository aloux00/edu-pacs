# EduPACS 教学病例库系统

基于 **Orthanc 1.13.0（DICOM 存储 + DICOMweb）+ OHIF Viewer 3.13 + FastAPI/SQLite** 的一体化医学影像教学平台，面向约 100 名师生的院内网部署场景。

## 功能特性

- **影像教学**：DICOM 上传（.dcm/zip）、OHIF 阅片器内嵌、窗宽窗位/测量/标注
- **图片病例导入**：论文/网络收集的 JPG/PNG 截图自动合成 DICOM 教学序列（模态 OT）
- **上传即脱敏**：患者身份替换为教学编号、PHI 标签清洗、私有标签删除、Study/Series/SOP UID 重写；原始信息仅存教师可见溯源表
- **匿名化临床信息**：上传/导入/编辑病例时可填写年龄、性别、主诉、病史与其他临床资料；仅存教学库、不写回 DICOM，支持列表徽标与全文检索
- **远程 PACS 对接**：DICOM 节点（Modalities）与 Orthanc 节点（Peers）维护、C-ECHO 测试、C-FIND 远程查询、C-MOVE 拉取并自动脱敏入库；设备直推数据（4242）一键导入
- **字典管理**：系统/模态/难度/疾病名称选项维护，支持 xlsx 模板导入与导出
- **教学闭环**：盲读模式（作答后解锁要点/诊断/答案）、问答与教师批阅、学习打卡笔记、学情统计
- **图形启动器**：原生 C 启动器（146KB 静态 exe，内嵌多尺寸应用图标，赛博朋克 HUD 界面，状态灯/一键启停/安全停止/CLI 模式 `--status --start --stop`），PowerShell 版作为回退保留。启动器为**系统托盘模式**：任务栏中间无窗口按钮，只在右下角托盘显示图标；点窗口 X 或最小化只是收进托盘（服务照常运行），单击托盘图标显示/隐藏，右键托盘菜单可「显示主窗口 / 一键启动 / 安全停止 / 退出启动器」；重复双击会自动唤起已运行的窗口（单实例）。
- **跨平台双轨部署**：① Windows 离线安装包（zip + `scripts\install.ps1` 一键安装器，防火墙/快捷方式/开机自启可选）；② Linux 两路线——Ubuntu 原生 apt + systemd（手册 4.4）或 **Docker 离线镜像包**（手册 4.5，官方 orthancteam 镜像 + 自建 app 镜像，内网无外网可部署，安全边界与 Windows 版对齐）
- **内置备份恢复**：AES-256-GCM 加密归档（.edubak）同机/异机多目的地，网页「备份恢复」页 + CLI `tools\backup.py` 双入口；停机一致性窗口（自动优雅停启 Orthanc）或热备份 WAL 重放，写后自动校验；恢复向导干跑分析→沙箱→正式（回滚保障目录），冒烟 32 项含备份子流程
- **离线可用**：前端纯静态无外网依赖，单机部署，每日备份两个目录（Windows）或两个 volume（Docker）即可完整恢复；也可用内置加密备份系统（推荐）

## 架构

```
浏览器 ──► 教学系统 (FastAPI :8000，唯一入口)
              │  上传即脱敏 (pydicom)
              │  /ohif/* 、/dicom-web/* 反向代理（自动附带认证）
              ▼
          Orthanc :8042（仅本机，HTTP 认证）/ 4242  ──► OHIF Viewer (插件内嵌)
              │
              └── DICOM 节点 ◄──► 院内 PACS / 影像设备 / 远程教学库
```

## 目录结构

```
├── EduPACS启动器.bat        # 双击启动图形启动器
├── app/
│   ├── backend/main.py      # FastAPI 后端（脱敏引擎/教学业务/Orthanc 代理）
│   └── static/              # 前端 SPA（原生 JS，无构建步骤）
├── orthanc/
│   ├── Orthanc.exe          # [需自行获取，见下文]
│   ├── orthanc.json         # Orthanc 配置（含动态维护的远程节点）
│   └── plugins/             # [需自行获取] DICOMWeb / OHIF / Explorer2 / ServeFolders
├── scripts/                 # install.ps1 一键安装器 + 图形启动器与启停脚本（UTF-8 BOM）
├── docker/                  # Docker 双轨方案：Dockerfile / compose / orthanc 配置 / 离线镜像包构建与部署脚本
├── tools/                   # 演示 DICOM 生成器与样例导入
├── docs/                    # 部署与使用手册（md + html）
└── requirements.txt
```

## 快速部署（Windows 内网服务器）

1. **获取 Orthanc 运行时**（本仓库不含二进制，因 GitHub 单文件限制）：
   - 官方安装包：https://orthanc.uclouvain.be/downloads/windows-64/orthanc-installer/（如 `OrthancInstaller-Win64-*.exe`）
   - 用 [innoextract](https://constexpr.org/innoextract/) 解包，或安装后从安装目录复制 `Orthanc.exe` 与 `plugins/*.dll` 到本仓库 `orthanc/` 目录
   - 亦可使用 Docker 镜像 `orthancteam/orthanc`（Linux 部署）
2. 安装 Python 3.12+（64 位）并安装依赖。部署包已内置 `wheels\`，可**完全离线**安装：
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
   ```
   手工等价命令：
   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install --no-index --find-links wheels -r requirements.lock.txt
   ```
3. 双击 `EduPACS启动器.bat` → 点「一键启动」，浏览器打开 `http://<服务器IP>:8000`
4. 默认管理员 `admin / admin123`（**上线后立即修改**）；演示数据可用 `tools/import_cases.py` 导入

## 端口与安全

| 端口 | 用途 | 开放范围 |
|---|---|---|
| 8000 | 教学系统 + OHIF 阅片（师生，唯一入口） | 院内网 |
| 8042 | Orthanc 管理界面/DICOMweb（HTTP 已启用认证） | 仅本机，无需对外开放 |
| 4242 | DICOM 设备直推 | 按需（推送数据入库前自动脱敏） |

**Orthanc HTTP 认证**：8042 仅保留服务账号（`orthanc/orthanc.json` 的 `RegisteredUsers`，部署后请修改密码）；师生与 OHIF 请求由教学系统反代转发，无需感知认证；DICOM 4242 不受影响。若仓库公开复用，**部署时务必修改服务账号密码与默认账号密码**。

**反代只读**：8000 上的 `/ohif/*`、`/dicom-web/*` 仅放行 GET/HEAD（QIDO/WADO），写操作一律 405；影像写入只能走教学系统上传通道（服务端执行，自动脱敏）。

**安全停止**：`scripts/stop_all.ps1` 只结束命令行可确认属于本项目的进程，不会误杀同机其它项目的 Orthanc/python 服务。`app/data/secret.key` 不随部署包分发，首次启动自动生成。

**备份**：管理员「备份恢复」页支持 AES-256-GCM 加密备份 / 校验 / 沙箱恢复，目的地可配同机与异机（UNC/NFS，每次备份写一个目的地）；命令行见 `tools\backup.py`，计划任务示例见手册 10.1 节。传统手工备份 `orthanc\storage\` 与 `app\data\` 仍有效。

## 文档

完整部署/迁移/脱敏设计/字典模板/远程节点/运维 FAQ 见 [`docs/部署与使用手册.md`](docs/部署与使用手册.md)。

---
AI生成

> AI生成
