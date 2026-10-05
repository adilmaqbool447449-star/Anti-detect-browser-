FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY polybot ./polybot
COPY scripts ./scripts
COPY config.yaml .

RUN useradd --create-home --uid 10001 polybot && mkdir -p /app/data && chown -R polybot /app/data
USER polybot

ENV POLYBOT_DASHBOARD_HOST=0.0.0.0
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8080/healthz',timeout=4)); exit(0 if d['feed_age_s'] is not None and d['feed_age_s']<60 else 1)"

CMD ["python", "-m", "polybot", "--config", "/app/config.yaml"]
