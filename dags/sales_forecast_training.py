"""
Train sales forecasting models.

Two kinds of run share this DAG:

* **Scheduled / manual runs** (no conf) train on the configured provider (M5)
  and promote to Production, exactly as before.
* **Training-request runs** are started by the API for an uploaded dataset.
  Their conf names an immutable dataset version; every task loads that exact
  version from object storage, no synthetic or provider data is generated, the
  split is by calendar date, and the candidates are registered for review --
  never promoted. Promotion happens only when an ML engineer approves one.
"""

from datetime import datetime, timedelta
from airflow.decorators import dag, task
from airflow.sdk import get_current_context
from airflow.providers.standard.operators.bash import BashOperator
import pandas as pd
import os
import sys

# Add include path
sys.path.append("/usr/local/airflow/include")


def _dataset_conf():
    """The training-request conf of this run, or None for a default run."""
    from training.dataset_run import is_dataset_run

    conf = (get_current_context()["dag_run"].conf or {})
    return conf if is_dataset_run(conf) else None


default_args = {
    "owner": "Sales Forecasting Team",
    "depends_on_past": False,
    "start_date": datetime(2025, 7, 22),
    "email_on_failure": False,
    "email_on_retry": False,
    "email": ["kg15072004@gmail.com"],
    "retries": 1,
    "retry_delay": timedelta(seconds=15),
}


@dag(
    schedule="@weekly",
    start_date=datetime(2025, 7, 22),
    catchup=False,
    default_args=default_args,
    description="Train sales forecasting models",
    tags=["ml", "training", "sales"],
)
def sales_forecast_training():
    @task()
    def extract_data_task():
        conf = _dataset_conf()
        if conf:
            from training import dag_tasks

            return dag_tasks.extract(conf)

        from include.data_providers.factory import get_data_provider

        provider = get_data_provider()
        result = provider.provide()
        lineage = result.lineage
        print(f"Data source: {lineage.get('data_source')} version={lineage.get('dataset_version')}")
        print(f"Dataset hash: {lineage.get('dataset_hash')}")
        print(f"Regenerated: {lineage.get('regenerated')}")
        print(f"Rows: {lineage.get('row_count')} stores={lineage.get('store_count')}")
        print(f"Output dir: {result.data_output_dir}")
        print(f"Total files: {result.total_files}")
        for data_type, paths in result.file_paths.items():
            print(f"  - {data_type}: {len(paths)} files")
        return {
            "mode": "default",
            "data_output_dir": result.data_output_dir,
            "file_paths": result.file_paths,
            "total_files": result.total_files,
            "lineage": lineage,
        }

    @task()
    def validate_data_task(extract_result):
        import glob
        import os

        if extract_result.get("mode") == "dataset":
            from training import dag_tasks

            return dag_tasks.validate(extract_result)

        file_paths = extract_result["file_paths"]
        total_rows = 0
        issues_found = []
        print(f"Validating {len(file_paths['sales'])} sales files...")
        for i, sales_file in enumerate(file_paths["sales"][:10]):
            # Defensive: skip missing/empty/corrupt parquet files instead of crashing the DAG.
            if not isinstance(sales_file, str):
                issues_found.append(f"Unexpected sales file type {type(sales_file)}: {sales_file!r}")
                continue
            if not os.path.exists(sales_file):
                issues_found.append(f"Missing file: {sales_file}")
                continue
            if os.path.getsize(sales_file) == 0:
                issues_found.append(f"Zero-byte file: {sales_file}")
                continue
            try:
                df = pd.read_parquet(sales_file)
            except Exception as e:
                issues_found.append(f"Failed to read parquet {sales_file}: {e}")
                continue
            if i == 0:
                print(f"Sales data columns: {df.columns.tolist()}")
            if df.empty:
                issues_found.append(f"Empty file: {sales_file}")
                continue
            required_cols = [
                "date",
                "store_id",
                "product_id",
                "quantity_sold",
                "revenue",
            ]
            missing_cols = set(required_cols) - set(df.columns)
            if missing_cols:
                issues_found.append(f"Missing columns in {sales_file}: {missing_cols}")
            total_rows += len(df)
            if df["quantity_sold"].min() < 0:
                issues_found.append(f"Negative quantities in {sales_file}")
            if df["revenue"].min() < 0:
                issues_found.append(f"Negative revenue in {sales_file}")
        for data_type in ["promotions", "store_events", "customer_traffic"]:
            if data_type in file_paths and file_paths[data_type]:
                sample_file = file_paths[data_type][0]
                try:
                    df = pd.read_parquet(sample_file)
                except Exception as e:
                    issues_found.append(f"Failed to read {data_type} parquet {sample_file}: {e}")
                    continue
                print(f"{data_type} data shape: {df.shape}")
                print(f"{data_type} columns: {df.columns.tolist()}")
        validation_summary = {
            "total_files_validated": len(file_paths["sales"][:10]),
            "total_rows": total_rows,
            "issues_found": len(issues_found),
            "issues": issues_found[:5],
        }
        if issues_found:
            print(f"Validation completed with {len(issues_found)} issues:")
            for issue in issues_found[:5]:
                print(f"  - {issue}")
        else:
            print(f"Validation passed! Total rows: {total_rows}")
        return validation_summary

    @task()
    def train_models_task(extract_result, validation_summary):
        if extract_result.get("mode") == "dataset":
            from training import dag_tasks

            return dag_tasks.train(extract_result, validation_summary)

        from include.data_providers.factory import load_data_config

        file_paths = extract_result["file_paths"]
        data_cfg = load_data_config()
        max_files = data_cfg.get("max_sales_files")
        sales_file_list = file_paths["sales"]
        if max_files is not None:
            sales_file_list = sales_file_list[: int(max_files)]
        print(
            f"Loading sales data from {len(sales_file_list)} of "
            f"{len(file_paths['sales'])} daily parquet files..."
        )
        # Reduced to store-day as each file is read: the full M5 history is
        # ~58M product-day rows, which would not fit in memory concatenated,
        # while the model only ever sees store-day totals.
        store_days = []
        for i, sales_file in enumerate(sales_file_list):
            df = pd.read_parquet(sales_file, columns=["date", "store_id", "revenue"])
            store_days.append(
                df.groupby(["date", "store_id"], as_index=False)["revenue"].sum()
            )
            if (i + 1) % 100 == 0:
                print(f"  Loaded {i + 1} files...")
        daily_sales = pd.concat(store_days, ignore_index=True).rename(
            columns={"revenue": "sales"}
        )
        daily_sales["date"] = pd.to_datetime(daily_sales["date"])
        print(f"Store-day sales shape: {daily_sales.shape}")

        # Exogenous inputs only. `quantity_sold` and `profit` are deliberately
        # absent: both are the same-day target under another name (M5 has
        # unit_price 1.0 and profit = 0.3 * revenue), so a model fed them reads
        # the answer back and learns nothing a scenario could move.
        if file_paths.get("promotions") and len(file_paths["promotions"]) > 0 and os.path.exists(file_paths["promotions"][0]):
            promo_df = pd.read_parquet(file_paths["promotions"][0])
            promo_df["date"] = pd.to_datetime(promo_df["date"])
            # Only genuine promotion records become `has_promotion`. M5's
            # promotions file is the calendar's "any event" flag; labelling it a
            # promotion made the Promotion scenario move a feature that meant
            # something else, so it is not used as one.
            if "has_promotion" in promo_df.columns:
                promo_summary = promo_df.groupby("date", as_index=False)["has_promotion"].max()
                daily_sales = daily_sales.merge(promo_summary, on="date", how="left")
            elif "product_id" in promo_df.columns:
                promo_summary = promo_df[["date"]].drop_duplicates()
                promo_summary["has_promotion"] = 1
                daily_sales = daily_sales.merge(promo_summary, on="date", how="left")
        if "has_promotion" in daily_sales.columns:
            daily_sales["has_promotion"] = daily_sales["has_promotion"].fillna(0)

        # Calendar drivers with a measured effect on M5 store sales: SNAP
        # benefit days (about +10% versus other days) and Christmas, the one day
        # every store is closed. Both are known in advance for any future date,
        # which is what makes them usable as forecast inputs and scenario levers.
        daily_sales["is_closed"] = (
            (daily_sales["date"].dt.month == 12) & (daily_sales["date"].dt.day == 25)
        ).astype(int)
        raw_path = (extract_result.get("lineage") or {}).get("raw_path") or data_cfg.get("raw_path")
        calendar_path = os.path.join(raw_path, "calendar.csv") if raw_path else None
        if calendar_path and os.path.exists(calendar_path):
            snap = pd.read_csv(
                calendar_path, usecols=["date", "snap_CA", "snap_TX", "snap_WI"], parse_dates=["date"]
            ).melt(id_vars="date", var_name="state", value_name="snap_day")
            snap["state"] = snap["state"].str.replace("snap_", "", regex=False)
            daily_sales["state"] = daily_sales["store_id"].astype(str).str[:2]
            daily_sales = daily_sales.merge(snap, on=["date", "state"], how="left").drop(
                columns=["state"]
            )
            daily_sales["snap_day"] = daily_sales["snap_day"].fillna(0).astype(int)

        if file_paths.get("customer_traffic"):
            traffic_dfs = []
            for traffic_file in file_paths["customer_traffic"][:10]:
                if os.path.exists(traffic_file):
                    traffic_dfs.append(pd.read_parquet(traffic_file))
            if traffic_dfs:
                traffic_df = pd.concat(traffic_dfs, ignore_index=True)
                traffic_df["date"] = pd.to_datetime(traffic_df["date"])
                agg_traffic = {}
                if "customer_traffic" in traffic_df.columns:
                    agg_traffic["customer_traffic"] = "sum"
                if "is_holiday" in traffic_df.columns:
                    agg_traffic["is_holiday"] = "max"
                if agg_traffic:
                    traffic_summary = (
                        traffic_df.groupby(["date", "store_id"])
                        .agg(agg_traffic)
                        .reset_index()
                    )
                    daily_sales = daily_sales.merge(
                        traffic_summary, on=["date", "store_id"], how="left"
                    )
                    daily_sales["customer_traffic"] = daily_sales["customer_traffic"].fillna(0)

        # An input that never varied carries no signal, and keeping it would
        # advertise a scenario lever the model cannot respond to (M5 has no
        # traffic data, so this was 0 on every training day).
        constant = [
            column
            for column in ("has_promotion", "customer_traffic")
            if column in daily_sales.columns and daily_sales[column].nunique() <= 1
        ]
        if constant:
            print(f"Dropping exogenous inputs that never vary: {constant}")
            daily_sales = daily_sales.drop(columns=constant)

        # `is_holiday` is produced by the feature pipeline from the US holiday
        # calendar, so it is not built here.
        print(f"Final training data shape: {daily_sales.shape}")
        print(f"Columns: {daily_sales.columns.tolist()}")
        from include.ml_models.train_models import ModelTrainer

        trainer = ModelTrainer()
        store_daily_sales = daily_sales.sort_values(["date", "store_id"]).reset_index(drop=True)
        train_df, val_df, test_df = trainer.prepare_data(
            store_daily_sales,
            target_col="sales",
            date_col="date",
            group_cols=["store_id"],
            categorical_cols=["store_id"],
        )
        print(
            f"Train shape: {train_df.shape}, Val shape: {val_df.shape}, Test shape: {test_df.shape}"
        )
        results = trainer.train_all_models(
            train_df,
            val_df,
            test_df,
            target_col="sales",
            use_optuna=True,
            dataset_lineage=extract_result.get("lineage"),
        )
        for model_name, model_results in results.items():
            if "metrics" in model_results:
                print(f"\n{model_name} metrics:")
                for metric, value in model_results["metrics"].items():
                    print(f"  {metric}: {value:.4f}")
        print("\nVisualization charts have been generated and saved to MLflow/MinIO")
        print("Charts include:")
        print("  - Model metrics comparison")
        print("  - Predictions vs actual values")
        print("  - Residuals analysis")
        print("  - Error distribution")
        print("  - Feature importance comparison")
        serializable_results = {}
        for model_name, model_results in results.items():
            serializable_results[model_name] = {
                "metrics": model_results.get("metrics", {})
            }
        import mlflow

        current_run_id = getattr(trainer, "current_run_id", None)
        if not current_run_id and mlflow.active_run():
            current_run_id = mlflow.active_run().info.run_id
        return {
            "mode": "default",
            "training_results": serializable_results,
            "mlflow_run_id": current_run_id,
            "data_lineage": extract_result.get("lineage"),
        }

    @task()
    def evaluate_models_task(training_result):
        from include.utils.mlflow_utils import MLflowManager

        results = training_result["training_results"]
        mlflow_manager = MLflowManager()
        best_model_name = None
        best_rmse = float("inf")
        for model_name, model_results in results.items():
            if "metrics" in model_results and "rmse" in model_results["metrics"]:
                if model_results["metrics"]["rmse"] < best_rmse:
                    best_rmse = model_results["metrics"]["rmse"]
                    best_model_name = model_name
        print(f"Best model: {best_model_name} with RMSE: {best_rmse:.4f}")
        run_id = training_result.get("mlflow_run_id")
        if not run_id:
            try:
                best_run = mlflow_manager.get_best_model(metric="rmse", ascending=True)
                run_id = best_run["run_id"]
            except Exception as e:
                print(f"Fallback searching best run: {e}")
                exp = mlflow_manager.client.get_experiment_by_name(
                    mlflow_manager.experiment_name
                )
                runs = mlflow_manager.client.search_runs(exp.experiment_id, max_results=1)
                run_id = runs[0].info.run_id if runs else "unknown"
        return {
            "best_model": best_model_name,
            "best_run_id": run_id,
            "mode": training_result.get("mode", "default"),
        }

    @task()
    def register_best_model_task(evaluation_result, training_result):
        from include.utils.mlflow_utils import MLflowManager

        best_model = evaluation_result.get("best_model")
        run_id = evaluation_result["best_run_id"]
        mlflow_manager = MLflowManager()
        model_versions = {}

        # The ensemble is included because it is the default model the inference
        # layer asks for; without a registered ensemble there is no Production
        # version for the serving path to resolve.
        for model_name in ["xgboost", "lightgbm", "ensemble"]:
            try:
                version = mlflow_manager.register_model(run_id, model_name, model_name)
            except Exception as exc:
                # One model failing to register must not hide the others, but it
                # is reported rather than silently skipped.
                print(f"Could not register {model_name}: {exc}")
                continue
            model_versions[model_name] = version
            print(f"Registered {model_name} version: {version}")

        if not model_versions:
            raise RuntimeError("No model could be registered; refusing to report success")

        print(f"Best model by holdout RMSE: {best_model}")
        if training_result.get("mode") == "dataset":
            from training import dag_tasks

            dag_tasks.tag_versions({"versions": model_versions}, training_result)
        return {
            "versions": model_versions,
            "best_model": best_model,
            "mode": training_result.get("mode", "default"),
        }

    @task()
    def transition_to_production_task(registration_result):
        from include.utils.mlflow_utils import MLflowManager

        versions = registration_result.get("versions", registration_result)
        best_model = registration_result.get("best_model")
        if registration_result.get("mode") == "dataset":
            # Uploaded-data candidates wait for an ML engineer's approval; this
            # DAG never promotes them.
            print(f"Manual review policy: not promoting {versions}")
            return {"promoted": {}, "best_model": best_model, "skipped": "manual_review"}

        mlflow_manager = MLflowManager()

        promoted = {}
        for model_name, version in versions.items():
            # `transition_model_stage` now reports whether promotion actually
            # happened, so a silent failure can no longer be printed as success.
            if mlflow_manager.transition_model_stage(model_name, version, "Production"):
                promoted[model_name] = version
                print(f"Transitioned {model_name} v{version} to Production")
            else:
                print(f"FAILED to transition {model_name} v{version} to Production")

        if not promoted:
            raise RuntimeError("No model version could be promoted to Production")

        return {"promoted": promoted, "best_model": best_model}

    @task()
    def generate_performance_report_task(training_result, validation_summary):
        results = training_result["training_results"]
        report = {
            "timestamp": datetime.now().isoformat(),
            "data_lineage": training_result.get("data_lineage"),
            "data_summary": {
                "total_rows": (
                    validation_summary.get("total_rows", 0) if validation_summary else 0
                ),
                "files_validated": (
                    validation_summary.get("total_files_validated", 0)
                    if validation_summary
                    else 0
                ),
                "issues_found": (
                    validation_summary.get("issues_found", 0)
                    if validation_summary
                    else 0
                ),
                "issues": (
                    validation_summary.get("issues", []) if validation_summary else []
                ),
            },
            "model_performance": {},
        }
        if results:
            for model_name, model_results in results.items():
                if "metrics" in model_results:
                    report["model_performance"][model_name] = model_results["metrics"]
        import json

        with open("/tmp/performance_report.json", "w") as f:
            json.dump(report, f, indent=2)
        print("Performance report generated")
        print(f"Models trained: {list(report['model_performance'].keys())}")
        return report

    @task()
    def quality_gate_task(training_result, registration_result):
        if training_result.get("mode") != "dataset":
            return {"skipped": "default run"}
        from training import dag_tasks

        results = dag_tasks.quality_gate(training_result, registration_result)
        if not any(gate["passed"] for gate in results.values()):
            # Recorded first, then failed: the run must not read as a usable
            # candidate when every model failed its gate.
            raise RuntimeError("No candidate passed the quality gate")
        return results

    @task()
    def compare_with_production_task(training_result):
        if training_result.get("mode") != "dataset":
            return {"skipped": "default run"}
        from training import dag_tasks

        return dag_tasks.compare(training_result)

    @task()
    def summarize_training_request_task(training_result, registration_result, gate, comparison):
        if training_result.get("mode") != "dataset":
            return {"skipped": "default run"}
        from training import dag_tasks

        return dag_tasks.summarize(training_result, registration_result, gate, comparison)

    # Task dependencies using function calls
    extract_result = extract_data_task()
    validation_summary = validate_data_task(extract_result)
    training_result = train_models_task(extract_result, validation_summary)
    evaluation_result = evaluate_models_task(training_result)
    model_versions = register_best_model_task(evaluation_result, training_result)
    transition = transition_to_production_task(model_versions)
    gate = quality_gate_task(training_result, model_versions)
    comparison = compare_with_production_task(training_result)
    model_versions >> comparison
    summary = summarize_training_request_task(training_result, model_versions, gate, comparison)
    report = generate_performance_report_task(training_result, validation_summary)
    cleanup = BashOperator(
        task_id="cleanup",
        bash_command="rm -f /tmp/performance_report.json || true",
    )
    report >> cleanup


sales_forecast_training_dag = sales_forecast_training()