"""The repository's own paperwork: package metadata, and the files a visitor reads first.

These pin what the public repository promises, so a later edit can't quietly
drop the security notes, break a link or let the changelog fall behind the
version.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PUBLIC = "https://github.com/BenCodeland/till"

PAPERWORK = [
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "ROADMAP.md",
    ".github/ISSUE_TEMPLATE/bug_report.md",
    ".github/ISSUE_TEMPLATE/feature_request.md",
    ".github/ISSUE_TEMPLATE/config.yml",
]

# Inline links, with or without a title, and reference definitions.
LINK = re.compile(r"\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)|^\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", re.M)


def project() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text())["project"]


@pytest.mark.parametrize("name", PAPERWORK)
def test_the_paperwork_is_there(name):
    path = REPO / name
    assert path.is_file(), f"{name} is missing"
    assert path.read_text(encoding="utf-8").strip(), f"{name} is empty"


def test_the_package_points_at_its_readme():
    readme = project()["readme"]
    assert readme == "README.md"
    assert (REPO / readme).is_file()


def test_the_author_is_named_without_an_email():
    assert project()["authors"] == [{"name": "Ben C"}]


def test_the_package_links_go_to_the_public_repository():
    urls = project()["urls"]
    assert set(urls) == {"Homepage", "Source", "Issues", "Changelog"}
    for label, url in urls.items():
        assert url == PUBLIC or url.startswith(PUBLIC + "/"), f"{label}: {url}"


def test_the_classifiers_say_linux_and_the_tested_pythons():
    classifiers = project()["classifiers"]
    assert "Operating System :: POSIX :: Linux" in classifiers
    for minor in ("3.11", "3.12", "3.13"):
        assert f"Programming Language :: Python :: {minor}" in classifiers
    # The licence is given as an SPDX expression; a licence classifier beside
    # it is an error for the build backend (PEP 639).
    assert not [c for c in classifiers if c.startswith("License ::")]


def test_the_changelog_starts_at_the_current_version():
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)
    assert versions, "no released version in CHANGELOG.md"
    assert versions[0] == project()["version"]
    assert versions[-1] == "0.1.0"


def test_the_security_notes_state_the_model():
    text = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    assert "Till listens on `127.0.0.1` only." in text
    assert "**There is no login.**" in text
    assert f"{PUBLIC}/security/advisories/new" in text


def test_contributing_says_how_outside_changes_land():
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "pytest" in text
    assert "not merged" in text


def test_the_roadmap_is_written_for_strangers():
    """Nothing from the private working notes: no task numbers, no review."""
    text = (REPO / "ROADMAP.md").read_text(encoding="utf-8").lower()
    for private in ("not planned", "review", "superpowers", "design note"):
        assert not re.search(rf"\b{private}", text), private
    assert not re.search(r"\btasks?\s*[-#]?\s*\d", text)


def markdown_files():
    yield from sorted(REPO.glob("*.md"))
    yield from sorted((REPO / ".github").rglob("*.md"))


@pytest.mark.parametrize("path", list(markdown_files()), ids=lambda p: str(p.relative_to(REPO)))
def test_relative_links_resolve(path):
    broken = []
    for match in LINK.finditer(path.read_text(encoding="utf-8")):
        target = match.group(1) or match.group(2)
        if re.match(r"[a-z]+:", target) or target.startswith("#"):
            continue
        if not (path.parent / target.split("#")[0]).exists():
            broken.append(target)
    assert not broken, f"{path.name} links to missing files: {broken}"
