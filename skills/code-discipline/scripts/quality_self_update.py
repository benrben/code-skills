"""Update the quality engine from GitHub.

Three layouts update differently: a clone of the code-skills repository pulls
with ``git``; an installed skill runs its installer; a standalone copy of the
scripts downloads every module the entry points import (found by following
their imports) and the bundled thresholds into the scripts folder.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from urllib.parse import quote

from quality_tools import download_bytes

GITHUB_REPOSITORY = "benrben/code-skills"
GITHUB_DEFAULT_REF = "main"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}"
SCRIPTS_PATH = "skills/code-discipline/scripts"
THRESHOLDS_PATH = "skills/code-discipline/quality-thresholds.json"
THRESHOLDS_FILE = "quality-thresholds.json"
ENTRY_MODULES = ("repo_quality_gate", "quality_loop")
VERSION_MODULE = "quality_config"

Download = Callable[[str], bytes]
Run = Callable[..., subprocess.CompletedProcess[str]]


def validate_update_ref(reference: str) -> str:
    if (
        not reference
        or reference.startswith("/")
        or ".." in reference.split("/")
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", reference)
    ):
        raise ValueError(
            "GitHub update ref may contain only letters, digits, '.', '_', '-', and '/'"
        )
    return reference


def github_raw_url(reference: str, relative_path: str) -> str:
    safe_reference = quote(validate_update_ref(reference), safe="/")
    return f"{GITHUB_RAW_BASE}/{safe_reference}/{quote(relative_path, safe='/')}"


def update_from_github(
    runner_path: Path,
    reference: str,
    *,
    download: Download = download_bytes,
    run: Run = subprocess.run,
) -> int:
    """Update the engine at ``runner_path``; the exit code to return."""
    try:
        reference = validate_update_ref(reference)
        checkout = shared_checkout_root(runner_path, run)
        if checkout is not None:
            return update_checkout(checkout, reference, run)
        installer = runner_path.with_name("install.py")
        if (runner_path.parent.parent / "SKILL.md").is_file() and installer.is_file():
            command = [sys.executable, str(installer), "--update-current", "--ref"]
            return run([*command, reference], text=True, check=False).returncode
        version = update_standalone(runner_path.parent, reference, download)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"Updated the standalone quality engine to {version} from {reference}.")
    print("Repository-owned .quality/ files were not changed.")
    return 0


# --- Shared checkout -----------------------------------------------------------


def normalized_git_remote(value: str) -> str:
    normalized = value.strip().removesuffix(".git").removesuffix("/")
    if normalized.startswith("git@github.com:"):
        normalized = "https://github.com/" + normalized.removeprefix("git@github.com:")
    return normalized.lower()


def _git(
    run: Run, directory: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return run(
        ["git", "-C", str(directory), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def shared_checkout_root(runner_path: Path, run: Run) -> Path | None:
    """The code-skills clone that holds ``runner_path``, if it is one."""
    if not shutil.which("git"):
        return None
    top = _git(run, runner_path.parent, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    root = Path(top.stdout.strip()).resolve()
    if runner_path.resolve() != root / SCRIPTS_PATH / "repo_quality_gate.py":
        return None
    remote = _git(run, root, "remote", "get-url", "origin")
    expected = normalized_git_remote(f"https://github.com/{GITHUB_REPOSITORY}")
    if remote.returncode != 0 or normalized_git_remote(remote.stdout) != expected:
        return None
    return root


def update_checkout(checkout: Path, reference: str, run: Run) -> int:
    status = _git(run, checkout, "status", "--porcelain")
    if status.returncode != 0:
        print(status.stderr.strip() or "error: git status failed", file=sys.stderr)
        return 2
    if status.stdout.strip():
        print(
            "error: the code-skills checkout has local changes; commit or stash "
            "them before updating",
            file=sys.stderr,
        )
        return 2
    pulled = _git(run, checkout, "pull", "--ff-only", "origin", reference)
    if pulled.returncode != 0:
        print(pulled.stderr.strip() or "error: git pull failed", file=sys.stderr)
        return 2
    print(f"Updated the code-skills checkout from GitHub ref {reference}.")
    return 0


# --- Standalone scripts --------------------------------------------------------


def local_imports(source: str, filename: str) -> set[str]:
    """Top-level names of non-standard-library modules ``source`` imports."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as error:
        raise ValueError(
            f"downloaded {filename} is not valid Python: {error}"
        ) from error
    return {name for name in _imported_names(tree) if not is_standard_module(name)}


def is_standard_module(name: str) -> bool:
    """True for a standard-library module. Python 3.9 has no
    ``sys.stdlib_module_names``, so there a module counts as standard when it
    is built in or loads from the standard library folder."""
    if name == "__future__" or name in sys.builtin_module_names:
        return True
    names: frozenset[str] | None = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return name in names
    spec = importlib.util.find_spec(name)
    origin = spec.origin if spec is not None else None
    stdlib = sysconfig.get_paths()["stdlib"]
    return (
        bool(origin)
        and str(origin).startswith(stdlib)
        and ("site-packages" not in str(origin))
    )


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


def download_modules(
    reference: str, download: Download, entries: Iterable[str] = ENTRY_MODULES
) -> dict[str, bytes]:
    """Every module the entry points need, keyed by file name."""
    pending = list(entries)
    payloads: dict[str, bytes] = {}
    while pending:
        name = pending.pop()
        filename = f"{name}.py"
        if filename in payloads:
            continue
        payload = download(github_raw_url(reference, f"{SCRIPTS_PATH}/{filename}"))
        payloads[filename] = payload
        source = payload.decode("utf-8", errors="replace")
        pending.extend(sorted(local_imports(source, filename)))
    return payloads


def declared_version(payload: bytes) -> str:
    tree = ast.parse(payload.decode("utf-8", errors="replace"))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        named = any(
            isinstance(target, ast.Name) and target.id == "VERSION"
            for target in node.targets
        )
        if named and isinstance(node.value.value, str):
            return node.value.value
    raise ValueError(f"the downloaded {VERSION_MODULE}.py declares no VERSION")


def validate_thresholds_payload(payload: bytes) -> None:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"the downloaded thresholds are not JSON: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("schema_version"), int):
        raise ValueError("the downloaded thresholds have no schema_version")


def update_standalone(directory: Path, reference: str, download: Download) -> str:
    """Download and install the scripts and thresholds; the new version."""
    files = download_modules(reference, download)
    version_file = f"{VERSION_MODULE}.py"
    if version_file not in files:
        files[version_file] = download(
            github_raw_url(reference, f"{SCRIPTS_PATH}/{version_file}")
        )
    version = declared_version(files[version_file])
    thresholds = download(github_raw_url(reference, THRESHOLDS_PATH))
    validate_thresholds_payload(thresholds)
    install_files(directory, {**files, THRESHOLDS_FILE: thresholds})
    return version


def install_files(directory: Path, files: Mapping[str, bytes]) -> None:
    """Replace every file, or none: a failure restores the originals."""
    originals = {
        name: (directory / name).read_bytes()
        for name in files
        if (directory / name).is_file()
    }
    try:
        for name, payload in files.items():
            replace_bytes(directory / name, payload)
    except OSError as error:
        _restore(directory, files, originals)
        raise ValueError(f"could not install the update: {error}") from error


def _restore(
    directory: Path, files: Iterable[str], originals: Mapping[str, bytes]
) -> None:
    for name in files:
        path = directory / name
        if name in originals:
            replace_bytes(path, originals[name])
        else:
            path.unlink(missing_ok=True)


def replace_bytes(path: Path, payload: bytes) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
