import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
import multiprocessing
import logging
from dotenv import load_dotenv
import argparse

from dotenv import load_dotenv

# fetch environment variables
load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")
STORAGE_SERVICE_KEY = os.getenv("STORAGE_SERVICE_KEY")

if PROJECT_ID:
    os.environ["GCLOUD_PROJECT"] = PROJECT_ID

if STORAGE_SERVICE_KEY:
    dirname = os.path.dirname(os.path.abspath(__file__))
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = f"{dirname}/{STORAGE_SERVICE_KEY}"

_log = logging.getLogger(__name__)

from ogrre.routers import router
from ogrre.internal import storage_api

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router.router)

if storage_api.STORAGE_BACKEND == "local":
    os.makedirs(storage_api.LOCAL_STORAGE_ROOT, exist_ok=True)
    app.mount(
        "/local-storage",
        StaticFiles(directory=storage_api.LOCAL_STORAGE_ROOT, check_dir=False),
        name="local-storage",
    )

load_dotenv()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-p",
        "--production",
        action="store_true",
        help="Run backend in production mode.",
    )
    parser.add_argument(
        "-d",
        "--docker",
        action="store_true",
        help="Run backend in mode fit for docker.",
    )
    args = parser.parse_args()
    multiprocessing.freeze_support()
    if args.production:
        _log.info(f"starting app in prod")
        uvicorn.run(
            "__main__:app", host="127.0.0.1", port=8001, reload=False, workers=8
        )
    elif args.docker:
        _log.info(f"starting app in docker")
        uvicorn.run("__main__:app", host="0.0.0.0", port=8001, reload=False, workers=8)
    else:
        _log.info(f"starting app in dev")
        multiprocessing.freeze_support()
        uvicorn.run("__main__:app", host="127.0.0.1", port=8001, reload=True)
