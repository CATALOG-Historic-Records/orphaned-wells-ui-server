"""Dispatch durable OGRRE processing jobs to local or Kubernetes workers."""

import logging
import os
import time

_log = logging.getLogger(__name__)


def _processing_job_mode():
    return os.getenv("PROCESSING_JOB_MODE", "background").strip().lower()


def _int_setting(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _processing_job_name(job_id, attempt=0):
    suffix = f"-{attempt}" if attempt else ""
    return f"ogrre-batch-{job_id}{suffix}"


def _processing_job_resources():
    cpu = os.getenv("PROCESSING_JOB_CPU_REQUEST", "1850m")
    memory = os.getenv("PROCESSING_JOB_MEMORY_REQUEST", "12Gi")
    ephemeral_storage = os.getenv("PROCESSING_JOB_EPHEMERAL_STORAGE", "10Gi")
    return {
        "requests": {
            "cpu": cpu,
            "memory": memory,
            "ephemeral-storage": ephemeral_storage,
        },
        "limits": {
            "cpu": os.getenv("PROCESSING_JOB_CPU_LIMIT", cpu),
            "memory": os.getenv("PROCESSING_JOB_MEMORY_LIMIT", memory),
            "ephemeral-storage": os.getenv(
                "PROCESSING_JOB_EPHEMERAL_STORAGE_LIMIT", ephemeral_storage
            ),
        },
    }


def _kubernetes_job_manifest(job_id, image, attempt=0):
    job_name = _processing_job_name(job_id, attempt)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "labels": {
                "app.kubernetes.io/name": "orphaned-wells-ui-server",
                "app.kubernetes.io/component": "processor",
                "ogrre.lbl.gov/processing-job-id": job_id,
            },
        },
        "spec": {
            "backoffLimit": _int_setting("PROCESSING_JOB_BACKOFF_LIMIT", 0),
            "activeDeadlineSeconds": _int_setting(
                "PROCESSING_JOB_ACTIVE_DEADLINE_SECONDS", 28800
            ),
            "ttlSecondsAfterFinished": _int_setting(
                "PROCESSING_JOB_TTL_SECONDS_AFTER_FINISHED", 604800
            ),
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "orphaned-wells-ui-server",
                        "app.kubernetes.io/component": "processor",
                        "ogrre.lbl.gov/processing-job-id": job_id,
                    }
                },
                "spec": {
                    "serviceAccountName": "processing-worker",
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 30,
                    "imagePullSecrets": [{"name": "dockerhub-pull"}],
                    "containers": [
                        {
                            "name": "processor",
                            "image": image,
                            "imagePullPolicy": "Always",
                            "command": ["python", "-m", "ogrre.processing_worker"],
                            "args": ["--job-id", job_id]
                            + (["--attempt", str(attempt)] if attempt else []),
                            "envFrom": [{"secretRef": {"name": "backend-runtime-env"}}],
                            "env": [
                                {"name": "PROCESSING_JOB_MODE", "value": "worker"},
                                {"name": "LOG_DIR", "value": "/logs"},
                                {
                                    "name": "LOCAL_STORAGE_ROOT",
                                    "value": "/data/local-storage",
                                },
                            ],
                            "resources": _processing_job_resources(),
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


def dispatch_batch_processing_job(job_id, data_manager, background_tasks=None):
    """Start a durable batch job without running processing in the API Pod."""
    mode = _processing_job_mode()
    job = data_manager.getProcessingJob(job_id)
    if job is None or job["status"] not in ("queued", "dispatched"):
        return job
    attempt = job.get("attempt", 0)
    expires_at = job.get("input", {}).get("upload_expires_at")
    if expires_at and expires_at <= time.time():
        return data_manager.completeProcessingJob(
            job_id,
            "error",
            "The directory upload expired before processing started.",
            attempt=attempt,
        )
    if mode == "background":
        if background_tasks is None:
            raise RuntimeError("background_tasks is required in background job mode")
        from ogrre.processing_worker import run_processing_job

        if data_manager.beginProcessingJobDispatch(
            job_id, {"worker.mode": "background"}, attempt
        ):
            background_tasks.add_task(run_processing_job, job_id, attempt)
        return data_manager.getProcessingJob(job_id)

    if mode != "kubernetes":
        raise ValueError(
            "PROCESSING_JOB_MODE must be 'background' for local development or "
            "'kubernetes' for GKE deployments"
        )

    namespace = os.getenv("PROCESSING_JOB_NAMESPACE")
    image = (
        job.get("worker", {}).get("image")
        or os.getenv("PROCESSING_JOB_IMAGE")
        or os.getenv("OGRRE_BACKEND_IMAGE")
    )
    if not namespace or not image:
        raise RuntimeError(
            "PROCESSING_JOB_NAMESPACE and PROCESSING_JOB_IMAGE are required in "
            "kubernetes processing job mode"
        )

    if not data_manager.reserveProcessingCapacity(
        f"{job_id}:{attempt}", _int_setting("PROCESSING_JOB_MAX_ACTIVE", 1)
    ):
        return data_manager.getProcessingJob(job_id)
    job_name = job.get("worker", {}).get("kubernetes_job_name") or _processing_job_name(
        job_id, attempt
    )
    data_manager.beginProcessingJobDispatch(
        job_id,
        {
            "worker.mode": "kubernetes",
            "worker.kubernetes_job_name": job_name,
            "worker.image": image,
        },
        attempt,
    )
    current = data_manager.getProcessingJob(job_id)
    if current.get("attempt", 0) != attempt or current["status"] != "dispatched":
        if current.get("attempt", 0) != attempt or current["status"] != "running":
            data_manager.releaseProcessingCapacity(f"{job_id}:{attempt}")
        return current
    try:
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException

        config.load_incluster_config()
        api = client.BatchV1Api()
        manifest = _kubernetes_job_manifest(job_id, image, attempt)
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
                "processing Job %s already exists in namespace %s", job_name, namespace
            )
    except Exception as error:
        _log.exception("unable to dispatch Kubernetes processing job %s", job_id)
        data_manager.completeProcessingJob(
            job_id, "error", error=error, attempt=attempt
        )
        raise RuntimeError("Unable to start document processing worker") from error
    return data_manager.getProcessingJob(job_id)


def reconcile_processing_job(job_id, data_manager):
    """Reflect an unexpected Kubernetes Job failure in durable job status."""
    job = data_manager.getProcessingJob(job_id)
    if (
        job is None
        or job.get("status") not in ("queued", "dispatched", "running")
        or job.get("worker", {}).get("mode") != "kubernetes"
    ):
        return job

    namespace = os.getenv("PROCESSING_JOB_NAMESPACE")
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
                    condition.message or condition.reason or "Kubernetes worker failed"
                )
                return data_manager.completeProcessingJob(
                    job_id, "error", error=reason, attempt=job.get("attempt", 0)
                )
            if condition.type == "Complete" and condition.status == "True":
                return data_manager.completeProcessingJob(
                    job_id,
                    "error",
                    error="Kubernetes worker exited without recording a final job status",
                    attempt=job.get("attempt", 0),
                )
    except ApiException as error:
        if error.status == 404:
            # A dispatcher can stop between persisting intent and creating the Job.
            if job["status"] == "dispatched":
                return dispatch_batch_processing_job(job_id, data_manager)
            return data_manager.completeProcessingJob(
                job_id,
                "error",
                error="Kubernetes worker Job was not found",
                attempt=job.get("attempt", 0),
            )
        _log.warning("unable to reconcile processing Job %s: %s", job_id, error)
    except Exception as error:
        _log.warning("unable to reconcile processing Job %s: %s", job_id, error)
    return data_manager.getProcessingJob(job_id)


def maintain_processing_jobs(data_manager):
    """Recover dispatches and release failed workers even when no browser is open."""
    if _processing_job_mode() != "kubernetes":
        return
    for job in data_manager.getActiveProcessingJobs():
        try:
            if job["status"] == "queued":
                dispatch_batch_processing_job(job["job_id"], data_manager)
            else:
                reconcile_processing_job(job["job_id"], data_manager)
        except Exception:
            _log.exception("unable to maintain processing job %s", job["job_id"])
    # Repair a crash after the terminal state write but before releasing capacity.
    capacity = (
        data_manager.db.processing_capacity.find_one({"_id": "batch_document"}) or {}
    )
    for token in capacity.get("jobs", []):
        job_id, attempt = token.rsplit(":", 1)
        job = data_manager.getProcessingJob(job_id)
        if (
            not job
            or job["status"] not in ("queued", "dispatched", "running")
            or str(job.get("attempt", 0)) != attempt
        ):
            data_manager.releaseProcessingCapacity(token)


def processing_worker_has_stopped(job):
    """A failed dispatch response does not prove the cloud never started a pod."""
    if job.get("worker", {}).get("mode") != "kubernetes":
        return True
    from kubernetes import client, config
    from kubernetes.client.exceptions import ApiException

    config.load_incluster_config()
    try:
        worker = client.BatchV1Api().read_namespaced_job(
            name=job["worker"]["kubernetes_job_name"],
            namespace=os.environ["PROCESSING_JOB_NAMESPACE"],
            _request_timeout=30,
        )
    except ApiException as error:
        if error.status == 404:
            return True
        raise
    return any(
        condition.status == "True" and condition.type in ("Complete", "Failed")
        for condition in worker.status.conditions or []
    )
