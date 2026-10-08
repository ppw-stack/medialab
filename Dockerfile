# ---- Build stage ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv

WORKDIR /build

# 依赖先行：最大化层缓存命中（FR-8.1）
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project --no-editable

COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-editable


# ---- Runtime stage ----
FROM python:3.12-slim AS runtime

# tini：PID 1 与信号处理（优雅停机、FR-8.2）
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    TZ=Asia/Shanghai

# 非 root 运行（§9.2 部署安全建议）
RUN groupadd --system --gid 1000 seedhelper && \
    useradd  --system --uid 1000 --gid seedhelper --home /app --shell /sbin/nologin seedhelper

WORKDIR /app

# 只复制已安装的 venv 与源码
COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/app /app/app

# 数据与日志目录（持久化卷，FR-8.1）
RUN mkdir -p /app/data /app/logs /app/config && \
    chown -R seedhelper:seedhelper /app

ENV PATH="/app/.venv/bin:${PATH}" \
    DATA_DIR=/app/data \
    LOG_DIR=/app/logs

USER seedhelper

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]