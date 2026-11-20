FROM python:3.12-slim-bookworm

WORKDIR /app

# mysql 客户端用于写 iTop 暂存表（官方采集器模式）；同步逻辑仅标准库
RUN apt-get update && apt-get install -y --no-install-recommends default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

COPY sync.py entrypoint.sh /app/

RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
