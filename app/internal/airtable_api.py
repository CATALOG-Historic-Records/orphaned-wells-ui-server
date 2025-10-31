import os
import pyairtable
from pyairtable import Api
from dotenv import load_dotenv
from app.internal.util import remap_airtable_keys

load_dotenv()

# AIRTABLE_API_TOKEN = os.environ["AIRTABLE_API_TOKEN"]
# AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
# AIRTABLE_PROCESSORS_TABLE_ID = os.environ["AIRTABLE_PROCESSORS_TABLE_ID"]
# AIRTABLE_PROCESSORS_TABLE_NAME = "Trained Models"


def get_airtable_base(AIRTABLE_API_TOKEN, AIRTABLE_BASE_ID):
    ## The api and base only need to be defined once, they will still detect changes made afterwards
    api = Api(AIRTABLE_API_TOKEN)
    airtable_base = api.base(AIRTABLE_BASE_ID)
    return airtable_base


def get_processor_name_and_id_from_row_id(
    airtable_base, record_id, processors_table_name="Processors"
):
    """Get processor name from linked record id.

    Args:
        airtable_base : airtable base object
        record_id : airtable processor row id
        processors_table_name : name of processors table in Airtable

    Returns:
        Processor name or None
    """
    tables = airtable_base.tables()
    for table in tables:
        if table.name == processors_table_name:
            linked_row = table.get(record_id)
            # print(f"linked row: {linked_row}")
            if linked_row:
                fields = linked_row.get("fields", {})
                processor_name = fields.get("Google Processor Name", None)
                processor_id = fields.get("Processor ID", None)
                # print(f"found processor: {processor_name}")
                return processor_name, processor_id
            else:
                print(f"unable to find {record_id} in processors table")
                return None, None

            break
    return None, None


def get_processor_list(airtable_base, models_table_name="Trained Models"):
    """Get list of extractors.

    Args:
        airtable_base : airtable base object
        models_table_name : name of models table in Airtable

    Returns:
        List of processors or None
    """
    extractors = []
    try:
        tables = airtable_base.tables()
        for table in tables:
            ## Each table has base, name, id
            ## table.all() returns table contents
            if table.name == models_table_name:
                table_contents = table.all()
                for row in table_contents:
                    row_fields = row.get("fields", {})
                    processor_type = row_fields.get("Processor Type", "").lower()
                    is_primary = (
                        row_fields.get("Primary Model in Processor", "").lower()
                        == "primary"
                    )
                    # if processor_type == "extractor" and is_primary:
                    if is_primary:
                        ## TODO: we have to convert processor name, it might be an id referencing another sheet
                        extractor_id = row_fields.get("Processor Name", None)
                        if (
                            extractor_id
                            and isinstance(extractor_id, list)
                            and len(extractor_id) > 0
                        ):
                            (
                                processor_name,
                                processor_id,
                            ) = get_processor_name_and_id_from_row_id(
                                airtable_base, extractor_id[0]
                            )
                            row_fields["Processor Name"] = processor_name
                            row_fields["Processor ID"] = processor_id
                        extractors.append(row_fields)
                break

    except Exception as e:
        print(f"unable to find processor list")
        return None
    return extractors


def get_table_data(airtable_base, table_name: str):
    attributes = []
    try:
        tables = airtable_base.tables()
        for table in tables:
            if table.name == table_name:
                table_contents = table.all()
                for row in table_contents:
                    row_fields = row.get("fields", {})
                    attributes.append(remap_airtable_keys(row_fields))
                break

    except Exception as e:
        print(f"unable to get attributes for: {table_name}")
        return None
    return attributes


def get_processor_by_id(airtable_base, processor_id: str):
    """Get processor data for given processor id.

    Args:
        processor_id: str

    Returns:
        Dict containing processor data, attributes or None
    """
    if not processor_id:
        return None

    ## TODO: we may already have the list loaded, in which case use that
    extractor_data = get_processor_list(airtable_base)

    if not extractor_data:
        return None

    processor_data = None
    for extractor in extractor_data:
        if extractor.get("Processor ID") == processor_id:
            processor_data = extractor
            break
    if not processor_data:
        print(f"unable to find processor data for id: {processor_id}")
        return None

    processor_name = processor_data.get("Processor Name")

    processor_data["attributes"] = get_table_data(airtable_base, processor_name)

    return processor_data


def get_processor_by_name(
    airtable_base,
    processor_name,
):
    """Get processor data for given processor name.

    Args:
        processor_name: str

    Returns:
        Dict containing processor data, attributes or None
    """
    if not processor_name:
        return None

    extractor_data = get_processor_list(airtable_base)

    if not extractor_data:
        return None

    processor_data = None
    for extractor in extractor_data:
        if extractor.get("Processor Name") == processor_name:
            processor_data = extractor
            break
    if not processor_data:
        print(f"unable to find processor data for: {processor_name}")
        return None

    processor_data["attributes"] = get_table_data(airtable_base, processor_name)

    return processor_data
