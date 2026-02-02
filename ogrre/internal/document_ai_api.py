import base64
import logging
import os
import sys

from google.api_core.client_options import ClientOptions
from google.cloud import documentai
import requests

from ogrre.internal.google_processor_manager import (
    deploy_processor_version,
    undeploy_processor_version,
)

_log = logging.getLogger(__name__)

LOCATION = os.getenv("LOCATION")
PROJECT_ID = os.getenv("PROJECT_ID")
STORAGE_SERVICE_KEY = os.getenv("STORAGE_SERVICE_KEY")
DOCUMENT_AI_BACKEND = os.getenv("DOCUMENT_AI_BACKEND", "google").lower()
DOCUMENT_AI_URL = os.getenv("DOCUMENT_AI_URL")
DOCUMENT_AI_TIMEOUT = float(os.getenv("DOCUMENT_AI_TIMEOUT", "60"))


_client_options = None
if LOCATION:
    _client_options = ClientOptions(
        api_endpoint=f"{LOCATION}-documentai.googleapis.com"
    )

_docai_client = documentai.DocumentProcessorServiceClient(
    client_options=_client_options
)


def _get_coordinates(entity, attribute):
    try:
        bounding_poly = entity.page_anchor.page_refs[0].bounding_poly
        coordinates = []
        for i in range(4):
            coordinate = bounding_poly.normalized_vertices[i]
            coordinates.append([coordinate.x, coordinate.y])
    except Exception as e:
        coordinates = None
        _log.info(f"unable to get coordinates of attribute {attribute}: {e}")
    return coordinates


def _get_page(entity, attribute):
    try:
        page = entity.page_anchor.page_refs[0].page
    except Exception as e:
        page = None
        _log.info(f"unable to get coordinates of attribute {attribute}: {e}")
    return page


def process_document_content(
    image_content,
    mime_type,
    processor_id,
    model_id,
    using_default_processor=False,
):
    if DOCUMENT_AI_BACKEND != "google":
        return _process_document_content_custom(
            image_content=image_content,
            mime_type=mime_type,
            processor_id=processor_id,
            model_id=model_id,
            using_default_processor=using_default_processor,
        )

    resource_name = _docai_client.processor_version_path(
        PROJECT_ID, LOCATION, processor_id, model_id
    )

    raw_document = documentai.RawDocument(content=image_content, mime_type=mime_type)
    request = documentai.ProcessRequest(name=resource_name, raw_document=raw_document)
    result = _docai_client.process_document(request=request)
    document_object = result.document

    document_entities = document_object.entities
    if using_default_processor:
        if not document_entities:
            return []
        _log.info("generic processor, diving into properties")
        document_entities = document_entities[0].properties

    attributes_list = []

    for entity in document_entities:
        attribute = entity.type_
        text_value = entity.text_anchor.content
        normalized_value = entity.normalized_value.text
        confidence = entity.confidence
        raw_text = entity.mention_text
        if normalized_value:
            value = normalized_value
        else:
            value = raw_text
        coordinates = _get_coordinates(entity, attribute)

        page = _get_page(entity, attribute)
        subattributes_list = []
        for prop in entity.properties:
            sub_text_value = prop.text_anchor.content
            sub_normalized_value = prop.normalized_value.text
            sub_attribute = prop.type_
            sub_confidence = prop.confidence
            sub_raw_text = prop.mention_text
            sub_coordinates = _get_coordinates(prop, sub_attribute)
            sub_page = _get_page(prop, sub_attribute)
            if sub_normalized_value:
                sub_value = sub_normalized_value
            else:
                sub_value = sub_raw_text
            original_sub_attribute = sub_attribute

            new_subattribute = {
                "key": original_sub_attribute,
                "ai_confidence": confidence,
                "confidence": sub_confidence,
                "raw_text": sub_raw_text,
                "text_value": sub_text_value,
                "value": sub_value,
                "normalized_vertices": sub_coordinates,
                "normalized_value": sub_normalized_value,
                "isSubattribute": True,
                "topLevelAttribute": attribute,
                "edited": False,
                "page": sub_page,
            }
            subattributes_list.append(new_subattribute)

        if len(subattributes_list) == 0:
            subattributes_list = None

        new_attribute = {
            "key": attribute,
            "ai_confidence": confidence,
            "confidence": confidence,
            "raw_text": raw_text,
            "text_value": text_value,
            "value": value,
            "normalized_vertices": coordinates,
            "normalized_value": normalized_value,
            "subattributes": subattributes_list,
            "isSubattribute": False,
            "edited": False,
            "page": page,
        }
        attributes_list.append(new_attribute)

    return attributes_list


def _process_document_content_custom(
    image_content,
    mime_type,
    processor_id,
    model_id,
    using_default_processor=False,
):
    if not DOCUMENT_AI_URL:
        raise ValueError(
            "DOCUMENT_AI_URL is required when DOCUMENT_AI_BACKEND != google"
        )

    payload = {
        "image_content_base64": base64.b64encode(image_content).decode("utf-8"),
        "mime_type": mime_type,
        "processor_id": processor_id,
        "model_id": model_id,
        "using_default_processor": using_default_processor,
    }
    response = requests.post(DOCUMENT_AI_URL, json=payload, timeout=DOCUMENT_AI_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data
    attributes_list = data.get("attributes_list")
    if attributes_list is None:
        raise ValueError("custom document ai response missing attributes_list")
    return attributes_list


def deploy_processor(rg_id, data_manager):
    if DOCUMENT_AI_BACKEND != "google":
        _log.info("custom document ai backend selected; skipping deploy")
        return "DEPLOYED"
    processor_id, model_id, _ = data_manager.getProcessorByRecordGroupID(rg_id)

    resource_name = _docai_client.processor_version_path(
        PROJECT_ID, LOCATION, processor_id, model_id
    )

    _log.debug(f"attempting to deploy processor model: {model_id}")
    deployment = deploy_processor_version(resource_name)
    return deployment


def undeploy_processor(rg_id, data_manager):
    if DOCUMENT_AI_BACKEND != "google":
        _log.info("custom document ai backend selected; skipping undeploy")
        return True
    processor_id, model_id, _ = data_manager.getProcessorByRecordGroupID(rg_id)

    resource_name = _docai_client.processor_version_path(
        PROJECT_ID, LOCATION, processor_id, model_id
    )
    undeploy_processor_version(resource_name)
    return True


def check_if_processor_is_deployed(rg_id, data_manager):
    if DOCUMENT_AI_BACKEND != "google":
        _log.info("custom document ai backend selected; returning deployed")
        return 1
    processor_id, model_id, _ = data_manager.getProcessorByRecordGroupID(rg_id)

    opts = None
    if LOCATION:
        opts = ClientOptions(api_endpoint=f"{LOCATION}-documentai.googleapis.com")
    client = documentai.DocumentProcessorServiceClient(client_options=opts)
    parent = client.processor_path(PROJECT_ID, LOCATION, processor_id)

    processor_versions = client.list_processor_versions(parent=parent)
    for processor_version in processor_versions:
        processor_version_id = client.parse_processor_version_path(
            processor_version.name
        )["processor_version"]
        if processor_version_id == model_id:
            _log.debug(f"processor state == {processor_version.state}")
            return processor_version.state
    _log.error(f"unable to find model id: {model_id}")
    return 10
