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
CMD ["uvicorn", "services.sync.main:app", "--host", "0.0.0.0", "--port", "8080"]
