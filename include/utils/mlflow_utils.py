import os
import mlflow
import mlflow.sklearn
import mlflow.xgboost
import mlflow.lightgbm
import mlflow.pyfunc
from mlflow.tracking import MlflowClient
from typing import Dict, Any, Optional, List
import yaml
import pandas as pd
import numpy as np
from datetime import datetime
import logging
import joblib
from .service_discovery import get_mlflow_endpoint, get_minio_endpoint

logger = logging.getLogger(__name__)


class MLflowManager:
    def __init__(self, config_path: str = "/usr/local/airflow/include/config/ml_config.yaml"):
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)
        
        mlflow_config = self.config['mlflow']
        # Use service discovery to get tracking URI
        self.tracking_uri = get_mlflow_endpoint()
        
        self.experiment_name = mlflow_config['experiment_name']
        self.registry_name = mlflow_config['registry_name']
        
        mlflow.set_tracking_uri(self.tracking_uri)
        
        # Try to create experiment, with fallback
        try:
            mlflow.set_experiment(self.experiment_name)
        except Exception as e:
            logger.warning(f"Failed to set experiment {self.experiment_name}: {e}")
            # Try with localhost if initial connection failed
            if 'mlflow' in self.tracking_uri:
                self.tracking_uri = "http://localhost:5001"
                mlflow.set_tracking_uri(self.tracking_uri)
                os.environ['MLFLOW_TRACKING_URI'] = self.tracking_uri
                logger.info(f"Retrying with localhost: {self.tracking_uri}")
                try:
                    mlflow.set_experiment(self.experiment_name)
                except Exception as e2:
                    logger.error(f"Failed to connect to MLflow: {e2}")
        
        # Configure S3 endpoint for MinIO using service discovery
        os.environ['MLFLOW_S3_ENDPOINT_URL'] = get_minio_endpoint()
        os.environ['AWS_ACCESS_KEY_ID'] = os.getenv('AWS_ACCESS_KEY_ID', 'minioadmin')
        os.environ['AWS_SECRET_ACCESS_KEY'] = os.getenv('AWS_SECRET_ACCESS_KEY', 'minioadmin')
        
        self.client = MlflowClient(tracking_uri=self.tracking_uri)
        
    def start_run(self, run_name: Optional[str] = None, tags: Optional[Dict[str, str]] = None) -> str:
        if run_name is None:
            run_name = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        run = mlflow.start_run(run_name=run_name, tags=tags)
        logger.info(f"Started MLflow run: {run.info.run_id}")
        return run.info.run_id
    
    def log_params(self, params: Dict[str, Any]):
        for key, value in params.items():
            mlflow.log_param(key, value)
    
    def log_metrics(self, metrics: Dict[str, float], step: Optional[int] = None):
        for key, value in metrics.items():
            mlflow.log_metric(key, value, step=step)
    
    def log_model(self, model, model_name: str, input_example: Optional[pd.DataFrame] = None,
                signature: Optional[Any] = None, registered_model_name: Optional[str] = None):
        """
        Log model to MLflow with compatibility for different versions.
        Falls back to saving models as artifacts if MLflow model logging fails.
        """
        try:
            # Save model to a temporary file first
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                model_path = os.path.join(tmpdir, f"{model_name}_model.pkl")
                joblib.dump(model, model_path)
                
                # Log as artifact
                mlflow.log_artifact(model_path, artifact_path=f"models/{model_name}")
                logger.info(f"Successfully saved {model_name} model as artifact")
                
                # Also save metadata
                metadata = {
                    "model_type": model_name,
                    "framework": type(model).__module__,
                    "class": type(model).__name__,
                    "timestamp": datetime.now().isoformat()
                }
                metadata_path = os.path.join(tmpdir, f"{model_name}_metadata.yaml")
                with open(metadata_path, 'w') as f:
                    yaml.dump(metadata, f)
                mlflow.log_artifact(metadata_path, artifact_path=f"models/{model_name}")

            # Additionally log a *real* MLflow model.
            #
            # The joblib pickle above is kept because the Streamlit loader reads
            # it directly, but a pickle is not a registrable MLflow model: it has
            # no MLmodel descriptor, so `register_model` had nothing to point at
            # and the registry stayed empty. This writes a proper flavoured model
            # alongside it, under `mlflow_model/{name}`, which IS registrable.
            self.log_mlflow_model(model, model_name, input_example=input_example)

        except Exception as e:
            logger.error(f"Failed to log model {model_name}: {e}")
            # Don't fail the entire run, just log the error

    def mlflow_model_path(self, model_name: str) -> str:
        """Artifact path of the registrable MLflow model for `model_name`."""
        return f"mlflow_model/{model_name}"

    @staticmethod
    def _ensemble_code_paths() -> List[str]:
        """
        Source directories to bundle with a custom-class model.

        Without this, the ensemble's pickle references `ml_models.ensemble_model`
        and only loads where that package happens to be importable.
        """
        try:
            import ml_models

            package_dir = os.path.dirname(os.path.abspath(ml_models.__file__))
            return [package_dir] if os.path.isdir(package_dir) else []
        except Exception as exc:  # noqa: BLE001 - bundling is best effort
            logger.warning("Could not resolve ml_models for code_paths: %s", exc)
            return []

    def log_mlflow_model(self, model, model_name: str,
                        input_example: Optional[pd.DataFrame] = None) -> Optional[str]:
        """
        Log a flavoured MLflow model so the version can be registered.

        Uses the native flavour where one exists and falls back to the generic
        sklearn flavour (the ensemble implements `predict`, which is all the
        flavour requires). Returns the artifact path, or None on failure.
        """
        artifact_path = self.mlflow_model_path(model_name)
        try:
            import shutil
            import tempfile

            # `log_model` is deliberately avoided: the installed client (MLflow
            # 3.x) routes it through the /api/2.0/mlflow/logged-models endpoint,
            # which the 2.x tracking server does not implement, so the call
            # fails outright. `save_model` is purely local and `log_artifacts`
            # uses the long-standing artifact API, so this works against either
            # server generation. The uploaded directory still contains a real
            # MLmodel descriptor, which is all `register_model` needs.
            with tempfile.TemporaryDirectory() as tmpdir:
                model_dir = os.path.join(tmpdir, model_name)
                # save_model requires the destination not to exist.
                shutil.rmtree(model_dir, ignore_errors=True)

                save_kwargs = {"path": model_dir}
                if input_example is not None:
                    save_kwargs["input_example"] = input_example

                if model_name == "xgboost":
                    mlflow.xgboost.save_model(xgb_model=model, **save_kwargs)
                elif model_name == "lightgbm":
                    mlflow.lightgbm.save_model(lgb_model=model, **save_kwargs)
                else:
                    # cloudpickle is requested explicitly: the sklearn flavour
                    # now defaults to skops, which refuses to serialise
                    # "untrusted" non-sklearn types and so rejects the ensemble
                    # (it wraps XGBoost and LightGBM boosters).
                    #
                    # cloudpickle pickles the class by *reference* whenever its
                    # module is importable, so a model saved here could only be
                    # loaded where `ml_models.ensemble_model` was on the path --
                    # it failed with ModuleNotFoundError anywhere else. The
                    # module is therefore bundled into the model via code_paths,
                    # which makes the artifact self-contained.
                    code_paths = self._ensemble_code_paths()
                    if code_paths:
                        save_kwargs["code_paths"] = code_paths
                    mlflow.sklearn.save_model(
                        sk_model=model,
                        serialization_format=mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE,
                        **save_kwargs,
                    )

                if not os.path.isfile(os.path.join(model_dir, "MLmodel")):
                    raise RuntimeError("save_model produced no MLmodel descriptor")

                mlflow.log_artifacts(model_dir, artifact_path=artifact_path)

            logger.info("Logged registrable MLflow model at %s", artifact_path)
            return artifact_path
        except Exception as e:
            logger.error("Failed to log MLflow model %s: %s", model_name, e)
            return None
    
    def log_artifacts(self, artifact_path: str):
        mlflow.log_artifacts(artifact_path)
    
    def log_figure(self, figure, artifact_file: str):
        mlflow.log_figure(figure, artifact_file)
    
    def end_run(self, status: str = "FINISHED"):
        # Get run ID before ending
        run = mlflow.active_run()
        run_id = run.info.run_id if run else None
        
        mlflow.end_run(status=status)
        logger.info("Ended MLflow run")
        
        # Sync artifacts to S3 after run ends
        if run_id and status == "FINISHED":
            try:
                from utils.mlflow_s3_utils import MLflowS3Manager
                s3_manager = MLflowS3Manager()
                s3_manager.sync_mlflow_artifacts_to_s3(run_id)
                logger.info(f"Synced artifacts to S3 for run {run_id}")
            except Exception as e:
                logger.warning(f"Failed to sync artifacts to S3: {e}")
    
    def get_best_model(self, metric: str = "rmse", ascending: bool = True) -> Dict[str, Any]:
        experiment = mlflow.get_experiment_by_name(self.experiment_name)
        runs = mlflow.search_runs(
            experiment_ids=[experiment.experiment_id],
            order_by=[f"metrics.{metric} {'ASC' if ascending else 'DESC'}"],
            max_results=1
        )
        
        if len(runs) == 0:
            raise ValueError("No runs found in the experiment")
        
        best_run = runs.iloc[0]
        return {
            "run_id": best_run["run_id"],
            "metrics": {col.replace("metrics.", ""): val 
                    for col, val in best_run.items() 
                    if col.startswith("metrics.")},
            "params": {col.replace("params.", ""): val 
                    for col, val in best_run.items() 
                    if col.startswith("params.")}
        }
    
    def load_model(self, model_uri: str):
        """Load model from MLflow or from artifacts"""
        try:
            return mlflow.pyfunc.load_model(model_uri)
        except:
            # Try loading from artifacts
            if "runs:/" in model_uri:
                run_id = model_uri.split("/")[1]
                artifact_path = "/".join(model_uri.split("/")[2:])
                local_path = mlflow.artifacts.download_artifacts(
                    run_id=run_id, 
                    artifact_path=f"{artifact_path}_model.pkl"
                )
                return joblib.load(local_path)
            else:
                raise ValueError(f"Cannot load model from {model_uri}")
    
    def register_model(self, run_id: str, model_name: str, artifact_path: str) -> str:
        """
        Register the run's MLflow model and return the new version number.

        `artifact_path` previously pointed at a bare pickle directory, which is
        not a registrable model; the registrable path is used instead and the
        caller's value is only a fallback. A failure is logged with its real
        cause rather than being hidden behind a bare `except`.
        """
        registered_name = f"{self.registry_name}_{model_name}"
        candidates = [self.mlflow_model_path(model_name), artifact_path]

        last_error: Optional[Exception] = None
        for path in candidates:
            try:
                model_version = mlflow.register_model(f"runs:/{run_id}/{path}", registered_name)
                logger.info(
                    "Registered %s version %s from %s", registered_name, model_version.version, path
                )
                return str(model_version.version)
            except Exception as e:
                last_error = e
                logger.warning("Could not register %s from '%s': %s", registered_name, path, e)

        logger.error("Model registration failed for %s: %s", registered_name, last_error)
        raise RuntimeError(f"Could not register {registered_name}: {last_error}") from last_error
    
    def transition_model_stage(self, model_name: str, version: str, stage: str) -> bool:
        """
        Promote a model version, and report whether it actually worked.

        The deployed client (MLflow 3.x) removed `transition_model_version_stage`
        while the tracking server is 2.x, so promotion is done two ways:

        1. a registry **alias** (`@production`), which both versions support and
           which is the forward-looking mechanism, and
        2. the server's REST stage endpoint, so readers that still filter on
           `current_stage == "Production"` keep working.

        Returns True if at least one succeeded. Previously this swallowed every
        failure, which is why the registry reported no Production model while the
        DAG logged success.
        """
        registered_name = f"{self.registry_name}_{model_name}"
        alias = stage.strip().lower()
        succeeded = []

        try:
            self.client.set_registered_model_alias(registered_name, alias, version)
            succeeded.append(f"alias @{alias}")
        except Exception as e:
            logger.warning("Could not set alias @%s on %s: %s", alias, registered_name, e)

        try:
            self._transition_stage_via_rest(registered_name, version, stage)
            succeeded.append(f"stage {stage}")
        except Exception as e:
            logger.warning("Could not set stage %s on %s: %s", stage, registered_name, e)

        if succeeded:
            logger.info("Promoted %s v%s (%s)", registered_name, version, ", ".join(succeeded))
            return True

        logger.error("Promotion failed for %s v%s", registered_name, version)
        return False

    def _transition_stage_via_rest(self, registered_name: str, version: str, stage: str) -> None:
        """
        Set a version's stage through the tracking server's REST API.

        Used because the installed client no longer exposes the stage call; the
        server still implements the endpoint.
        """
        import json
        import urllib.error
        import urllib.request

        url = f"{self.tracking_uri.rstrip('/')}/api/2.0/mlflow/model-versions/transition-stage"
        payload = json.dumps(
            {
                "name": registered_name,
                "version": str(version),
                "stage": stage,
                "archive_existing_versions": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.getcode() not in (200, 201):
                    raise RuntimeError(f"unexpected status {response.getcode()}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from e
    
    def get_latest_model_version(self, model_name: str, stage: Optional[str] = None) -> Dict[str, Any]:
        try:
            filter_string = f"name='{self.registry_name}_{model_name}'"
            if stage:
                filter_string += f" AND current_stage='{stage}'"
            
            versions = self.client.search_model_versions(filter_string)
            if not versions:
                raise ValueError(f"No model versions found for {model_name}")
            
            latest_version = max(versions, key=lambda x: int(x.version))
            return {
                "version": latest_version.version,
                "stage": latest_version.current_stage,
                "run_id": latest_version.run_id,
                "source": latest_version.source
            }
        except:
            # Fallback to finding the best run
            best_model = self.get_best_model()
            return {
                "version": best_model["run_id"],
                "stage": "None",
                "run_id": best_model["run_id"],
                "source": f"runs:/{best_model['run_id']}/models"
            }