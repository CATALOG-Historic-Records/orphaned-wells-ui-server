import io
import os
import logging
import aiofiles
import requests
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from typing import Annotated
import jwt
from fastapi import (
    # Body,
    Request,
    APIRouter,
    HTTPException,
    File,
    UploadFile,
    BackgroundTasks,
    Depends,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer
import zipfile

from app.internal.data_manager import data_manager, Project, Roles
from app.internal.image_handling import (
    process_single_file,
    process_document,
    process_zip,
    convert_pdf,
    convert_tiff,
    process_image,
    upload_to_google_storage,
)
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
    """Function authenticating API calls; required as a dependency for all API calls.

    Args:
        id_token: token provided upon signin

    Returns:
        user account information
    """
    try:
        user_info = id_token.verify_oauth2_token(
            token, google_requests.Request(), client_id
        )
        # _log.info(f"user_info: {user_info}")
        return user_info
    except Exception as e:  # should probably specify exception type
        ## return something to inform the frontend to prompt the user to log back in
        _log.info(f"unable to authenticate: {e}")
        raise HTTPException(status_code=401, detail=f"unable to authenticate: {e}")


@router.post("/auth_login")
async def auth_login(request: Request):
    """Function for logging into google account.

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
    except Exception as e:  # should probably specify exception type
        _log.info(f"unable to authenticate: {e}")
        raise HTTPException(status_code=401, detail=f"unable to authenticate: {e}")

    role = data_manager.checkForUser(user_info, add=False)
    if role == "not found":
        _log.info(f"user is not authorized")
        raise HTTPException(status_code=403, detail=user_info)
    if role < Roles.base_user:
        _log.info(f"user is not authorized")
        raise HTTPException(status_code=403, detail=user_info)
    else:
        _log.info(f"user has role {role}")
        return user_tokens


@router.post("/auth_refresh")
async def auth_refresh(request: Request):
    """Function for refreshing user tokens.

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
    role = data_manager.checkForUser(user_info)
    if role < Roles.base_user:
        _log.info(f"user is not authorized")
        raise HTTPException(status_code=403, detail=user_info)
    else:
        _log.info(f"user has role {role}")
        return user_tokens


@router.post("/check_auth")
async def check_authorization(user_info: dict = Depends(authenticate)):
    """Function authenticating API calls; required as a dependency for all API calls.

    Args:
        id_token: token provided upon signin

    Returns:
        user account information
    """
    role = data_manager.checkForUser(
        {"email": user_info.get("email", "")}, update=False, add=False
    )
    user_info["role"] = role
    return user_info


@router.post("/logout")
async def logout(request: Request):
    """Function for logging out and revoking tokens.

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
    """
    Fetch all projects
    """
    resp = data_manager.fetchProjects(user_info.get("email", ""))
    return resp


@router.get("/get_project/{project_id}")
async def get_project_data(project_id: str, user_info: dict = Depends(authenticate)):
    """Fetch project data.

    Args:
        project_id: Project identifier

    Returns:
        Project data, all records associated with that project
    """
    project_data, records = data_manager.fetchProjectData(
        project_id, user_info.get("email", "")
    )
    if project_data is None:
        raise HTTPException(
            403,
            detail=f"You do not have access to this project, please contact the project creator to gain access.",
        )
    return {"project_data": project_data, "records": records}


@router.get("/get_team_records")
async def get_team_records(user_info: dict = Depends(authenticate)):
    """Fetch project data.

    Args:
        project_id: Project identifier

    Returns:
        Project data, all records associated with that project
    """
    records = data_manager.getTeamRecords(user_info)
    return {"records": records}


@router.get("/get_record/{record_id}")
async def get_record_data(record_id: str, user_info: dict = Depends(authenticate)):
    """Fetch document record data.

    Args:
        record_id: Record identifier

    Returns:
        Record data
    """
    record, is_locked = data_manager.fetchRecordData(record_id, user_info)
    if record is None:
        raise HTTPException(
            403,
            detail=f"You do not have access to this record, please contact the project creator to gain access.",
        )
    elif is_locked:
        return JSONResponse(
            status_code=303, content={"direction": "next", "recordData": record}
        )
    return record


@router.post("/get_next_record")
async def get_next_record(request: Request, user_info: dict = Depends(authenticate)):
    """Fetch document record data.

    Args:
        record_id: Record identifier

    Returns:
        Record data
    """
    req = await request.json()
    data = req.get("data", {})
    reviewed = req.get("reviewed", False)
    reviewStatus = req.get("review_status", None)
    if reviewed:
        data_manager.updateRecordReviewStatus(
            data.get("_id", ""), reviewStatus, user_info
        )
    record, is_locked = data_manager.fetchNextRecord(
        data.get("dateCreated", ""), data.get("project_id", ""), user_info
    )
    if is_locked:
        return JSONResponse(
            status_code=303, content={"direction": "next", "recordData": record}
        )
    return record


@router.post("/get_previous_record")
async def get_previous_record(
    request: Request, user_info: dict = Depends(authenticate)
):
    """Fetch document record data.

    Args:
        record_id: Record identifier

    Returns:
        Record data
    """
    data = await request.json()
    record, is_locked = data_manager.fetchPreviousRecord(
        data.get("dateCreated", ""), data.get("project_id", ""), user_info
    )
    if is_locked:
        return JSONResponse(
            status_code=303, content={"direction": "previous", "recordData": record}
        )
    return record


@router.post("/add_project")
async def add_project(request: Request, user_info: dict = Depends(authenticate)):
    """Add new project.

    Args:
        request data: Project data

    Returns:
        New project identifier
    """
    data = await request.json()

    # _log.info(f"adding project with data: {data}")
    new_id = data_manager.createProject(data, user_info)
    return new_id


@router.post("/upload_document/{project_id}/{user_email}")
async def upload_document(
    project_id: str,
    user_email: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    # user_info: dict = Depends(authenticate),
):
    """Upload document for processing.Documents are processed asynchronously.

    Args:
        project_id: Project identifier to be associated with this document
        file: Document file

    Returns:
        New document record identifier.
    """
    user_info = data_manager.getUserInfo(user_email)
    project_is_valid = data_manager.checkProjectValidity(project_id)
    if not project_is_valid:
        raise HTTPException(404, detail=f"Project not found")
    filename, file_ext = os.path.splitext(file.filename)
    if file_ext.lower() == ".zip":
        output_dir = f"{data_manager.app_settings.img_dir}"
        return process_zip(
            project_id,
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
                project_id,
                user_info,
                background_tasks,
                original_output_path,
                file_ext,
                filename,
                data_manager,
                mime_type,
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
        request data: New data for provided project

    Returns:
        Success response
    """
    data = await request.json()
    data_manager.updateProject(project_id, data, user_info)

    return {"response": "success"}


@router.post("/update_record/{record_id}")
async def update_record(
    record_id: str, request: Request, user_info: dict = Depends(authenticate)
):
    """Update record data.

    Args:
        record_id: Record identifier
        request data: New data for provided record

    Returns:
        Success response
    """
    req = await request.json()
    data = req.get("data", None)
    update_type = req.get("type", None)
    updated = data_manager.updateRecord(record_id, data, update_type, user_info)
    if not updated:
        raise HTTPException(status_code=403, detail=f"Record is locked by another user")

    return {"response": "success"}


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
    data_manager.deleteProject(project_id, background_tasks, user_info)

    return {"response": "success"}


@router.post("/delete_record/{record_id}")
async def delete_record(record_id: str, user_info: dict = Depends(authenticate)):
    """Delete record.

    Args:
        record_id: Record identifier

    Returns:
        Success response
    """
    data_manager.deleteRecord(record_id, user_info)

    return {"response": "success"}


@router.post("/download_records/{project_id}", response_class=FileResponse)
async def download_records(
    project_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    user_info: dict = Depends(authenticate),
):
    """Download records for given project ID.

    Args:
        project_id: Project identifier

    Returns:
        CSV file containing all records associated with that project
    """
    req = await request.json()
    # _log.info(req)
    exportType = req.get("exportType", "csv")
    selectedColumns = req.get("columns", None)

    export_file = data_manager.downloadRecords(
        project_id, exportType, selectedColumns, user_info
    )
    ## remove file after 30 seconds to allow for the user download to finish
    background_tasks.add_task(
        data_manager.deleteFiles, filepaths=[export_file], sleep_time=30
    )
    return export_file


@router.post("/get_users/{role}")
async def get_users(
    role: str, request: Request, user_info: dict = Depends(authenticate)
):
    """Fetch all users from DB with role base_user or lower. Checks if user has proper role (admin)

    Returns:
        List of users, role types
    """
    ## TODO: add team id as a request parameter
    req = await request.json()
    project_id = req.get("project_id", None)
    users = data_manager.getUsers(Roles[role], user_info, project_id_exclude=project_id)
    return users


@router.post("/add_contributors/{project_id}")
async def add_contributors(
    project_id: str, request: Request, user_info: dict = Depends(authenticate)
):
    """Add user to application database with role 'pending'

    Args:
        email: User email address

    Returns:
        user status
    """
    ## TODO: change project to team
    req = await request.json()
    users = req.get("users", "")
    return data_manager.addUsersToProject(users, project_id)


## admin functions
@router.post("/approve_user/{email}")
async def approve_user(email: str, user_info: dict = Depends(authenticate)):
    """Approve user for use of application by changing role from 'pending' to 'user'

    Args:
        email: User email address

    Returns:
        approved user information
    """
    if data_manager.hasRole(user_info, Roles.admin):
        return data_manager.approveUser(email)
    else:
        raise HTTPException(
            status_code=403, detail=f"User is not authorized to perform this operation"
        )


@router.post("/add_user/{email}")
async def add_user(email: str, user_info: dict = Depends(authenticate)):
    """Add user to application database with role 'pending'

    Args:
        email: User email address

    Returns:
        user status
    """
    if data_manager.hasRole(user_info, Roles.admin):
        ## TODO check if provided email is a valid email address
        admin_document = data_manager.getDocument(
            "users", {"email": user_info.get("email", "")}
        )
        team = admin_document.get("default_team", None)
        ## this function will check for and then add user if it is not found
        role = data_manager.checkForUser(
            {"email": email}, update=False, team=team, add=False
        )
        if role == "not found":
            resp = data_manager.addUser({"email": email}, team, role=Roles.base_user)
        elif role > 0:
            ## TODO: in this case, just add user to team without creating new user
            resp = data_manager.addUserToTeam(email, team, role=Roles.base_user)
            if resp == "already_exists":
                ## 406 Not acceptable: user provided an email that is already on this team
                raise HTTPException(
                    status_code=406, detail=f"This user is already on this team."
                )
            else:
                return {"base_user": email}

        else:
            return {"base_user": email}

    else:
        raise HTTPException(
            status_code=403, detail=f"User is not authorized to perform this operation"
        )


@router.post("/delete_user/{email}")
async def delete_user(email: str, user_info: dict = Depends(authenticate)):
    """Delete user from application database

    Args:
        email: User email address

    Returns:
        result
    """
    if data_manager.hasRole(user_info, Roles.admin):
        data_manager.deleteUser(email, user_info)
        return {"Deleted", email}

    else:
        raise HTTPException(
            status_code=403, detail=f"User is not authorized to perform this operation"
        )
