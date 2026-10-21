# Serving layer: forecast, model discovery, and the Scenario Planner.
#
# Python 3.12 to match the training image. The model artifacts are joblib
# pickles of fitted estimators, and keeping the interpreter and the ML library
# versions aligned with the process that wrote them is what makes them loadable
# at all.
FROM python:3.12-slim

# PYTHONPATH includes `include/` because the ensemble artifact is a pickle of
# `ml_models.ensemble_model.EnsembleModel` stored by reference: that module must
# be importable for the model to unpickle. (Comments are kept out of the ENV
# continuation itself, where a `#` is not reliably treated as a comment.)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/include \
    MODEL_CACHE_DIR=/app/.cache/models

WORKDIR /app

# libgomp is the OpenMP runtime both XGBoost and LightGBM link against; without
# it `import xgboost` fails at load time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so a code change does not reinstall them.
#
# A BuildKit cache mount rather than `PIP_NO_CACHE_DIR`: this layer pulls
# ~250 MB of wheels (pandas, scipy, scikit-learn, mlflow), and a single dropped
# connection used to fail the whole build and re-download everything. With the
# cache, a retry resumes from what already arrived. The cache lives in the
# builder, not in the image, so the image size is unaffected.
COPY api/requirements.txt ./api/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 10 --timeout 120 -r api/requirements.txt

COPY api ./api
COPY include ./include

# The artifact cache is a real directory owned by the app user so a volume can
# be mounted over it; without the chown, a mounted volume would be root-owned
# and the unprivileged process could not write to it.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p "$MODEL_CACHE_DIR" \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Shallow probe: the deep one queries MLflow and MinIO, and a brief upstream
# outage must not get this container restarted.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://localhost:8000/health?deep=false" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
