#!/bin/sh
# itop-sync 容器入口：直接进入同步循环
# （数据源供给由 compose 的 provision 服务负责：docker compose up provision）
set -u

echo "[entrypoint] waiting 20s for itop-web to be ready..."
sleep 20

echo "[entrypoint] entering sync loop (interval=${SYNC_INTERVAL_SECONDS:-14400}s)"
exec python3 /app/sync.py
