# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

WORKDIR /app
COPY requirements-mcp.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY services /app/services

ENV PYTHONPATH=/app
ENV PORT=8080
ENV MCP_TRANSPORT=streamable-http

EXPOSE 8080
CMD ["python", "-m", "services.mcp_server.main"]
