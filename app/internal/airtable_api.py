import os
import pyairtable
from pyairtable import Api
from dotenv import load_dotenv

load_dotenv()

AIRTABLE_API_TOKEN = os.environ["AIRTABLE_API_TOKEN"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_PROCESSORS_TABLE_ID = os.environ["AIRTABLE_PROCESSORS_TABLE_ID"]
AIRTABLE_PROCESSORS_TABLE_NAME = "Trained Models"

## The api and base only need to be defined once, they will still detect changes made afterwards
api = Api(AIRTABLE_API_TOKEN)
airtable_base = api.base(AIRTABLE_BASE_ID)


def get_processor_list(
    processors_table_name="Trained Models", collaborator: str = "isgs"
):
    """Get list of extractors.

    Args:
        collaborator: eg. isgs

    Returns:
        List of processors or None
    """

    extractors = []
    try:
        tables = airtable_base.tables()
        for table in tables:
            ## Each table has base, name, id
            ## table.all() returns table contents
            if table.name == processors_table_name:
                table_contents = table.all()
                for row in table_contents:
                    row_fields = row.get("fields", {})
                    processor_type = row_fields.get("Processor Type", "").lower()
                    is_primary = (
                        row_fields.get("Primary Model in Processor", "").lower()
                        == "primary"
                    )
                    if processor_type == "extractor" and is_primary:
                        extractors.append(row_fields)
                break

    except Exception as e:
        print(f"unable to find processor list for collaborator: {collaborator}")
        return None
    return extractors


def get_table_data(table_name: str):
    attributes = []
    try:
        tables = airtable_base.tables()
        for table in tables:
            if table.name == table_name:
                table_contents = table.all()
                for row in table_contents:
                    row_fields = row.get("fields", {})
                    ## TODO: we have to convert field names
                    attributes.append(row_fields)
                break

    except Exception as e:
        print(f"unable to get attributes for: {table_name}")
        return None
    return attributes


def get_processor_by_id(processor_id: str, collaborator: str = "isgs"):
    """Get processor data for given processor id.

    Args:
        collaborator: str = isgs
        processor_id: str

    Returns:
        Dict containing processor data, attributes or None
    """
    if not processor_id:
        return None

    ## TODO: we may already have the list loaded, in which case use that
    extractor_data = get_processor_list()

    if not extractor_data:
        return None

    processor_data = None
    for extractor in extractor_data:
        if extractor.get("Processor ID") == processor_id:
            processor_data = extractor
            break
    if not processor_data:
        print(f"unable to find processor data for {collaborator} id: {processor_id}")
        return None

    processor_name = processor_data.get("Processor Name")

    processor_data["attributes"] = get_table_data(processor_name)

    return processor_data


def get_processor_by_name(
    processor_name,
    collaborator: str = "isgs",
):
    """Get processor data for given processor name.

    Args:
        collaborator: str = isgs
        processor_name: str

    Returns:
        Dict containing processor data, attributes or None
    """
    if not processor_name:
        return None

    extractor_data = get_processor_list()

    if not extractor_data:
        return None

    processor_data = None
    for extractor in extractor_data:
        if extractor.get("Processor Name") == processor_name:
            processor_data = extractor
            break
    if not processor_data:
        print(
            f"unable to find processor data for {collaborator} named: {processor_name}"
        )
        return None

    processor_data["attributes"] = get_table_data(processor_name)

    return processor_data
