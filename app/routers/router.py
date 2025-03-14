import os
import logging
import aiofiles
import requests
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
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import OAuth2PasswordBearer

from app.internal.data_manager import data_manager
from app.internal.image_handling import process_document, process_zip
import app.internal.util as util
import app.internal.auth as auth

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
    user = data_manager.getUser(email)
    return user


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


@router.get("/get_processors/{state}", response_model=list)
async def get_processors(state: str, user_info: dict = Depends(authenticate)):
    """Fetch all processors for a given state/organization.

    Returns:
        List containing processors and metadata
    """
    resp = data_manager.fetchProcessors(user_info.get("email", ""), state)
    return resp


@router.post("/get_project/{project_id}")
async def get_project_data(
    request: Request,
    project_id: str,
    page: int = None,
    records_per_page: int = None,
    user_info: dict = Depends(authenticate),
):
    """Fetch individual project data.

    Args:
        project_id: Project identifier

    Returns:
        Dictionary containing project data, list of records
    """
    request_body = await request.json()
    sort_by = request_body.get(
        "sort", ["dateCreated", 1]
    )  ## 1 is ascending, -1 is descending`
    if sort_by[1] != 1 and sort_by[1] != -1:
        sort_by[1] = 1
    filter_by = request_body.get("filter", {})
    project_data, records, record_count = data_manager.fetchProjectData(
        project_id,
        user_info.get("email", ""),
        page,
        records_per_page,
        sort_by,
        filter_by,
    )
    if project_data is None:
        raise HTTPException(
            403,
            detail=f"You do not have access to this project, please contact the project creator to gain access.",
        )
    return {
        "project_data": project_data,
        "records": records,
        "record_count": record_count,
    }


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


@router.get("/get_record/{record_id}")
async def get_record_data(record_id: str, user_info: dict = Depends(authenticate)):
    """Fetch document record data.

    Args:
        record_id: Record identifier

    Returns:
        List containing record data
    """
    record, is_locked = data_manager.fetchRecordData(record_id, user_info)
    if record is None:
        raise HTTPException(
            403,
            detail=f"You do not have access to this record, please contact the project creator to gain access.",
        )

    ## get record schema
    _, processor_attributes = data_manager.getProcessorByRecordGroupID(record["rg_id"])
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
    resp = data_manager.fetchProcessor(google_id)
    return resp


@router.get("/get_column_data/{location}/{_id}", response_model=dict)
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
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    reprocessed: bool = False,
    preventDuplicates: bool = False,
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

    ##TODO: provide boolean for run_cleaning_functions for frontend. for now, make this true
    run_cleaning_functions = True

    if file_ext.lower() == ".zip":
        output_dir = f"{data_manager.app_settings.img_dir}"
        return process_zip(
            rg_id,
            user_info,
            background_tasks,
            file,
            output_dir,
            filename,
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
            )
        except Exception as e:
            _log.error(f"unable to read image file: {e}")
            raise HTTPException(400, detail=f"Unable to process image file: {e}")


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
            record_id, data, update_type, field_to_clean, user_info
        )
    if not update:
        raise HTTPException(status_code=403, detail=f"Record is locked by another user")

    return update


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


@router.post("/download_records/{location}/{_id}", response_class=Response)
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
        project_id: Project identifier
        request body:
            exportType: type of export (csv or json)
            columns: list attributes to export

    Returns:
        CSV or JSON file containing all record data for provided project
    """
    req = await request.json()
    # _log.info(req)
    selectedColumns = req.get("columns", [])

    filter_by = req.get("filter", {})
    sort_by = req.get("sort", ["dateCreated", 1])

    keep_all_columns = False
    if len(selectedColumns) == 0:
        keep_all_columns = True

    if location == "project":
        records, _ = data_manager.fetchRecordsByProject(
            user_info, _id, filter_by=filter_by, sort_by=sort_by
        )
    elif location == "record_group":
        records, _ = data_manager.fetchRecordsByRecordGroup(
            user_info, _id, filter_by=filter_by, sort_by=sort_by
        )
    elif location == "team":
        records, _ = data_manager.fetchRecordsByTeam(
            user_info, filter_by=filter_by, sort_by=sort_by
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
                output_filename=output_name,
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
                output_filename=output_name,
            )
            filepaths.append(json_file)
        if export_images:
            documents = util.compileDocumentImageList(records)
        else:
            documents = None

        zipped_files = util.zip_files(filepaths, documents)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {e}")
    ## remove file after 30 seconds to allow for the user download to finish
    background_tasks.add_task(util.deleteFiles, filepaths=filepaths, sleep_time=30)
    return Response(content=zipped_files, media_type="application/zip")


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
    new_role = req.get("new_roles", None)
    email = req.get("email", None)
    team = data_manager.getUserInfo(user_info["email"])["default_team"]
    if new_role and role_category and email:
        data_manager.updateUserRole(email, team, role_category, new_role)
        return email
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Please provide an update and an email in the request body",
        )


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


@router.get("/fetch_roles/{role_category}", response_model=list)
async def fetch_roles(role_category: str, user_info: dict = Depends(authenticate)):
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
    resp = data_manager.fetchRoles(role_category)
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
