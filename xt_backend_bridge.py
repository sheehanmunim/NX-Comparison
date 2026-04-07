#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from xt_backend_setup import ensure_backend_runtimes


def _venv_python_path(venv_root: Path) -> Path:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return venv_root / scripts_dir / executable


def _backend_python(env_var: str) -> Path | None:
    ensure_backend_runtimes(verbose=lambda *_args, **_kwargs: None)
    raw = os.environ.get(env_var)
    if not raw:
        return None
    root = Path(raw)
    python_path = _venv_python_path(root)
    return python_path if python_path.exists() else None


def call_backend_worker(action: str, payload: dict[str, Any], *, env_var: str) -> dict[str, Any] | None:
    python_path = _backend_python(env_var)
    if python_path is None:
        return None

    worker_path = Path(__file__).resolve().with_name("xt_backend_worker.py")
    completed = subprocess.run(
        [str(python_path), str(worker_path), action],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        message = stderr or stdout or f"Backend worker failed for action {action}."
        raise RuntimeError(message)

    raw_output = completed.stdout or "{}"
    start = raw_output.find("{")
    end = raw_output.rfind("}")
    candidate = raw_output[start : end + 1] if start != -1 and end != -1 and end >= start else raw_output
    try:
        return json.loads(candidate or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Backend worker returned invalid JSON for {action}: {exc}") from exc


def step_occ_preview_from_text(raw_text: str, *, display_name: str) -> dict[str, Any] | None:
    suffix = Path(display_name or "model.stp").suffix or ".stp"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as handle:
        handle.write(raw_text)
        temp_path = Path(handle.name)

    try:
        return call_backend_worker(
            "step_occ_preview",
            {
                "path": str(temp_path),
                "display_name": display_name,
            },
            env_var="XT_STEP_OCC_VENV",
        )
    finally:
        temp_path.unlink(missing_ok=True)


def predict_match_probabilities(
    *,
    training_features: list[list[float]],
    training_labels: list[int],
    predict_features: list[list[float]],
    model_name: str,
) -> dict[str, Any] | None:
    if not training_features or not predict_features:
        return None

    return call_backend_worker(
        "ml_match_probabilities",
        {
            "training_features": training_features,
            "training_labels": training_labels,
            "predict_features": predict_features,
            "model_name": model_name,
        },
        env_var="XT_FUSION_ML_VENV",
    )
