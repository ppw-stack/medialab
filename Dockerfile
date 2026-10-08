# ---- Frontend builder ----
FROM node:22-slim AS web-builder

WORKDIR /build

# 安装依赖（缓存层）
COPY web/package.json web/package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

# 拷贝源码并构建
COPY web/ ./
RUN npm run build


# ---- Python builder ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN pip install --no-cache-dir uv

WORKDIR /build

# 依赖先行（最大化层缓存命中，FR-8.1）
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project --no-editable

# 拷贝 README + app，build（hatchling 需要 readme）
COPY README.md ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-editable \
    && /opt/venv/bin/python -c "import app, app.main; print('import ok:', app.__version__)"


# ---- Runtime ----
FROM python:3.12-slim AS runtime

# tini：PID 1 与信号处理（优雅停机、FR-8.2）
# curl：HEALTHCHECK；ca-certificates：HTTPS 访问 PT 站
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    TZ=Asia/Shanghai

# 非 root 运行（§9.2 部署安全建议）
RUN groupadd --system --gid 1000 seedhelper && \
    useradd  --system --uid 1000 --gid seedhelper --home-dir /srv/seedhelper --shell /sbin/nologin seedhelper

WORKDIR /srv/seedhelper

# Python venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# 前端构建产物（不安装 npm，运行时只读静态文件）
COPY --from=web-builder /build/dist /app/web/dist

# 数据与日志目录（持久化卷，FR-8.1）
RUN mkdir -p /app/data /app/logs /app/config && \
    chown -R seedhelper:seedhelper /app /srv/seedhelper

ENV DATA_DIR=/app/data \
    LOG_DIR=/app/logs

USER seedhelper

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
