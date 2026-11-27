# EduPACS 教学系统应用层镜像（FastAPI + 前端静态文件）
#
# 构建上下文 = 项目根目录（不是 docker/ 目录）：
#   docker build -f docker/Dockerfile -t edupacs-app:1.0.0 .
#   （可加 --build-arg PIP_INDEX=... 指定 pip 源，默认清华镜像）
#
# 设计要点：
#   - 纯静态前端 + Python 后端打包成一个镜像，无外网依赖、运行时零下载
#   - app/data 用 volume 挂载持久化（SQLite + secret.key 首启自动生成）
#   - Orthanc 地址/凭据全部走环境变量（main.py 原生支持 ORTHANC_URL /
#     ORTHANC_AUTH_USER / ORTHANC_AUTH_PASS），应用代码零改动
#   - tools/smoke_test.py 与 tools/backup.py 随镜像分发：部署后容器内即可跑冒烟回归
#     与 AES-256-GCM 加密备份（Orthanc 索引/影像通过 orthanc-storage 共享卷读取）
#   - config/backup.json 提供同机/异机目的地模板，容器内可用 CLI 或网页端修改
FROM python:3.12-slim

ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /edu-pacs

# 依赖先行（利用层缓存）：锁定版本，保证与开发环境完全一致
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt --index-url ${PIP_INDEX}

# 应用代码（app/ = 后端 + 前端静态；冒烟测试工具 + 备份 CLI + 备份配置）
COPY app ./app
COPY tools/smoke_test.py ./tools/smoke_test.py
COPY tools/backup.py ./tools/backup.py
COPY config/backup.json ./config/backup.json
# LTI 1.3 / xAPI 默认配置（compose 会用 runtime/config 覆盖，便于一键集成写入 ID）
COPY config/lti.json ./config/lti.json
COPY config/xapi.json ./config/xapi.json

# 上传临时目录（容器内可写，无需持久化）；app/data 与 orthanc/storage 为 volume 挂载点
RUN mkdir -p .temp/uploads app/data orthanc/storage/config

EXPOSE 8000

# 就绪探针：/api/orthanc/health 会带认证探测 Orthanc /system，
# 因此通过 = app 与 orthanc 双端就绪且凭据正确
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/orthanc/health',timeout=8));exit(0 if d.get('ok') else 1)"

CMD ["python", "-m", "uvicorn", "app.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
