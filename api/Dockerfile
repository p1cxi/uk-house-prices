FROM python:3.12-slim

WORKDIR /

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY ingest/requirements.txt /tmp/ingest-requirements.txt
COPY api/requirements.txt /tmp/api-requirements.txt
RUN pip install --no-cache-dir -r /tmp/ingest-requirements.txt -r /tmp/api-requirements.txt

# The api container only needs the pipelines package (imported by api/ingest.py
# via `from pipelines.ppd import ...`), not the ingest container's CLI shims.
COPY ingest/pipelines/ /app/pipelines/
COPY api/ /api/

ENV PYTHONPATH=/app

RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app /api
USER app

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/"]
