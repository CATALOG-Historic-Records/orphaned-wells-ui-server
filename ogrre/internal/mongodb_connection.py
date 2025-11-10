import os
import certifi
import urllib.parse
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

DB_USERNAME = os.getenv("DB_USERNAME")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
DB_CONNECTION = os.getenv("DB_CONNECTION")

ca = certifi.where()


def connectToDatabase():
    username = urllib.parse.quote_plus(DB_USERNAME)
    password = urllib.parse.quote_plus(DB_PASSWORD)
    db_connection = urllib.parse.quote_plus(DB_CONNECTION)

    uri = f"mongodb+srv://{username}:{password}@{db_connection}.mongodb.net/?retryWrites=true&w=majority"
    client = MongoClient(uri, server_api=ServerApi("1"), tlsCAFile=ca)
    # Send a ping to confirm a successful connection
    try:
        client.admin.command("ping")
        print("Successfully connected to MongoDB!")
    except Exception as e:
        print(f"unable to connect to db: {e}")

    db = client[DB_NAME]
    return db
