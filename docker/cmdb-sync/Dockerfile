FROM python:3.12-slim-bookworm

WORKDIR /app

# mysql 客户端用于写 iTop 暂存表（官方采集器模式）；同步逻辑仅标准库
RUN apt-get update && apt-get install -y --no-install-recommends default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

COPY sync.py entrypoint.sh /app/
# vc_sn_map.json 作为镜像内默认副本；compose 里另有一条 bind mount 覆盖它，
# 便于不重建镜像就热更新宿主 SN 映射表（vc_sn_pull.py 直读 vCenter 产出）
COPY vc_sn_map.json /app/vc_sn_map.json

RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
