FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home workbench
COPY --chown=workbench:workbench . .
RUN mkdir -p /app/data && chown workbench:workbench /app/data && chmod 700 /app/data
USER workbench
EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=6s --retries=5 --start-period=15s \
    CMD ["python", "scripts/container_healthcheck.py"]
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8090"]
