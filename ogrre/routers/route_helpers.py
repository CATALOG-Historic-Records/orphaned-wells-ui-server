from fastapi import HTTPException, Request

from ogrre.internal.data_manager import data_manager


def require_permission(user_info: dict, permission: str, detail: str):
    email = (user_info or {}).get("email")
    if not email or not data_manager.hasPermission(email, permission):
        raise HTTPException(403, detail=detail)


async def read_json_object(
    request: Request,
    *,
    required: bool = True,
    detail: str = "JSON request body must be an object.",
):
    try:
        data = await request.json()
    except Exception:
        if required:
            raise HTTPException(400, detail=detail)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HTTPException(400, detail=detail)
    return data


def get_filter_from_request_body(data: dict):
    filter_by = data.get("filter", {})
    if filter_by is None:
        return {}
    if not isinstance(filter_by, dict):
        raise HTTPException(400, detail="Filter must be an object.")
    return filter_by


def normalize_sort_by(sort_by):
    if not isinstance(sort_by, (list, tuple)) or len(sort_by) < 2:
        return ["dateCreated", 1]
    sort_field = sort_by[0] if isinstance(sort_by[0], str) else "dateCreated"
    sort_direction = sort_by[1] if sort_by[1] in (1, -1) else 1
    return [sort_field, sort_direction]


def fetch_records_for_location(
    *,
    user_info,
    location,
    _id,
    filter_by,
    sort_by,
    document_types=None,
    include_attribute_fields=None,
    forDownload=False,
):
    if document_types is None:
        document_types = []
    if not isinstance(document_types, list):
        raise HTTPException(400, detail="document_types must be a list.")

    if location == "project":
        if not data_manager.userCanAccessProject(_id, user_info):
            raise HTTPException(
                403,
                detail="You do not have access to this project, please contact the project creator to gain access.",
            )
        records, _ = data_manager.fetchRecordsByProject(
            user_info,
            _id,
            filter_by=filter_by,
            sort_by=sort_by,
            include_attribute_fields=include_attribute_fields,
            forDownload=forDownload,
        )
    elif location == "record_group":
        _, rg_data = data_manager.fetchRecordGroupData(_id, user_info)
        if rg_data is None:
            raise HTTPException(
                403,
                detail="You do not have access to this record group, please contact the project creator to gain access.",
            )
        records, _ = data_manager.fetchRecordsByRecordGroup(
            user_info,
            _id,
            filter_by=filter_by,
            sort_by=sort_by,
            include_attribute_fields=include_attribute_fields,
            forDownload=forDownload,
        )
    elif location == "team":
        records, _ = data_manager.fetchRecordsByTeam(
            user_info,
            filter_by=filter_by,
            sort_by=sort_by,
            include_attribute_fields=include_attribute_fields,
            forDownload=forDownload,
        )
    elif location == "documentType":
        if not data_manager.userCanAccessProject(_id, user_info):
            raise HTTPException(
                403,
                detail="You do not have access to this project, please contact the project creator to gain access.",
            )
        records, _ = data_manager.fetchRecordsByProjectAndDocumentTypes(
            user_info,
            _id,
            document_types,
            filter_by=filter_by,
            sort_by=sort_by,
            include_attribute_fields=include_attribute_fields,
            forDownload=forDownload,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Location must be project, record_group, documentType, or team",
        )
    return records
