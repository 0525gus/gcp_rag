# syntax=docker/dockerfile:1
# HWP/HWPX → MD (rhwp-python) 파서
FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-parser.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import rhwp; print('rhwp', getattr(rhwp, 'version', lambda: '?')())"

COPY shared /app/shared
COPY services /app/services

ENV PYTHONPATH=/app
ENV PORT=8080
ENV QG_MODE=log

EXPOSE 8080
CMD ["uvicorn", "services.parser.main:app", "--host", "0.0.0.0", "--port", "8080"]
