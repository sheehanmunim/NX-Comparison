#!/usr/bin/env python3

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_BACKEND_VENVS = (
    ".venv-stepocc312",
    ".venv-stepocc311",
    ".venv-stepocc310",
    ".venv-stepocc",
    ".venv-fusion-ml",
)
REQUIRED_IMPORTS = ("OCP", "sklearn", "numpy", "scipy")
BACKEND_PACKAGES = ("cadquery-ocp", "cadquery", "numpy", "scikit-learn")


def _venv_python_path(venv_root: Path) -> Path:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return venv_root / scripts_dir / executable


def _ensure_stable_venv_alias(repo_root: Path, target_venv: Path, alias_name: str = ".venv-stepocc") -> None:
    alias_path = repo_root / alias_name
    try:
        if alias_path.resolve() == target_venv.resolve():
            return
    except OSError:
        pass

    if alias_path.exists() or alias_path.is_symlink():
        if alias_path.is_symlink():
            try:
                alias_path.unlink()
            except OSError:
                return
        else:
            return

    try:
        relative_target = Path(os.path.relpath(target_venv, start=repo_root))
        alias_path.symlink_to(relative_target, target_is_directory=True)
    except OSError:
        return


def _apply_backend_runtime_env(repo_root: Path, runtime_root: Path, *, overwrite: bool) -> None:
    _ensure_stable_venv_alias(repo_root, runtime_root)
    if overwrite:
        os.environ["XT_STEP_OCC_VENV"] = str(runtime_root)
        os.environ["XT_FUSION_ML_VENV"] = str(runtime_root)
    else:
        os.environ.setdefault("XT_STEP_OCC_VENV", str(runtime_root))
        os.environ.setdefault("XT_FUSION_ML_VENV", str(runtime_root))


def _run_capture(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )


def _python_can_import(python_executable: Path, modules: tuple[str, ...]) -> bool:
    if not python_executable.exists():
        return False
    script = (
        "import importlib.util, sys;"
        f"mods={list(modules)!r};"
        "missing=[name for name in mods if importlib.util.find_spec(name) is None];"
        "sys.exit(0 if not missing else 1)"
    )
    completed = _run_capture([str(python_executable), "-c", script])
    return completed.returncode == 0


def _existing_ready_venv(repo_root: Path) -> Path | None:
    for name in DEFAULT_BACKEND_VENVS:
        venv_root = repo_root / name
        python_executable = _venv_python_path(venv_root)
        if _python_can_import(python_executable, REQUIRED_IMPORTS):
            return venv_root
    return None


def _current_ready_venv() -> Path | None:
    candidates: list[Path] = []
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        candidates.append(Path(virtual_env))

    current_python = Path(sys.executable).resolve()
    if current_python.parent.name in {"bin", "Scripts"}:
        candidates.append(current_python.parent.parent)

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        python_executable = _venv_python_path(resolved)
        if _python_can_import(python_executable, REQUIRED_IMPORTS):
            return resolved
    return None


def _python_command_candidates() -> list[tuple[str, list[str], str]]:
    candidates: list[tuple[str, list[str], str]] = []
    explicit = os.environ.get("XT_BACKEND_BOOTSTRAP_PYTHON")
    if explicit:
        candidates.append(("explicit", [explicit], "explicit"))

    for name, version_label in (("python3.12", "3.12"), ("python3.11", "3.11"), ("python3.10", "3.10")):
        path = shutil.which(name)
        if path:
            candidates.append((name, [path], version_label))

    py_launcher = shutil.which("py")
    if py_launcher:
        for version_label in ("3.12", "3.11", "3.10"):
            candidates.append((f"py-{version_label}", [py_launcher, f"-{version_label}"], version_label))

    current_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if sys.version_info[:2] <= (3, 12):
        candidates.append(("current", [sys.executable], current_version))

    deduped: list[tuple[str, list[str], str]] = []
    seen: set[tuple[str, ...]] = set()
    for label, command, version_label in candidates:
        marker = tuple(command)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append((label, command, version_label))
    return deduped


def _pick_bootstrap_python() -> tuple[list[str], str] | None:
    for _label, command, version_label in _python_command_candidates():
        completed = _run_capture(command + ["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"])
        if completed.returncode != 0:
            continue
        reported = completed.stdout.strip()
        if reported.startswith("3.12"):
            return command, "3.12"
        if reported.startswith("3.11"):
            return command, "3.11"
        if reported.startswith("3.10"):
            return command, "3.10"
    return None


def _target_venv_name(version_label: str) -> str:
    if version_label.startswith("3.12"):
        return ".venv-stepocc312"
    if version_label.startswith("3.11"):
        return ".venv-stepocc311"
    if version_label.startswith("3.10"):
        return ".venv-stepocc310"
    return ".venv-stepocc"


def _install_backend_packages(
    python_command: list[str],
    target_venv: Path,
    *,
    verbose,
) -> tuple[bool, str]:
    if not _venv_python_path(target_venv).exists():
        verbose(f"Creating local backend runtime in {target_venv.name}...")
        completed = subprocess.run(python_command + ["-m", "venv", str(target_venv)], text=True, check=False)
        if completed.returncode != 0:
            return False, f"Failed to create backend virtual environment at {target_venv}."

    venv_python = _venv_python_path(target_venv)
    verbose("Installing STEP and ML backend packages. This can take a minute on first run...")
    completed = subprocess.run(
        [str(venv_python), "-m", "pip", "install", *BACKEND_PACKAGES],
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return False, "Package installation failed for the local backend runtime."

    if not _python_can_import(venv_python, REQUIRED_IMPORTS):
        return False, "Backend runtime was created, but required packages are still missing."

    return True, f"Backend runtime is ready in {target_venv.name}."


def ensure_backend_runtimes(*, verbose=print) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parent
    if os.environ.get("XT_SKIP_BACKEND_BOOTSTRAP"):
        return {
            "ready": False,
            "auto_install_attempted": False,
            "message_already_reported": False,
            "message": "Backend bootstrap skipped via XT_SKIP_BACKEND_BOOTSTRAP.",
        }

    ready_venv = _existing_ready_venv(repo_root)
    if ready_venv is not None:
        _apply_backend_runtime_env(repo_root, ready_venv, overwrite=False)
        return {
            "ready": True,
            "auto_install_attempted": False,
            "message_already_reported": False,
            "venv": str(ready_venv),
            "message": f"Using backend runtime from {ready_venv.name}.",
        }

    current_venv = _current_ready_venv()
    if current_venv is not None:
        _apply_backend_runtime_env(repo_root, current_venv, overwrite=False)
        return {
            "ready": True,
            "auto_install_attempted": False,
            "message_already_reported": False,
            "venv": str(current_venv),
            "message": f"Using active backend runtime from {current_venv.name}.",
        }

    bootstrap = _pick_bootstrap_python()
    if bootstrap is None:
        message = (
            "Could not find a compatible Python 3.10-3.12 interpreter for automatic STEP/ML backend setup. "
            "The app will still run, but STEP tessellation and strong ML fusion may fall back."
        )
        verbose(message)
        return {
            "ready": False,
            "auto_install_attempted": False,
            "message_already_reported": True,
            "message": message,
        }

    python_command, version_label = bootstrap
    target_venv = repo_root / _target_venv_name(version_label)
    ok, message = _install_backend_packages(python_command, target_venv, verbose=verbose)
    verbose(message)
    if ok:
        _apply_backend_runtime_env(repo_root, target_venv, overwrite=True)
    return {
        "ready": ok,
        "auto_install_attempted": True,
        "message_already_reported": True,
        "venv": str(target_venv),
        "message": message,
    }
