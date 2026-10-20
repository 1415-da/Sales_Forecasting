# Serving layer for the Scenario Planner.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so a code change does not reinstall them.
COPY api/requirements.txt ./api/requirements.txt
RUN pip install -r api/requirements.txt

# `include/` is copied because the API reads the training pipeline's dataset
# manifests and lineage helpers.
COPY api ./api
COPY include ./include

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:8000/health?deep=false" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
