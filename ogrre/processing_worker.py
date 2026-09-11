"""Command-line entry point for short-lived OGRRE processing worker Pods."""

import argparse
import logging

from ogrre.internal import batch_document_processing
from ogrre.internal.data_manager import DataManager

_log = logging.getLogger(__name__)


def run_processing_job(job_id, attempt=0):
    data_manager = DataManager()
    job = data_manager.getProcessingJob(job_id)
    if job is None:
        raise ValueError(f"Processing job not found: {job_id}")
    if job.get("attempt", 0) != attempt:
        return
    if job.get("type") != "batch_document":
        raise ValueError(f"Unsupported processing job type: {job.get('type')}")
    return batch_document_processing.process_batch_document_job(
        job_id, data_manager, attempt
    )


def main():
    parser = argparse.ArgumentParser(description="Run an OGRRE processing job")
    parser.add_argument(
        "--job-id", required=True, help="Durable Mongo processing job id"
    )
    parser.add_argument("--attempt", type=int, default=0)
    args = parser.parse_args()
    _log.info("starting processing worker job_id=%s", args.job_id)
    run_processing_job(args.job_id, args.attempt)


if __name__ == "__main__":
    main()
