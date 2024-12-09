import sys
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import multiprocessing
import logging
from dotenv import load_dotenv
import argparse

_log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from app.routers import router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router.router)

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
