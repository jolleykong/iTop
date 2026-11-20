FROM python:3.12-slim-bookworm

WORKDIR /app

# 仅标准库（urllib），零 pip 依赖，构建离线可重复
COPY sync.py entrypoint.sh /app/

RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
