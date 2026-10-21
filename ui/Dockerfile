# ML Operations Console.
#
# An API client only: no MLflow, MinIO, or object-store credentials are present,
# and no ML library is installed, so this image cannot load a model artifact
# even by accident. It reaches models through the forecast API.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    FORECAST_API_URL=http://api:8000

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "inference_app.py", "--server.address", "0.0.0.0", "--server.port", "8501", "--server.headless", "true"]
