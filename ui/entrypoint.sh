#!/bin/bash
set -euo pipefail

# The console is an API client: it no longer loads models, so the ML stack
# (mlflow, scikit-learn, xgboost, lightgbm, boto3, dill) is deliberately not
# installed here. Removing it cuts several minutes off a cold start and removes
# the only code path that could read a model artifact directly.
pip install --no-cache-dir \
  streamlit==1.32.2 \
  pandas==2.2.0 \
  numpy==1.26.4 \
  plotly==5.19.0 \
  requests==2.32.3 \
  python-dateutil==2.8.2

streamlit run inference_app.py --server.address 0.0.0.0 --server.port 8501
