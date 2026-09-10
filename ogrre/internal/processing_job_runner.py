"""Dispatch durable OGRRE processing jobs to local or Kubernetes workers."""

import logging
import os

_log = logging.getLogger(__name__)


def _processing_job_mode():
    return os.getenv("PROCESSING_JOB_MODE", "background").strip().lower()


def _int_setting(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _processing_job_name(job_id):
    return f"ogrre-batch-{job_id[:24]}"


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


def _kubernetes_job_manifest(job_id, image):
    job_name = _processing_job_name(job_id)
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
                            "args": ["--job-id", job_id],
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
    if mode == "background":
        if background_tasks is None:
            raise RuntimeError("background_tasks is required in background job mode")
        from ogrre.processing_worker import run_processing_job

        data_manager.updateProcessingJob(
            job_id, {"status": "dispatched", "worker.mode": "background"}
        )
        background_tasks.add_task(run_processing_job, job_id)
        return data_manager.getProcessingJob(job_id)

    if mode != "kubernetes":
        raise ValueError(
            "PROCESSING_JOB_MODE must be 'background' for local development or "
            "'kubernetes' for GKE deployments"
        )

    namespace = os.getenv("PROCESSING_JOB_NAMESPACE")
    image = os.getenv("PROCESSING_JOB_IMAGE") or os.getenv("OGRRE_BACKEND_IMAGE")
    if not namespace or not image:
        raise RuntimeError(
            "PROCESSING_JOB_NAMESPACE and PROCESSING_JOB_IMAGE are required in "
            "kubernetes processing job mode"
        )

    job_name = _processing_job_name(job_id)
    data_manager.updateProcessingJob(
        job_id,
        {
            "status": "dispatched",
            "worker.mode": "kubernetes",
            "worker.kubernetes_job_name": job_name,
            "worker.image": image,
        },
    )
    try:
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException

        config.load_incluster_config()
        api = client.BatchV1Api()
        try:
            api.create_namespaced_job(
                namespace=namespace, body=_kubernetes_job_manifest(job_id, image)
            )
        except ApiException as error:
            if error.status != 409:
                raise
            _log.info(
                "processing Job %s already exists in namespace %s", job_name, namespace
            )
    except Exception as error:
        _log.exception("unable to dispatch Kubernetes processing job %s", job_id)
        data_manager.completeProcessingJob(job_id, "error", error=error)
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
            name=job_name, namespace=namespace
        )
        for condition in kubernetes_job.status.conditions or []:
            if condition.type == "Failed" and condition.status == "True":
                reason = (
                    condition.message or condition.reason or "Kubernetes worker failed"
                )
                return data_manager.completeProcessingJob(job_id, "error", error=reason)
            if condition.type == "Complete" and condition.status == "True":
                return data_manager.completeProcessingJob(
                    job_id,
                    "error",
                    error="Kubernetes worker exited without recording a final job status",
                )
    except ApiException as error:
        if error.status == 404:
            return data_manager.completeProcessingJob(
                job_id,
                "error",
                error="Kubernetes worker Job was not found",
            )
        _log.warning("unable to reconcile processing Job %s: %s", job_id, error)
    except Exception as error:
        _log.warning("unable to reconcile processing Job %s: %s", job_id, error)
    return data_manager.getProcessingJob(job_id)
