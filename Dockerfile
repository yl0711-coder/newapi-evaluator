FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TEST_BIND_HOST=0.0.0.0 TEST_PORT=8000
WORKDIR /app
RUN groupadd --system evaluator && useradd --system --gid evaluator --home /app evaluator
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
COPY run.py local_runner.py manage_accounts.py ./
RUN mkdir -p /data /backups && chown -R evaluator:evaluator /app /data /backups
USER evaluator
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)); raise SystemExit(0 if data['status'] in ('ok','degraded') else 1)"
CMD ["python", "run.py"]
