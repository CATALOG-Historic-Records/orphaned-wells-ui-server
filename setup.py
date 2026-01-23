#################################################################################

#################################################################################
"""
Project setup with setuptools
"""

from setuptools import setup, find_packages
from pathlib import Path

cwd = Path(__file__).parent
long_description = (cwd / "README.md").read_text()


def read_requirements(path: Path):
    reqs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("-r "):
            nested = line.split(maxsplit=1)[1]
            reqs.extend(read_requirements(cwd / nested))
            continue

        if line.startswith("-"):
            continue
        reqs.append(line)
    print(f"reqs: {reqs}")
    return reqs


setup(
    name="orphaned-wells-ui-server",
    url="https://github.com/CATALOG-Historic-Records/orphaned-wells-ui-server",
    version="1.0.dev",
    description="Orphaned Wells UI Backend Server",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Michael Pesce",
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
        include=("ogrre*",),
    ),
    python_requires=">=3.8",
    install_requires=read_requirements(cwd / "requirements.txt"),
    extras_require={
        "dev": [
            "pytest",
        ],
    },
)
