# 多阶段构建：前端 → Python 依赖 → 运行时
#
# 网络受限时用本地镜像覆盖：
#   docker build --build-arg PY_IMAGE=python:3.13-bookworm
ARG PY_IMAGE=python:3.13-slim
ARG NODE_IMAGE=node:22-slim

# ---- Frontend builder ----
FROM ${NODE_IMAGE} AS web-builder

WORKDIR /build

# 依赖先行（最大化层缓存命中）
COPY web/package.json web/package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

# 拷贝源码并构建
COPY web/ ./
RUN npm run build

# ---- Python builder ----
FROM ${PY_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN pip install --no-cache-dir uv

WORKDIR /build

# 依赖先行（最大化层缓存命中）
# --extra p115：115 驱动依赖（p115client / qrcode 等）。
# --extra guangya：光鸭盘驱动依赖（guangyaclient）。
# **两个都必须装**——否则镜像里没有对应驱动，扫码登录用不了。
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --extra p115 --extra guangya --no-install-project --no-editable

# 拷贝 README + app，build（hatchling 需要 readme）
COPY README.md ./
COPY app ./app
# --reinstall：强制重装 app，绕开 uv 缓存的旧 wheel
# （uv sync 只哈希 pyproject.toml/uv.lock，不感知 app/ 源码变化）
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --extra p115 --extra guangya --no-editable --reinstall \
    && /opt/venv/bin/python -c "import app, app.main; print('import ok:', app.__version__)" \
    && /opt/venv/bin/python -c "from p115client import P115Client, tool; print('p115client ok')" \
    && /opt/venv/bin/python -c "from guangyaclient import GuangyaClient; print('guangyaclient ok')"

# ---- Runtime ----
FROM ${PY_IMAGE} AS runtime

# tini：PID 1 与信号处理（优雅停机）
# curl：docker-compose 的 healthcheck 用；ca-certificates：HTTPS 访问 115 / PT 站
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

# 非 root 运行
RUN groupadd --system --gid 1000 medialab && \
    useradd  --system --uid 1000 --gid medialab --home-dir /srv/medialab --shell /sbin/nologin medialab

WORKDIR /srv/medialab

# Python venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# 前端构建产物（不安装 npm，运行时只读静态文件）
COPY --from=web-builder /build/dist /app/web/dist

# 数据与日志目录（持久化卷）
RUN mkdir -p /app/data /app/logs /app/config && \
    chown -R medialab:medialab /app /srv/medialab

ENV DATA_DIR=/app/data \
    LOG_DIR=/app/logs

USER medialab

EXPOSE 8080

# healthcheck 在 docker-compose*.yml 中按部署级定义（不同环境可调）
# 此处不重复维护，避免两处参数不一致

ENTRYPOINT ["/usr/bin/tini", "--"]
# --workers 1：SQLite 单写者 + APScheduler 内存态，多进程会出问题
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
