#!/bin/sh
# itop-sync 容器入口：先做一次性数据源供给，再进入同步循环
set -u

echo "[entrypoint] waiting 20s for itop-web to be ready..."
sleep 20

echo "[entrypoint] running provision (one-time Synchro Data Source setup)..."
python3 /app/provision.py

echo "[entrypoint] entering sync loop (interval=${SYNC_INTERVAL_SECONDS:-14400}s)"
exec python3 /app/sync.py
