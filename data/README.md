# Versioned training data

Configure the source in `include/config/ml_config.yaml` under `data:`.

## Default: Kaggle M5 (via kagglehub)

Training uses **`kagglehub.competition_download('m5-forecasting-accuracy')`** — you do **not** paste URLs in config.

### One-time Kaggle authentication

kagglehub needs access to your Kaggle account. Use **one** of:

1. **Environment variables** (recommended in Docker / Airflow):

   ```bash
   export KAGGLE_USERNAME=your_username
   export KAGGLE_KEY=your_api_key
   ```

2. **`~/.kaggle/kaggle.json`** on the host, mounted into the Airflow container if you run via Astronomer.

Create an API token from [Kaggle Account → API](https://www.kaggle.com/settings).

### Config (`data.source: kaggle_m5`)

| Field | Purpose |
|-------|---------|
| `kaggle_competition` | Slug passed to kagglehub (`m5-forecasting-accuracy`) |
| `kaggle_download` | `true` = download via kagglehub when ingesting |
| `kaggle_competition_path` | If set to an existing folder with CSVs, skip download |
| `train_start_date` / `train_end_date` | Filter rows after melt (default M5 history window) |
| `regenerate` | `false` = reuse ingested parquet + manifest; `true` = re-ingest |

First DAG run: set `regenerate: true` once (download + ingest can take 10–30+ minutes). Later runs: `regenerate: false`.

Ingested parquet lives under:

`/usr/local/airflow/data/raw/kaggle_m5/v1.0.0/sales/...`

Kaggle’s raw CSV cache is managed by kagglehub (typically under `~/.cache/kagglehub/`).

## Other sources

| `data.source` | Use case |
|---------------|----------|
| `synthetic` | CI, offline demos (`random_seed: 42`) |
| `local_csv` | Your own `sales.csv` under `raw_path` |

## Lineage

`dataset_manifest.json` and MLflow params (`data.source`, `data.hash`, `kaggle_competition`, etc.) record what was used for each training run.
