"""Dispatch and execute durable OGRRE export jobs in local or Kubernetes workers."""

import logging
import os
import tempfile
import time
from threading import Lock

from ogrre.internal import storage_api, util

_log = logging.getLogger(__name__)
_background_lock = Lock()
_background_failures = {}
_background_active = set()


def _int_setting(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _export_job_mode():
    mode = os.getenv("EXPORT_JOB_MODE") or os.getenv(
        "PROCESSING_JOB_MODE", "background"
    )
    return mode.strip().lower()


def _export_job_name(job_id, attempt=0):
    suffix = f"-{attempt}" if attempt else ""
    return f"ogrre-export-{job_id}{suffix}"


def _export_job_resources():
    cpu = os.getenv(
        "EXPORT_JOB_CPU_REQUEST", os.getenv("PROCESSING_JOB_CPU_REQUEST", "1")
    )
    memory = os.getenv(
        "EXPORT_JOB_MEMORY_REQUEST", os.getenv("PROCESSING_JOB_MEMORY_REQUEST", "4Gi")
    )
    ephemeral_storage = os.getenv(
        "EXPORT_JOB_EPHEMERAL_STORAGE",
        os.getenv("PROCESSING_JOB_EPHEMERAL_STORAGE", "10Gi"),
    )
    return {
        "requests": {
            "cpu": cpu,
            "memory": memory,
            "ephemeral-storage": ephemeral_storage,
        },
        "limits": {
            "cpu": os.getenv(
                "EXPORT_JOB_CPU_LIMIT", os.getenv("PROCESSING_JOB_CPU_LIMIT", cpu)
            ),
            "memory": os.getenv(
                "EXPORT_JOB_MEMORY_LIMIT",
                os.getenv("PROCESSING_JOB_MEMORY_LIMIT", memory),
            ),
            "ephemeral-storage": os.getenv(
                "EXPORT_JOB_EPHEMERAL_STORAGE_LIMIT",
                os.getenv("PROCESSING_JOB_EPHEMERAL_STORAGE_LIMIT", ephemeral_storage),
            ),
        },
    }


def _kubernetes_export_job_manifest(job_id, image, attempt=0):
    job_name = _export_job_name(job_id, attempt)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "labels": {
                "app.kubernetes.io/name": "orphaned-wells-ui-server",
                "app.kubernetes.io/component": "exporter",
                "ogrre.lbl.gov/export-job-id": job_id,
            },
        },
        "spec": {
            "backoffLimit": _int_setting("EXPORT_JOB_BACKOFF_LIMIT", 0),
            "activeDeadlineSeconds": _int_setting(
                "EXPORT_JOB_ACTIVE_DEADLINE_SECONDS", 3600
            ),
            "ttlSecondsAfterFinished": _int_setting(
                "EXPORT_JOB_TTL_SECONDS_AFTER_FINISHED", 86400
            ),
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "orphaned-wells-ui-server",
                        "app.kubernetes.io/component": "exporter",
                        "ogrre.lbl.gov/export-job-id": job_id,
                    }
                },
                "spec": {
                    "serviceAccountName": "processing-worker",
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 30,
                    "imagePullSecrets": [{"name": "dockerhub-pull"}],
                    "containers": [
                        {
                            "name": "exporter",
                            "image": image,
                            "imagePullPolicy": "Always",
                            "command": ["python", "-m", "ogrre.processing_worker"],
                            "args": [
                                "--job-type",
                                "export",
                                "--job-id",
                                job_id,
                            ]
                            + (["--attempt", str(attempt)] if attempt else []),
                            "envFrom": [{"secretRef": {"name": "backend-runtime-env"}}],
                            "env": [
                                {"name": "EXPORT_JOB_MODE", "value": "worker"},
                                {"name": "PROCESSING_JOB_MODE", "value": "worker"},
                                {"name": "LOG_DIR", "value": "/logs"},
                                {
                                    "name": "LOCAL_STORAGE_ROOT",
                                    "value": "/data/local-storage",
                                },
                            ],
                            "resources": _export_job_resources(),
                            "volumeMounts": [
                                {"name": "worker-logs", "mountPath": "/logs"},
                                {"name": "worker-data", "mountPath": "/data"},
                                {
                                    "name": "backend-runtime-files",
                                    "mountPath": "/code/ogrre/storage-service-key.json",
                                    "subPath": "storage-service-key.json",
                                    "readOnly": True,
                                },
                                {
                                    "name": "backend-runtime-files",
                                    "mountPath": "/code/ogrre/document-ai-service-key.json",
                                    "subPath": "document-ai-service-key.json",
                                    "readOnly": True,
                                },
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "worker-logs", "emptyDir": {}},
                        {"name": "worker-data", "emptyDir": {}},
                        {
                            "name": "backend-runtime-files",
                            "secret": {"secretName": "backend-runtime-files"},
                        },
                    ],
                },
            },
        },
    }


def _persist_background_failures(data_manager):
    with _background_lock:
        failures = list(_background_failures.items())
    for (job_id, attempt), message in failures:
        try:
            data_manager.completeExportJob(
                job_id, "error", error=message, attempt=attempt
            )
        except Exception:
            _log.exception(
                "unable to save local export worker failure job_id=%s", job_id
            )
            continue
        with _background_lock:
            _background_failures.pop((job_id, attempt), None)


def _run_background_export_job(job_id, attempt, data_manager):
    try:
        run_export_job(job_id, attempt, data_manager=data_manager)
    except Exception as error:
        _log.exception(
            "local export worker failed job_id=%s attempt=%s", job_id, attempt
        )
        with _background_lock:
            _background_failures[
                (job_id, attempt)
            ] = f"Export generation failed ({type(error).__name__}): {error}"
    finally:
        with _background_lock:
            _background_active.discard((job_id, attempt))
        _persist_background_failures(data_manager)


def run_export_job(job_id, attempt=0, data_manager=None):
    """Execute an export job, writing intermediate files to a temporary directory and uploading the ZIP to storage."""
    if data_manager is None:
        from ogrre.internal.data_manager import DataManager

        data_manager = DataManager()

    existing = data_manager.getExportJob(job_id)
    if existing is None:
        raise ValueError(f"Export job not found: {job_id}")
    if existing.get("attempt", 0) != attempt:
        return existing

    job = data_manager.claimExportJob(job_id, attempt=attempt)
    if job is None:
        return data_manager.getExportJob(job_id)

    try:
        data_manager.updateExportJobProgress(
            job_id,
            stage="collecting_records",
            status_text="Collecting records...",
        )
        records, sets_of_records = data_manager.fetchExportRecordsForJob(job)
        record_count = len(records)

        location = job.get("location") or "record_group"
        target_id = job.get("target_id") or ""
        raw_output_name = job.get("output_name") or "records"
        output_name = util.sanitize_filename_component(raw_output_name) or "records"
        request_origin = job.get("request_origin", "")
        user_info = job.get("request_user", {})
        options = job.get("options", {})
        export_csv = bool(options.get("export_csv", False))
        export_json = bool(options.get("export_json", False))
        export_images = bool(options.get("export_images", False))
        export_embedded_pdfs = bool(options.get("export_embedded_pdfs", False))
        selected_columns = options.get("columns") or []
        keep_all_columns = len(selected_columns) == 0
        export_raw_values = bool(options.get("export_raw_values", False))
        output_file_id = job_id[:8]

        with tempfile.TemporaryDirectory(prefix=f"ogrre-export-{job_id}-") as temp_dir:
            filepaths = []

            if export_csv:
                data_manager.updateExportJobProgress(
                    job_id,
                    stage="generating_csv",
                    status_text=f"Generating CSV for {record_count} record(s)...",
                )
                sets_iter = (
                    sets_of_records.items()
                    if isinstance(sets_of_records, dict)
                    else sets_of_records
                )
                for set_identifier, set_records in sets_iter:
                    safe_set_id = (
                        util.sanitize_filename_component(
                            str(set_identifier or "records")
                        )
                        or "records"
                    )
                    csv_base = os.path.join(temp_dir, f"{safe_set_id}_{output_file_id}")
                    csv_file = data_manager.downloadRecords(
                        set_records,
                        "csv",
                        user_info,
                        target_id,
                        location,
                        selectedColumns=selected_columns,
                        keep_all_columns=keep_all_columns,
                        output_filename=csv_base,
                        request_origin=request_origin,
                        export_raw_values=export_raw_values,
                    )
                    filepaths.append(csv_file)

            if export_json:
                data_manager.updateExportJobProgress(
                    job_id,
                    stage="generating_json",
                    status_text=f"Generating JSON for {record_count} record(s)...",
                )
                json_base = os.path.join(temp_dir, f"{output_name}_{output_file_id}")
                json_file = data_manager.downloadRecords(
                    records,
                    "json",
                    user_info,
                    target_id,
                    location,
                    selectedColumns=selected_columns,
                    keep_all_columns=keep_all_columns,
                    output_filename=json_base,
                    export_raw_values=export_raw_values,
                )
                filepaths.append(json_file)

            last_update = [0.0]

            def make_throttled_progress(stage_name, label_prefix):
                def _callback(current, total):
                    now = time.time()
                    if current < total and (now - last_update[0]) < 0.5:
                        return
                    last_update[0] = now
                    data_manager.updateExportJobProgress(
                        job_id,
                        stage=stage_name,
                        status_text=f"{label_prefix} ({current} / {total})...",
                        current=current,
                        total=total,
                    )

                return _callback

            if export_embedded_pdfs:
                data_manager.updateExportJobProgress(
                    job_id,
                    stage="generating_pdfs",
                    status_text="Generating searchable PDFs...",
                    current=0,
                    total=record_count,
                )
                embedded_pdfs = util.compile_embedded_pdfs(
                    records,
                    on_progress=make_throttled_progress(
                        "generating_pdfs", "Generating searchable PDFs"
                    ),
                )
            else:
                embedded_pdfs = []

            if export_images:
                data_manager.updateExportJobProgress(
                    job_id,
                    stage="calculating_size",
                    status_text="Preparing document images...",
                )
                documents = util.compileDocumentImageList(records)
            else:
                documents = []

            artifact_meta = job.get("artifact") or {}
            archive_filename = artifact_meta.get("filename") or f"{output_name}.zip"
            if not archive_filename.lower().endswith(".zip"):
                archive_filename = f"{archive_filename}.zip"
            local_zip_path = os.path.join(temp_dir, "export_archive.zip")

            data_manager.updateExportJobProgress(
                job_id,
                stage="packaging_archive",
                status_text="Packaging export archive...",
            )
            util.build_export_zip(
                local_zip_path,
                filepaths,
                documents=documents,
                embedded_pdfs=embedded_pdfs,
                include_log=bool(documents),
                on_progress=make_throttled_progress(
                    "packaging_archive", "Packaging export archive"
                ),
            )

            size_bytes = os.path.getsize(local_zip_path)
            storage_key = storage_api.get_export_storage_key(job_id, archive_filename)

            data_manager.updateExportJobProgress(
                job_id,
                stage="uploading_export",
                status_text="Uploading export archive to storage...",
            )
            storage_api.upload_export_artifact(local_zip_path, storage_key)

        ttl_seconds = _int_setting("EXPORT_TTL_SECONDS", 3600)
        return data_manager.completeExportJob(
            job_id,
            "completed",
            artifact={
                "storage_key": storage_key,
                "filename": archive_filename,
                "size_bytes": size_bytes,
                "record_count": record_count,
            },
            ttl_seconds=ttl_seconds,
            attempt=attempt,
        )
    except Exception as error:
        _log.exception("export job %s failed", job_id)
        return data_manager.completeExportJob(
            job_id,
            "error",
            error=str(error) or f"Export failed ({type(error).__name__})",
            attempt=attempt,
        )


def dispatch_export_job(job_id, data_manager, background_tasks=None):
    """Start an export job in background mode (local/dev) or as a Kubernetes Job (GKE)."""
    mode = _export_job_mode()
    job = data_manager.getExportJob(job_id)
    if job is None or job["status"] not in ("queued", "dispatched"):
        return job
    attempt = job.get("attempt", 0)

    if mode == "background":
        if background_tasks is None:
            raise RuntimeError("background_tasks is required in background export mode")
        if data_manager.beginExportJobDispatch(
            job_id, {"worker.mode": "background"}, attempt
        ):
            with _background_lock:
                _background_active.add((job_id, attempt))
            background_tasks.add_task(
                _run_background_export_job, job_id, attempt, data_manager
            )
        return data_manager.getExportJob(job_id)

    if mode != "kubernetes":
        raise ValueError(
            "EXPORT_JOB_MODE must be 'background' for local development or "
            "'kubernetes' for GKE deployments"
        )

    namespace = os.getenv("EXPORT_JOB_NAMESPACE") or os.getenv(
        "PROCESSING_JOB_NAMESPACE"
    )
    image = (
        job.get("worker", {}).get("image")
        or os.getenv("EXPORT_JOB_IMAGE")
        or os.getenv("PROCESSING_JOB_IMAGE")
        or os.getenv("OGRRE_BACKEND_IMAGE")
    )
    if not namespace or not image:
        raise RuntimeError(
            "PROCESSING_JOB_NAMESPACE and PROCESSING_JOB_IMAGE are required in "
            "kubernetes export job mode"
        )

    if not data_manager.reserveExportCapacity(
        f"{job_id}:{attempt}", _int_setting("EXPORT_JOB_MAX_ACTIVE", 2)
    ):
        return data_manager.getExportJob(job_id)

    job_name = job.get("worker", {}).get("kubernetes_job_name") or _export_job_name(
        job_id, attempt
    )
    data_manager.beginExportJobDispatch(
        job_id,
        {
            "worker.mode": "kubernetes",
            "worker.kubernetes_job_name": job_name,
            "worker.image": image,
        },
        attempt,
    )
    current = data_manager.getExportJob(job_id)
    if current.get("attempt", 0) != attempt or current["status"] != "dispatched":
        if current.get("attempt", 0) != attempt or current["status"] != "running":
            data_manager.releaseExportCapacity(f"{job_id}:{attempt}")
        return current

    try:
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException

        config.load_incluster_config()
        api = client.BatchV1Api()
        manifest = _kubernetes_export_job_manifest(job_id, image, attempt)
        manifest["metadata"]["name"] = job_name
        try:
            api.create_namespaced_job(
                namespace=namespace,
                body=manifest,
                _request_timeout=30,
            )
        except ApiException as error:
            if error.status != 409:
                raise
            _log.info(
                "export Job %s already exists in namespace %s", job_name, namespace
            )
    except Exception as error:
        _log.exception("unable to dispatch Kubernetes export job %s", job_id)
        data_manager.completeExportJob(job_id, "error", error=error, attempt=attempt)
        raise RuntimeError("Unable to start export worker") from error
    return data_manager.getExportJob(job_id)


def reconcile_export_job(job_id, data_manager):
    """Reflect an unexpected Kubernetes Job failure or expiration in durable export job status."""
    data_manager.expireExportJobs()
    job = data_manager.getExportJob(job_id)
    if (
        job is None
        or job.get("status") not in ("queued", "dispatched", "running")
        or job.get("worker", {}).get("mode") != "kubernetes"
    ):
        return job

    namespace = os.getenv("EXPORT_JOB_NAMESPACE") or os.getenv(
        "PROCESSING_JOB_NAMESPACE"
    )
    job_name = job.get("worker", {}).get("kubernetes_job_name")
    if not namespace or not job_name:
        return job
    try:
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException

        config.load_incluster_config()
        kubernetes_job = client.BatchV1Api().read_namespaced_job(
            name=job_name,
            namespace=namespace,
            _request_timeout=30,
        )
        for condition in kubernetes_job.status.conditions or []:
            if condition.type == "Failed" and condition.status == "True":
                reason = (
                    condition.message
                    or condition.reason
                    or "Kubernetes export worker failed"
                )
                return data_manager.completeExportJob(
                    job_id, "error", error=reason, attempt=job.get("attempt", 0)
                )
            if condition.type == "Complete" and condition.status == "True":
                return data_manager.completeExportJob(
                    job_id,
                    "error",
                    error="Kubernetes export worker exited without recording a final job status",
                    attempt=job.get("attempt", 0),
                )
    except ApiException as error:
        if error.status == 404:
            if job["status"] == "dispatched":
                return dispatch_export_job(job_id, data_manager)
            return data_manager.completeExportJob(
                job_id,
                "error",
                error="Kubernetes export worker Job was not found",
                attempt=job.get("attempt", 0),
            )
        _log.warning("unable to reconcile export Job %s: %s", job_id, error)
    except Exception as error:
        _log.warning("unable to reconcile export Job %s: %s", job_id, error)
    return data_manager.getExportJob(job_id)


def maintain_export_jobs(data_manager):
    """Expire old artifacts, dispatch queued exports, and reconcile active Kubernetes workers."""
    try:
        data_manager.expireExportJobs()
    except Exception:
        _log.exception("unable to expire export jobs")

    mode = _export_job_mode()
    if mode == "background":
        _persist_background_failures(data_manager)
        return
    if mode != "kubernetes":
        return

    for job in data_manager.getActiveExportJobs():
        try:
            if job["status"] == "queued":
                dispatch_export_job(job["job_id"], data_manager)
            else:
                reconcile_export_job(job["job_id"], data_manager)
        except Exception:
            _log.exception("unable to maintain export job %s", job["job_id"])

    capacity = data_manager.db.processing_capacity.find_one({"_id": "export"}) or {}
    for token in capacity.get("jobs", []):
        job_id, attempt = token.rsplit(":", 1)
        job = data_manager.getExportJob(job_id)
        if (
            not job
            or job["status"] not in ("queued", "dispatched", "running")
            or str(job.get("attempt", 0)) != attempt
        ):
            data_manager.releaseExportCapacity(token)
