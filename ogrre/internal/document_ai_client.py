import os
from pathlib import Path

from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from google.oauth2 import service_account


PACKAGE_DIR = Path(__file__).resolve().parents[1]
DOCUMENT_AI_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

_docai_client = None


def _resolve_document_ai_service_account_path():
    service_key = os.getenv("DOCUMENT_AI_SERVICE_KEY")
    if not service_key:
        return None

    service_path = Path(service_key).expanduser()
    if service_path.is_absolute():
        return str(service_path)
    return str(PACKAGE_DIR / service_path)


def _get_document_ai_credentials():
    service_path = _resolve_document_ai_service_account_path()
    if not service_path:
        return None
    return service_account.Credentials.from_service_account_file(
        service_path, scopes=DOCUMENT_AI_SCOPES
    )


def get_docai_client():
    global _docai_client
    if _docai_client is None:
        location = os.getenv("LOCATION", "us")
        client_options = None
        if location:
            client_options = ClientOptions(
                api_endpoint=f"{location}-documentai.googleapis.com"
            )

        credentials = _get_document_ai_credentials()
        client_kwargs = {"client_options": client_options}
        if credentials is not None:
            client_kwargs["credentials"] = credentials

        _docai_client = documentai.DocumentProcessorServiceClient(**client_kwargs)
    return _docai_client
