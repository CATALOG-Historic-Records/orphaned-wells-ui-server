import os
from dotenv import load_dotenv


# fetch environment variables
load_dotenv()


def get_google_credentials():
    token_uri = os.getenv("token_uri")
    client_id = os.getenv("client_id")
    client_secret = os.getenv("client_secret")
    return token_uri, client_id, client_secret
