# ---- Build stage ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN pip install --no-cache-dir uv

WORKDIR /build

# 依赖先行：最大化层缓存命中（FR-8.1）
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project

# 安装项目本体为**非可编辑**包：可编辑安装会写入 .pth 指向 /build，
# 而 /build 在 runtime 阶段不存在，导致 import app 失败。
COPY README.md ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-editable \
    && /opt/venv/bin/python -c "import app, app.main; print('import ok:', app.__version__)"


# ---- Runtime stage ----
FROM python:3.12-slim AS runtime

# tini：PID 1 与信号处理（优雅停机、FR-8.2）
# curl：HEALTHCHECK；ca-certificates：HTTPS 访问 PT 站（RSS/API）
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

# WORKDIR 刻意避开 /app：否则 cwd 的 /app 目录会被 Python 当作名为 `app` 的
# namespace package，遮蔽 site-packages 中真正安装的同名包。
WORKDIR /srv/seedhelper

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# 数据与日志目录（持久化卷，FR-8.1）
RUN mkdir -p /app/data /app/logs /app/config && \
    chown -R seedhelper:seedhelper /app /srv/seedhelper

ENV DATA_DIR=/app/data \
    LOG_DIR=/app/logs \
    PYTHONPATH=

USER seedhelper

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]