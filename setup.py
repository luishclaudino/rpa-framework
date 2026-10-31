"""Empacotamento do rpa_3003."""

from __future__ import annotations

import re
from pathlib import Path

from setuptools import find_packages, setup

ROOT: Path = Path(__file__).resolve().parent


def read_version() -> str:
    text = (ROOT / "rpa_3003" / "_version.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__(?::\s*str)?\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise RuntimeError("Versão não encontrada em rpa_3003/_version.py")
    return match.group(1)


def read_requirements(filename: str) -> list[str]:
    lines = (ROOT / filename).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith(("#", "-"))]


setup(
    name="rpa-3003",
    version=read_version(),
    description="Framework RPA Python para automações web e desktop em Windows",
    long_description=(ROOT / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    python_requires=">=3.12",
    packages=find_packages(include=["rpa_3003", "rpa_3003.*"]),
    include_package_data=True,
    package_data={"rpa_3003": ["py.typed", "templates/*/*", "templates/*/**/*"]},
    install_requires=read_requirements("requirements.txt"),
    extras_require={"dev": read_requirements("requirements-dev.txt")},
    entry_points={"console_scripts": ["rpa3003=rpa_3003.cli:main"]},
    classifiers=[
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Operating System :: Microsoft :: Windows",
        "Typing :: Typed",
    ],
)
