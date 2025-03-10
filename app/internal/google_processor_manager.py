import os
import sys
import logging

from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from google.api_core.exceptions import FailedPrecondition, InvalidArgument

try:
    if os.getenv("LOCATION") is None:
        os.environ["GCLOUD_PROJECT"]
except:
    from dotenv import load_dotenv

    # loading variables from .env file
    load_dotenv()


_log = logging.getLogger(__name__)

LOCATION = os.getenv("LOCATION")
PROJECT_ID = os.getenv("PROJECT_ID")
PROCESSOR_ID = os.getenv("PROCESSOR_ID")
PROCESSOR_VERSION_ID = os.getenv("PROCESSOR_VERSION_ID")
STORAGE_SERVICE_KEY = os.getenv("STORAGE_SERVICE_KEY")
BUCKET_NAME = os.getenv("STORAGE_BUCKET_NAME")
os.environ["GCLOUD_PROJECT"] = PROJECT_ID
DIRNAME, FILENAME = os.path.split(os.path.abspath(sys.argv[0]))

docai_client = documentai.DocumentProcessorServiceClient(
    client_options=ClientOptions(api_endpoint=f"{LOCATION}-documentai.googleapis.com"),
    # credentials=f"{DIRNAME}/creds.json"
)


def deploy_processor_version(RESOURCE_NAME, timeout=900, _Timeout=True):
    """
    This function is based off of the Google Documentation for making a DeployProcessorVersion request.
    The documentation can be found at https://cloud.google.com/document-ai/docs/manage-processor-versions#deploy

    The modification provides TimeoutError handeling.
    TimeoutError regularly occurs so to ensure the processor version is actually deployed
        this function is recursivly called once.
        The potential outcomes are:
            (Most Likely) An immediate error indicating the processor is already deployed
                which then causes this function return the string "DEPLOYED"
            A second attempt to deploy the processor version
                which is this also fails will log the error and return the error message
                from the recursive function call.

    Parameters
    ----------
    RESOURCE_NAME : string
        The string generated from docai_client.processor_version_path.
    timeout : int, optional
        The number of seconds for docai_client.deploy_processor_version to wait before raising a
            TimeoutError. The default timeout value for docai_client.deploy_processor_version
            is 900 seconds. The default is 900.
    _Timeout : bool, optional
        Intended for internal use limiting the recursive call to once. The default is True.

    Returns
    -------
    Error object or string
        Upon failure to deploy the processor version the error object is returned
        Upon success the string "DEPLOYED" is returned.
    """
    try:
        operation = docai_client.deploy_processor_version(
            name=RESOURCE_NAME, timeout=timeout
        )
        # Print operation details
        _log.info(f"Deployment operation Name: {operation.operation.name}")
        # Wait for operation to complete
        operation.result()
        return "DEPLOYED"
    # Deploy request will fail if the
    # processor version is already deployed
    except (FailedPrecondition, Exception) as e:
        if isinstance(e, TimeoutError):
            if _Timeout:
                e = deploy_processor_version(RESOURCE_NAME, _Timeout=False)
                if e == "DEPLOYED":
                    return e
            else:
                return e
        try:
            if e.metadata["current_state"] == "DEPLOYED":
                return "DEPLOYED"
            _log.error(f"Error Message: {e.message}")
            return e
        except:
            _log.error(f"Error Message: {e}")
            return e


def undeploy_processor_version(RESOURCE_NAME):
    """
    This function is from the Google Documentation for making an UndeployProcessorVersion request.
    The documentation can be found at https://cloud.google.com/document-ai/docs/manage-processor-versions#undeploy

    Parameters
    ----------
    RESOURCE_NAME : string
        The string generated from docai_client.processor_version_path.

    Returns
    -------
    None.

    """
    try:
        operation = docai_client.undeploy_processor_version(name=RESOURCE_NAME)
        # Print operation details
        _log.info(operation.operation.name)
        # Wait for operation to complete
        operation.result()
    # Undeploy request will fail if the
    # processor version is already undeployed
    # or if a request is made on a pretrained processor version
    except (FailedPrecondition, InvalidArgument) as e:
        _log.error(e.message)


if __name__ == "__main__":
    """
    Below I added an example for a potential implementation for deploying the processors
        at the start of image_hangeling.py,  process_image(). Additionally, consider where
        to add the undeploying function.

    Using docai_client.processor_version_path in the place of docai_client.processor_path
        is supposed to direct the document processing to the specified processor version
        without needing to set the default processor version, which can also be performed
        though the API if needed.
    Something to note is that the current default processor version cannot be identifed
        through the API. Not sure why but the capability was deliberately left out.
    """

    ## Document AI functions
    def process_image(
        file_name,
        mime_type,
        rg_id,
        record_id,
        processor_id,
        processor_attributes,
        data_manager,
        image_content,
        reprocessed=False,
        files_to_delete=[],
        processor_version_id=None,
    ):
        if processor_id is None:
            _log.info(
                f"processor id is none, rolling with default processor: {PROCESSOR_ID}"
            )
            processor_id = PROCESSOR_ID

        if processor_version_id is None:
            RESOURCE_NAME = docai_client.processor_path(
                PROJECT_ID, LOCATION, processor_id
            )
            # Which processor version is the default cannot be determined through API
            _log.info(
                "processor version id is none, rolling with default processor version."
            )
        else:
            RESOURCE_NAME = docai_client.processor_version_path(
                PROJECT_ID, LOCATION, processor_id, processor_version_id
            )
            deployment = deploy_processor_version(RESOURCE_NAME)
            if deployment != "DEPLOYED":
                _log.error(
                    "Failed to Deploy"
                    + f"Project: {PROJECT_ID}"
                    + f"Location: {LOCATION}"
                    + f"Processor: {processor_id}"
                    + f"Processor Version: {processor_version_id}"
                )
                raise deployment

        raw_document = documentai.RawDocument(
            content=image_content, mime_type=mime_type
        )
