"""Guards for the things a public repository has to get right, and keep right.

This project is published; the paperwork that makes that safe — a license, a security policy that
separates the deliberate flaw from an unintended one, contribution guidance, and a README that
states the boundary before a stranger runs anything — is as much a deliverable as the code, and it
rots exactly as quietly.

The leak tripwires below are written **by shape, never by name**. A test that hard-codes a private
identifier in order to forbid it has published that identifier, and once merged the match
survives in the provider's retained pull-request refs, where no history rewrite can reach it. So
the guards match patterns instead: a repository reference that is not this repository, an absolute
path out of an author's home directory. They carry no secret of their own.

Their reach is the files this repository ships, which is what a regression test can see.
Reachable Git history, branches, tags, provider metadata, issues, pull requests, Actions logs, and
retained run artifacts are audited by the publication gate before the visibility change, not from
in here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

# Anchored on this file rather than on the installed package: the verification image installs
# `racejack` into the interpreter's own site-packages, so the package location says nothing about
# where the repository content was copied.
REPO_ROOT = Path(__file__).resolve().parent.parent

OWNER = "maximalfocus"
PROJECT = "racejack"
COPYRIGHT_YEAR = "2026"

# The only repository any shipped file may name. Anything else — a private companion in
# particular — fails, without this file ever writing down what that companion is called.
PERMITTED_REPOSITORIES = frozenset({f"{OWNER}/{PROJECT}"})

REPOSITORY_URL = re.compile(r"github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)")
OWNER_QUALIFIED_NAME = re.compile(rf"\b{re.escape(OWNER)}/[A-Za-z0-9._-]+")
AUTHOR_HOME_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")

# Files the public repository presents. Listed rather than globbed so that a file silently missing
# from the image is a failure instead of a scan that quietly covers nothing.
PUBLIC_DOCUMENTS = (
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "WALKTHROUGH.md",
    "pyproject.toml",
    "docker-compose.yml",
)


def _document(name: str) -> str:
    path = REPO_ROOT / name
    assert path.is_file(), f"{name} is missing from the repository the tests can see"
    return path.read_text()


def _prose(name: str) -> str:
    """The document with its hard wrapping collapsed.

    Every document here is wrapped at 100 columns, so a phrase assertion made against the raw
    text fails the moment a paragraph is rewrapped — a false alarm that teaches contributors to
    weaken the assertion. Collapsing the whitespace keeps the guard about content.
    """
    return " ".join(_document(name).split())


def _shipped_files() -> list[Path]:
    """Every file this repository ships that the verification image can reach."""
    files = [REPO_ROOT / name for name in PUBLIC_DOCUMENTS]
    for directory, pattern in (("src", "*.py"), ("tests", "*.py"), ("scripts", "*.sh")):
        files.extend(sorted((REPO_ROOT / directory).rglob(pattern)))
    return [path for path in files if path.is_file()]


# --- FR-026: the license ------------------------------------------------------------------------


def test_the_license_is_the_canonical_mit_text() -> None:
    assert _document("LICENSE").startswith("MIT License")
    text = _prose("LICENSE")
    for clause in (
        "Permission is hereby granted, free of charge, to any person obtaining a copy",
        "without restriction, including without limitation the rights",
        "The above copyright notice and this permission notice shall be included in all",
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
        "IN NO EVENT SHALL THE",
    ):
        assert clause in text, f"the license is missing a canonical MIT clause: {clause!r}"


def test_the_license_attributes_a_year_and_a_holder() -> None:
    assert f"Copyright (c) {COPYRIGHT_YEAR} {OWNER}" in _prose("LICENSE")


def test_package_metadata_declares_the_same_license_the_repository_carries() -> None:
    """SPDX in the metadata and the file on disk must not be able to drift apart."""
    metadata = tomllib.loads(_document("pyproject.toml"))["project"]
    assert metadata["license"] == "MIT"
    assert "LICENSE" in metadata["license-files"]
    assert (REPO_ROOT / "LICENSE").is_file()


def test_the_readme_names_the_license() -> None:
    assert "MIT" in _prose("README.md")


# --- FR-027: the public-facing description -------------------------------------------------------


@pytest.mark.parametrize(
    ("claim", "phrase"),
    [
        ("educational purpose", "educational material"),
        ("local-only operating boundary", "Docker Compose"),
        ("the intentionally vulnerable component", "intentionally vulnerable"),
        ("no hosted service", "no hosted service"),
        ("no production-safety claim", "production-safety claim"),
        ("the opt-in environment variable", "ALLOW_VULNERABLE_DEMO=true"),
        ("a link to the security policy", "SECURITY.md"),
        ("a link to the contribution guidance", "CONTRIBUTING.md"),
    ],
)
def test_the_readme_states_the_public_boundary(claim: str, phrase: str) -> None:
    assert phrase in _prose("README.md"), f"the README does not state {claim}"


# --- FR-027 / NFR-008: the security policy -------------------------------------------------------


def test_the_security_policy_separates_the_demonstrated_flaw_from_an_unintended_one() -> None:
    text = _prose("SECURITY.md")
    assert "Please do not report" in text, "the policy does not tell a reader what not to report"
    assert "unintended" in text.lower(), "the policy does not name the category that is reportable"
    for reportable in ("secure application's guards", "container", "credential"):
        assert reportable in text, f"the policy does not say {reportable!r} is in scope"


def test_the_security_policy_gives_a_non_public_reporting_path() -> None:
    text = _prose("SECURITY.md")
    assert "private vulnerability reporting" in text.lower()
    assert "Report a vulnerability" in text
    assert "do not open a public issue" in text.lower()


def test_the_security_policy_exposes_no_personal_contact() -> None:
    """NFR-008: responsible participation without publishing a personal credential."""
    text = _prose("SECURITY.md")
    addresses = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    assert addresses == [], f"the security policy publishes a contact address: {addresses}"


def test_the_security_policy_makes_no_support_or_production_promise() -> None:
    text = _prose("SECURITY.md")
    for promise in ("service-level", "support-duration", "compatibility", "production-readiness"):
        assert promise in text, f"the policy does not disclaim a {promise} promise"
    assert "not deployed" in text


# --- FR-027: contribution guidance ---------------------------------------------------------------


def test_the_contribution_guidance_names_the_one_verification_command() -> None:
    assert "bash scripts/verify.sh" in _prose("CONTRIBUTING.md")


def test_the_contribution_guidance_carries_the_hard_constraints() -> None:
    text = _prose("CONTRIBUTING.md")
    for constraint in ("fictional", "ALLOW_VULNERABLE_DEMO=true", "performance claim", "MIT"):
        assert constraint in text, f"the guidance does not state the {constraint!r} constraint"


# --- FR-025 / NFR-006: the leak tripwires, matched by shape --------------------------------------


def test_no_shipped_file_references_a_repository_other_than_this_one() -> None:
    """A private companion is forbidden here without this file ever naming one."""
    offenders: dict[str, list[str]] = {}
    for path in _shipped_files():
        text = path.read_text(errors="replace")
        referenced = REPOSITORY_URL.findall(text) + OWNER_QUALIFIED_NAME.findall(text)
        found = [name for name in referenced if name not in PERMITTED_REPOSITORIES]
        if found:
            offenders[str(path.relative_to(REPO_ROOT))] = sorted(set(found))
    assert offenders == {}, f"shipped files name a repository that is not this one: {offenders}"


def test_no_shipped_file_carries_an_absolute_path_from_an_author_machine() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _shipped_files():
        found = AUTHOR_HOME_PATH.findall(path.read_text(errors="replace"))
        if found:
            offenders[str(path.relative_to(REPO_ROOT))] = sorted(set(found))
    assert offenders == {}, f"shipped files leak a local filesystem path: {offenders}"


def test_the_leak_tripwires_actually_scanned_the_public_documents() -> None:
    """A guard that silently scanned nothing is worse than no guard."""
    scanned = {path.name for path in _shipped_files()}
    for name in PUBLIC_DOCUMENTS:
        assert name in scanned, f"{name} was not reachable, so the leak tripwires never saw it"
    assert len(_shipped_files()) > 40, "the shipped-file scan is far smaller than this repository"
