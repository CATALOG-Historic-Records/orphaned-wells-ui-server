import os
import logging
import aiofiles
import requests
import time
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from fastapi import (
    Request,
    APIRouter,
    HTTPException,
    File,
    UploadFile,
    BackgroundTasks,
    Depends,
)
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import OAuth2PasswordBearer

from ogrre.internal.data_manager import data_manager
from ogrre.internal.image_handling import (
    process_document,
    process_zip,
    deployProcessor,
    undeployProcessor,
    check_if_processor_is_deployed,
)
import ogrre.internal.util as util
import ogrre.internal.auth as auth

_log = logging.getLogger(__name__)

token_uri, client_id, client_secret = auth.get_google_credentials()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

router = APIRouter(
    prefix="",
    tags=["uow"],
    responses={404: {"description": "route not found"}},
)


@router.post("/token")
async def authenticate(token: str = Depends(oauth2_scheme)):
    """Authenticate API calls; required as a dependency for other API calls.

    Args:
        id_token: token provided upon signin

    Returns:
        user account information
    """
    try:
        user_info = id_token.verify_oauth2_token(
            token, google_requests.Request(), client_id
        )
        user_info["email"] = user_info.get("email", "").lower()
        return user_info
    except Exception as e:
        _log.info(f"unable to authenticate: {e}")
        raise HTTPException(status_code=401, detail=f"unable to authenticate: {e}")


@router.post("/auth_login")
async def auth_login(request: Request):
    """Authorize OGRRE account.

    Args:
        code: code provided by react google sign in

    Returns:
        user tokens (id_token, access_token, refresh_token)
    """
    code = await request.json()
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": "postmessage",
        "grant_type": "authorization_code",
    }
    response = requests.post(token_uri, data=data)
    user_tokens = response.json()
    try:
        user_info = id_token.verify_oauth2_token(
            user_tokens["id_token"], google_requests.Request(), client_id
        )
        user_info["email"] = user_info.get("email", "").lower()
    except Exception as e:  # should probably specify exception type
        _log.info(f"unable to authenticate on 1st try: {e}")
        _log.info(f"waiting 2 seconds")
        try:
            time.sleep(2.5)
            user_info = id_token.verify_oauth2_token(
                user_tokens["id_token"], google_requests.Request(), client_id
            )
            user_info["email"] = user_info.get("email", "").lower()
        except Exception as e:  # should probably specify exception type
            _log.info(f"unable to authenticate: {e}")
            raise HTTPException(status_code=401, detail=f"unable to authenticate: {e}")

    email = user_info["email"]
    user = data_manager.getUser(email)
    if user is None:
        _log.info(f"user {email} is not found in database")
        data_manager.recordHistory("login", email, notes="denied access")
        raise HTTPException(status_code=403, detail=user_info)

    authorized = util.validateUser(user)
    if not authorized:
        _log.info(f"user is not authorized")
        data_manager.recordHistory("login", email, notes="denied access")
        raise HTTPException(status_code=403, detail=user_info)
    data_manager.recordHistory("login", email)
    data_manager.updateUserObject(user_info)
    return user_tokens


@router.post("/auth_refresh")
async def auth_refresh(request: Request):
    """Refresh user tokens.

    Args:
        refresh_token: refresh token provided upon signin

    Returns:
        user tokens (id_token, access_token, refresh_token)
    """
    refresh_token = await request.json()
    data = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    }
    response = requests.post(token_uri, data=data)
    user_tokens = response.json()
    try:
        user_info = id_token.verify_oauth2_token(
            user_tokens["id_token"], google_requests.Request(), client_id
        )
    except Exception as e:
        _log.info(f"unable to authenticate: {e}")
        raise HTTPException(status_code=401, detail=f"unable to authenticate: {e}")
    email = user_info["email"]
    user = data_manager.getUser(email)
    if user is None:
        _log.info(f"user {email} is not found in database")
        data_manager.recordHistory("refresh", email, notes="denied access")
        raise HTTPException(status_code=403, detail=user_info)
    authorized = util.validateUser(user)
    if not authorized:
        _log.info(f"user is not authorized")
        data_manager.recordHistory("refresh", email, notes="denied access")
        raise HTTPException(status_code=403, detail=user_info)
    data_manager.recordHistory("refresh", email)
    return user_tokens


@router.post("/check_auth")
async def check_authorization(user_info: dict = Depends(authenticate)):
    """Ensure user is authorized.

    Args:
        id_token: token provided upon signin

    Returns:
        user account information
    """
    email = user_info["email"]
    db = os.environ.get("DB_CONNECTION", None)
    if "staging" in db:
        environment = "staging"
    else:
        environment = os.environ.get("ENVIRONMENT", None)
    user = data_manager.getUser(email)
    return {"user_data": user, "environment": environment}


@router.post("/logout")
async def logout(request: Request):
    """Log user out, revoke refresh token.

    Args:
        refresh_token: refresh token provided upon signin

    Returns:
        response code
    """
    refresh_token = await request.json()
    response = requests.post(
        "https://oauth2.googleapis.com/revoke",
        params={"token": refresh_token},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    return {"logout_status": response.status_code}


@router.get("/get_projects", response_model=list)
async def get_projects(user_info: dict = Depends(authenticate)):
    """Fetch all projects that a user has access to.

    Returns:
        List containing projects and metadata
    """
    resp = data_manager.fetchProjects(user_info.get("email", ""))
    return resp


@router.get("/get_record_groups/{project_id}", response_model=dict)
async def get_record_groups(project_id: str, user_info: dict = Depends(authenticate)):
    """Fetch all record groups are in a project.

    Returns:
        List containing record groups and metadata
    """
    resp = data_manager.fetchRecordGroups(project_id, user_info.get("email", ""))
    return resp


@router.post("/get_records/{get_by}", response_model=dict)
async def get_records(
    request: Request,
    get_by: str,
    page: int = None,
    records_per_page: int = None,
    user_info: dict = Depends(authenticate),
):
    """Fetch records for a given query.

    Args:
        request.query: DB Query

    Returns:
        List of records
    """
    data = await request.json()
    if get_by == "project" or get_by == "record_group":
        sort_by = data.get(
            "sort", ["dateCreated", 1]
        )  ## 1 is ascending, -1 is descending`
        if sort_by[1] != 1 and sort_by[1] != -1:
            sort_by[1] = 1
        filter_by = data.get("filter", {})
        if get_by == "project":
            project_id = data.get("id", None)
            if project_id is not None:
                records, record_count = data_manager.fetchRecordsByProject(
                    user_info,
                    project_id,
                    page,
                    records_per_page,
                    sort_by,
                    filter_by,
                )
                return {"records": records, "record_count": record_count}
        elif get_by == "record_group":
            rg_id = data.get("id", None)
            if rg_id is not None:
                records, record_count = data_manager.fetchRecordsByRecordGroup(
                    user_info, rg_id, page, records_per_page, sort_by, filter_by
                )
                return {"records": records, "record_count": record_count}
    elif get_by == "team":
        sort_by = data.get(
            "sort", ["dateCreated", 1]
        )  ## 1 is ascending, -1 is descending`
        if sort_by[1] != 1 and sort_by[1] != -1:
            sort_by[1] = 1
        filter_by = data.get("filter", {})
        records, record_count = data_manager.fetchRecordsByTeam(
            user_info,
            page,
            records_per_page,
            sort_by,
            filter_by,
        )
        return {"records": records, "record_count": record_count}

    _log.error(f"unable to process record query")
    raise HTTPException(400, detail=f"unable to process record query")


@router.get("/get_processors", response_model=dict)
async def get_processors(user_info: dict = Depends(authenticate)):
    """Fetch all processors for a given state/organization.

    Returns:
        List containing processors and metadata
    """
    resp = data_manager.fetchProcessors(user_info.get("email", ""))
    return resp


@router.get("/get_record_group/{rg_id}")
async def get_record_group_data(
    rg_id: str,
    user_info: dict = Depends(authenticate),
):
    """Fetch record group data.

    Args:
        rg_id: Document group identifier

    Returns:
        Dictionary containing record group data, list of records
    """
    project_document, rg_data = data_manager.fetchRecordGroupData(
        rg_id, user_info.get("email", "")
    )
    if rg_data is None:
        raise HTTPException(
            403,
            detail=f"You do not have access to this project, please contact the project creator to gain access.",
        )
    return {
        "rg_data": rg_data,
        "project": project_document,
    }


@router.post("/get_record/{record_id}")
async def get_record_data(
    request: Request,
    record_id: str,
    background_tasks: BackgroundTasks,
    user_info: dict = Depends(authenticate),
):
    """Fetch document record data.

    Args:
        record_id: Record identifier

    Returns:
        List containing record data
    """
    try:
        data = await request.json()
        location = data.get("location")
        group_id = data.get("group_id")
        filterBy = data.get("filterBy")
        sortBy = data.get("sortBy")
        if location and group_id:
            page_state = {
                "location": location,
                "group_id": group_id,
                "filterBy": filterBy,
                "sortBy": sortBy,
            }
        else:
            page_state = None
    except Exception as e:
        _log.info(f"unable to get page state: {e}")
        page_state = None
    record, is_locked = data_manager.fetchRecordData(
        record_id, user_info, page_state=page_state, background_tasks=background_tasks
    )
    if record is None:
        raise HTTPException(
            403,
            detail=f"You do not have access to this record, please contact the project creator to gain access.",
        )

    ## get record schema
    _, _, processor_attributes = data_manager.getProcessorByRecordGroupID(
        record["rg_id"]
    )
    processor_attributes = util.convert_processor_attributes_to_dict(
        processor_attributes
    )

    ## lock record if it is awaiting verification and user does not have permission to verify
    verification_status = record.get("verification_status", None)
    if (
        verification_status == "required" or verification_status == "verified"
    ) and not is_locked:
        if not data_manager.hasPermission(user_info["email"], "verify_record"):
            if verification_status == "required":
                lockedMessage = "This record is awaiting verification by a team lead."
            else:
                lockedMessage = f"This record has been verified as {record.get('review_status')}, and can only be edited by a team lead."
            return JSONResponse(
                status_code=303,
                content={
                    "direction": "next",
                    "recordData": record,
                    "lockedMessage": lockedMessage,
                    "recordSchema": processor_attributes,
                },
            )
    if is_locked:
        return JSONResponse(
            status_code=303,
            content={
                "direction": "next",
                "recordData": record,
                "lockedMessage": "This record is currently being reviewed by a team member.",
                "recordSchema": processor_attributes,
            },
        )
    return {"recordData": record, "recordSchema": processor_attributes}


@router.get("/get_record_notes/{record_id}")
async def get_record_notes(record_id: str, user_info: dict = Depends(authenticate)):
    """Fetch record notes.

    Args:
        record_id: Record identifier

    Returns:
        List containing record notes
    """
    record_notes = data_manager.fetchRecordNotes(record_id, user_info)

    return record_notes


@router.get("/get_processor_data/{google_id}", response_model=dict)
async def get_processor_data(google_id: str, user_info: dict = Depends(authenticate)):
    """Fetch processor data for provided id.

    Returns:
        Dictionary containing processor data
    """
    resp = data_manager.getProcessorByGoogleId(google_id)
    return resp


@router.get("/get_column_data/{location}/{_id}", response_model=dict or None)
async def get_column_data(
    location: str, _id: str, user_info: dict = Depends(authenticate)
):
    """Fetch processor data for provided id.

    Returns:
        Dictionary containing processor data
    """
    resp = data_manager.fetchColumnData(location, _id)
    return resp


@router.get("/get_team_info")
async def get_team_info(user_info: dict = Depends(authenticate)):
    """Get user's team information

    Returns:
        Dictionary containing team information
    """
    resp = data_manager.fetchTeamInfo(user_info["email"])
    return resp


@router.post("/add_project")
async def add_project(request: Request, user_info: dict = Depends(authenticate)):
    """Add new project.

    Args:
        Request body
            data: Project data

    Returns:
        New project id
    """
    if not data_manager.hasPermission(user_info["email"], "create_project"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to create projects for this team. Please contact a team lead.",
        )
    data = await request.json()
    new_id = data_manager.createProject(data, user_info)
    return new_id


@router.post("/add_record_group")
async def add_record_group(request: Request, user_info: dict = Depends(authenticate)):
    """Add new record group.

    Args:
        Request body
            data: Document group data

    Returns:
        New project id
    """
    if not data_manager.hasPermission(user_info["email"], "create_record_group"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to create record groups for this team. Please contact a team lead.",
        )
    data = await request.json()
    new_id = data_manager.createRecordGroup(data, user_info)
    return new_id


@router.post("/upload_document/{rg_id}/{user_email}")
async def upload_document(
    rg_id: str,
    user_email: str,
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    reprocessed: bool = False,
    preventDuplicates: bool = False,
    run_cleaning_functions: bool = True,
    undeployProcessor: bool = True,
):
    """Upload document for processing. Documents are processed asynchronously.

    Args:
        rg_id: Record group identifier to be associated with this document
        file: Document file

    Returns:
        New document record identifier.
    """
    user_email = user_email.lower()
    if not data_manager.hasPermission(user_email, "upload_document"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to upload records for this project. Please contact a team lead or project manager.",
        )
    if preventDuplicates:
        record_exists = data_manager.checkIfRecordExists(file.filename, rg_id)
        if record_exists:
            return JSONResponse(
                status_code=208,
                content={"message": f"{file.filename} exists for {rg_id}, returning"},
            )

    user_info = data_manager.getUserInfo(user_email)
    project_is_valid = data_manager.checkRecordGroupValidity(rg_id)
    if not project_is_valid:
        raise HTTPException(404, detail=f"Project not found")
    filename, file_ext = os.path.splitext(file.filename)

    if file_ext.lower() == ".zip":
        backend_url = str(request.base_url)
        output_dir = f"{data_manager.app_settings.img_dir}"
        return process_zip(
            rg_id, user_info, background_tasks, file, output_dir, filename, backend_url
        )

    else:
        original_output_path = f"{data_manager.app_settings.img_dir}/{file.filename}"
        mime_type = file.content_type
        ## read document file
        try:
            async with aiofiles.open(original_output_path, "wb") as out_file:
                content = await file.read()  # async read
                await out_file.write(content)
            return process_document(
                rg_id,
                user_info,
                background_tasks,
                original_output_path,
                file_ext,
                filename,
                data_manager,
                mime_type,
                content,
                reprocessed=reprocessed,
                run_cleaning_functions=run_cleaning_functions,
                undeployProcessor=undeployProcessor,
            )
        except Exception as e:
            _log.error(f"unable to read image file: {e}")
            raise HTTPException(400, detail=f"Unable to process image file: {e}")


@router.post("/deploy_processor/{rg_id}")
async def deploy_processor(
    rg_id: str,
    background_tasks: BackgroundTasks,
    user_info: dict = Depends(authenticate),
):
    """Deploy processor model.

    Args:
        rg_id: Record group identifier

    Returns:
        Boolean indicating success or not
    """
    user_email = user_info["email"]
    if not data_manager.hasPermission(user_email, "upload_document"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to deploy processors. Please contact a team lead or project manager.",
        )
    try:
        background_tasks.add_task(
            deployProcessor, rg_id=rg_id, data_manager=data_manager
        )
        return 2
    except Exception as e:
        _log.error(f"unable to deploy processor: {e}")
        return 3


@router.post("/undeploy_processor/{rg_id}")
async def undeploy_processor(rg_id: str, user_info: dict = Depends(authenticate)):
    """Undeploy processor model.

    Args:
        rg_id: Record group identifier

    Returns:
        Boolean indicating success or not
    """
    user_email = user_info["email"]
    if not data_manager.hasPermission(user_email, "upload_document"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to deploy processors. Please contact a team lead or project manager.",
        )
    try:
        undeployed = undeployProcessor(rg_id, data_manager)
        if undeployed:
            return 3
        else:
            return 10
    except Exception as e:
        _log.error(f"unable to undeploy processor: {e}")
        return 10


@router.get("/check_processor_status/{rg_id}")
async def check_processor_status(rg_id: str):
    """Check status of processor model.

    Args:
        rg_id: Record group identifier

    Returns:
        Boolean indicating deployed or not
    """
    try:
        return check_if_processor_is_deployed(rg_id, data_manager)
    except Exception as e:
        _log.error(f"unable to undeploy processor: {e}")
        return 10


@router.post("/update_project/{project_id}")
async def update_project(
    project_id: str, request: Request, user_info: dict = Depends(authenticate)
):
    """Update project data.

    Args:
        project_id: Project identifier
        request body:
            data: New data for provided project

    Returns:
        Success response
    """
    if not data_manager.hasPermission(user_info["email"], "manage_project"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to update projects. Please contact a team lead or project manager.",
        )
    data = await request.json()
    return data_manager.updateProject(project_id, data, user_info)


@router.post("/update_record_group/{rg_id}")
async def update_record_group(
    rg_id: str, request: Request, user_info: dict = Depends(authenticate)
):
    """Update record group data.

    Args:
        rg_id: Project identifier
        request body:
            data: New data for provided project

    Returns:
        Success response
    """
    if not data_manager.hasPermission(user_info["email"], "manage_project"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to update projects. Please contact a team lead or project manager.",
        )
    data = await request.json()
    return data_manager.updateRecordGroup(rg_id, data, user_info)


@router.post("/update_record/{record_id}")
async def update_record(
    record_id: str, request: Request, user_info: dict = Depends(authenticate)
):
    """Update record data.

    Args:
        record_id: Record identifier
        request body:
            data: New data for provided record

    Returns:
        Success response
    """
    if not data_manager.hasPermission(user_info["email"], "review_record"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to review records. Please contact a team lead or project manager.",
        )
    req = await request.json()
    data = req.get("data", None)
    update_type = req.get("type", None)
    field_to_clean = req.get("fieldToClean", None)
    if update_type == "record_notes":
        update = data_manager.updateRecordNotes(record_id, data, user_info)
    else:
        update = data_manager.updateRecord(
            record_id,
            data,
            update_type,
            field_to_clean,
            user_info,
            calling_function="update_record",
        )
    if not update:
        raise HTTPException(status_code=403, detail=f"Record is locked by another user")

    return update


@router.post("/delete_processor/{processor_name}")
async def delete_processor(
    processor_name: str,
    user_info: dict = Depends(authenticate),
):
    """Delete processor.

    Args:
        processor_id: google processor id
        model_id: model id

    Returns:
        Delete query
    """
    if not data_manager.hasPermission(user_info["email"], "delete"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to delete processors. Please contact a team lead or project manager.",
        )
    if not processor_name:
        raise HTTPException(
            400,
            detail=f"Please provide processor name.",
        )
    return data_manager.deleteProcessorSchema(
        processorName=processor_name, user_info=user_info
    )


@router.post("/delete_project/{project_id}")
async def delete_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    user_info: dict = Depends(authenticate),
):
    """Delete project.

    Args:
        project_id: Project identifier

    Returns:
        Success response
    """
    if not data_manager.hasPermission(user_info["email"], "delete"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to delete projects. Please contact a team lead or project manager.",
        )
    data_manager.deleteProject(project_id, background_tasks, user_info)

    return {"response": "success"}


@router.post("/delete_record_group/{rg_id}")
async def delete_record_group(
    rg_id: str,
    background_tasks: BackgroundTasks,
    user_info: dict = Depends(authenticate),
):
    """Delete record group.

    Args:
        rg_id: record group identifier

    Returns:
        Success response
    """
    if not data_manager.hasPermission(user_info["email"], "delete"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to delete record groups. Please contact a team lead or project manager.",
        )
    data_manager.deleteRecordGroup(rg_id, background_tasks, user_info)
    return {"response": "success"}


@router.post("/delete_record/{record_id}")
async def delete_record(record_id: str, user_info: dict = Depends(authenticate)):
    """Delete record.

    Args:
        record_id: Record identifier

    Returns:
        Success response
    """
    if not data_manager.hasPermission(user_info["email"], "delete"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to delete records. Please contact a team lead or project manager.",
        )
    data_manager.deleteRecord(record_id, user_info)

    return {"response": "success"}


@router.post("/check_if_records_exist/{rg_id}")
async def check_if_records_exist(
    request: Request, rg_id: str, user_info: dict = Depends(authenticate)
):
    """Check if records exist for a given list of files.

    Args:
        file_list: List of file names
        rg_id: record group id

    Returns:
        JSON with duplicate_records
    """
    req = await request.json()
    file_list = req.get("file_list", [])
    return data_manager.checkIfRecordsExist(file_list, rg_id)


@router.post("/get_download_size/{location}/{_id}")
async def get_download_size(
    location: str,
    _id: str,
    request: Request,
    user_info: dict = Depends(authenticate),
):
    """Download records for given project ID.

    Args:
        location: one of: team, project, record_group
        _id: id of team, project or record group
        request body:
            exportType: type of export (csv or json)
            columns: list attributes to export

    Returns:
        Some combination of
            JSON file containing all or subset of record data for provided location
            CSV file containing all or subset of record data for provided location
            All document images for provided location
    """
    req = await request.json()

    filter_by = req.get("filter", {})
    sort_by = req.get("sort", ["dateCreated", 1])

    json_fields_to_include = {
        "topLevelFields": ["name", "filename", "image_files", "record_group_id"],
        "attributesList": ["key", "value", "normalized_vertices", "subattributes"],
    }

    if location == "project":
        records, _ = data_manager.fetchRecordsByProject(
            user_info,
            _id,
            filter_by=filter_by,
            sort_by=sort_by,
            include_attribute_fields=json_fields_to_include,
            forDownload=True,
        )
    elif location == "record_group":
        records, _ = data_manager.fetchRecordsByRecordGroup(
            user_info,
            _id,
            filter_by=filter_by,
            sort_by=sort_by,
            include_attribute_fields=json_fields_to_include,
            forDownload=True,
        )
    elif location == "team":
        records, _ = data_manager.fetchRecordsByTeam(
            user_info,
            filter_by=filter_by,
            sort_by=sort_by,
            include_attribute_fields=json_fields_to_include,
            forDownload=True,
        )
    else:
        raise HTTPException(
            status_code=400, detail=f"Location must be project, record_group, or team"
        )

    try:
        documents = util.compileDocumentImageList(records)
        gcs_paths = util.generate_gcs_paths(documents)
        totalBytes = util.compute_total_size([], gcs_paths.keys())
        return totalBytes

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {e}")


@router.post("/download_records/{location}/{_id}", response_class=StreamingResponse)
async def download_records(
    location: str,
    _id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    export_csv: bool = True,
    export_json: bool = False,
    export_images: bool = False,
    output_name: str = None,
    user_info: dict = Depends(authenticate),
):
    """Download records for given project ID.

    Args:
        location: one of: team, project, record_group
        _id: id of team, project or record group
        request body:
            exportType: type of export (csv or json)
            columns: list attributes to export

    Returns:
        Some combination of
            JSON file containing all or subset of record data for provided location
            CSV file containing all or subset of record data for provided location
            All document images for provided location
    """
    req = await request.json()

    request_origin = request.headers.get("origin")
    # _log.info(req)
    selectedColumns = req.get("columns", [])

    filter_by = req.get("filter", {})
    sort_by = req.get("sort", ["dateCreated", 1])

    json_fields_to_include = {
        "topLevelFields": ["name", "filename", "image_files", "record_group_id"],
        "attributesList": ["key", "value", "normalized_vertices", "subattributes"],
    }

    output_file_id = util.last4_before_decimal()

    keep_all_columns = False
    if len(selectedColumns) == 0:
        keep_all_columns = True

    if location == "project":
        records, _ = data_manager.fetchRecordsByProject(
            user_info,
            _id,
            filter_by=filter_by,
            sort_by=sort_by,
            include_attribute_fields=json_fields_to_include,
            forDownload=True,
        )
    elif location == "record_group":
        records, _ = data_manager.fetchRecordsByRecordGroup(
            user_info,
            _id,
            filter_by=filter_by,
            sort_by=sort_by,
            include_attribute_fields=json_fields_to_include,
            forDownload=True,
        )
    elif location == "team":
        records, _ = data_manager.fetchRecordsByTeam(
            user_info,
            filter_by=filter_by,
            sort_by=sort_by,
            include_attribute_fields=json_fields_to_include,
            forDownload=True,
        )
    else:
        raise HTTPException(
            status_code=400, detail=f"Location must be project, record_group, or team"
        )
    try:
        filepaths = []
        if export_csv:
            csv_file = data_manager.downloadRecords(
                records,
                "csv",
                user_info,
                _id,
                location,
                selectedColumns=selectedColumns,
                keep_all_columns=keep_all_columns,
                output_filename=f"{output_name}_{output_file_id}",
                request_origin=request_origin,
            )
            filepaths.append(csv_file)
        if export_json:
            json_file = data_manager.downloadRecords(
                records,
                "json",
                user_info,
                _id,
                location,
                selectedColumns=selectedColumns,
                keep_all_columns=keep_all_columns,
                output_filename=f"{output_name}_{output_file_id}",
            )
            filepaths.append(json_file)
        if export_images:
            documents = util.compileDocumentImageList(records)
        else:
            documents = []
        ## TODO: make this file name more unique, so multiple downloads dont have the same name
        download_log_file = f"zip_log_{output_file_id}.txt"
        z = util.zip_files_stream(filepaths, documents, log_to_file=download_log_file)

        ## remove file after 60 seconds to allow for the user download to finish
        filepaths.append(download_log_file)
        background_tasks.add_task(util.deleteFiles, filepaths=filepaths, sleep_time=60)
        headers = {"Content-Disposition": "attachment; filename=records.zip"}
        # _log.info(f"returning streaming response")
        return StreamingResponse(z, media_type="application/zip", headers=headers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {e}")


@router.get("/get_users")
async def get_users(user_info: dict = Depends(authenticate)):
    """Fetch all users from DB for a given user's team.

    Returns:
        List of users, role types
    """
    users = data_manager.getUsers(user_info)
    return users


@router.post("/run_cleaning_functions/{location}/{_id}")
async def run_cleaning_functions(
    location: str, _id: str, user_info: dict = Depends(authenticate)
):
    """Run cleaning functions on project (not supported yet), record group, or recorde.

    Args:
        location: project (not supported yet), record_group, or record
        _id: the _id of the collection to run the cleaning functions on

    """
    if not data_manager.hasPermission(user_info["email"], "manage_system"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to run cleaning functions. Please contact a team lead or project manager.",
        )

    data_manager.cleanCollection(location, _id)

    return _id


## admin functions
@router.post("/add_user/{email}")
async def add_user(
    request: Request, email: str, user_info: dict = Depends(authenticate)
):
    """Add user to application database

    Args:
        email: User email address

    Returns:
        user status
    """
    req = await request.json()
    team_lead = req.get("team_lead", False)
    sys_admin = req.get("sys_admin", False)
    email = email.lower().replace(" ", "")
    if data_manager.hasPermission(user_info["email"], "add_user"):
        admin_document = data_manager.getDocument(
            "users", {"email": user_info.get("email", "")}
        )
        team = admin_document.get("default_team", None)

        ## check if this user exists already. if not add to database
        new_user = data_manager.getUser(email)
        if new_user is None:
            resp = data_manager.addUser({"email": email}, team, team_lead, sys_admin)

        else:
            ## this user exists already. add them to this team
            new_user_team = new_user["default_team"]
            if new_user_team == team:
                _log.info(f"{email} is already on team {team}")
                raise HTTPException(
                    status_code=406, detail=f"This user is already on this team."
                )
            else:
                ## in this case, just add user to team without creating new user
                resp = data_manager.addUserToTeam(email, team)
                return resp
    else:
        raise HTTPException(
            status_code=403, detail=f"User is not authorized to perform this operation"
        )


@router.post("/update_user_roles")
async def update_user_roles(request: Request, user_info: dict = Depends(authenticate)):
    """Update roles for a user

    Args:
        role_category: category of role (team, project, system)
        new_role: new list of roles
        email: User email address

    Returns:
        result
    """
    if not data_manager.hasPermission(user_info["email"], "manage_team"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to manage team roles. Please contact a team lead or project manager.",
        )

    req = await request.json()
    role_category = req.get("role_category", None)
    new_roles = req.get("new_roles", None)
    email = req.get("email", None)
    team = data_manager.getUserInfo(user_info["email"])["default_team"]
    if new_roles is not None and role_category and email:
        data_manager.updateUserRole(email, team, role_category, new_roles)
        return email
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Please provide an update and an email in the request body",
        )


@router.get("/get_schema")
async def get_schema(user_info: dict = Depends(authenticate)):
    """Get schema

    Args:

    Returns:
        schema: dict
    """
    if not data_manager.hasPermission(user_info["email"], "manage_schema"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to manage schema. Please contact a team lead or project manager.",
        )
    return data_manager.getSchema(user_info)


@router.post("/upload_processor_schema")
async def upload_processor_schema(
    name: str = None,
    displayName: str = None,
    processorId: str = None,
    modelId: str = None,
    documentType: str = None,
    img: str = None,
    file: UploadFile = File(...),
    user_info: dict = Depends(authenticate),
):
    """Upload Schema

    Args:
        name: processor name,
        displayName: display name for processor,
        processorId: google processor id,
        modelId: google model id for primary model,
        documentType: type of document,
        csv_file

    Returns:
        schema: dict
    """
    if not data_manager.hasPermission(user_info["email"], "manage_schema"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to manage schema. Please contact a team lead or project manager.",
        )
    if (
        not name
        or not displayName
        or not processorId
        or not modelId
        or not documentType
    ):
        raise HTTPException(
            400,
            detail=f"Please provide each of the following as query parameters: name, displayName, processorId, modelId, documentType.",
        )
    schema_meta = {
        "name": name,
        "displayName": displayName,
        "processorId": processorId,
        "modelId": modelId,
        "documentType": documentType,
        "img": img,
    }
    return data_manager.uploadProcessorSchema(
        file=file, schema_meta=schema_meta, user_info=user_info
    )


@router.post("/upload_sample_image/{processor_name}")
async def upload_sample_image(
    processor_name: str,
    file: UploadFile = File(...),
    user_info: dict = Depends(authenticate),
):
    if not data_manager.hasPermission(user_info["email"], "manage_schema"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to manage schema. Please contact a team lead or project manager.",
        )
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image.")

    file_bytes = await file.read()
    public_url = util.upload_to_gcs(file_bytes, file.filename, processor_name)

    return {"url": public_url}


@router.get("/get_image_url/{processor_name}")
async def get_image_url(processor_name: str, user_info: dict = Depends(authenticate)):
    url = util.generate_download_signed_url_v4(path=f"sample_images/{processor_name}")
    return {"url": url}


@router.post("/update_processor")
async def update_processor(request: Request, user_info: dict = Depends(authenticate)):
    """Update processor

    Args:
        processor: processor data

    Returns:

    """
    if not data_manager.hasPermission(user_info["email"], "manage_schema"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to manage schema. Please contact a team lead or project manager.",
        )

    req = await request.json()

    request_fields = [
        "name",
        "displayName",
        "processorId",
        "modelId",
        "documentType",
        "img",
    ]

    new_schema_data = {
        key: req.get(key) for key in request_fields if req.get(key) is not None
    }

    return data_manager.updateProcessor(new_schema_data, user_info)


@router.post("/update_default_team")
async def update_default_team(
    request: Request, user_info: dict = Depends(authenticate)
):
    """Update user's default team

    Args:
        new_team: new default team

    Returns:
        result
    """
    if not data_manager.hasPermission(user_info["email"], "manage_system"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to perform this action. Please contact a team lead or project manager.",
        )

    req = await request.json()
    new_team = req.get("new_team", None)

    if new_team:
        data_manager.updateDefaultTeam(user_info["email"], new_team)
        return new_team
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Please provide a new team in the request body",
        )


@router.post("/fetch_roles", response_model=list)
async def fetch_roles(request: Request, user_info: dict = Depends(authenticate)):
    """Fetch all available roles for a certain category.

    Args:
        role_category: category of role (team, project, system)

    Returns:
        List containing available roles
    """
    if not data_manager.hasPermission(user_info["email"], "manage_team"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to manage team roles. Please contact a team lead or project manager.",
        )
    role_categories = await request.json()
    resp = data_manager.fetchRoles(role_categories)
    return resp


@router.get("/fetch_teams", response_model=list)
async def fetch_teams(user_info: dict = Depends(authenticate)):
    """Fetch all teams that a user is on.

    Returns:
        List containing teams
    """
    if not data_manager.hasPermission(user_info["email"], "manage_system"):
        raise HTTPException(
            403,
            detail=f"You are not authorized to manage system. Please contact a team lead or project manager.",
        )
    resp = data_manager.fetchTeams(user_info)
    return resp


@router.post("/delete_user/{email}")
async def delete_user(email: str, user_info: dict = Depends(authenticate)):
    """Delete user from application database

    Args:
        email: User email address

    Returns:
        result
    """
    email = email.lower()
    if data_manager.hasPermission(user_info["email"], "delete"):
        data_manager.deleteUser(email, user_info)
        return {"Deleted", email}

    else:
        raise HTTPException(
            status_code=403, detail=f"User is not authorized to perform this operation"
        )
