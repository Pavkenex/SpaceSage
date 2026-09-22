"""Scaffold smoke tests: package metadata, CLI entry points, CI configuration."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
import yaml

import spacesage
from spacesage.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``python -m spacesage`` in a subprocess and capture its output."""
    return subprocess.run(
        [sys.executable, "-m", "spacesage", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def load_ci_workflow() -> dict[str, object]:
    """Parse the CI workflow with PyYAML (guards against YAML syntax rot)."""
    assert CI_WORKFLOW.is_file(), f"missing CI workflow: {CI_WORKFLOW}"
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


# --------------------------------------------------------------------------- #
# Package
# --------------------------------------------------------------------------- #


def test_version_constant() -> None:
    # Pinned to the released version: the release workflow refuses a tag that
    # does not match what the package reports, so this is the CI-side half of
    # that check (v0.1.1 is the tag of the release that first shipped the
    # OpenCode Zen preset and the Windows path/hard-link fixes).
    assert spacesage.__version__ == "0.1.1"


def test_installed_metadata_matches_version_constant() -> None:
    assert metadata.version("spacesage") == spacesage.__version__


def test_package_ships_py_typed_marker() -> None:
    package_dir = Path(spacesage.__file__).parent
    assert (package_dir / "py.typed").is_file()


def test_console_script_entry_point_is_declared() -> None:
    scripts = {entry.name: entry.value for entry in metadata.entry_points(group="console_scripts")}
    assert scripts.get("spacesage") == "spacesage.cli:main"


def test_runtime_dependencies_are_empty() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_module_version_flag_prints_version() -> None:
    result = run_cli("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"spacesage {spacesage.__version__}"


def test_module_without_arguments_prints_help() -> None:
    result = run_cli()
    assert result.returncode == 0
    assert "usage: spacesage" in result.stdout


def test_main_returns_zero_and_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: spacesage" in capsys.readouterr().out


def test_main_version_flag_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


# --------------------------------------------------------------------------- #
# CI workflow
# --------------------------------------------------------------------------- #


def test_ci_workflow_parses_and_defines_required_jobs() -> None:
    workflow = load_ci_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert {"ruff", "mypy", "pytest"} <= set(jobs)


def test_ci_pytest_matrix_covers_platforms_and_python_versions() -> None:
    workflow = load_ci_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    matrix = jobs["pytest"]["strategy"]["matrix"]  # type: ignore[index]
    assert matrix["os"] == ["ubuntu-latest", "windows-latest"]
    assert matrix["python-version"] == ["3.11", "3.13"]
