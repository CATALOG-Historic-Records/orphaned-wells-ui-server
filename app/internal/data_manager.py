import logging
from pathlib import Path
import time
import os
import csv
import json
from enum import Enum
import threading
import traceback

from typing import Union, List
from pydantic import BaseModel
from bson import ObjectId
from pymongo import ASCENDING, DESCENDING

from app.internal.mongodb_connection import connectToDatabase
from app.internal.settings import AppSettings
from app.internal.image_handling import (
    generate_download_signed_url_v4,
    delete_google_storage_directory,
)
import app.internal.util as util


_log = logging.getLogger(__name__)


class Roles(int, Enum):
    """Roles for user accessibility.
    Only approved users should be able to access the app.
    Only special users (admins) should be capable of approving other users.
    """

    pending = -1
    base_user = 1
    admin = 10


class DataManager:
    """Manage the active data."""

    VERSION = 1

    def __init__(self, **kwargs) -> None:
        self.app_settings = AppSettings(**kwargs)
        self.db = connectToDatabase()
        self.environment = os.getenv("ENVIRONMENT")
        _log.info(f"working in environment: {self.environment}")

        self.LOCKED = False
        ## lock_duration: amount of seconds that records remain locked if no changes are made
        self.lock_duration = 120

    ## lock functions
    def fetchLock(self, user):
        ## Can't use variable stored in memory for this
        while self.LOCKED and self.LOCKED != user:
            _log.info(f"{user} waiting for lock")
            time.sleep(0.1)
        self.LOCKED = user
        _log.info(f"{user} grabbed lock")

    def releaseLock(self, user):
        ## Can't use variable stored in memory for this
        _log.info(f"{user} releasing lock")
        self.LOCKED = False

    def lockRecord(self, record_id, user, release_previous_record=True):
        _log.info(f"{user} locking {record_id}")
        if release_previous_record:
            ## remove any record locks that this user may already have in place
            self.releaseRecord(user=user)
        query = {"record_id": record_id}
        data = {
            "user": user,
            "record_id": record_id,
            "timestamp": time.time(),
        }
        self.db.locked_records.update_one(query, {"$set": data}, upsert=True)

    def releaseRecord(self, record_id=None, user=None):
        _log.info(f"releasing record {record_id} or user {user}")
        if record_id:
            self.db.locked_records.delete_many({"record_id": record_id})
        elif user:
            self.db.locked_records.delete_many({"user": user})

    def tryLockingRecord(self, record_id, user):
        try:
            # self.fetchLock(user)
            attained_lock = False
            locked_record_cursor = self.db.locked_records.find({"record_id": record_id})
            record_is_locked = False
            for locked_record_document in locked_record_cursor:
                record_is_locked = True
                break
            locked_record_cursor.close()
            _log.info(f"record_is_locked: {record_is_locked}")
            if record_is_locked:
                ## someone has a lock for this.
                ## (1) check who
                ## (2) check if expired
                locked_time = locked_record_document.get("timestamp", 0)
                lockholder = locked_record_document.get("user", None)
                current_time = time.time()
                if lockholder == user:
                    self.lockRecord(
                        record_id=record_id, user=user, release_previous_record=False
                    )
                    attained_lock = True
                elif locked_time + self.lock_duration < current_time:
                    ## lock is expired
                    self.lockRecord(
                        record_id=record_id, user=user, release_previous_record=True
                    )
                    attained_lock = True
                else:
                    ## lock is still valid by other user
                    attained_lock = False
            else:
                ## record is unlocked, go on ahead
                self.lockRecord(
                    record_id=record_id, user=user, release_previous_record=True
                )
                attained_lock = True
            # self.releaseLock(user)
            return attained_lock
        except Exception as e:
            _log.error(f"error trying to lock record: {e}")
            return False

    ## user functions
    def getUser(self, email):
        cursor = self.db.users.find({"email": email})
        user = None
        for document in cursor:
            user = document
            user["_id"] = str(user["_id"])
        return user

    def updateUserObject(self, user_info):
        cursor = self.db.users.find({"email": user_info["email"]})
        user = None
        for document in cursor:
            user = document
        if user == None:
            return None

        ## update name, picture, hd
        for each in ["name", "picture", "hd"]:
            new_val = user_info.get(each, False)
            if new_val and new_val != "":
                user[each] = new_val

        email = user_info.get("email", "")
        myquery = {"email": email}
        newvalues = {"$set": user}
        self.db.users.update_one(myquery, newvalues)
        return user

    def addUser(self, user_info, default_team, role=Roles.base_user):
        if default_team is None:
            _log.error(f"failed to add user {user_info}. default team is required")
            return False

        ## get team's projects
        project_list = self.getTeamProjectList(default_team)
        project_roles = {}
        for project in project_list:
            project_roles[str(project)] = role
        user = {
            "email": user_info.get("email", ""),
            "name": user_info.get("name", ""),
            "picture": user_info.get("picture", ""),
            "hd": user_info.get("hd", ""),
            "default_team": default_team,
            "role": role,
            "roles": {
                "teams": {
                    default_team: role,
                },
                "projects": project_roles,
                "system": 1,
            },
            "time_created": time.time(),
        }
        user["default_team"] = default_team
        db_response = self.db.users.insert_one(user)

        ## add user to team's users
        team_query = {"name": default_team}
        team_document = self.getDocument("teams", team_query)
        team_users = team_document.get("users", [])
        team_users.append(user_info.get("email", ""))
        newvalues = {"$set": {"users": team_users}}
        self.db.teams.update_one(team_query, newvalues)

        return db_response

    def addUserToTeam(self, email, team, role=Roles.base_user):
        ## CHECK IF USER IS NOT ALREADY ON THIS TEAM
        checkvalues = {"name": team, "users": email}
        found_user = self.db.teams.count_documents(checkvalues)
        if found_user > 0:
            _log.info(f"found {email} on {team}")
            return "already_exists"

        ## update user's teams
        # myquery = {"email": email}
        # newvalues = { "$push": { "teams": team } }
        # cursor = self.db.users.update_one(myquery, newvalues)

        ## update team's users
        myquery = {"name": team}
        newvalues = {"$push": {"users": email}}
        cursor = self.db.teams.update_one(myquery, newvalues)
        return "success"

    def updateUser(self, user_info):
        # _log.info(f"updating user {user_info}")
        email = user_info.get("email", "")
        user = {
            "name": user_info.get("name", ""),
            "picture": user_info.get("picture", ""),
            "hd": user_info.get("hd", ""),
        }
        myquery = {"email": email}
        newvalues = {"$set": user}
        cursor = self.db.users.update_one(myquery, newvalues)
        return cursor

    def approveUser(self, user_email):
        user = {"role": Roles.base_user}
        myquery = {"email": user_email}
        newvalues = {"$set": user}
        self.db.users.update_one(myquery, newvalues)
        return "success"

    def hasRole(self, user_info, role=Roles.admin):
        email = user_info.get("email", "")
        cursor = self.db.users.find({"email": email})
        try:
            document = cursor.next()
            if document.get("role", Roles.pending) == role:
                return True
            else:
                return False
        except:
            return False

    def getUserInfo(self, email):
        user_document = self.getDocument("users", {"email": email}, clean_id=True)
        return user_document

    def getUsers(
        self, role, user_info, project_id_exclude=None, includeLowerRoles=True
    ):
        ## TODO: accept team id as parameter and use that to determine which users to return
        user = user_info.get("email", "")
        user_document = self.getDocument("users", {"email": user})
        team_id = user_document.get("default_team", None)
        team_document = self.getDocument("teams", {"name": team_id})
        team_users = team_document.get("users", [])
        if includeLowerRoles:  # get all users with provided role or lower
            query = {"role": {"$lte": role}}
        else:  # get only users with provided role
            query = {"role": role}
        cursor = self.db.users.find(query)
        users = []
        if project_id_exclude is not None:
            project_id = ObjectId(project_id_exclude)
            for document in cursor:
                if project_id not in document.get("projects", []):
                    next_user = document.get("email", "")
                    if next_user in team_users:
                        users.append(
                            {
                                "email": document.get("email", ""),
                                "name": document.get("name", ""),
                                "hd": document.get("hd", ""),
                                "picture": document.get("picture", ""),
                                "role": document.get("role", -1),
                            }
                        )
        else:
            for document in cursor:
                next_user = document.get("email", "")
                if next_user in team_users:
                    users.append(
                        {
                            "email": document.get("email", ""),
                            "name": document.get("name", ""),
                            "hd": document.get("hd", ""),
                            "picture": document.get("picture", ""),
                            "role": document.get("role", -1),
                        }
                    )
        return users

    def removeUserFromTeam(self, user, team):
        query = {"email": user}
        # delete_response = self.db.users.delete_one(query)
        return user

    def deleteUser(self, email, user_info):
        admin_email = user_info.get("email", None)
        query = {"email": email}
        delete_response = self.db.users.delete_one(query)
        ## TODO: remove user form all teams that include him/her
        query = {"users": email}
        teams_cursor = self.db.teams.find(query)
        for document in teams_cursor:
            team_id = document["_id"]
            user_list = document.get("users", [])
            user_list.remove(email)
            query = {"_id": team_id}
            newvalue = {"$set": {"users": user_list}}
            self.db.teams.update_one(query, newvalue)
        self.recordHistory("deleteUser", user=admin_email)
        return email

    def addUsersToProject(self, users, project_id):
        ## TODO:
        ## (1) change project to team
        _id = ObjectId(project_id)
        try:
            for user in users:
                email = user.get("email", "")
                query = {"email": email}
                cursor = self.db.users.find(query)
                user_object = cursor.next()
                user_projects = user_object.get("projects", [])
                user_projects.append(_id)
                update_query = {"projects": user_projects}
                self.updateUserProjects(email, update_query)
            return {"result": "success"}
        except Exception as e:
            _log.error(f"unable to add users: {e}")
            return {"result": f"{e}"}

    ## Fetch/get functions
    def getDocument(self, collection, query, clean_id=False, return_list=False):
        try:
            cursor = self.db[collection].find(query)
            if not return_list:
                document = cursor.next()
                if clean_id:
                    document_id = document.get("_id", "")
                    document["_id"] = str(document_id)
                return document
        except Exception as e:
            _log.error(f"unable to find {query} in {collection}: {e}")
            return None

    def getProjectFromRecordGroup(self, rg_id):
        project_cursor = self.db.projects.find({"record_groups": rg_id})
        project_document = project_cursor.next()
        project_document["_id"] = str(project_document["_id"])
        return project_document

    def getTeamProjectList(self, team):
        team_query = {"name": team}
        team_cursor = self.db.teams.find(team_query)
        team_document = team_cursor.next()
        projects = team_document.get("project_list", [])
        for i in range(len(projects)):
            projects[i] = ObjectId(projects[i])
        return projects

    def getUserProjectList(self, user):
        user_query = {"email": user}
        user_cursor = self.db.users.find(user_query)
        user_document = user_cursor.next()
        default_team = user_document.get("default_team", None)
        return self.getTeamProjectList(default_team)

    def getUserRecordGroups(self, user):
        projects = self.fetchProjects(user)
        record_groups = []
        for project in projects:
            record_groups += project.get("record_groups", [])
        return record_groups

    def getRecordGroupProgress(self, rg_id):
        ## get total records count
        query = {"record_group_id": rg_id}
        total_amt = self.db.records.count_documents(query)

        ## get count of reviewed and defective
        query = {
            "record_group_id": rg_id,
            "review_status": {"$in": ["defective", "reviewed"]},
        }
        reviewed_amt = self.db.records.count_documents(query)
        return total_amt, reviewed_amt

    def fetchTeamInfo(self, email):
        user_doc = self.db.users.find({"email": email}).next()
        team_name = user_doc["default_team"]
        team_doc = self.db.teams.find({"name": team_name}).next()
        if "projects" in team_doc:
            del team_doc["projects"]
        ## convert object ids to strings
        team_doc["_id"] = str(team_doc["_id"])
        for i in range(len(team_doc["project_list"])):
            project_object_id = team_doc["project_list"][i]
            team_doc["project_list"][i] = str(project_object_id)

        return team_doc

    def fetchProject(self, project_id):
        cursor = self.db.projects.find({"_id": ObjectId(project_id)})
        for document in cursor:
            document["_id"] = str(document["_id"])
            return document
        return None

    def fetchProjects(self, user):
        user_projects = self.getUserProjectList(user)
        projects = []
        cursor = self.db.projects.find({"_id": {"$in": user_projects}})
        for document in cursor:
            document["_id"] = str(document["_id"])
            projects.append(document)
        return projects

    def getProjectRecordGroupsList(self, project_id):
        query = {"_id": ObjectId(project_id)}
        cursor = self.db.projects.find(query)
        document = cursor.next()
        record_groups_list = document.get("record_groups", [])
        return record_groups_list

    def getTeamRecordGroupsList(self, team_name):
        query = {"name": team_name}
        cursor = self.db.teams.find(query)
        document = cursor.next()
        project_list = document.get("project_list", [])
        record_groups_list = []
        for project_id in project_list:
            try:
                rgs = self.getProjectRecordGroupsList(str(project_id))
                record_groups_list += rgs
            except Exception as e:
                _log.error(f"unable to get record groups for project {project_id}: {e}")
        return record_groups_list

    def fetchRecords(
        self, sort_by=["dateCreated", 1], filter_by={}, page=None, records_per_page=None
    ):
        records = []
        record_index = 1
        if page is not None and records_per_page is not None and records_per_page != -1:
            cursor = (
                self.db.records.find(filter_by)
                .sort(
                    sort_by[0],
                    sort_by[1]
                    # )
                )
                .skip(records_per_page * page)
                .limit(records_per_page)
            )
            record_index += page * records_per_page
        else:
            cursor = self.db.records.find(filter_by).sort(sort_by[0], sort_by[1])

        for document in cursor:
            document["_id"] = str(document["_id"])
            document["recordIndex"] = record_index
            record_index += 1
            records.append(document)
        record_count = self.db.records.count_documents(filter_by)
        return records, record_count

    def fetchRecordsByTeam(
        self,
        user,
        page=None,
        records_per_page=None,
        sort_by=["dateCreated", 1],
        filter_by={},
    ):
        team_info = self.fetchTeamInfo(user["email"])
        rg_list = self.getTeamRecordGroupsList(team_info["name"])
        filter_by["record_group_id"] = {"$in": rg_list}
        return self.fetchRecords(sort_by, filter_by, page, records_per_page)

    def fetchRecordsByRecordGroup(
        self,
        user,
        rg_id,
        page=None,
        records_per_page=None,
        sort_by=["dateCreated", 1],
        filter_by={},
    ):
        filter_by["record_group_id"] = rg_id
        return self.fetchRecords(sort_by, filter_by, page, records_per_page)

    def fetchRecordsByProject(
        self,
        user,
        project_id,
        page=None,
        records_per_page=None,
        sort_by=["dateCreated", 1],
        filter_by={},
    ):
        ## if we arent filtering by record_group_id, add filter to look for ALL record_ids in given project
        if "record_group_id" not in filter_by:
            record_group_ids = self.getProjectRecordGroupsList(project_id)
            filter_by["record_group_id"] = {"$in": record_group_ids}
        return self.fetchRecords(sort_by, filter_by, page, records_per_page)

    def fetchRecordGroups(self, project_id, user):
        project = self.fetchProject(project_id)
        if project is None:
            _log.info(f"project {project_id} not found")
            return {}
        project_record_groups = project.get("record_groups", [])
        record_group_ids = []
        for i in range(len(project_record_groups)):
            record_group_ids.append(ObjectId(project_record_groups[i]))
        record_groups = []
        cursor = self.db.record_groups.find({"_id": {"$in": record_group_ids}})
        for document in cursor:
            document["_id"] = str(document["_id"])
            (
                document["total_amt"],
                document["reviewed_amt"],
            ) = self.getRecordGroupProgress(document["_id"])
            record_groups.append(document)
        return {"project": project, "record_groups": record_groups}

    def fetchColumnData(self, location, _id):
        if location == "project" or location == "team":
            columns = set()
            if location == "project":
                # get project, set name and settings
                document = self.db.projects.find({"_id": ObjectId(_id)}).next()
                document["_id"] = _id
                # get all record groups
                record_groups = self.getProjectRecordGroupsList(_id)
            else:
                document = self.db.teams.find({"name": _id}).next()
                document["_id"] = str(document["_id"])
                ##TODO: fix object ids in team project list?
                for i in range(len(document["project_list"])):
                    document["project_list"][i] = str(document["project_list"][i])
                record_groups = self.getTeamRecordGroupsList(_id)
            for rg_id in record_groups:
                rg_document = self.db.record_groups.find(
                    {"_id": ObjectId(rg_id)}
                ).next()
                google_id = rg_document["processorId"]
                processor = self.getProcessorByGoogleId(google_id)
                for attr in processor["attributes"]:
                    columns.add(attr["name"])
            columns = list(columns)
            return {"columns": columns, "obj": document}

        elif location == "record_group":
            columns = []
            rg_document = self.db.record_groups.find({"_id": ObjectId(_id)}).next()
            rg_document["_id"] = _id
            google_id = rg_document["processorId"]
            processor = self.getProcessorByGoogleId(google_id)
            for attr in processor["attributes"]:
                columns.append(attr["name"])
            return {"columns": columns, "obj": rg_document}
        return None

    def getProcessorByGoogleId(self, google_id):
        cursor = self.db.processors.find({"processor_id": google_id})
        for document in cursor:
            document["_id"] = str(document["_id"])
            return document
        return None

    def fetchProcessor(self, google_id):
        cursor = self.db.processors.find({"id": google_id})
        for document in cursor:
            document["_id"] = str(document["_id"])
            return document
        return None

    def fetchProcessors(self, user, state):
        processors = []
        cursor = self.db.processors.find({"state": state})
        for document in cursor:
            document["_id"] = str(document["_id"])
            processors.append(document)
        return processors

    def fetchProjectData(
        self, project_id, user, page, records_per_page, sort_by, filter_by
    ):
        ## get user's projects, check if user has access to this project
        user_projects = self.getUserProjectList(user)
        _id = ObjectId(project_id)
        if not _id in user_projects:
            return None, None

        ## get project data
        cursor = self.db.projects.find({"_id": _id})
        project_data = cursor.next()
        project_data["_id"] = str(project_data["_id"])

        ## get project's records
        records = []
        filter_by["project_id"] = project_id
        record_index = 1
        if page is not None and records_per_page is not None and records_per_page != -1:
            cursor = (
                self.db.records.find(filter_by)
                .sort(
                    sort_by[0],
                    sort_by[1]
                    # )
                )
                .skip(records_per_page * page)
                .limit(records_per_page)
            )
            record_index += page * records_per_page
        else:
            cursor = self.db.records.find(filter_by).sort(sort_by[0], sort_by[1])

        for document in cursor:
            document["_id"] = str(document["_id"])
            document["recordIndex"] = record_index
            record_index += 1
            records.append(document)

        record_count = self.db.records.count_documents(filter_by)
        return project_data, records, record_count

    def fetchRecordGroupData(self, rg_id, user):
        ## get user's projects, check if user has access to this project
        user_record_groups = self.getUserRecordGroups(user)
        if not rg_id in user_record_groups:
            return None, None

        ## get record group data
        _id = ObjectId(rg_id)
        cursor = self.db.record_groups.find({"_id": _id})
        record_group = cursor.next()
        record_group["_id"] = str(record_group["_id"])

        project_document = self.getProjectFromRecordGroup(rg_id)

        return project_document, record_group

    def fetchRecordData(self, record_id, user_info, direction="next"):
        user = user_info.get("email", "")
        _id = ObjectId(record_id)
        cursor = self.db.records.find({"_id": _id})
        document = cursor.next()
        document["_id"] = str(document["_id"])
        rg_id = document.get("record_group_id", "")
        # projectId = document.get("project_id", "")
        # project_id = ObjectId(projectId)

        ## try to attain lock
        attained_lock = self.tryLockingRecord(record_id, user)

        user_record_groups = self.getUserRecordGroups(user)
        if not rg_id in user_record_groups:
            return None, None
        image_urls = []
        for image in document.get("image_files", []):
            if util.imageIsValid(image):
                image_urls.append(
                    generate_download_signed_url_v4(
                        document["record_group_id"], document["_id"], image
                    )
                )
        if len(image_urls) == 0:
            if document.get("filename", False):
                image_urls.append(
                    generate_download_signed_url_v4(
                        document["record_group_id"],
                        document["_id"],
                        document["filename"],
                    )
                )
        document["img_urls"] = image_urls

        ## get record group name
        rg = self.getDocument("record_groups", {"_id": ObjectId(rg_id)})
        rg_name = rg.get("name", "")
        document["rg_name"] = rg_name
        document["rg_id"] = rg_id

        ## get project name
        project_document = self.getProjectFromRecordGroup(rg_id)
        project_name = project_document.get("name", "")
        project_id = str(project_document.get("_id", ""))
        document["project_name"] = project_name
        document["project_id"] = project_id

        ## get record index
        dateCreated = document.get("dateCreated", 0)
        record_index_query = {
            "dateCreated": {"$lte": dateCreated},
            "record_group_id": rg_id,
        }
        record_index = self.db.records.count_documents(record_index_query)
        document["recordIndex"] = record_index

        ## get previous and next IDs
        document["previous_id"] = self.getPreviousRecordId(dateCreated, rg_id)
        document["next_id"] = self.getNextRecordId(dateCreated, rg_id)

        ## sort record attributes
        try:
            google_id = rg["processorId"]
            processor_doc = self.db.processors.find({"google_id": google_id}).next()
            sorted_attributes = util.sortRecordAttributes(
                document["attributesList"], processor_doc
            )
            document["attributesList"] = sorted_attributes
        except Exception as e:
            _log.error(f"unable to sort attributes: {e}")
            _log.error(traceback.format_exc())

        return document, not attained_lock

    def getNextRecordId(self, dateCreated, rg_id):
        # _log.info(f"fetching next record for {dateCreated} and {rg_id}")
        cursor = self.db.records.find(
            {"dateCreated": {"$gt": dateCreated}, "record_group_id": rg_id}
        ).sort("dateCreated", ASCENDING)
        for document in cursor:
            record_id = str(document.get("_id", ""))
            return record_id
        cursor = self.db.records.find({"record_group_id": rg_id}).sort(
            "dateCreated", ASCENDING
        )
        document = cursor.next()
        record_id = str(document.get("_id", ""))
        return record_id

    def getPreviousRecordId(self, dateCreated, rg_id):
        # _log.info(f"fetching previous record for {dateCreated} and {rg_id}")
        cursor = self.db.records.find(
            {"dateCreated": {"$lt": dateCreated}, "record_group_id": rg_id}
        ).sort("dateCreated", DESCENDING)
        for document in cursor:
            record_id = str(document.get("_id", ""))
            return record_id
        cursor = self.db.records.find({"record_group_id": rg_id}).sort(
            "dateCreated", DESCENDING
        )
        document = cursor.next()
        record_id = str(document.get("_id", ""))
        return record_id

    def getProcessorByRecordGroupID(self, rg_id):
        _id = ObjectId(rg_id)
        try:
            cursor = self.db.record_groups.find({"_id": _id})
            document = cursor.next()
            google_id = document.get("processorId", None)
            processor_id = document.get("processor_id", None)
            _processor_id = ObjectId(processor_id)
            processor_cursor = self.db.processors.find({"_id": _processor_id})
            processor_document = processor_cursor.next()
            processor_attributes = processor_document.get("attributes", None)
            return google_id, processor_attributes
        except Exception as e:
            _log.error(f"unable to find processor id: {e}")
            return None

    ## create/add functions
    def createProject(self, project_info, user_info):
        ## get user's default team
        user_email = user_info.get("email", "")
        user_document = self.getDocument("users", ({"email": user_email}))
        default_team = user_document.get("default_team", None)
        if default_team is None:
            _log.info(f"user {user_email} has no default team")
            return False

        ## add default data to project
        project_info["creator"] = user_info
        project_info["team"] = default_team
        project_info["dateCreated"] = time.time()
        project_info["record_groups"] = []
        project_info["history"] = []
        project_info["tags"] = []
        project_info["settings"] = {}

        ## create new project entry
        db_response = self.db.projects.insert_one(project_info)
        new_project_id = db_response.inserted_id

        ## add project to team's project list:
        team_query = {"name": default_team}
        team_document = self.getDocument("teams", team_query)
        team_projects = team_document.get("project_list", [])
        team_projects.append(new_project_id)
        newvalues = {"$set": {"project_list": team_projects}}
        self.db.teams.update_one(team_query, newvalues)

        self.recordHistory("createProject", user_email, str(new_project_id))

        return str(new_project_id)

    def createRecordGroup(self, rg_info, user_info):
        ## get user's default team
        user_email = user_info.get("email", "")
        user_document = self.getDocument("users", ({"email": user_email}))
        default_team = user_document.get("default_team", None)
        if default_team is None:
            _log.info(f"user {user_email} has no default team")
            return False

        ## add user and timestamp to record group
        rg_info["creator"] = user_info
        rg_info["team"] = default_team
        rg_info["dateCreated"] = time.time()
        rg_info["settings"] = {}

        ## get processor id
        processor_document = self.getProcessorByGoogleId(rg_info["processorId"])
        rg_info["processor_id"] = str(processor_document["_id"])

        ## add record group to db collection
        db_response = self.db.record_groups.insert_one(rg_info)
        new_rg_id = db_response.inserted_id

        ## add record group to project's rg list:
        project_query = {"_id": ObjectId(rg_info.get("project_id", None))}
        _log.info(f"project_query: {project_query}")
        project_update = {"$push": {"record_groups": str(new_rg_id)}}

        _log.info(f"project_update: {project_update}")
        self.db.projects.update_one(project_query, project_update)

        self.recordHistory("createRecordGroup", user_email, str(new_rg_id))

        return str(new_rg_id)

    def createRecord(self, record, user_info={}):
        user = user_info.get("email", None)
        ## add timestamp to project
        record["dateCreated"] = time.time()
        ## add record to db collection
        db_response = self.db.records.insert_one(record)
        new_id = db_response.inserted_id
        self.recordHistory("createRecord", user, record_id=str(new_id))
        return str(new_id)

    ## update functions
    def updateProject(self, project_id, new_data, user_info={}):
        user = user_info.get("email", None)
        _id = ObjectId(project_id)
        ## need to choose a subset of the data to update. can't update entire record because _id is immutable
        myquery = {"_id": _id}
        newvalues = {"$set": new_data}
        self.db.projects.update_one(myquery, newvalues)
        self.recordHistory("updateProject", user, project_id)
        cursor = self.db.projects.find(myquery)
        for document in cursor:
            document["_id"] = str(document["_id"])
            return document
        return None

    def updateRecordGroup(self, rg_id, new_data, user_info={}):
        user = user_info.get("email", None)
        _id = ObjectId(rg_id)
        ## need to choose a subset of the data to update. can't update entire record because _id is immutable
        myquery = {"_id": _id}
        newvalues = {"$set": new_data}
        self.db.record_groups.update_one(myquery, newvalues)
        self.recordHistory("updateRecordGroup", user, rg_id)
        cursor = self.db.record_groups.find(myquery)
        for document in cursor:
            document["_id"] = str(document["_id"])
            return document
        return None

    def updateUserProjects(self, email, new_data):
        _log.info(f"updating {email} to be {new_data}")
        ## need to choose a subset of the data to update. can't update entire record because _id is immutable
        myquery = {"email": email}
        newvalues = {"$set": new_data}
        self.db.users.update_one(myquery, newvalues)
        # _log.info(f"successfully updated project? cursor is : {cursor}")
        return "success"

    def updateRecordReviewStatus(self, record_id, review_status, user_info):
        new_data = {"review_status": review_status}
        self.updateRecord(record_id, new_data, "record", user_info)

    def updateRecord(
        self, record_id, new_data, update_type=None, user_info=None, forceUpdate=False
    ):
        # _log.info(f"updating {record_id} to be {new_data}")
        attained_lock = False
        user = None
        if user_info is None and not forceUpdate:
            return False
        elif user_info is not None:
            user = user_info.get("email", None)
            attained_lock = self.tryLockingRecord(record_id, user)
        if attained_lock or forceUpdate:
            if update_type is None:
                return False
            _id = ObjectId(record_id)
            search_query = {"_id": _id}
            if update_type == "record":
                data_update = new_data
                update_query = {"$set": data_update}
            else:
                data_update = {update_type: new_data.get(update_type, None)}
                if (
                    update_type == "attributesList"
                    and new_data.get("review_status", None) == "unreviewed"
                ):
                    ## if an attribute is updated and the record is unreviewed, automatically move review_status to incomplete
                    data_update["review_status"] = "incomplete"
                elif (
                    update_type == "review_status"
                    and new_data.get("review_status", None) == "unreviewed"
                ):
                    data_update = self.resetRecord(record_id, new_data, user)
                update_query = {"$set": data_update}
            if not forceUpdate:
                ## fetch record's current data so we know what changed in the future
                try:
                    record_doc = self.db.records.find(
                        {"_id": ObjectId(record_id)}
                    ).next()
                    previous_state = {}
                    for each in data_update:
                        previous_state[each] = record_doc.get(each, None)
                except Exception as e:
                    _log.info(f"unable to get record's previous state: {e}")
                    previous_state = None
                self.recordHistory(
                    "updateRecord",
                    user,
                    record_id=record_id,
                    query=data_update,
                    previous_state=previous_state,
                )
            self.db.records.update_one(search_query, update_query)

            return data_update
        else:
            return False

    def resetRecord(self, record_id, record_data, user):
        print(f"resetting record: {record_id}")
        record_attributes = record_data["attributesList"]
        for attribute in record_attributes:
            attribute_name = attribute["key"]
            if attribute["normalized_value"] != "":
                original_value = attribute["normalized_value"]
            else:
                original_value = attribute["raw_text"]
            attribute["value"] = original_value
            attribute["confidence"] = attribute["ai_confidence"]
            attribute["edited"] = False
            ## check for subattributes and reset those
            if attribute["subattributes"] is not None:
                record_subattributes = attribute["subattributes"]
                for subattribute in record_subattributes:
                    if subattribute["normalized_value"] != "":
                        original_value = subattribute["normalized_value"]
                    else:
                        original_value = subattribute["raw_text"]
                    subattribute["value"] = original_value
                    subattribute["confidence"] = subattribute.get("ai_confidence", None)
                    subattribute["edited"] = False
        update = {
            "review_status": "unreviewed",
            "attributesList": record_attributes,
        }
        # history is recorded in the function that calls this
        return update

    ## delete functions
    def deleteProject(self, project_id, background_tasks, user_info):
        ## TODO: check if user is a part of the team who owns this project
        _log.info(f"deleting project {project_id}")
        _id = ObjectId(project_id)
        myquery = {"_id": _id}

        ## add to deleted projects collection first
        project_cursor = self.db.projects.find(myquery)
        project_document = project_cursor.next()
        project_document["deleted_by"] = user_info
        team = project_document.get("team", "")
        self.db.deleted_projects.insert_one(project_document)

        ## delete from projects collection
        self.db.projects.delete_one(myquery)

        ## delete record groups
        record_groups = project_document.get("record_groups", [])
        self.deleteRecordGroups(record_groups=record_groups, deletedBy=user_info)

        ## add records to deleted records collection and remove from records collection
        background_tasks.add_task(
            self.deleteRecords,
            query={"record_group_id": {"$in": record_groups}},
            deletedBy=user_info,
        )

        self.recordHistory(
            "deleteProject", user_info.get("email", None), project_id=project_id
        )

        self.removeProjectFromTeam(_id, team)
        return "success"

    def deleteRecordGroup(self, rg_id, background_tasks, user_info):
        _log.info(f"deleting record group {rg_id}")
        _id = ObjectId(rg_id)
        myquery = {"_id": _id}

        ## add to deleted record groups collection first
        record_group_cursor = self.db.record_groups.find(myquery)
        record_group_doc = record_group_cursor.next()
        record_group_doc["deleted_by"] = user_info
        team = record_group_doc.get("team", "")
        self.db.deleted_record_groups.insert_one(record_group_doc)

        ## delete from record groups collection
        self.db.record_groups.delete_one(myquery)

        ## add records to deleted records collection and remove from records collection
        background_tasks.add_task(
            self.deleteRecords,
            query={"record_group": rg_id},
            deletedBy=user_info,
        )

        self.recordHistory(
            "deleteRecordGroup", user_info.get("email", None), rg_id=rg_id
        )

        ## remove from project list
        self.removeRecordGroupFromProject(rg_id)

        self.removeRecordGroupFromTeam(_id, team)
        return "success"

    def deleteRecord(self, record_id, user_info):
        user = user_info.get("email", None)
        ## TODO: check if user is a part of the team who owns the project that owns this record
        _log.info(f"deleting {record_id}")
        _id = ObjectId(record_id)
        myquery = {"_id": _id}
        self.db.records.delete_one(myquery)
        self.recordHistory("deleteRecord", user=user, record_id=record_id)
        return "success"

    def deleteRecords(self, query, deletedBy):
        user = deletedBy.get("email", None)
        _log.info(f"deleting records with query: {query}")
        ## add records to deleted records collection
        record_cursor = self.db.records.find(query)
        try:
            for record_document in record_cursor:
                record_document["deleted_by"] = deletedBy
                self.db.deleted_records.insert_one(record_document)
        except Exception as e:
            _log.error(f"unable to move all deleted records: {e}")

        ## Delete records associated with this project
        self.db.records.delete_many(query)
        # self.recordHistory("deleteRecords", user=user, notes=query)
        return "success"

    def deleteRecordGroups(self, record_groups, deletedBy):
        user = deletedBy.get("email", None)
        _log.info(f"deleting record groups: {record_groups}")
        record_group_ids = []
        for i in range(len(record_groups)):
            record_group_ids.append(ObjectId(record_groups[i]))
        ## add to deleted records collection
        query = {"_id": {"$in": record_group_ids}}
        cursor = self.db.record_groups.find(query)
        try:
            for document in cursor:
                document["deleted_by"] = deletedBy
                self.db.deleted_record_groups.insert_one(document)
        except Exception as e:
            _log.error(f"unable to move all deleted record groups: {e}")

        ## Delete records associated with this project
        self.db.record_groups.delete_many(query)
        return "success"

    def removeProjectFromTeam(self, project_id, team):
        team_query = {"name": team}
        update = {"$pull": {"project_list": project_id}}
        self.db.teams.update_many(team_query, update)

    def removeRecordGroupFromProject(self, rg_id):
        query = {"record_groups": rg_id}
        update = {"$pull": {"record_groups": rg_id}}
        self.db.projects.update_many(query, update)

    def removeRecordGroupFromTeam(self, rg_id, team):
        team_query = {"name": team}
        update = {"$pull": {"record_groups": rg_id}}
        self.db.teams.update_many(team_query, update)

    ## miscellaneous functions
    def downloadRecords(
        self,
        records,
        exportType,
        user_info,
        _id,
        location,
        selectedColumns=[],
        keep_all_columns=False,
    ):
        user = user_info.get("email", None)
        ## TODO: check if user is a part of the team who owns this project
        today = time.time()
        output_dir = self.app_settings.export_dir
        output_file = os.path.join(output_dir, f"{_id}_{today}.{exportType}")
        attributes = ["file"]
        subattributes = []
        record_attributes = []
        if exportType == "csv":
            for document in records:
                current_attributes = set()
                record_attribute = {}
                for document_attribute in document["attributesList"]:
                    attribute_name = document_attribute["key"].replace(" ", "")
                    if attribute_name in selectedColumns or keep_all_columns:
                        original_attribute_name = attribute_name
                        i = 2
                        while attribute_name in current_attributes:
                            ## add a number to the end of the attribute so it (and its subattributes)
                            ## is differentiable from other instances of the attribute
                            attribute_name = f"{original_attribute_name}_{i}"
                            i += 1
                        current_attributes.add(attribute_name)
                        if attribute_name not in attributes:
                            attributes.append(attribute_name)
                        record_attribute[attribute_name] = document_attribute["value"]
                        ## add subattributes
                        if document_attribute.get("subattributes", None):
                            for document_subattribute in document_attribute[
                                "subattributes"
                            ]:
                                subattribute_name = (
                                    f"{attribute_name}[{document_subattribute['key']}]"
                                )
                                record_attribute[
                                    subattribute_name
                                ] = document_subattribute["value"]
                                if subattribute_name not in subattributes:
                                    subattributes.append(subattribute_name)
                record_attribute["file"] = document.get("filename", "")
                record_attributes.append(record_attribute)

            # compute the output file directory and name
            with open(output_file, "w", newline="") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=attributes + subattributes)
                writer.writeheader()
                writer.writerows(record_attributes)
        else:
            for document in records:
                record_attribute = {}
                for document_attribute in document["attributesList"]:
                    attribute_name = document_attribute["key"]
                    if attribute_name in selectedColumns or keep_all_columns:
                        record_attribute[attribute_name] = document_attribute
                record_attribute["file"] = document.get("filename", "")
                record_attributes.append(record_attribute)
            with open(output_file, "w", newline="") as jsonfile:
                json.dump(record_attributes, jsonfile)

        if location == "project":
            self.recordHistory("downloadRecords", user=user, project_id=_id)
        elif location == "record_group":
            self.recordHistory("downloadRecords", user=user, rg_id=_id)
        elif location == "team":
            self.recordHistory(
                "downloadRecords", user=user, notes="downloaded team records"
            )
        return output_file

    def checkProjectValidity(self, projectId):
        try:
            project_id = ObjectId(projectId)
        except:
            return False
        project = self.getDocument("projects", {"_id": project_id})
        if project is not None:
            return True

    def checkRecordGroupValidity(self, rg_id):
        try:
            rg_id = ObjectId(rg_id)
        except:
            return False
        rg = self.getDocument("record_groups", {"_id": rg_id})
        if rg is not None:
            return True

    def recordHistory(
        self,
        action,
        user=None,
        project_id=None,
        rg_id=None,
        record_id=None,
        notes=None,
        query=None,
        previous_state=None,
    ):
        try:
            history_item = {
                "action": action,
                "user": user,
                "project_id": project_id,
                "record_group_id": rg_id,
                "record_id": record_id,
                "notes": notes,
                "query": query,
                "previous_state": previous_state,
                "timestamp": time.time(),
            }
            self.db.history.insert_one(history_item)
        except Exception as e:
            _log.error(f"unable to record history item: {e}")


data_manager = DataManager()
