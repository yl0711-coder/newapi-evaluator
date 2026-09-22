FROM python:3.13-alpine@sha256:79e7a9b9ff1cbceff819f856fb374477792a5967759d94df266de7b7b4120e6f
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY hourly_channel_diagnostic.py channel_catalog.py manage.py control_server.py /app/
ENTRYPOINT ["python", "-B"]
CMD ["manage.py", "--help"]
