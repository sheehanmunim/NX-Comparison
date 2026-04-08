#!/usr/bin/env python3

import base64
import json
import os
import re
import webbrowser
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xt_backend_bridge import predict_match_probabilities
from xt_backend_setup import ensure_backend_runtimes
from xt_part_report import (
    _analyze_input_file_single,
    _fused_report_from_step_and_json,
    analyze_input_bytes,
    analyze_input_text,
    metric_and_imperial,
    parse_step_input_text,
)


HOST = "127.0.0.1"
PORT = 8765


def _is_workspace_sample_path(path: Path) -> bool:
    ignored_parts = {"__pycache__", ".git", ".hg", ".svn", "node_modules", "schemas"}
    for part in path.parts:
        if part in ignored_parts:
            return False
        if part.startswith(".venv"):
            return False
    return path.is_file()


def _report_key(report: dict) -> tuple[str, str]:
    return (
        str(report.get("file") or ""),
        str(report.get("file_name") or ""),
    )


def _normalize_identity_token(value: str | None) -> str | None:
    if not value:
        return None
    token = re.sub(r"[^a-z0-9]+", "", value.lower())
    return token or None


def _report_identity_tokens(report: dict) -> set[str]:
    tokens: set[str] = set()
    candidates = [
        Path(str(report.get("file_name") or "")).stem,
        Path(str(report.get("file") or "")).stem,
        *((report.get("decoded_names") or [])[:6]),
        str((report.get("source_part_summary") or {}).get("part_name") or ""),
        str((report.get("source_part_summary") or {}).get("leaf") or ""),
    ]
    for candidate in candidates:
        token = _normalize_identity_token(candidate)
        if token:
            tokens.add(token)
    return tokens


def _report_stem_token(report: dict) -> str | None:
    for candidate in (
        Path(str(report.get("file_name") or "")).stem,
        Path(str(report.get("file") or "")).stem,
    ):
        token = _normalize_identity_token(candidate)
        if token:
            return token
    return None


def _preview_metadata_for_report(report: dict) -> dict:
    source_format = str((report.get("transmit_info") or {}).get("source_format") or "")
    if source_format == "step_file":
        backend = ((report.get("step_import") or {}).get("backend") or "").strip().lower()
        if backend == "native_step_parser":
            return {
                "strategy": "step_quick_preview",
                "label": "STEP Quick Preview",
                "description": "Loaded quickly from decoded STEP topology while the refined STEP mesh preview can be generated in the background.",
            }
        return {
            "strategy": "step_import_preview",
            "label": "STEP Import",
            "description": "Imported directly from the STEP file and shown as a direct STEP mesh preview.",
        }
    if source_format == "siemens_nx_json":
        return {
            "strategy": "json_reconstruction_preview",
            "label": "JSON Reconstruction",
            "description": "Reconstructed from Siemens NX JSON topology and geometry for preview.",
        }
    if source_format == "stl_file":
        return {
            "strategy": "stl_mesh_preview",
            "label": "STL Mesh",
            "description": "Imported directly from the STL file and shown as a triangle mesh.",
        }
    if source_format == "fused_step_json":
        step_reference = ((report.get("fusion") or {}).get("reference_step") or {}).get("file_name")
        description = "Reconstructed from Siemens NX JSON and improved with referenced STEP geometry."
        if step_reference:
            description = f"Reconstructed from Siemens NX JSON and improved with STEP reference {step_reference}."
        return {
            "strategy": "fused_json_step_preview",
            "label": "Fusion",
            "description": description,
        }
    return {
        "strategy": "report_preview",
        "label": "Preview",
        "description": "Rendered from the best available decoded preview geometry in the report.",
    }


def _append_auto_fused_reports(reports: list[dict]) -> list[dict]:
    if not reports:
        return reports

    result = list(reports)
    seen = {_report_key(report) for report in result}
    steps = [report for report in result if (report.get("transmit_info") or {}).get("source_format") == "step_file"]
    json_reports = [
        report for report in result if (report.get("transmit_info") or {}).get("source_format") == "siemens_nx_json"
    ]
    if not steps or not json_reports:
        return result

    used_step_keys: set[tuple[str, str]] = set()
    used_json_keys: set[tuple[str, str]] = set()
    candidate_pairs: list[tuple[dict, dict]] = []

    step_by_stem: dict[str, list[dict]] = {}
    json_by_stem: dict[str, list[dict]] = {}
    for report in steps:
        token = _report_stem_token(report)
        if token:
            step_by_stem.setdefault(token, []).append(report)
    for report in json_reports:
        token = _report_stem_token(report)
        if token:
            json_by_stem.setdefault(token, []).append(report)

    for token in sorted(set(step_by_stem).intersection(json_by_stem)):
        if len(step_by_stem[token]) == 1 and len(json_by_stem[token]) == 1:
            candidate_pairs.append((step_by_stem[token][0], json_by_stem[token][0]))

    for step_report in steps:
        step_key = _report_key(step_report)
        if step_key in used_step_keys:
            continue
        step_tokens = _report_identity_tokens(step_report)
        matches = []
        for json_report in json_reports:
            json_key = _report_key(json_report)
            if json_key in used_json_keys:
                continue
            if step_tokens.intersection(_report_identity_tokens(json_report)):
                matches.append(json_report)
        if len(matches) == 1:
            candidate_pairs.append((step_report, matches[0]))

    for step_report, json_report in candidate_pairs:
        step_key = _report_key(step_report)
        json_key = _report_key(json_report)
        if step_key in used_step_keys or json_key in used_json_keys:
            continue
        fused = create_fused_report(step_report, json_report)
        if fused is None:
            continue
        fused_key = _report_key(fused)
        used_step_keys.add(step_key)
        used_json_keys.add(json_key)
        if fused_key in seen:
            continue
        seen.add(fused_key)
        result.append(fused)

    return result


def _prepare_reports(reports: list[dict]) -> list[dict]:
    enriched = [enrich_report(report) for report in reports]
    return _append_auto_fused_reports(enriched)


def workspace_sample_reports() -> list[dict]:
    reports: list[dict] = []
    seen: set[Path] = set()
    for pattern in ("*.x_t", "*.stp", "*.step", "*.stl", "*.json"):
        for path in sorted(candidate for candidate in Path.cwd().rglob(pattern) if _is_workspace_sample_path(candidate)):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            reports.extend(_analyze_input_file_single(resolved))
    return _prepare_reports(reports)


def analyze_paths(paths: list[str]) -> list[dict]:
    reports: list[dict] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if path in seen:
            continue
        seen.add(path)
        reports.extend(_analyze_input_file_single(path))
    return _prepare_reports(reports)


def analyze_uploaded_files(file_payloads: list[dict]) -> list[dict]:
    reports: list[dict] = []
    for file_payload in file_payloads:
        raw_bytes: bytes
        if file_payload.get("encoding") == "base64":
            raw_bytes = base64.b64decode(file_payload.get("data", "") or "")
        else:
            raw_bytes = str(file_payload.get("text", "")).encode("utf-8")
        imported_reports = analyze_input_bytes(
            raw_bytes,
            display_name=file_payload.get("name", "<upload>"),
            source_path=f"uploaded:{file_payload.get('name', '<upload>')}",
            file_size_bytes=file_payload.get("size"),
        )
        reports.extend(imported_reports)
    return _prepare_reports(reports)


def analyze_uploaded_binary_files(file_payloads: list[dict]) -> list[dict]:
    reports: list[dict] = []
    for file_payload in file_payloads:
        raw_bytes = bytes(file_payload.get("data") or b"")
        imported_reports = analyze_input_bytes(
            raw_bytes,
            display_name=file_payload.get("name", "<upload>"),
            source_path=f"uploaded:{file_payload.get('name', '<upload>')}",
            file_size_bytes=file_payload.get("size", len(raw_bytes)),
        )
        reports.extend(imported_reports)
    return _prepare_reports(reports)


def create_fused_report(left_report: dict, right_report: dict) -> dict | None:
    fused_report = _fused_report_from_step_and_json(left_report, right_report)
    if fused_report is None:
        return None

    formats = {
        left_report.get("transmit_info", {}).get("source_format"): left_report,
        right_report.get("transmit_info", {}).get("source_format"): right_report,
    }
    step_report = formats.get("step_file")
    json_report = formats.get("siemens_nx_json")
    if not step_report or not json_report:
        return None

    base_name = (
        (json_report.get("decoded_names") or [None])[0]
        or (step_report.get("decoded_names") or [None])[0]
        or Path(str(json_report.get("file_name") or step_report.get("file_name") or "Fusion")).stem
    )
    fused_report["file_name"] = f"{base_name} STEP+JSON"
    fused_report["file"] = (
        f"fusion:{json_report.get('file_name', 'json')}+{step_report.get('file_name', 'step')}"
    )
    fused_report["fusion"] = {
        **dict(fused_report.get("fusion") or {}),
        "display_name": "STEP+JSON",
        "reference_step": {
            "file": step_report.get("file"),
            "file_name": step_report.get("file_name"),
            "decoded_name": (step_report.get("decoded_names") or [None])[0],
        },
        "reference_json": {
            "file": json_report.get("file"),
            "file_name": json_report.get("file_name"),
            "decoded_name": (json_report.get("decoded_names") or [None])[0],
        },
        "improves": "siemens_nx_json",
        "improved_by": "step_file",
    }
    fused_report["transmit_info"] = {
        **dict(fused_report.get("transmit_info") or {}),
        "reference_step_file": step_report.get("file"),
        "reference_step_name": step_report.get("file_name"),
        "reference_json_file": json_report.get("file"),
        "reference_json_name": json_report.get("file_name"),
        "preview_strategy": "json_reconstruction_with_step_reference",
    }
    model_summary = list((fused_report.get("model_analysis") or {}).get("summary") or [])
    if model_summary:
        model_summary[0] = f"JSON reconstruction for {base_name} improved by referencing STEP geometry."
        fused_report["model_analysis"] = {
            **dict(fused_report.get("model_analysis") or {}),
            "summary": model_summary,
        }
    return enrich_report(fused_report)


def _identity_token(object_identity: dict | None, fallback: str) -> str:
    object_identity = object_identity or {}
    journal_identifier = object_identity.get("journal_identifier")
    if journal_identifier:
        return f"jid:{journal_identifier}"
    tag = object_identity.get("tag")
    if tag is not None:
        try:
            return f"tag:{int(tag)}"
        except (TypeError, ValueError):
            return f"tag:{tag}"
    return fallback


def _bounds_from_points(points: list[list[float]]) -> dict[str, Any] | None:
    if not points:
        return None
    mins = [float(points[0][0]), float(points[0][1]), float(points[0][2])]
    maxs = mins[:]
    for point in points[1:]:
        for index in range(3):
            value = float(point[index])
            if value < mins[index]:
                mins[index] = value
            if value > maxs[index]:
                maxs[index] = value
    return {
        "min": mins,
        "max": maxs,
        "size": [maxs[index] - mins[index] for index in range(3)],
    }


def _component_profile(points: list[list[float]], bounds: dict[str, Any] | None, *, bins: int = 6, sample_limit: int = 260) -> dict[str, Any] | None:
    if not bounds or not points:
        return None
    sample = (
        points
        if len(points) <= sample_limit
        else [points[min(len(points) - 1, int(index * len(points) / sample_limit))] for index in range(sample_limit)]
    )
    minimum = [float(value) for value in bounds.get("min", [0.0, 0.0, 0.0])]
    size = [max(float(value), 1e-9) for value in bounds.get("size", [0.0, 0.0, 0.0])]
    occupancy: set[tuple[int, int, int]] = set()
    centroid = [0.0, 0.0, 0.0]
    radial_distances: list[float] = []
    for point in sample:
        normalized = [
            max(0.0, min(1.0, (float(point[index]) - minimum[index]) / size[index]))
            for index in range(3)
        ]
        occupancy.add(
            tuple(
                min(bins - 1, max(0, int(normalized[index] * bins)))
                for index in range(3)
            )
        )
        for index in range(3):
            centroid[index] += normalized[index]
        radial_distances.append(sum((normalized[index] - 0.5) ** 2 for index in range(3)) ** 0.5)
    count = max(len(sample), 1)
    centroid = [value / count for value in centroid]
    radial_mean = sum(radial_distances) / len(radial_distances) if radial_distances else 0.0
    radial_std = (
        (sum((value - radial_mean) ** 2 for value in radial_distances) / len(radial_distances)) ** 0.5
        if radial_distances
        else 0.0
    )
    max_extent = max(size) if size else 1.0
    return {
        "occupancy": occupancy,
        "occupancy_count": len(occupancy),
        "centroid": centroid,
        "radial_mean": radial_mean,
        "radial_std": radial_std,
        "extent_ratios": [value / max_extent for value in size],
    }


def _profile_delta(left_profile: dict[str, Any] | None, right_profile: dict[str, Any] | None) -> dict[str, float]:
    if not left_profile or not right_profile:
        return {
            "mismatch_ratio": 1.0,
            "centroid_delta": 1.0,
            "radial_delta": 1.0,
            "density_delta": 1.0,
            "extent_delta": 1.0,
        }
    union = set(left_profile["occupancy"]).union(right_profile["occupancy"])
    intersection = set(left_profile["occupancy"]).intersection(right_profile["occupancy"])
    mismatch_ratio = 1.0 - (len(intersection) / len(union)) if union else 0.0
    centroid_delta = sum(
        (float(right_profile["centroid"][index]) - float(left_profile["centroid"][index])) ** 2
        for index in range(3)
    ) ** 0.5
    radial_delta = abs(float(right_profile["radial_mean"]) - float(left_profile["radial_mean"])) + abs(
        float(right_profile["radial_std"]) - float(left_profile["radial_std"])
    )
    density_delta = abs(float(right_profile["occupancy_count"]) - float(left_profile["occupancy_count"])) / max(
        float(left_profile["occupancy_count"]),
        float(right_profile["occupancy_count"]),
        1.0,
    )
    extent_delta = max(
        abs(float(right_profile["extent_ratios"][index]) - float(left_profile["extent_ratios"][index]))
        for index in range(3)
    )
    return {
        "mismatch_ratio": mismatch_ratio,
        "centroid_delta": centroid_delta,
        "radial_delta": radial_delta,
        "density_delta": density_delta,
        "extent_delta": extent_delta,
    }


def _component_name_overlap(left_name: str, right_name: str) -> float:
    left_tokens = {token for token in re.split(r"[^a-z0-9]+", left_name.lower()) if token}
    right_tokens = {token for token in re.split(r"[^a-z0-9]+", right_name.lower()) if token}
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens.intersection(right_tokens)) / len(left_tokens.union(right_tokens))


def _relative_delta(left: float, right: float) -> float:
    return abs(float(left) - float(right)) / max(abs(float(left)), abs(float(right)), 1e-9)


def _display_color(component: dict) -> list[float] | None:
    display_properties = (component.get("metadata") or {}).get("display_properties") or {}
    rgb = display_properties.get("rgb")
    if not isinstance(rgb, list) or len(rgb) < 3:
        return None
    return [float(value) for value in rgb[:3]]


def _color_distance(left_component: dict, right_component: dict) -> float:
    left_color = _display_color(left_component)
    right_color = _display_color(right_component)
    if not left_color or not right_color:
        return 0.0
    return sum((right_color[index] - left_color[index]) ** 2 for index in range(3)) ** 0.5 / 255.0


def _average_point(points: list[list[float]]) -> list[float] | None:
    if not points:
        return None
    totals = [0.0, 0.0, 0.0]
    for point in points:
        for index in range(3):
            totals[index] += float(point[index])
    return [value / len(points) for value in totals]


def _mesh_bin_key(point: list[float], bounds: dict[str, Any] | None, *, bins: int = 8) -> str | None:
    if not bounds or not point or len(point) < 3:
        return None
    minimum = [float(value) for value in bounds.get("min", [0.0, 0.0, 0.0])]
    size = [max(float(value), 1e-9) for value in bounds.get("size", [0.0, 0.0, 0.0])]
    return "|".join(
        str(min(bins - 1, max(0, int(((float(point[index]) - minimum[index]) / size[index]) * bins))))
        for index in range(3)
    )


def _component_signature(body: dict, body_index: int) -> dict[str, Any] | None:
    faces = list(body.get("faces") or [])
    edges = list(body.get("edges") or [])
    face_points = [
        [float(vertex[0]), float(vertex[1]), float(vertex[2])]
        for face in faces
        for vertex in (face.get("vertices") or [])
        if isinstance(vertex, list) and len(vertex) >= 3
    ]
    edge_points = [
        [float(point[0]), float(point[1]), float(point[2])]
        for edge in edges
        for point in (edge.get("points") or [])
        if isinstance(point, list) and len(point) >= 3
    ]
    all_points = face_points + edge_points
    bounds = _bounds_from_points(all_points)
    if not bounds:
        return None
    metadata = body.get("metadata") or {}
    name = str(body.get("name") or metadata.get("name") or f"Body {body_index}")
    primitive = str(metadata.get("primitive") or body.get("kind") or "body")
    axis = str(metadata.get("axis") or "")
    size = list(bounds["size"])
    body_raw_key = _identity_token(
        metadata.get("object_identity"),
        f"body:{int(metadata.get('nx_body_index') or body_index)}",
    )
    face_entries = []
    for face_index, face in enumerate(faces, start=1):
        vertices = [
            [float(vertex[0]), float(vertex[1]), float(vertex[2])]
            for vertex in (face.get("vertices") or [])
            if isinstance(vertex, list) and len(vertex) >= 3
        ]
        centroid = _average_point(vertices)
        bin_key = _mesh_bin_key(centroid, bounds) if centroid else None
        face_entries.append(
            {
                "key": _identity_token(
                    (face.get("metadata") or {}).get("object_identity"),
                    f"body:{int((face.get('metadata') or {}).get('source_body_index') or metadata.get('nx_body_index') or body_index)}:face:{int((face.get('metadata') or {}).get('source_face_index') or face_index)}",
                ),
                "centroid": centroid,
                "bin": bin_key,
            }
        )
    edge_entries = []
    for edge_index, edge in enumerate(edges, start=1):
        points = [
            [float(point[0]), float(point[1]), float(point[2])]
            for point in (edge.get("points") or [])
            if isinstance(point, list) and len(point) >= 3
        ]
        centroid = _average_point(points)
        bin_key = _mesh_bin_key(centroid, bounds) if centroid else None
        edge_entries.append(
            {
                "key": _identity_token(
                    edge.get("object_identity"),
                    f"body:{int(metadata.get('nx_body_index') or body_index)}:edge:{int(edge.get('source_edge_index') or edge_index)}",
                ),
                "centroid": centroid,
                "bin": bin_key,
            }
        )
    return {
        "name": name,
        "primitive": primitive,
        "axis": axis or None,
        "body_index": int(metadata.get("nx_body_index") or body_index),
        "body_raw_key": body_raw_key,
        "bounds": bounds,
        "center": [(float(bounds["min"][index]) + float(bounds["max"][index])) / 2.0 for index in range(3)],
        "size": size,
        "diagonal": sum(value * value for value in size) ** 0.5,
        "volume": max(size[0] * size[1] * size[2], 0.0),
        "face_count": len(faces),
        "edge_count": len(edges),
        "point_count": len(all_points),
        "profile": _component_profile(all_points, bounds),
        "face_entries": [entry for entry in face_entries if entry["key"] and entry["bin"] and entry["centroid"]],
        "edge_entries": [entry for entry in edge_entries if entry["key"] and entry["bin"] and entry["centroid"]],
        "display_color": _display_color(body),
    }


def _flatten_report_components(report: dict) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for body_index, body in enumerate(report.get("kernel_bodies") or [], start=1):
        signature = _component_signature(body, body_index)
        if signature:
            components.append(signature)
    return components


def _component_match_cost(left_component: dict, right_component: dict) -> float:
    reference_span = max(left_component["diagonal"], right_component["diagonal"], 1e-9)
    center_cost = sum(
        (float(right_component["center"][index]) - float(left_component["center"][index])) ** 2
        for index in range(3)
    ) ** 0.5 / reference_span
    size_cost = sum(
        abs(float(left_component["size"][index]) - float(right_component["size"][index]))
        / max(abs(float(left_component["size"][index])), abs(float(right_component["size"][index])), 1e-9)
        for index in range(3)
    ) / 3.0
    profile_delta = _profile_delta(left_component.get("profile"), right_component.get("profile"))
    return (
        (center_cost * 4.8)
        + (size_cost * 4.1)
        + (_relative_delta(left_component["volume"], right_component["volume"]) * 1.1)
        + (_relative_delta(left_component["face_count"], right_component["face_count"]) * 1.2)
        + (_relative_delta(left_component["edge_count"], right_component["edge_count"]) * 0.8)
        + (profile_delta["mismatch_ratio"] * 2.2)
        + profile_delta["centroid_delta"]
        + (profile_delta["radial_delta"] * 0.8)
        + (_color_distance(left_component, right_component) * 0.3)
        - (0.65 if left_component["body_raw_key"] == right_component["body_raw_key"] else 0.0)
        - (0.35 * _component_name_overlap(left_component["name"], right_component["name"]))
        - (0.2 if left_component["primitive"] == right_component["primitive"] else 0.0)
        - (0.15 if left_component["axis"] and left_component["axis"] == right_component["axis"] else 0.0)
    )


def _component_match_features(left_component: dict, right_component: dict) -> list[float]:
    reference_span = max(left_component["diagonal"], right_component["diagonal"], 1e-9)
    profile_delta = _profile_delta(left_component.get("profile"), right_component.get("profile"))
    return [
        sum(
            (float(right_component["center"][index]) - float(left_component["center"][index])) ** 2
            for index in range(3)
        ) ** 0.5 / reference_span,
        sum(
            abs(float(left_component["size"][index]) - float(right_component["size"][index]))
            / max(abs(float(left_component["size"][index])), abs(float(right_component["size"][index])), 1e-9)
            for index in range(3)
        ) / 3.0,
        _relative_delta(left_component["volume"], right_component["volume"]),
        _relative_delta(left_component["face_count"], right_component["face_count"]),
        _relative_delta(left_component["edge_count"], right_component["edge_count"]),
        _relative_delta(left_component["point_count"], right_component["point_count"]),
        profile_delta["mismatch_ratio"],
        profile_delta["centroid_delta"],
        profile_delta["radial_delta"],
        profile_delta["density_delta"],
        profile_delta["extent_delta"],
        _color_distance(left_component, right_component),
        1.0 if left_component["body_raw_key"] == right_component["body_raw_key"] else 0.0,
        _component_name_overlap(left_component["name"], right_component["name"]),
        1.0 if left_component["primitive"] == right_component["primitive"] else 0.0,
        1.0 if left_component["axis"] and left_component["axis"] == right_component["axis"] else 0.0,
        1.0 if left_component["body_index"] == right_component["body_index"] else 0.0,
    ]


def _heuristic_compare_pairs(left_components: list[dict[str, Any]], right_components: list[dict[str, Any]]) -> list[tuple[dict, dict, dict]]:
    pairs: list[tuple[dict, dict, dict]] = []
    used_right: set[int] = set()
    for left_index, left_component in enumerate(left_components, start=1):
        exact_index = next(
            (
                right_index
                for right_index, right_component in enumerate(right_components, start=1)
                if right_index not in used_right
                and right_component["body_raw_key"] == left_component["body_raw_key"]
            ),
            None,
        )
        if exact_index is not None:
            right_component = right_components[exact_index - 1]
            used_right.add(exact_index)
            pairs.append((left_component, right_component, {"left_index": left_index, "right_index": exact_index, "cost": _component_match_cost(left_component, right_component), "reason": "shared_body_key"}))
            continue
        best: tuple[int, dict[str, Any], float] | None = None
        for right_index, right_component in enumerate(right_components, start=1):
            if right_index in used_right:
                continue
            cost = _component_match_cost(left_component, right_component)
            if best is None or cost < best[2]:
                best = (right_index, right_component, cost)
        if best is None:
            continue
        used_right.add(best[0])
        pairs.append((left_component, best[1], {"left_index": left_index, "right_index": best[0], "cost": best[2], "reason": "geometric_similarity"}))
    return pairs


def _compare_match_quality(probability: float) -> str:
    if probability >= 0.82:
        return "High"
    if probability >= 0.58:
        return "Medium"
    return "Low"


def compare_reports_ml(left_report: dict, right_report: dict) -> dict:
    left_components = _flatten_report_components(left_report)
    right_components = _flatten_report_components(right_report)
    if not left_components or not right_components:
        return {
            "available": False,
            "used_ml": False,
            "summary": [],
            "stats": {"matched_components": 0, "changed_components": 0, "added_components": 0, "removed_components": 0},
            "highlightBodyKeys": {"left": [], "right": []},
            "highlightRawFaces": {"left": [], "right": []},
            "highlightRawEdges": {"left": [], "right": []},
            "annotations": {"left": [], "right": []},
            "exactHighlightBodyKeys": {"left": [], "right": []},
            "exactHighlightRawFaces": {"left": [], "right": []},
            "exactHighlightRawEdges": {"left": [], "right": []},
            "exactAnnotations": {"left": [], "right": []},
        }

    heuristic_pairs = _heuristic_compare_pairs(left_components, right_components)
    exact_positive_keys = {
        (int(meta["left_index"]), int(meta["right_index"]))
        for _left, _right, meta in heuristic_pairs
        if meta["reason"] == "shared_body_key"
    } or {
        (int(meta["left_index"]), int(meta["right_index"]))
        for _left, _right, meta in heuristic_pairs
    }

    probability_map: dict[tuple[int, int], float] = {}
    model_info = None
    training_features: list[list[float]] = []
    training_labels: list[int] = []
    predict_features: list[list[float]] = []
    pair_lookup: list[tuple[int, int, dict, dict, float]] = []
    for left_index, left_component in enumerate(left_components, start=1):
        for right_index, right_component in enumerate(right_components, start=1):
            features = _component_match_features(left_component, right_component)
            pair_lookup.append((left_index, right_index, left_component, right_component, _component_match_cost(left_component, right_component)))
            predict_features.append(features)
            training_features.append(features)
            training_labels.append(1 if (left_index, right_index) in exact_positive_keys else 0)
    ml_result = predict_match_probabilities(
        training_features=training_features,
        training_labels=training_labels,
        predict_features=predict_features,
        model_name="compare_component_match_ensemble",
    )
    if ml_result and ml_result.get("ok"):
        model_info = ml_result.get("model")
        for (left_index, right_index, _left_component, _right_component, _cost), probability in zip(pair_lookup, ml_result.get("probabilities", [])):
            probability_map[(left_index, right_index)] = float(probability)

    selected: list[tuple[dict, dict, dict]] = []
    remaining_left = {index + 1: component for index, component in enumerate(left_components)}
    remaining_right = {index + 1: component for index, component in enumerate(right_components)}
    while remaining_left and remaining_right:
        candidates = []
        for left_index, right_index, left_component, right_component, cost in pair_lookup:
            if left_index not in remaining_left or right_index not in remaining_right:
                continue
            probability = probability_map.get((left_index, right_index), max(0.0, 1.0 - min(cost / 5.0, 1.0)))
            candidates.append((probability, -cost, left_index, right_index, left_component, right_component, cost))
        if not candidates:
            break
        probability, _neg_cost, left_index, right_index, left_component, right_component, cost = max(candidates)
        selected.append((left_component, right_component, {"left_index": left_index, "right_index": right_index, "probability": probability, "cost": cost, "match_quality": _compare_match_quality(probability)}))
        remaining_left.pop(left_index, None)
        remaining_right.pop(right_index, None)

    highlight_body_keys = {"left": [], "right": []}
    highlight_raw_faces = {"left": [], "right": []}
    highlight_raw_edges = {"left": [], "right": []}
    annotations = {"left": [], "right": []}
    summary: list[str] = []
    changed_count = 0

    for left_component, right_component, meta in selected:
        profile_delta = _profile_delta(left_component.get("profile"), right_component.get("profile"))
        size_delta = sum(
            abs(float(left_component["size"][index]) - float(right_component["size"][index]))
            / max(abs(float(left_component["size"][index])), abs(float(right_component["size"][index])), 1e-9)
            for index in range(3)
        ) / 3.0
        changed = (
            profile_delta["mismatch_ratio"] >= 0.08
            or size_delta >= 0.06
            or left_component["face_count"] != right_component["face_count"]
            or left_component["edge_count"] != right_component["edge_count"]
        )
        if not changed:
            continue
        changed_count += 1
        highlight_body_keys["left"].append(left_component["body_raw_key"])
        highlight_body_keys["right"].append(right_component["body_raw_key"])
        left_face_bins = {entry["bin"] for entry in left_component["face_entries"]}
        right_face_bins = {entry["bin"] for entry in right_component["face_entries"]}
        left_edge_bins = {entry["bin"] for entry in left_component["edge_entries"]}
        right_edge_bins = {entry["bin"] for entry in right_component["edge_entries"]}
        left_changed_faces = [entry for entry in left_component["face_entries"] if entry["bin"] not in right_face_bins][:180]
        right_changed_faces = [entry for entry in right_component["face_entries"] if entry["bin"] not in left_face_bins][:180]
        left_changed_edges = [entry for entry in left_component["edge_entries"] if entry["bin"] not in right_edge_bins][:120]
        right_changed_edges = [entry for entry in right_component["edge_entries"] if entry["bin"] not in left_edge_bins][:120]
        highlight_raw_faces["left"].extend(entry["key"] for entry in left_changed_faces)
        highlight_raw_faces["right"].extend(entry["key"] for entry in right_changed_faces)
        highlight_raw_edges["left"].extend(entry["key"] for entry in left_changed_edges)
        highlight_raw_edges["right"].extend(entry["key"] for entry in right_changed_edges)
        left_point = _average_point([entry["centroid"] for entry in left_changed_faces if entry["centroid"]]) or left_component["center"]
        right_point = _average_point([entry["centroid"] for entry in right_changed_faces if entry["centroid"]]) or right_component["center"]
        mismatch_percent = int(round(profile_delta["mismatch_ratio"] * 100))
        summary.append(
            f"ML compare matched {left_component['name']} to {right_component['name']} with {meta['match_quality'].lower()} confidence and found about {mismatch_percent}% regional mismatch."
        )
        annotations["left"].append({"point": left_point, "lines": [f"{left_component['name']} ML-changed region ({mismatch_percent}% mismatch)"]})
        annotations["right"].append({"point": right_point, "lines": [f"{right_component['name']} ML-changed region ({mismatch_percent}% mismatch)"]})

    summary.insert(
        0,
        f"ML compare matched {len(selected)} component(s); {changed_count} changed, {len(remaining_right)} added, {len(remaining_left)} removed."
    )
    for component in remaining_left.values():
        highlight_body_keys["left"].append(component["body_raw_key"])
        summary.append(f"ML compare flagged removed component: {component['name']}.")
    for component in remaining_right.values():
        highlight_body_keys["right"].append(component["body_raw_key"])
        summary.append(f"ML compare flagged added component: {component['name']}.")

    return {
        "available": bool(model_info),
        "used_ml": bool(model_info),
        "model": model_info,
        "summary": summary[:12],
        "stats": {
            "matched_components": len(selected),
            "changed_components": changed_count,
            "added_components": len(remaining_right),
            "removed_components": len(remaining_left),
        },
        "highlightBodyKeys": {
            "left": list(dict.fromkeys(item for item in highlight_body_keys["left"] if item)),
            "right": list(dict.fromkeys(item for item in highlight_body_keys["right"] if item)),
        },
        "highlightRawFaces": {
            "left": list(dict.fromkeys(item for item in highlight_raw_faces["left"] if item)),
            "right": list(dict.fromkeys(item for item in highlight_raw_faces["right"] if item)),
        },
        "highlightRawEdges": {
            "left": list(dict.fromkeys(item for item in highlight_raw_edges["left"] if item)),
            "right": list(dict.fromkeys(item for item in highlight_raw_edges["right"] if item)),
        },
        "annotations": {
            "left": annotations["left"][:6],
            "right": annotations["right"][:6],
        },
        "exactHighlightBodyKeys": {"left": [], "right": []},
        "exactHighlightRawFaces": {"left": [], "right": []},
        "exactHighlightRawEdges": {"left": [], "right": []},
        "exactAnnotations": {"left": [], "right": []},
    }


def preview_score(report: dict) -> tuple[int, int]:
    geometry = report.get("geometry_hints", {})
    topology = report.get("model_analysis", {}).get("topology")
    return (
        int(geometry.get("point_count", 0)),
        1 if topology else 0,
    )


def _transport_face(face: dict) -> dict:
    metadata = dict(face.get("metadata") or {})
    return {
        "name": face.get("name"),
        "vertices": list(face.get("vertices") or []),
        "normal": face.get("normal"),
        "metadata": {
            "kind": metadata.get("kind"),
            "axis": metadata.get("axis"),
            "source_body_index": metadata.get("source_body_index"),
            "source_face_index": metadata.get("source_face_index"),
            "source_face_type": metadata.get("source_face_type"),
            "object_identity": metadata.get("object_identity"),
            "display_properties": metadata.get("display_properties"),
            "component_context": metadata.get("component_context"),
        },
    }


def _transport_edge(edge: dict) -> dict:
    return {
        "kind": edge.get("kind"),
        "name": edge.get("name"),
        "source_edge_index": edge.get("source_edge_index"),
        "points": list(edge.get("points") or []),
        "object_identity": edge.get("object_identity"),
        "display_properties": edge.get("display_properties"),
        "component_context": edge.get("component_context"),
    }


def _transport_kernel_body(body: dict) -> dict:
    metadata = dict(body.get("metadata") or {})
    return {
        "kind": body.get("kind"),
        "name": body.get("name"),
        "faces": [_transport_face(face) for face in (body.get("faces") or [])],
        "edges": [_transport_edge(edge) for edge in (body.get("edges") or [])],
        "vertices": list(body.get("vertices") or []),
        "metadata": {
            "name": metadata.get("name"),
            "primitive": metadata.get("primitive"),
            "shape_summary": metadata.get("shape_summary"),
            "source": metadata.get("source"),
            "nx_body_index": metadata.get("nx_body_index"),
            "object_identity": metadata.get("object_identity"),
            "display_properties": metadata.get("display_properties"),
            "component_context": metadata.get("component_context"),
            "edge_count": metadata.get("edge_count"),
            "vertex_count": metadata.get("vertex_count"),
            "face_patch_count": metadata.get("face_patch_count"),
            "source_face_count": metadata.get("source_face_count"),
        },
    }


def _transport_report(report: dict) -> dict:
    transported = dict(report)
    kernel_bodies = report.get("kernel_bodies")
    if isinstance(kernel_bodies, list) and kernel_bodies:
        transported["kernel_bodies"] = [_transport_kernel_body(body) for body in kernel_bodies if body]
        transported["kernel_body"] = transported["kernel_bodies"][0] if len(transported["kernel_bodies"]) == 1 else None
    elif report.get("kernel_body"):
        transported["kernel_body"] = _transport_kernel_body(report["kernel_body"])

    if report.get("preview_kernel_bodies") is report.get("kernel_bodies"):
        transported.pop("preview_kernel_bodies", None)
    elif isinstance(report.get("preview_kernel_bodies"), list) and report.get("preview_kernel_bodies"):
        transported["preview_kernel_bodies"] = [
            _transport_kernel_body(body) for body in report.get("preview_kernel_bodies") if body
        ]
    return transported


def _reports_for_client(reports: list[dict]) -> list[dict]:
    return [_transport_report(report) for report in reports]


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CAD Part Viewer & Compare</title>
  <style>
    * { box-sizing: border-box; }
    html, body { margin: 0; height: 100%; }
    :root {
      --app-bg: #d9d9d9;
      --chrome-bg: #efefef;
      --panel-bg: #f7f7f7;
      --panel-border: #b8b8b8;
      --panel-shadow: 0 14px 32px rgba(0, 0, 0, 0.08);
      --text-main: #1f1f1f;
      --text-muted: #5f5f5f;
      --accent: #ff9f1c;
      --accent-strong: #ff7a00;
      --viewport-top: #252525;
      --viewport-bottom: #1a1a1a;
      --viewport-grid: rgba(255, 255, 255, 0.06);
      --viewport-grid-strong: rgba(255, 255, 255, 0.12);
      --viewport-frame: #5a5a5a;
    }
    body {
      margin: 0;
      font-family: "Segoe UI", "Aptos", "Helvetica Neue", Arial, sans-serif;
      background: var(--app-bg);
      color: var(--text-main);
    }
    .app {
      display: grid;
      grid-template-rows: auto auto auto 1fr;
      gap: 10px;
      padding: 10px;
      box-sizing: border-box;
      min-height: 100vh;
    }
    .header, .toolbar, .sidebar, .main, .status {
      background: rgba(247, 247, 247, 0.96);
      border: 1px solid var(--panel-border);
      box-shadow: var(--panel-shadow);
    }
    .header, .toolbar, .status {
      margin: 0;
      padding: 10px;
    }
    .header h1 {
      margin: 0 0 6px 0;
      font-size: 24px;
      font-weight: 700;
    }
    .header p, .status {
      margin: 0;
      color: var(--text-muted);
      font-size: 14px;
      line-height: 1.4;
    }
    .status {
      display: grid;
      gap: 8px;
    }
    .timing-panel {
      display: grid;
      gap: 6px;
      padding-top: 2px;
      border-top: 1px solid #dddddd;
    }
    .timing-panel[hidden] {
      display: none;
    }
    .timing-title {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--text-muted);
    }
    .timing-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 6px;
    }
    .timing-chip {
      border: 1px solid #d2d2d2;
      background: #f1f1f1;
      padding: 6px 8px;
      min-height: 48px;
    }
    .timing-chip-label {
      font-size: 11px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .timing-chip-value {
      font-size: 16px;
      font-weight: 700;
      color: var(--text-main);
      margin-top: 2px;
    }
    .timing-chip-note {
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 2px;
      line-height: 1.3;
    }
    .status-progress {
      height: 8px;
      border: 1px solid #d0d0d0;
      background: #ededed;
      overflow: hidden;
    }
    .status-progress[hidden] {
      display: none;
    }
    .status-progress-bar {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #f59e0b, #fbbf24);
      transition: width 120ms linear;
    }
    .status-progress.indeterminate .status-progress-bar {
      width: 35%;
      animation: status-progress-slide 1.1s linear infinite;
    }
    @keyframes status-progress-slide {
      from { transform: translateX(-120%); }
      to { transform: translateX(340%); }
    }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      background: rgba(239, 239, 239, 0.96);
    }
    .toolbar input[type="text"] {
      flex: 1 1 320px;
      min-width: 220px;
      padding: 10px 12px;
      border: 1px solid #b8b8b8;
      border-radius: 0;
      background: #fff;
      color: var(--text-main);
    }
    button, .file-upload {
      padding: 8px 12px;
      border: 1px solid #ababab;
      border-radius: 0;
      background: #e6e6e6;
      color: var(--text-main);
      cursor: pointer;
      font: inherit;
      transition: background 120ms ease, transform 120ms ease, border-color 120ms ease;
    }
    button:hover, .file-upload:hover {
      background: #dcdcdc;
      border-color: #8d8d8d;
      transform: translateY(-1px);
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
      transform: none;
      background: #ececec;
      border-color: #c5c5c5;
    }
    .file-upload input { display: none; }
    .content {
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr);
      gap: 10px;
      padding: 0;
      min-height: 0;
    }
    .sidebar {
      padding: 10px;
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      gap: 10px;
      min-height: 0;
      background: rgba(242, 242, 242, 0.97);
    }
    .sidebar h2, .panel h2 {
      margin: 0;
      font-size: 16px;
    }
    .hint {
      color: var(--text-muted);
      font-size: 13px;
      line-height: 1.4;
    }
    .file-list {
      overflow: auto;
      min-height: 0;
      border: 1px solid #d0d0d0;
      border-radius: 0;
      background: #f2f2f2;
    }
    .file-item {
      padding: 10px;
      border-bottom: 1px solid #d7d7d7;
      cursor: pointer;
      background: transparent;
      transition: background 120ms ease, border-color 120ms ease;
    }
    .file-item:last-child { border-bottom: 0; }
    .file-item:hover { background: rgba(210, 210, 210, 0.35); }
    .file-item.selected {
      background: #e2e2e2;
      box-shadow: inset 3px 0 0 var(--accent);
    }
    .sidebar-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .file-item strong {
      display: block;
      margin-bottom: 4px;
      font-size: 14px;
    }
    .file-item span {
      display: block;
      color: var(--text-muted);
      font-size: 12px;
      line-height: 1.4;
      word-break: break-word;
    }
    .main {
      padding: 10px;
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 10px;
      min-height: 0;
    }
    .top-row {
      display: grid;
      grid-template-columns: minmax(300px, 420px) minmax(0, 1fr);
      gap: 10px;
    }
    .panel {
      border: 1px solid #d2d2d2;
      border-radius: 0;
      background: #f5f5f5;
      padding: 10px;
      min-height: 0;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.7);
    }
    .summary-grid {
      display: grid;
      gap: 8px;
      font-size: 14px;
    }
    .summary-grid .muted { color: var(--text-muted); }
    .preview-wrap {
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 8px;
      min-height: 320px;
    }
    .preview-toolbar {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
    }
    .toolbar-cluster {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }
    .toolbar-label {
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-right: 2px;
    }
    .preview-mode, .view-preset {
      padding: 6px 10px;
      border: 1px solid #9a9a9a;
      border-radius: 0;
      background: #e6e6e6;
      cursor: pointer;
      font-size: 12px;
      font-weight: 600;
    }
    .preview-mode.active, .view-preset.active {
      background: #ffd8a8;
      border-color: #f59e0b;
      color: #7c3f00;
      font-weight: 700;
    }
    .canvas-frame {
      position: relative;
      min-height: 320px;
      border: 0;
      border-radius: 0;
      overflow: hidden;
      background: #1d1d1d;
      box-shadow: none;
    }
    canvas {
      width: 100%;
      height: 100%;
      min-height: 320px;
      background: transparent;
      display: block;
    }
    .annotation-canvas {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      min-height: 320px;
      background: transparent;
      pointer-events: none;
      z-index: 2;
    }
    .viewport-overlay {
      position: absolute;
      inset: 0;
      display: none;
      align-items: flex-start;
      justify-content: flex-start;
      padding: 14px 16px;
      pointer-events: none;
      font-size: 13px;
      line-height: 1.45;
      color: #343434;
      white-space: pre-wrap;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.22), rgba(255, 255, 255, 0));
      z-index: 3;
    }
    .viewport-overlay.visible {
      display: flex;
    }
    .viewport-overlay[data-tone="error"] {
      color: #7f1d1d;
      background: linear-gradient(180deg, rgba(255, 245, 245, 0.85), rgba(255, 255, 255, 0));
    }
    .viewport-footer {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      flex-wrap: wrap;
      padding: 10px 12px 0 12px;
    }
    .preview-note {
      font-size: 12px;
      color: var(--text-muted);
      line-height: 1.4;
      max-width: 700px;
    }
    .viewport-badges {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      justify-content: flex-end;
    }
    .viewport-badge {
      padding: 5px 9px;
      border-radius: 0;
      border: 1px solid #c8c8c8;
      background: rgba(255,255,255,0.86);
      color: var(--text-main);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .tabs {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .tab {
      padding: 8px 10px;
      border: 1px solid #b3b3b3;
      border-radius: 0;
      background: #e6e6e6;
      cursor: pointer;
      user-select: none;
      font-size: 13px;
      font-weight: 600;
    }
    .tab.active {
      background: #d8d8d8;
      border-color: #8c8c8c;
      font-weight: 700;
    }
    .tab-panels {
      border: 1px solid #d2d2d2;
      border-radius: 0;
      background: #f5f5f5;
      overflow: auto;
      min-height: 0;
    }
    .tab-panel {
      display: none;
      padding: 10px;
      min-height: 100%;
    }
    .tab-panel.active { display: block; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th, td {
      border: 1px solid #d8d8d8;
      padding: 8px;
      text-align: left;
      vertical-align: top;
    }
    th {
      background: #efefef;
      font-weight: 700;
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: Consolas, Menlo, monospace;
      font-size: 13px;
      line-height: 1.5;
    }
    .empty {
      border: 1px dashed #bdbdbd;
      border-radius: 0;
      padding: 16px;
      color: var(--text-muted);
      background: rgba(255,255,255,0.78);
      font-size: 14px;
    }
    .compare-layout {
      display: grid;
      gap: 10px;
    }
    .compare-summary {
      border: 1px solid #d0d0d0;
      border-radius: 0;
      background: #ededed;
      padding: 12px;
    }
    .compare-summary h3 {
      margin: 0 0 10px 0;
      font-size: 14px;
    }
    .compare-summary-list {
      display: grid;
      gap: 8px;
    }
    .compare-summary-item {
      padding: 10px 12px;
      border-radius: 0;
      border: 1px solid rgba(140, 140, 140, 0.32);
      background: rgba(255,255,255,0.9);
      font-size: 13px;
      line-height: 1.45;
      color: var(--text-main);
    }
    .compare-change-row td {
      background: rgba(30,136,229,0.08);
    }
    .compare-change-cell {
      display: grid;
      gap: 8px;
    }
    .compare-change-pill {
      padding: 8px 10px;
      border: 1px solid rgba(30,136,229,0.28);
      background: rgba(255,255,255,0.94);
      color: var(--text-main);
      font-size: 13px;
      line-height: 1.45;
    }
    .compare-table-row-changed td {
      background: rgba(30,136,229,0.05);
    }
    .compare-controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      padding: 10px 12px;
      border: 1px solid #d2d2d2;
      border-radius: 0;
      background: #efefef;
      font-size: 13px;
      color: #333333;
    }
    .compare-controls label {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      user-select: none;
    }
    .compare-preview-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .compare-preview-card {
      border: 1px solid #d2d2d2;
      border-radius: 0;
      background: #f5f5f5;
      padding: 10px;
    }
    .compare-preview-card h3 {
      margin: 0 0 8px 0;
      font-size: 14px;
    }
    .compare-canvas {
      width: 100%;
      height: 360px;
      min-height: 360px;
      display: block;
    }
    .compare-note {
      margin-top: 8px;
      color: var(--text-muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .body-visibility {
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid #d5d5d5;
      display: grid;
      gap: 10px;
    }
    .body-visibility-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .body-visibility-list {
      display: grid;
      gap: 6px;
    }
    .body-visibility-item {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--text-main);
    }
    .body-visibility-item small {
      color: var(--text-muted);
    }
    @media (max-width: 980px) {
      .content { grid-template-columns: 1fr; }
      .top-row { grid-template-columns: 1fr; }
      .compare-preview-grid { grid-template-columns: 1fr; }
    }
  </style>
  <script type="importmap">
    {
      "imports": {
        "three": "/vendor/three/build/three.module.js"
      }
    }
  </script>
  <script type="text/javascript" src="/vendor/occt-import-js/dist/occt-import-js.js"></script>
</head>
<body>
  <div class="app">
    <div class="header">
      <h1>CAD Part Viewer & Compare</h1>
      <p>STEP files preview as direct STEP imports, STL files preview as direct triangle meshes, Siemens NX JSON files preview as reconstructed parts, and Fusion previews improve the JSON by referencing the matching STEP geometry. `.x_t`, compatible JSON, and text reports are also supported.</p>
    </div>

    <div class="toolbar">
      <button id="load-samples">Load Workspace Samples</button>
      <label class="file-upload">Upload CAD Files<input id="file-input" type="file" accept=".x_t,.step,.stp,.stl,.json,.txt,.md,text/plain,application/json,model/stl,application/sla" multiple></label>
      <input id="path-input" type="text" placeholder="Enter one or more file paths separated by commas">
      <button id="load-paths">Load Paths</button>
      <button id="export-json">Export Current JSON</button>
      <button id="reset-view">Reset Preview View</button>
    </div>

    <div class="status" id="status">
      <div id="status-text">Load a STEP or STL file to preview it, or load a matching STEP and JSON pair to build a STEP-referenced fusion.</div>
      <div class="status-progress" id="status-progress" hidden>
        <div class="status-progress-bar" id="status-progress-bar"></div>
      </div>
      <div class="timing-panel" id="timing-panel" hidden></div>
    </div>

    <div class="content">
      <div class="sidebar">
        <h2>Loaded Parts</h2>
        <div class="hint">Click one part to preview it. Use Ctrl-click, Cmd-click, or Shift-click to add a second part, then compare what changed.</div>
        <div class="file-list" id="file-list"></div>
        <div class="hint" id="sidebar-foot">No parts loaded yet.</div>
      </div>

      <div class="main">
        <div class="top-row">
          <div class="panel">
            <h2 id="part-title">No part selected</h2>
            <div class="summary-grid" id="summary-grid">
              <div class="muted">Load a file to see part information.</div>
            </div>
          </div>

          <div class="panel preview-wrap">
            <h2>Preview</h2>
            <div class="preview-toolbar">
              <div class="toolbar-cluster" id="preview-modes"></div>
              <div class="toolbar-cluster" id="preview-toolbar-right"></div>
            </div>
            <div class="canvas-frame">
              <canvas id="preview-canvas"></canvas>
              <div id="preview-overlay" class="viewport-overlay"></div>
            </div>
            <div class="viewport-footer">
            <div class="preview-note" id="preview-note">Orbit with drag. Pan with Shift + drag or right-drag. Mouse wheel zooms. Double-click fits the model. STEP files use direct STEP import geometry, STL files use direct triangle meshes, JSON files use reconstructed preview geometry, and Fusion previews use JSON geometry improved by STEP references.</div>
              <div class="viewport-badges">
                <div class="viewport-badge">Orbit</div>
                <div class="viewport-badge">Pan</div>
                <div class="viewport-badge">Inspect</div>
              </div>
            </div>
          </div>
        </div>

        <div class="tabs" id="tabs"></div>

        <div class="tab-panels">
          <div class="tab-panel active" data-panel="overview"><div id="overview-panel"></div></div>
          <div class="tab-panel" data-panel="notes"><pre id="notes-panel"></pre></div>
          <div class="tab-panel" data-panel="header"><pre id="header-panel"></pre></div>
          <div class="tab-panel" data-panel="topology"><pre id="topology-panel"></pre></div>
          <div class="tab-panel" data-panel="geometry"><pre id="geometry-panel"></pre></div>
          <div class="tab-panel" data-panel="compare"><div id="compare-panel"></div></div>
          <div class="tab-panel" data-panel="json"><pre id="json-panel"></pre></div>
        </div>
      </div>
    </div>

  </div>

  <script>
    function identityRotation() {
      return [
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1]
      ];
    }

    function multiplyRotationMatrices(a, b) {
      return a.map((row, rowIndex) => ([
        row[0] * b[0][0] + row[1] * b[1][0] + row[2] * b[2][0],
        row[0] * b[0][1] + row[1] * b[1][1] + row[2] * b[2][1],
        row[0] * b[0][2] + row[1] * b[1][2] + row[2] * b[2][2]
      ]));
    }

    function applyRotationMatrix(matrix, point) {
      return [
        matrix[0][0] * point[0] + matrix[0][1] * point[1] + matrix[0][2] * point[2],
        matrix[1][0] * point[0] + matrix[1][1] * point[1] + matrix[1][2] * point[2],
        matrix[2][0] * point[0] + matrix[2][1] * point[1] + matrix[2][2] * point[2]
      ];
    }

    function rotationAroundX(angle) {
      const c = Math.cos(angle);
      const s = Math.sin(angle);
      return [
        [1, 0, 0],
        [0, c, -s],
        [0, s, c]
      ];
    }

    function rotationAroundY(angle) {
      const c = Math.cos(angle);
      const s = Math.sin(angle);
      return [
        [c, 0, -s],
        [0, 1, 0],
        [s, 0, c]
      ];
    }

    function vectorLength(vector) {
      return Math.hypot(vector[0], vector[1], vector[2]);
    }

    function normalizeVector(vector) {
      const length = vectorLength(vector);
      if (!length) return [0, 0, 0];
      return vector.map((value) => value / length);
    }

    function crossProduct(a, b) {
      return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0]
      ];
    }

    function normalizeRotationMatrix(matrix) {
      const xAxis = normalizeVector(matrix[0]);
      let yAxis = matrix[1];
      let zAxis = normalizeVector(crossProduct(xAxis, yAxis));
      if (!vectorLength(zAxis)) {
        zAxis = [0, 0, 1];
      }
      yAxis = normalizeVector(crossProduct(zAxis, xAxis));
      return [xAxis, yAxis, zAxis];
    }

    function rotationFromYawPitch(yaw, pitch) {
      return multiplyRotationMatrices(rotationAroundX(pitch), rotationAroundY(yaw));
    }

    function defaultRotation() {
      return rotationFromYawPitch(-0.7, 0.5);
    }

    function defaultViewerState() {
      return {
        rotation: defaultRotation(),
        zoom: 1,
        panX: 0,
        panY: 0,
        mode: "solid"
      };
    }

    const state = {
      reports: [],
      selected: [],
      activeTab: "overview",
      viewer: defaultViewerState(),
      viewportBackground: "gray",
      compareSync: false,
      compareMlCache: {},
      compareMlPending: {},
      hiddenPreviewBodies: {},
      timing: {
        import: null,
        compare: null,
        backgroundStep: {},
      },
      compareViewers: {
        left: defaultViewerState(),
        right: defaultViewerState()
      }
    };

    const tabs = [
      ["overview", "Overview"],
      ["compare", "Compare"],
      ["notes", "Part Notes"],
      ["header", "Header"],
      ["topology", "Topology"],
      ["geometry", "Geometry"],
      ["json", "JSON"]
    ];

    const AXES = ["x", "y", "z"];
    const DIMENSION_TOLERANCE = 1e-6;
    const MAX_RENDER_FACES = 3200;
    const MAX_RENDER_OCC_FACES = 2200;
    const VIEW_PRESETS = [
      ["iso", "ISO"],
      ["front", "Front"],
      ["top", "Top"],
      ["right", "Right"],
      ["fit", "Fit"]
    ];
    const VIEWPORT_BACKGROUNDS = [
      ["white", "White", "#ffffff"],
      ["gray", "Gray", "#d9d9d9"],
      ["dark", "Dark", "#1d1d1d"]
    ];
    const THREE_MODULE_URL = "/vendor/three/build/three.module.js";
    const OCCT_IMPORT_JS_BASE_URL = "/vendor/occt-import-js/dist/";
    const previewCanvas = document.getElementById("preview-canvas");
    const canvas = previewCanvas;
    const previewOverlay = document.getElementById("preview-overlay");
    let dragState = null;
    let previewViewport = null;
    let compareViewports = { left: null, right: null };
    let webGlModulesPromise = null;
    let occtImportPromise = null;
    let pendingStepRefinements = new Set();

    function byId(id) {
      return document.getElementById(id);
    }

    function nowMs() {
      if (typeof performance !== "undefined" && typeof performance.now === "function") {
        return performance.now();
      }
      return Date.now();
    }

    function formatDuration(ms) {
      const value = Number(ms);
      if (!Number.isFinite(value) || value < 0) return "—";
      if (value < 1000) return `${value < 100 ? value.toFixed(1) : Math.round(value)} ms`;
      if (value < 10000) return `${(value / 1000).toFixed(2)} s`;
      return `${(value / 1000).toFixed(1)} s`;
    }

    function escapeTiming(value) {
      return escapeHtml(value == null ? "—" : String(value));
    }

    function timingChip(label, value, note = "") {
      return `
        <div class="timing-chip">
          <div class="timing-chip-label">${escapeHtml(label)}</div>
          <div class="timing-chip-value">${escapeTiming(value)}</div>
          <div class="timing-chip-note">${escapeTiming(note)}</div>
        </div>
      `;
    }

    function renderTimingPanel() {
      const panel = byId("timing-panel");
      if (!panel) return;

      const importTiming = state.timing.import;
      const compareTiming = state.timing.compare;
      const backgroundEntries = Object.values(state.timing.backgroundStep || {})
        .filter(Boolean)
        .sort((a, b) => Number(b.completedAt || 0) - Number(a.completedAt || 0));
      const latestBackground = backgroundEntries[0] || null;

      if (!importTiming && !compareTiming && !latestBackground) {
        panel.hidden = true;
        panel.innerHTML = "";
        return;
      }

      const sections = [];
      if (importTiming) {
        const title = importTiming.fileCount > 1
          ? `Latest Import: ${importTiming.fileCount} files`
          : `Latest Import: ${importTiming.primaryName || "1 file"}`;
        sections.push(`
          <div>
            <div class="timing-title">${escapeHtml(title)}</div>
            <div class="timing-grid">
              ${timingChip("Total", formatDuration(importTiming.totalMs), importTiming.stepFileCount ? `${importTiming.stepFileCount} STEP preview${importTiming.stepFileCount === 1 ? "" : "s"}` : "Direct analysis flow")}
              ${timingChip("Upload", formatDuration(importTiming.uploadMs), importTiming.uploadBytes ? `${Math.round(importTiming.uploadBytes / 1024)} KB sent` : "Browser to server")}
              ${timingChip("Preview", formatDuration(importTiming.browserPreviewMs), importTiming.stepFileCount ? (importTiming.browserPreviewBreakdown || "Browser STEP import") : "No browser STEP preview")}
              ${timingChip("Server Analysis", formatDuration(importTiming.serverAnalysisMs), importTiming.stepFileCount ? "Returned after preview merge" : "Upload request processing")}
            </div>
          </div>
        `);
      }

      if (latestBackground) {
        sections.push(`
          <div>
            <div class="timing-title">${escapeHtml(`Latest STEP Detail Analysis: ${latestBackground.fileName || "STEP file"}`)}</div>
            <div class="timing-grid">
              ${timingChip("Background STEP", formatDuration(latestBackground.durationMs), latestBackground.status || "Detailed analysis")}
            </div>
          </div>
        `);
      }

      if (compareTiming) {
        sections.push(`
          <div>
            <div class="timing-title">${escapeHtml(`Latest Compare: ${compareTiming.label || "Selected pair"}`)}</div>
            <div class="timing-grid">
              ${timingChip("Compare Request", formatDuration(compareTiming.requestMs), compareTiming.status || "ML-assisted compare")}
              ${timingChip("Changed Parts", compareTiming.changedComponents ?? "—", `${compareTiming.removedComponents ?? 0} removed, ${compareTiming.addedComponents ?? 0} added`)}
              ${timingChip("Match Mode", compareTiming.mode || "—", compareTiming.modelType || "No ML model info")}
            </div>
          </div>
        `);
      }

      panel.hidden = false;
      panel.innerHTML = sections.join("");
    }

    function reportKey(report) {
      return report.file + "::" + report.file_name;
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function setStatus(message) {
      const statusText = byId("status-text");
      if (statusText) {
        statusText.textContent = message;
        return;
      }
      byId("status").textContent = message;
    }

    function setStatusProgress(value = null, options = {}) {
      const progressEl = byId("status-progress");
      const barEl = byId("status-progress-bar");
      if (!progressEl || !barEl) return;

      const { indeterminate = false, visible = true } = options;
      if (!visible) {
        progressEl.hidden = true;
        progressEl.classList.remove("indeterminate");
        barEl.style.width = "0%";
        return;
      }

      progressEl.hidden = false;
      progressEl.classList.toggle("indeterminate", indeterminate);
      if (!indeterminate) {
        const clamped = Math.max(0, Math.min(100, Number(value) || 0));
        barEl.style.width = `${clamped}%`;
      }
    }

    function clearStatusProgress() {
      setStatusProgress(0, { visible: false });
    }

    function updateImportTiming(timing) {
      state.timing.import = {
        ...(state.timing.import || {}),
        ...timing,
        completedAt: Date.now(),
      };
      renderTimingPanel();
    }

    function updateBackgroundStepTiming(fileName, timing) {
      state.timing.backgroundStep = {
        ...(state.timing.backgroundStep || {}),
        [fileName]: {
          ...(state.timing.backgroundStep?.[fileName] || {}),
          ...timing,
          fileName,
          completedAt: Date.now(),
        },
      };
      renderTimingPanel();
    }

    function updateCompareTiming(timing) {
      state.timing.compare = {
        ...(state.timing.compare || {}),
        ...timing,
        completedAt: Date.now(),
      };
      renderTimingPanel();
    }

    function setPreviewOverlay(message = "", tone = "info") {
      setViewportOverlay({ overlay: previewOverlay }, message, tone);
    }

    function setViewportOverlay(viewport, message = "", tone = "info") {
      const overlay = viewport?.overlay;
      if (!overlay) return;
      overlay.textContent = message || "";
      overlay.dataset.tone = tone;
      overlay.classList.toggle("visible", Boolean(message));
    }

    function activeReport() {
      if (!state.selected.length) return null;
      return state.reports.find((report) => reportKey(report) === state.selected[0]) || null;
    }

    function selectedReports() {
      return state.selected
        .map((key) => state.reports.find((report) => reportKey(report) === key))
        .filter(Boolean);
    }

    function kernelBodies(report) {
      if (!report) return [];
      if (Array.isArray(report.kernel_bodies) && report.kernel_bodies.length) {
        return report.kernel_bodies.filter(Boolean);
      }
      return report.kernel_body ? [report.kernel_body] : [];
    }

    function previewBodies(report) {
      if (!report) return [];
      if (Array.isArray(report.preview_kernel_bodies) && report.preview_kernel_bodies.length) {
        return report.preview_kernel_bodies.filter(Boolean);
      }
      return kernelBodies(report);
    }

    function previewGeometryHints(report) {
      return report?.preview_geometry_hints || report?.geometry_hints || {};
    }

    function previewBodyKey(body, bodyIndex = 0) {
      return rawIdentityKey(
        body?.metadata?.object_identity,
        `preview-body:${Number(body?.metadata?.nx_body_index || body?.index || bodyIndex + 1)}`
      );
    }

    function hiddenPreviewBodiesForReport(report) {
      if (!report) return [];
      return state.hiddenPreviewBodies[reportKey(report)] || [];
    }

    function visiblePreviewBodyCount(report) {
      const bodies = previewBodies(report);
      if (!bodies.length) return 0;
      const hidden = new Set(hiddenPreviewBodiesForReport(report));
      return bodies.filter((body, bodyIndex) => !hidden.has(previewBodyKey(body, bodyIndex))).length;
    }

    function setHiddenPreviewBodies(report, hiddenKeys) {
      if (!report) return;
      state.hiddenPreviewBodies = {
        ...state.hiddenPreviewBodies,
        [reportKey(report)]: dedupeList(hiddenKeys)
      };
    }

    function togglePreviewBody(report, bodyKey, visible) {
      const hidden = new Set(hiddenPreviewBodiesForReport(report));
      if (visible) hidden.delete(bodyKey);
      else hidden.add(bodyKey);
      setHiddenPreviewBodies(report, [...hidden]);
      renderOverview();
      drawPreview();
      if (state.activeTab === "compare") {
        drawComparePreviews();
      }
    }

    function inferredTopology(report) {
      return report?.model_analysis?.topology || report?.kernel_topology || null;
    }

    function reportPreviewScore(report) {
      return (previewGeometryHints(report)?.point_count || 0) + ((inferredTopology(report) ? 1 : 0) * 1000);
    }

    function bestReportKey(reports) {
      if (!reports.length) return null;
      const best = [...reports].sort((a, b) => reportPreviewScore(b) - reportPreviewScore(a))[0];
      return reportKey(best);
    }

    function comparisonReports() {
      if (state.selected.length !== 2) return [];
      return selectedReports();
    }

    function canCompareSelection() {
      return state.selected.length === 2;
    }

    function formatDensity(report) {
      if (!report || !report.density) return "Not found";
      return `${report.density.value} ${report.density.unit || ""}`.trim();
    }

    function formatColors(report) {
      if (!report || !report.colors || !report.colors.length) return "Not found";
      return report.colors.map((color) => color.hex).join(", ");
    }

    function sourceFormatLabel(report) {
      const format = report?.transmit_info?.source_format || "";
      const labels = {
        fused_step_json: "Fusion (JSON + STEP Reference)",
        siemens_nx_json: "Siemens NX JSON",
        stl_file: "STL",
        step_file: "STEP",
        parasolid_analysis_report: "Parasolid analysis report"
      };
      if (labels[format]) return labels[format];
      const fileName = String(report?.file_name || report?.file || "").toLowerCase();
      if (fileName.endsWith(".x_t")) return "Parasolid XT";
      return format ? format.replaceAll("_", " ") : "Unknown";
    }

    function previewMetadata(report) {
      return report?.preview || {
        strategy: "report_preview",
        label: "Preview",
        description: "Rendered from the best available decoded preview geometry in the report."
      };
    }

    function previewSourceLabel(report) {
      return previewMetadata(report).label || "Preview";
    }

    function previewSourceDescription(report) {
      return previewMetadata(report).description || "Rendered from the best available decoded preview geometry in the report.";
    }

    function previewNoteText(report) {
      const baseControls = "Orbit with drag. Pan with Shift + drag or right-drag. Mouse wheel zooms. Double-click fits the model.";
      const strategy = previewMetadata(report).strategy;
      if (strategy === "step_quick_preview") {
        return `${baseControls} This STEP file is shown with a fast topology-based preview first, then it will refine toward the full STEP mesh automatically.`;
      }
      if (strategy === "step_import_preview") {
        return `${baseControls} This file is being shown as a direct STEP import preview.`;
      }
      if (strategy === "stl_mesh_preview") {
        return `${baseControls} This file is being shown as a direct STL triangle mesh preview in WebGL.`;
      }
      if (strategy === "json_reconstruction_preview") {
        return `${baseControls} This file is being shown as a reconstructed part preview built from Siemens NX JSON topology and geometry.`;
      }
      if (strategy === "fused_json_step_preview") {
        const stepName = report?.fusion?.reference_step?.file_name || "the matching STEP file";
        return `${baseControls} This Fusion preview starts from the JSON reconstruction, then improves the faces and edges by referencing ${stepName}.`;
      }
      return `${baseControls} The preview uses the best decoded geometry available in the current report.`;
    }

    function renderPreviewNote() {
      const node = byId("preview-note");
      if (!node) return;
      node.textContent = previewNoteText(activeReport());
    }

    function previewReadyMessage(report) {
      if (!report) return "Preview ready.";
      const strategy = previewMetadata(report).strategy;
      if (strategy === "step_quick_preview") return `Loaded ${report.file_name}. Fast STEP preview ready; refining the full STEP mesh in the background.`;
      if (strategy === "step_import_preview" && report?.step_import?.analysis_pending) {
        return `Loaded ${report.file_name}. STEP preview ready; detailed analysis is continuing in the background.`;
      }
      if (strategy === "step_import_preview") return `Loaded ${report.file_name}. STEP import preview ready.`;
      if (strategy === "stl_mesh_preview") return `Loaded ${report.file_name}. STL mesh preview ready.`;
      if (strategy === "json_reconstruction_preview") return `Loaded ${report.file_name}. JSON reconstruction preview ready.`;
      if (strategy === "fused_json_step_preview") return `Loaded ${report.file_name}. STEP-referenced Fusion preview ready.`;
      return `Loaded ${report.file_name}. Preview ready.`;
    }

    function loadedReportsStatus(reports) {
      const previewKinds = new Set((reports || []).map((report) => previewMetadata(report).strategy));
      const stepAnalysisPending = (reports || []).some((report) => report?.step_import?.analysis_pending);
      const fusionCount = (reports || []).filter((report) => previewMetadata(report).strategy === "fused_json_step_preview").length;
      if (fusionCount) {
        return `Loaded ${reports.length} file(s) and built ${fusionCount} STEP-referenced Fusion preview(s). Select one to inspect it or select two to compare them.`;
      }
      if (previewKinds.has("step_import_preview") && previewKinds.has("json_reconstruction_preview")) {
        return `Loaded ${reports.length} file(s). STEP imports, STL meshes, and JSON reconstructions are ready; matching STEP+JSON pairs can be fused automatically or compared side-by-side.`;
      }
      if (stepAnalysisPending) {
        return `Loaded ${reports.length} file(s). STEP previews are ready and detailed analysis is continuing in the background.`;
      }
      return `Loaded ${reports.length} file(s). Select one to preview it or two to compare them.`;
    }

    function stepBackendLabel(report) {
      const sourceFormat = report?.transmit_info?.source_format || "";
      if (!["step_file", "fused_step_json"].includes(sourceFormat)) return "Not applicable";
      const backend = report?.step_import?.backend || report?.entity_hints?.step_backend || "native_step_parser";
      if (backend === "browser_occt_import_js") return "Browser STEP Import";
      if (backend === "native_step_parser" && report?.step_import?.refinement_pending) {
        return "Built-in STEP topology importer (refining mesh preview)";
      }
      if (backend === "opencascade_occ") return "Open Cascade";
      if (backend === "native_step_parser") return "Built-in STEP topology importer";
      return backend.replaceAll("_", " ");
    }

    function fusionLabel(report) {
      if (!report?.fusion?.enabled) return "Off";
      const mlPrefix = report?.fusion?.ml_assisted ? "ML-assisted " : "";
      const stepName = report?.fusion?.reference_step?.file_name || report?.transmit_info?.reference_step_name;
      const faceRegionCounts = report.fusion?.face_region_source_counts || {};
      const edgeRegionCounts = report.fusion?.edge_region_source_counts || {};
      if (Object.keys(faceRegionCounts).length || Object.keys(edgeRegionCounts).length) {
        return `${mlPrefix}On${stepName ? ` via ${stepName}` : ""} (${faceRegionCounts.json || 0} JSON face regions, ${faceRegionCounts.step || 0} STEP face regions; ${edgeRegionCounts.json || 0} JSON edge regions, ${edgeRegionCounts.step || 0} STEP edge regions)`;
      }
      const faceCounts = report.fusion?.face_source_counts || {};
      const edgeCounts = report.fusion?.edge_source_counts || {};
      return `${mlPrefix}On${stepName ? ` via ${stepName}` : ""} (${faceCounts.json || 0} JSON face bodies, ${faceCounts.step || 0} STEP face bodies; ${edgeCounts.json || 0} JSON edge bodies, ${edgeCounts.step || 0} STEP edge bodies)`;
    }

    function fusionMlLabel(report) {
      if (!report?.fusion?.ml_assisted) return "Off";
      const model = report?.fusion?.ml_model?.name || "Unknown";
      const decisions = report?.fusion?.body_decisions || [];
      const confidences = decisions
        .map((item) => Number(item?.ml_match_confidence))
        .filter((value) => Number.isFinite(value));
      if (!confidences.length) return model;
      const average = confidences.reduce((sum, value) => sum + value, 0) / confidences.length;
      return `${model} (avg confidence ${average.toFixed(2)})`;
    }

    function fusionDecisionLines(report, limit = 4) {
      const decisions = report?.fusion?.body_decisions || [];
      return decisions.slice(0, limit).map((item) => {
        const bodyIndex = item?.body_index ?? "?";
        const matchReason = item?.match_reason || "unknown";
        const confidence = Number(item?.ml_match_confidence);
        const confidenceText = Number.isFinite(confidence) ? confidence.toFixed(2) : "n/a";
        return `Body ${bodyIndex}: ${matchReason}, confidence ${confidenceText}, faces ${item?.face_source || "?"}, edges ${item?.edge_source || "?"}`;
      });
    }

    function canFuseSelection() {
      const reports = selectedReports();
      if (reports.length !== 2) return false;
      const formats = new Set(reports.map((report) => report?.transmit_info?.source_format || ""));
      return formats.has("siemens_nx_json") && formats.has("step_file");
    }

    function formatBounds(bounds) {
      if (!bounds) return "Not inferred";
      return bounds.size.map((value) => Number(value).toFixed(6)).join(" x ");
    }

    function formatLength(value) {
      const numeric = Number(value || 0);
      return `${(numeric * 1000).toFixed(3)} mm (${(numeric * 39.37007874015748).toFixed(3)} in)`;
    }

    function formatSignedInches(value) {
      const inches = Number(value || 0) * 39.37007874015748;
      return `${inches >= 0 ? "+" : ""}${inches.toFixed(3)} in`;
    }

    function formatInches(value) {
      return `${(Number(value || 0) * 39.37007874015748).toFixed(3)} in`;
    }

    function reportUnitLabel(report) {
      return (
        report?.source_part_summary?.units
        || report?.transmit_info?.original_units
        || report?.header?.PART1?.UNITS
        || ""
      );
    }

    function reportLengthScale(report) {
      const normalized = String(reportUnitLabel(report) || "").trim().toLowerCase();
      if (["inch", "inches", "in"].includes(normalized)) return 0.0254;
      if (["millimeter", "millimeters", "mm"].includes(normalized)) return 0.001;
      if (["centimeter", "centimeters", "cm"].includes(normalized)) return 0.01;
      if (["meter", "meters", "metre", "metres", "m"].includes(normalized)) return 1.0;
      return 1.0;
    }

    function scalePoint(point, scale = 1) {
      const normalized = pointArray(point);
      if (!normalized) return null;
      return normalized.map((value) => value * scale);
    }

    function changeVerb(delta) {
      return delta >= 0 ? "increased" : "decreased";
    }

    function capitalizeLabel(value) {
      return String(value || "")
        .replaceAll("_", " ")
        .replace(/\\b\\w/g, (letter) => letter.toUpperCase());
    }

    function componentIdentity(component, bodyIndex, componentIndex) {
      const metadata = component?.metadata || {};
      const primitive = metadata.primitive || component?.name || "component";
      const axis = metadata.axis || "na";
      return {
        id: `b${bodyIndex}-c${componentIndex}-${primitive}-${axis}`,
        primitive,
        axis: metadata.axis || null,
        bodyIndex,
        componentIndex,
      };
    }

    function componentLabel(component, identity, siblingCount = 1) {
      const componentContext = component?.metadata?.component_context || {};
      const explicitName = componentContext.display_name || componentContext.name || component?.name;
      if (explicitName) return capitalizeLabel(explicitName);
      const primitive = identity.primitive;
      if (primitive === "box") return siblingCount > 1 ? "Base block" : "Block";
      if (primitive === "cylinder") return "Cylinder";
      if (primitive === "sphere") return "Sphere";
      return component?.name ? capitalizeLabel(component.name) : `Component ${identity.componentIndex + 1}`;
    }

    function hashString(value) {
      let hash = 0;
      for (const char of String(value || "")) {
        hash = ((hash << 5) - hash) + char.charCodeAt(0);
        hash |= 0;
      }
      return Math.abs(hash);
    }

    function inferredComponentColor(component, componentName, identity) {
      const text = `${componentName || ""} ${component?.name || ""} ${identity?.primitive || ""}`.toLowerCase();

      if (/(seal|o-ring|oring|gasket)/.test(text)) return [42, 42, 46];
      if (/(body|housing|case|barrel)/.test(text)) return [188, 154, 126];
      if (/(lever|arm|bracket|link)/.test(text)) return [206, 210, 216];
      if (/(piston|pin|rod|shaft|cotter)/.test(text)) return [214, 217, 222];
      if (/(washer|spacer|bushing)/.test(text)) return [196, 199, 205];
      if (/(spring|clip)/.test(text)) return [122, 128, 138];

      const palette = [
        [206, 210, 216],
        [188, 154, 126],
        [214, 217, 222],
        [176, 182, 192],
        [158, 166, 178],
      ];
      return palette[hashString(text) % palette.length];
    }

    function nxColorIndexToRgb(index) {
      const palette = {
        1: [0, 0, 255],
        2: [255, 0, 0],
        3: [0, 255, 0],
        4: [0, 255, 255],
        5: [255, 255, 0],
        6: [255, 0, 255],
        7: [255, 255, 255],
        8: [80, 80, 80],
        9: [168, 168, 168],
        10: [122, 184, 255],
        11: [255, 160, 122],
        12: [144, 238, 144],
        13: [255, 214, 102],
        14: [214, 170, 122],
        15: [196, 199, 205],
        16: [160, 164, 172],
        17: [92, 96, 104],
        18: [42, 42, 46],
        186: [188, 154, 126],
      };
      return palette[index] || null;
    }

    function displayColorFromProperties(displayProperties, fallback = null) {
      if (!displayProperties) return fallback;
      if (Array.isArray(displayProperties.rgb) && displayProperties.rgb.length >= 3) {
        return displayProperties.rgb.slice(0, 3).map((value) => Number(value));
      }
      if (Number.isFinite(Number(displayProperties.color_index))) {
        return nxColorIndexToRgb(Number(displayProperties.color_index)) || fallback;
      }
      return fallback;
    }

    function displayOpacityFromProperties(displayProperties, fallback = 1) {
      if (!displayProperties) return fallback;
      const translucency = Number(displayProperties.translucency);
      if (!Number.isFinite(translucency)) return fallback;
      return Math.max(0.16, Math.min(1, 1 - (translucency / 100)));
    }

    function isBlankedFromDisplay(displayProperties) {
      return Boolean(displayProperties?.is_blanked);
    }

    function boundsFromCenterSize(center, size) {
      return {
        min: center.map((value, index) => value - size[index] / 2),
        max: center.map((value, index) => value + size[index] / 2),
        size: [...size],
      };
    }

    function cylinderBounds(axis, center, radius, height) {
      const extents = [radius, radius, radius];
      const axisIndex = AXES.indexOf(axis || "z");
      extents[Math.max(axisIndex, 0)] = height / 2;
      return {
        min: center.map((value, index) => value - extents[index]),
        max: center.map((value, index) => value + extents[index]),
        size: extents.map((value) => value * 2),
      };
    }

    function perpendicularAxisForCylinder(axis) {
      if (axis === "x") return "y";
      if (axis === "y") return "x";
      return "x";
    }

    function longestBoundsAxis(bounds) {
      if (!bounds?.size) return "z";
      let bestAxis = "x";
      let bestValue = Number(bounds.size[0] || 0);
      AXES.slice(1).forEach((axis, index) => {
        const value = Number(bounds.size[index + 1] || 0);
        if (value > bestValue) {
          bestAxis = axis;
          bestValue = value;
        }
      });
      return bestAxis;
    }

    function samplePointCloud(points, limit = 320) {
      const cloud = [...(points || [])];
      if (cloud.length <= limit) return cloud;
      const sampled = [];
      const step = cloud.length / limit;
      for (let index = 0; index < limit; index++) {
        sampled.push(cloud[Math.min(cloud.length - 1, Math.floor(index * step))]);
      }
      return sampled;
    }

    function meshProfileFromPointCloud(pointCloud, bounds, stats = {}) {
      if (!bounds || !pointCloud?.length) return null;
      const sampled = samplePointCloud(pointCloud, 320);
      const size = (bounds.size || [0, 0, 0]).map((value) => Math.max(Number(value) || 0, 1e-9));
      const min = (bounds.min || [0, 0, 0]).map(Number);
      const bins = 6;
      const occupancy = new Set();
      const occupancyCounts = new Map();
      const centroid = [0, 0, 0];
      const radialDistances = [];

      sampled.forEach((point) => {
        const normalized = point.map((value, index) => Math.max(0, Math.min(1, (Number(value) - min[index]) / size[index])));
        centroid[0] += normalized[0];
        centroid[1] += normalized[1];
        centroid[2] += normalized[2];
        const binKey = normalized
          .map((value) => Math.min(bins - 1, Math.max(0, Math.floor(value * bins))))
          .join("|");
        occupancy.add(binKey);
        occupancyCounts.set(binKey, Number(occupancyCounts.get(binKey) || 0) + 1);
        radialDistances.push(Math.hypot(normalized[0] - 0.5, normalized[1] - 0.5, normalized[2] - 0.5));
      });

      const count = sampled.length || 1;
      const radialMean = radialDistances.length
        ? radialDistances.reduce((sum, value) => sum + value, 0) / radialDistances.length
        : 0;
      const radialStd = radialDistances.length
        ? Math.sqrt(radialDistances.reduce((sum, value) => sum + ((value - radialMean) ** 2), 0) / radialDistances.length)
        : 0;
      const maxExtent = Math.max(...size, 1e-9);
      return {
        pointCount: pointCloud.length,
        sampleCount: sampled.length,
        faceCount: Number(stats.faceCount || 0),
        edgeCount: Number(stats.edgeCount || 0),
        occupancy,
        occupancyCounts,
        occupancyCount: occupancy.size,
        centroid: centroid.map((value) => value / count),
        radialMean,
        radialStd,
        extentRatios: size.map((value) => value / maxExtent),
      };
    }

    function meshProfileDelta(leftProfile, rightProfile) {
      if (!leftProfile || !rightProfile) return null;
      const union = new Set([...leftProfile.occupancy, ...rightProfile.occupancy]);
      const intersectionCount = [...leftProfile.occupancy].filter((key) => rightProfile.occupancy.has(key)).length;
      const mismatchRatio = union.size ? 1 - (intersectionCount / union.size) : 0;
      const centroidDelta = Math.hypot(
        rightProfile.centroid[0] - leftProfile.centroid[0],
        rightProfile.centroid[1] - leftProfile.centroid[1],
        rightProfile.centroid[2] - leftProfile.centroid[2]
      );
      const radialDelta = Math.abs(Number(rightProfile.radialMean || 0) - Number(leftProfile.radialMean || 0))
        + Math.abs(Number(rightProfile.radialStd || 0) - Number(leftProfile.radialStd || 0));
      const densityDelta = Math.abs(Number(rightProfile.occupancyCount || 0) - Number(leftProfile.occupancyCount || 0))
        / Math.max(Number(leftProfile.occupancyCount || 0), Number(rightProfile.occupancyCount || 0), 1);
      const extentDelta = Math.max(
        ...((leftProfile.extentRatios || []).map((value, index) =>
          Math.abs(Number((rightProfile.extentRatios || [])[index] || 0) - Number(value || 0))
        )),
        0
      );
      return { mismatchRatio, centroidDelta, radialDelta, densityDelta, extentDelta };
    }

    function meshProfileLikelyScaleOnly(leftComponent, rightComponent, profileDelta, properties = []) {
      if (!leftComponent || !rightComponent || !profileDelta) return false;
      const sizeProperties = (properties || []).filter((property) => property?.kind === "size");
      if (!sizeProperties.length || sizeProperties.length > 2) return false;
      const meshLike = leftComponent.primitive === "occ_mesh" || rightComponent.primitive === "occ_mesh";
      if (!meshLike) return false;

      const relativeSizeDeltas = sizeProperties
        .map((property) => relativeDelta(property.leftValue, property.rightValue))
        .filter((value) => Number.isFinite(value));
      const maxRelativeSizeDelta = Math.max(...relativeSizeDeltas, 0);
      const boundedCrossSectionShift = profileDelta.centroidDelta < 0.08 && profileDelta.radialDelta < 0.12;
      const extentTracksSize = profileDelta.extentDelta <= Math.max(0.12, maxRelativeSizeDelta * 1.35);
      const mismatchWithinScaleBand = profileDelta.mismatchRatio <= 0.7;
      return boundedCrossSectionShift && extentTracksSize && mismatchWithinScaleBand;
    }

    function averagePoint(points) {
      if (!Array.isArray(points) || !points.length) return null;
      const totals = [0, 0, 0];
      points.forEach((point) => {
        totals[0] += Number(point[0] || 0);
        totals[1] += Number(point[1] || 0);
        totals[2] += Number(point[2] || 0);
      });
      return totals.map((value) => value / points.length);
    }

    function meshBinKey(point, bounds, bins = 8) {
      if (!bounds || !Array.isArray(point) || point.length < 3) return null;
      const min = bounds.min || [0, 0, 0];
      const size = (bounds.size || [0, 0, 0]).map((value) => Math.max(Number(value) || 0, 1e-9));
      return point
        .map((value, index) => Math.min(
          bins - 1,
          Math.max(0, Math.floor(((Number(value) - Number(min[index] || 0)) / size[index]) * bins))
        ))
        .join("|");
    }

    function colorDistance(leftColor, rightColor) {
      if (!Array.isArray(leftColor) || !Array.isArray(rightColor)) return 0;
      return Math.hypot(
        Number(rightColor[0] || 0) - Number(leftColor[0] || 0),
        Number(rightColor[1] || 0) - Number(leftColor[1] || 0),
        Number(rightColor[2] || 0) - Number(leftColor[2] || 0)
      );
    }

    function formatRgb(rgb) {
      if (!Array.isArray(rgb) || rgb.length < 3) return "Unknown";
      return `rgb(${rgb.slice(0, 3).map((value) => Math.round(Number(value || 0))).join(", ")})`;
    }

    function componentFaceEntries(component, bounds, identity) {
      const bodyIndex = previewBodyIndex(component, identity.bodyIndex || 0);
      return (component?.faces || [])
        .map((face, faceIndex) => {
          const vertices = Array.isArray(face?.vertices) ? face.vertices : [];
          if (vertices.length < 3) return null;
          const centroid = averagePoint(vertices);
          return {
            rawFaceKey: rawIdentityKey(
              face?.metadata?.object_identity,
              `body:${Number(face?.metadata?.source_body_index || bodyIndex)}:face:${Number(face?.metadata?.source_face_index || faceIndex + 1)}`
            ),
            centroid,
            binKey: meshBinKey(centroid, bounds),
          };
        })
        .filter((entry) => entry?.rawFaceKey && entry?.binKey && entry?.centroid);
    }

    function componentEdgeEntries(component, bounds, identity) {
      const bodyIndex = previewBodyIndex(component, identity.bodyIndex || 0);
      return (component?.edges || [])
        .map((edge, edgeIndex) => {
          const points = Array.isArray(edge?.points) ? edge.points : [];
          if (points.length < 2) return null;
          const centroid = averagePoint(points);
          return {
            rawEdgeKey: rawIdentityKey(
              edge?.object_identity,
              `body:${bodyIndex}:edge:${Number(edge?.source_edge_index || edgeIndex + 1)}`
            ),
            centroid,
            binKey: meshBinKey(centroid, bounds),
          };
        })
        .filter((entry) => entry?.rawEdgeKey && entry?.binKey && entry?.centroid);
    }

    function averageEntryPoint(entries, key) {
      const points = (entries || []).map((entry) => entry?.[key]).filter(Boolean);
      return averagePoint(points);
    }

    function relativeDelta(leftValue, rightValue) {
      return Math.abs(Number(rightValue || 0) - Number(leftValue || 0))
        / Math.max(Math.abs(Number(leftValue || 0)), Math.abs(Number(rightValue || 0)), 1e-9);
    }

    function componentSimilarityCost(leftComponent, rightComponent) {
      const leftBounds = leftComponent?.bounds;
      const rightBounds = rightComponent?.bounds;
      const leftSize = leftBounds?.size || [0, 0, 0];
      const rightSize = rightBounds?.size || [0, 0, 0];
      const referenceSpan = Math.max(
        Math.hypot(...leftSize.map(Number)),
        Math.hypot(...rightSize.map(Number)),
        1e-9
      );
      const centerCost = Math.hypot(
        Number(rightComponent.center?.[0] || 0) - Number(leftComponent.center?.[0] || 0),
        Number(rightComponent.center?.[1] || 0) - Number(leftComponent.center?.[1] || 0),
        Number(rightComponent.center?.[2] || 0) - Number(leftComponent.center?.[2] || 0)
      ) / referenceSpan;
      const sizeCost = AXES.reduce((sum, _axis, index) =>
        sum + relativeDelta(leftSize[index], rightSize[index]), 0
      ) / AXES.length;
      const primitivePenalty = leftComponent.primitive === rightComponent.primitive ? 0 : 2.2;
      const axisPenalty = (leftComponent.axis || null) === (rightComponent.axis || null)
        ? 0
        : ((leftComponent.axis || rightComponent.axis) ? 0.35 : 0);
      const faceCost = relativeDelta(leftComponent.faceCount || 0, rightComponent.faceCount || 0) * 0.8;
      const edgeCost = relativeDelta(leftComponent.edgeCount || 0, rightComponent.edgeCount || 0) * 0.45;
      const colorCost = colorDistance(leftComponent.displayColor, rightComponent.displayColor) / 255;
      const meshDelta = meshProfileDelta(leftComponent.meshProfile, rightComponent.meshProfile);
      const meshCost = meshDelta
        ? (meshDelta.mismatchRatio * 1.9) + (meshDelta.centroidDelta * 0.8) + (meshDelta.radialDelta * 0.9) + (meshDelta.extentDelta * 0.7)
        : 0;
      const labelBonus = leftComponent.label && rightComponent.label && leftComponent.label === rightComponent.label ? -0.35 : 0;
      const bodyBonus = leftComponent.bodyRawKey && rightComponent.bodyRawKey && leftComponent.bodyRawKey === rightComponent.bodyRawKey ? -0.7 : 0;
      return primitivePenalty + axisPenalty + (centerCost * 3.6) + (sizeCost * 3.4) + faceCost + edgeCost + (colorCost * 0.4) + meshCost + labelBonus + bodyBonus;
    }

    function matchQualityFromCost(cost) {
      const normalized = Math.max(0, Math.min(1, 1 - (Number(cost || 0) / 5)));
      if (normalized >= 0.82) return { label: "High", value: normalized };
      if (normalized >= 0.58) return { label: "Medium", value: normalized };
      return { label: "Low", value: normalized };
    }

    function componentRegionHighlights(leftComponent, rightComponent) {
      const leftFaceEntries = leftComponent?.faceEntries || [];
      const rightFaceEntries = rightComponent?.faceEntries || [];
      const leftEdgeEntries = leftComponent?.edgeEntries || [];
      const rightEdgeEntries = rightComponent?.edgeEntries || [];
      const leftFaceBins = new Set(leftFaceEntries.map((entry) => entry.binKey));
      const rightFaceBins = new Set(rightFaceEntries.map((entry) => entry.binKey));
      const leftEdgeBins = new Set(leftEdgeEntries.map((entry) => entry.binKey));
      const rightEdgeBins = new Set(rightEdgeEntries.map((entry) => entry.binKey));
      const leftChangedFaces = leftFaceEntries.filter((entry) => !rightFaceBins.has(entry.binKey)).slice(0, 180);
      const rightChangedFaces = rightFaceEntries.filter((entry) => !leftFaceBins.has(entry.binKey)).slice(0, 180);
      const leftChangedEdges = leftEdgeEntries.filter((entry) => !rightEdgeBins.has(entry.binKey)).slice(0, 120);
      const rightChangedEdges = rightEdgeEntries.filter((entry) => !leftEdgeBins.has(entry.binKey)).slice(0, 120);
      const meshDelta = meshProfileDelta(leftComponent.meshProfile, rightComponent.meshProfile);
      const mismatchPercent = meshDelta ? `${(Number(meshDelta.mismatchRatio || 0) * 100).toFixed(0)}%` : null;

      return {
        highlightRawFaces: {
          left: leftChangedFaces.map((entry) => entry.rawFaceKey),
          right: rightChangedFaces.map((entry) => entry.rawFaceKey),
        },
        highlightRawEdges: {
          left: leftChangedEdges.map((entry) => entry.rawEdgeKey),
          right: rightChangedEdges.map((entry) => entry.rawEdgeKey),
        },
        annotations: {
          left: leftChangedFaces.length || leftChangedEdges.length
            ? [{
                point: averageEntryPoint(leftChangedFaces, "centroid") || averageEntryPoint(leftChangedEdges, "centroid") || leftComponent.center,
                lines: [`${leftComponent.label} changed region${mismatchPercent ? ` (${mismatchPercent} mismatch)` : ""}`],
              }]
            : [],
          right: rightChangedFaces.length || rightChangedEdges.length
            ? [{
                point: averageEntryPoint(rightChangedFaces, "centroid") || averageEntryPoint(rightChangedEdges, "centroid") || rightComponent.center,
                lines: [`${rightComponent.label} changed region${mismatchPercent ? ` (${mismatchPercent} mismatch)` : ""}`],
              }]
            : [],
        },
      };
    }

    function describeComponent(component, identity, siblingCount = 1) {
      const metadata = component?.metadata || {};
      const primitive = identity.primitive;
      const label = componentLabel(component, identity, siblingCount);
      const bodyRawKey = previewBodyRawKey(component, identity.bodyIndex || 0);
      const displayColor = displayColorFromProperties(
        component?.metadata?.display_properties,
        displayColorFromProperties(component?.metadata?.component_context?.display_properties, null)
      );

      if (primitive === "box") {
        const dimensions = metadata.dimensions || {};
        const center = (metadata.center || [0, 0, 0]).map(Number);
        const size = AXES.map((axis) => Number(dimensions[axis] || 0));
        return {
          ...identity,
          label,
          bodyRawKey,
          displayColor,
          bounds: boundsFromCenterSize(center, size),
          center,
          faceCount: component?.faces?.length || 0,
          edgeCount: component?.edges?.length || 0,
          measurements: Object.fromEntries(AXES.map((axis, index) => [axis, size[index]])),
        };
      }

      if (primitive === "cylinder") {
        const axis = metadata.axis || "z";
        const radius = Number(metadata.radius || 0);
        const height = Number(metadata.height || 0);
        const center = (metadata.center || [0, 0, 0]).map(Number);
        return {
          ...identity,
          label,
          axis,
          bodyRawKey,
          displayColor,
          center,
          bounds: cylinderBounds(axis, center, radius, height),
          faceCount: component?.faces?.length || 0,
          edgeCount: component?.edges?.length || 0,
          measurements: {
            height,
            radius,
            diameter: radius * 2,
          },
        };
      }

      if (primitive === "sphere") {
        const radius = Number(metadata.radius || 0);
        const center = (metadata.center || [0, 0, 0]).map(Number);
        const size = [radius * 2, radius * 2, radius * 2];
        return {
          ...identity,
          label,
          bodyRawKey,
          displayColor,
          center,
          bounds: boundsFromCenterSize(center, size),
          faceCount: component?.faces?.length || 0,
          edgeCount: component?.edges?.length || 0,
          measurements: {
            diameter: radius * 2,
            radius,
          },
        };
      }

      const pointCloud = [
        ...(component?.faces || []).flatMap((face) => face.vertices || []),
        ...(component?.edges || []).flatMap((edge) => edge.points || []),
      ].map((point) => point.map(Number));
      const bounds = pointCloud.length ? boundsFromPoints(pointCloud) : null;
      const faceEntries = componentFaceEntries(component, bounds, identity);
      const edgeEntries = componentEdgeEntries(component, bounds, identity);
      return {
        ...identity,
        label,
        bodyRawKey,
        displayColor,
        center: bounds
          ? bounds.min.map((value, index) => (value + bounds.max[index]) / 2)
          : [0, 0, 0],
        bounds,
        faceCount: component?.faces?.length || 0,
        edgeCount: component?.edges?.length || 0,
        faceEntries,
        edgeEntries,
        meshProfile: meshProfileFromPointCloud(pointCloud, bounds, {
          faceCount: component?.faces?.length || 0,
          edgeCount: component?.edges?.length || 0,
        }),
        measurements: bounds
          ? Object.fromEntries(AXES.map((axis, index) => [axis, Number(bounds.size[index] || 0)]))
          : {},
      };
    }

    function reportComponents(report) {
      const results = [];
      previewBodies(report).forEach((body, bodyIndex) => {
        if (body?.metadata?.primitive === "compound" && Array.isArray(body.metadata.components)) {
          const componentCount = body.metadata.components.length;
          body.metadata.components.forEach((component, componentIndex) => {
            results.push(describeComponent(component, componentIdentity(component, bodyIndex, componentIndex), componentCount));
          });
          return;
        }
        results.push(describeComponent(body, componentIdentity(body, bodyIndex, 0), 1));
      });
      return results.filter((component) => component && component.bounds);
    }

    function compareAxisDimensions(left, right) {
      const leftBounds = left?.geometry_hints?.bounds;
      const rightBounds = right?.geometry_hints?.bounds;
      if (!leftBounds || !rightBounds) return [];

      return AXES.map((axis, index) => {
        const leftValue = Number(leftBounds.size?.[index] || 0);
        const rightValue = Number(rightBounds.size?.[index] || 0);
        const delta = rightValue - leftValue;
        return {
          axis,
          leftValue,
          rightValue,
          delta,
          changed: Math.abs(delta) > DIMENSION_TOLERANCE
        };
      });
    }

    function matchRightComponent(leftComponent, rightComponents, used) {
      let match = rightComponents.find((candidate) => !used.has(candidate.id) && candidate.id === leftComponent.id);
      if (match) return { component: match, cost: 0 };

      const candidates = rightComponents
        .filter((candidate) => !used.has(candidate.id))
        .map((candidate) => ({ candidate, cost: componentSimilarityCost(leftComponent, candidate) }))
        .sort((a, b) => a.cost - b.cost);

      if (!candidates.length) return null;
      const best = candidates[0];
      if (best.cost > 4.1) return null;
      return { component: best.candidate, cost: best.cost };
    }

    function componentPropertyChanges(leftComponent, rightComponent) {
      const primitive = leftComponent.primitive;
      const properties = [];

      if (primitive === "box") {
        AXES.forEach((axis) => {
          const leftValue = Number(leftComponent.measurements[axis] || 0);
          const rightValue = Number(rightComponent.measurements[axis] || 0);
          const delta = rightValue - leftValue;
          if (Math.abs(delta) > DIMENSION_TOLERANCE) {
            properties.push({ kind: "size", axis, label: `${axis.toUpperCase()} size`, leftValue, rightValue, delta });
          }
        });
        return properties;
      }

      if (primitive === "cylinder") {
        const heightDelta = Number(rightComponent.measurements.height || 0) - Number(leftComponent.measurements.height || 0);
        if (Math.abs(heightDelta) > DIMENSION_TOLERANCE) {
          properties.push({
            kind: "height",
            axis: leftComponent.axis || rightComponent.axis || "z",
            label: `${(leftComponent.axis || rightComponent.axis || "z").toUpperCase()} height`,
            leftValue: Number(leftComponent.measurements.height || 0),
            rightValue: Number(rightComponent.measurements.height || 0),
            delta: heightDelta,
          });
        }

        const diameterLeft = Number(leftComponent.measurements.diameter || 0);
        const diameterRight = Number(rightComponent.measurements.diameter || 0);
        const diameterDelta = diameterRight - diameterLeft;
        if (Math.abs(diameterDelta) > DIMENSION_TOLERANCE) {
          properties.push({
            kind: "diameter",
            axis: perpendicularAxisForCylinder(leftComponent.axis || rightComponent.axis || "z"),
            label: "Diameter",
            leftValue: diameterLeft,
            rightValue: diameterRight,
            delta: diameterDelta,
          });
        }
        return properties;
      }

      AXES.forEach((axis, index) => {
        const leftValue = Number(leftComponent.bounds?.size?.[index] || 0);
        const rightValue = Number(rightComponent.bounds?.size?.[index] || 0);
        const delta = rightValue - leftValue;
        if (Math.abs(delta) > DIMENSION_TOLERANCE) {
          properties.push({ kind: "size", axis, label: `${axis.toUpperCase()} size`, leftValue, rightValue, delta });
        }
      });

      const leftMesh = leftComponent.meshProfile;
      const rightMesh = rightComponent.meshProfile;
      if (leftMesh && rightMesh) {
        const profileDelta = meshProfileDelta(leftMesh, rightMesh);
        const scaleOnlyProfile = meshProfileLikelyScaleOnly(leftComponent, rightComponent, profileDelta, properties);
        const faceDelta = Number(rightMesh.faceCount || 0) - Number(leftMesh.faceCount || 0);
        if (!scaleOnlyProfile && Math.abs(faceDelta) >= 1) {
          properties.push({
            kind: "facet_count",
            axis: longestBoundsAxis(leftComponent.bounds || rightComponent.bounds),
            label: "Mesh faces",
            leftValue: Number(leftMesh.faceCount || 0),
            rightValue: Number(rightMesh.faceCount || 0),
            delta: faceDelta,
          });
        }

        const edgeDelta = Number(rightMesh.edgeCount || 0) - Number(leftMesh.edgeCount || 0);
        if (!scaleOnlyProfile && Math.abs(edgeDelta) >= 1) {
          properties.push({
            kind: "edge_count",
            axis: longestBoundsAxis(leftComponent.bounds || rightComponent.bounds),
            label: "Mesh edges",
            leftValue: Number(leftMesh.edgeCount || 0),
            rightValue: Number(rightMesh.edgeCount || 0),
            delta: edgeDelta,
          });
        }

        if (!scaleOnlyProfile && profileDelta && (
          profileDelta.mismatchRatio >= 0.08
          || profileDelta.centroidDelta >= 0.03
          || profileDelta.radialDelta >= 0.045
          || profileDelta.extentDelta >= 0.06
        )) {
          properties.push({
            kind: "shape_profile",
            axis: longestBoundsAxis(leftComponent.bounds || rightComponent.bounds),
            label: "Shape profile",
            leftValue: profileDelta.mismatchRatio,
            rightValue: 0,
            delta: profileDelta.mismatchRatio,
            centroidDelta: profileDelta.centroidDelta,
            radialDelta: profileDelta.radialDelta,
            extentDelta: profileDelta.extentDelta,
          });
        }
      }

      const colorDeltaValue = colorDistance(leftComponent.displayColor, rightComponent.displayColor);
      if (colorDeltaValue >= 18) {
        properties.push({
          kind: "color",
          axis: longestBoundsAxis(leftComponent.bounds || rightComponent.bounds),
          label: "Color",
          leftValue: leftComponent.displayColor,
          rightValue: rightComponent.displayColor,
          delta: colorDeltaValue,
        });
      }
      return properties;
    }

    function propertySeverity(property) {
      if (!property) return 0;
      if (property.kind === "shape_profile") return 6 + ((Number(property.delta || 0) * 10) || 0);
      if (property.kind === "facet_count" || property.kind === "edge_count") return 3 + Math.min(4, Math.abs(Number(property.delta || 0)) / 4);
      if (property.kind === "color") return 1.5 + (Number(property.delta || 0) / 64);
      return 1 + Math.abs(Number(property.delta || 0));
    }

    function changeSeverity(change) {
      return (change?.properties || []).reduce((sum, property) => sum + propertySeverity(property), 0);
    }

    function cylinderDiameterEndpoints(component, axis) {
      const center = component.center.map(Number);
      const radius = Number(component.measurements.radius || component.measurements.diameter / 2 || 0);
      const alongAxis = component.axis || "z";
      const capCoordinate = component.bounds.max[AXES.indexOf(alongAxis)];
      if (alongAxis === "x") {
        if (axis === "z") return [[center[0], center[1], capCoordinate - radius], [center[0], center[1], capCoordinate + radius]];
        return [[center[0], center[1] - radius, capCoordinate], [center[0], center[1] + radius, capCoordinate]];
      }
      if (alongAxis === "y") {
        if (axis === "z") return [[center[0], center[1], capCoordinate - radius], [center[0], center[1], capCoordinate + radius]];
        return [[center[0] - radius, center[1], capCoordinate], [center[0] + radius, center[1], capCoordinate]];
      }
      return [[center[0] - radius, center[1], capCoordinate], [center[0] + radius, center[1], capCoordinate]];
    }

    function annotationEndpointsForProperty(component, property) {
      if (!component?.bounds) return null;
      if (property.kind === "diameter" && component.primitive === "cylinder") {
        return cylinderDiameterEndpoints(component, property.axis);
      }
      return axisDimensionEndpoints(component.bounds, property.axis || "z");
    }

    function measurementLineForProperty(component, property) {
      const endpoints = annotationEndpointsForProperty(component, property);
      const axis = property.axis || "z";
      const axisIndex = AXES.indexOf(axis);
      if (!endpoints || axisIndex < 0) return null;
      const start = endpoints[0].map(Number);
      const end = endpoints[1].map(Number);
      return {
        axis,
        axisIndex,
        start,
        end,
        minCoord: Math.min(start[axisIndex], end[axisIndex]),
        maxCoord: Math.max(start[axisIndex], end[axisIndex]),
      };
    }

    function pointOnMeasurementLine(line, coordinate) {
      const point = line.start.map(Number);
      point[line.axisIndex] = Number(coordinate);
      return point;
    }

    function measurementDifferenceSegments(referenceLine, changedLine) {
      if (!referenceLine || !changedLine) return [];
      const overlapMin = Math.max(referenceLine.minCoord, changedLine.minCoord);
      const overlapMax = Math.min(referenceLine.maxCoord, changedLine.maxCoord);

      if (overlapMax - overlapMin <= DIMENSION_TOLERANCE) {
        return [{ start: changedLine.start, end: changedLine.end }];
      }

      const segments = [];
      if (changedLine.minCoord < overlapMin - DIMENSION_TOLERANCE) {
        segments.push({
          start: pointOnMeasurementLine(changedLine, changedLine.minCoord),
          end: pointOnMeasurementLine(changedLine, overlapMin),
        });
      }
      if (changedLine.maxCoord > overlapMax + DIMENSION_TOLERANCE) {
        segments.push({
          start: pointOnMeasurementLine(changedLine, overlapMax),
          end: pointOnMeasurementLine(changedLine, changedLine.maxCoord),
        });
      }
      return segments;
    }

    function focusSegmentsForProperty(leftComponent, rightComponent, property) {
      const leftLine = measurementLineForProperty(leftComponent, property);
      const rightLine = measurementLineForProperty(rightComponent, property);
      if (!leftLine || !rightLine) {
        return { left: [], right: [] };
      }

      const leftSpan = leftLine.maxCoord - leftLine.minCoord;
      const rightSpan = rightLine.maxCoord - rightLine.minCoord;
      if (Math.abs(rightSpan - leftSpan) <= DIMENSION_TOLERANCE) {
        return { left: [], right: [] };
      }

      if (rightSpan > leftSpan) {
        return {
          left: [],
          right: measurementDifferenceSegments(leftLine, rightLine),
        };
      }

      return {
        left: measurementDifferenceSegments(rightLine, leftLine),
        right: [],
      };
    }

    function summarizeComponentChange(change) {
      if (change.properties.some((property) => property.kind === "shape_profile")) {
        const faceProperty = change.properties.find((property) => property.kind === "facet_count");
        const edgeProperty = change.properties.find((property) => property.kind === "edge_count");
        const profileProperty = change.properties.find((property) => property.kind === "shape_profile");
        const parts = [
          `${change.label} surface shape changed visibly`,
          faceProperty ? `mesh faces ${Math.round(faceProperty.leftValue)} -> ${Math.round(faceProperty.rightValue)}` : null,
          edgeProperty ? `mesh edges ${Math.round(edgeProperty.leftValue)} -> ${Math.round(edgeProperty.rightValue)}` : null,
          profileProperty ? `profile mismatch ${(Number(profileProperty.delta || 0) * 100).toFixed(0)}%` : null,
          profileProperty?.radialDelta ? `radial signature ${(Number(profileProperty.radialDelta) * 100).toFixed(0)}%` : null,
          change.matchQuality ? `match quality ${change.matchQuality.label.toLowerCase()}` : null,
        ].filter(Boolean);
        return `${parts.join(", ")}.`;
      }

      const propertyDescriptions = change.properties.map((property) => {
        if (property.kind === "facet_count" || property.kind === "edge_count") {
          return `${property.label} changed from ${Math.round(property.leftValue)} to ${Math.round(property.rightValue)}`;
        }
        if (property.kind === "color") {
          return `${property.label} changed from ${formatRgb(property.leftValue)} to ${formatRgb(property.rightValue)}`;
        }
        return `${property.label} ${changeVerb(property.delta)} from ${formatLength(property.leftValue)} to ${formatLength(property.rightValue)} (${formatSignedInches(property.delta)})`;
      });

      if (change.primitive === "cylinder" && change.properties.length === 1 && change.properties[0].kind === "height") {
        const radiusValue = Number(change.left.measurements.radius || change.right.measurements.radius || 0);
        return `${change.label} height ${changeVerb(change.properties[0].delta)} from ${formatLength(change.properties[0].leftValue)} to ${formatLength(change.properties[0].rightValue)} (${formatSignedInches(change.properties[0].delta)}). Radius stayed ${formatLength(radiusValue)}.`;
      }

      return `${change.label}: ${propertyDescriptions.join("; ")}${change.matchQuality ? ` Match quality ${change.matchQuality.label.toLowerCase()}.` : "."}`;
    }

    function topologyLabel(report) {
      const topology = report?.model_analysis?.topology;
      if (topology?.shape_label) return topology.shape_label;

      const bodies = kernelBodies(report);
      if (bodies.length > 1) return `${bodies.length}-body import`;

      const primaryBody = bodies[0];
      const primitive = primaryBody?.metadata?.primitive;
      if (primitive === "box") return primaryBody?.name || "box";
      if (primitive === "cylinder") return "cylinder";
      if (primitive === "sphere") return "sphere";
      if (primitive === "arc_or_circle") return "arc / circle";
      if (primitive === "occ_mesh") return "mesh solid";

      return primaryBody?.name || "Not inferred";
    }

    function topologyCounts(report) {
      const topology = report?.model_analysis?.topology;
      if (topology) {
        return `${topology.face_count} faces / ${topology.edge_count} edges`;
      }

      const kernelTopology = report?.kernel_topology;
      if (kernelTopology) {
        return `${kernelTopology.faces?.length || 0} faces / ${kernelTopology.edges?.length || 0} edges`;
      }

      const kernelBody = report?.kernel_body;
      if (kernelBody) {
        if (kernelBody?.metadata?.primitive === "compound" && Array.isArray(kernelBody?.metadata?.components)) {
          const faceCount = kernelBody.metadata.components.reduce((sum, body) => sum + (body.faces?.length || 0), 0);
          const edgeCount = kernelBody.metadata.components.reduce((sum, body) => sum + (body.edges?.length || 0), 0);
          return `${faceCount} faces / ${edgeCount} edges across ${kernelBody.metadata.components.length} preview primitives`;
        }
        return `${kernelBody.faces?.length || 0} faces / ${kernelBody.edges?.length || 0} edges`;
      }

      const bodies = kernelBodies(report);
      if (bodies.length) {
        const faceCount = bodies.reduce((sum, body) => sum + (body.faces?.length || 0), 0);
        const edgeCount = bodies.reduce((sum, body) => sum + (body.edges?.length || 0), 0);
        return `${faceCount} faces / ${edgeCount} edges across ${bodies.length} bodies`;
      }

      return "Not inferred";
    }

    function kernelLabel(report) {
      const bodies = kernelBodies(report);
      if (!bodies.length) return "Not reconstructed";
      if (bodies.length === 1) return bodies[0]?.name || "Not reconstructed";
      return `multiple bodies (${bodies.length})`;
    }

    function rawIdentityKey(objectIdentity, fallback = null) {
      if (objectIdentity?.journal_identifier) return `jid:${objectIdentity.journal_identifier}`;
      if (Number.isFinite(Number(objectIdentity?.tag))) return `tag:${Number(objectIdentity.tag)}`;
      return fallback;
    }

    function previewBodyIndex(body, fallbackIndex = 0) {
      const numeric = Number(body?.metadata?.nx_body_index);
      return Number.isFinite(numeric) ? numeric : Number(fallbackIndex || 0);
    }

    function previewBodyRawKey(body, fallbackIndex = 0) {
      const objectIdentity = body?.metadata?.object_identity || body?.object_identity || {};
      return rawIdentityKey(objectIdentity, `body:${previewBodyIndex(body, fallbackIndex)}`);
    }

    function structuredBodies(report) {
      if (Array.isArray(report?.source_bodies) && report.source_bodies.length) {
        return report.source_bodies.filter(Boolean);
      }
      if (Array.isArray(report?.bodies) && report.bodies.length) {
        return report.bodies.filter(Boolean);
      }
      return [];
    }

    function structuredTopologyCounts(report) {
      const bodies = structuredBodies(report);
      const bodyCount = bodies.length || Number(report?.source_body_count || 0);
      const faceCount = bodies.reduce((sum, body) => sum + (body?.faces?.length || 0), 0);
      const edgeCount = bodies.reduce((sum, body) => sum + (body?.edges?.length || 0), 0);
      return { bodyCount, faceCount, edgeCount };
    }

    function structuredTopologyLabel(report) {
      const counts = structuredTopologyCounts(report);
      if (!counts.bodyCount && !counts.faceCount && !counts.edgeCount) return "Not available";
      return `${counts.faceCount} faces / ${counts.edgeCount} edges across ${counts.bodyCount} bodies`;
    }

    function structuredBodyName(body, fallbackIndex = 0) {
      return body?.metadata?.name || body?.name || `Body ${fallbackIndex + 1}`;
    }

    function structuredBodyIdentity(body, fallbackIndex = 0) {
      const objectIdentity = body?.metadata?.object_identity || body?.object_identity || {};
      const name = structuredBodyName(body, fallbackIndex);
      const bodyIndex = Number(body?.index || (fallbackIndex + 1));
      return {
        id: rawIdentityKey(objectIdentity, `${name.toLowerCase()}::${bodyIndex}`),
        name,
        index: bodyIndex,
      };
    }

    function structuredEdgeIdentity(body, edge, bodyIndex = 0, edgeIndex = 0) {
      return rawIdentityKey(
        edge?.object_identity,
        `body:${Number(body?.index || bodyIndex + 1)}:edge:${Number(edge?.index || edgeIndex + 1)}`
      );
    }

    function structuredFaceIdentity(body, face, bodyIndex = 0, faceIndex = 0) {
      return rawIdentityKey(
        face?.object_identity,
        `body:${Number(body?.index || bodyIndex + 1)}:face:${Number(face?.index || faceIndex + 1)}`
      );
    }

    function matchStructuredBody(leftBody, rightBodies, used) {
      const leftIdentity = structuredBodyIdentity(leftBody);

      let match = rightBodies.find((candidate, candidateIndex) =>
        !used.has(structuredBodyIdentity(candidate, candidateIndex).id)
        && structuredBodyIdentity(candidate, candidateIndex).id === leftIdentity.id
      );
      if (match) return match;

      match = rightBodies.find((candidate, candidateIndex) =>
        !used.has(structuredBodyIdentity(candidate, candidateIndex).id)
        && structuredBodyIdentity(candidate, candidateIndex).name === leftIdentity.name
      );
      if (match) return match;

      match = rightBodies.find((candidate, candidateIndex) =>
        !used.has(structuredBodyIdentity(candidate, candidateIndex).id)
        && Number(candidate?.index || (candidateIndex + 1)) === leftIdentity.index
      );
      if (match) return match;

      return null;
    }

    function finiteNumber(value) {
      const numeric = Number(value);
      return Number.isFinite(numeric) ? numeric : null;
    }

    function pointArray(value) {
      if (Array.isArray(value) && value.length >= 3) {
        return value.slice(0, 3).map((item) => Number(item));
      }
      if (value && typeof value === "object") {
        const x = finiteNumber(value.x);
        const y = finiteNumber(value.y);
        const z = finiteNumber(value.z);
        if (x !== null && y !== null && z !== null) return [x, y, z];
      }
      return null;
    }

    function vectorLength(vector) {
      if (!Array.isArray(vector) || vector.length < 3) return 0;
      return Math.hypot(Number(vector[0] || 0), Number(vector[1] || 0), Number(vector[2] || 0));
    }

    function normalizeVector(vector) {
      const length = vectorLength(vector);
      if (length <= 1e-12) return null;
      return vector.slice(0, 3).map((value) => Number(value || 0) / length);
    }

    function distanceBetweenPoints(leftPoint, rightPoint) {
      if (!Array.isArray(leftPoint) || !Array.isArray(rightPoint)) return Infinity;
      return Math.hypot(
        Number(rightPoint[0] || 0) - Number(leftPoint[0] || 0),
        Number(rightPoint[1] || 0) - Number(leftPoint[1] || 0),
        Number(rightPoint[2] || 0) - Number(leftPoint[2] || 0)
      );
    }

    function midpointBetweenPoints(leftPoint, rightPoint) {
      if (!Array.isArray(leftPoint) || !Array.isArray(rightPoint)) return null;
      return [
        (Number(leftPoint[0] || 0) + Number(rightPoint[0] || 0)) / 2,
        (Number(leftPoint[1] || 0) + Number(rightPoint[1] || 0)) / 2,
        (Number(leftPoint[2] || 0) + Number(rightPoint[2] || 0)) / 2,
      ];
    }

    function vectorBetweenPoints(leftPoint, rightPoint) {
      if (!Array.isArray(leftPoint) || !Array.isArray(rightPoint)) return null;
      return [
        Number(rightPoint[0] || 0) - Number(leftPoint[0] || 0),
        Number(rightPoint[1] || 0) - Number(leftPoint[1] || 0),
        Number(rightPoint[2] || 0) - Number(leftPoint[2] || 0),
      ];
    }

    function scaleVector(vector, scale = 1) {
      if (!Array.isArray(vector) || vector.length < 3) return null;
      return [
        Number(vector[0] || 0) * scale,
        Number(vector[1] || 0) * scale,
        Number(vector[2] || 0) * scale,
      ];
    }

    function addVectors(leftVector, rightVector) {
      if (!Array.isArray(leftVector) || !Array.isArray(rightVector)) return null;
      return [
        Number(leftVector[0] || 0) + Number(rightVector[0] || 0),
        Number(leftVector[1] || 0) + Number(rightVector[1] || 0),
        Number(leftVector[2] || 0) + Number(rightVector[2] || 0),
      ];
    }

    function structuredFaceKind(face) {
      return String(
        face?.type
        || face?.surface_definition?.type
        || face?.analytic_data?.type
        || "Unknown"
      );
    }

    function structuredFaceRadius(face, lengthScale = 1) {
      const radius =
        finiteNumber(face?.measurement?.radius_or_diameter)
        ?? finiteNumber(face?.analytic_data?.radius)
        ?? finiteNumber(face?.surface_definition?.radius)
        ?? finiteNumber(face?.blend_radius);
      return radius === null ? null : radius * lengthScale;
    }

    function structuredFaceCenter(face, lengthScale = 1) {
      return scalePoint(
        face?.measurement?.center
        || face?.analytic_data?.reference_point
        || face?.surface_definition?.reference_point
        || face?.surface_definition?.axis_point
        || null,
        lengthScale
      );
    }

    function structuredFaceDirection(face) {
      return normalizeVector(
        pointArray(face?.surface_definition?.direction)
        || pointArray(face?.surface_definition?.axis_direction)
        || pointArray(face?.analytic_data?.direction)
        || null
      );
    }

    function structuredFaceEntry(body, face, bodyIndex = 0, faceIndex = 0, lengthScale = 1) {
      return {
        key: structuredFaceIdentity(body, face, bodyIndex, faceIndex),
        faceIndex: Number(face?.index || (faceIndex + 1)),
        kind: structuredFaceKind(face),
        radius: structuredFaceRadius(face, lengthScale),
        center: structuredFaceCenter(face, lengthScale),
        direction: structuredFaceDirection(face),
        edgeCount: Number(face?.edge_count || 0),
        loopCount: Number((face?.loops || []).length || 0),
        bodyLabel: structuredBodyName(body, bodyIndex),
      };
    }

    function structuredFaceMatchCost(leftEntry, rightEntry) {
      if (!leftEntry || !rightEntry) return Infinity;
      let cost = 0;
      if (leftEntry.kind !== rightEntry.kind) cost += 5;
      if (leftEntry.radius !== null && rightEntry.radius !== null) {
        cost += Math.abs(rightEntry.radius - leftEntry.radius) / Math.max(leftEntry.radius, rightEntry.radius, 1e-9);
      } else if (leftEntry.radius !== rightEntry.radius) {
        cost += 1.5;
      }
      if (leftEntry.center && rightEntry.center) {
        cost += distanceBetweenPoints(leftEntry.center, rightEntry.center) / Math.max(leftEntry.radius || rightEntry.radius || 0.01, 1e-6);
      }
      if (leftEntry.direction && rightEntry.direction) {
        const dot = Math.max(-1, Math.min(1,
          (leftEntry.direction[0] * rightEntry.direction[0])
          + (leftEntry.direction[1] * rightEntry.direction[1])
          + (leftEntry.direction[2] * rightEntry.direction[2])
        ));
        cost += (1 - Math.abs(dot)) * 0.8;
      }
      cost += Math.abs((leftEntry.edgeCount || 0) - (rightEntry.edgeCount || 0)) * 0.15;
      cost += Math.abs((leftEntry.loopCount || 0) - (rightEntry.loopCount || 0)) * 0.25;
      return cost;
    }

    function matchStructuredFace(leftEntry, rightEntries, usedKeys) {
      let match = rightEntries.find((candidate) =>
        !usedKeys.has(candidate.key)
        && candidate.key === leftEntry.key
      );
      if (match) return match;

      match = rightEntries.find((candidate) =>
        !usedKeys.has(candidate.key)
        && candidate.faceIndex === leftEntry.faceIndex
      );
      if (match) return match;

      const candidates = rightEntries
        .filter((candidate) => !usedKeys.has(candidate.key))
        .map((candidate) => ({ candidate, cost: structuredFaceMatchCost(leftEntry, candidate) }))
        .sort((a, b) => a.cost - b.cost);
      if (!candidates.length) return null;
      return candidates[0].cost <= 2.5 ? candidates[0].candidate : null;
    }

    function structuredEdgeKind(edge) {
      return String(
        edge?.type
        || edge?.curve_measurement?.type
        || edge?.exact_geometry?.analytic_curve?.type
        || "Unknown"
      );
    }

    function structuredEdgeLength(edge, lengthScale = 1) {
      const length = finiteNumber(edge?.length) ?? finiteNumber(edge?.curve_measurement?.length);
      return length === null ? null : length * lengthScale;
    }

    function structuredEdgeRadius(edge, lengthScale = 1) {
      const radius =
        finiteNumber(edge?.curve_measurement?.radius)
        ?? finiteNumber(edge?.exact_geometry?.analytic_curve?.radius);
      return radius === null ? null : radius * lengthScale;
    }

    function structuredEdgeEndpoints(edge, lengthScale = 1) {
      const analyticCurve = edge?.exact_geometry?.analytic_curve || {};
      const start = scalePoint(analyticCurve?.start_point || edge?.start_point || null, lengthScale);
      const end = scalePoint(analyticCurve?.end_point || edge?.end_point || null, lengthScale);
      return start && end ? [start, end] : null;
    }

    function structuredEdgeCenter(edge, lengthScale = 1) {
      const analyticCurve = edge?.exact_geometry?.analytic_curve || {};
      return scalePoint(
        edge?.curve_measurement?.center
        || analyticCurve?.center
        || midpointBetweenPoints(
          pointArray(analyticCurve?.start_point || edge?.start_point || null),
          pointArray(analyticCurve?.end_point || edge?.end_point || null)
        )
        || (Array.isArray(edge?.preview_points) && edge.preview_points.length
          ? edge.preview_points[Math.floor(edge.preview_points.length / 2)]
          : null),
        lengthScale
      );
    }

    function structuredEdgeDirection(edge) {
      const endpoints = structuredEdgeEndpoints(edge, 1);
      if (!endpoints) return null;
      return normalizeVector([
        Number(endpoints[1][0] || 0) - Number(endpoints[0][0] || 0),
        Number(endpoints[1][1] || 0) - Number(endpoints[0][1] || 0),
        Number(endpoints[1][2] || 0) - Number(endpoints[0][2] || 0),
      ]);
    }

    function structuredEdgeEntry(body, edge, bodyIndex = 0, edgeIndex = 0, lengthScale = 1) {
      return {
        key: structuredEdgeIdentity(body, edge, bodyIndex, edgeIndex),
        edgeIndex: Number(edge?.index || (edgeIndex + 1)),
        kind: structuredEdgeKind(edge),
        length: structuredEdgeLength(edge, lengthScale),
        radius: structuredEdgeRadius(edge, lengthScale),
        center: structuredEdgeCenter(edge, lengthScale),
        direction: structuredEdgeDirection(edge),
        endpoints: structuredEdgeEndpoints(edge, lengthScale),
        bodyLabel: structuredBodyName(body, bodyIndex),
      };
    }

    function structuredEdgeEndpointCost(leftEndpoints, rightEndpoints) {
      if (!Array.isArray(leftEndpoints) || !Array.isArray(rightEndpoints)) return Infinity;
      const direct =
        distanceBetweenPoints(leftEndpoints[0], rightEndpoints[0])
        + distanceBetweenPoints(leftEndpoints[1], rightEndpoints[1]);
      const reversed =
        distanceBetweenPoints(leftEndpoints[0], rightEndpoints[1])
        + distanceBetweenPoints(leftEndpoints[1], rightEndpoints[0]);
      return Math.min(direct, reversed);
    }

    function structuredEdgeMatchCost(leftEntry, rightEntry) {
      if (!leftEntry || !rightEntry) return Infinity;
      let cost = 0;
      if (leftEntry.kind !== rightEntry.kind) cost += 4;
      if (leftEntry.length !== null && rightEntry.length !== null) {
        cost += Math.abs(rightEntry.length - leftEntry.length) / Math.max(leftEntry.length, rightEntry.length, 1e-9);
      } else if (leftEntry.length !== rightEntry.length) {
        cost += 1.5;
      }
      if (leftEntry.radius !== null && rightEntry.radius !== null) {
        cost += Math.abs(rightEntry.radius - leftEntry.radius) / Math.max(leftEntry.radius, rightEntry.radius, 1e-9);
      } else if (leftEntry.radius !== rightEntry.radius) {
        cost += 0.75;
      }
      if (leftEntry.center && rightEntry.center) {
        cost += distanceBetweenPoints(leftEntry.center, rightEntry.center)
          / Math.max(leftEntry.length || rightEntry.length || leftEntry.radius || rightEntry.radius || 0.01, 1e-6);
      }
      if (leftEntry.direction && rightEntry.direction) {
        const dot = Math.max(-1, Math.min(1,
          (leftEntry.direction[0] * rightEntry.direction[0])
          + (leftEntry.direction[1] * rightEntry.direction[1])
          + (leftEntry.direction[2] * rightEntry.direction[2])
        ));
        cost += (1 - Math.abs(dot)) * 0.5;
      }
      if (leftEntry.endpoints && rightEntry.endpoints) {
        cost += structuredEdgeEndpointCost(leftEntry.endpoints, rightEntry.endpoints)
          / Math.max(leftEntry.length || rightEntry.length || leftEntry.radius || rightEntry.radius || 0.01, 1e-6);
      }
      return cost;
    }

    function matchStructuredEdge(leftEntry, rightEntries, usedKeys) {
      let match = rightEntries.find((candidate) =>
        !usedKeys.has(candidate.key)
        && candidate.key === leftEntry.key
      );
      if (match) return match;

      match = rightEntries.find((candidate) =>
        !usedKeys.has(candidate.key)
        && candidate.edgeIndex === leftEntry.edgeIndex
      );
      if (match) return match;

      const candidates = rightEntries
        .filter((candidate) => !usedKeys.has(candidate.key))
        .map((candidate) => ({ candidate, cost: structuredEdgeMatchCost(leftEntry, candidate) }))
        .sort((a, b) => a.cost - b.cost);
      if (!candidates.length) return null;
      return candidates[0].cost <= 2.25 ? candidates[0].candidate : null;
    }

    function dedupeList(values) {
      return [...new Set((values || []).filter(Boolean))];
    }

    function formatArea(value) {
      return `${(Number(value || 0) * 1000000).toFixed(3)} mm²`;
    }

    function formatVolume(value) {
      return `${(Number(value || 0) * 1000000000).toFixed(3)} mm³`;
    }

    function metricValueList(values) {
      return values
        .map((value) => finiteNumber(value))
        .filter((value) => value !== null)
        .sort((a, b) => a - b);
    }

    function compareNumericLists(leftValues, rightValues, tolerance = DIMENSION_TOLERANCE, maxChanges = 3) {
      const left = metricValueList(leftValues);
      const right = metricValueList(rightValues);
      const changes = [];
      let leftIndex = 0;
      let rightIndex = 0;

      while (leftIndex < left.length || rightIndex < right.length) {
        const leftValue = leftIndex < left.length ? left[leftIndex] : null;
        const rightValue = rightIndex < right.length ? right[rightIndex] : null;

        if (leftValue !== null && rightValue !== null && Math.abs(rightValue - leftValue) <= tolerance) {
          leftIndex += 1;
          rightIndex += 1;
          continue;
        }

        if (leftValue !== null && rightValue !== null) {
          changes.push({ kind: "changed", leftValue, rightValue, delta: rightValue - leftValue });
          leftIndex += 1;
          rightIndex += 1;
        } else if (leftValue !== null) {
          changes.push({ kind: "removed", leftValue });
          leftIndex += 1;
        } else if (rightValue !== null) {
          changes.push({ kind: "added", rightValue });
          rightIndex += 1;
        }

        if (changes.length >= maxChanges) break;
      }

      return {
        changes,
        leftCount: left.length,
        rightCount: right.length,
        truncated: leftIndex < left.length || rightIndex < right.length,
      };
    }

    function compareValueEntries(leftEntries, rightEntries, tolerance = DIMENSION_TOLERANCE, maxChanges = 3) {
      const left = [...(leftEntries || [])]
        .filter((entry) => entry && finiteNumber(entry.value) !== null)
        .sort((a, b) => a.value - b.value);
      const right = [...(rightEntries || [])]
        .filter((entry) => entry && finiteNumber(entry.value) !== null)
        .sort((a, b) => a.value - b.value);

      const changes = [];
      let leftIndex = 0;
      let rightIndex = 0;

      while (leftIndex < left.length || rightIndex < right.length) {
        const leftEntry = leftIndex < left.length ? left[leftIndex] : null;
        const rightEntry = rightIndex < right.length ? right[rightIndex] : null;

        if (leftEntry && rightEntry && Math.abs(rightEntry.value - leftEntry.value) <= tolerance) {
          leftIndex += 1;
          rightIndex += 1;
          continue;
        }

        if (leftEntry && rightEntry) {
          changes.push({ kind: "changed", left: leftEntry, right: rightEntry, delta: rightEntry.value - leftEntry.value });
          leftIndex += 1;
          rightIndex += 1;
        } else if (leftEntry) {
          changes.push({ kind: "removed", left: leftEntry });
          leftIndex += 1;
        } else if (rightEntry) {
          changes.push({ kind: "added", right: rightEntry });
          rightIndex += 1;
        }

        if (changes.length >= maxChanges) break;
      }

      return { changes, truncated: leftIndex < left.length || rightIndex < right.length };
    }

    function circularEdgeDiameters(body, lengthScale = 1) {
      return (body?.edges || [])
        .filter((edge) => edge?.type === "Circular")
        .map((edge) =>
          finiteNumber(edge?.curve_measurement?.diameter)
          ?? (
            finiteNumber(edge?.curve_measurement?.radius) !== null
              ? finiteNumber(edge?.curve_measurement?.radius) * 2
              : null
          )
          ?? (
            finiteNumber(edge?.exact_geometry?.analytic_curve?.radius) !== null
              ? finiteNumber(edge?.exact_geometry?.analytic_curve?.radius) * 2
              : null
            )
        )
        .map((value) => (value === null ? null : value * lengthScale))
        .filter((value) => value !== null);
    }

    function circularEdgeEntries(body, bodyIndex = 0, lengthScale = 1) {
      return (body?.edges || [])
        .filter((edge) => edge?.type === "Circular")
        .map((edge, edgeIndex) => {
          const analyticCurve = edge?.exact_geometry?.analytic_curve || {};
          const diameter =
            finiteNumber(edge?.curve_measurement?.diameter)
            ?? (
              finiteNumber(edge?.curve_measurement?.radius) !== null
                ? finiteNumber(edge?.curve_measurement?.radius) * 2
                : null
            )
            ?? (
              finiteNumber(edge?.exact_geometry?.analytic_curve?.radius) !== null
                ? finiteNumber(edge?.exact_geometry?.analytic_curve?.radius) * 2
                : null
            );
          if (diameter === null) return null;
          const center = scalePoint(
            edge?.curve_measurement?.center
            || analyticCurve?.center
            || edge?.start_point
            || (Array.isArray(edge?.preview_points) && edge.preview_points.length ? edge.preview_points[0] : null),
            lengthScale
          );
          const radius =
            finiteNumber(edge?.curve_measurement?.radius)
            ?? finiteNumber(analyticCurve?.radius);
          const axisDirection = normalizeVector(
            pointArray(analyticCurve?.matrix?.x_axis)
            || vectorBetweenPoints(
              pointArray(analyticCurve?.center || edge?.curve_measurement?.center || null),
              pointArray(edge?.start_point || analyticCurve?.start_point || null)
            )
            || null
          );
          const radiusVector = radius !== null && axisDirection
            ? scaleVector(axisDirection, radius * lengthScale)
            : null;
          return {
            key: structuredEdgeIdentity(body, edge, bodyIndex, edgeIndex),
            value: diameter * lengthScale,
            center,
            start: center && radiusVector ? addVectors(center, scaleVector(radiusVector, -1)) : null,
            end: center && radiusVector ? addVectors(center, radiusVector) : null,
            label: structuredBodyName(body, bodyIndex),
          };
        })
        .filter(Boolean);
    }

    function faceRadiusValues(body, lengthScale = 1) {
      return (body?.faces || [])
        .map((face) =>
          finiteNumber(face?.measurement?.radius_or_diameter)
          ?? finiteNumber(face?.analytic_data?.radius)
          ?? finiteNumber(face?.surface_definition?.radius)
          ?? finiteNumber(face?.blend_radius)
        )
        .map((value) => (value === null ? null : value * lengthScale))
        .filter((value) => value !== null && value > 0);
    }

    function faceRadiusEntries(body, bodyIndex = 0, lengthScale = 1) {
      return (body?.faces || [])
        .map((face, faceIndex) => {
          const surfaceDefinition = face?.surface_definition || {};
          const radius =
            finiteNumber(face?.measurement?.radius_or_diameter)
            ?? finiteNumber(face?.analytic_data?.radius)
            ?? finiteNumber(surfaceDefinition?.radius)
            ?? finiteNumber(face?.blend_radius);
          if (radius === null || radius <= 0) return null;
          return {
            key: structuredFaceIdentity(body, face, bodyIndex, faceIndex),
            value: radius * lengthScale,
            center: scalePoint(
              face?.measurement?.center
              || face?.analytic_data?.reference_point
              || surfaceDefinition?.reference_point
              || surfaceDefinition?.axis_point
              || null,
              lengthScale
            ),
            label: structuredBodyName(body, bodyIndex),
          };
        })
        .filter(Boolean);
    }

    function summarizeBreakdownDiff(kind, leftBreakdown, rightBreakdown) {
      const keys = [...new Set([
        ...Object.keys(leftBreakdown || {}),
        ...Object.keys(rightBreakdown || {}),
      ])];
      const changes = keys
        .map((key) => ({
          key,
          leftValue: Number(leftBreakdown?.[key] || 0),
          rightValue: Number(rightBreakdown?.[key] || 0),
        }))
        .filter((entry) => entry.leftValue !== entry.rightValue)
        .slice(0, 3);

      if (!changes.length) return null;
      const parts = changes.map((entry) => `${entry.key} ${entry.leftValue} -> ${entry.rightValue}`);
      return `${kind} changed (${parts.join(", ")}${keys.length > changes.length ? ", ..." : ""}).`;
    }

    function summarizeMetricSetDiff(bodyLabel, metricLabel, leftValues, rightValues, formatter = formatLength) {
      const diff = compareNumericLists(leftValues, rightValues);
      if (!diff.changes.length) return null;

      const parts = diff.changes.map((change) => {
        if (change.kind === "changed") {
          return `${formatter(change.leftValue)} -> ${formatter(change.rightValue)}`;
        }
        if (change.kind === "removed") {
          return `removed ${formatter(change.leftValue)}`;
        }
        return `added ${formatter(change.rightValue)}`;
      });

      return `${bodyLabel}: ${metricLabel} changed (${parts.join(", ")}${diff.truncated ? ", ..." : ""}).`;
    }

    function summarizeScalarChange(bodyLabel, metricLabel, leftValue, rightValue, formatter) {
      const leftNumeric = finiteNumber(leftValue);
      const rightNumeric = finiteNumber(rightValue);
      if (leftNumeric === null || rightNumeric === null) return null;
      if (Math.abs(rightNumeric - leftNumeric) <= DIMENSION_TOLERANCE) return null;
      return `${bodyLabel}: ${metricLabel} changed from ${formatter(leftNumeric)} to ${formatter(rightNumeric)}.`;
    }

    function compareStructuredReportData(left, right) {
      const leftBodies = structuredBodies(left);
      const rightBodies = structuredBodies(right);
      if (!leftBodies.length || !rightBodies.length) {
        return {
          summary: [],
          highlightBodyKeys: { left: [], right: [] },
          highlightRawFaces: { left: [], right: [] },
          highlightRawEdges: { left: [], right: [] },
          exactHighlightBodyKeys: { left: [], right: [] },
          exactHighlightRawFaces: { left: [], right: [] },
          exactHighlightRawEdges: { left: [], right: [] },
          annotations: { left: [], right: [] },
          exactAnnotations: { left: [], right: [] },
        };
      }

      const summary = [];
      const highlightBodyKeys = { left: [], right: [] };
      const highlightRawFaces = { left: [], right: [] };
      const highlightRawEdges = { left: [], right: [] };
      const exactHighlightBodyKeys = { left: [], right: [] };
      const exactHighlightRawFaces = { left: [], right: [] };
      const exactHighlightRawEdges = { left: [], right: [] };
      const annotations = { left: [], right: [] };
      const exactAnnotations = { left: [], right: [] };
      const usedRightBodyIds = new Set();
      const leftLengthScale = reportLengthScale(left);
      const rightLengthScale = reportLengthScale(right);
      const leftAreaScale = leftLengthScale * leftLengthScale;
      const rightAreaScale = rightLengthScale * rightLengthScale;
      const leftVolumeScale = leftAreaScale * leftLengthScale;
      const rightVolumeScale = rightAreaScale * rightLengthScale;

      const leftCounts = structuredTopologyCounts(left);
      const rightCounts = structuredTopologyCounts(right);
      if (
        leftCounts.bodyCount !== rightCounts.bodyCount
        || leftCounts.faceCount !== rightCounts.faceCount
        || leftCounts.edgeCount !== rightCounts.edgeCount
      ) {
        summary.push(
          `Source topology changed from ${leftCounts.faceCount} faces / ${leftCounts.edgeCount} edges across ${leftCounts.bodyCount} bodies to ${rightCounts.faceCount} faces / ${rightCounts.edgeCount} edges across ${rightCounts.bodyCount} bodies.`
        );
      }

      leftBodies.forEach((leftBody, leftIndex) => {
        const rightBody = matchStructuredBody(leftBody, rightBodies, usedRightBodyIds);
        if (!rightBody) {
          summary.push(`Removed source body: ${structuredBodyName(leftBody, leftIndex)}.`);
          highlightBodyKeys.left.push(previewBodyRawKey(leftBody, leftIndex));
          return;
        }

        const rightIndex = rightBodies.indexOf(rightBody);
        const rightIdentity = structuredBodyIdentity(rightBody, rightIndex);
        const leftPreviewBodyKey = previewBodyRawKey(leftBody, leftIndex);
        const rightPreviewBodyKey = previewBodyRawKey(rightBody, rightIndex);
        usedRightBodyIds.add(rightIdentity.id);

        const bodyLabel = structuredBodyName(leftBody, leftIndex);
        const leftFaceCount = leftBody?.faces?.length || 0;
        const rightFaceCount = rightBody?.faces?.length || 0;
        const leftEdgeCount = leftBody?.edges?.length || 0;
        const rightEdgeCount = rightBody?.edges?.length || 0;
        if (leftFaceCount !== rightFaceCount || leftEdgeCount !== rightEdgeCount) {
          summary.push(
            `${bodyLabel}: source topology changed from ${leftFaceCount} faces / ${leftEdgeCount} edges to ${rightFaceCount} faces / ${rightEdgeCount} edges.`
          );
        }

        const faceBreakdownSummary = summarizeBreakdownDiff(
          "face types",
          leftBody?.face_type_breakdown || {},
          rightBody?.face_type_breakdown || {}
        );
        if (faceBreakdownSummary) {
          summary.push(`${bodyLabel}: ${faceBreakdownSummary}`);
        }

        const edgeBreakdownSummary = summarizeBreakdownDiff(
          "edge types",
          leftBody?.edge_type_breakdown || {},
          rightBody?.edge_type_breakdown || {}
        );
        if (edgeBreakdownSummary) {
          summary.push(`${bodyLabel}: ${edgeBreakdownSummary}`);
        }

        const leftFaceEntries = (leftBody?.faces || []).map((face, faceIndex) =>
          structuredFaceEntry(leftBody, face, leftIndex, faceIndex, leftLengthScale)
        );
        const rightFaceEntries = (rightBody?.faces || []).map((face, faceIndex) =>
          structuredFaceEntry(rightBody, face, rightIndex, faceIndex, rightLengthScale)
        );
        const matchedLeftFaceKeys = new Set();
        const usedRightFaceKeys = new Set();
        let structuredChangedFaceCount = 0;
        leftFaceEntries.forEach((leftFaceEntry) => {
          const rightFaceEntry = matchStructuredFace(leftFaceEntry, rightFaceEntries, usedRightFaceKeys);
          if (!rightFaceEntry) return;
          matchedLeftFaceKeys.add(leftFaceEntry.key);
          usedRightFaceKeys.add(rightFaceEntry.key);

          const kindChanged = leftFaceEntry.kind !== rightFaceEntry.kind;
          const radiusChanged = leftFaceEntry.radius !== null && rightFaceEntry.radius !== null
            && Math.abs(rightFaceEntry.radius - leftFaceEntry.radius) > DIMENSION_TOLERANCE;
          const centerShift = leftFaceEntry.center && rightFaceEntry.center
            && distanceBetweenPoints(leftFaceEntry.center, rightFaceEntry.center) > DIMENSION_TOLERANCE * 2;
          const loopChanged = leftFaceEntry.loopCount !== rightFaceEntry.loopCount;
          const edgeChanged = leftFaceEntry.edgeCount !== rightFaceEntry.edgeCount;
          const directionChanged = leftFaceEntry.direction && rightFaceEntry.direction
            && Math.abs(
              1 - Math.abs(
                (leftFaceEntry.direction[0] * rightFaceEntry.direction[0])
                + (leftFaceEntry.direction[1] * rightFaceEntry.direction[1])
                + (leftFaceEntry.direction[2] * rightFaceEntry.direction[2])
              )
            ) > 0.08;
          if (!(kindChanged || radiusChanged || centerShift || loopChanged || edgeChanged || directionChanged)) return;

          structuredChangedFaceCount += 1;
          exactHighlightRawFaces.left.push(leftFaceEntry.key);
          exactHighlightRawFaces.right.push(rightFaceEntry.key);
          highlightRawFaces.left.push(leftFaceEntry.key);
          highlightRawFaces.right.push(rightFaceEntry.key);

          const faceLabel = `${bodyLabel} face ${leftFaceEntry.faceIndex} (${leftFaceEntry.kind.toLowerCase()})`;
          if (leftFaceEntry.center) {
            exactAnnotations.left.push({
              point: leftFaceEntry.center,
              tone: "exact",
              lines: [
                radiusChanged
                  ? `${faceLabel}: ${formatInches(leftFaceEntry.radius)} -> ${formatInches(rightFaceEntry.radius)}`
                  : `${faceLabel} changed`
              ],
            });
          }
          if (rightFaceEntry.center) {
            exactAnnotations.right.push({
              point: rightFaceEntry.center,
              tone: "exact",
              lines: [
                radiusChanged
                  ? `${faceLabel}: ${formatInches(rightFaceEntry.radius)} (${formatSignedInches(rightFaceEntry.radius - leftFaceEntry.radius)})`
                  : `${faceLabel} changed`
              ],
            });
          }
        });
        if (structuredChangedFaceCount) {
          summary.push(`${bodyLabel}: ${structuredChangedFaceCount} source face region(s) changed.`);
        }

        const unmatchedLeftFaceEntries = leftFaceEntries.filter((entry) => !matchedLeftFaceKeys.has(entry.key));
        const unmatchedRightFaceEntries = rightFaceEntries.filter((entry) => !usedRightFaceKeys.has(entry.key));
        unmatchedLeftFaceEntries.slice(0, 6).forEach((entry) => {
          highlightRawFaces.left.push(entry.key);
          exactHighlightRawFaces.left.push(entry.key);
          if (entry.center) {
            exactAnnotations.left.push({
              point: entry.center,
              tone: "exact",
              lines: [`${bodyLabel} face ${entry.faceIndex} (${entry.kind.toLowerCase()}) removed`],
            });
          }
        });
        unmatchedRightFaceEntries.slice(0, 6).forEach((entry) => {
          highlightRawFaces.right.push(entry.key);
          exactHighlightRawFaces.right.push(entry.key);
          if (entry.center) {
            exactAnnotations.right.push({
              point: entry.center,
              tone: "exact",
              lines: [`${bodyLabel} face ${entry.faceIndex} (${entry.kind.toLowerCase()}) added`],
            });
          }
        });
        if (unmatchedLeftFaceEntries.length || unmatchedRightFaceEntries.length) {
          summary.push(
            `${bodyLabel}: source faces changed (${unmatchedLeftFaceEntries.length} removed, ${unmatchedRightFaceEntries.length} added).`
          );
        }

        const leftEdgeEntries = (leftBody?.edges || []).map((edge, edgeIndex) =>
          structuredEdgeEntry(leftBody, edge, leftIndex, edgeIndex, leftLengthScale)
        );
        const rightEdgeEntries = (rightBody?.edges || []).map((edge, edgeIndex) =>
          structuredEdgeEntry(rightBody, edge, rightIndex, edgeIndex, rightLengthScale)
        );
        const matchedLeftEdgeKeys = new Set();
        const usedRightEdgeKeys = new Set();
        let structuredChangedEdgeCount = 0;
        leftEdgeEntries.forEach((leftEdgeEntry) => {
          const rightEdgeEntry = matchStructuredEdge(leftEdgeEntry, rightEdgeEntries, usedRightEdgeKeys);
          if (!rightEdgeEntry) return;
          matchedLeftEdgeKeys.add(leftEdgeEntry.key);
          usedRightEdgeKeys.add(rightEdgeEntry.key);

          const kindChanged = leftEdgeEntry.kind !== rightEdgeEntry.kind;
          const lengthChanged = leftEdgeEntry.length !== null && rightEdgeEntry.length !== null
            && Math.abs(rightEdgeEntry.length - leftEdgeEntry.length) > DIMENSION_TOLERANCE;
          const radiusChanged = leftEdgeEntry.radius !== null && rightEdgeEntry.radius !== null
            && Math.abs(rightEdgeEntry.radius - leftEdgeEntry.radius) > DIMENSION_TOLERANCE;
          const centerShift = leftEdgeEntry.center && rightEdgeEntry.center
            && distanceBetweenPoints(leftEdgeEntry.center, rightEdgeEntry.center) > DIMENSION_TOLERANCE * 2;
          const endpointShift = leftEdgeEntry.endpoints && rightEdgeEntry.endpoints
            && structuredEdgeEndpointCost(leftEdgeEntry.endpoints, rightEdgeEntry.endpoints) > DIMENSION_TOLERANCE * 4;
          if (!(kindChanged || lengthChanged || radiusChanged || centerShift || endpointShift)) return;

          structuredChangedEdgeCount += 1;
          exactHighlightRawEdges.left.push(leftEdgeEntry.key);
          exactHighlightRawEdges.right.push(rightEdgeEntry.key);
          highlightRawEdges.left.push(leftEdgeEntry.key);
          highlightRawEdges.right.push(rightEdgeEntry.key);

          const edgeLabel = `${bodyLabel} edge ${leftEdgeEntry.edgeIndex} (${leftEdgeEntry.kind.toLowerCase()})`;
          const leftLines = [];
          const rightLines = [];
          if (radiusChanged && !["Circular", "Arc"].includes(leftEdgeEntry.kind)) {
            leftLines.push(`${edgeLabel}: radius ${formatInches(leftEdgeEntry.radius)} -> ${formatInches(rightEdgeEntry.radius)}`);
            rightLines.push(`${edgeLabel}: radius ${formatInches(rightEdgeEntry.radius)} (${formatSignedInches(rightEdgeEntry.radius - leftEdgeEntry.radius)})`);
          } else if (lengthChanged && !["Circular", "Arc"].includes(leftEdgeEntry.kind)) {
            leftLines.push(`${edgeLabel}: length ${formatInches(leftEdgeEntry.length)} -> ${formatInches(rightEdgeEntry.length)}`);
            rightLines.push(`${edgeLabel}: length ${formatInches(rightEdgeEntry.length)} (${formatSignedInches(rightEdgeEntry.length - leftEdgeEntry.length)})`);
          } else if (!["Circular", "Arc"].includes(leftEdgeEntry.kind)) {
            leftLines.push(`${edgeLabel} changed`);
            rightLines.push(`${edgeLabel} changed`);
          }
          if (leftLines.length && leftEdgeEntry.center) {
            exactAnnotations.left.push({
              point: leftEdgeEntry.center,
              tone: "exact",
              lines: leftLines,
            });
          }
          if (rightLines.length && rightEdgeEntry.center) {
            exactAnnotations.right.push({
              point: rightEdgeEntry.center,
              tone: "exact",
              lines: rightLines,
            });
          }
        });
        if (structuredChangedEdgeCount) {
          summary.push(`${bodyLabel}: ${structuredChangedEdgeCount} source edge region(s) changed.`);
        }

        const unmatchedLeftEdgeEntries = leftEdgeEntries.filter((entry) => !matchedLeftEdgeKeys.has(entry.key));
        const unmatchedRightEdgeEntries = rightEdgeEntries.filter((entry) => !usedRightEdgeKeys.has(entry.key));
        unmatchedLeftEdgeEntries.slice(0, 6).forEach((entry) => {
          highlightRawEdges.left.push(entry.key);
          exactHighlightRawEdges.left.push(entry.key);
          if (entry.center) {
            exactAnnotations.left.push({
              point: entry.center,
              tone: "exact",
              lines: [`${bodyLabel} edge ${entry.edgeIndex} (${entry.kind.toLowerCase()}) removed`],
            });
          }
        });
        unmatchedRightEdgeEntries.slice(0, 6).forEach((entry) => {
          highlightRawEdges.right.push(entry.key);
          exactHighlightRawEdges.right.push(entry.key);
          if (entry.center) {
            exactAnnotations.right.push({
              point: entry.center,
              tone: "exact",
              lines: [`${bodyLabel} edge ${entry.edgeIndex} (${entry.kind.toLowerCase()}) added`],
            });
          }
        });
        if (unmatchedLeftEdgeEntries.length || unmatchedRightEdgeEntries.length) {
          summary.push(
            `${bodyLabel}: source edges changed (${unmatchedLeftEdgeEntries.length} removed, ${unmatchedRightEdgeEntries.length} added).`
          );
        }

        const circularDiameterSummary = summarizeMetricSetDiff(
          bodyLabel,
          "circular diameters",
          circularEdgeDiameters(leftBody, leftLengthScale),
          circularEdgeDiameters(rightBody, rightLengthScale),
          formatLength
        );
        if (circularDiameterSummary) summary.push(circularDiameterSummary);

        const circularEntryDiff = compareValueEntries(
          circularEdgeEntries(leftBody, leftIndex, leftLengthScale),
          circularEdgeEntries(rightBody, rightIndex, rightLengthScale)
        );
        circularEntryDiff.changes.forEach((change) => {
          if (change.left?.key) exactHighlightRawEdges.left.push(change.left.key);
          if (change.right?.key) exactHighlightRawEdges.right.push(change.right.key);
          const circularLabel = `${bodyLabel} circular diameter`;
          if (change.left?.start && change.left?.end) {
            exactAnnotations.left.push({
              start: change.left.start,
              end: change.left.end,
              tone: "exact",
              lines: [
                change.kind === "changed"
                  ? `${circularLabel}: ${formatInches(change.left.value)} -> ${formatInches(change.right.value)}`
                  : `${circularLabel} removed: ${formatInches(change.left.value)}`
              ],
            });
          } else if (change.left?.center) {
            exactAnnotations.left.push({
              point: change.left.center,
              tone: "exact",
              lines: [
                change.kind === "changed"
                  ? `${circularLabel}: ${formatInches(change.left.value)} -> ${formatInches(change.right.value)}`
                  : `${circularLabel} removed: ${formatInches(change.left.value)}`
              ],
            });
          }
          if (change.right?.start && change.right?.end) {
            exactAnnotations.right.push({
              start: change.right.start,
              end: change.right.end,
              tone: "exact",
              lines: [
                change.kind === "changed"
                  ? `${circularLabel}: ${formatInches(change.right.value)} (${formatSignedInches(change.delta)})`
                  : `${circularLabel} added: ${formatInches(change.right.value)}`
              ],
              focusSegments: change.kind === "changed"
                ? [{ start: change.right.start, end: change.right.end }]
                : [],
              deltaLabel: change.kind === "changed" ? formatSignedInches(change.delta) : null,
            });
          } else if (change.right?.center) {
            exactAnnotations.right.push({
              point: change.right.center,
              tone: "exact",
              lines: [
                change.kind === "changed"
                  ? `${circularLabel}: ${formatInches(change.right.value)} (${formatSignedInches(change.delta)})`
                  : `${circularLabel} added: ${formatInches(change.right.value)}`
              ],
            });
          }
        });

        const faceRadiusSummary = summarizeMetricSetDiff(
          bodyLabel,
          "face radii",
          faceRadiusValues(leftBody, leftLengthScale),
          faceRadiusValues(rightBody, rightLengthScale),
          formatLength
        );
        if (faceRadiusSummary) summary.push(faceRadiusSummary);

        const faceEntryDiff = compareValueEntries(
          faceRadiusEntries(leftBody, leftIndex, leftLengthScale),
          faceRadiusEntries(rightBody, rightIndex, rightLengthScale)
        );
        faceEntryDiff.changes.forEach((change) => {
          if (change.left?.key) exactHighlightRawFaces.left.push(change.left.key);
          if (change.right?.key) exactHighlightRawFaces.right.push(change.right.key);
          if (change.left?.key) highlightRawFaces.left.push(change.left.key);
          if (change.right?.key) highlightRawFaces.right.push(change.right.key);
          if (change.left?.center) {
            exactAnnotations.left.push({
              point: change.left.center,
              tone: "exact",
              lines: [
                change.kind === "changed"
                  ? `${bodyLabel} face radius: ${formatInches(change.left.value)} -> ${formatInches(change.right.value)}`
                  : `${bodyLabel} face radius removed: ${formatInches(change.left.value)}`
              ],
            });
          }
          if (change.right?.center) {
            exactAnnotations.right.push({
              point: change.right.center,
              tone: "exact",
              lines: [
                change.kind === "changed"
                  ? `${bodyLabel} face radius: ${formatInches(change.right.value)} (${formatSignedInches(change.delta)})`
                  : `${bodyLabel} face radius added: ${formatInches(change.right.value)}`
              ],
            });
          }
        });

        const areaSummary = summarizeScalarChange(
          bodyLabel,
          "surface area",
          finiteNumber(leftBody?.measurement?.surface_area) !== null ? Number(leftBody.measurement.surface_area) * leftAreaScale : null,
          finiteNumber(rightBody?.measurement?.surface_area) !== null ? Number(rightBody.measurement.surface_area) * rightAreaScale : null,
          formatArea
        );
        if (areaSummary) {
          summary.push(areaSummary);
        }

        const volumeSummary = summarizeScalarChange(
          bodyLabel,
          "volume",
          finiteNumber(leftBody?.measurement?.volume) !== null ? Number(leftBody.measurement.volume) * leftVolumeScale : null,
          finiteNumber(rightBody?.measurement?.volume) !== null ? Number(rightBody.measurement.volume) * rightVolumeScale : null,
          formatVolume
        );
        if (volumeSummary) {
          summary.push(volumeSummary);
        }
      });

      const unmatchedRightBodies = rightBodies
        .map((body, index) => ({ body, index }))
        .filter(({ body, index }) => !usedRightBodyIds.has(structuredBodyIdentity(body, index).id));
      unmatchedRightBodies.slice(0, 2).forEach(({ body, index }) => {
        summary.push(`Added source body: ${structuredBodyName(body, index)}.`);
        highlightBodyKeys.right.push(previewBodyRawKey(body, index));
      });

      return {
        summary: summary.slice(0, 12),
        highlightBodyKeys: {
          left: dedupeList(highlightBodyKeys.left),
          right: dedupeList(highlightBodyKeys.right),
        },
        highlightRawFaces: {
          left: dedupeList(highlightRawFaces.left),
          right: dedupeList(highlightRawFaces.right),
        },
        highlightRawEdges: {
          left: dedupeList(highlightRawEdges.left),
          right: dedupeList(highlightRawEdges.right),
        },
        exactHighlightBodyKeys: {
          left: dedupeList(exactHighlightBodyKeys.left),
          right: dedupeList(exactHighlightBodyKeys.right),
        },
        exactHighlightRawFaces: {
          left: dedupeList(exactHighlightRawFaces.left),
          right: dedupeList(exactHighlightRawFaces.right),
        },
        exactHighlightRawEdges: {
          left: dedupeList(exactHighlightRawEdges.left),
          right: dedupeList(exactHighlightRawEdges.right),
        },
        annotations: {
          left: (annotations.left || []).slice(0, 6),
          right: (annotations.right || []).slice(0, 6),
        },
        exactAnnotations: {
          left: (exactAnnotations.left || []).slice(0, 6),
          right: (exactAnnotations.right || []).slice(0, 6),
        },
      };
    }

    function compareDiffInfo(left, right) {
      const info = {
        changedAxes: [],
        changedShape: false,
        summary: [],
        dimensions: compareAxisDimensions(left, right),
        highlightComponents: { left: [], right: [] },
        highlightBodyKeys: { left: [], right: [] },
        highlightRawFaces: { left: [], right: [] },
        highlightRawEdges: { left: [], right: [] },
        annotations: { left: [], right: [] },
        exactHighlightBodyKeys: { left: [], right: [] },
        exactHighlightRawFaces: { left: [], right: [] },
        exactHighlightRawEdges: { left: [], right: [] },
        exactAnnotations: { left: [], right: [] },
        stats: {
          matchedComponents: 0,
          changedComponents: 0,
          addedComponents: 0,
          removedComponents: 0,
          matchQuality: "Unknown",
        },
      };

      if (!left || !right) {
        return info;
      }

      const structuredDiff = compareStructuredReportData(left, right);

      const leftComponents = reportComponents(left);
      const rightComponents = reportComponents(right);
      const usedRightComponents = new Set();
      const componentChanges = [];
      const removedComponents = [];
      const addedComponents = [];
      const matchQualities = [];

      for (const leftComponent of leftComponents) {
        const matched = matchRightComponent(leftComponent, rightComponents, usedRightComponents);
        if (!matched?.component) {
          removedComponents.push(leftComponent);
          continue;
        }
        const rightComponent = matched.component;
        usedRightComponents.add(rightComponent.id);
        const properties = componentPropertyChanges(leftComponent, rightComponent);
        const matchQuality = matchQualityFromCost(matched.cost);
        matchQualities.push(matchQuality.value);
        if (!properties.length) {
          componentChanges.push({
            primitive: leftComponent.primitive,
            label: leftComponent.label,
            left: leftComponent,
            right: rightComponent,
            properties: [],
            changed: false,
            matchQuality,
          });
          continue;
        }

        componentChanges.push({
          primitive: leftComponent.primitive,
          label: leftComponent.label,
          left: leftComponent,
          right: rightComponent,
          properties,
          changed: true,
          matchQuality,
        });
      }

      rightComponents
        .filter((candidate) => !usedRightComponents.has(candidate.id))
        .forEach((candidate) => addedComponents.push(candidate));

      const leftShape = topologyLabel(left);
      const rightShape = topologyLabel(right);
      info.changedShape = leftShape !== rightShape;

      const changedComponents = componentChanges
        .filter((change) => change.changed)
        .sort((a, b) => changeSeverity(b) - changeSeverity(a));
      info.stats = {
        matchedComponents: componentChanges.length,
        changedComponents: changedComponents.length,
        addedComponents: addedComponents.length,
        removedComponents: removedComponents.length,
        matchQuality: matchQualities.length
          ? matchQualityFromCost((1 - (matchQualities.reduce((sum, value) => sum + value, 0) / matchQualities.length)) * 5).label
          : "Unknown",
      };
      if (componentChanges.length || addedComponents.length || removedComponents.length) {
        info.summary.push(
          `Preview comparison matched ${componentChanges.length} component(s); ${changedComponents.length} changed, ${addedComponents.length} added, ${removedComponents.length} removed.`
        );
      }
      if (changedComponents.length) {
        info.changedAxes = [...new Set(changedComponents.flatMap((change) => change.properties.map((property) => property.axis).filter(Boolean)))];
        changedComponents.forEach((change) => {
          info.summary.push(summarizeComponentChange(change));
          info.highlightComponents.left.push(change.left.id);
          info.highlightComponents.right.push(change.right.id);
          if (change.left.bodyRawKey) info.highlightBodyKeys.left.push(change.left.bodyRawKey);
          if (change.right.bodyRawKey) info.highlightBodyKeys.right.push(change.right.bodyRawKey);
          if (change.properties.some((property) => property.kind === "shape_profile")) {
            const regionHighlights = componentRegionHighlights(change.left, change.right);
            info.highlightRawFaces.left.push(...regionHighlights.highlightRawFaces.left);
            info.highlightRawFaces.right.push(...regionHighlights.highlightRawFaces.right);
            info.highlightRawEdges.left.push(...regionHighlights.highlightRawEdges.left);
            info.highlightRawEdges.right.push(...regionHighlights.highlightRawEdges.right);
            info.annotations.left.push(...regionHighlights.annotations.left);
            info.annotations.right.push(...regionHighlights.annotations.right);
          }
          change.properties.forEach((property) => {
            const leftEndpoints = annotationEndpointsForProperty(change.left, property);
            const rightEndpoints = annotationEndpointsForProperty(change.right, property);
            const focusSegments = focusSegmentsForProperty(change.left, change.right, property);
            const exactProperty = !["shape_profile", "color", "facet_count", "edge_count"].includes(property.kind);
            if (property.kind === "shape_profile" || property.kind === "color") {
              info.annotations.left.push({
                point: change.left.center,
                lines: [property.kind === "color" ? `${change.label} color changed` : `${change.label} shape changed`],
              });
              info.annotations.right.push({
                point: change.right.center,
                lines: [property.kind === "color" ? `${change.label} color changed` : `${change.label} shape changed`],
              });
              return;
            }
            if (leftEndpoints) {
              const exactLeftAnnotation = {
                start: leftEndpoints[0],
                end: leftEndpoints[1],
                tone: exactProperty ? "exact" : "changed",
                lines: [
                  (property.kind === "facet_count" || property.kind === "edge_count")
                    ? `${change.label} ${property.label}: ${Math.round(property.leftValue)}`
                    : `${change.label} ${property.label}: ${formatInches(property.leftValue)}`
                ],
                focusSegments: focusSegments.left,
                deltaLabel: property.kind === "facet_count"
                  ? null
                  : (focusSegments.left.length ? formatSignedInches(property.delta) : null),
              };
              if (exactProperty) {
                info.exactAnnotations.left.push(exactLeftAnnotation);
              } else {
                info.annotations.left.push(exactLeftAnnotation);
              }
            }
            if (rightEndpoints) {
              const exactRightAnnotation = {
                start: rightEndpoints[0],
                end: rightEndpoints[1],
                tone: exactProperty ? "exact" : "changed",
                lines: [
                  (property.kind === "facet_count" || property.kind === "edge_count")
                    ? `${change.label} ${property.label}: ${Math.round(property.rightValue)}`
                    : `${change.label} ${property.label}: ${formatInches(property.rightValue)} (${formatSignedInches(property.delta)})`
                ],
                focusSegments: focusSegments.right,
                deltaLabel: property.kind === "facet_count"
                  ? null
                  : (focusSegments.right.length ? formatSignedInches(property.delta) : null),
              };
              if (exactProperty) {
                info.exactAnnotations.right.push(exactRightAnnotation);
              } else {
                info.annotations.right.push(exactRightAnnotation);
              }
            }
            if (exactProperty) {
              if (change.left.bodyRawKey) info.exactHighlightBodyKeys.left.push(change.left.bodyRawKey);
              if (change.right.bodyRawKey) info.exactHighlightBodyKeys.right.push(change.right.bodyRawKey);
            }
          });
        });

        const unchangedComponents = componentChanges.filter((change) => !change.changed).slice(0, 2);
        unchangedComponents.forEach((change) => {
          info.summary.push(`${change.label} stayed unchanged.`);
        });
      } else if (info.dimensions.some((item) => item.changed)) {
        info.changedAxes = info.dimensions.filter((item) => item.changed).map((item) => item.axis);
        info.summary.push(`Changed axes: ${info.changedAxes.map((axis) => axis.toUpperCase()).join(", ")}`);
        info.dimensions.filter((dimension) => dimension.changed).forEach((item) => {
          info.summary.push(
            `${item.axis.toUpperCase()} span changed from ${formatLength(item.leftValue)} to ${formatLength(item.rightValue)} (${formatSignedInches(item.delta)})`
          );
          const leftBounds = left?.geometry_hints?.bounds;
          const rightBounds = right?.geometry_hints?.bounds;
          const focusSegments = leftBounds && rightBounds
            ? focusSegmentsForProperty(
                { bounds: leftBounds },
                { bounds: rightBounds },
                { kind: "size", axis: item.axis }
              )
            : { left: [], right: [] };
          if (leftBounds) {
            const leftEndpoints = axisDimensionEndpoints(leftBounds, item.axis);
            info.annotations.left.push({
              start: leftEndpoints[0],
              end: leftEndpoints[1],
              lines: [`${item.axis.toUpperCase()} span: ${formatInches(item.leftValue)}`],
              focusSegments: focusSegments.left,
              deltaLabel: focusSegments.left.length ? formatSignedInches(item.delta) : null,
            });
          }
          if (rightBounds) {
            const rightEndpoints = axisDimensionEndpoints(rightBounds, item.axis);
            info.annotations.right.push({
              start: rightEndpoints[0],
              end: rightEndpoints[1],
              lines: [`${item.axis.toUpperCase()} span: ${formatInches(item.rightValue)} (${formatSignedInches(item.delta)})`],
              focusSegments: focusSegments.right,
              deltaLabel: focusSegments.right.length ? formatSignedInches(item.delta) : null,
            });
          }
        });
      } else if (!structuredDiff.summary.length) {
        info.summary.push("No geometric size change was inferred from the current preview data.");
      }

      if (info.changedShape) {
        info.summary.push(`Shape changed: ${leftShape || "unknown"} -> ${rightShape || "unknown"}`);
      }

      removedComponents.slice(0, 3).forEach((component) => {
        info.summary.push(`Removed preview component: ${component.label}.`);
        info.highlightComponents.left.push(component.id);
        if (component.bodyRawKey) info.highlightBodyKeys.left.push(component.bodyRawKey);
      });
      addedComponents.slice(0, 3).forEach((component) => {
        info.summary.push(`Added preview component: ${component.label}.`);
        info.highlightComponents.right.push(component.id);
        if (component.bodyRawKey) info.highlightBodyKeys.right.push(component.bodyRawKey);
      });

      if (structuredDiff.summary.length) {
        info.summary = [...structuredDiff.summary, ...info.summary];
        info.highlightBodyKeys.left.push(...structuredDiff.highlightBodyKeys.left);
        info.highlightBodyKeys.right.push(...structuredDiff.highlightBodyKeys.right);
        info.highlightRawFaces.left.push(...(structuredDiff.highlightRawFaces.left || []));
        info.highlightRawFaces.right.push(...(structuredDiff.highlightRawFaces.right || []));
        info.highlightRawEdges.left.push(...(structuredDiff.highlightRawEdges.left || []));
        info.highlightRawEdges.right.push(...(structuredDiff.highlightRawEdges.right || []));
        info.exactHighlightBodyKeys.left.push(...(structuredDiff.exactHighlightBodyKeys.left || []));
        info.exactHighlightBodyKeys.right.push(...(structuredDiff.exactHighlightBodyKeys.right || []));
        info.exactHighlightRawFaces.left.push(...(structuredDiff.exactHighlightRawFaces.left || []));
        info.exactHighlightRawFaces.right.push(...(structuredDiff.exactHighlightRawFaces.right || []));
        info.exactHighlightRawEdges.left.push(...(structuredDiff.exactHighlightRawEdges.left || []));
        info.exactHighlightRawEdges.right.push(...(structuredDiff.exactHighlightRawEdges.right || []));
        info.annotations.left.push(...structuredDiff.annotations.left);
        info.annotations.right.push(...structuredDiff.annotations.right);
        info.exactAnnotations.left.push(...structuredDiff.exactAnnotations.left);
        info.exactAnnotations.right.push(...structuredDiff.exactAnnotations.right);
      }

      const hasStructuredExactGeometry = Boolean(
        (structuredDiff.exactHighlightRawFaces.left || []).length
        || (structuredDiff.exactHighlightRawFaces.right || []).length
        || (structuredDiff.exactHighlightRawEdges.left || []).length
        || (structuredDiff.exactHighlightRawEdges.right || []).length
      );
      if (hasStructuredExactGeometry) {
        info.highlightComponents = { left: [], right: [] };
        info.highlightBodyKeys = {
          left: [...(structuredDiff.highlightBodyKeys.left || [])],
          right: [...(structuredDiff.highlightBodyKeys.right || [])],
        };
        info.exactHighlightBodyKeys = {
          left: [...(structuredDiff.exactHighlightBodyKeys.left || [])],
          right: [...(structuredDiff.exactHighlightBodyKeys.right || [])],
        };
        info.annotations = {
          left: [...(structuredDiff.annotations.left || [])],
          right: [...(structuredDiff.annotations.right || [])],
        };
      }

      info.summary = dedupeList(info.summary).slice(0, 18);
      info.highlightComponents = {
        left: dedupeList(info.highlightComponents.left),
        right: dedupeList(info.highlightComponents.right),
      };
      info.highlightBodyKeys = {
        left: dedupeList(info.highlightBodyKeys.left),
        right: dedupeList(info.highlightBodyKeys.right),
      };
      info.highlightRawFaces = {
        left: dedupeList(info.highlightRawFaces.left),
        right: dedupeList(info.highlightRawFaces.right),
      };
      info.highlightRawEdges = {
        left: dedupeList(info.highlightRawEdges.left),
        right: dedupeList(info.highlightRawEdges.right),
      };
      info.exactHighlightBodyKeys = {
        left: dedupeList(info.exactHighlightBodyKeys.left),
        right: dedupeList(info.exactHighlightBodyKeys.right),
      };
      info.exactHighlightRawFaces = {
        left: dedupeList(info.exactHighlightRawFaces.left),
        right: dedupeList(info.exactHighlightRawFaces.right),
      };
      info.exactHighlightRawEdges = {
        left: dedupeList(info.exactHighlightRawEdges.left),
        right: dedupeList(info.exactHighlightRawEdges.right),
      };
      info.annotations = {
        left: (info.annotations.left || []).slice(0, 10),
        right: (info.annotations.right || []).slice(0, 10),
      };
      info.exactAnnotations = {
        left: (info.exactAnnotations.left || []).slice(0, 10),
        right: (info.exactAnnotations.right || []).slice(0, 10),
      };

      return info;
    }

    function previewViewerDistance(preview) {
      return Math.max(4, Number(preview?.span || 0) * 2.5);
    }

    function projectModelPoint(point, preview, viewer, scale, canvasEl) {
      return projectPoint(
        rotatePoint(
          [
            point[0] - preview.center[0],
            point[1] - preview.center[1],
            point[2] - preview.center[2]
          ],
          viewer
        ),
        scale,
        canvasEl,
        viewer,
        previewViewerDistance(preview)
      );
    }

    function axisDimensionEndpoints(bounds, axis) {
      const min = bounds.min.map(Number);
      const max = bounds.max.map(Number);
      if (axis === "x") return [[min[0], max[1], max[2]], [max[0], max[1], max[2]]];
      if (axis === "y") return [[max[0], min[1], max[2]], [max[0], max[1], max[2]]];
      return [[max[0], min[1], min[2]], [max[0], min[1], max[2]]];
    }

    function inferFaceSide(face) {
      const name = String(face?.name || "").toLowerCase();
      if (name.includes("-min face") || name.includes("start cap")) return "min";
      if (name.includes("-max face") || name.includes("end cap")) return "max";
      return face?.side || null;
    }

    function normalFromVertices(vertices) {
      if (!Array.isArray(vertices) || vertices.length < 3) return null;
      const anchor = vertices[0];
      for (let index = 1; index < vertices.length - 1; index++) {
        const u = [
          vertices[index][0] - anchor[0],
          vertices[index][1] - anchor[1],
          vertices[index][2] - anchor[2],
        ];
        const v = [
          vertices[index + 1][0] - anchor[0],
          vertices[index + 1][1] - anchor[1],
          vertices[index + 1][2] - anchor[2],
        ];
        const normal = [
          u[1] * v[2] - u[2] * v[1],
          u[2] * v[0] - u[0] * v[2],
          u[0] * v[1] - u[1] * v[0],
        ];
        const magnitude = Math.hypot(normal[0], normal[1], normal[2]);
        if (magnitude > 1e-8) {
          return normal.map((value) => value / magnitude);
        }
      }
      return null;
    }

    function normalFromTriangle(a, b, c, fallback = null) {
      const normal = normalFromVertices([a, b, c]);
      return normal || fallback;
    }

    function faceMatchesHighlight(face, highlightFaces, highlightComponents, highlightBodyKeys, highlightRawFaces) {
      if (highlightComponents?.length && face.componentId && highlightComponents.includes(face.componentId)) {
        return true;
      }
      if (highlightBodyKeys?.length && face.bodyRawKey && highlightBodyKeys.includes(face.bodyRawKey)) {
        return true;
      }
      if (highlightRawFaces?.length && face.rawFaceKey && highlightRawFaces.includes(face.rawFaceKey)) {
        return true;
      }
      if (!highlightFaces?.length) return false;
      return highlightFaces.some((item) => item.axis === face.axis && item.side === face.side);
    }

    function faceHighlightTone(face, options = {}) {
      const exactMatch = faceMatchesHighlight(
        face,
        [],
        options.exactHighlightComponents || [],
        options.exactHighlightBodyKeys || [],
        options.exactHighlightRawFaces || []
      );
      if (exactMatch) return "exact";
      if (faceMatchesHighlight(
        face,
        options.highlightFaces || [],
        options.highlightComponents || [],
        options.highlightBodyKeys || [],
        options.highlightRawFaces || []
      )) {
        return "changed";
      }
      return null;
    }

    function edgeMatchesHighlight(edge, highlightComponents, highlightBodyKeys, highlightRawEdges) {
      if (highlightComponents?.length && edge.componentId && highlightComponents.includes(edge.componentId)) {
        return true;
      }
      if (highlightBodyKeys?.length && edge.bodyRawKey && highlightBodyKeys.includes(edge.bodyRawKey)) {
        return true;
      }
      if (highlightRawEdges?.length && edge.rawEdgeKey && highlightRawEdges.includes(edge.rawEdgeKey)) {
        return true;
      }
      return false;
    }

    function edgeHighlightTone(edge, options = {}) {
      const exactMatch = edgeMatchesHighlight(
        edge,
        options.exactHighlightComponents || [],
        options.exactHighlightBodyKeys || [],
        options.exactHighlightRawEdges || []
      );
      if (exactMatch) return "exact";
      if (edgeMatchesHighlight(
        edge,
        options.highlightComponents || [],
        options.highlightBodyKeys || [],
        options.highlightRawEdges || []
      )) {
        return "changed";
      }
      return null;
    }

    function compareAnnotationLines(annotations, limit = 6, tone = null) {
      const seen = new Set();
      const lines = [];
      for (const annotation of annotations || []) {
        if (tone && (annotation?.tone || "changed") !== tone) continue;
        for (const line of annotation?.lines || []) {
          const text = String(line || "").trim();
          if (!text || seen.has(text)) continue;
          seen.add(text);
          lines.push(text);
          if (lines.length >= limit) return lines;
        }
      }
      return lines;
    }

    function drawArrow(ctx2d, start, end, color) {
      const dx = end[0] - start[0];
      const dy = end[1] - start[1];
      const length = Math.hypot(dx, dy);
      if (length < 1e-6) return;

      const ux = dx / length;
      const uy = dy / length;
      const head = Math.max(8, Math.min(14, length * 0.12));
      const wing = head * 0.55;

      ctx2d.strokeStyle = color;
      ctx2d.lineWidth = 2;
      ctx2d.beginPath();
      ctx2d.moveTo(start[0], start[1]);
      ctx2d.lineTo(end[0], end[1]);
      ctx2d.stroke();

      ctx2d.fillStyle = color;
      for (const tip of [start, end]) {
        const direction = tip === start ? -1 : 1;
        const backX = tip[0] - ux * head * direction;
        const backY = tip[1] - uy * head * direction;
        ctx2d.beginPath();
        ctx2d.moveTo(tip[0], tip[1]);
        ctx2d.lineTo(backX - uy * wing, backY + ux * wing);
        ctx2d.lineTo(backX + uy * wing, backY - ux * wing);
        ctx2d.closePath();
        ctx2d.fill();
      }
    }

    function drawCompareAnnotations(ctx2d, canvasEl, preview, viewer, scale, compareAnnotation) {
      const annotationList = [
        ...(compareAnnotation?.annotations || []),
        ...(compareAnnotation?.exactAnnotations || []),
      ];
      if (!annotationList.length || !preview) return;
      const hasExactGeometryHighlight = Boolean(
        (compareAnnotation?.exactHighlightRawFaces || []).length
        || (compareAnnotation?.exactHighlightRawEdges || []).length
        || (compareAnnotation?.exactHighlightBodyKeys || []).length
      );
      const shadow = "rgba(255,255,255,0.94)";
      const labelStack = [];
      const seenLabels = new Set();

      for (const annotation of annotationList) {
        const exactTone = (annotation?.tone || "changed") === "exact";
        const color = exactTone ? "#1e88e5" : "#ef6c00";
        const fillColor = exactTone ? "rgba(30,136,229,0.18)" : "rgba(255,190,102,0.22)";
        const focusColor = "#1e88e5";
        if (annotation?.point) {
          if (exactTone && hasExactGeometryHighlight) {
            for (const line of annotation.lines || []) {
              const key = String(line || "").trim();
              if (!key || seenLabels.has(key)) continue;
              seenLabels.add(key);
              labelStack.push(key);
            }
            continue;
          }
          const point = projectModelPoint(annotation.point, preview, viewer, scale, canvasEl);
          ctx2d.save();
          ctx2d.fillStyle = fillColor;
          ctx2d.strokeStyle = color;
          ctx2d.lineWidth = 2;
          ctx2d.beginPath();
          ctx2d.arc(point[0], point[1], 9, 0, Math.PI * 2);
          ctx2d.fill();
          ctx2d.stroke();
          ctx2d.restore();
          for (const line of annotation.lines || []) {
            const key = String(line || "").trim();
            if (!key || seenLabels.has(key)) continue;
            seenLabels.add(key);
            labelStack.push(key);
          }
          continue;
        }
        if (!annotation?.start || !annotation?.end) continue;
        const start = projectModelPoint(annotation.start, preview, viewer, scale, canvasEl);
        const end = projectModelPoint(annotation.end, preview, viewer, scale, canvasEl);
        const dx = end[0] - start[0];
        const dy = end[1] - start[1];
        const length = Math.hypot(dx, dy);
        if (length < 18) {
          continue;
        }

        const nx = -dy / length;
        const ny = dx / length;
        const offset = 18;
        const arrowStart = [start[0] + nx * offset, start[1] + ny * offset];
        const arrowEnd = [end[0] + nx * offset, end[1] + ny * offset];
        drawArrow(ctx2d, arrowStart, arrowEnd, color);

        ctx2d.strokeStyle = color;
        ctx2d.lineWidth = 1.5;
        ctx2d.beginPath();
        ctx2d.moveTo(start[0], start[1]);
        ctx2d.lineTo(arrowStart[0], arrowStart[1]);
        ctx2d.moveTo(end[0], end[1]);
        ctx2d.lineTo(arrowEnd[0], arrowEnd[1]);
        ctx2d.stroke();

        if (annotation.focusSegments?.length) {
          for (const segment of annotation.focusSegments) {
            const segmentStart = projectModelPoint(segment.start, preview, viewer, scale, canvasEl);
            const segmentEnd = projectModelPoint(segment.end, preview, viewer, scale, canvasEl);
            const highlightedStart = [segmentStart[0] + nx * offset, segmentStart[1] + ny * offset];
            const highlightedEnd = [segmentEnd[0] + nx * offset, segmentEnd[1] + ny * offset];
            const highlightedLength = Math.hypot(highlightedEnd[0] - highlightedStart[0], highlightedEnd[1] - highlightedStart[1]);
            if (highlightedLength < 8) continue;

            ctx2d.strokeStyle = exactTone ? "rgba(30,136,229,0.26)" : "rgba(30,136,229,0.18)";
            ctx2d.lineWidth = 10;
            ctx2d.lineCap = "round";
            ctx2d.beginPath();
            ctx2d.moveTo(highlightedStart[0], highlightedStart[1]);
            ctx2d.lineTo(highlightedEnd[0], highlightedEnd[1]);
            ctx2d.stroke();

            ctx2d.strokeStyle = focusColor;
            ctx2d.lineWidth = 4;
            ctx2d.beginPath();
            ctx2d.moveTo(highlightedStart[0], highlightedStart[1]);
            ctx2d.lineTo(highlightedEnd[0], highlightedEnd[1]);
            ctx2d.stroke();
          }

          if (annotation.deltaLabel) {
            const labelX = (arrowStart[0] + arrowEnd[0]) / 2;
            const labelY = (arrowStart[1] + arrowEnd[1]) / 2 - 22;
            ctx2d.save();
            ctx2d.font = "bold 11px 'Segoe UI'";
            ctx2d.textAlign = "center";
            ctx2d.textBaseline = "middle";
            const deltaText = annotation.focusSegments.length > 1
              ? `${annotation.deltaLabel} total`
              : annotation.deltaLabel;
            const horizontalPadding = 8;
            const verticalPadding = 6;
            const deltaWidth = ctx2d.measureText(deltaText).width + horizontalPadding * 2;
            const deltaHeight = 11 + verticalPadding * 2;

            ctx2d.fillStyle = "rgba(255,255,255,0.96)";
            ctx2d.fillRect(labelX - deltaWidth / 2, labelY, deltaWidth, deltaHeight);
            ctx2d.strokeStyle = focusColor;
            ctx2d.lineWidth = 1;
            ctx2d.strokeRect(labelX - deltaWidth / 2, labelY, deltaWidth, deltaHeight);
            ctx2d.fillStyle = focusColor;
            ctx2d.fillText(deltaText, labelX, labelY + deltaHeight / 2);
            ctx2d.restore();
          }

          ctx2d.lineCap = "butt";
        }

        for (const line of annotation.lines || []) {
          const key = String(line || "").trim();
          if (!key || seenLabels.has(key)) continue;
          seenLabels.add(key);
          labelStack.push(key);
        }
      }

      if (!labelStack.length) return;

      ctx2d.font = "bold 12px 'Segoe UI'";
      ctx2d.textBaseline = "top";
      const width = Math.max(...labelStack.map((line) => ctx2d.measureText(line).width)) + 18;
      const height = labelStack.length * 16 + 14;
      const x = canvasEl.width - width - 14;
      const y = 14;

      ctx2d.fillStyle = shadow;
      ctx2d.fillRect(x, y, width, height);
      ctx2d.strokeStyle = color;
      ctx2d.lineWidth = 1;
      ctx2d.strokeRect(x, y, width, height);

      ctx2d.fillStyle = color;
      labelStack.forEach((line, index) => {
        ctx2d.fillText(line, x + 9, y + 7 + index * 16);
      });
    }

    function projectModelPointToViewport(point, preview, viewport, canvasEl) {
      if (!viewport?.camera || !viewport?.THREE || !preview) return null;
      const vector = new viewport.THREE.Vector3(
        Number(point[0]) - preview.center[0],
        Number(point[1]) - preview.center[1],
        Number(point[2]) - preview.center[2]
      );
      vector.project(viewport.camera);
      return [
        ((vector.x + 1) * 0.5) * canvasEl.width,
        ((1 - vector.y) * 0.5) * canvasEl.height,
        vector.z,
      ];
    }

    function renderViewportAnnotations(viewport) {
      const canvasEl = viewport?.annotationCanvas;
      if (!canvasEl) return;
      resizeCanvasElement(canvasEl);
      const ctx2d = canvasEl.getContext("2d");
      if (!ctx2d) return;
      ctx2d.clearRect(0, 0, canvasEl.width, canvasEl.height);
      if (!viewport?.currentPreview || !viewport?.currentCompareAnnotation) return;

      const annotationList = [
        ...(viewport.currentCompareAnnotation.annotations || []),
        ...(viewport.currentCompareAnnotation.exactAnnotations || []),
      ];
      if (!annotationList.length) return;

      const hasExactGeometryHighlight = Boolean(
        (viewport.currentCompareAnnotation.exactHighlightRawFaces || []).length
        || (viewport.currentCompareAnnotation.exactHighlightRawEdges || []).length
        || (viewport.currentCompareAnnotation.exactHighlightBodyKeys || []).length
      );
      const shadow = "rgba(255,255,255,0.94)";
      const labelStack = [];
      const seenLabels = new Set();

      for (const annotation of annotationList) {
        const exactTone = (annotation?.tone || "changed") === "exact";
        const color = exactTone ? "#1e88e5" : "#ef6c00";
        const fillColor = exactTone ? "rgba(30,136,229,0.18)" : "rgba(255,190,102,0.22)";
        const focusColor = "#1e88e5";
        if (annotation?.point) {
          if (exactTone && hasExactGeometryHighlight) {
            for (const line of annotation.lines || []) {
              const key = String(line || "").trim();
              if (!key || seenLabels.has(key)) continue;
              seenLabels.add(key);
              labelStack.push(key);
            }
            continue;
          }
          const point = projectModelPointToViewport(annotation.point, viewport.currentPreview, viewport, canvasEl);
          if (!point) continue;
          ctx2d.save();
          ctx2d.fillStyle = fillColor;
          ctx2d.strokeStyle = color;
          ctx2d.lineWidth = 2;
          ctx2d.beginPath();
          ctx2d.arc(point[0], point[1], 9, 0, Math.PI * 2);
          ctx2d.fill();
          ctx2d.stroke();
          ctx2d.restore();
          for (const line of annotation.lines || []) {
            const key = String(line || "").trim();
            if (!key || seenLabels.has(key)) continue;
            seenLabels.add(key);
            labelStack.push(key);
          }
          continue;
        }
        if (!annotation?.start || !annotation?.end) continue;
        const start = projectModelPointToViewport(annotation.start, viewport.currentPreview, viewport, canvasEl);
        const end = projectModelPointToViewport(annotation.end, viewport.currentPreview, viewport, canvasEl);
        if (!start || !end) continue;
        const dx = end[0] - start[0];
        const dy = end[1] - start[1];
        const length = Math.hypot(dx, dy);
        if (length < 18) continue;

        const nx = -dy / length;
        const ny = dx / length;
        const offset = 18;
        const arrowStart = [start[0] + nx * offset, start[1] + ny * offset];
        const arrowEnd = [end[0] + nx * offset, end[1] + ny * offset];
        drawArrow(ctx2d, arrowStart, arrowEnd, color);

        ctx2d.strokeStyle = color;
        ctx2d.lineWidth = 1.5;
        ctx2d.beginPath();
        ctx2d.moveTo(start[0], start[1]);
        ctx2d.lineTo(arrowStart[0], arrowStart[1]);
        ctx2d.moveTo(end[0], end[1]);
        ctx2d.lineTo(arrowEnd[0], arrowEnd[1]);
        ctx2d.stroke();

        if (annotation.focusSegments?.length) {
          for (const segment of annotation.focusSegments) {
            const segmentStart = projectModelPointToViewport(segment.start, viewport.currentPreview, viewport, canvasEl);
            const segmentEnd = projectModelPointToViewport(segment.end, viewport.currentPreview, viewport, canvasEl);
            if (!segmentStart || !segmentEnd) continue;
            const highlightedStart = [segmentStart[0] + nx * offset, segmentStart[1] + ny * offset];
            const highlightedEnd = [segmentEnd[0] + nx * offset, segmentEnd[1] + ny * offset];
            const highlightedLength = Math.hypot(highlightedEnd[0] - highlightedStart[0], highlightedEnd[1] - highlightedStart[1]);
            if (highlightedLength < 8) continue;

            ctx2d.strokeStyle = exactTone ? "rgba(30,136,229,0.26)" : "rgba(30,136,229,0.18)";
            ctx2d.lineWidth = 10;
            ctx2d.lineCap = "round";
            ctx2d.beginPath();
            ctx2d.moveTo(highlightedStart[0], highlightedStart[1]);
            ctx2d.lineTo(highlightedEnd[0], highlightedEnd[1]);
            ctx2d.stroke();

            ctx2d.strokeStyle = focusColor;
            ctx2d.lineWidth = 4;
            ctx2d.beginPath();
            ctx2d.moveTo(highlightedStart[0], highlightedStart[1]);
            ctx2d.lineTo(highlightedEnd[0], highlightedEnd[1]);
            ctx2d.stroke();
          }

          if (annotation.deltaLabel) {
            const labelX = (arrowStart[0] + arrowEnd[0]) / 2;
            const labelY = (arrowStart[1] + arrowEnd[1]) / 2 - 22;
            ctx2d.save();
            ctx2d.font = "bold 11px 'Segoe UI'";
            ctx2d.textAlign = "center";
            ctx2d.textBaseline = "middle";
            const deltaText = annotation.focusSegments.length > 1
              ? `${annotation.deltaLabel} total`
              : annotation.deltaLabel;
            const horizontalPadding = 8;
            const verticalPadding = 6;
            const deltaWidth = ctx2d.measureText(deltaText).width + horizontalPadding * 2;
            const deltaHeight = 11 + verticalPadding * 2;

            ctx2d.fillStyle = "rgba(255,255,255,0.96)";
            ctx2d.fillRect(labelX - deltaWidth / 2, labelY, deltaWidth, deltaHeight);
            ctx2d.strokeStyle = focusColor;
            ctx2d.lineWidth = 1;
            ctx2d.strokeRect(labelX - deltaWidth / 2, labelY, deltaWidth, deltaHeight);
            ctx2d.fillStyle = focusColor;
            ctx2d.fillText(deltaText, labelX, labelY + deltaHeight / 2);
            ctx2d.restore();
          }

          ctx2d.lineCap = "butt";
        }

        for (const line of annotation.lines || []) {
          const key = String(line || "").trim();
          if (!key || seenLabels.has(key)) continue;
          seenLabels.add(key);
          labelStack.push(key);
        }
      }

      if (!labelStack.length) return;

      ctx2d.font = "bold 12px 'Segoe UI'";
      ctx2d.textBaseline = "top";
      const width = Math.max(...labelStack.map((line) => ctx2d.measureText(line).width)) + 18;
      const height = labelStack.length * 16 + 14;
      const x = canvasEl.width - width - 14;
      const y = 14;

      ctx2d.fillStyle = shadow;
      ctx2d.fillRect(x, y, width, height);
      ctx2d.strokeStyle = "#1e88e5";
      ctx2d.lineWidth = 1;
      ctx2d.strokeRect(x, y, width, height);

      ctx2d.fillStyle = "#1e88e5";
      labelStack.forEach((line, index) => {
        ctx2d.fillText(line, x + 9, y + 7 + index * 16);
      });
    }

    function cloneViewerState(viewer) {
      return {
        rotation: (viewer.rotation || defaultRotation()).map((row) => [...row]),
        zoom: viewer.zoom,
        panX: viewer.panX,
        panY: viewer.panY,
        mode: viewer.mode || "solid"
      };
    }

    function presetRotation(name) {
      if (name === "front") return rotationFromYawPitch(0, 0);
      if (name === "top") return rotationFromYawPitch(0, -Math.PI / 2);
      if (name === "right") return rotationFromYawPitch(-Math.PI / 2, 0);
      return defaultRotation();
    }

    function applyViewPreset(viewers, preset) {
      normalizeViewerList(viewers).forEach((viewer) => {
        if (preset === "fit") {
          viewer.zoom = 1;
          viewer.panX = 0;
          viewer.panY = 0;
          return;
        }
        viewer.rotation = presetRotation(preset);
        viewer.zoom = 1;
        viewer.panX = 0;
        viewer.panY = 0;
      });
    }

    function compareInteractionViewers(side) {
      if (!state.compareSync) {
        return [state.compareViewers[side]];
      }
      return side === "left"
        ? [state.compareViewers.left, state.compareViewers.right]
        : [state.compareViewers.right, state.compareViewers.left];
    }

    function normalizeViewerList(viewers) {
      return Array.isArray(viewers) ? viewers : [viewers];
    }

    function bindCanvasInteractions(canvasEl, viewerAccessor, redraw, reset, label) {
      if (!canvasEl) return;
      canvasEl.style.cursor = "grab";
      canvasEl.addEventListener("contextmenu", (event) => event.preventDefault());

      canvasEl.addEventListener("mousedown", (event) => {
        event.preventDefault();
        dragState = {
          x: event.clientX,
          y: event.clientY,
          pan: event.shiftKey || event.button === 1 || event.button === 2,
          viewers: normalizeViewerList(viewerAccessor()),
          redraw,
          canvasEl,
        };
        canvasEl.style.cursor = "grabbing";
        setStatus(`${label}: drag to rotate, Shift + drag to pan.`);
      });

      canvasEl.addEventListener("wheel", (event) => {
        event.preventDefault();
        const viewers = normalizeViewerList(viewerAccessor());
        const factor = event.deltaY > 0 ? 0.9 : 1.1;
        viewers.forEach((viewer) => {
          viewer.zoom = Math.min(10, Math.max(0.2, viewer.zoom * factor));
        });
        redraw();
      }, { passive: false });

      canvasEl.addEventListener("dblclick", () => {
        reset();
        redraw();
        setStatus(`${label} fit to view.`);
      });
    }

    function formatKeyValues(values) {
      const entries = Object.entries(values || {});
      if (!entries.length) return "No data found.";
      const width = Math.max(...entries.map(([key]) => key.length));
      return entries.map(([key, value]) => `${key.padEnd(width)} : ${value}`).join("\\n");
    }

    function setPrimarySelection(key) {
      state.selected = [key];
    }

    function addComparisonSelection(key) {
      state.selected = state.selected.filter((value) => value !== key);
      state.selected.push(key);
      if (state.selected.length > 2) {
        state.selected = state.selected.slice(-2);
      }
    }

    function handleSelectionClick(key, event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey) {
        addComparisonSelection(key);
      } else {
        setPrimarySelection(key);
      }
      if (state.selected.length === 2) {
        state.activeTab = "compare";
      } else if (state.activeTab === "compare") {
        state.activeTab = "overview";
      }
      resetPreviewIfNeeded();
      render();
    }

    function resetPreviewIfNeeded() {
      state.viewer = {
        ...defaultViewerState(),
        mode: state.viewer?.mode || "solid"
      };
      state.compareViewers = {
        left: {
          ...defaultViewerState(),
          mode: "solid"
        },
        right: {
          ...defaultViewerState(),
          mode: "solid"
        }
      };
    }

    function enrichReports(reports) {
      return reports.map((report) => {
        const geometry = report.geometry_hints || {};
        const scalars = geometry.notable_scalar_values || [];
        geometry.notable_scalar_values = scalars.map((item) => ({
          ...item,
          pretty_metric_and_imperial:
            item.pretty_metric_and_imperial ||
            `${item.value} (${item.assuming_meters.millimeters.toFixed(3)} mm, ${item.assuming_meters.inches.toFixed(3)} in)`
        }));
        return report;
      });
    }

    function mergeReports(incoming) {
      mergeReportsInternal(incoming, {});
    }

    function preserveExistingStepPreview(existing, report) {
      if (!existing || !report) return report;
      if (existing?.transmit_info?.source_format !== "step_file") return report;
      if (!Array.isArray(existing?.preview_kernel_bodies) || !existing.preview_kernel_bodies.length) return report;
      const existingBackend = String(existing?.step_import?.backend || "");
      const shouldPreserve = ["opencascade_occ", "browser_occt_import_js"].includes(existingBackend)
        || existing?.preview?.strategy === "step_import_preview";
      if (!shouldPreserve) return report;
      const mergedBackend = existingBackend || report?.step_import?.backend || report?.entity_hints?.step_backend || "browser_occt_import_js";
      return {
        ...report,
        preview: existing.preview || report.preview,
        preview_kernel_bodies: existing.preview_kernel_bodies,
        preview_geometry_hints: existing.preview_geometry_hints || report.preview_geometry_hints || report.geometry_hints,
        step_import: {
          ...(report.step_import || {}),
          backend: mergedBackend,
          occ_preview: existing.step_import?.occ_preview || report.step_import?.occ_preview,
          analysis_pending: false,
          refinement_pending: false,
        },
        entity_hints: {
          ...(report.entity_hints || {}),
          step_backend: mergedBackend,
        },
      };
    }

    function invalidateViewportCaches() {
      if (previewViewport) {
        previewViewport.currentReportKey = null;
        previewViewport.currentHiddenKey = "";
      }
      Object.values(compareViewports).forEach((viewport) => {
        if (!viewport) return;
        viewport.currentReportKey = null;
        viewport.currentHiddenKey = "";
      });
    }

    function mergeReportsInternal(incoming, options = {}) {
      const { preserveSelection = false } = options;
      const reportMap = new Map(state.reports.map((report) => [reportKey(report), report]));
      for (const report of enrichReports(incoming)) {
        reportMap.set(reportKey(report), report);
      }
      state.reports = [...reportMap.values()];
      if (incoming.length && !preserveSelection) {
        setPrimarySelection(bestReportKey(incoming));
      }
      invalidateViewportCaches();
      if (!preserveSelection) {
        resetPreviewIfNeeded();
      }
      render();
    }

    function mergeStepAnalysisReports(incoming) {
      const merged = enrichReports(incoming).map((report) => {
        const existing = state.reports.find((item) => reportKey(item) === reportKey(report));
        if (!existing) return report;
        return preserveExistingStepPreview(existing, report);
      });
      mergeReportsInternal(merged, { preserveSelection: true });
    }

    function mergeUploadedAnalysisReports(incoming, options = {}) {
      const { preserveSelection = false } = options;
      const merged = enrichReports(incoming).map((report) => {
        const existing = state.reports.find((item) => reportKey(item) === reportKey(report));
        return preserveExistingStepPreview(existing, report);
      });
      mergeReportsInternal(merged, { preserveSelection });
    }

    function renderFusionButton() {
      const button = byId("create-fusion");
      if (!button) return;
      button.disabled = !canFuseSelection();
    }

    function renderTabs() {
      const root = byId("tabs");
      root.innerHTML = "";
      for (const [id, label] of tabs) {
        const tab = document.createElement("div");
        tab.className = "tab" + (state.activeTab === id ? " active" : "");
        tab.textContent = label;
        tab.onclick = () => {
          state.activeTab = id;
          render();
        };
        root.appendChild(tab);
      }

      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.dataset.panel === state.activeTab);
      });
    }

    function renderPreviewModes() {
      const root = byId("preview-modes");
      const modes = [
        ["solid", "Solid"],
        ["wireframe", "Wireframe"],
        ["points", "Points"]
      ];
      root.innerHTML = '<span class="toolbar-label">Display</span>';
      for (const [mode, label] of modes) {
        const button = document.createElement("button");
        button.className = "preview-mode" + (state.viewer.mode === mode ? " active" : "");
        button.textContent = label;
        button.onclick = () => {
          state.viewer.mode = mode;
          renderPreviewModes();
          drawPreview();
        };
        root.appendChild(button);
      }
    }

    function renderPreviewPresets() {
      const root = byId("preview-toolbar-right");
      root.innerHTML = "";

      const actionGroup = document.createElement("div");
      actionGroup.className = "toolbar-cluster";
      const fusionButton = document.createElement("button");
      fusionButton.id = "create-fusion";
      fusionButton.className = "view-preset";
      fusionButton.textContent = "Improve JSON With STEP";
      fusionButton.disabled = !canFuseSelection();
      fusionButton.onclick = () => createFusionFromSelection().catch((error) => setStatus(error.message));
      actionGroup.appendChild(fusionButton);

      const viewGroup = document.createElement("div");
      viewGroup.className = "toolbar-cluster";
      viewGroup.innerHTML = '<span class="toolbar-label">Views</span>';
      VIEW_PRESETS.forEach(([preset, label]) => {
        const button = document.createElement("button");
        button.className = "view-preset";
        button.textContent = label;
        button.onclick = () => {
          applyPreviewPreset(preset);
          drawPreview();
          setStatus(`Preview set to ${label.toLowerCase()} view.`);
        };
        viewGroup.appendChild(button);
      });

      const backgroundGroup = document.createElement("div");
      backgroundGroup.className = "toolbar-cluster";
      backgroundGroup.innerHTML = '<span class="toolbar-label">Background</span>';
      VIEWPORT_BACKGROUNDS.forEach(([value, label]) => {
        const button = document.createElement("button");
        button.className = "view-preset" + (state.viewportBackground === value ? " active" : "");
        button.textContent = label;
        button.onclick = () => {
          state.viewportBackground = value;
          renderPreviewPresets();
          if (state.activeTab === "compare") {
            renderCompare();
          } else {
            drawPreview();
          }
          setStatus(`Viewport background set to ${label.toLowerCase()}.`);
        };
        backgroundGroup.appendChild(button);
      });

      root.appendChild(actionGroup);
      root.appendChild(viewGroup);
      root.appendChild(backgroundGroup);
    }

    function renderFileList() {
      const list = byId("file-list");
      list.innerHTML = "";

      if (!state.reports.length) {
        list.innerHTML = '<div class="empty">No reports loaded.</div>';
        byId("sidebar-foot").textContent = "Use the toolbar to load a STEP, STL, or another supported CAD file.";
        return;
      }

      const sorted = [...state.reports].sort((a, b) => a.file_name.localeCompare(b.file_name));
      for (const report of sorted) {
        const key = reportKey(report);
        const item = document.createElement("div");
        item.className = "file-item" + (state.selected.includes(key) ? " selected" : "");
        const bounds = report.geometry_hints?.bounds;
        item.innerHTML = `
          <strong>${escapeHtml(report.file_name)}</strong>
          <span>${escapeHtml(sourceFormatLabel(report))}</span>
          <span>${escapeHtml(report.decoded_names?.join(", ") || "No decoded names")}</span>
          <span>${escapeHtml(bounds ? formatBounds(bounds) : "No bounds inferred")}</span>
        `;
        item.onclick = (event) => handleSelectionClick(key, event);
        list.appendChild(item);
      }

      byId("sidebar-foot").textContent =
        canFuseSelection()
          ? "Two parts are selected. Compare them now, or use Improve JSON With STEP to make the JSON reconstruction reference the STEP geometry."
          : state.selected.length === 2
            ? "Two parts selected. The Compare view is ready."
            : `${state.reports.length} part report(s) loaded. STEP imports, STL meshes, JSON reconstructions, and Fusion previews appear together here.`;
    }

    function renderSummary() {
      const report = activeReport();
      byId("part-title").textContent = report ? report.file_name : "No part selected";

      const summary = byId("summary-grid");
      summary.innerHTML = "";

      if (!report) {
        summary.innerHTML = '<div class="muted">Load a file to see part information.</div>';
        return;
      }

      const rows = [
        ["Path", report.file],
        ["Source Format", sourceFormatLabel(report)],
        ["Preview Source", previewSourceLabel(report)],
        ["Preview Detail", previewSourceDescription(report)],
        ["STEP Importer", stepBackendLabel(report)],
        ["Decoded Names", report.decoded_names?.join(", ") || "None"],
        ["Source App", report.header?.PART1?.APPL || "Unknown"],
        ["Date", report.header?.PART1?.DATE || "Unknown"],
        ["Schema", report.header?.PART2?.SCH || "Unknown"],
        ["Kernel Body", kernelLabel(report)],
        ["Inferred Shape", topologyLabel(report)],
        ["Faces / Edges", topologyCounts(report)],
        ["Raw NX Faces / Edges", structuredTopologyLabel(report)],
        ["Density", formatDensity(report)],
        ["Colors", formatColors(report)],
        ["Bounds", formatBounds(report.geometry_hints?.bounds)],
        ["Preview Points", String(report.geometry_hints?.point_count || 0)],
        ["Scale Factor Attribute", report.has_scale_factor_attribute ? "Present" : "Not found"]
      ];

      if (report?.fusion?.enabled) {
        rows.splice(3, 0, ["Fusion", fusionLabel(report)], ["Fusion ML", fusionMlLabel(report)]);
        rows.splice(4, 0, ["Referenced STEP", report?.fusion?.reference_step?.file_name || report?.transmit_info?.reference_step_name || "Unknown"]);
      }

      for (const [label, value] of rows) {
        const row = document.createElement("div");
        row.innerHTML = `<strong>${escapeHtml(label)}:</strong> <span>${escapeHtml(value)}</span>`;
        summary.appendChild(row);
      }
    }

    function renderOverview() {
      const root = byId("overview-panel");
      const report = activeReport();
      if (!report) {
        root.innerHTML = '<div class="empty">Select a part to inspect it.</div>';
        return;
      }

      const rows = [
        ["File", report.file_name],
        ["Path", report.file],
        ["Source Format", sourceFormatLabel(report)],
        ["STEP Importer", stepBackendLabel(report)],
        ["Date", report.header?.PART1?.DATE || "Unknown"],
        ["Decoded Names", report.decoded_names?.join(", ") || "None"],
        ["Kernel Body", kernelLabel(report)],
        ["Inferred Shape", topologyLabel(report)],
        ["Faces / Edges", topologyCounts(report)],
        ["Raw NX Faces / Edges", structuredTopologyLabel(report)],
        ["Density", formatDensity(report)],
        ["Colors", formatColors(report)],
        ["Bounds", formatBounds(previewGeometryHints(report)?.bounds || report.geometry_hints?.bounds)],
        ["Preview Point Count", String(previewGeometryHints(report)?.point_count || 0)],
        ["Visible Preview Bodies", String(visiblePreviewBodyCount(report) || 0)],
        ["Object State IDs", String(report.object_state_ids?.length || 0)]
      ];

      if (report?.fusion?.enabled) {
        rows.splice(3, 0, ["Fusion", fusionLabel(report)], ["Fusion ML", fusionMlLabel(report)]);
      }

      const bodies = previewBodies(report);
      const hiddenBodyKeys = new Set(hiddenPreviewBodiesForReport(report));
      const bodyVisibilityHtml = bodies.length > 1
        ? `
          <div class="body-visibility">
            <div><strong>Preview Bodies</strong></div>
            <div class="body-visibility-actions">
              <button type="button" id="show-all-bodies">Show All</button>
              <button type="button" id="hide-all-bodies">Hide All</button>
            </div>
            <div class="body-visibility-list">
              ${bodies.map((body, bodyIndex) => {
                const bodyKey = previewBodyKey(body, bodyIndex);
                const bodyName = escapeHtml(body?.name || body?.metadata?.name || `Body ${bodyIndex + 1}`);
                const checked = hiddenBodyKeys.has(bodyKey) ? "" : "checked";
                const faceCount = Number(body?.faces?.length || 0);
                const edgeCount = Number(body?.edges?.length || 0);
                return `
                  <label class="body-visibility-item">
                    <input type="checkbox" class="body-toggle" data-body-key="${escapeHtml(bodyKey)}" ${checked}>
                    <span>${bodyName}</span>
                    <small>${faceCount} faces / ${edgeCount} edges</small>
                  </label>
                `;
              }).join("")}
            </div>
          </div>
        `
        : "";

      root.innerHTML = `
        <table>
          <thead><tr><th>Field</th><th>Value</th></tr></thead>
          <tbody>${rows.map(([label, value]) => `<tr><td>${escapeHtml(label)}</td><td>${escapeHtml(value)}</td></tr>`).join("")}</tbody>
        </table>
        ${bodyVisibilityHtml}
      `;

      if (bodies.length > 1) {
        byId("show-all-bodies").onclick = () => {
          setHiddenPreviewBodies(report, []);
          renderOverview();
          drawPreview();
          if (state.activeTab === "compare") drawComparePreviews();
        };
        byId("hide-all-bodies").onclick = () => {
          setHiddenPreviewBodies(report, bodies.map((body, bodyIndex) => previewBodyKey(body, bodyIndex)));
          renderOverview();
          drawPreview();
          if (state.activeTab === "compare") drawComparePreviews();
        };
        root.querySelectorAll(".body-toggle").forEach((input) => {
          input.onchange = (event) => {
            togglePreviewBody(report, event.target.dataset.bodyKey, event.target.checked);
          };
        });
      }
    }

    function renderNotes() {
      const panel = byId("notes-panel");
      const report = activeReport();
      if (!report) {
        panel.textContent = "Select a part to inspect notes.";
        return;
      }

      const lines = [
        "Part Summary",
        "",
        `Decoded entity names: ${report.decoded_names?.join(", ") || "None"}`,
        `Kernel body: ${kernelLabel(report)}`,
        `STEP importer: ${stepBackendLabel(report)}`,
        `Inferred shape: ${topologyLabel(report)}`,
        `Faces / edges: ${topologyCounts(report)}`,
        `Material density: ${formatDensity(report)}`,
        `Color swatches: ${formatColors(report)}`,
        `Scale factor attribute: ${report.has_scale_factor_attribute ? "Present" : "Not found"}`,
        `Object state IDs found: ${report.object_state_ids?.length || 0}`,
        `Preview point count: ${previewGeometryHints(report)?.point_count || 0}`,
        "",
        "Notable dimensions and scalar values",
        ""
      ];

      if (report?.fusion?.enabled) {
        lines.push("");
        lines.push("Fusion");
        lines.push("");
        lines.push(`Fusion status: ${fusionLabel(report)}`);
        lines.push(`Fusion ML: ${fusionMlLabel(report)}`);
        for (const line of fusionDecisionLines(report, 6)) {
          lines.push(line);
        }
      }

      const scalars = report.geometry_hints?.notable_scalar_values || [];
      if (scalars.length) {
        for (const item of scalars.slice(0, 8)) {
          lines.push(`- ${item.pretty_metric_and_imperial} [seen ${item.frequency} times]`);
        }
      } else {
        lines.push("No notable repeated scalar values were inferred.");
      }

      if (report.geometry_hints?.unit_inference) {
        lines.push("", "Unit inference", "", report.geometry_hints.unit_inference);
      }

      const modelSummary = report.model_analysis?.summary || [];
      if (modelSummary.length) {
        lines.push("", "Model-based inference", "");
        for (const line of modelSummary) {
          lines.push(`- ${line}`);
        }
      }

      const curveHints = report.model_analysis?.curve_hints || [];
      if (curveHints.length) {
        lines.push("", "Curve/entity hints", "");
        for (const line of curveHints) {
          lines.push(`- ${line}`);
        }
      }

      panel.textContent = lines.join("\\n");
    }

    function renderHeader() {
      const panel = byId("header-panel");
      const report = activeReport();
      if (!report) {
        panel.textContent = "Select a part to inspect header metadata.";
        return;
      }

      const lines = [];
      for (const [name, values] of Object.entries(report.header || {})) {
        lines.push(name, "-".repeat(name.length), formatKeyValues(values), "");
      }
      lines.push("Transmit Info", "-------------", formatKeyValues(report.transmit_info || {}));
      panel.textContent = lines.join("\\n").trim();
    }

    function renderGeometry() {
      const panel = byId("geometry-panel");
      const report = activeReport();
      if (!report) {
        panel.textContent = "Select a part to inspect geometry hints.";
        return;
      }

      const geometry = previewGeometryHints(report);
      const bounds = geometry?.bounds;
      const points = geometry?.preview_points || [];
      const ids = report.object_state_ids || [];

      panel.textContent = [
        "Bounds",
        "------",
        bounds
          ? `Min  : ${JSON.stringify(bounds.min)}\\nMax  : ${JSON.stringify(bounds.max)}\\nSize : ${JSON.stringify(bounds.size)}`
          : "No reliable bounding box was inferred from the text records in this file.",
        "",
        "Preview Points",
        "--------------",
        points.length ? points.map((point) => JSON.stringify(point)).join("\\n") : "No preview points were extracted.",
        "",
        "Object State IDs",
        "----------------",
        ids.length ? ids.join("\\n") : "None"
      ].join("\\n");
    }

    function renderTopology() {
      const panel = byId("topology-panel");
      const report = activeReport();
      if (!report) {
        panel.textContent = "Select a part to inspect imported or reconstructed faces and edges.";
        return;
      }

      const analysis = report.model_analysis || {};
      const topology = analysis.topology;
      const kernelBodiesList = kernelBodies(report);
      const kernelBody = kernelBodiesList.length === 1 ? kernelBodiesList[0] : null;
      const kernelTopology = report.kernel_topology || null;
      const importedBodies = report.nx_import?.body_summaries || [];
      const levels = analysis.unique_axis_levels || {};
      const lines = [
        "Model Analysis",
        "--------------",
      ];

      if (analysis.summary?.length) {
        for (const line of analysis.summary) {
          lines.push(`- ${line}`);
        }
      } else {
        lines.push("No high-confidence model summary was inferred.");
      }

      lines.push(
        "",
        "Kernel Body",
        "-----------",
        `Name : ${kernelBody?.name || "None"}`,
        `Kind : ${kernelBody?.kind || "None"}`,
        `Primitive : ${kernelBody?.metadata?.primitive || "None"}`,
        `Topology vertices : ${kernelTopology?.vertices?.length || 0}`,
        `Topology edges : ${kernelTopology?.edges?.length || 0}`,
        `Topology loops : ${kernelTopology?.loops?.length || 0}`,
        `Topology faces : ${kernelTopology?.faces?.length || 0}`,
        `Imported bodies : ${kernelBodiesList.length || 0}`,
        "",
        "Axis Levels",
        "-----------",
        `X : ${(levels.x || []).join(", ") || "None"}`,
        `Y : ${(levels.y || []).join(", ") || "None"}`,
        `Z : ${(levels.z || []).join(", ") || "None"}`
      );

      if (importedBodies.length) {
        lines.push("", "Imported Body Summary", "---------------------");
        for (const body of importedBodies) {
          lines.push(`Body ${body.index} | ${body.shape_summary} | ${body.face_count} faces | ${body.edge_count} edges`);
        }
      }

      if (!topology) {
        if (analysis.curve_hints?.length) {
          lines.push("", "Curve / Entity Hints", "--------------------");
          for (const hint of analysis.curve_hints) {
            lines.push(`- ${hint}`);
          }
        }
        if (kernelTopology) {
          lines.push("", "Kernel Topology", "---------------");
          for (const face of kernelTopology.faces || []) {
            lines.push(`${face.id} | ${face.name} | ${face.surface_kind} | loops ${face.loop_ids.join(", ")}`);
          }
        }
        panel.textContent = lines.join("\\n");
        return;
      }

      lines.push(
        "",
        "Topology",
        "--------",
        `Kind : ${topology.kind}`,
        `Shape: ${topology.shape_label}`,
        `Faces: ${topology.face_count}`,
        `Edges: ${topology.edge_count}`,
        `Matched corners: ${topology.matched_corner_count} of ${topology.corner_count}`
      );

      lines.push("", "Faces", "-----");
      for (const face of topology.faces || []) {
        lines.push(
          `${face.name} | ${face.kind} | dims ${face.dimensions.first.model_units} x ${face.dimensions.second.model_units} | area ${face.area.model_units}`
        );
      }

      lines.push("", "Edge Families", "-------------");
      for (const edge of topology.edge_families || []) {
        lines.push(
          `Length ${edge.length.model_units} (${edge.length.millimeters} mm, ${edge.length.inches} in) | count ${edge.count}`
        );
      }

      if (kernelTopology) {
        lines.push("", "Kernel Topology", "---------------");
        for (const edge of kernelTopology.edges || []) {
          lines.push(`${edge.id} | ${edge.kind} | vertices ${edge.vertex_ids.join(" -> ")}`);
        }
        for (const face of kernelTopology.faces || []) {
          lines.push(`${face.id} | ${face.name} | ${face.surface_kind} | loops ${face.loop_ids.join(", ")}`);
        }
      }

      if (analysis.curve_hints?.length) {
        lines.push("", "Curve / Entity Hints", "--------------------");
        for (const hint of analysis.curve_hints) {
          lines.push(`- ${hint}`);
        }
      }

      panel.textContent = lines.join("\\n");
    }

    function renderCompare() {
      const root = byId("compare-panel");
      const reports = comparisonReports();
      if (reports.length !== 2) {
        destroyCompareViewports();
        root.innerHTML = '<div class="empty">Select exactly two parts from the left list to compare them.</div>';
        return;
      }

      const [left, right] = reports;
      requestCompareMl(left, right).catch((error) => setStatus(error.message));
      const compareMl = compareMlResult(left, right);
      const diff = mergeCompareResults(compareDiffInfo(left, right), compareMl);
      const changeLines = diff.summary.length
        ? diff.summary
        : ["No geometric size change was inferred from the current preview data."];
      const leftExactCallouts = compareAnnotationLines(diff.exactAnnotations.left, 4, "exact");
      const rightExactCallouts = compareAnnotationLines(diff.exactAnnotations.right, 4, "exact");
      const leftCallouts = compareAnnotationLines(diff.annotations.left);
      const rightCallouts = compareAnnotationLines(diff.annotations.right);
      const rows = [
        ["Decoded Names", left.decoded_names?.join(", ") || "None", right.decoded_names?.join(", ") || "None"],
        ["Inferred Shape", topologyLabel(left), topologyLabel(right)],
        ["Faces / Edges", topologyCounts(left), topologyCounts(right)],
        ["Raw NX Faces / Edges", structuredTopologyLabel(left), structuredTopologyLabel(right)],
        ["Matched Preview Components", String(diff.stats.matchedComponents || 0), String(diff.stats.matchedComponents || 0)],
        ["Changed / Removed / Added", `${diff.stats.changedComponents || 0} / ${diff.stats.removedComponents || 0} / 0`, `${diff.stats.changedComponents || 0} / 0 / ${diff.stats.addedComponents || 0}`],
        ["Match Quality", diff.stats.matchQuality || "Unknown", diff.stats.matchQuality || "Unknown"],
        ["Comparison ML", diff.stats.ml || "Loading", diff.stats.ml || "Loading"],
        ["ML Changed / Removed / Added", `${diff.stats.mlChangedComponents ?? "-"} / ${diff.stats.mlRemovedComponents ?? "-"} / 0`, `${diff.stats.mlChangedComponents ?? "-"} / 0 / ${diff.stats.mlAddedComponents ?? "-"}`],
        ["Density", formatDensity(left), formatDensity(right)],
        ["Colors", formatColors(left), formatColors(right)],
        ["Bounds", formatBounds(left.geometry_hints?.bounds), formatBounds(right.geometry_hints?.bounds)],
        ["Preview Points", String(left.geometry_hints?.point_count || 0), String(right.geometry_hints?.point_count || 0)],
        ["Object State IDs", String(left.object_state_ids?.length || 0), String(right.object_state_ids?.length || 0)]
      ];
      const rowMarkup = rows.map(([label, a, b]) => {
        const rowClass = a !== b ? ' class="compare-table-row-changed"' : "";
        return `<tr${rowClass}><td>${escapeHtml(label)}</td><td>${escapeHtml(a)}</td><td>${escapeHtml(b)}</td></tr>`;
      }).join("");
      const changeMarkup = changeLines
        .map((line) => `<div class="compare-change-pill">${escapeHtml(line)}</div>`)
        .join("");

      root.innerHTML = `
        <div class="compare-layout">
          <div class="compare-summary">
            <h3>What Changed</h3>
            <div class="compare-summary-list">
              ${changeLines.map((line) => `<div class="compare-summary-item">${escapeHtml(line)}</div>`).join("")}
            </div>
          </div>
          <div class="compare-controls">
            <label>
              <input type="checkbox" id="compare-sync-toggle" ${state.compareSync ? "checked" : ""}>
              Move both models together
            </label>
            <div class="toolbar-cluster" id="compare-view-presets"></div>
            <div class="toolbar-cluster" id="compare-background-presets"></div>
          </div>
          <div class="compare-preview-grid">
            <div class="compare-preview-card">
              <h3>${escapeHtml(left.file_name)}</h3>
              <div class="canvas-frame">
                <canvas id="compare-left-canvas" class="compare-canvas"></canvas>
                <canvas id="compare-left-annotation-canvas" class="annotation-canvas"></canvas>
                <div id="compare-left-overlay" class="viewport-overlay"></div>
              </div>
              <div class="compare-note">Orbit with drag. Pan with Shift + drag or right-drag. Mouse wheel zooms. This view now renders directly in WebGL.</div>
              <div class="compare-note">Orange marks changed regions. Blue marks exact measured differences.</div>
              <div class="compare-note">${compareMl ? escapeHtml(`Comparison ML: ${compareMlStatusLabel(compareMl)}`) : "Comparison ML is loading in the background."}</div>
              <div class="compare-note">${leftExactCallouts.length ? leftExactCallouts.map((line) => escapeHtml(line)).join("<br>") : "Exact size, radius, and diameter differences will show in blue when detected."}</div>
              <div class="compare-note">${leftCallouts.length ? leftCallouts.map((line) => escapeHtml(line)).join("<br>") : "Changed regions will highlight in orange on this side."}</div>
            </div>
            <div class="compare-preview-card">
              <h3>${escapeHtml(right.file_name)}</h3>
              <div class="canvas-frame">
                <canvas id="compare-right-canvas" class="compare-canvas"></canvas>
                <canvas id="compare-right-annotation-canvas" class="annotation-canvas"></canvas>
                <div id="compare-right-overlay" class="viewport-overlay"></div>
              </div>
              <div class="compare-note">This comparison pane uses the same WebGL mesh pipeline as the main preview.</div>
              <div class="compare-note">Orange marks changed regions. Blue marks exact measured differences.</div>
              <div class="compare-note">${compareMl?.model?.type ? escapeHtml(`ML model: ${compareMl.model.type}`) : "Learning-based compare will refine matches when available."}</div>
              <div class="compare-note">${rightExactCallouts.length ? rightExactCallouts.map((line) => escapeHtml(line)).join("<br>") : "Exact size, radius, and diameter differences will show in blue when detected."}</div>
              <div class="compare-note">${rightCallouts.length ? rightCallouts.map((line) => escapeHtml(line)).join("<br>") : "Changed regions will highlight in orange on this side."}</div>
            </div>
          </div>
          <table>
            <thead>
              <tr>
                <th>Field</th>
                <th>${escapeHtml(left.file_name)}</th>
                <th>${escapeHtml(right.file_name)}</th>
              </tr>
            </thead>
            <tbody>
              <tr class="compare-change-row">
                <td>What Changed</td>
                <td colspan="2">
                  <div class="compare-change-cell">${changeMarkup}</div>
                </td>
              </tr>
              ${rowMarkup}
            </tbody>
          </table>
        </div>
      `;

      const presetRoot = byId("compare-view-presets");
      presetRoot.innerHTML = '<span class="toolbar-label">Views</span>';
      VIEW_PRESETS.forEach(([preset, label]) => {
        const button = document.createElement("button");
        button.className = "view-preset";
        button.textContent = label;
        button.onclick = () => {
          applyComparePreset(preset);
          setStatus(`Comparison previews set to ${label.toLowerCase()} view.`);
        };
        presetRoot.appendChild(button);
      });

      const backgroundRoot = byId("compare-background-presets");
      backgroundRoot.innerHTML = '<span class="toolbar-label">Background</span>';
      VIEWPORT_BACKGROUNDS.forEach(([value, label]) => {
        const button = document.createElement("button");
        button.className = "view-preset" + (state.viewportBackground === value ? " active" : "");
        button.textContent = label;
        button.onclick = () => {
          state.viewportBackground = value;
          renderPreviewPresets();
          renderCompare();
          setStatus(`Viewport background set to ${label.toLowerCase()}.`);
        };
        backgroundRoot.appendChild(button);
      });

      byId("compare-sync-toggle").addEventListener("change", (event) => {
        state.compareSync = event.target.checked;
        if (state.compareSync) {
          syncCompareViewports();
          setStatus("Comparison sync enabled. Moving either model now moves both.");
        } else {
          setStatus("Comparison sync disabled. Each model moves independently.");
        }
      });

      drawComparePreviews();
    }

    function drawComparePreviews() {
      const reports = comparisonReports();
      if (reports.length !== 2) {
        destroyCompareViewports();
        return;
      }

      const [left, right] = reports;
      const diff = mergeCompareResults(compareDiffInfo(left, right), compareMlResult(left, right));
      Promise.all([
        ensureCompareViewport("left"),
        ensureCompareViewport("right"),
      ])
        .then(([leftViewport, rightViewport]) => {
          if (!leftViewport || !rightViewport) return;
          setPreviewViewportBackground(leftViewport);
          setPreviewViewportBackground(rightViewport);
          rebuildPreviewViewport(leftViewport, left, {
            fitView: leftViewport.currentReportKey !== reportKey(left) || !leftViewport.previewObjects,
            mode: "solid",
            compareAnnotation: {
              annotations: diff.annotations.left,
              exactAnnotations: diff.exactAnnotations.left,
              exactHighlightBodyKeys: diff.exactHighlightBodyKeys.left,
              exactHighlightRawFaces: diff.exactHighlightRawFaces.left,
              exactHighlightRawEdges: diff.exactHighlightRawEdges.left,
            },
            highlightComponents: diff.highlightComponents.left,
            highlightBodyKeys: diff.highlightBodyKeys.left,
            highlightRawFaces: diff.highlightRawFaces.left,
            highlightRawEdges: diff.highlightRawEdges.left,
            exactHighlightBodyKeys: diff.exactHighlightBodyKeys.left,
            exactHighlightRawFaces: diff.exactHighlightRawFaces.left,
            exactHighlightRawEdges: diff.exactHighlightRawEdges.left,
          });
          rebuildPreviewViewport(rightViewport, right, {
            fitView: rightViewport.currentReportKey !== reportKey(right) || !rightViewport.previewObjects,
            mode: "solid",
            compareAnnotation: {
              annotations: diff.annotations.right,
              exactAnnotations: diff.exactAnnotations.right,
              exactHighlightBodyKeys: diff.exactHighlightBodyKeys.right,
              exactHighlightRawFaces: diff.exactHighlightRawFaces.right,
              exactHighlightRawEdges: diff.exactHighlightRawEdges.right,
            },
            highlightComponents: diff.highlightComponents.right,
            highlightBodyKeys: diff.highlightBodyKeys.right,
            highlightRawFaces: diff.highlightRawFaces.right,
            highlightRawEdges: diff.highlightRawEdges.right,
            exactHighlightBodyKeys: diff.exactHighlightBodyKeys.right,
            exactHighlightRawFaces: diff.exactHighlightRawFaces.right,
            exactHighlightRawEdges: diff.exactHighlightRawEdges.right,
          });
          syncCompareViewports();
        })
        .catch((error) => {
          setStatus(`Comparison preview error: ${error.message}`);
        });
    }

    function renderJson() {
      const panel = byId("json-panel");
      const report = activeReport();
      panel.textContent = report ? JSON.stringify(report, null, 2) : "Select a part to inspect its JSON.";
    }

    function resizeCanvasElement(canvasEl) {
      const rect = canvasEl.getBoundingClientRect();
      canvasEl.width = Math.max(320, Math.floor(rect.width));
      canvasEl.height = Math.max(320, Math.floor(rect.height));
    }

    function rotatePoint(point, viewer) {
      return applyRotationMatrix(viewer?.rotation || defaultRotation(), point);
    }

    function projectPoint(point, scale, canvasEl, viewer, viewerDistance = 4) {
      const safeDistance = Math.max(4, Number(viewerDistance) || 4);
      const denominator = Math.max(safeDistance * 0.2, safeDistance + point[2]);
      const depth = safeDistance / denominator;
      return [
        point[0] * scale * depth + canvasEl.width / 2 + viewer.panX,
        -point[1] * scale * depth + canvasEl.height / 2 + viewer.panY,
        depth
      ];
    }

    function drawCornerTriad(canvasEl, ctx2d, viewer) {
      const theme = currentViewportTheme();
      const centerX = 72;
      const centerY = canvasEl.height - 72;
      const triadScale = 34;

      function projectTriad(point) {
        const rotated = rotatePoint(point, viewer);
        return [
          centerX + rotated[0] * triadScale,
          centerY - rotated[1] * triadScale,
          rotated[2]
        ];
      }

      const origin = projectTriad([0, 0, 0]);
      const axes = [
        { point: [1, 0, 0], color: "#c62828", label: "X" },
        { point: [0, 1, 0], color: "#2e7d32", label: "Y" },
        { point: [0, 0, 1], color: "#1565c0", label: "Z" }
      ];

      ctx2d.fillStyle = theme.hudFill;
      ctx2d.strokeStyle = theme.hudStroke;
      ctx2d.lineWidth = 1;
      ctx2d.beginPath();
      ctx2d.rect(16, canvasEl.height - 128, 112, 112);
      ctx2d.fill();
      ctx2d.stroke();

      for (const axis of axes) {
        const end = projectTriad(axis.point);
        ctx2d.strokeStyle = axis.color;
        ctx2d.lineWidth = 2;
        ctx2d.beginPath();
        ctx2d.moveTo(origin[0], origin[1]);
        ctx2d.lineTo(end[0], end[1]);
        ctx2d.stroke();
        ctx2d.fillStyle = axis.color;
        ctx2d.beginPath();
        ctx2d.arc(end[0], end[1], 4, 0, Math.PI * 2);
        ctx2d.fill();
        ctx2d.font = "bold 12px 'Segoe UI'";
        ctx2d.fillText(axis.label, end[0] + 7, end[1] - 4);
      }

      ctx2d.fillStyle = theme.hudText;
      ctx2d.beginPath();
      ctx2d.arc(origin[0], origin[1], 3, 0, Math.PI * 2);
      ctx2d.fill();
    }

    function currentViewportTheme() {
      if (state.viewportBackground === "white") {
        return {
          background: "#ffffff",
          hudFill: "rgba(245, 245, 245, 0.92)",
          hudStroke: "rgba(70, 70, 70, 0.22)",
          hudText: "#333333",
          secondaryText: "#4a4a4a",
          point: "#444444",
          faceStroke: "rgba(58, 62, 70, 0.34)",
          bounds: "rgba(110, 110, 110, 0.5)",
          wireframe: "rgba(70, 70, 70, 0.68)",
          emptyText: "rgba(70, 70, 70, 0.92)",
          surfaceShadow: [76, 80, 88],
          surfaceHighlight: [156, 162, 172],
          surfaceAmbient: 0.36,
        };
      }
      if (state.viewportBackground === "dark") {
        return {
          background: "#1d1d1d",
          hudFill: "rgba(24, 24, 24, 0.72)",
          hudStroke: "rgba(220, 220, 220, 0.15)",
          hudText: "rgba(242, 242, 242, 0.95)",
          secondaryText: "rgba(241, 241, 241, 0.92)",
          point: "rgba(241, 241, 241, 0.86)",
          faceStroke: "rgba(208, 214, 224, 0.18)",
          bounds: "rgba(215, 215, 215, 0.4)",
          wireframe: "rgba(226, 226, 226, 0.6)",
          emptyText: "rgba(241, 241, 241, 0.92)",
          surfaceShadow: [68, 72, 80],
          surfaceHighlight: [146, 152, 164],
          surfaceAmbient: 0.3,
        };
      }
      return {
        background: "#d9d9d9",
        hudFill: "rgba(238, 238, 238, 0.9)",
        hudStroke: "rgba(80, 80, 80, 0.18)",
        hudText: "#323232",
        secondaryText: "#404040",
        point: "#404040",
        faceStroke: "rgba(54, 58, 66, 0.3)",
        bounds: "rgba(95, 95, 95, 0.45)",
        wireframe: "rgba(70, 70, 70, 0.62)",
        emptyText: "rgba(55, 55, 55, 0.92)",
        surfaceShadow: [72, 76, 84],
        surfaceHighlight: [148, 154, 164],
        surfaceAmbient: 0.34,
      };
    }

    function drawViewportBackground(ctx2d, canvasEl) {
      ctx2d.fillStyle = currentViewportTheme().background;
      ctx2d.fillRect(0, 0, canvasEl.width, canvasEl.height);
    }

    function hexToRgb(value) {
      const text = String(value || "").trim();
      if (/^#([0-9a-f]{3})$/i.test(text)) {
        const [, short] = text.match(/^#([0-9a-f]{3})$/i);
        return short.split("").map((char) => parseInt(char + char, 16));
      }
      if (/^#([0-9a-f]{6})$/i.test(text)) {
        const [, full] = text.match(/^#([0-9a-f]{6})$/i);
        return [0, 2, 4].map((index) => parseInt(full.slice(index, index + 2), 16));
      }
      return [200, 200, 200];
    }

    function rgbCss(rgb) {
      if (typeof rgb === "string") return rgb;
      return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
    }

    function previewEdgePalette() {
      if (state.viewportBackground === "dark") {
        return {
          edgeColor: [224, 229, 236],
          pointColor: [241, 241, 241],
        };
      }
      if (state.viewportBackground === "white") {
        return {
          edgeColor: [86, 94, 106],
          pointColor: [48, 55, 65],
        };
      }
      return {
        edgeColor: [78, 84, 94],
        pointColor: [56, 60, 68],
      };
    }

    function previewPresetDirection(preset) {
      const directions = {
        iso: [1.2, 1, 0.85],
        front: [0, 0, 1],
        top: [0, 1, 0],
        right: [1, 0, 0],
      };
      return normalizeVector(directions[preset] || directions.iso);
    }

    function previewPresetUp(direction) {
      const worldUp = [0, 1, 0];
      const fallbackUp = [0, 0, 1];
      const alignment = Math.abs(direction[0] * worldUp[0] + direction[1] * worldUp[1] + direction[2] * worldUp[2]);
      return alignment > 0.98 ? fallbackUp : worldUp;
    }

    function disposeThreeObject(object3d) {
      if (!object3d) return;
      object3d.traverse((child) => {
        if (child.geometry?.dispose) {
          child.geometry.dispose();
        }
        if (Array.isArray(child.material)) {
          child.material.forEach((material) => material?.dispose?.());
        } else if (child.material?.dispose) {
          child.material.dispose();
        }
      });
    }

    function clearPreviewViewportContent(viewport) {
      if (!viewport?.contentGroup) return;
      const children = [...viewport.contentGroup.children];
      for (const child of children) {
        viewport.contentGroup.remove(child);
        disposeThreeObject(child);
      }
      viewport.previewObjects = null;
      viewport.currentPreview = null;
      viewport.currentReportKey = null;
      viewport.currentHiddenKey = "";
      viewport.currentCompareAnnotation = null;
      if (viewport.annotationCanvas) {
        resizeCanvasElement(viewport.annotationCanvas);
        viewport.annotationCanvas.getContext("2d")?.clearRect(0, 0, viewport.annotationCanvas.width, viewport.annotationCanvas.height);
      }
    }

    function resizePreviewViewport(viewport) {
      if (!viewport?.renderer || !viewport?.camera) return;
      const rect = viewport.canvas?.getBoundingClientRect?.() || { width: 0, height: 0 };
      const width = Math.max(320, Math.floor(rect.width));
      const height = Math.max(320, Math.floor(rect.height));
      viewport.renderer.setSize(width, height, false);
      viewport.camera.aspect = width / height;
      viewport.camera.updateProjectionMatrix();
    }

    function setPreviewViewportBackground(viewport) {
      if (!viewport?.renderer || !viewport?.THREE) return;
      const background = VIEWPORT_BACKGROUNDS.find(([value]) => value === state.viewportBackground)?.[2] || "#d9d9d9";
      viewport.renderer.setClearColor(new viewport.THREE.Color(background), 1);
    }

    function renderPreviewViewport(viewport) {
      if (!viewport?.renderer || !viewport?.scene || !viewport?.camera) return;
      resizePreviewViewport(viewport);
      viewport.renderer.render(viewport.scene, viewport.camera);
      renderViewportAnnotations(viewport);
    }

    function fitPreviewViewport(viewport, preset = "iso") {
      if (!viewport?.camera || !viewport?.controls || !viewport?.currentPreview) return;
      const direction = previewPresetDirection(preset);
      const preview = viewport.currentPreview;
      const radius = Math.max(
        preview?.bounds?.size ? Math.hypot(...preview.bounds.size) * 0.5 : 0,
        preview?.span ? preview.span * 0.6 : 0,
        1e-3
      );
      const fovRadians = (viewport.camera.fov * Math.PI) / 180;
      const distance = Math.max(radius / Math.sin(Math.max(fovRadians / 2, 1e-3)), radius * 1.75);
      viewport.controls.target.set(0, 0, 0);
      viewport.camera.position.set(
        direction[0] * distance,
        direction[1] * distance,
        direction[2] * distance
      );
      viewport.camera.up.set(...previewPresetUp(direction));
      viewport.camera.near = Math.max(0.001, distance / 1000);
      viewport.camera.far = Math.max(10, distance + radius * 8);
      viewport.camera.updateProjectionMatrix();
      viewport.controls.update();
      viewport.currentPreset = preset === "fit" ? (viewport.currentPreset || "iso") : preset;
      renderPreviewViewport(viewport);
    }

    function applyPreviewViewportMode(viewport, mode) {
      if (!viewport?.previewObjects) return;
      const previewStrategy = String(viewport?.currentPreview?.strategy || "");
      const reconstructedSolidPreview = previewStrategy === "json_reconstruction_preview" || previewStrategy === "fused_json_step_preview";
      const {
        compareStyle,
        solidMesh,
        wireframe,
        pointCloud,
        edgeLines,
        fallbackEdgeLines,
        highlightSurfaceMesh,
        exactHighlightSurfaceMesh,
        highlightSurfaceWireframe,
        exactHighlightSurfaceWireframe,
        highlightEdgeLines,
        exactHighlightEdgeLines,
      } = viewport.previewObjects;
      const hasSurfaceGeometry = Boolean(
        solidMesh
        || wireframe
        || edgeLines
        || fallbackEdgeLines
        || highlightSurfaceMesh
        || exactHighlightSurfaceMesh
        || highlightSurfaceWireframe
        || exactHighlightSurfaceWireframe
        || highlightEdgeLines
        || exactHighlightEdgeLines
      );
      const showSolid = mode === "solid";
      const showWireframe = mode === "wireframe";
      const showPoints = mode === "points";
      if (solidMesh) solidMesh.visible = mode === "solid";
      if (solidMesh?.material) {
        solidMesh.material.opacity = 1;
        solidMesh.material.transparent = false;
        solidMesh.material.depthWrite = true;
      }
      if (highlightSurfaceMesh) highlightSurfaceMesh.visible = showSolid;
      if (exactHighlightSurfaceMesh) exactHighlightSurfaceMesh.visible = showSolid;
      if (pointCloud) pointCloud.visible = showPoints || !hasSurfaceGeometry;
      if (wireframe) {
        wireframe.visible = showWireframe;
        wireframe.material.opacity = showWireframe ? 1 : 0;
      }
      if (edgeLines) {
        edgeLines.visible = showWireframe || (showSolid && reconstructedSolidPreview);
        edgeLines.material.opacity = showWireframe ? 0.78 : (showSolid && reconstructedSolidPreview ? 0.2 : 0.32);
      }
      if (fallbackEdgeLines) {
        fallbackEdgeLines.visible = showWireframe || showSolid;
        fallbackEdgeLines.material.opacity = showWireframe ? 0.78 : 0.4;
      }
      if (highlightSurfaceWireframe) {
        highlightSurfaceWireframe.visible = showWireframe || showSolid;
        highlightSurfaceWireframe.material.opacity = showWireframe ? 1 : 0.98;
      }
      if (exactHighlightSurfaceWireframe) {
        exactHighlightSurfaceWireframe.visible = (showWireframe || showSolid);
        exactHighlightSurfaceWireframe.material.opacity = showWireframe ? 1 : 0.98;
      }
      if (highlightEdgeLines) {
        highlightEdgeLines.visible = showWireframe || showSolid;
        highlightEdgeLines.material.opacity = showWireframe ? 0.98 : 0.92;
      }
      if (exactHighlightEdgeLines) {
        exactHighlightEdgeLines.visible = (showWireframe || showSolid);
        exactHighlightEdgeLines.material.opacity = showWireframe ? 0.99 : 0.96;
      }
    }

    function buildPreviewViewportObjects(viewport, preview, options = {}) {
      const { THREE } = viewport;
      const palette = previewEdgePalette();
      const comparisonStyle = Boolean(options.compareStyle);
      const highlightComponents = options.highlightComponents || [];
      const highlightBodyKeys = options.highlightBodyKeys || [];
      const highlightRawFaces = options.highlightRawFaces || [];
      const highlightRawEdges = options.highlightRawEdges || [];
      const exactHighlightComponents = options.exactHighlightComponents || [];
      const exactHighlightBodyKeys = options.exactHighlightBodyKeys || [];
      const exactHighlightRawFaces = options.exactHighlightRawFaces || [];
      const exactHighlightRawEdges = options.exactHighlightRawEdges || [];
      const surfaceExactHighlightComponents = exactHighlightComponents;
      const surfaceExactHighlightBodyKeys = exactHighlightBodyKeys;
      const surfaceExactHighlightRawFaces = exactHighlightRawFaces;
      const surfaceExactHighlightRawEdges = exactHighlightRawEdges;
      const group = new THREE.Group();
      const solidPositions = [];
      const solidNormals = [];
      const solidColors = [];
      const edgePositions = [];
      const fallbackEdgePositions = [];
      const highlightSurfacePositions = [];
      const exactHighlightSurfacePositions = [];
      const highlightEdgePositions = [];
      const exactHighlightEdgePositions = [];
      const pointPositions = [];
      const pointSeen = new Set();
      const surfaceBodyKeys = new Set();

      function pushPoint(position) {
        const centered = [
          Number(position[0]) - preview.center[0],
          Number(position[1]) - preview.center[1],
          Number(position[2]) - preview.center[2],
        ];
        const marker = centered.map((value) => value.toFixed(6)).join("|");
        if (pointSeen.has(marker)) return;
        pointSeen.add(marker);
        pointPositions.push(...centered);
      }

      for (const face of preview.kernelGeometry?.faces || []) {
        const vertices = Array.isArray(face?.vertices) ? face.vertices : [];
        if (vertices.length < 3) continue;
        const faceBodyKey = face?.bodyRawKey || face?.componentId || null;
        if (faceBodyKey) surfaceBodyKeys.add(faceBodyKey);
        const faceNormal = (face.normal || normalFromVertices(vertices) || [0, 0, 1]).map(Number);
        const highlightTone = faceHighlightTone(face, {
          highlightComponents,
          highlightBodyKeys,
          highlightRawFaces,
          exactHighlightComponents: surfaceExactHighlightComponents,
          exactHighlightBodyKeys: surfaceExactHighlightBodyKeys,
          exactHighlightRawFaces: surfaceExactHighlightRawFaces,
        });
        const baseColor = (face.baseColor || [196, 199, 205]).map((value) => Number(value) / 255);

        vertices.forEach(pushPoint);
        for (let index = 1; index < vertices.length - 1; index++) {
          const triangle = [vertices[0], vertices[index], vertices[index + 1]];
          const triangleNormal = (normalFromTriangle(triangle[0], triangle[1], triangle[2], faceNormal) || faceNormal).map(Number);
          const highlightSurfaceTarget = highlightTone === "exact"
            ? exactHighlightSurfacePositions
            : (highlightTone === "changed" ? highlightSurfacePositions : null);
          for (const vertex of triangle) {
            const centeredVertex = [
              Number(vertex[0]) - preview.center[0],
              Number(vertex[1]) - preview.center[1],
              Number(vertex[2]) - preview.center[2],
            ];
            solidPositions.push(...centeredVertex);
            solidNormals.push(triangleNormal[0], triangleNormal[1], triangleNormal[2]);
            solidColors.push(baseColor[0], baseColor[1], baseColor[2]);
            if (highlightSurfaceTarget) {
              highlightSurfaceTarget.push(...centeredVertex);
            }
          }
        }
      }

      for (const edge of preview.kernelGeometry?.edges || []) {
        const points = Array.isArray(edge.points) ? edge.points : [];
        const edgeBodyKey = edge?.bodyRawKey || edge?.componentId || null;
        const highlightTone = edgeHighlightTone(edge, {
          highlightComponents,
          highlightBodyKeys,
          highlightRawEdges,
          exactHighlightComponents: surfaceExactHighlightComponents,
          exactHighlightBodyKeys: surfaceExactHighlightBodyKeys,
          exactHighlightRawEdges: surfaceExactHighlightRawEdges,
        });
        const targetPositions = edgeBodyKey && !surfaceBodyKeys.has(edgeBodyKey)
          ? fallbackEdgePositions
          : edgePositions;
        for (let index = 1; index < points.length; index++) {
          const start = points[index - 1];
          const end = points[index];
          const centered = [
            Number(start[0]) - preview.center[0],
            Number(start[1]) - preview.center[1],
            Number(start[2]) - preview.center[2],
            Number(end[0]) - preview.center[0],
            Number(end[1]) - preview.center[1],
            Number(end[2]) - preview.center[2]
          ];
          targetPositions.push(
            ...centered
          );
          if (highlightTone === "changed") {
            highlightEdgePositions.push(...centered);
          } else if (highlightTone === "exact") {
            exactHighlightEdgePositions.push(...centered);
          }
          pushPoint(start);
          pushPoint(end);
        }
      }

      for (const point of preview.points || []) {
        pushPoint(point);
      }

      let solidMesh = null;
      let wireframe = null;
      let edgeLines = null;
      let fallbackEdgeLines = null;
      let highlightSurfaceMesh = null;
      let exactHighlightSurfaceMesh = null;
      let highlightSurfaceWireframe = null;
      let exactHighlightSurfaceWireframe = null;
      let highlightEdgeLines = null;
      let exactHighlightEdgeLines = null;
      let pointCloud = null;

      if (solidPositions.length) {
        const solidGeometry = new THREE.BufferGeometry();
        solidGeometry.setAttribute("position", new THREE.Float32BufferAttribute(solidPositions, 3));
        solidGeometry.setAttribute("normal", new THREE.Float32BufferAttribute(solidNormals, 3));
        solidGeometry.setAttribute("color", new THREE.Float32BufferAttribute(solidColors, 3));
        const solidMaterial = new THREE.MeshStandardMaterial({
          vertexColors: true,
          side: THREE.DoubleSide,
          roughness: 0.58,
          metalness: 0.08,
          transparent: false,
          opacity: 1,
          depthWrite: true,
          polygonOffset: true,
          polygonOffsetFactor: 1,
          polygonOffsetUnits: 1,
        });
        solidMesh = new THREE.Mesh(
          solidGeometry,
          solidMaterial
        );
        solidMesh.renderOrder = 0;
        group.add(solidMesh);

        wireframe = new THREE.LineSegments(
          new THREE.WireframeGeometry(solidGeometry),
          new THREE.LineBasicMaterial({
            color: new THREE.Color(rgbCss(palette.edgeColor)),
            transparent: true,
            opacity: 0.22,
          })
        );
        wireframe.renderOrder = 1;
        group.add(wireframe);
      }

      if (highlightSurfacePositions.length) {
        const highlightSurfaceGeometry = new THREE.BufferGeometry();
        highlightSurfaceGeometry.setAttribute("position", new THREE.Float32BufferAttribute(highlightSurfacePositions, 3));
        highlightSurfaceMesh = new THREE.Mesh(
          highlightSurfaceGeometry,
          new THREE.MeshBasicMaterial({
            color: new THREE.Color("#ffbe66"),
            transparent: false,
            side: THREE.DoubleSide,
            depthTest: true,
            depthWrite: false,
            toneMapped: false,
            polygonOffset: true,
            polygonOffsetFactor: -1,
            polygonOffsetUnits: -1,
          })
        );
        highlightSurfaceMesh.renderOrder = 3;
        group.add(highlightSurfaceMesh);
        highlightSurfaceWireframe = new THREE.LineSegments(
          new THREE.WireframeGeometry(highlightSurfaceGeometry),
          new THREE.LineBasicMaterial({
            color: new THREE.Color("#ef6c00"),
            transparent: true,
            opacity: 0.96,
            depthTest: true,
            depthWrite: false,
          })
        );
        highlightSurfaceWireframe.renderOrder = 5;
        group.add(highlightSurfaceWireframe);
      }

      if (exactHighlightSurfacePositions.length) {
        const exactHighlightSurfaceGeometry = new THREE.BufferGeometry();
        exactHighlightSurfaceGeometry.setAttribute("position", new THREE.Float32BufferAttribute(exactHighlightSurfacePositions, 3));
        exactHighlightSurfaceMesh = new THREE.Mesh(
          exactHighlightSurfaceGeometry,
          new THREE.MeshBasicMaterial({
            color: new THREE.Color("#ffbe66"),
            transparent: true,
            opacity: 0.62,
            side: THREE.DoubleSide,
            depthTest: true,
            depthWrite: false,
            toneMapped: false,
            polygonOffset: true,
            polygonOffsetFactor: -1,
            polygonOffsetUnits: -1,
          })
        );
        exactHighlightSurfaceMesh.renderOrder = 4;
        group.add(exactHighlightSurfaceMesh);
        exactHighlightSurfaceWireframe = new THREE.LineSegments(
          new THREE.WireframeGeometry(exactHighlightSurfaceGeometry),
          new THREE.LineBasicMaterial({
            color: new THREE.Color("#1e88e5"),
            transparent: true,
            opacity: 0.9,
            depthTest: true,
            depthWrite: false,
          })
        );
        exactHighlightSurfaceWireframe.renderOrder = 6;
        group.add(exactHighlightSurfaceWireframe);
      }

      if (edgePositions.length) {
        const edgeGeometry = new THREE.BufferGeometry();
        edgeGeometry.setAttribute("position", new THREE.Float32BufferAttribute(edgePositions, 3));
        edgeLines = new THREE.LineSegments(
          edgeGeometry,
          new THREE.LineBasicMaterial({
            color: new THREE.Color(rgbCss(palette.edgeColor)),
            transparent: true,
            opacity: 0.26,
          })
        );
        edgeLines.renderOrder = 1;
        group.add(edgeLines);
      }

      if (fallbackEdgePositions.length) {
        const fallbackEdgeGeometry = new THREE.BufferGeometry();
        fallbackEdgeGeometry.setAttribute("position", new THREE.Float32BufferAttribute(fallbackEdgePositions, 3));
        fallbackEdgeLines = new THREE.LineSegments(
          fallbackEdgeGeometry,
          new THREE.LineBasicMaterial({
            color: new THREE.Color(rgbCss(palette.edgeColor)),
            transparent: true,
            opacity: 0.4,
          })
        );
        fallbackEdgeLines.renderOrder = 1;
        group.add(fallbackEdgeLines);
      }

      if (highlightEdgePositions.length) {
        const highlightEdgeGeometry = new THREE.BufferGeometry();
        highlightEdgeGeometry.setAttribute("position", new THREE.Float32BufferAttribute(highlightEdgePositions, 3));
        highlightEdgeLines = new THREE.LineSegments(
          highlightEdgeGeometry,
          new THREE.LineBasicMaterial({
            color: new THREE.Color("#ef6c00"),
            transparent: true,
            opacity: 0.98,
            depthTest: true,
            depthWrite: false,
          })
        );
        highlightEdgeLines.renderOrder = 5;
        group.add(highlightEdgeLines);
      }

      if (exactHighlightEdgePositions.length) {
        const exactHighlightEdgeGeometry = new THREE.BufferGeometry();
        exactHighlightEdgeGeometry.setAttribute("position", new THREE.Float32BufferAttribute(exactHighlightEdgePositions, 3));
        exactHighlightEdgeLines = new THREE.LineSegments(
          exactHighlightEdgeGeometry,
          new THREE.LineBasicMaterial({
            color: new THREE.Color("#1e88e5"),
            transparent: true,
            opacity: 0.98,
            depthTest: true,
            depthWrite: false,
          })
        );
        exactHighlightEdgeLines.renderOrder = 6;
        group.add(exactHighlightEdgeLines);
      }

      if (pointPositions.length) {
        const pointGeometry = new THREE.BufferGeometry();
        pointGeometry.setAttribute("position", new THREE.Float32BufferAttribute(pointPositions, 3));
        pointCloud = new THREE.Points(
          pointGeometry,
          new THREE.PointsMaterial({
            color: new THREE.Color(rgbCss(palette.pointColor)),
            size: Math.max((preview.span || 0.1) * 0.018, 0.0015),
            sizeAttenuation: true,
          })
        );
        group.add(pointCloud);
      }

      return {
        compareStyle: comparisonStyle,
        group,
        solidMesh,
        wireframe,
        edgeLines,
        fallbackEdgeLines,
        highlightSurfaceMesh,
        exactHighlightSurfaceMesh,
        highlightSurfaceWireframe,
        exactHighlightSurfaceWireframe,
        highlightEdgeLines,
        exactHighlightEdgeLines,
        pointCloud,
      };
    }

    function destroyViewport(viewport) {
      if (!viewport) return;
      clearPreviewViewportContent(viewport);
      viewport.controls?.dispose?.();
      viewport.renderer?.dispose?.();
      setViewportOverlay(viewport, "");
    }

    function destroyCompareViewports() {
      destroyViewport(compareViewports.left);
      destroyViewport(compareViewports.right);
      compareViewports = { left: null, right: null };
    }

    function syncViewportCamera(sourceViewport, targetViewport) {
      if (!sourceViewport?.camera || !targetViewport?.camera || sourceViewport === targetViewport) return;
      targetViewport.syncingFromPeer = true;
      targetViewport.controls.target.copy(sourceViewport.controls.target);
      targetViewport.camera.position.copy(sourceViewport.camera.position);
      targetViewport.camera.up.copy(sourceViewport.camera.up);
      targetViewport.camera.quaternion.copy(sourceViewport.camera.quaternion);
      targetViewport.camera.near = sourceViewport.camera.near;
      targetViewport.camera.far = sourceViewport.camera.far;
      targetViewport.camera.updateProjectionMatrix();
      targetViewport.controls.update();
      renderPreviewViewport(targetViewport);
      targetViewport.syncingFromPeer = false;
    }

    function syncCompareViewports() {
      if (!state.compareSync) return;
      const leftViewport = compareViewports.left;
      const rightViewport = compareViewports.right;
      if (!leftViewport?.currentPreview || !rightViewport?.currentPreview) return;
      syncViewportCamera(leftViewport, rightViewport);
    }

    function ensureWebGlModules() {
      if (!webGlModulesPromise) {
        webGlModulesPromise = import(THREE_MODULE_URL).then((THREE) => ({
          THREE,
        }));
      }
      return webGlModulesPromise;
    }

    function createCadCameraControls(THREE, camera, domElement) {
      class CadCameraControls extends THREE.EventDispatcher {
        constructor() {
          super();
          this.camera = camera;
          this.domElement = domElement;
          this.enabled = true;
          this.target = new THREE.Vector3();
          this.rotateSpeed = 2;
          this.panSpeed = 1;
          this.zoomSpeed = 1;
          this.minDistance = 0.001;
          this.maxDistance = 1e6;
          this._pointerId = null;
          this._mode = null;
          this._lastX = 0;
          this._lastY = 0;

          this._onPointerDown = this._onPointerDown.bind(this);
          this._onPointerMove = this._onPointerMove.bind(this);
          this._onPointerUp = this._onPointerUp.bind(this);
          this._onWheel = this._onWheel.bind(this);

          this.domElement.style.touchAction = "none";
          this.domElement.addEventListener("pointerdown", this._onPointerDown);
          this.domElement.addEventListener("pointermove", this._onPointerMove);
          this.domElement.addEventListener("pointerup", this._onPointerUp);
          this.domElement.addEventListener("pointercancel", this._onPointerUp);
          this.domElement.addEventListener("wheel", this._onWheel, { passive: false });
          this.update();
        }

        dispose() {
          this.domElement.removeEventListener("pointerdown", this._onPointerDown);
          this.domElement.removeEventListener("pointermove", this._onPointerMove);
          this.domElement.removeEventListener("pointerup", this._onPointerUp);
          this.domElement.removeEventListener("pointercancel", this._onPointerUp);
          this.domElement.removeEventListener("wheel", this._onWheel);
        }

        update() {
          this.camera.lookAt(this.target);
          this.camera.updateMatrixWorld();
        }

        _viewportSize() {
          const rect = this.domElement.getBoundingClientRect();
          return {
            width: Math.max(rect.width, 1),
            height: Math.max(rect.height, 1),
          };
        }

        _onPointerDown(event) {
          if (!this.enabled || this._pointerId !== null) return;
          event.preventDefault();
          this._pointerId = event.pointerId;
          this._mode = (event.shiftKey || event.button === 1 || event.button === 2) ? "pan" : "rotate";
          this._lastX = event.clientX;
          this._lastY = event.clientY;
          this.domElement.setPointerCapture?.(event.pointerId);
          this.dispatchEvent({ type: "start" });
        }

        _onPointerMove(event) {
          if (!this.enabled || event.pointerId !== this._pointerId) return;
          event.preventDefault();
          const dx = event.clientX - this._lastX;
          const dy = event.clientY - this._lastY;
          this._lastX = event.clientX;
          this._lastY = event.clientY;

          if (this._mode === "pan") {
            this._pan(dx, dy);
          } else {
            this._rotate(dx, dy);
          }
          this.update();
          this.dispatchEvent({ type: "change" });
        }

        _onPointerUp(event) {
          if (event.pointerId !== this._pointerId) return;
          this.domElement.releasePointerCapture?.(event.pointerId);
          this._pointerId = null;
          this._mode = null;
          this.dispatchEvent({ type: "end" });
        }

        _onWheel(event) {
          if (!this.enabled) return;
          event.preventDefault();
          this._dolly(event.deltaY);
          this.update();
          this.dispatchEvent({ type: "change" });
        }

        _rotate(dx, dy) {
          const { width, height } = this._viewportSize();
          const offset = this.camera.position.clone().sub(this.target);
          if (offset.lengthSq() <= 1e-12) return;

          const yawAngle = (-dx / width) * Math.PI * this.rotateSpeed;
          const pitchAngle = (-dy / height) * Math.PI * this.rotateSpeed;

          const upAxis = this.camera.up.clone().normalize();
          if (upAxis.lengthSq() > 1e-12 && yawAngle) {
            const yawQuat = new THREE.Quaternion().setFromAxisAngle(upAxis, yawAngle);
            offset.applyQuaternion(yawQuat);
          }

          const rightAxis = new THREE.Vector3().crossVectors(this.camera.up, offset).normalize();
          if (rightAxis.lengthSq() > 1e-12 && pitchAngle) {
            const pitchQuat = new THREE.Quaternion().setFromAxisAngle(rightAxis, pitchAngle);
            offset.applyQuaternion(pitchQuat);
            this.camera.up.applyQuaternion(pitchQuat).normalize();
          }

          this.camera.position.copy(this.target).add(offset);
        }

        _pan(dx, dy) {
          const { width, height } = this._viewportSize();
          const offset = this.camera.position.clone().sub(this.target);
          const distance = Math.max(offset.length(), this.minDistance);
          const verticalSpan = 2 * distance * Math.tan(THREE.MathUtils.degToRad(this.camera.fov || 45) / 2);
          const horizontalSpan = verticalSpan * (this.camera.aspect || 1);
          const viewDirection = this.camera.getWorldDirection(new THREE.Vector3());
          const right = new THREE.Vector3().crossVectors(viewDirection, this.camera.up).normalize();
          const up = this.camera.up.clone().normalize();
          const pan = right.multiplyScalar((-dx / width) * horizontalSpan * this.panSpeed)
            .add(up.multiplyScalar((dy / height) * verticalSpan * this.panSpeed));
          this.camera.position.add(pan);
          this.target.add(pan);
        }

        _dolly(deltaY) {
          const offset = this.camera.position.clone().sub(this.target);
          const distance = Math.max(offset.length(), this.minDistance);
          const scale = Math.pow(1.0015, deltaY * this.zoomSpeed);
          const nextDistance = Math.min(this.maxDistance, Math.max(this.minDistance, distance * scale));
          offset.setLength(nextDistance);
          this.camera.position.copy(this.target).add(offset);
        }
      }

      return new CadCameraControls();
    }

    function createWebGlViewport(modules, canvasEl, overlayEl, options = {}) {
      const { THREE } = modules;
      const renderer = new THREE.WebGLRenderer({
        canvas: canvasEl,
        antialias: true,
        alpha: false,
        powerPreference: "high-performance",
      });
      if ("outputColorSpace" in renderer && THREE.SRGBColorSpace) {
        renderer.outputColorSpace = THREE.SRGBColorSpace;
      }
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(45, 1, 0.001, 1000);
      const controls = createCadCameraControls(THREE, camera, canvasEl);

      const hemiLight = new THREE.HemisphereLight(0xffffff, 0x4f5660, 1.18);
      scene.add(hemiLight);

      const keyLight = new THREE.DirectionalLight(0xffffff, 1.25);
      keyLight.position.set(2.2, 3.1, 4.4);
      scene.add(keyLight);

      const fillLight = new THREE.DirectionalLight(0xffffff, 0.48);
      fillLight.position.set(-2.1, -1.8, -3.4);
      scene.add(fillLight);

      const contentGroup = new THREE.Group();
      scene.add(contentGroup);

      const viewport = {
        canvas: canvasEl,
        annotationCanvas: options.annotationCanvas || null,
        overlay: overlayEl,
        label: options.label || "Preview",
        side: options.side || null,
        THREE,
        renderer,
        scene,
        camera,
        controls,
        contentGroup,
        previewObjects: null,
        currentPreview: null,
        currentPreset: "iso",
        currentReportKey: null,
        currentHiddenKey: "",
        currentCompareAnnotation: null,
        syncingFromPeer: false,
      };

      controls.addEventListener("change", () => {
        if (!viewport.syncingFromPeer && viewport.side && state.compareSync) {
          const peer = compareViewports[viewport.side === "left" ? "right" : "left"];
          if (peer?.currentPreview) {
            syncViewportCamera(viewport, peer);
          }
        }
        renderPreviewViewport(viewport);
      });

      canvasEl.style.cursor = "grab";
      canvasEl.addEventListener("contextmenu", (event) => event.preventDefault());
      controls.addEventListener("start", () => {
        canvasEl.style.cursor = "grabbing";
      });
      controls.addEventListener("end", () => {
        canvasEl.style.cursor = "grab";
      });
      canvasEl.addEventListener("dblclick", () => {
        fitPreviewViewport(viewport, "fit");
        if (viewport.side && state.compareSync) {
          const peer = compareViewports[viewport.side === "left" ? "right" : "left"];
          if (peer?.currentPreview) {
            syncViewportCamera(viewport, peer);
          }
        }
        setStatus(`${viewport.label} fit to view.`);
      });

      setPreviewViewportBackground(viewport);
      resizePreviewViewport(viewport);
      return viewport;
    }

    async function ensurePreviewViewport() {
      if (previewViewport) return previewViewport;
      setPreviewOverlay("Loading WebGL preview...");
      const modules = await ensureWebGlModules();
      previewViewport = createWebGlViewport(modules, previewCanvas, previewOverlay, {
        label: "Preview",
      });
      return previewViewport;
    }

    async function ensureCompareViewport(side) {
      const canvasEl = byId(`compare-${side}-canvas`);
      if (!canvasEl) return null;
      const annotationCanvasEl = byId(`compare-${side}-annotation-canvas`);
      const overlayEl = byId(`compare-${side}-overlay`);
      const existing = compareViewports[side];
      if (existing?.canvas === canvasEl && existing?.annotationCanvas === annotationCanvasEl) return existing;
      destroyViewport(existing);
      const modules = await ensureWebGlModules();
      const viewport = createWebGlViewport(modules, canvasEl, overlayEl, {
        annotationCanvas: annotationCanvasEl,
        label: `${capitalizeLabel(side)} comparison preview`,
        side,
      });
      compareViewports = {
        ...compareViewports,
        [side]: viewport,
      };
      return viewport;
    }

    function rebuildPreviewViewport(viewport, report, options = {}) {
      const resolvedReport = report || activeReport();
      clearPreviewViewportContent(viewport);
      if (!resolvedReport) {
        setViewportOverlay(viewport, "Load a STEP, STL, or JSON file to preview it here.");
        renderPreviewViewport(viewport);
        return;
      }

      const preview = getPreviewData(resolvedReport);
      if (!preview) {
        const names = resolvedReport?.decoded_names?.length ? `\nNamed entities: ${resolvedReport.decoded_names.join(", ")}` : "";
        setViewportOverlay(viewport, `No previewable solid geometry was decoded from this file.${names}`);
        renderPreviewViewport(viewport);
        return;
      }

      const objects = buildPreviewViewportObjects(viewport, preview, {
        compareStyle: Boolean(options.compareAnnotation),
        highlightComponents: options.highlightComponents || [],
        highlightBodyKeys: options.highlightBodyKeys || [],
        highlightRawFaces: options.highlightRawFaces || [],
        highlightRawEdges: options.highlightRawEdges || [],
        exactHighlightComponents: options.exactHighlightComponents || [],
        exactHighlightBodyKeys: options.exactHighlightBodyKeys || [],
        exactHighlightRawFaces: options.exactHighlightRawFaces || [],
        exactHighlightRawEdges: options.exactHighlightRawEdges || [],
      });
      viewport.contentGroup.add(objects.group);
      viewport.previewObjects = objects;
      viewport.currentPreview = preview;
      viewport.currentReportKey = reportKey(resolvedReport);
      viewport.currentHiddenKey = JSON.stringify(hiddenPreviewBodiesForReport(resolvedReport));
      viewport.currentCompareAnnotation = options.compareAnnotation || null;
      setViewportOverlay(viewport, "");
      applyPreviewViewportMode(viewport, options.mode || state.viewer.mode);
      if (options.fitView) {
        fitPreviewViewport(viewport, viewport.currentPreset || "iso");
      }
      renderPreviewViewport(viewport);
    }

    function drawPreviewWebGl() {
      ensurePreviewViewport()
        .then((viewport) => {
          const report = activeReport();
          const nextReportKey = report ? reportKey(report) : null;
          const nextHiddenKey = report ? JSON.stringify(hiddenPreviewBodiesForReport(report)) : "";
          const fitView = nextReportKey !== viewport.currentReportKey;
          setPreviewViewportBackground(viewport);
          if (fitView || nextHiddenKey !== viewport.currentHiddenKey || !viewport.previewObjects) {
            rebuildPreviewViewport(viewport, report, { fitView, mode: state.viewer.mode });
            return;
          }
          applyPreviewViewportMode(viewport, state.viewer.mode);
          setViewportOverlay(viewport, "");
          renderPreviewViewport(viewport);
        })
        .catch((error) => {
          setPreviewOverlay(`WebGL preview failed: ${error.message}`, "error");
          setStatus(`Preview error: ${error.message}`);
        });
    }

    function applyPreviewPreset(preset) {
      ensurePreviewViewport()
        .then((viewport) => {
          if (!viewport) return;
          if (preset !== "fit") {
            viewport.currentPreset = preset;
          }
          fitPreviewViewport(viewport, preset);
        })
        .catch((error) => {
          setPreviewOverlay(`WebGL preview failed: ${error.message}`, "error");
        });
    }

    function applyComparePreset(preset) {
      Promise.all([
        ensureCompareViewport("left"),
        ensureCompareViewport("right"),
      ])
        .then((viewports) => {
          viewports.filter(Boolean).forEach((viewport) => {
            if (preset !== "fit") {
              viewport.currentPreset = preset;
            }
            fitPreviewViewport(viewport, preset);
          });
          syncCompareViewports();
        })
        .catch((error) => {
          setStatus(`Comparison preview error: ${error.message}`);
        });
    }

    function drawViewportHud(ctx2d, canvasEl, report, preview, viewer, options = {}) {
      const theme = currentViewportTheme();
      const lines = [
        report?.file_name || "Preview",
        previewSourceLabel(report),
        `${capitalizeLabel(options.mode || viewer.mode || "solid")} view`,
      ];
      if (preview?.bounds?.size) {
        lines.push(`Size ${preview.bounds.size.map((value) => formatInches(value)).join(" x ")}`);
      }
      if (options.highlightComponents?.length) {
        lines.push(`${options.highlightComponents.length} feature${options.highlightComponents.length === 1 ? "" : "s"} changed`);
      }

      ctx2d.save();
      ctx2d.font = "600 12px 'Segoe UI'";
      ctx2d.textBaseline = "top";
      const width = Math.max(...lines.map((line) => ctx2d.measureText(line).width)) + 24;
      const height = lines.length * 16 + 16;
      ctx2d.fillStyle = theme.hudFill;
      ctx2d.strokeStyle = theme.hudStroke;
      ctx2d.lineWidth = 1;
      ctx2d.beginPath();
      ctx2d.roundRect(16, 16, width, height, 12);
      ctx2d.fill();
      ctx2d.stroke();
      ctx2d.fillStyle = theme.hudText;
      lines.forEach((line, index) => {
        ctx2d.fillText(line, 28, 24 + index * 16);
      });
      ctx2d.restore();
    }

    function axisVector(axis) {
      if (axis === "x") return [1, 0, 0];
      if (axis === "y") return [0, 1, 0];
      return [0, 0, 1];
    }

    function orthogonalBasis(axis) {
      if (axis === "x") return [[0, 1, 0], [0, 0, 1]];
      if (axis === "y") return [[1, 0, 0], [0, 0, 1]];
      return [[1, 0, 0], [0, 1, 0]];
    }

    function addVec(a, b) {
      return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
    }

    function scaleVec(v, factor) {
      return [v[0] * factor, v[1] * factor, v[2] * factor];
    }

    function circlePoints(center, axis, radius, segmentCount, offsetAlongAxis = 0) {
      const axisVec = axisVector(axis);
      const [u, v] = orthogonalBasis(axis);
      const shiftedCenter = addVec(center, scaleVec(axisVec, offsetAlongAxis));
      const points = [];
      for (let i = 0; i < segmentCount; i++) {
        const angle = (i / segmentCount) * Math.PI * 2;
        const radial = addVec(scaleVec(u, Math.cos(angle) * radius), scaleVec(v, Math.sin(angle) * radius));
        points.push(addVec(shiftedCenter, radial));
      }
      return points;
    }

    function pointExtents(points) {
      if (!Array.isArray(points) || !points.length) return null;

      const first = points[0].map(Number);
      const min = [...first];
      const max = [...first];

      for (const point of points.slice(1)) {
        for (let index = 0; index < 3; index++) {
          const value = Number(point[index]);
          if (value < min[index]) min[index] = value;
          if (value > max[index]) max[index] = value;
        }
      }

      return { min, max };
    }

    function boundsFromPoints(points) {
      const extents = pointExtents(points);
      if (!extents) return null;
      const { min, max } = extents;
      return {
        min,
        max,
        size: [max[0] - min[0], max[1] - min[1], max[2] - min[2]]
      };
    }

    function previewGeometryFromKernelBody(body, context = {}) {
      if (!body) return { faces: [], points: [], edges: [] };

      const bodyKey = previewBodyKey(body, context.bodyIndex || 0);
      if (context.hiddenBodyKeys?.has(bodyKey)) {
        return { faces: [], points: [], edges: [] };
      }

      const bodyDisplay = body?.metadata?.display_properties || null;
      const componentDisplay = body?.metadata?.component_context?.display_properties || null;
      if (isBlankedFromDisplay(bodyDisplay) || isBlankedFromDisplay(componentDisplay)) {
        return { faces: [], points: [], edges: [] };
      }

      if (body.metadata?.primitive === "compound" && Array.isArray(body.metadata?.components)) {
        return body.metadata.components
          .map((component, componentIndex) =>
            previewGeometryFromKernelBody(component, {
              bodyIndex: context.bodyIndex || 0,
              componentIndex,
            })
          )
          .reduce(
            (combined, geometry) => ({
              faces: combined.faces.concat(geometry.faces || []),
              points: combined.points.concat(geometry.points || []),
              edges: combined.edges.concat(geometry.edges || []),
            }),
            { faces: [], points: [], edges: [] }
          );
      }

      const identity = componentIdentity(
        body,
        context.bodyIndex || 0,
        context.componentIndex || 0
      );
      const componentId = identity.id;
      const bodyRawKey = previewBodyRawKey(body, context.bodyIndex || 0);
      const previewBodyId = previewBodyIndex(body, context.bodyIndex || 0);
      const componentName = componentLabel(body, identity, 1);
      const baseColor = displayColorFromProperties(
        bodyDisplay,
        displayColorFromProperties(
          componentDisplay,
          inferredComponentColor(body, componentName, identity)
        )
      );
      const baseOpacity = displayOpacityFromProperties(
        bodyDisplay,
        displayOpacityFromProperties(componentDisplay, 1)
      );

      if (body.faces?.some((face) => face.vertices?.length)) {
        const faces = body.faces
          .filter((face) => face.vertices?.length && !isBlankedFromDisplay(face.metadata?.display_properties))
          .map((face, faceIndex) => ({
            name: face.name,
            axis: face.metadata?.axis || null,
            side: inferFaceSide(face),
            normal: face.normal || null,
            vertices: face.vertices.map((vertex) => vertex.map(Number)),
            componentId,
            bodyRawKey,
            rawFaceKey: rawIdentityKey(
              face.metadata?.object_identity,
              `body:${Number(face.metadata?.source_body_index || previewBodyId)}:face:${Number(face.metadata?.source_face_index || faceIndex + 1)}`
            ),
            componentName,
            // Face-level NX color indices often vary across analytic/trim patches.
            // Keep the preview visually coherent by preferring the body/component color.
            baseColor,
            opacity: displayOpacityFromProperties(face.metadata?.display_properties, baseOpacity),
          }));
        const edges = (body.edges || [])
          .filter((edge) => edge.points?.length >= 2 && !isBlankedFromDisplay(edge.display_properties))
          .map((edge, edgeIndex) => ({
            kind: edge.kind || "edge",
            axis: edge.axis || null,
            points: edge.points.map((point) => point.map(Number)),
            componentId,
            bodyRawKey,
            rawEdgeKey: rawIdentityKey(
              edge.object_identity,
              `body:${previewBodyId}:edge:${Number(edge.source_edge_index || edgeIndex + 1)}`
            ),
            componentName,
            strokeColor: displayColorFromProperties(edge.display_properties, baseColor),
            opacity: displayOpacityFromProperties(edge.display_properties, 1),
          }));
        const points = faces.flatMap((face) => face.vertices);
        return { faces, points, edges };
      }

      if (body.metadata?.primitive === "cylinder") {
        const axis = body.metadata.axis;
        const radius = Number(body.metadata.radius);
        const height = Number(body.metadata.height);
        const center = body.metadata.center.map(Number);
        const segmentCount = 28;
        const axisVec = axisVector(axis);
        const startCenter = addVec(center, scaleVec(axisVec, -height / 2));
        const endCenter = addVec(center, scaleVec(axisVec, height / 2));
        const startRing = circlePoints(startCenter, axis, radius, segmentCount, 0);
        const endRing = circlePoints(endCenter, axis, radius, segmentCount, 0);
        const faces = [];

        for (let i = 0; i < segmentCount; i++) {
          const next = (i + 1) % segmentCount;
          faces.push({
            name: `Cylinder side ${i}`,
            axis,
            normal: null,
            vertices: [startRing[i], startRing[next], endRing[next], endRing[i]],
            componentId,
            bodyRawKey,
            componentName,
            baseColor,
            opacity: baseOpacity,
          });
        }

        faces.push({
          name: "Start cap",
          axis,
          side: "min",
          normal: scaleVec(axisVec, -1),
          vertices: [...startRing].reverse(),
          componentId,
          bodyRawKey,
          componentName,
          baseColor,
          opacity: baseOpacity,
        });
        faces.push({
          name: "End cap",
          axis,
          side: "max",
          normal: axisVec,
          vertices: endRing,
          componentId,
          bodyRawKey,
          componentName,
          baseColor,
          opacity: baseOpacity,
        });

        return {
          faces,
          points: [...startRing, ...endRing],
          edges: []
        };
      }

      if (body.metadata?.primitive === "sphere") {
        const center = body.metadata.center.map(Number);
        const radius = Number(body.metadata.radius);
        const latSegments = 10;
        const lonSegments = 20;
        const faces = [];
        const points = [];

        function spherePoint(phi, theta) {
          return [
            center[0] + radius * Math.sin(phi) * Math.cos(theta),
            center[1] + radius * Math.sin(phi) * Math.sin(theta),
            center[2] + radius * Math.cos(phi)
          ];
        }

        for (let lat = 0; lat < latSegments; lat++) {
          const phi1 = (lat / latSegments) * Math.PI;
          const phi2 = ((lat + 1) / latSegments) * Math.PI;
          for (let lon = 0; lon < lonSegments; lon++) {
            const theta1 = (lon / lonSegments) * Math.PI * 2;
            const theta2 = ((lon + 1) / lonSegments) * Math.PI * 2;
            const quad = [
              spherePoint(phi1, theta1),
              spherePoint(phi1, theta2),
              spherePoint(phi2, theta2),
              spherePoint(phi2, theta1)
            ];
            faces.push({
              name: `Sphere patch ${lat}-${lon}`,
              axis: null,
              normal: null,
              vertices: quad,
              componentId,
              bodyRawKey,
              componentName,
              baseColor,
              opacity: baseOpacity,
            });
            points.push(...quad);
          }
        }

        return { faces, points, edges: [] };
      }

      if (body.metadata?.primitive === "arc_or_circle") {
        const edge = body.edges?.[0];
        if (!edge) return { faces: [], points: [], edges: [] };
        const center = edge.center.map(Number);
        const radius = Number(edge.radius);
        const normal = edge.normal?.map(Number) || [0, 0, 1];
        const segmentCount = 48;
        const axis = Math.abs(normal[2]) >= Math.abs(normal[0]) && Math.abs(normal[2]) >= Math.abs(normal[1])
          ? "z"
          : (Math.abs(normal[1]) > Math.abs(normal[0]) ? "y" : "x");
        const points = circlePoints(center, axis, radius, segmentCount, 0);
        const edges = [];
        for (let i = 0; i < points.length; i++) {
          edges.push({
            kind: "arc_segment",
            axis,
            points: [points[i], points[(i + 1) % points.length]],
            componentId,
            bodyRawKey,
            componentName,
            strokeColor: baseColor,
            opacity: 1,
          });
        }
        return { faces: [], points, edges };
      }

      if (body.metadata?.primitive === "wireframe" || body.edges?.some((edge) => edge.points?.length >= 2)) {
        const edges = (body.edges || [])
          .filter((edge) => edge.points?.length >= 2 && !isBlankedFromDisplay(edge.display_properties))
          .map((edge, edgeIndex) => ({
            kind: edge.kind || "edge",
            axis: edge.axis || null,
            points: edge.points.map((point) => point.map(Number)),
            componentId,
            bodyRawKey,
            rawEdgeKey: rawIdentityKey(
              edge.object_identity,
              `body:${previewBodyId}:edge:${Number(edge.source_edge_index || edgeIndex + 1)}`
            ),
            componentName,
            strokeColor: displayColorFromProperties(edge.display_properties, baseColor),
            opacity: displayOpacityFromProperties(edge.display_properties, 1),
          }));
        const points = [
          ...(body.vertices || []).map((point) => point.map(Number)),
          ...edges.flatMap((edge) => edge.points),
        ];
        return { faces: [], points, edges };
      }

      return { faces: [], points: [], edges: [] };
    }

    function simplifyFacesForPreview(faces) {
      if (!Array.isArray(faces) || !faces.length) return [];

      const preserved = [];
      const reducible = [];
      for (const face of faces) {
        const kind = String(face?.surface?.kind || face?.metadata?.kind || "").toLowerCase();
        const isTriangleMesh = kind === "occ_triangle_mesh" || kind === "facet_triangle";
        if (isTriangleMesh && Array.isArray(face.vertices) && face.vertices.length === 3) {
          reducible.push(face);
        } else {
          preserved.push(face);
        }
      }

      const totalLimit = Math.max(400, MAX_RENDER_FACES - preserved.length);
      const occLimit = Math.max(300, Math.min(MAX_RENDER_OCC_FACES, totalLimit));
      if (reducible.length <= occLimit && (preserved.length + reducible.length) <= MAX_RENDER_FACES) {
        return faces;
      }

      if (!reducible.length) {
        return preserved.slice(0, MAX_RENDER_FACES);
      }

      const target = Math.max(1, Math.min(occLimit, MAX_RENDER_FACES - preserved.length));
      const sampleGroup = (group, count) => {
        if (!Array.isArray(group) || !group.length || count <= 0) return [];
        if (count >= group.length) return [...group];
        const result = [];
        const step = group.length / count;
        for (let index = 0; index < count; index++) {
          const face = group[Math.min(group.length - 1, Math.floor(index * step))];
          if (face) result.push(face);
        }
        return result;
      };

      const reducibleGroups = new Map();
      reducible.forEach((face, index) => {
        const key = face?.bodyRawKey || face?.componentId || face?.rawFaceKey || `reducible-${index}`;
        if (!reducibleGroups.has(key)) reducibleGroups.set(key, []);
        reducibleGroups.get(key).push(face);
      });

      const groups = [...reducibleGroups.values()].filter((group) => group.length);
      if (!groups.length) {
        return preserved.slice(0, MAX_RENDER_FACES);
      }

      const guaranteedPerGroup = Math.min(groups.length, target);
      const allocations = groups.map(() => 0);
      let remaining = target;
      for (let index = 0; index < guaranteedPerGroup; index++) {
        allocations[index] = 1;
        remaining -= 1;
      }

      if (remaining > 0) {
        const extraCapacities = groups.map((group, index) => Math.max(0, group.length - allocations[index]));
        const extraTotal = extraCapacities.reduce((sum, value) => sum + value, 0);
        if (extraTotal > 0) {
          const fractional = [];
          groups.forEach((group, index) => {
            const capacity = extraCapacities[index];
            if (capacity <= 0) {
              fractional.push({ index, remainder: 0 });
              return;
            }
            const exact = (capacity / extraTotal) * remaining;
            const add = Math.min(capacity, Math.floor(exact));
            allocations[index] += add;
            remaining -= add;
            fractional.push({ index, remainder: exact - add });
          });

          if (remaining > 0) {
            fractional
              .sort((left, right) => right.remainder - left.remainder)
              .forEach(({ index }) => {
                if (remaining <= 0) return;
                if (allocations[index] >= groups[index].length) return;
                allocations[index] += 1;
                remaining -= 1;
              });
          }
        }
      }

      if (remaining > 0) {
        for (let index = 0; index < groups.length && remaining > 0; index++) {
          while (remaining > 0 && allocations[index] < groups[index].length) {
            allocations[index] += 1;
            remaining -= 1;
          }
        }
      }

      const sampled = groups.flatMap((group, index) => sampleGroup(group, allocations[index] || 0));
      return preserved.concat(sampled);
    }

    function simplifyKernelGeometryForPreview(kernelGeometry, options = {}) {
      const originalFaces = kernelGeometry?.faces || [];
      if (options?.preserveTriangleMeshFaces || options?.preserveAllFaces) {
        return {
          ...kernelGeometry,
          faces: originalFaces,
          renderStats: {
            originalFaceCount: originalFaces.length,
            renderedFaceCount: originalFaces.length,
            faceReductionApplied: false,
          }
        };
      }
      const faces = simplifyFacesForPreview(originalFaces);
      return {
        ...kernelGeometry,
        faces,
        renderStats: {
          originalFaceCount: originalFaces.length,
          renderedFaceCount: faces.length,
          faceReductionApplied: faces.length < originalFaces.length,
        }
      };
    }

    function getPreviewData(report) {
      if (!report) return null;
      const bodies = previewBodies(report);
      const geometry = previewGeometryHints(report);
      const points = (!bodies.length ? (geometry.preview_points || []) : []).map((point) => point.map(Number));
      const hiddenBodyKeys = new Set(hiddenPreviewBodiesForReport(report));
      const kernelGeometry = bodies
        .map((body, bodyIndex) => previewGeometryFromKernelBody(body, { bodyIndex, hiddenBodyKeys }))
        .reduce(
          (combined, geometry) => ({
            faces: combined.faces.concat(geometry.faces || []),
            points: combined.points.concat(geometry.points || []),
            edges: combined.edges.concat(geometry.edges || []),
          }),
          { faces: [], points: [], edges: [] }
        );
      const strategy = previewMetadata(report).strategy;
      const preserveTriangleMeshFaces = ["opencascade_occ", "browser_occt_import_js"].includes(report?.step_import?.backend || "");
      const preserveAllFaces = (
        (strategy === "json_reconstruction_preview" || strategy === "fused_json_step_preview")
        && (kernelGeometry.faces || []).length <= 12000
      );
      const simplifiedKernelGeometry = simplifyKernelGeometryForPreview(kernelGeometry, {
        preserveTriangleMeshFaces,
        preserveAllFaces,
      });
      const cloud = [...points, ...kernelGeometry.points];
      const kernelBody = bodies.length === 1 ? bodies[0] : null;
      let bounds = geometry?.bounds
        ? {
            min: geometry.bounds.min.map(Number),
            max: geometry.bounds.max.map(Number),
            size: geometry.bounds.size.map(Number)
          }
        : null;

      if (!bounds && kernelGeometry.points.length) {
        bounds = boundsFromPoints(kernelGeometry.points);
      }

      if (bounds) {
        const min = bounds.min;
        const max = bounds.max;
        cloud.push(
          [min[0], min[1], min[2]],
          [max[0], min[1], min[2]],
          [max[0], max[1], min[2]],
          [min[0], max[1], min[2]],
          [min[0], min[1], max[2]],
          [max[0], min[1], max[2]],
          [max[0], max[1], max[2]],
          [min[0], max[1], max[2]]
        );
      }

      if (!cloud.length) return null;

      const extents = pointExtents(cloud);
      if (!extents) return null;
      const { min, max } = extents;
      const center = [
        (min[0] + max[0]) / 2,
        (min[1] + max[1]) / 2,
        (min[2] + max[2]) / 2
      ];
      const span = Math.max(
        max[0] - min[0],
        max[1] - min[1],
        max[2] - min[2],
        1e-6
      );

      return {
        points,
        bounds,
        kernelBody,
        kernelGeometry: simplifiedKernelGeometry,
        renderStats: simplifiedKernelGeometry.renderStats,
        strategy,
        center,
        span
      };
    }

    function shadeRgbForNormal(normal, theme, baseColor = null) {
      const light = [0.38, -0.28, 0.88];
      const len = Math.hypot(...light) || 1;
      const unitLight = light.map((value) => value / len);
      const nLen = Math.hypot(normal[0], normal[1], normal[2]) || 1;
      const unitNormal = normal.map((value) => value / nLen);
      const diffuse = Math.max(0, unitNormal[0] * unitLight[0] + unitNormal[1] * unitLight[1] + unitNormal[2] * unitLight[2]);
      const ambient = theme?.surfaceAmbient ?? 0.38;
      const mix = Math.min(1, ambient + diffuse * (1 - ambient));
      const shadow = Array.isArray(baseColor)
        ? baseColor.map((value) => Math.max(0, Math.round(value * 0.48)))
        : (theme?.surfaceShadow ?? [160, 168, 180]);
      const highlight = Array.isArray(baseColor)
        ? baseColor.map((value) => Math.min(255, Math.round(value * 1.06 + 22)))
        : (theme?.surfaceHighlight ?? [232, 236, 242]);
      return shadow.map((value, index) => {
        return Math.round(value + (highlight[index] - value) * mix);
      });
    }

    function shadeForNormal(normal, theme) {
      return rgbCss(shadeRgbForNormal(normal, theme));
    }

    function edgeFunction(ax, ay, bx, by, px, py) {
      return (px - ax) * (by - ay) - (py - ay) * (bx - ax);
    }

    function triangulateProjectedFace(face) {
      if (!face?.projectedVertices || face.projectedVertices.length < 3) return [];
      const triangles = [];
      for (let index = 1; index < face.projectedVertices.length - 1; index++) {
        triangles.push({
          projected: [
            face.projectedVertices[0],
            face.projectedVertices[index],
            face.projectedVertices[index + 1],
          ],
          rotatedNormal: normalFromTriangle(
            face.rotatedVertices?.[0],
            face.rotatedVertices?.[index],
            face.rotatedVertices?.[index + 1],
            face.rotatedNormal || [0, 0, -1]
          ) || face.rotatedNormal || [0, 0, -1],
        });
      }
      return triangles;
    }

    function rasterizeSolidFaces(ctx2d, canvasEl, faces, theme) {
      if (!faces.length) return;
      const width = canvasEl.width;
      const height = canvasEl.height;
      const imageData = ctx2d.getImageData(0, 0, width, height);
      const pixels = imageData.data;
      const depthBuffer = new Float32Array(width * height);
      depthBuffer.fill(-Infinity);

      for (const face of faces) {
        const fillAlpha = Math.max(0.18, Math.min(1, Number(face.opacity ?? 1)));

        for (const triangleInfo of triangulateProjectedFace(face)) {
          const [a, b, c] = triangleInfo.projected;
          const fillRgb = (face.exactHighlighted || face.highlighted)
            ? [255, 191, 102]
            : shadeRgbForNormal(triangleInfo.rotatedNormal || face.rotatedNormal || [0, 0, -1], theme, face.baseColor);
          const area = edgeFunction(a[0], a[1], b[0], b[1], c[0], c[1]);
          if (Math.abs(area) <= 1e-5) continue;

          const minX = Math.max(0, Math.floor(Math.min(a[0], b[0], c[0])));
          const maxX = Math.min(width - 1, Math.ceil(Math.max(a[0], b[0], c[0])));
          const minY = Math.max(0, Math.floor(Math.min(a[1], b[1], c[1])));
          const maxY = Math.min(height - 1, Math.ceil(Math.max(a[1], b[1], c[1])));

          for (let y = minY; y <= maxY; y++) {
            for (let x = minX; x <= maxX; x++) {
              const px = x + 0.5;
              const py = y + 0.5;
              const w0 = edgeFunction(b[0], b[1], c[0], c[1], px, py);
              const w1 = edgeFunction(c[0], c[1], a[0], a[1], px, py);
              const w2 = edgeFunction(a[0], a[1], b[0], b[1], px, py);

              const inside = area < 0
                ? (w0 <= 0 && w1 <= 0 && w2 <= 0)
                : (w0 >= 0 && w1 >= 0 && w2 >= 0);
              if (!inside) continue;

              const depth = (w0 * a[2] + w1 * b[2] + w2 * c[2]) / area;
              const offset = (y * width + x);
              if (depth <= depthBuffer[offset]) continue;
              depthBuffer[offset] = depth;

              const pixelOffset = offset * 4;
              const existingAlpha = pixels[pixelOffset + 3] / 255;
              const blendedAlpha = fillAlpha + existingAlpha * (1 - fillAlpha);
              const existingWeight = blendedAlpha > 0 ? (existingAlpha * (1 - fillAlpha)) / blendedAlpha : 0;
              const fillWeight = blendedAlpha > 0 ? fillAlpha / blendedAlpha : 0;
              pixels[pixelOffset] = Math.round((pixels[pixelOffset] * existingWeight) + (fillRgb[0] * fillWeight));
              pixels[pixelOffset + 1] = Math.round((pixels[pixelOffset + 1] * existingWeight) + (fillRgb[1] * fillWeight));
              pixels[pixelOffset + 2] = Math.round((pixels[pixelOffset + 2] * existingWeight) + (fillRgb[2] * fillWeight));
              pixels[pixelOffset + 3] = Math.round(blendedAlpha * 255);
            }
          }
        }
      }

      ctx2d.putImageData(imageData, 0, 0);
    }

    function paintSolidFacesNative(ctx2d, faces, theme) {
      for (const face of faces) {
        if (!face.projectedVertices?.length) continue;
        const fillRgb = (face.exactHighlighted || face.highlighted)
          ? [255, 191, 102]
          : shadeRgbForNormal(face.rotatedNormal || [0, 0, -1], theme, face.baseColor);
        ctx2d.fillStyle = rgbCss(fillRgb);
        ctx2d.globalAlpha = Math.max(0.2, Math.min(1, Number(face.opacity ?? 1)));
        ctx2d.beginPath();
        ctx2d.moveTo(face.projectedVertices[0][0], face.projectedVertices[0][1]);
        for (const vertex of face.projectedVertices.slice(1)) {
          ctx2d.lineTo(vertex[0], vertex[1]);
        }
        ctx2d.closePath();
        ctx2d.fill();
      }
      ctx2d.globalAlpha = 1;
    }

    function drawModelPreview(canvasEl, report, viewer, options = {}) {
      const ctx2d = canvasEl.getContext("2d");
      const theme = currentViewportTheme();
      const highlightAxes = options.highlightAxes || [];
      const highlightFaces = options.highlightFaces || [];
      const highlightComponents = options.highlightComponents || [];
      const highlightBodyKeys = options.highlightBodyKeys || [];
      const highlightRawFaces = options.highlightRawFaces || [];
      const highlightRawEdges = options.highlightRawEdges || [];
      const exactHighlightComponents = options.exactHighlightComponents || [];
      const exactHighlightBodyKeys = options.exactHighlightBodyKeys || [];
      const exactHighlightRawFaces = options.exactHighlightRawFaces || [];
      const exactHighlightRawEdges = options.exactHighlightRawEdges || [];
      const compareAnnotation = options.compareAnnotation || null;
      const forceMode = options.mode || viewer.mode || "solid";

      resizeCanvasElement(canvasEl);
      ctx2d.clearRect(0, 0, canvasEl.width, canvasEl.height);
      drawViewportBackground(ctx2d, canvasEl);

      const preview = getPreviewData(report);
      if (!preview) {
        ctx2d.fillStyle = theme.emptyText;
        ctx2d.font = "14px 'Segoe UI'";
        ctx2d.fillText("No previewable solid geometry was decoded from this file.", 16, 24);
        if (report?.decoded_names?.length) {
          ctx2d.fillText(`Named entities: ${report.decoded_names.join(", ")}`, 16, 48);
        }
        ctx2d.fillText("This viewport renders the app's imported/custom-kernel geometry from the available B-rep data.", 16, 72);
        drawCornerTriad(canvasEl, ctx2d, viewer);
        return;
      }

      const scale = (Math.min(canvasEl.width, canvasEl.height) * 0.32 / preview.span) * viewer.zoom;
      const viewerDistance = previewViewerDistance(preview);
      const centeredPoints = preview.points.map((point) => [
        point[0] - preview.center[0],
        point[1] - preview.center[1],
        point[2] - preview.center[2]
      ]);

      const projected = centeredPoints.map((point) =>
        projectPoint(rotatePoint(point, viewer), scale, canvasEl, viewer, viewerDistance)
      );

      const projectedFaces = (preview.kernelGeometry?.faces || [])
        .filter((face) => face?.vertices?.length >= 3)
        .map((face) => {
        const rotatedVertices = face.vertices.map((point) => {
          return rotatePoint([
            point[0] - preview.center[0],
            point[1] - preview.center[1],
            point[2] - preview.center[2]
          ], viewer);
        });
        const projectedVertices = rotatedVertices.map((point) => projectPoint(point, scale, canvasEl, viewer, viewerDistance));
        const sourceNormal = face.normal ? face.normal.map(Number) : normalFromVertices(face.vertices.map((vertex) => vertex.map(Number)));
        const rotatedNormal = sourceNormal ? rotatePoint(sourceNormal, viewer) : normalFromVertices(rotatedVertices);
        const averageDepth = rotatedVertices.reduce((sum, point) => sum + point[2], 0) / rotatedVertices.length;
        const minDepth = rotatedVertices.reduce((value, point) => Math.min(value, point[2]), Infinity);
        const maxDepth = rotatedVertices.reduce((value, point) => Math.max(value, point[2]), -Infinity);
        let inferredAxis = face.axis;
        if (!inferredAxis && face.vertices.length >= 2) {
          const xs = face.vertices.map((vertex) => vertex[0]);
          const ys = face.vertices.map((vertex) => vertex[1]);
          const zs = face.vertices.map((vertex) => vertex[2]);
          if (Math.max(...xs) - Math.min(...xs) < 1e-6) inferredAxis = "x";
          else if (Math.max(...ys) - Math.min(...ys) < 1e-6) inferredAxis = "y";
          else if (Math.max(...zs) - Math.min(...zs) < 1e-6) inferredAxis = "z";
        }
        return {
          ...face,
          axis: inferredAxis,
          side: face.side || inferFaceSide(face),
          rotatedVertices,
          rotatedNormal,
          averageDepth,
          minDepth,
          maxDepth,
          exactHighlighted: faceHighlightTone(face, {
            exactHighlightComponents,
            exactHighlightBodyKeys,
            exactHighlightRawFaces,
          }) === "exact",
          highlighted: faceHighlightTone(face, {
            highlightFaces,
            highlightComponents,
            highlightBodyKeys,
            highlightRawFaces,
            exactHighlightComponents,
            exactHighlightBodyKeys,
            exactHighlightRawFaces,
          }) === "changed"
            || (!highlightFaces.length && !highlightComponents.length && highlightAxes.includes(inferredAxis)),
          projectedVertices
        };
      // Use a more stable back-to-front ordering for trimmed patches by
      // preferring the farthest depth extent first, then average depth.
      }).sort((a, b) => {
        if (Math.abs(b.maxDepth - a.maxDepth) > 1e-6) return b.maxDepth - a.maxDepth;
        if (Math.abs(b.averageDepth - a.averageDepth) > 1e-6) return b.averageDepth - a.averageDepth;
        return b.minDepth - a.minDepth;
      });

      if (forceMode === "solid" && projectedFaces.length) {
        const useNativeFill = (
          projectedFaces.length > 1400
          && preview.strategy !== "json_reconstruction_preview"
          && preview.strategy !== "fused_json_step_preview"
        );
        if (useNativeFill) {
          paintSolidFacesNative(ctx2d, projectedFaces, theme);
        } else {
          rasterizeSolidFaces(ctx2d, canvasEl, projectedFaces, theme);
        }
        for (const face of projectedFaces) {
          ctx2d.beginPath();
          ctx2d.moveTo(face.projectedVertices[0][0], face.projectedVertices[0][1]);
          for (const vertex of face.projectedVertices.slice(1)) {
            ctx2d.lineTo(vertex[0], vertex[1]);
          }
          ctx2d.closePath();
          if (face.highlighted || face.exactHighlighted) {
            ctx2d.strokeStyle = face.exactHighlighted ? "#1e88e5" : "#ef6c00";
            ctx2d.lineWidth = face.exactHighlighted ? 2.6 : 2.2;
            ctx2d.stroke();
          }
        }
      }

      if (preview.bounds && forceMode !== "points" && (forceMode !== "solid" || !projectedFaces.length)) {
        const min = preview.bounds.min.map(Number);
        const max = preview.bounds.max.map(Number);
        const corners = [
          [min[0], min[1], min[2]],
          [max[0], min[1], min[2]],
          [max[0], max[1], min[2]],
          [min[0], max[1], min[2]],
          [min[0], min[1], max[2]],
          [max[0], min[1], max[2]],
          [max[0], max[1], max[2]],
          [min[0], max[1], max[2]]
        ].map((point) => [
          point[0] - preview.center[0],
          point[1] - preview.center[1],
          point[2] - preview.center[2]
        ]);

        const projectedCorners = corners.map((point) =>
          projectPoint(rotatePoint(point, viewer), scale, canvasEl, viewer, viewerDistance)
        );

        const edges = [
          [0,1,"x"],[1,2,"y"],[2,3,"x"],[3,0,"y"],
          [4,5,"x"],[5,6,"y"],[6,7,"x"],[7,4,"y"],
          [0,4,"z"],[1,5,"z"],[2,6,"z"],[3,7,"z"]
        ];

        for (const [a, b, axis] of edges) {
          ctx2d.strokeStyle = highlightAxes.includes(axis)
            ? "#ef6c00"
            : (forceMode === "solid" ? theme.bounds : theme.wireframe);
          ctx2d.lineWidth = highlightAxes.includes(axis) ? 2 : 1;
          ctx2d.beginPath();
          ctx2d.moveTo(projectedCorners[a][0], projectedCorners[a][1]);
          ctx2d.lineTo(projectedCorners[b][0], projectedCorners[b][1]);
          ctx2d.stroke();
        }
      }

      if (preview.kernelGeometry?.edges?.length && forceMode !== "points") {
        for (const edge of preview.kernelGeometry.edges) {
          const edgeTone = edgeHighlightTone(edge, {
            highlightComponents,
            highlightBodyKeys,
            highlightRawEdges,
            exactHighlightComponents,
            exactHighlightBodyKeys,
            exactHighlightRawEdges,
          });
          const edgeHighlighted = edgeTone === "changed" || highlightAxes.includes(edge.axis);
          const edgeExactHighlighted = edgeTone === "exact";
          const projectedEdge = edge.points.map((point) =>
            projectPoint(
              rotatePoint(
                [
                  point[0] - preview.center[0],
                  point[1] - preview.center[1],
                  point[2] - preview.center[2]
                ],
                viewer
              ),
              scale,
              canvasEl,
              viewer,
              viewerDistance
            )
          );
          const edgeKind = String(edge.kind || "").toLowerCase();
          const detailEdge = /spcurve|intersection|spline|trimmedcurve|circular|arc/.test(edgeKind);
          ctx2d.strokeStyle = edgeExactHighlighted ? "#1e88e5" : (edgeHighlighted ? "#ef6c00" : rgbCss(edge.strokeColor || theme.wireframe));
          ctx2d.lineWidth = edgeExactHighlighted ? 2.5 : (edgeHighlighted ? 2 : (forceMode === "solid" ? (detailEdge ? 0.95 : 0.7) : 1.5));
          const solidAlpha = detailEdge ? 0.48 : 0.2;
          ctx2d.globalAlpha = (edgeHighlighted || edgeExactHighlighted)
            ? 1
            : ((forceMode === "solid" ? solidAlpha : 1) * Math.max(0.2, Math.min(1, Number(edge.opacity ?? 1))));
          ctx2d.beginPath();
          ctx2d.moveTo(projectedEdge[0][0], projectedEdge[0][1]);
          for (const point of projectedEdge.slice(1)) {
            ctx2d.lineTo(point[0], point[1]);
          }
          ctx2d.stroke();
          ctx2d.globalAlpha = 1;
        }
      }

      const ordered = projected
        .map((point, index) => ({ point, index }))
        .sort((a, b) => a.point[2] - b.point[2]);

      if (forceMode !== "solid" || !projectedFaces.length) {
        ctx2d.fillStyle = highlightAxes.length || highlightComponents.length ? "#ef6c00" : theme.point;
        for (const item of ordered) {
          const [x, y, depth] = item.point;
          const radius = Math.max(3, ordered.length <= 2 ? 7 : 4 * depth);
          ctx2d.beginPath();
          ctx2d.arc(x, y, radius, 0, Math.PI * 2);
          ctx2d.fill();
        }
      }

      if (ordered.length === 1) {
        ctx2d.fillStyle = theme.secondaryText;
        ctx2d.font = "13px 'Segoe UI'";
        ctx2d.fillText("Only one geometric point was decoded from this file.", 16, 24);
      }

      if (preview?.renderStats?.faceReductionApplied) {
        ctx2d.fillStyle = theme.secondaryText;
        ctx2d.font = "12px 'Segoe UI'";
        ctx2d.fillText(
          `Preview mesh simplified: ${preview.renderStats.renderedFaceCount} of ${preview.renderStats.originalFaceCount} faces shown`,
          16,
          canvasEl.height - 18
        );
      }

      drawCompareAnnotations(ctx2d, canvasEl, preview, viewer, scale, compareAnnotation);
      drawViewportHud(ctx2d, canvasEl, report, preview, viewer, {
        mode: forceMode,
        highlightComponents: dedupeList([...highlightComponents, ...highlightBodyKeys, ...highlightRawFaces, ...highlightRawEdges]),
      });
      drawCornerTriad(canvasEl, ctx2d, viewer);
    }

    function drawPreview() {
      drawPreviewWebGl();
    }

    function render() {
      renderTabs();
      renderPreviewModes();
      renderPreviewPresets();
      renderFileList();
      renderFusionButton();
      renderSummary();
      renderOverview();
      renderNotes();
      renderHeader();
      renderTopology();
      renderGeometry();
      renderCompare();
      renderJson();
      renderPreviewNote();
      drawPreview();
    }

    async function postJson(url, payload) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({ error: "Unknown error" }));
        throw new Error(error.error || `Request failed: ${response.status}`);
      }
      return response.json();
    }

    function comparePairKey(left, right) {
      return `${reportKey(left)}||${reportKey(right)}`;
    }

    function compareMlPayload(report) {
      return {
        file: report?.file,
        file_name: report?.file_name,
        decoded_names: report?.decoded_names || [],
        geometry_hints: {
          bounds: report?.geometry_hints?.bounds || null,
          point_count: report?.geometry_hints?.point_count || 0,
        },
        kernel_bodies: kernelBodies(report),
      };
    }

    function compareMlResult(left, right) {
      return state.compareMlCache[comparePairKey(left, right)] || null;
    }

    async function requestCompareMl(left, right) {
      const key = comparePairKey(left, right);
      if (!left || !right || state.compareMlCache[key] || state.compareMlPending[key]) return;
      state.compareMlPending[key] = true;
      const startedAt = nowMs();
      try {
        const data = await postJson("/api/compare-ml", {
          left: compareMlPayload(left),
          right: compareMlPayload(right),
        });
        state.compareMlCache[key] = data || {};
        updateCompareTiming({
          label: `${left.file_name} vs ${right.file_name}`,
          requestMs: nowMs() - startedAt,
          status: data?.used_ml ? "Completed with ML" : (data?.available === false ? "Completed with fallback" : "Completed"),
          changedComponents: data?.stats?.changed_components ?? 0,
          removedComponents: data?.stats?.removed_components ?? 0,
          addedComponents: data?.stats?.added_components ?? 0,
          mode: data?.used_ml ? "ML" : "Fallback",
          modelType: data?.model?.type || (data?.available === false ? "Heuristic compare" : "Unknown"),
        });
      } catch (error) {
        state.compareMlCache[key] = { error: error.message, available: false, used_ml: false };
        updateCompareTiming({
          label: `${left.file_name} vs ${right.file_name}`,
          requestMs: nowMs() - startedAt,
          status: "Failed",
          changedComponents: "—",
          removedComponents: "—",
          addedComponents: "—",
          mode: "Error",
          modelType: error.message,
        });
      } finally {
        delete state.compareMlPending[key];
        const reports = comparisonReports();
        if (reports.length === 2 && comparePairKey(reports[0], reports[1]) === key) {
          renderCompare();
        }
      }
    }

    function compareMlStatusLabel(compareMl) {
      if (!compareMl) return "Loading";
      if (compareMl?.error) return "Unavailable";
      if (compareMl?.used_ml) {
        const modelType = compareMl?.model?.type || "ML";
        return `On (${modelType})`;
      }
      if (compareMl?.available === false) return "Fallback";
      return "Waiting";
    }

    function mergeCompareResults(baseDiff, compareMl) {
      if (!compareMl) return baseDiff;
      const hasExactStructuredGeometry = Boolean(
        (baseDiff.exactHighlightRawFaces?.left || []).length
        || (baseDiff.exactHighlightRawFaces?.right || []).length
        || (baseDiff.exactHighlightRawEdges?.left || []).length
        || (baseDiff.exactHighlightRawEdges?.right || []).length
      );
      const mlHighlightBodyKeys = hasExactStructuredGeometry ? { left: [], right: [] } : (compareMl.highlightBodyKeys || {});
      const mlHighlightRawFaces = hasExactStructuredGeometry ? { left: [], right: [] } : (compareMl.highlightRawFaces || {});
      const mlHighlightRawEdges = hasExactStructuredGeometry ? { left: [], right: [] } : (compareMl.highlightRawEdges || {});
      const mlAnnotations = hasExactStructuredGeometry ? { left: [], right: [] } : (compareMl.annotations || {});
      return {
        ...baseDiff,
        summary: dedupeList([...(compareMl.summary || []), ...(baseDiff.summary || [])]).slice(0, 18),
        highlightComponents: baseDiff.highlightComponents,
        highlightBodyKeys: {
          left: dedupeList([...(baseDiff.highlightBodyKeys?.left || []), ...(mlHighlightBodyKeys.left || [])]),
          right: dedupeList([...(baseDiff.highlightBodyKeys?.right || []), ...(mlHighlightBodyKeys.right || [])]),
        },
        highlightRawFaces: {
          left: dedupeList([...(baseDiff.highlightRawFaces?.left || []), ...(mlHighlightRawFaces.left || [])]),
          right: dedupeList([...(baseDiff.highlightRawFaces?.right || []), ...(mlHighlightRawFaces.right || [])]),
        },
        highlightRawEdges: {
          left: dedupeList([...(baseDiff.highlightRawEdges?.left || []), ...(mlHighlightRawEdges.left || [])]),
          right: dedupeList([...(baseDiff.highlightRawEdges?.right || []), ...(mlHighlightRawEdges.right || [])]),
        },
        exactHighlightBodyKeys: {
          left: dedupeList([...(baseDiff.exactHighlightBodyKeys?.left || []), ...((compareMl.exactHighlightBodyKeys || {}).left || [])]),
          right: dedupeList([...(baseDiff.exactHighlightBodyKeys?.right || []), ...((compareMl.exactHighlightBodyKeys || {}).right || [])]),
        },
        exactHighlightRawFaces: {
          left: dedupeList([...(baseDiff.exactHighlightRawFaces?.left || []), ...((compareMl.exactHighlightRawFaces || {}).left || [])]),
          right: dedupeList([...(baseDiff.exactHighlightRawFaces?.right || []), ...((compareMl.exactHighlightRawFaces || {}).right || [])]),
        },
        exactHighlightRawEdges: {
          left: dedupeList([...(baseDiff.exactHighlightRawEdges?.left || []), ...((compareMl.exactHighlightRawEdges || {}).left || [])]),
          right: dedupeList([...(baseDiff.exactHighlightRawEdges?.right || []), ...((compareMl.exactHighlightRawEdges || {}).right || [])]),
        },
        annotations: {
          left: [...(mlAnnotations.left || []), ...(baseDiff.annotations?.left || [])].slice(0, 10),
          right: [...(mlAnnotations.right || []), ...(baseDiff.annotations?.right || [])].slice(0, 10),
        },
        exactAnnotations: {
          left: [...(compareMl.exactAnnotations?.left || []), ...(baseDiff.exactAnnotations?.left || [])].slice(0, 10),
          right: [...(compareMl.exactAnnotations?.right || []), ...(baseDiff.exactAnnotations?.right || [])].slice(0, 10),
        },
        stats: {
          ...(baseDiff.stats || {}),
          ml: compareMlStatusLabel(compareMl),
          mlMatchedComponents: compareMl?.stats?.matched_components ?? null,
          mlChangedComponents: compareMl?.stats?.changed_components ?? null,
          mlAddedComponents: compareMl?.stats?.added_components ?? null,
          mlRemovedComponents: compareMl?.stats?.removed_components ?? null,
        },
        compareMl,
      };
    }

    function stepUploadCount(files) {
      return [...(files || [])].filter((file) => /\\.(step|stp)$/i.test(file?.name || "")).length;
    }

    function previewImportMessage(files) {
      const uploadFiles = [...(files || [])];
      const totalCount = uploadFiles.length;
      const stepCount = stepUploadCount(uploadFiles);
      if (!totalCount) return "Importing preview...";
      if (stepCount === totalCount) return `Importing STEP preview for ${totalCount} file(s)...`;
      if (stepCount > 0) return `Importing preview for ${totalCount} file(s)... STEP previews appear first.`;
      return `Importing preview for ${totalCount} file(s)...`;
    }

    async function ensureOcctImportJs() {
      if (occtImportPromise) return occtImportPromise;
      if (typeof window.occtimportjs !== "function") {
        throw new Error("Browser STEP importer is not available.");
      }
      occtImportPromise = window.occtimportjs({
        locateFile: (path) => `${OCCT_IMPORT_JS_BASE_URL}${path}`,
      });
      return occtImportPromise;
    }

    function importedRgbToDisplay(rgb, fallback = [196, 199, 205]) {
      if (!(Array.isArray(rgb) || ArrayBuffer.isView(rgb)) || rgb.length < 3) return fallback;
      const normalized = Array.from(rgb).slice(0, 3).map((value) => Number(value));
      if (!normalized.every((value) => Number.isFinite(value))) return fallback;
      const scale = normalized.some((value) => value > 1.001) ? 1 : 255;
      return normalized.map((value) => Math.max(0, Math.min(255, Math.round(value * scale))));
    }

    function triangleAverageNormal(normalArray, a, b, c) {
      if (!(Array.isArray(normalArray) || ArrayBuffer.isView(normalArray)) || !normalArray.length) return null;
      const indices = [a, b, c];
      const vectors = indices
        .map((index) => [normalArray[index * 3], normalArray[(index * 3) + 1], normalArray[(index * 3) + 2]])
        .filter((vector) => vector.every((value) => Number.isFinite(Number(value))));
      if (!vectors.length) return null;
      const average = vectors.reduce(
        (sum, vector) => [sum[0] + Number(vector[0]), sum[1] + Number(vector[1]), sum[2] + Number(vector[2])],
        [0, 0, 0]
      );
      const length = Math.hypot(average[0], average[1], average[2]) || 1;
      return average.map((value) => value / length);
    }

    function scaleBrowserStepPoint(point) {
      return [
        Number(point[0] || 0) * 0.001,
        Number(point[1] || 0) * 0.001,
        Number(point[2] || 0) * 0.001,
      ];
    }

    function browserStepMeshToKernelBody(mesh, bodyIndex) {
      const positions = mesh?.attributes?.position?.array || [];
      const normals = mesh?.attributes?.normal?.array || [];
      const indices = mesh?.index?.array || [];
      const displayProperties = { rgb: importedRgbToDisplay(mesh?.color) };
      const faces = [];
      const points = [];

      for (let triangleOffset = 0; triangleOffset + 2 < indices.length; triangleOffset += 3) {
        const triangle = [indices[triangleOffset], indices[triangleOffset + 1], indices[triangleOffset + 2]]
          .map((index) => Number(index));
        if (!triangle.every((index) => Number.isInteger(index) && index >= 0)) continue;
        const vertices = triangle.map((index) => scaleBrowserStepPoint([
          Number(positions[index * 3] || 0),
          Number(positions[(index * 3) + 1] || 0),
          Number(positions[(index * 3) + 2] || 0),
        ]));
        faces.push({
          name: `${mesh?.name || `Mesh ${bodyIndex}`} triangle ${(triangleOffset / 3) + 1}`,
          vertices,
          normal: triangleAverageNormal(normals, triangle[0], triangle[1], triangle[2]),
          metadata: {
            kind: "browser_occ_triangle_mesh",
            source_face_type: "browser_occ_triangle_mesh",
            source_face_index: (triangleOffset / 3) + 1,
            display_properties: displayProperties,
          },
        });
        points.push(...vertices);
      }

      return {
        kind: "solid",
        name: mesh?.name || `Body ${bodyIndex}`,
        faces,
        edges: [],
        vertices: [],
        metadata: {
          name: mesh?.name || `Body ${bodyIndex}`,
          primitive: "occ_mesh",
          shape_summary: `Browser STEP mesh with ${faces.length} triangles.`,
          source: "browser_occt_import_js",
          nx_body_index: bodyIndex,
          display_properties: displayProperties,
        },
        bounding_box: boundsFromPoints(points),
      };
    }

    function buildBrowserStepPreviewReport(file, result) {
      const meshes = Array.isArray(result?.meshes) ? result.meshes : [];
      const previewBodies = meshes
        .map((mesh, meshIndex) => browserStepMeshToKernelBody(mesh, meshIndex + 1))
        .filter((body) => (body.faces || []).length);
      const previewPoints = previewBodies.flatMap((body) => body.faces.flatMap((face) => face.vertices || []));
      const bounds = boundsFromPoints(previewPoints);
      const partName = file.name.replace(/\\.(step|stp)$/i, "") || file.name;

      return {
        file: `uploaded:${file.name}`,
        file_name: file.name,
        file_size_bytes: Number(file.size || 0),
        line_count: 0,
        header: {
          PART1: {
            APPL: "Browser STEP Preview Import",
            DATE: "",
            PART: partName,
          },
          PART2: {
            SCH: "STEP-ISO10303",
          },
        },
        transmit_info: {
          source_format: "step_file",
          original_units: "Millimeter",
        },
        decoded_names: [partName],
        density: null,
        colors: [],
        has_scale_factor_attribute: false,
        object_state_ids: [],
        structured_parse: null,
        preview: {
          strategy: "step_import_preview",
          label: "STEP Import",
          description: "Imported directly in the browser from the STEP file for immediate preview while detailed analysis continues in the background.",
        },
        entity_hints: {
          source: "step_file",
          step_backend: "browser_occt_import_js",
          mesh_triangle_count: previewBodies.reduce((sum, body) => sum + (body.faces || []).length, 0),
        },
        source_part_summary: {
          part_name: partName,
          leaf: file.name,
          units: "Millimeter",
        },
        source_bodies: [],
        source_body_count: previewBodies.length,
        kernel_body: previewBodies.length === 1 ? previewBodies[0] : null,
        kernel_bodies: previewBodies,
        preview_kernel_bodies: previewBodies,
        kernel_topology: null,
        geometry_hints: {
          point_count: previewPoints.length,
          point_samples: previewPoints.slice(0, 12),
          preview_points: previewPoints.slice(0, 1200),
          bounds,
          notable_scalar_values: [],
          unit_inference: "STEP preview mesh was imported in millimeters and normalized to the app's meter-based comparison units. Detailed topology and units may be refined after background analysis finishes.",
        },
        preview_geometry_hints: {
          point_count: previewPoints.length,
          preview_points: previewPoints.slice(0, 1200),
          bounds,
        },
        model_analysis: {
          unique_axis_levels: { x: [], y: [], z: [] },
          curve_hints: [],
          topology: null,
          summary: [
            `Loaded STEP preview mesh for ${partName} in the browser.`,
            `Detected ${previewBodies.length} body/bodies and ${previewBodies.reduce((sum, body) => sum + (body.faces || []).length, 0)} triangle faces for immediate preview.`,
            "Detailed STEP analysis is continuing in the background.",
          ],
        },
        step_import: {
          backend: "browser_occt_import_js",
          analysis_pending: true,
          refinement_pending: false,
        },
      };
    }

    async function importBrowserStepPreview(file) {
      const importer = await ensureOcctImportJs();
      const buffer = await file.arrayBuffer();
      await new Promise((resolve) => setTimeout(resolve, 0));
      const result = importer.ReadStepFile(new Uint8Array(buffer), {
        linearUnit: "millimeter",
        linearDeflectionType: "bounding_box_ratio",
        linearDeflection: 0.0025,
        angularDeflection: 0.35,
      });
      if (!result?.success || !(result?.meshes || []).length) {
        throw new Error(`Browser STEP import failed for ${file.name}.`);
      }
      return buildBrowserStepPreviewReport(file, result);
    }

    async function importBrowserStepPreviews(files) {
      const stepFiles = [...(files || [])].filter((file) => /\\.(step|stp)$/i.test(file?.name || ""));
      if (!stepFiles.length) return { reports: [], timing: { totalMs: 0, perFile: [] } };

      const importedReports = [];
      const perFile = [];
      const startedAt = nowMs();
      for (const file of stepFiles) {
        setStatus(`Importing STEP preview for ${file.name} in the browser...`);
        const fileStartedAt = nowMs();
        try {
          const report = await importBrowserStepPreview(file);
          importedReports.push(report);
          perFile.push({ name: file.name, ms: nowMs() - fileStartedAt, ok: true });
          mergeReports([report]);
          setStatus(previewReadyMessage(report));
        } catch (error) {
          perFile.push({ name: file.name, ms: nowMs() - fileStartedAt, ok: false, error: error.message });
          setStatus(`STEP preview import fallback for ${file.name}: ${error.message}`);
        }
      }
      return {
        reports: importedReports,
        timing: {
          totalMs: nowMs() - startedAt,
          perFile,
        },
      };
    }

    async function uploadFilesWithProgress(files) {
      return new Promise((resolve, reject) => {
        const uploadFiles = [...files];
        const xhr = new XMLHttpRequest();
        const formData = new FormData();
        const startedAt = nowMs();
        let uploadFinishedAt = null;

        uploadFiles.forEach((file) => {
          formData.append("files", file, file.name);
        });

        xhr.open("POST", "/api/analyze-uploads");
        xhr.responseType = "json";

        xhr.upload.addEventListener("progress", (event) => {
          if (event.lengthComputable && event.total > 0) {
            const percent = (event.loaded / event.total) * 100;
            setStatus(`Uploading ${uploadFiles.length} file(s)... ${Math.round(percent)}%`);
            setStatusProgress(percent);
          } else {
            setStatus(`Uploading ${uploadFiles.length} file(s)...`);
            setStatusProgress(null, { indeterminate: true });
          }
        });

        xhr.upload.addEventListener("load", () => {
          uploadFinishedAt = nowMs();
          setStatus(previewImportMessage(uploadFiles));
          setStatusProgress(null, { indeterminate: true });
        });

        xhr.addEventListener("load", () => {
          const response = xhr.response || (() => {
            try {
              return JSON.parse(xhr.responseText || "{}");
            } catch {
              return {};
            }
          })();
          if (xhr.status >= 200 && xhr.status < 300) {
            const finishedAt = nowMs();
            resolve({
              response,
              timing: {
                uploadMs: (uploadFinishedAt ?? finishedAt) - startedAt,
                serverAnalysisMs: finishedAt - (uploadFinishedAt ?? startedAt),
                totalRequestMs: finishedAt - startedAt,
                uploadBytes: uploadFiles.reduce((sum, file) => sum + Number(file?.size || 0), 0),
              },
            });
            return;
          }
          reject(new Error(response.error || `Request failed: ${xhr.status}`));
        });

        xhr.addEventListener("error", () => {
          reject(new Error("Upload failed."));
        });

        xhr.addEventListener("abort", () => {
          reject(new Error("Upload cancelled."));
        });

        setStatus(`Uploading ${uploadFiles.length} file(s)...`);
        setStatusProgress(0);
        xhr.send(formData);
      });
    }

    async function postStepAnalysis(file) {
      const formData = new FormData();
      formData.append("files", file, file.name);
      const response = await fetch("/api/analyze-step-details", {
        method: "POST",
        body: formData,
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({ error: "Unknown error" }));
        throw new Error(error.error || `Request failed: ${response.status}`);
      }
      return response.json();
    }

    function analyzeStepFilesInBackground(files) {
      [...files]
        .filter((file) => /\\.(step|stp)$/i.test(file.name))
        .forEach((file) => {
          const refinementKey = `uploaded:${file.name}::${file.name}`;
          if (pendingStepRefinements.has(refinementKey)) return;
          pendingStepRefinements.add(refinementKey);
          const startedAt = nowMs();
          postStepAnalysis(file)
            .then((data) => {
              updateBackgroundStepTiming(file.name, {
                durationMs: nowMs() - startedAt,
                status: "Completed",
              });
              if (data?.reports?.length) {
                mergeStepAnalysisReports(data.reports);
                const active = activeReport();
                if (active && reportKey(active) === refinementKey) {
                  setStatus(`Detailed STEP analysis ready for ${active.file_name}.`);
                }
              }
            })
            .catch(() => {
              updateBackgroundStepTiming(file.name, {
                durationMs: nowMs() - startedAt,
                status: "Failed",
              });
              // Keep the viewer-style STEP preview if background analysis fails.
            })
            .finally(() => {
              pendingStepRefinements.delete(refinementKey);
            });
        });
    }

    async function loadSamples() {
      setStatus("Loading workspace samples...");
      const response = await fetch("/api/samples");
      const data = await response.json();
      state.reports = enrichReports(data.reports || []);
      state.selected = state.reports.length ? [bestReportKey(state.reports)] : [];
      resetPreviewIfNeeded();
      render();
      setStatus(loadedReportsStatus(state.reports));
    }

    async function loadPaths() {
      const raw = byId("path-input").value.trim();
      if (!raw) {
        setStatus("Enter one or more file paths first.");
        return;
      }
      const paths = raw.split(",").map((item) => item.trim()).filter(Boolean);
      setStatus("Loading file paths...");
      const data = await postJson("/api/analyze-paths", { paths });
      mergeReports(data.reports || []);
      if (data.reports.length === 1) {
        setStatus(previewReadyMessage(data.reports[0]));
      } else {
        setStatus(loadedReportsStatus(data.reports || []));
      }
    }

    async function loadUploads(files) {
      if (!files.length) return;
      const uploadFiles = [...files];
      const stepFiles = uploadFiles.filter((file) => /\\.(step|stp)$/i.test(file.name));
      const browserPreviewPromise = stepFiles.length ? importBrowserStepPreviews(uploadFiles) : Promise.resolve([]);
      const uploadPromise = uploadFilesWithProgress(uploadFiles);
      const importStartedAt = nowMs();
      try {
        const browserPreviewResult = await browserPreviewPromise;
        const uploadResult = await uploadPromise;
        const browserPreviewReports = Array.isArray(browserPreviewResult) ? browserPreviewResult : (browserPreviewResult?.reports || []);
        const browserPreviewTiming = browserPreviewResult?.timing || { totalMs: 0, perFile: [] };
        const data = uploadResult?.response || {};
        const requestTiming = uploadResult?.timing || {};
        updateImportTiming({
          fileCount: uploadFiles.length,
          stepFileCount: stepFiles.length,
          primaryName: uploadFiles.length === 1 ? uploadFiles[0].name : `${uploadFiles.length} files`,
          totalMs: nowMs() - importStartedAt,
          uploadMs: requestTiming.uploadMs,
          serverAnalysisMs: requestTiming.serverAnalysisMs,
          browserPreviewMs: browserPreviewTiming.totalMs,
          uploadBytes: requestTiming.uploadBytes,
          browserPreviewBreakdown: (browserPreviewTiming.perFile || []).map((entry) => `${entry.name}: ${formatDuration(entry.ms)}`).join(", "),
        });
        if (browserPreviewReports.length) {
          mergeUploadedAnalysisReports(data.reports || [], { preserveSelection: true });
          if ((data.reports || []).length === 1) {
            setStatus(`Detailed analysis ready for ${data.reports[0].file_name}.`);
          } else {
            setStatus(loadedReportsStatus(data.reports || []));
          }
        } else {
          mergeReports(data.reports || []);
          if (data.reports.length === 1) {
            setStatus(previewReadyMessage(data.reports[0]));
          } else {
            setStatus(loadedReportsStatus(data.reports || []));
          }
        }
      } finally {
        clearStatusProgress();
      }
    }

    function openCompareFromSelection() {
      if (!canCompareSelection()) {
        setStatus("Select exactly two parts first.");
        return;
      }
      state.activeTab = "compare";
      render();
      setStatus("Comparing the two selected parts.");
    }

    async function createFusionFromSelection() {
      const reports = selectedReports();
      if (reports.length !== 2) {
        setStatus("Select exactly two parts first.");
        return;
      }
      if (!canFuseSelection()) {
        setStatus("Select one Siemens NX JSON report and one matching STEP report to improve the JSON reconstruction.");
        return;
      }
      setStatus("Building an improved JSON reconstruction using the selected STEP file...");
      const data = await postJson("/api/fuse-reports", { reports });
      mergeReports(data.reports || []);
      const created = data.reports?.[0];
      setStatus(created ? `Created STEP-referenced Fusion preview: ${created.file_name}.` : "Created STEP-referenced Fusion preview.");
    }

    function exportCurrentJson() {
      const report = activeReport();
      if (!report) {
        setStatus("Select a part first, then export its JSON.");
        return;
      }
      const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${report.file_name.replace(/\\.(x_t|step|stp)$/i, "") || "report"}_report.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      setStatus(`Exported ${report.file_name} as JSON.`);
    }

    window.addEventListener("mousemove", (event) => {
      if (!dragState) return;
      const dx = event.clientX - dragState.x;
      const dy = event.clientY - dragState.y;
      dragState.x = event.clientX;
      dragState.y = event.clientY;
      const viewers = dragState.viewers || [];

      viewers.forEach((viewer) => {
        if (dragState.pan) {
          viewer.panX += dx;
          viewer.panY += dy;
        } else {
          const yawRotation = rotationAroundY(-dx * 0.01);
          const pitchRotation = rotationAroundX(-dy * 0.01);
          viewer.rotation = normalizeRotationMatrix(
            multiplyRotationMatrices(
              pitchRotation,
              multiplyRotationMatrices(yawRotation, viewer.rotation || defaultRotation())
            )
          );
        }
      });
      dragState.redraw();
    });

    window.addEventListener("mouseup", () => {
      if (dragState?.canvasEl) {
        dragState.canvasEl.style.cursor = "grab";
      }
      dragState = null;
    });

    window.addEventListener("resize", () => {
      drawPreview();
      if (state.activeTab === "compare") {
        drawComparePreviews();
      }
    });

    byId("load-samples").onclick = () => loadSamples().catch((error) => setStatus(error.message));
    byId("load-paths").onclick = () => loadPaths().catch((error) => setStatus(error.message));
    byId("export-json").onclick = exportCurrentJson;
    byId("reset-view").onclick = () => {
      resetPreviewIfNeeded();
      drawPreview();
      setStatus("Preview view reset.");
    };
    byId("file-input").addEventListener("change", (event) => {
      loadUploads(event.target.files).catch((error) => setStatus(error.message));
      event.target.value = "";
    });

    render();
  </script>
</body>
</html>
"""


def enrich_report(report: dict) -> dict:
    geometry = report.get("geometry_hints", {})
    for item in geometry.get("notable_scalar_values", []):
        item["pretty_metric_and_imperial"] = metric_and_imperial(item["value"])
    report["preview"] = {
        **dict(report.get("preview") or {}),
        **_preview_metadata_for_report(report),
    }
    return report


class XTPartRequestHandler(BaseHTTPRequestHandler):
    def _send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b"{}"
        return json.loads(body.decode("utf-8"))

    def _read_multipart_files(self) -> list[dict]:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return []
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b""
        if not body:
            return []

        message = BytesParser(policy=policy.default).parsebytes(
            (
                f"Content-Type: {content_type}\r\n"
                "MIME-Version: 1.0\r\n"
                "\r\n"
            ).encode("utf-8")
            + body
        )
        if not message.is_multipart():
            return []

        payloads: list[dict] = []
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            if part.get_param("name", header="content-disposition") != "files":
                continue
            raw_bytes = part.get_payload(decode=True) or b""
            payloads.append(
                {
                    "name": part.get_filename() or "<upload>",
                    "data": raw_bytes,
                    "size": len(raw_bytes),
                }
            )
        return payloads

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HTML)
            return

        if parsed.path.startswith("/vendor/"):
            repo_root = Path(__file__).resolve().parent
            relative = Path(parsed.path.lstrip("/"))
            file_path = (repo_root / relative).resolve()
            vendor_root = (repo_root / "vendor").resolve()
            if vendor_root not in file_path.parents and file_path != vendor_root:
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if not file_path.exists() or not file_path.is_file():
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return

            suffix = file_path.suffix.lower()
            content_type = {
                ".js": "text/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".map": "application/json; charset=utf-8",
                ".wasm": "application/wasm",
            }.get(suffix, "application/octet-stream")
            self._send_bytes(file_path.read_bytes(), content_type)
            return

        if parsed.path == "/api/samples":
            try:
                reports = _reports_for_client([enrich_report(report) for report in workspace_sample_reports()])
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json({"reports": reports})
            return

        if parsed.path == "/api/ping":
            self._send_json({"ok": True})
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        try:
            if parsed.path == "/api/analyze-uploads":
                reports: list[dict] = []
                for file_payload in self._read_multipart_files():
                    name = str(file_payload.get("name") or "<upload>")
                    raw_bytes = bytes(file_payload.get("data") or b"")
                    reports.extend(
                        analyze_uploaded_binary_files(
                            [
                                {
                                    "name": name,
                                    "data": raw_bytes,
                                    "size": int(file_payload.get("size") or len(raw_bytes)),
                                }
                            ]
                        )
                    )
                self._send_json({"reports": _reports_for_client(reports)})
                return

            if parsed.path == "/api/analyze-step-details":
                files = self._read_multipart_files()
                reports: list[dict] = []
                for file_payload in files:
                    name = str(file_payload.get("name") or "<upload>")
                    if not re.search(r"\.(stp|step)$", name, flags=re.IGNORECASE):
                        continue
                    raw_bytes = bytes(file_payload.get("data") or b"")
                    raw_text = raw_bytes.decode("utf-8", errors="ignore")
                    reports.extend(
                        parse_step_input_text(
                            raw_text,
                            display_name=name,
                            source_path=f"uploaded:{name}",
                            file_size_bytes=int(file_payload.get("size") or len(raw_bytes)),
                            prefer_occ_preview=False,
                        )
                        or []
                    )
                self._send_json({"reports": _reports_for_client([enrich_report(report) for report in reports])})
                return

            payload = self._read_json()
            if parsed.path == "/api/analyze-paths":
                reports = _reports_for_client([enrich_report(report) for report in analyze_paths(payload.get("paths", []))])
                self._send_json({"reports": reports})
                return

            if parsed.path == "/api/analyze-text":
                reports = _reports_for_client([enrich_report(report) for report in analyze_uploaded_files(payload.get("files", []))])
                self._send_json({"reports": reports})
                return

            if parsed.path == "/api/fuse-reports":
                selected_reports = payload.get("reports", [])
                if len(selected_reports) != 2:
                    raise ValueError("Select exactly two reports to fuse.")
                fused_report = create_fused_report(selected_reports[0], selected_reports[1])
                if fused_report is None:
                    raise ValueError("Selected reports are not a compatible STEP + JSON fusion pair.")
                reports = _reports_for_client([enrich_report(fused_report)])
                self._send_json({"reports": reports})
                return

            if parsed.path == "/api/compare-ml":
                left_report = payload.get("left") or {}
                right_report = payload.get("right") or {}
                self._send_json(compare_reports_ml(left_report, right_report))
                return
        except FileNotFoundError as exc:
            self._send_json({"error": f"File not found: {exc}"}, status=HTTPStatus.NOT_FOUND)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        return


def open_browser() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}/")


def create_server(start_port: int) -> ThreadingHTTPServer:
    global PORT
    for port in range(start_port, start_port + 25):
        try:
            server = ThreadingHTTPServer((HOST, port), XTPartRequestHandler)
            PORT = port
            return server
        except OSError:
            continue
    raise OSError(f"Could not bind any port in range {start_port}-{start_port + 24}")


def main() -> int:
    bootstrap_status = ensure_backend_runtimes(verbose=print)
    if not bootstrap_status.get("message_already_reported"):
        print(bootstrap_status.get("message"))

    server = create_server(PORT)
    print(f"Serving {HOST}:{PORT}")
    print("Open this in your browser if it does not launch automatically:")
    print(f"http://{HOST}:{PORT}/")

    if not os.environ.get("CODEX_NO_BROWSER"):
        try:
            open_browser()
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
