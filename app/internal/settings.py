"""
Configuration for the backend
"""
from pathlib import Path
import logging
from typing import List, Union
from pydantic import field_validator
from pydantic_settings import BaseSettings


class AppSettings(BaseSettings):
    log_dir: Union[Path, None] = None
    img_dir: Union[Path, None] = None
    csv_dir: Union[Path, None] = None

    @field_validator("log_dir")
    def validate_log_dir(cls, v):
        if v is None:
            v = Path.home() / ".uow" / "logs"
        v.mkdir(parents=True, exist_ok=True)
        loggingFormat = "[%(levelname)s] %(asctime)s %(name)s (%(filename)s:%(lineno)s): %(message)s"
        loggingFileHandler = logging.handlers.RotatingFileHandler(
            v / "orphaned-wells-ui.log", backupCount=2, maxBytes=5000000
        )
        logging.basicConfig(
            level=logging.INFO,
            format=loggingFormat,
            handlers=[loggingFileHandler, logging.StreamHandler()],
        )
        return v

    @field_validator("img_dir")
    def validate_img_dir(cls, v):
        if v is None:
            v = Path.home() / ".uow" / "uploaded_images"
        v.mkdir(parents=True, exist_ok=True)
        return v
    
    @field_validator("csv_dir")
    def validate_csv_dir(cls, v):
        if v is None:
            v = Path.home() / ".uow" / "csv_outputs"
        v.mkdir(parents=True, exist_ok=True)
        return v
