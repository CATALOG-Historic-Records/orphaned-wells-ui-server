#################################################################################

#################################################################################
"""
Project setup with setuptools
"""

from setuptools import setup, find_packages
from pathlib import Path

cwd = Path(__file__).parent
long_description = (cwd / "README.md").read_text()

# Arguments marked as "Required" below must be included for upload to PyPI.
# Fields marked as "Optional" may be commented out.

setup(
    name="orphaned-wells-ui-server",
    url="https://github.com/CATALOG-Historic-Records/orphaned-wells-ui-server",
    version="0.1.dev",
    description="Orphaned Wells UI Backend Server",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="UOW team",
    license="",
    # Classifiers help users find your project by categorizing it.
    #
    # For a list of valid classifiers, see https://pypi.org/classifiers/
    classifiers=[
        "Natural Language :: English",
        "Operating System :: MacOS",
        "Operating System :: Microsoft :: Windows",
        "Operating System :: Unix",
        "Programming Language :: Python",
    ],
    keywords="orphaned wells",
    packages=find_packages(
        include=("backend*",),
    ),
    python_requires=">=3.8",
    install_requires=[
        "pymongo>3",  # database interface
        "annotated-types",
        "aiofiles",
        "aiohttp",
        "anyio",
        "click",
        "dnspython",
        "exceptiongroup",
        "fastapi",
        "gcloud-aio-storage",
        "google-cloud-storage",
        "google-cloud-documentai",
        "h11",
        "idna",
        "pillow",
        "pydantic>2",
        "pydantic_core",
        "pydantic_settings",
        "PyMuPDF",
        "python-dotenv",
        "python-multipart",
        "sniffio",
        "starlette",
        "typing_extensions",
        "uvicorn",
    ],
    extras_require={
        "dev": [
            "pytest",
        ],
    },
)
