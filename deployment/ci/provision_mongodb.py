"""Provision a scoped MongoDB Atlas user and initialize an empty OGRRE database.

Used by deploy-k8s-dispatch.yml during the first deployment of a new collaborator
when database credentials are not already present in RUNTIME_ENV, GCS, or GKE.
"""

import argparse
import os
import secrets
import string
import sys
import time
import urllib.parse


ROLES = [
    {
        "id": "proj_collaborator",
        "name": "Project Collaborator",
        "permissions": [
            "view_project",
            "review_record",
            "update_coordinates",
        ],
        "includes": [],
        "category": "project",
    },
    {
        "id": "proj_manager",
        "name": "Project Manager",
        "permissions": [
            "manage_project",
            "verify_record",
            "view_project",
            "review_record",
            "clean_record",
            "update_coordinates",
        ],
        "includes": ["proj_collaborator"],
        "category": "project",
    },
    {
        "id": "sys_admin",
        "name": "System Admin",
        "permissions": [
            "create_project",
            "create_record_group",
            "create_team",
            "upload_document",
            "add_user",
            "manage_team",
            "manage_project",
            "review_record",
            "view_project",
            "verify_record",
            "delete",
            "manage_system",
            "clean_record",
            "system_administration",
            "developer",
            "view_schema",
            "manage_schema",
            "manage_schema_destructive",
            "update_coordinates",
        ],
        "includes": ["team_lead"],
        "category": "system",
    },
    {
        "id": "developer",
        "name": "Developer",
        "permissions": [
            "create_project",
            "create_record_group",
            "create_team",
            "upload_document",
            "add_user",
            "manage_team",
            "manage_project",
            "review_record",
            "view_project",
            "verify_record",
            "manage_system",
            "clean_record",
            "view_schema",
            "manage_schema",
            "developer",
            "update_coordinates",
        ],
        "includes": ["team_lead"],
        "category": "system",
    },
    {
        "id": "team_lead",
        "name": "Team Lead",
        "permissions": [
            "manage_team",
            "manage_project",
            "verify_record",
            "view_project",
            "review_record",
            "add_user",
            "clean_record",
            "update_coordinates",
        ],
        "includes": ["team_member", "proj_manager"],
        "category": "team",
    },
    {
        "id": "team_member",
        "name": "Team Member",
        "permissions": [
            "view_project",
            "review_record",
            "update_coordinates",
        ],
        "includes": ["proj_collaborator"],
        "category": "team",
    },
]

ATLAS_API_BASE = "https://cloud.mongodb.com/api/atlas/v2"
ATLAS_ACCEPT_HEADER = "application/vnd.atlas.2023-02-01+json"


def generate_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def parse_admin_emails(raw_emails: str) -> list[str]:
    emails = []
    for item in (raw_emails or "").replace(";", ",").split(","):
        cleaned = item.strip().lower()
        if cleaned and "@" in cleaned and cleaned not in emails:
            emails.append(cleaned)
    return emails or ["mpesce@lbl.gov"]


def is_database_empty(db) -> tuple[bool, list[str]]:
    """Return (True, []) only if the database has zero existing collections and documents."""
    collections = [
        name for name in db.list_collection_names() if not name.startswith("system.")
    ]
    if collections:
        return False, sorted(collections)
    for critical in ("roles", "teams", "users", "projects", "record_groups", "records"):
        if db[critical].count_documents({}, limit=1) > 0:
            return False, [critical]
    return True, []


def initialize_empty_database(db, team_name: str, admin_emails: list[str]) -> bool:
    """Seed roles, initial team, and admin users only if the database is completely empty.

    Returns True if initialized, False if skipped because the database already had data.
    """
    empty, existing = is_database_empty(db)
    if not empty:
        print(
            f"Database '{db.name}' already contains collections ({', '.join(existing)}); "
            "skipping initialization to prevent overwriting existing data."
        )
        return False

    normalized_emails = parse_admin_emails(",".join(admin_emails))
    new_team = {
        "name": team_name,
        "display_name": team_name,
        "users": normalized_emails,
        "project_list": [],
    }
    new_users = [
        {
            "email": email,
            "default_team": team_name,
            "roles": {
                "system": ["sys_admin"],
                "team": {team_name: ["team_lead"]},
            },
        }
        for email in normalized_emails
    ]

    db.roles.insert_many([dict(role) for role in ROLES])
    db.teams.insert_one(new_team)
    db.users.insert_many(new_users)
    print(
        f"Initialized empty database '{db.name}' with team '{team_name}' "
        f"and admin user(s): {', '.join(normalized_emails)}"
    )
    return True


def _build_atlas_auth(
    public_key: str,
    private_key: str,
    client_id: str,
    client_secret: str,
    http,
):
    from requests.auth import HTTPDigestAuth

    if client_id and client_secret:
        token_resp = http.post(
            "https://cloud.mongodb.com/api/oauth/token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        if token_resp.status_code != 200:
            raise RuntimeError(
                f"Failed to obtain MongoDB Atlas OAuth token: HTTP {token_resp.status_code}"
            )
        token = (token_resp.json() or {}).get("access_token")
        if not token:
            raise RuntimeError(
                "MongoDB Atlas OAuth response did not include access_token"
            )
        return None, {"Authorization": f"Bearer {token}"}

    return HTTPDigestAuth(public_key, private_key), {}


def create_atlas_database_user(
    project_id: str,
    public_key: str,
    private_key: str,
    db_name: str,
    username: str,
    password: str,
    client_id: str = "",
    client_secret: str = "",
    session=None,
) -> None:
    """Create a database-scoped MongoDB Atlas user, failing if the user already exists."""
    import requests

    http = session or requests
    auth, extra_headers = _build_atlas_auth(
        public_key=public_key,
        private_key=private_key,
        client_id=client_id,
        client_secret=client_secret,
        http=http,
    )
    headers = {
        "Accept": ATLAS_ACCEPT_HEADER,
        "Content-Type": "application/json",
        **extra_headers,
    }

    encoded_user = urllib.parse.quote(username, safe="")
    get_url = f"{ATLAS_API_BASE}/groups/{project_id}/databaseUsers/admin/{encoded_user}"
    check_resp = http.get(get_url, auth=auth, headers=headers, timeout=30)
    if check_resp.status_code == 200:
        raise RuntimeError(
            f"MongoDB Atlas user '{username}' already exists in project '{project_id}'. "
            "Refusing to overwrite existing database user credentials. "
            "Provide the existing credentials in RUNTIME_ENV or GCS."
        )
    if check_resp.status_code != 404:
        raise RuntimeError(
            f"Failed to check existing MongoDB Atlas user '{username}': "
            f"HTTP {check_resp.status_code}"
        )

    create_url = f"{ATLAS_API_BASE}/groups/{project_id}/databaseUsers"
    payload = {
        "groupId": project_id,
        "databaseName": "admin",
        "username": username,
        "password": password,
        "roles": [
            {
                "roleName": "readWrite",
                "databaseName": db_name,
            }
        ],
        "description": f"OGRRE runtime user for {db_name} (provisioned by GitHub Actions)",
    }
    create_resp = http.post(
        create_url, auth=auth, headers=headers, json=payload, timeout=30
    )
    if create_resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to create MongoDB Atlas user '{username}': "
            f"HTTP {create_resp.status_code}"
        )
    print(
        f"Created MongoDB Atlas user '{username}' scoped to readWrite on database '{db_name}'."
    )


def connect_with_retry(
    username: str,
    password: str,
    db_connection: str,
    db_name: str,
    retries: int = 12,
    delay_seconds: float = 5.0,
):
    import certifi
    from pymongo.mongo_client import MongoClient
    from pymongo.server_api import ServerApi

    if db_connection.startswith("mongodb://") or db_connection.startswith(
        "mongodb+srv://"
    ):
        uri = db_connection
        kwargs = {"server_api": ServerApi("1")}
    else:
        enc_user = urllib.parse.quote_plus(username)
        enc_pass = urllib.parse.quote_plus(password)
        enc_host = urllib.parse.quote_plus(db_connection)
        uri = f"mongodb+srv://{enc_user}:{enc_pass}@{enc_host}.mongodb.net/?retryWrites=true&w=majority"
        kwargs = {"server_api": ServerApi("1"), "tlsCAFile": certifi.where()}

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=10000, **kwargs)
            client.admin.command("ping")
            return client[db_name]
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                print(
                    f"Waiting for MongoDB Atlas user propagation (attempt {attempt}/{retries})..."
                )
                time.sleep(delay_seconds)
    raise RuntimeError(
        f"Unable to connect to MongoDB after {retries} attempts: {last_error}"
    )


def append_db_env(
    env_file: str,
    db_name: str,
    db_connection: str,
    db_username: str,
    db_password: str,
) -> None:
    with open(env_file, "a", encoding="utf-8") as handle:
        handle.write(f"DB_NAME={db_name}\n")
        handle.write(f"DB_CONNECTION={db_connection}\n")
        handle.write(f"DB_USERNAME={db_username}\n")
        handle.write(f"DB_PASSWORD={db_password}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision MongoDB Atlas user and initialize empty OGRRE database."
    )
    parser.add_argument("--collaborator", required=True, help="Collaborator key")
    parser.add_argument(
        "--env-file",
        default="",
        help="Target runtime .env file path (required unless --dry-run)",
    )
    parser.add_argument("--db-name", default="", help="Override database name")
    parser.add_argument("--team-name", default="", help="Override initial team name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify Atlas API authentication and confirm the target user does not exist without creating anything",
    )
    args = parser.parse_args()

    collaborator = args.collaborator.strip().lower()
    if not collaborator:
        raise ValueError("collaborator is required")
    if not args.dry_run and not args.env_file.strip():
        raise ValueError("--env-file is required unless --dry-run is set")

    project_id = (os.getenv("ATLAS_PROJECT_ID") or "").strip()
    public_key = (os.getenv("ATLAS_PUBLIC_KEY") or "").strip()
    private_key = (os.getenv("ATLAS_PRIVATE_KEY") or "").strip()
    client_id = (os.getenv("ATLAS_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("ATLAS_CLIENT_SECRET") or "").strip()
    db_connection = (os.getenv("DB_CONNECTION") or "").strip()
    admin_emails = parse_admin_emails(os.getenv("DEFAULT_ADMIN_EMAILS") or "")

    has_auth = bool((public_key and private_key) or (client_id and client_secret))
    if not (project_id and has_auth and db_connection):
        raise RuntimeError(
            "Missing required Atlas provisioning environment variables "
            "(ATLAS_PROJECT_ID, ATLAS_PUBLIC_KEY/ATLAS_PRIVATE_KEY or "
            "ATLAS_CLIENT_ID/ATLAS_CLIENT_SECRET, DB_CONNECTION)."
        )

    db_name = args.db_name.strip() or f"ogrre-{collaborator}"
    username = f"ogrre-{collaborator}"
    team_name = args.team_name.strip() or collaborator.upper()

    if args.dry_run:
        import requests

        auth, extra_headers = _build_atlas_auth(
            public_key=public_key,
            private_key=private_key,
            client_id=client_id,
            client_secret=client_secret,
            http=requests,
        )
        headers = {
            "Accept": ATLAS_ACCEPT_HEADER,
            "Content-Type": "application/json",
            **extra_headers,
        }
        encoded_user = urllib.parse.quote(username, safe="")
        get_url = (
            f"{ATLAS_API_BASE}/groups/{project_id}/databaseUsers/admin/{encoded_user}"
        )
        resp = requests.get(get_url, auth=auth, headers=headers, timeout=30)
        if resp.status_code == 200:
            print(
                f"[DRY RUN] Atlas API auth succeeded; user '{username}' already exists."
            )
        elif resp.status_code == 404:
            print(
                f"[DRY RUN] Atlas API auth succeeded; user '{username}' does not exist and is ready to provision for database '{db_name}' (team '{team_name}', admins: {', '.join(admin_emails)})."
            )
        else:
            raise RuntimeError(
                f"[DRY RUN] Atlas API check returned unexpected status: HTTP {resp.status_code}"
            )
        return 0

    password = generate_password()

    create_atlas_database_user(
        project_id=project_id,
        public_key=public_key,
        private_key=private_key,
        client_id=client_id,
        client_secret=client_secret,
        db_name=db_name,
        username=username,
        password=password,
    )

    db = connect_with_retry(
        username=username,
        password=password,
        db_connection=db_connection,
        db_name=db_name,
    )
    initialize_empty_database(db, team_name=team_name, admin_emails=admin_emails)
    append_db_env(
        env_file=args.env_file,
        db_name=db_name,
        db_connection=db_connection,
        db_username=username,
        db_password=password,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
