"""
Server-side loading of a model version's artifacts.

Downloads the joblib artifacts a training run produced and hands back a ready
``ModelBundle``. Downloads are cached on disk (so a restart does not refetch)
and loaded bundles are cached in memory (so a forecast does not re-unpickle a
model on every request).

Transport
---------
Artifacts are fetched over the tracking server's ``/get-artifact`` proxy rather
than through the MLflow client's artifact repository. Two reasons:

* it needs no object-store credentials in this process -- the server proxies
  MinIO, so no MinIO key ever has to be present at request time;
* it is immune to the client/server version split: the 3.x client's
  ``list_artifacts``/``download_artifacts`` path calls ``/logged-models``
  endpoints that the 2.9.2 server does not expose.

The MLflow client remains a fallback for deployments where the proxy is off.

Why the raw joblib artifacts and not the flavoured model
-------------------------------------------------------
Training also writes ``mlflow_model/<name>``, but those were produced by MLflow
3.x: the LightGBM flavour is serialised as ``model.skops`` and the descriptors
declare ``mlflow_version: 3.17.1``. Loading them would couple this service to
skops and to the writer's exact flavour version. The raw artifacts are the same
weights, carry the preprocessing objects inference also needs, and are what the
existing Streamlit stack has always read -- so model behaviour is identical.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from api.config import (
    ENCODERS_ARTIFACT,
    FEATURE_COLS_ARTIFACT,
    SCALERS_ARTIFACT,
    TRAINING_SCALER_KEY,
    Settings,
    get_settings,
)
from api.core.errors import ArtifactDownloadFailedError

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_SECONDS = 60.0
STORE_ENCODER_KEY = "store_id"


@dataclass
class ModelBundle:
    """Everything needed to turn a feature row into a prediction."""

    run_id: str
    model_name: str
    model: Any
    feature_cols: List[str]
    scaler: Optional[Any] = None
    encoders: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Normalise the encoder mapping so callers can index it safely."""
        if self.encoders is None:
            self.encoders = {}

    @property
    def supported_stores(self) -> List[str]:
        """Store identifiers the label encoder recognises, in encoder order."""
        encoder = self.encoders.get(STORE_ENCODER_KEY)
        classes = getattr(encoder, "classes_", None)
        if classes is None:
            return []
        return [str(value) for value in classes]

    @property
    def feature_count(self) -> int:
        """Number of model inputs."""
        return len(self.feature_cols)


class ArtifactService:
    """
    Fetches and caches the artifacts of a training run.

    ``downloader`` and ``loader`` are injectable so the cache, the bundle
    assembly, and the error contract can all be tested without MLflow, a
    network, or a real pickle on disk.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        downloader: Optional[Any] = None,
        loader: Optional[Any] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._downloader = downloader or self._download_artifact
        self._loader = loader or _joblib_load
        self._bundles: "OrderedDict[Tuple[str, str], ModelBundle]" = OrderedDict()
        self._lock = threading.Lock()
        self._path_prepared = False

    # -- import path -------------------------------------------------------

    def _ensure_import_path(self) -> None:
        """
        Make the training packages importable.

        The ensemble artifact is a pickle of ``ml_models.ensemble_model
        .EnsembleModel``; cloudpickle stored it by reference, so unpickling
        needs that module on ``sys.path``. Done once per process, right before
        the first load.
        """
        if self._path_prepared:
            return
        import sys

        include_dir = self.settings.include_dir
        if os.path.isdir(include_dir) and include_dir not in sys.path:
            sys.path.insert(0, include_dir)
        self._path_prepared = True

    # -- download ----------------------------------------------------------

    def _cache_path(self, run_id: str, artifact_path: str) -> str:
        """Local path an artifact is cached at."""
        return os.path.join(self.settings.model_cache_dir, run_id, artifact_path)

    def _download_artifact(self, run_id: str, artifact_path: str) -> str:
        """
        Fetch one artifact, returning its local path.

        A cached copy is reused as-is: run artifacts are immutable, so there is
        nothing to invalidate.
        """
        destination = self._cache_path(run_id, artifact_path)
        if os.path.isfile(destination) and os.path.getsize(destination) > 0:
            return destination

        os.makedirs(os.path.dirname(destination), exist_ok=True)
        tracking_uri = self.settings.mlflow_tracking_uri

        if tracking_uri.startswith(("http://", "https://")):
            try:
                self._http_download(tracking_uri, run_id, artifact_path, destination)
                return destination
            except Exception as exc:  # noqa: BLE001 - fall through to the client
                logger.info(
                    "Artifact proxy download failed for %s, trying the MLflow "
                    "client: %s",
                    artifact_path,
                    exc,
                )

        self._client_download(run_id, artifact_path, destination)
        return destination

    @staticmethod
    def _http_download(
        tracking_uri: str, run_id: str, artifact_path: str, destination: str
    ) -> None:
        """Download via the tracking server's artifact proxy."""
        query = urllib.parse.urlencode({"run_uuid": run_id, "path": artifact_path})
        url = f"{tracking_uri.rstrip('/')}/get-artifact?{query}"
        partial = f"{destination}.partial"
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            if response.getcode() != 200:
                raise RuntimeError(f"artifact proxy returned {response.getcode()}")
            with open(partial, "wb") as handle:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
        # Rename only after a complete read, so an interrupted download never
        # leaves a truncated file that the cache would then trust.
        os.replace(partial, destination)

    def _client_download(self, run_id: str, artifact_path: str, destination: str) -> None:
        """Download via the MLflow client, as a fallback."""
        try:
            import mlflow

            if self.settings.mlflow_tracking_uri:
                mlflow.set_tracking_uri(self.settings.mlflow_tracking_uri)
            local = mlflow.artifacts.download_artifacts(
                run_id=run_id,
                artifact_path=artifact_path,
                dst_path=os.path.dirname(destination),
            )
            if os.path.abspath(local) != os.path.abspath(destination) and os.path.isfile(
                local
            ):
                os.replace(local, destination)
        except Exception as exc:  # noqa: BLE001 - reported as a typed error
            raise ArtifactDownloadFailedError(
                "A required model artifact could not be retrieved.",
                details={"artifact": artifact_path, "error": str(exc)},
            ) from exc

    # -- load --------------------------------------------------------------

    def _load(self, run_id: str, artifact_path: str, required: bool) -> Optional[Any]:
        """
        Download and deserialise one artifact.

        An optional artifact that is absent returns ``None``; a required one
        that is absent or unreadable is an ``ArtifactDownloadFailedError``.
        """
        try:
            local_path = self._downloader(run_id, artifact_path)
            return self._loader(local_path)
        except ArtifactDownloadFailedError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified by `required`
            if not required:
                logger.info(
                    "Optional artifact %s unavailable for run %s: %s",
                    artifact_path,
                    run_id,
                    exc,
                )
                return None
            raise ArtifactDownloadFailedError(
                "A required model artifact could not be retrieved.",
                details={"artifact": artifact_path, "error": str(exc)},
            ) from exc

    @staticmethod
    def _normalise_scalers(scalers: Any) -> Optional[Any]:
        """
        Pick the feature scaler out of the scalers artifact.

        Training writes ``{'standard': StandardScaler}``; earlier runs used
        ``'features'``. Both are accepted so an older run stays loadable.
        """
        if scalers is None:
            return None
        if isinstance(scalers, dict):
            for key in (TRAINING_SCALER_KEY, "features"):
                if key in scalers:
                    return scalers[key]
            return None
        return scalers

    def load_bundle(self, run_id: str, model_name: str) -> ModelBundle:
        """
        Load (or return the cached) bundle for one model of one run.

        The model and the ``feature_cols`` list are required: without either
        there is no way to build a correctly ordered feature matrix. The scaler
        and encoders are optional, because a future run might not need them.
        """
        key = (run_id, model_name)
        with self._lock:
            cached = self._bundles.get(key)
            if cached is not None:
                self._bundles.move_to_end(key)
                return cached

        self._ensure_import_path()

        feature_cols = self._load(run_id, FEATURE_COLS_ARTIFACT, required=True)
        if not feature_cols:
            raise ArtifactDownloadFailedError(
                "The model's feature list artifact is empty or unreadable.",
                details={"artifact": FEATURE_COLS_ARTIFACT},
            )

        model = self._load(
            run_id, self.settings.model_artifact_path(model_name), required=True
        )
        if model is None or not hasattr(model, "predict"):
            raise ArtifactDownloadFailedError(
                "The stored model artifact is not a usable predictor.",
                details={"model": model_name},
            )

        scaler = self._normalise_scalers(
            self._load(run_id, SCALERS_ARTIFACT, required=False)
        )
        encoders = self._load(run_id, ENCODERS_ARTIFACT, required=False) or {}
        if not isinstance(encoders, dict):
            encoders = {}

        bundle = ModelBundle(
            run_id=run_id,
            model_name=model_name,
            model=model,
            feature_cols=[str(column) for column in feature_cols],
            scaler=scaler,
            encoders=encoders,
        )

        with self._lock:
            self._bundles[key] = bundle
            while len(self._bundles) > max(1, self.settings.model_cache_size):
                evicted, _ = self._bundles.popitem(last=False)
                logger.info("Evicted cached model bundle %s", evicted)
        return bundle

    def try_load_bundle(self, run_id: str, model_name: str) -> Optional[ModelBundle]:
        """Load a bundle, returning ``None`` instead of raising. For probes."""
        try:
            return self.load_bundle(run_id, model_name)
        except Exception as exc:  # noqa: BLE001 - probes report, never raise
            logger.info("Could not load bundle %s/%s: %s", run_id, model_name, exc)
            return None

    def clear_cache(self) -> None:
        """Drop in-memory bundles. The on-disk cache is left in place."""
        with self._lock:
            self._bundles.clear()


def _joblib_load(path: str) -> Any:
    """
    Deserialise an artifact, preferring joblib and falling back to pickle.

    Mirrors the existing loader's behaviour: some artifacts in older runs were
    written with plain ``pickle``.
    """
    import joblib

    try:
        return joblib.load(path)
    except Exception as joblib_error:  # noqa: BLE001 - try the other reader
        logger.info("joblib could not read %s (%s); trying pickle", path, joblib_error)
        import pickle

        with open(path, "rb") as handle:
            return pickle.load(handle)


__all__ = ["STORE_ENCODER_KEY", "ArtifactService", "ModelBundle"]
