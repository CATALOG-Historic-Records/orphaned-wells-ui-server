import os
import sys
import logging

from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from google.api_core.exceptions import FailedPrecondition, InvalidArgument


_log = logging.getLogger(__name__)

LOCATION = os.getenv("LOCATION")

docai_client = documentai.DocumentProcessorServiceClient(
    client_options=ClientOptions(api_endpoint=f"{LOCATION}-documentai.googleapis.com"),
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
