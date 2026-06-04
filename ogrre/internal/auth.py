import os
from dotenv import load_dotenv


# fetch environment variables
load_dotenv()


def get_google_credentials():
    token_uri = os.getenv("token_uri")
    client_id = os.getenv("client_id")
    client_secret = os.getenv("client_secret")
    return token_uri, client_id, client_secret


def get_cilogon_credentials():
    token_uri = os.getenv("cilogon_token_uri")
    client_id = os.getenv("cilogon_client_id")
    client_secret = os.getenv("cilogon_client_secret")
    return token_uri, client_id, client_secret


def parse_allowed_origins():
    """
    Parse ALLOWED_ORIGINS env var into a clean list.

    Supported formats:
      - comma-separated list:
          "https://a.example.com,https://b.example.com"
      - JSON array:
          ["https://a.example.com", "https://b.example.com"]
    """
    raw = os.getenv("ALLOWED_ORIGINS", "")
    if not raw:
        return []

    cleaned = raw.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        try:
            import json

            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return [str(origin).strip() for origin in parsed if str(origin).strip()]
        except Exception:
            pass

    return [origin.strip() for origin in cleaned.split(",") if origin.strip()]
