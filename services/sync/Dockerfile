# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

WORKDIR /app
COPY requirements-sync.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY services /app/services

ENV PYTHONPATH=/app
ENV PORT=8080

EXPOSE 8080
# Cloud Run 규약은 주입된 $PORT 를 듣는 것이다. 8080 을 박아두면 런타임이 다른
# 포트를 주는 순간 컨테이너가 트래픽을 못 받는다(기본이 8080 이라 여태 안 터졌을 뿐).
CMD ["sh", "-c", "exec uvicorn services.sync.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
