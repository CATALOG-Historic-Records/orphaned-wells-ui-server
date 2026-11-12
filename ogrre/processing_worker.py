"""Command-line entry point for short-lived OGRRE processing worker Pods."""

import argparse
import logging
from dotenv import load_dotenv

_log = logging.getLogger(__name__)


def run_processing_job(job_id, attempt=0, data_manager=None, job_type="batch_document"):
    from ogrre.internal import batch_document_processing, export_job_runner
    from ogrre.internal.data_manager import DataManager

    if data_manager is None:
        data_manager = DataManager()
    if job_type == "export":
        return export_job_runner.run_export_job(
            job_id, attempt, data_manager=data_manager
        )
    job = data_manager.getProcessingJob(job_id)
    if job is None:
        raise ValueError(f"Processing job not found: {job_id}")
    if job.get("attempt", 0) != attempt:
        return
    if job.get("type") != "batch_document":
        raise ValueError(f"Unsupported processing job type: {job.get('type')}")
    data_manager.ensureDirectoryUploadRecords(job)
    return batch_document_processing.process_batch_document_job(
        job_id, data_manager, attempt
    )


def main():
    parser = argparse.ArgumentParser(description="Run an OGRRE worker job")
    parser.add_argument(
        "--job-id", required=True, help="Durable Mongo processing or export job id"
    )
    parser.add_argument(
        "--job-type",
        choices=["batch_document", "export"],
        default="batch_document",
        help="Worker job type to run",
    )
    parser.add_argument("--attempt", type=int, default=0)
    args = parser.parse_args()
    # Load local CLI configuration before importing runtime integrations.
    load_dotenv()
    _log.info("starting worker job_type=%s job_id=%s", args.job_type, args.job_id)
    run_processing_job(args.job_id, args.attempt, job_type=args.job_type)


if __name__ == "__main__":
    main()
