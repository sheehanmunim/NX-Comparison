#!/usr/bin/env python3

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from xt_engine.topology import build_topology_from_kernel_body
from xt_kernel import build_kernel_body
from xt_parser_foundation import infer_entity_hints, parse_xt_structure


NUMBER_PATTERN = r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:e[+-]?\d+)?"
PRINTABLE_ASCII = set(range(32, 127))


def normalize_xt_text(raw_text: str) -> str:
    lines = raw_text.splitlines()
    rebuilt: list[str] = []

    for index, line in enumerate(lines):
        line = line.rstrip("\r\n")
        if index == 0:
            rebuilt.append(line)
            continue

        previous_char = rebuilt[-1][-1] if rebuilt[-1] else " "
        if previous_char.isspace() or (line and line[0].isspace()):
            rebuilt.append(" " + line.lstrip())
        else:
            rebuilt.append(line)

    return "".join(rebuilt)


def clean_value(value: str) -> str:
    return value.strip().rstrip(";")


def parse_header(raw_text: str) -> dict[str, dict[str, str]]:
    header: dict[str, dict[str, str]] = {}
    section = "root"
    header[section] = {}

    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("**PART"):
            section = stripped.rstrip(";*").lstrip("*")
            header.setdefault(section, {})
            continue
        if stripped.startswith("**END_OF_HEADER"):
            break
        if not stripped or stripped.startswith("**"):
            continue
        if "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        header[section][clean_value(key)] = clean_value(value)

    return header


def unique_preserve_order(items: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    unique_items: list[Any] = []

    for item in items:
        marker = json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else item
        if marker in seen:
            continue
        seen.add(marker)
        unique_items.append(item)

    return unique_items


def round_key(value: float, digits: int = 12) -> float:
    return round(value, digits)


def nearly_equal(a: float, b: float, tolerance: float = 1e-6) -> bool:
    return abs(a - b) <= tolerance


def extract_unames(normalized_text: str) -> list[str]:
    names: list[str] = []
    pattern = re.compile(
        r"98\s+255\s+(\d+)\s+((?:\d+\s+){2,40})79\s+\d+\s+\d+\s+SDL/TYSA_UNAME(?:\d+)?"
    )

    for match in pattern.finditer(normalized_text):
        length = int(match.group(1))
        ints = [int(token) for token in match.group(2).split()]
        candidates = [
            ints[-length:],
            ints[1 : length + 1],
            ints[:length],
        ]

        for candidate in candidates:
            if len(candidate) != length:
                continue
            if all(code in PRINTABLE_ASCII for code in candidate):
                names.append("".join(chr(code) for code in candidate))
                break

    return unique_preserve_order(names)


def extract_density(normalized_text: str) -> dict[str, Any] | None:
    pattern = re.compile(
        r"3\s+83\s+1\s+\d+\s+("
        + NUMBER_PATTERN
        + r")\s+84\s+\d+\s+(.+?)\s+79\s+16\s+\d+\s+SDL/TYSA_DENSITY(?:\d+)?"
    )
    match = pattern.search(normalized_text)
    if not match:
        return None

    unit_tokens = match.group(2).split()
    if unit_tokens and unit_tokens[0].isdigit():
        unit_tokens = unit_tokens[1:]

    unit = " ".join(unit_tokens).strip()
    return {
        "value": float(match.group(1)),
        "unit": unit or None,
    }


def extract_colors(normalized_text: str) -> list[dict[str, Any]]:
    colors: list[dict[str, Any]] = []
    pattern = re.compile(
        r"83\s+255\s+3\s+\d+\s+("
        + NUMBER_PATTERN
        + r")\s+("
        + NUMBER_PATTERN
        + r")\s+("
        + NUMBER_PATTERN
        + r")\s+79\s+255\s+\d+\s+\d+\s+SDL/TYSA_COLOUR(?:_281)?(?:\d+)?"
    )

    for match in pattern.finditer(normalized_text):
        rgb = tuple(float(match.group(index)) for index in range(1, 4))
        colors.append(
            {
                "rgb": rgb,
                "hex": "#{:02x}{:02x}{:02x}".format(
                    *[max(0, min(255, round(component * 255))) for component in rgb]
                ),
            }
        )

    return unique_preserve_order(colors)


def extract_rgb_like_values(normalized_text: str) -> set[float]:
    values: set[float] = set()
    pattern = re.compile(
        r"83(?:\s+255)?\s+3\s+\d+\s+("
        + NUMBER_PATTERN
        + r")\s+("
        + NUMBER_PATTERN
        + r")\s+("
        + NUMBER_PATTERN
        + r")"
    )

    for match in pattern.finditer(normalized_text):
        values.update(round_key(float(match.group(index))) for index in range(1, 4))

    return values


def extract_object_state_ids(normalized_text: str) -> list[str]:
    ids: list[str] = []
    for match in re.finditer(r"UGS/ObjectState(?:\d+)?", normalized_text):
        window = normalized_text[max(0, match.start() - 500) : match.end() + 500]
        ids.extend(re.findall(r"\b[0-9a-f]{20,40}\b", window))
    return unique_preserve_order(ids)


def extract_transmit_info(normalized_text: str) -> dict[str, str]:
    info: dict[str, str] = {}

    version_match = re.search(
        r"TRANSMIT FILE created by modeller version\s+([^\s]+)\s+([^\s]+)",
        normalized_text,
    )
    if version_match:
        info["modeller_version"] = version_match.group(1)
        info["schema_version"] = version_match.group(2)

    return info


def extract_geometry_points(normalized_text: str) -> list[tuple[float, float, float]]:
    points: list[tuple[float, float, float]] = []
    pattern = re.compile(
        r"\+\s*(" + NUMBER_PATTERN + r")\s+(" + NUMBER_PATTERN + r")\s+(" + NUMBER_PATTERN + r")"
    )

    for match in pattern.finditer(normalized_text):
        raw_values = [match.group(index) for index in range(1, 4)]
        point = tuple(float(value) for value in raw_values)
        if not all(abs(value) <= 2 for value in point):
            continue
        # Keep the origin even when XT encodes it as plain integers; several
        # simple round-part files pair that origin with a decimal end-center.
        if not any("." in value or "e" in value.lower() for value in raw_values) and any(abs(value) > 1e-12 for value in point):
            continue
        points.append(point)

    deduped: list[tuple[float, float, float]] = []
    seen: set[tuple[float, float, float]] = set()
    for point in points:
        marker = tuple(round_key(value) for value in point)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(point)

    return deduped


def extract_notable_scalars(normalized_text: str, excluded_values: set[float]) -> list[dict[str, Any]]:
    values: list[float] = []
    counts: Counter[float] = Counter()

    for match in re.finditer(NUMBER_PATTERN, normalized_text):
        raw = match.group(0)
        if "." not in raw and "e" not in raw.lower():
            continue
        value = float(raw)
        abs_value = abs(value)
        if abs_value < 1e-6 or abs(abs_value - 1.0) < 1e-9:
            continue
        if abs_value > 2:
            continue
        key = round_key(abs_value)
        if key in excluded_values:
            continue
        counts[key] += 1
        values.append(abs_value)

    results: list[dict[str, Any]] = []
    for value, frequency in counts.most_common():
        results.append(
            {
                "value": value,
                "frequency": frequency,
            }
        )

    return results[:10]


def summarize_bounds(points: list[tuple[float, float, float]]) -> dict[str, Any] | None:
    if len(points) < 2:
        return None

    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    zs = [point[2] for point in points]
    minimum = (min(xs), min(ys), min(zs))
    maximum = (max(xs), max(ys), max(zs))
    size = tuple(maximum[index] - minimum[index] for index in range(3))

    return {
        "min": minimum,
        "max": maximum,
        "size": size,
    }


def format_triplet(values: tuple[float, float, float]) -> list[float]:
    return [round(value, 12) for value in values]


def metric_and_imperial(value: float) -> str:
    millimeters = value * 1000.0
    inches = value * 39.37007874015748
    return f"{value:.12g} ({millimeters:.3f} mm, {inches:.3f} in)"


def infer_length_unit(notable_scalars: list[dict[str, Any]]) -> str | None:
    close_to_round_inches = 0
    for item in notable_scalars[:5]:
        inches = item["value"] * 39.37007874015748
        if abs(inches - round(inches, 3)) < 1e-6:
            close_to_round_inches += 1

    if close_to_round_inches >= 2:
        return "Values strongly suggest the model is stored in meters; several dimensions convert to exact inch values."

    return None


def describe_dimension(value: float) -> dict[str, float]:
    return {
        "model_units": round(value, 12),
        "millimeters": round(value * 1000.0, 6),
        "inches": round(value * 39.37007874015748, 6),
    }


def summarize_unique_levels(points: list[tuple[float, float, float]], tolerance: float = 1e-6) -> dict[str, list[float]]:
    axis_values = {"x": [], "y": [], "z": []}
    for point in points:
        for axis, value in zip(("x", "y", "z"), point):
            if not any(nearly_equal(value, existing, tolerance) for existing in axis_values[axis]):
                axis_values[axis].append(value)

    for axis in axis_values:
        axis_values[axis].sort()

    return {axis: [round(value, 12) for value in values] for axis, values in axis_values.items()}


def infer_box_topology(
    points: list[tuple[float, float, float]],
    bounds: dict[str, Any] | None,
    tolerance: float = 1e-6,
) -> dict[str, Any] | None:
    if not bounds:
        return None

    minimum = tuple(float(value) for value in bounds["min"])
    maximum = tuple(float(value) for value in bounds["max"])
    size = tuple(float(value) for value in bounds["size"])

    if any(value <= tolerance for value in size):
        return None

    expected_corners = [
        (x, y, z)
        for x in (minimum[0], maximum[0])
        for y in (minimum[1], maximum[1])
        for z in (minimum[2], maximum[2])
    ]

    matched_corners: list[tuple[float, float, float]] = []
    for corner in expected_corners:
        if any(all(nearly_equal(point[i], corner[i], tolerance) for i in range(3)) for point in points):
            matched_corners.append(corner)

    if len(matched_corners) < 6:
        return None

    corners = {
        "000": (minimum[0], minimum[1], minimum[2]),
        "100": (maximum[0], minimum[1], minimum[2]),
        "110": (maximum[0], maximum[1], minimum[2]),
        "010": (minimum[0], maximum[1], minimum[2]),
        "001": (minimum[0], minimum[1], maximum[2]),
        "101": (maximum[0], minimum[1], maximum[2]),
        "111": (maximum[0], maximum[1], maximum[2]),
        "011": (minimum[0], maximum[1], maximum[2]),
    }

    face_specs = [
        ("x", "min", minimum[0], (size[1], size[2]), (-1, 0, 0), ["000", "010", "011", "001"]),
        ("x", "max", maximum[0], (size[1], size[2]), (1, 0, 0), ["100", "101", "111", "110"]),
        ("y", "min", minimum[1], (size[0], size[2]), (0, -1, 0), ["000", "001", "101", "100"]),
        ("y", "max", maximum[1], (size[0], size[2]), (0, 1, 0), ["010", "110", "111", "011"]),
        ("z", "min", minimum[2], (size[0], size[1]), (0, 0, -1), ["000", "100", "110", "010"]),
        ("z", "max", maximum[2], (size[0], size[1]), (0, 0, 1), ["001", "011", "111", "101"]),
    ]

    faces = []
    for axis, side, position, dimensions, normal, vertex_keys in face_specs:
        width, height = dimensions
        face_kind = "square" if nearly_equal(width, height, tolerance) else "rectangular"
        faces.append(
            {
                "name": f"{axis.upper()}-{side} face",
                "axis": axis,
                "side": side,
                "position": round(position, 12),
                "dimensions": {
                    "first": describe_dimension(width),
                    "second": describe_dimension(height),
                },
                "area": describe_dimension(width * height),
                "kind": face_kind,
                "normal": list(normal),
                "vertices": [format_triplet(corners[key]) for key in vertex_keys],
            }
        )

    edge_families = []
    unique_lengths = unique_preserve_order([round_key(value) for value in size if value > tolerance])
    for length in unique_lengths:
        actual = next(value for value in size if nearly_equal(value, length, tolerance))
        multiplicity = sum(1 for value in size if nearly_equal(value, actual, tolerance))
        count = 4 * multiplicity
        edge_families.append(
            {
                "length": describe_dimension(actual),
                "count": count,
            }
        )

    sorted_dims = sorted(size, reverse=True)
    square_plan = nearly_equal(size[0], size[1], tolerance) or nearly_equal(size[1], size[2], tolerance) or nearly_equal(size[0], size[2], tolerance)
    cube_like = nearly_equal(size[0], size[1], tolerance) and nearly_equal(size[1], size[2], tolerance)
    thin_ratio = sorted_dims[2] / sorted_dims[0] if sorted_dims[0] > tolerance else 1.0

    if cube_like:
        shape_label = "cube-like solid"
    elif square_plan and thin_ratio < 0.35:
        shape_label = "square plate / square prism"
    elif thin_ratio < 0.35:
        shape_label = "rectangular plate / block"
    elif nearly_equal(size[0], size[1], tolerance):
        shape_label = "square-section rectangular prism"
    else:
        shape_label = "rectangular prism"

    return {
        "kind": "axis_aligned_box",
        "shape_label": shape_label,
        "matched_corner_count": len(matched_corners),
        "corner_count": 8,
        "corners": [format_triplet(corner) for corner in expected_corners],
        "faces": faces,
        "edge_families": edge_families,
        "face_count": 6,
        "edge_count": 12,
    }


def infer_curve_hints(decoded_names: list[str], notable_scalars: list[dict[str, Any]]) -> list[str]:
    hints: list[str] = []
    joined = " ".join(decoded_names).lower()

    if "arc" in joined:
        hints.append("Decoded names suggest at least one arc entity is present.")
    if "line" in joined:
        hints.append("Decoded names suggest line entities are present.")

    if notable_scalars:
        dominant = notable_scalars[0]["value"]
        if "arc" in joined:
            hints.append(
                f"The dominant repeated scalar {metric_and_imperial(dominant)} may correspond to an arc radius or diameter-related value."
            )

    return hints


def infer_model_analysis(
    points: list[tuple[float, float, float]],
    bounds: dict[str, Any] | None,
    decoded_names: list[str],
    notable_scalars: list[dict[str, Any]],
) -> dict[str, Any]:
    topology = infer_box_topology(points, bounds)
    analysis: dict[str, Any] = {
        "unique_axis_levels": summarize_unique_levels(points),
        "curve_hints": infer_curve_hints(decoded_names, notable_scalars),
        "topology": topology,
        "summary": [],
    }

    if topology:
        summary = [
            f"Model geometry matches an inferred {topology['shape_label']}.",
            f"Detected {topology['face_count']} faces and {topology['edge_count']} edges from the inferred box topology.",
        ]

        square_faces = sum(1 for face in topology["faces"] if face["kind"] == "square")
        rectangular_faces = topology["face_count"] - square_faces
        if square_faces:
            summary.append(f"{square_faces} faces are square-like and {rectangular_faces} are rectangular.")

        analysis["summary"] = summary
        return analysis

    if bounds and len(points) >= 3:
        analysis["summary"] = [
            "The part exposes enough geometry points for a preview and bounding box, but not enough corner evidence for a reliable solid-face reconstruction.",
        ]
        return analysis

    if decoded_names:
        analysis["summary"] = [
            "The file contains named geometry entities, but there is not enough extracted 3D point data to reconstruct solid faces and edges reliably.",
        ]
        return analysis

    analysis["summary"] = [
        "The file does not expose enough directly readable geometry records to infer faces and edges confidently without a full Parasolid kernel.",
    ]
    return analysis


def analyze_xt_text(
    raw_text: str,
    *,
    display_name: str = "<memory>",
    source_path: str | None = None,
    file_size_bytes: int | None = None,
) -> dict[str, Any]:
    normalized_text = normalize_xt_text(raw_text)
    header = parse_header(raw_text)
    colors = extract_colors(normalized_text)
    color_components = extract_rgb_like_values(normalized_text)
    geometry_points = extract_geometry_points(normalized_text)
    notable_scalars = extract_notable_scalars(normalized_text, excluded_values=color_components)
    bounds = summarize_bounds(geometry_points)
    decoded_names = extract_unames(normalized_text)
    model_analysis = infer_model_analysis(geometry_points, bounds, decoded_names, notable_scalars)
    structured_parse = parse_xt_structure(normalized_text)
    entity_hints = infer_entity_hints(structured_parse, decoded_names)
    kernel_body_object = build_kernel_body(geometry_points, bounds, decoded_names, structured_parse, notable_scalars, entity_hints)
    kernel_body = kernel_body_object.to_dict() if kernel_body_object else None
    if kernel_body is None:
        kernel_body = infer_compound_preview_from_xt_scalars(notable_scalars, entity_hints, decoded_names)
        if kernel_body is not None:
            model_analysis["summary"].append(
                "A heuristic compound preview was synthesized from repeated XT scalar values and surface records."
            )
    kernel_topology = build_topology_from_kernel_body(
        kernel_body if kernel_body and kernel_body.get("metadata", {}).get("primitive") != "compound" else None
    )

    result: dict[str, Any] = {
        "file": source_path or display_name,
        "file_name": display_name,
        "file_size_bytes": file_size_bytes if file_size_bytes is not None else len(raw_text.encode()),
        "line_count": raw_text.count("\n") + (1 if raw_text else 0),
        "header": header,
        "transmit_info": extract_transmit_info(normalized_text),
        "decoded_names": decoded_names,
        "density": extract_density(normalized_text),
        "colors": colors,
        "has_scale_factor_attribute": "SDL/TYSA_SCALE_FACTOR" in normalized_text,
        "object_state_ids": extract_object_state_ids(normalized_text),
        "structured_parse": structured_parse,
        "entity_hints": entity_hints,
        "kernel_body": kernel_body,
        "kernel_topology": kernel_topology,
        "geometry_hints": {
            "point_count": len(geometry_points),
            "point_samples": [format_triplet(point) for point in geometry_points[:12]],
            "preview_points": [format_triplet(point) for point in geometry_points[:500]],
            "bounds": (
                {
                    "min": format_triplet(bounds["min"]),
                    "max": format_triplet(bounds["max"]),
                    "size": format_triplet(bounds["size"]),
                }
                if bounds
                else None
            ),
            "notable_scalar_values": [
                {
                    "value": item["value"],
                    "frequency": item["frequency"],
                    "assuming_meters": {
                        "millimeters": round(item["value"] * 1000.0, 6),
                        "inches": round(item["value"] * 39.37007874015748, 6),
                    },
                }
                for item in notable_scalars
            ],
            "unit_inference": infer_length_unit(notable_scalars),
        },
        "model_analysis": model_analysis,
    }

    return result


def analyze_xt_file(path: Path) -> dict[str, Any]:
    raw_text = path.read_text(errors="ignore")
    return analyze_xt_text(
        raw_text,
        display_name=path.name,
        source_path=str(path),
        file_size_bytes=path.stat().st_size,
    )


def unit_scale_to_meters(unit_label: str | None) -> float:
    normalized = (unit_label or "").strip().lower()
    if normalized in {"inch", "inches", "in"}:
        return 0.0254
    if normalized in {"millimeter", "millimeters", "mm"}:
        return 0.001
    if normalized in {"centimeter", "centimeters", "cm"}:
        return 0.01
    if normalized in {"meter", "meters", "m"}:
        return 1.0
    return 1.0


def _round_triplet(values: tuple[float, float, float] | list[float]) -> list[float]:
    return [round(float(value), 12) for value in values]


def _merge_bounds(bounds_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not bounds_list:
        return None

    minimum = [
        min(float(bounds["min"][index]) for bounds in bounds_list)
        for index in range(3)
    ]
    maximum = [
        max(float(bounds["max"][index]) for bounds in bounds_list)
        for index in range(3)
    ]
    return {
        "min": _round_triplet(minimum),
        "max": _round_triplet(maximum),
        "size": _round_triplet([maximum[index] - minimum[index] for index in range(3)]),
    }


def _box_face_specs(minimum: list[float], maximum: list[float]) -> list[dict[str, Any]]:
    corners = {
        "000": [minimum[0], minimum[1], minimum[2]],
        "100": [maximum[0], minimum[1], minimum[2]],
        "110": [maximum[0], maximum[1], minimum[2]],
        "010": [minimum[0], maximum[1], minimum[2]],
        "001": [minimum[0], minimum[1], maximum[2]],
        "101": [maximum[0], minimum[1], maximum[2]],
        "111": [maximum[0], maximum[1], maximum[2]],
        "011": [minimum[0], maximum[1], maximum[2]],
    }
    size = [maximum[index] - minimum[index] for index in range(3)]
    return [
        {
            "name": "X-min face",
            "surface": {"kind": "plane", "dimensions": {"width": round(size[1], 12), "height": round(size[2], 12)}},
            "vertices": [corners[key] for key in ("000", "010", "011", "001")],
            "normal": [-1, 0, 0],
            "metadata": {"kind": "rectangular", "axis": "x", "side": "min"},
        },
        {
            "name": "X-max face",
            "surface": {"kind": "plane", "dimensions": {"width": round(size[1], 12), "height": round(size[2], 12)}},
            "vertices": [corners[key] for key in ("100", "101", "111", "110")],
            "normal": [1, 0, 0],
            "metadata": {"kind": "rectangular", "axis": "x", "side": "max"},
        },
        {
            "name": "Y-min face",
            "surface": {"kind": "plane", "dimensions": {"width": round(size[0], 12), "height": round(size[2], 12)}},
            "vertices": [corners[key] for key in ("000", "001", "101", "100")],
            "normal": [0, -1, 0],
            "metadata": {"kind": "rectangular", "axis": "y", "side": "min"},
        },
        {
            "name": "Y-max face",
            "surface": {"kind": "plane", "dimensions": {"width": round(size[0], 12), "height": round(size[2], 12)}},
            "vertices": [corners[key] for key in ("010", "110", "111", "011")],
            "normal": [0, 1, 0],
            "metadata": {"kind": "rectangular", "axis": "y", "side": "max"},
        },
        {
            "name": "Z-min face",
            "surface": {"kind": "plane", "dimensions": {"width": round(size[0], 12), "height": round(size[1], 12)}},
            "vertices": [corners[key] for key in ("000", "100", "110", "010")],
            "normal": [0, 0, -1],
            "metadata": {"kind": "rectangular", "axis": "z", "side": "min"},
        },
        {
            "name": "Z-max face",
            "surface": {"kind": "plane", "dimensions": {"width": round(size[0], 12), "height": round(size[1], 12)}},
            "vertices": [corners[key] for key in ("001", "011", "111", "101")],
            "normal": [0, 0, 1],
            "metadata": {"kind": "rectangular", "axis": "z", "side": "max"},
        },
    ]


def _infer_box_dimensions_from_edges(body_payload: dict[str, Any], scale: float) -> list[float] | None:
    counts: Counter[float] = Counter()
    for edge in body_payload.get("edges", []):
        if edge.get("type") != "Linear":
            continue
        counts[round(float(edge.get("length", 0.0)), 6)] += 1

    dimensions: list[float] = []
    for length, count in sorted(counts.items(), key=lambda item: item[0], reverse=True):
        repeat = max(1, round(count / 4))
        dimensions.extend([float(length) * scale] * repeat)

    if len(dimensions) >= 3:
        return dimensions[:3]
    if len(dimensions) == 1:
        return [dimensions[0], dimensions[0], dimensions[0]]
    return None


def _build_nx_box_body(
    body_payload: dict[str, Any],
    *,
    scale: float,
    body_index: int,
) -> tuple[dict[str, Any], dict[str, Any], list[list[float]], str] | None:
    dimensions = _infer_box_dimensions_from_edges(body_payload, scale)
    centroid_payload = body_payload.get("measurement", {}).get("centroid") or {}
    if not dimensions or not {"x", "y", "z"} <= centroid_payload.keys():
        return None

    centroid = [
        float(centroid_payload["x"]) * scale,
        float(centroid_payload["y"]) * scale,
        float(centroid_payload["z"]) * scale,
    ]
    minimum = [centroid[index] - dimensions[index] / 2 for index in range(3)]
    maximum = [centroid[index] + dimensions[index] / 2 for index in range(3)]
    bounds = {
        "min": _round_triplet(minimum),
        "max": _round_triplet(maximum),
        "size": _round_triplet(dimensions),
    }
    faces = _box_face_specs(minimum, maximum)
    points = [vertex for face in faces for vertex in face["vertices"]]
    label = body_payload.get("metadata", {}).get("shape_summary") or "Likely a rectangular prism / block."
    kernel_body = {
        "kind": "solid",
        "name": "rectangular prism",
        "faces": faces,
        "edges": [
            {
                "kind": "line",
                "name": f"{axis.upper()}-aligned edges",
                "axis": axis,
                "length": round(length, 12),
            }
            for axis, length in zip(("x", "y", "z"), dimensions)
        ],
        "vertices": [],
        "metadata": {
            "primitive": "box",
            "dimensions": {
                "x": round(dimensions[0], 12),
                "y": round(dimensions[1], 12),
                "z": round(dimensions[2], 12),
            },
            "source": "nx_json_import",
            "nx_body_index": body_index,
            "object_identity": (body_payload.get("metadata") or {}).get("object_identity"),
            "display_properties": (body_payload.get("metadata") or {}).get("display_properties"),
            "component_context": (body_payload.get("metadata") or {}).get("component_context"),
        },
    }
    return kernel_body, bounds, points, label


def _build_nx_cylinder_body(
    body_payload: dict[str, Any],
    *,
    scale: float,
    body_index: int,
) -> tuple[dict[str, Any], dict[str, Any], list[list[float]], str] | None:
    circular_edges = [
        float(edge.get("length", 0.0)) * scale
        for edge in body_payload.get("edges", [])
        if edge.get("type") == "Circular" and edge.get("length") is not None
    ]
    if not circular_edges:
        return None

    radius = sum(circular_edges) / len(circular_edges) / (2 * math.pi)
    if radius <= 0:
        return None

    measurement = body_payload.get("measurement", {})
    centroid_payload = measurement.get("centroid") or {}
    if not {"x", "y", "z"} <= centroid_payload.keys():
        return None

    volume = measurement.get("volume")
    surface_area = measurement.get("surface_area")
    volume_m3 = float(volume) * (scale ** 3) if volume is not None else None
    area_m2 = float(surface_area) * (scale ** 2) if surface_area is not None else None

    height = None
    if volume_m3 is not None and radius > 0:
        height = volume_m3 / (math.pi * radius * radius)
    if (height is None or height <= 0) and area_m2 is not None and radius > 0:
        height = max(area_m2 / (2 * math.pi * radius) - radius, 0.0)
    if height is None or height <= 0:
        return None

    center = [
        float(centroid_payload["x"]) * scale,
        float(centroid_payload["y"]) * scale,
        float(centroid_payload["z"]) * scale,
    ]
    minimum = [center[0] - radius, center[1] - radius, center[2] - height / 2]
    maximum = [center[0] + radius, center[1] + radius, center[2] + height / 2]
    bounds = {
        "min": _round_triplet(minimum),
        "max": _round_triplet(maximum),
        "size": _round_triplet([radius * 2, radius * 2, height]),
    }

    point_count = 24
    start_z = center[2] - height / 2
    end_z = center[2] + height / 2
    points = []
    for index in range(point_count):
        angle = (index / point_count) * math.pi * 2
        x = center[0] + math.cos(angle) * radius
        y = center[1] + math.sin(angle) * radius
        points.append(_round_triplet([x, y, start_z]))
        points.append(_round_triplet([x, y, end_z]))

    label = body_payload.get("metadata", {}).get("shape_summary") or "Likely a right circular cylinder."
    kernel_body = {
        "kind": "solid",
        "name": "cylinder",
        "faces": [],
        "edges": [
            {"kind": "circle", "name": "Start rim", "axis": "z", "radius": round(radius, 12)},
            {"kind": "circle", "name": "End rim", "axis": "z", "radius": round(radius, 12)},
        ],
        "vertices": [],
        "metadata": {
            "primitive": "cylinder",
            "axis": "z",
            "radius": round(radius, 12),
            "height": round(height, 12),
            "center": _round_triplet(center),
            "source": "nx_json_import",
            "nx_body_index": body_index,
            "object_identity": (body_payload.get("metadata") or {}).get("object_identity"),
            "display_properties": (body_payload.get("metadata") or {}).get("display_properties"),
            "component_context": (body_payload.get("metadata") or {}).get("component_context"),
        },
    }
    return kernel_body, bounds, points, label


def _box_body_from_center_and_dimensions(
    center: list[float],
    dimensions: list[float],
    *,
    body_index: int,
    source: str,
) -> tuple[dict[str, Any], list[list[float]]]:
    minimum = [center[index] - dimensions[index] / 2 for index in range(3)]
    maximum = [center[index] + dimensions[index] / 2 for index in range(3)]
    faces = _box_face_specs(minimum, maximum)
    points = [vertex for face in faces for vertex in face["vertices"]]
    body = {
        "kind": "solid",
        "name": "rectangular prism",
        "faces": faces,
        "edges": [
            {
                "kind": "line",
                "name": f"{axis.upper()}-aligned edges",
                "axis": axis,
                "length": round(length, 12),
            }
            for axis, length in zip(("x", "y", "z"), dimensions)
        ],
        "vertices": [],
        "metadata": {
            "primitive": "box",
            "dimensions": {
                "x": round(dimensions[0], 12),
                "y": round(dimensions[1], 12),
                "z": round(dimensions[2], 12),
            },
            "center": _round_triplet(center),
            "source": source,
            "nx_body_index": body_index,
        },
    }
    return body, points


def _cylinder_body_from_center_radius_height(
    center: list[float],
    radius: float,
    height: float,
    *,
    axis: str,
    body_index: int,
    source: str,
) -> tuple[dict[str, Any], list[list[float]]]:
    point_count = 24
    axis_index = {"x": 0, "y": 1, "z": 2}[axis]
    start = center.copy()
    end = center.copy()
    start[axis_index] -= height / 2
    end[axis_index] += height / 2
    points: list[list[float]] = []

    for index in range(point_count):
        angle = (index / point_count) * math.pi * 2
        if axis == "z":
            offset = [math.cos(angle) * radius, math.sin(angle) * radius, 0.0]
        elif axis == "x":
            offset = [0.0, math.cos(angle) * radius, math.sin(angle) * radius]
        else:
            offset = [math.cos(angle) * radius, 0.0, math.sin(angle) * radius]
        points.append(_round_triplet([start[i] + offset[i] for i in range(3)]))
        points.append(_round_triplet([end[i] + offset[i] for i in range(3)]))

    body = {
        "kind": "solid",
        "name": "cylinder",
        "faces": [],
        "edges": [
            {"kind": "circle", "name": "Start rim", "axis": axis, "radius": round(radius, 12)},
            {"kind": "circle", "name": "End rim", "axis": axis, "radius": round(radius, 12)},
        ],
        "vertices": [],
        "metadata": {
            "primitive": "cylinder",
            "axis": axis,
            "radius": round(radius, 12),
            "height": round(height, 12),
            "center": _round_triplet(center),
            "source": source,
            "nx_body_index": body_index,
        },
    }
    return body, points


def _translate_kernel_body(body: dict[str, Any], delta: list[float]) -> dict[str, Any]:
    shifted = json.loads(json.dumps(body))

    def shift_point(point: list[float]) -> list[float]:
        return _round_triplet([float(point[index]) + delta[index] for index in range(3)])

    for face in shifted.get("faces", []):
        if face.get("vertices"):
            face["vertices"] = [shift_point(vertex) for vertex in face["vertices"]]

    if shifted.get("vertices"):
        shifted["vertices"] = [shift_point(vertex) for vertex in shifted["vertices"]]

    metadata = shifted.get("metadata", {})
    if isinstance(metadata.get("center"), list) and len(metadata["center"]) == 3:
        metadata["center"] = shift_point(metadata["center"])

    for component in metadata.get("components", []):
        translated = _translate_kernel_body(component, delta)
        component.clear()
        component.update(translated)

    return shifted


def _dominant_axis_label(direction: dict[str, Any] | list[float] | None) -> str:
    if isinstance(direction, dict):
        values = [float(direction.get("x", 0.0)), float(direction.get("y", 0.0)), float(direction.get("z", 0.0))]
    elif isinstance(direction, list) and len(direction) >= 3:
        values = [float(direction[0]), float(direction[1]), float(direction[2])]
    else:
        return "z"

    index = max(range(3), key=lambda idx: abs(values[idx]))
    return ("x", "y", "z")[index]


def _scaled_bounding_box(body_payload: dict[str, Any], scale: float) -> dict[str, Any] | None:
    bounding_box = body_payload.get("bounding_box")
    if not isinstance(bounding_box, dict):
        return None
    required = ["x_min", "y_min", "z_min", "x_max", "y_max", "z_max"]
    if not all(key in bounding_box and bounding_box[key] is not None for key in required):
        return None
    minimum = [float(bounding_box["x_min"]) * scale, float(bounding_box["y_min"]) * scale, float(bounding_box["z_min"]) * scale]
    maximum = [float(bounding_box["x_max"]) * scale, float(bounding_box["y_max"]) * scale, float(bounding_box["z_max"]) * scale]
    return {
        "min": _round_triplet(minimum),
        "max": _round_triplet(maximum),
        "size": _round_triplet([maximum[index] - minimum[index] for index in range(3)]),
    }


def _scaled_topology_vertices(body_payload: dict[str, Any], scale: float) -> list[list[float]]:
    vertices = []
    topology = body_payload.get("topology") or {}
    for vertex in topology.get("vertices", []):
        point = vertex.get("point") or {}
        if {"x", "y", "z"} <= point.keys():
            vertices.append(
                [
                    float(point["x"]) * scale,
                    float(point["y"]) * scale,
                    float(point["z"]) * scale,
                ]
            )
    return vertices


def _scaled_topology_vertex_lookup(body_payload: dict[str, Any], scale: float) -> dict[int, list[float]]:
    lookup: dict[int, list[float]] = {}
    topology = body_payload.get("topology") or {}
    for vertex in topology.get("vertices", []):
        point = vertex.get("point") or {}
        index = vertex.get("index")
        if index is None or not {"x", "y", "z"} <= point.keys():
            continue
        lookup[int(index)] = [
            float(point["x"]) * scale,
            float(point["y"]) * scale,
            float(point["z"]) * scale,
        ]
    return lookup


def _polyline_bounds(points: list[list[float]]) -> dict[str, Any] | None:
    if not points:
        return None
    minimum = [min(point[index] for point in points) for index in range(3)]
    maximum = [max(point[index] for point in points) for index in range(3)]
    return {
        "min": _round_triplet(minimum),
        "max": _round_triplet(maximum),
        "size": _round_triplet([maximum[index] - minimum[index] for index in range(3)]),
    }


def _point_from_mapping(point_payload: dict[str, Any] | None, scale: float) -> list[float] | None:
    if not isinstance(point_payload, dict) or not {"x", "y", "z"} <= point_payload.keys():
        return None
    return [
        float(point_payload["x"]) * scale,
        float(point_payload["y"]) * scale,
        float(point_payload["z"]) * scale,
    ]


def _polyline_from_preview_payload(points_payload: Any, scale: float) -> list[list[float]] | None:
    if not isinstance(points_payload, list):
        return None
    polyline: list[list[float]] = []
    for point_payload in points_payload:
        point = _point_from_mapping(point_payload, scale)
        if point is None:
            continue
        rounded = _round_triplet(point)
        if polyline and polyline[-1] == rounded:
            continue
        polyline.append(rounded)
    return polyline if len(polyline) >= 2 else None


def _edge_polyline_from_payload(
    edge_payload: dict[str, Any],
    scale: float,
    vertex_lookup: dict[int, list[float]] | None = None,
) -> list[list[float]] | None:
    preview_polyline = _polyline_from_preview_payload(edge_payload.get("preview_points"), scale)
    if preview_polyline:
        return preview_polyline

    analytic_curve = ((edge_payload.get("exact_geometry") or {}).get("analytic_curve") or {})
    curve_type = analytic_curve.get("type")

    if curve_type == "Arc":
        center = _point_from_mapping(analytic_curve.get("center"), scale)
        radius = analytic_curve.get("radius")
        matrix = analytic_curve.get("matrix") or {}
        x_axis_payload = matrix.get("x_axis")
        y_axis_payload = matrix.get("y_axis")
        x_axis = _point_from_mapping(x_axis_payload, 1.0)
        y_axis = _point_from_mapping(y_axis_payload, 1.0)
        if center and radius is not None and x_axis and y_axis:
            start_angle = float(analytic_curve.get("start_angle", 0.0) or 0.0)
            end_angle = float(analytic_curve.get("end_angle", 0.0) or 0.0)
            sweep = end_angle - start_angle
            if sweep <= 1e-9 and edge_payload.get("start_vertex_index") == edge_payload.get("end_vertex_index"):
                sweep = 2 * math.pi
            elif sweep < 0:
                sweep += 2 * math.pi
            if sweep > 1e-6:
                segment_count = max(12, min(72, int(math.ceil(abs(sweep) / (math.pi / 18)))))
                radius = float(radius) * scale
                polyline: list[list[float]] = []
                for index in range(segment_count + 1):
                    angle = start_angle + (sweep * index / segment_count)
                    polyline.append(
                        _round_triplet(
                            [
                                center[axis] + radius * (math.cos(angle) * x_axis[axis] + math.sin(angle) * y_axis[axis])
                                for axis in range(3)
                            ]
                        )
                    )
                return polyline

    if curve_type == "Line":
        start_point = _point_from_mapping(analytic_curve.get("start_point"), scale)
        end_point = _point_from_mapping(analytic_curve.get("end_point"), scale)
        if start_point and end_point:
            return [_round_triplet(start_point), _round_triplet(end_point)]

    exact_vertices = edge_payload.get("exact_vertices") or {}
    start_point = _point_from_mapping(exact_vertices.get("start_point"), scale) or _point_from_mapping(edge_payload.get("start_point"), scale)
    end_point = _point_from_mapping(exact_vertices.get("end_point"), scale) or _point_from_mapping(edge_payload.get("end_point"), scale)
    if start_point and end_point:
        return [_round_triplet(start_point), _round_triplet(end_point)]

    if vertex_lookup:
        start_vertex = edge_payload.get("start_vertex_index")
        end_vertex = edge_payload.get("end_vertex_index")
        if start_vertex is not None and end_vertex is not None:
            start_point = vertex_lookup.get(int(start_vertex))
            end_point = vertex_lookup.get(int(end_vertex))
            if start_point and end_point:
                return [_round_triplet(start_point), _round_triplet(end_point)]

    return None


def _point_distance_sq(a: list[float], b: list[float]) -> float:
    return sum((float(a[index]) - float(b[index])) ** 2 for index in range(3))


def _closed_ring_from_polyline(polyline: list[list[float]] | None) -> list[list[float]] | None:
    if not polyline or len(polyline) < 3:
        return None

    ring = [_round_triplet(point) for point in polyline]
    if _point_distance_sq(ring[0], ring[-1]) <= 1e-12:
        ring = ring[:-1]
    if len(ring) < 3:
        return None
    return ring


def _polyline_length(points: list[list[float]]) -> float:
    if len(points) < 2:
        return 0.0
    return sum(math.sqrt(_point_distance_sq(points[index], points[(index + 1) % len(points)])) for index in range(len(points)))


def _vector_from_payload(payload: dict[str, Any] | list[float] | None) -> list[float] | None:
    if isinstance(payload, dict) and {"x", "y", "z"} <= payload.keys():
        return [float(payload["x"]), float(payload["y"]), float(payload["z"])]
    if isinstance(payload, list) and len(payload) >= 3:
        return [float(payload[0]), float(payload[1]), float(payload[2])]
    return None


def _normalize_vector(vector: list[float] | None) -> list[float] | None:
    if not vector:
        return None
    magnitude = math.sqrt(sum(component * component for component in vector))
    if magnitude <= 1e-9:
        return None
    return [component / magnitude for component in vector]


def _face_normal_from_vertices(vertices: list[list[float]]) -> list[float] | None:
    if len(vertices) < 3:
        return None

    anchor = vertices[0]
    for index in range(1, len(vertices) - 1):
        u = [vertices[index][axis] - anchor[axis] for axis in range(3)]
        v = [vertices[index + 1][axis] - anchor[axis] for axis in range(3)]
        normal = [
            u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0],
        ]
        magnitude = math.sqrt(sum(component * component for component in normal))
        if magnitude > 1e-9:
            return [component / magnitude for component in normal]
    return None


def _surface_kind_from_face_payload(face_payload: dict[str, Any]) -> str:
    face_type = str(face_payload.get("type") or "unknown").lower()
    return {
        "planar": "plane",
        "cylindrical": "cylinder",
        "conical": "cone",
        "surfaceofrevolution": "surface_of_revolution",
        "spherical": "sphere",
        "parametric": "parametric",
        "blending": "blend",
    }.get(face_type, face_type)


def _scaled_mapping(point_payload: dict[str, Any] | None, scale: float) -> dict[str, float] | None:
    point = _point_from_mapping(point_payload, scale)
    if point is None:
        return None
    return {"x": point[0], "y": point[1], "z": point[2]}


def _surface_payload_from_face(face_payload: dict[str, Any], scale: float) -> dict[str, Any]:
    surface_definition = dict(face_payload.get("surface_definition") or {})
    if not surface_definition:
        surface_definition = {"type": face_payload.get("type") or "Unknown"}

    surface_definition["kind"] = _surface_kind_from_face_payload(face_payload)
    for key in ("reference_point", "direction", "axis_point", "axis_direction", "sampled_normal"):
        scaled_value = _scaled_mapping(surface_definition.get(key), scale)
        if scaled_value is not None:
            surface_definition[key] = scaled_value

    if surface_definition.get("radius") is not None:
        surface_definition["radius"] = float(surface_definition["radius"]) * scale
    if surface_definition.get("radius_data") is not None and surface_definition.get("kind") not in {"cone", "surface_of_revolution"}:
        surface_definition["radius_data"] = float(surface_definition["radius_data"]) * scale

    return surface_definition


def _shift_ring(ring: list[list[float]], offset: int) -> list[list[float]]:
    if not ring:
        return []
    offset %= len(ring)
    return ring[offset:] + ring[:offset]


def _resample_ring(ring: list[list[float]], count: int) -> list[list[float]]:
    if not ring:
        return []
    if len(ring) == count:
        return [_round_triplet(point) for point in ring]
    return [_round_triplet(ring[int(index * len(ring) / count) % len(ring)]) for index in range(count)]


def _align_ring_pair(reference_ring: list[list[float]], target_ring: list[list[float]]) -> tuple[list[list[float]], list[list[float]]]:
    sample_count = max(12, min(72, max(len(reference_ring), len(target_ring))))
    reference = _resample_ring(reference_ring, sample_count)
    target_base = _resample_ring(target_ring, sample_count)

    def score(candidate: list[list[float]]) -> float:
        return sum(_point_distance_sq(reference[index], candidate[index]) for index in range(sample_count))

    best = target_base
    best_score = float("inf")
    for candidate_base in (target_base, list(reversed(target_base))):
        for offset in range(sample_count):
            candidate = _shift_ring(candidate_base, offset)
            candidate_score = score(candidate)
            if candidate_score < best_score:
                best = candidate
                best_score = candidate_score

    return reference, best


def _stitch_loop_polyline(loop_payload: dict[str, Any], edge_polyline_lookup: dict[int, list[list[float]]]) -> list[list[float]] | None:
    stitched: list[list[float]] = []

    for coedge in loop_payload.get("coedges", []):
        edge_index = coedge.get("edge_index")
        if edge_index is None:
            continue
        polyline = edge_polyline_lookup.get(int(edge_index))
        if not polyline or len(polyline) < 2:
            continue

        segment = polyline if coedge.get("sense") != "backward" else list(reversed(polyline))
        if not stitched:
            stitched = [_round_triplet(point) for point in segment]
            continue

        if _point_distance_sq(stitched[-1], segment[0]) <= 1e-12:
            stitched.extend(_round_triplet(point) for point in segment[1:])
        elif _point_distance_sq(stitched[-1], segment[-1]) <= 1e-12:
            reversed_segment = list(reversed(segment))
            stitched.extend(_round_triplet(point) for point in reversed_segment[1:])
        else:
            stitched.extend(_round_triplet(point) for point in segment)

    return _closed_ring_from_polyline(stitched)


def _facet_meshes_from_payload(
    face_payload: dict[str, Any],
    surface_payload: dict[str, Any],
    base_metadata: dict[str, Any],
    *,
    scale: float,
) -> list[dict[str, Any]]:
    facet_mesh = face_payload.get("facet_mesh") or {}
    triangles = facet_mesh.get("triangles") or []
    meshes: list[dict[str, Any]] = []
    face_index = face_payload.get("index", "?")
    for triangle_index, triangle in enumerate(triangles, start=1):
        vertices = []
        for point_payload in triangle.get("vertices") or []:
            point = _point_from_mapping(point_payload, scale)
            if point is not None:
                vertices.append(_round_triplet(point))
        if len(vertices) != 3:
            continue
        normal = _vector_from_payload(triangle.get("normal"))
        meshes.append(
            {
                "name": f"Face {face_index} facet {triangle_index}",
                "surface": surface_payload,
                "vertices": vertices,
                "normal": _normalize_vector(normal) or _face_normal_from_vertices(vertices),
                "loops": [],
                "metadata": {
                    **base_metadata,
                    "kind": "facet_triangle",
                    "triangle_index": triangle_index,
                    "facet_mesh_source": facet_mesh.get("source"),
                },
            }
        )
    return meshes


def _face_meshes_from_payload(
    face_payload: dict[str, Any],
    edge_polyline_lookup: dict[int, list[list[float]]],
    *,
    scale: float,
) -> list[dict[str, Any]]:
    face_index = face_payload.get("index", "?")
    surface_payload = _surface_payload_from_face(face_payload, scale)
    analytic_data = face_payload.get("analytic_data") or {}
    face_normal = (
        _normalize_vector(_vector_from_payload(surface_payload.get("sampled_normal")))
        or _normalize_vector(_vector_from_payload(surface_payload.get("direction")))
        or _normalize_vector(_vector_from_payload(analytic_data.get("direction")))
    )
    axis = _dominant_axis_label(face_normal)
    base_metadata = {
        "kind": "nx_imported_face",
        "axis": axis,
        "source_face_index": face_payload.get("index"),
        "source_face_type": face_payload.get("type"),
        "object_identity": face_payload.get("object_identity"),
        "display_properties": face_payload.get("display_properties"),
        "component_context": face_payload.get("component_context"),
        "surface_definition": surface_payload,
        "uv_bounds": face_payload.get("uv_bounds"),
        "periodicity": face_payload.get("periodicity"),
        "topology": face_payload.get("topology"),
        "trimmed_bsurface": face_payload.get("trimmed_bsurface"),
        "facet_mesh": face_payload.get("facet_mesh"),
        "adjacent_face_indices": face_payload.get("adjacent_face_indices", []),
        "loop_count": 0,
    }
    facet_meshes = _facet_meshes_from_payload(face_payload, surface_payload, base_metadata, scale=scale)
    if facet_meshes and surface_payload["kind"] in {"parametric", "blend", "surface_of_revolution", "sphere", "cone"}:
        return facet_meshes

    loop_rings: list[tuple[dict[str, Any], list[list[float]]]] = []
    for loop_payload in face_payload.get("loops", []):
        has_closed_coedge = False
        for coedge in loop_payload.get("coedges", []):
            edge_index = coedge.get("edge_index")
            if edge_index is None:
                continue
            polyline = edge_polyline_lookup.get(int(edge_index))
            if not polyline:
                continue
            oriented = polyline if coedge.get("sense") != "backward" else list(reversed(polyline))
            ring = _closed_ring_from_polyline(oriented)
            if ring:
                loop_rings.append((loop_payload, ring))
                has_closed_coedge = True
        if not has_closed_coedge:
            stitched = _stitch_loop_polyline(loop_payload, edge_polyline_lookup)
            if stitched:
                loop_rings.append((loop_payload, stitched))

    if not loop_rings:
        return facet_meshes

    rings = [ring for _, ring in loop_rings]
    base_metadata["loop_count"] = len(loop_rings)
    meshes: list[dict[str, Any]] = []

    if surface_payload["kind"] == "plane":
        if len(rings) == 1:
            ring = rings[0]
            meshes.append(
                {
                    "name": f"Face {face_index}",
                    "surface": surface_payload,
                    "vertices": ring,
                    "normal": face_normal or _face_normal_from_vertices(ring),
                    "loops": [{"id": f"L{face_index}-1", "vertices": ring}],
                    "metadata": {**base_metadata, "kind": "planar_loop"},
                }
            )
            return meshes

        ordered_rings = sorted(rings, key=_polyline_length, reverse=True)
        outer, inner = _align_ring_pair(ordered_rings[0], ordered_rings[1])
        patch_normal = face_normal or _face_normal_from_vertices(outer)
        for index in range(len(outer)):
            next_index = (index + 1) % len(outer)
            quad = [outer[index], outer[next_index], inner[next_index], inner[index]]
            meshes.append(
                {
                    "name": f"Face {face_index} patch {index + 1}",
                    "surface": surface_payload,
                    "vertices": quad,
                    "normal": patch_normal,
                    "loops": [
                        {"id": f"L{face_index}-outer", "vertices": outer},
                        {"id": f"L{face_index}-inner", "vertices": inner},
                    ],
                    "metadata": {**base_metadata, "kind": "planar_ring_patch", "patch_index": index + 1},
                }
            )
        return meshes

    if surface_payload["kind"] in {"cylinder", "cone", "surface_of_revolution", "sphere", "parametric", "blend"} and len(rings) >= 2:
        ordered_rings = sorted(rings, key=_polyline_length, reverse=True)
        outer, inner = _align_ring_pair(ordered_rings[0], ordered_rings[1])
        for index in range(len(outer)):
            next_index = (index + 1) % len(outer)
            quad = [outer[index], outer[next_index], inner[next_index], inner[index]]
            meshes.append(
                {
                    "name": f"Face {face_index} patch {index + 1}",
                    "surface": surface_payload,
                    "vertices": quad,
                    "normal": face_normal or _face_normal_from_vertices(quad),
                    "loops": [
                        {"id": f"L{face_index}-outer", "vertices": outer},
                        {"id": f"L{face_index}-inner", "vertices": inner},
                    ],
                    "metadata": {**base_metadata, "kind": "trimmed_surface_patch", "patch_index": index + 1},
                }
            )
        return meshes

    if len(rings) == 1:
        ring = rings[0]
        meshes.append(
            {
                "name": f"Face {face_index}",
                "surface": surface_payload,
                "vertices": ring,
                "normal": face_normal or _face_normal_from_vertices(ring),
                "loops": [{"id": f"L{face_index}-1", "vertices": ring}],
                "metadata": {**base_metadata, "kind": "trim_boundary_polygon"},
            }
        )
        return meshes

    return meshes


def _build_nx_wireframe_body(
    body_payload: dict[str, Any],
    *,
    scale: float,
    body_index: int,
) -> tuple[dict[str, Any], dict[str, Any], list[list[float]], str] | None:
    vertex_lookup = _scaled_topology_vertex_lookup(body_payload, scale)
    topology_vertices = [_round_triplet(point) for point in vertex_lookup.values()]
    edge_polylines: list[dict[str, Any]] = []
    edge_polyline_lookup: dict[int, list[list[float]]] = {}
    polyline_points: list[list[float]] = []

    for edge_index, edge_payload in enumerate(body_payload.get("edges", []), start=1):
        polyline = _edge_polyline_from_payload(edge_payload, scale, vertex_lookup)
        if not polyline or len(polyline) < 2:
            continue
        edge_identifier = int(edge_payload.get("index", edge_index))
        edge_polyline_lookup[edge_identifier] = polyline
        edge_kind = str(((edge_payload.get("exact_geometry") or {}).get("analytic_curve") or {}).get("type") or edge_payload.get("type") or "edge").lower()
        edge_polylines.append(
            {
                "kind": edge_kind,
                "name": f"Edge {edge_identifier}",
                "points": polyline,
                "object_identity": edge_payload.get("object_identity"),
                "display_properties": edge_payload.get("display_properties"),
                "component_context": edge_payload.get("component_context"),
            }
        )
        polyline_points.extend(polyline)

    preview_points = topology_vertices + polyline_points
    if not preview_points:
        return None

    bounds = _scaled_bounding_box(body_payload, scale) or _polyline_bounds(preview_points)
    if not bounds:
        return None

    preview_faces: list[dict[str, Any]] = []
    for face_payload in body_payload.get("faces", []):
        preview_faces.extend(_face_meshes_from_payload(face_payload, edge_polyline_lookup, scale=scale))

    label = (
        "Surface-enhanced topology preview reconstructed from NX face loops, exact edge curves, and imported surface definitions."
        if preview_faces
        else "Topology wireframe preview reconstructed from NX edge and vertex data."
    )
    kernel_body = {
        "kind": "solid" if preview_faces else "wireframe",
        "name": body_payload.get("metadata", {}).get("name") or f"Body {body_index}",
        "faces": preview_faces,
        "edges": edge_polylines,
        "vertices": topology_vertices,
        "metadata": {
            "primitive": "nx_brep_import" if preview_faces else "wireframe",
            "source": "nx_surface_topology_preview" if preview_faces else "nx_topology_wireframe",
            "nx_body_index": body_index,
            "object_identity": (body_payload.get("metadata") or {}).get("object_identity"),
            "display_properties": (body_payload.get("metadata") or {}).get("display_properties"),
            "component_context": (body_payload.get("metadata") or {}).get("component_context"),
            "edge_count": len(edge_polylines),
            "vertex_count": len(topology_vertices),
            "face_patch_count": len(preview_faces),
            "source_face_count": len(body_payload.get("faces", [])),
        },
    }
    return kernel_body, bounds, preview_points, label


def _build_exact_compound_from_rich_body(
    body_payload: dict[str, Any],
    *,
    scale: float,
    body_index: int,
) -> tuple[dict[str, Any], dict[str, Any], list[list[float]], str] | None:
    topology_vertices = _scaled_topology_vertices(body_payload, scale)
    if not topology_vertices:
        return None

    circle_edges = []
    for edge in body_payload.get("edges", []):
        analytic = ((edge.get("exact_geometry") or {}).get("analytic_curve") or {})
        if analytic.get("type") != "Arc":
            continue
        center = analytic.get("center") or {}
        if not {"x", "y", "z"} <= center.keys():
            continue
        radius = analytic.get("radius")
        if radius is None:
            continue
        start_angle = analytic.get("start_angle")
        end_angle = analytic.get("end_angle")
        sweep = None
        if start_angle is not None and end_angle is not None:
            sweep = abs(float(end_angle) - float(start_angle))
        circle_edges.append(
            {
                "center": [float(center["x"]) * scale, float(center["y"]) * scale, float(center["z"]) * scale],
                "radius": float(radius) * scale,
                "sweep": sweep,
                "matrix": analytic.get("matrix"),
                "edge": edge,
            }
        )

    if not circle_edges:
        return None

    full_circle = next(
        (
            item
            for item in circle_edges
            if item["sweep"] is not None and abs(item["sweep"] - (2 * math.pi)) <= 0.05
        ),
        None,
    )
    if full_circle is None:
        full_circle = next(
            (
                item
                for item in circle_edges
                if item["edge"].get("start_vertex_index") == item["edge"].get("end_vertex_index")
            ),
            None,
        )
    if full_circle is None:
        return None

    axis = "z"
    cylindrical_face = next((face for face in body_payload.get("faces", []) if face.get("type") == "Cylindrical"), None)
    if cylindrical_face:
        surface_definition = cylindrical_face.get("surface_definition") or {}
        axis = _dominant_axis_label(surface_definition.get("axis_direction") or surface_definition.get("direction"))
    elif full_circle.get("matrix"):
        axis = _dominant_axis_label(full_circle["matrix"].get("z_axis"))

    axis_index = {"x": 0, "y": 1, "z": 2}[axis]
    center = full_circle["center"]
    radius = full_circle["radius"]
    if radius <= 0:
        return None

    bounds = _scaled_bounding_box(body_payload, scale)
    if bounds:
        axis_min = float(bounds["min"][axis_index])
        axis_max = float(bounds["max"][axis_index])
    else:
        axis_values = [point[axis_index] for point in topology_vertices]
        axis_min = min(axis_values)
        axis_max = max(axis_values)

    if axis_max <= axis_min:
        return None

    cylinder_center = center.copy()
    cylinder_center[axis_index] = (axis_min + axis_max) / 2
    cylinder_height = axis_max - axis_min
    cylinder_body, cylinder_points = _cylinder_body_from_center_radius_height(
        cylinder_center,
        radius,
        cylinder_height,
        axis=axis,
        body_index=body_index,
        source="nx_exact_topology_preview",
    )

    unique_axis_levels = sorted({round(point[axis_index], 12) for point in topology_vertices})
    box_top = None
    if len(unique_axis_levels) >= 2:
        for candidate in unique_axis_levels:
            if candidate > axis_min + 1e-6:
                box_top = candidate
                break
    if box_top is None or box_top >= axis_max:
        return None

    box_extents_min = [min(point[index] for point in topology_vertices) for index in range(3)]
    box_extents_max = [max(point[index] for point in topology_vertices) for index in range(3)]
    for index in range(3):
        if index == axis_index:
            box_extents_min[index] = axis_min
            box_extents_max[index] = box_top
        else:
            box_extents_max[index] = center[index]

    if any(box_extents_max[index] <= box_extents_min[index] for index in range(3)):
        return None

    box_center = [(box_extents_min[index] + box_extents_max[index]) / 2 for index in range(3)]
    box_dimensions = [box_extents_max[index] - box_extents_min[index] for index in range(3)]
    box_body, box_points = _box_body_from_center_and_dimensions(
        box_center,
        box_dimensions,
        body_index=body_index,
        source="nx_exact_topology_preview",
    )
    inherited_metadata = {
        "object_identity": (body_payload.get("metadata") or {}).get("object_identity"),
        "display_properties": (body_payload.get("metadata") or {}).get("display_properties"),
        "component_context": (body_payload.get("metadata") or {}).get("component_context"),
    }
    box_body["metadata"].update(inherited_metadata)
    cylinder_body["metadata"].update(inherited_metadata)

    points = box_points + cylinder_points
    merged_bounds = bounds or _merge_bounds(
        [
            {
                "min": [min(point[index] for point in box_points) for index in range(3)],
                "max": [max(point[index] for point in box_points) for index in range(3)],
            },
            {
                "min": [min(point[index] for point in cylinder_points) for index in range(3)],
                "max": [max(point[index] for point in cylinder_points) for index in range(3)],
            },
        ]
    )

    label = "Exact block + cylinder preview from topology/edge geometry."
    compound_body = {
        "kind": "solid",
        "name": "compound cylinder + block",
        "faces": [],
        "edges": [],
        "vertices": [],
        "metadata": {
            "primitive": "compound",
            "components": [box_body, cylinder_body],
            "source": "nx_exact_topology_preview",
            "nx_body_index": body_index,
            "object_identity": (body_payload.get("metadata") or {}).get("object_identity"),
            "display_properties": (body_payload.get("metadata") or {}).get("display_properties"),
            "component_context": (body_payload.get("metadata") or {}).get("component_context"),
        },
    }
    return compound_body, merged_bounds, points, label


def _build_nx_compound_block_cylinder_body(
    body_payload: dict[str, Any],
    *,
    scale: float,
    body_index: int,
) -> tuple[dict[str, Any], dict[str, Any], list[list[float]], str] | None:
    circular_lengths = sorted(
        [
            float(edge.get("length", 0.0)) * scale / (2 * math.pi)
            for edge in body_payload.get("edges", [])
            if edge.get("type") == "Circular" and edge.get("length") is not None
        ]
    )
    if not circular_lengths:
        return None

    linear_lengths = sorted(
        {
            round(float(edge.get("length", 0.0)) * scale, 12)
            for edge in body_payload.get("edges", [])
            if edge.get("type") == "Linear" and edge.get("length") is not None
        },
        reverse=True,
    )
    if len(linear_lengths) < 3:
        return None

    radius = max(circular_lengths)
    box_length = linear_lengths[0]
    common_depth = linear_lengths[1]
    box_height = linear_lengths[2]
    cylinder_height = common_depth
    if radius <= 0 or box_length <= 0 or common_depth <= 0 or box_height <= 0:
        return None

    box_center = [-(radius + box_length / 2), 0.0, box_height / 2]
    cylinder_center = [0.0, 0.0, cylinder_height / 2]
    box_body, box_points = _box_body_from_center_and_dimensions(
        box_center,
        [box_length, common_depth, box_height],
        body_index=body_index,
        source="nx_json_compound_preview",
    )
    cylinder_body, cylinder_points = _cylinder_body_from_center_radius_height(
        cylinder_center,
        radius,
        cylinder_height,
        axis="z",
        body_index=body_index,
        source="nx_json_compound_preview",
    )

    measurement = body_payload.get("measurement", {})
    centroid_payload = measurement.get("centroid") or {}
    if {"x", "y", "z"} <= centroid_payload.keys():
        desired_centroid = [
            float(centroid_payload["x"]) * scale,
            float(centroid_payload["y"]) * scale,
            float(centroid_payload["z"]) * scale,
        ]
        box_volume = box_length * common_depth * box_height
        cylinder_volume = math.pi * radius * radius * cylinder_height
        approx_centroid = [
            (box_center[index] * box_volume + cylinder_center[index] * cylinder_volume) / (box_volume + cylinder_volume)
            for index in range(3)
        ]
        delta = [desired_centroid[index] - approx_centroid[index] for index in range(3)]
        box_body = _translate_kernel_body(box_body, delta)
        cylinder_body = _translate_kernel_body(cylinder_body, delta)
        box_points = [_round_triplet([point[i] + delta[i] for i in range(3)]) for point in box_points]
        cylinder_points = [_round_triplet([point[i] + delta[i] for i in range(3)]) for point in cylinder_points]

    inherited_metadata = {
        "object_identity": (body_payload.get("metadata") or {}).get("object_identity"),
        "display_properties": (body_payload.get("metadata") or {}).get("display_properties"),
        "component_context": (body_payload.get("metadata") or {}).get("component_context"),
    }
    box_body["metadata"].update(inherited_metadata)
    cylinder_body["metadata"].update(inherited_metadata)

    points = box_points + cylinder_points
    bounds = _merge_bounds(
        [
            {
                "min": [min(point[index] for point in box_points) for index in range(3)],
                "max": [max(point[index] for point in box_points) for index in range(3)],
            },
            {
                "min": [min(point[index] for point in cylinder_points) for index in range(3)],
                "max": [max(point[index] for point in cylinder_points) for index in range(3)],
            },
        ]
    )
    label = "Likely a block joined to a vertical cylinder."
    compound_body = {
        "kind": "solid",
        "name": "compound cylinder + block",
        "faces": [],
        "edges": [],
        "vertices": [],
        "metadata": {
            "primitive": "compound",
            "components": [box_body, cylinder_body],
            "source": "nx_json_compound_preview",
            "nx_body_index": body_index,
            "object_identity": (body_payload.get("metadata") or {}).get("object_identity"),
            "display_properties": (body_payload.get("metadata") or {}).get("display_properties"),
            "component_context": (body_payload.get("metadata") or {}).get("component_context"),
        },
    }
    return compound_body, bounds, points, label


def _build_nx_body(
    body_payload: dict[str, Any],
    *,
    scale: float,
    body_index: int,
) -> tuple[dict[str, Any], dict[str, Any], list[list[float]], str] | None:
    shape_summary = str(body_payload.get("metadata", {}).get("shape_summary") or "").lower()
    face_breakdown = body_payload.get("face_type_breakdown") or {}
    edge_breakdown = body_payload.get("edge_type_breakdown") or {}
    face_types = {str(name).lower() for name in face_breakdown}
    edge_types = {str(name).lower() for name in edge_breakdown}
    total_faces = sum(int(value) for value in face_breakdown.values())
    total_edges = sum(int(value) for value in edge_breakdown.values())
    circular_edges = int(edge_breakdown.get("Circular", edge_breakdown.get("circular", 0)) or 0)
    linear_edges = int(edge_breakdown.get("Linear", edge_breakdown.get("linear", 0)) or 0)
    cylindrical_faces = int(face_breakdown.get("Cylindrical", face_breakdown.get("cylindrical", 0)) or 0)
    looks_like_simple_cylinder = (
        cylindrical_faces == 1
        and circular_edges == 2
        and total_faces == 3
    )
    looks_like_simple_box = (
        total_faces == 6
        and total_edges == 12
        and face_types == {"planar"}
        and edge_types == {"linear"}
        and linear_edges == 12
    )
    looks_like_compound_cylinder_block = (
        cylindrical_faces == 1
        and total_faces >= 8
        and circular_edges >= 3
        and linear_edges >= 8
    )
    has_complex_face_types = bool(
        face_types.intersection({"parametric", "blending", "surfaceofrevolution", "conical", "spherical"})
    )
    rich_preview = _build_nx_wireframe_body(body_payload, scale=scale, body_index=body_index)

    if rich_preview and (has_complex_face_types or total_faces > 16 or total_edges > 48):
        return rich_preview

    if looks_like_compound_cylinder_block:
        built = _build_nx_compound_block_cylinder_body(body_payload, scale=scale, body_index=body_index)
        if built:
            return built

    if looks_like_simple_cylinder or "right circular cylinder" in shape_summary:
        built = _build_nx_cylinder_body(body_payload, scale=scale, body_index=body_index)
        if built:
            return built

    if looks_like_simple_box or "rectangular prism" in shape_summary:
        built = _build_nx_box_body(body_payload, scale=scale, body_index=body_index)
        if built:
            return built

    exact_built = _build_exact_compound_from_rich_body(body_payload, scale=scale, body_index=body_index)
    if exact_built:
        return exact_built

    return rich_preview


def normalize_existing_report(
    payload: dict[str, Any],
    *,
    display_name: str,
    source_path: str | None,
    file_size_bytes: int | None,
    raw_text: str,
) -> dict[str, Any]:
    report = dict(payload)
    report.setdefault("file", source_path or display_name)
    report.setdefault("file_name", display_name)
    report.setdefault("file_size_bytes", file_size_bytes if file_size_bytes is not None else len(raw_text.encode()))
    report.setdefault("line_count", raw_text.count("\n") + (1 if raw_text else 0))
    report.setdefault("header", {})
    report.setdefault("transmit_info", {})
    report.setdefault("decoded_names", [])
    report.setdefault("density", None)
    report.setdefault("colors", [])
    report.setdefault("has_scale_factor_attribute", False)
    report.setdefault("object_state_ids", [])
    report.setdefault("structured_parse", None)
    report.setdefault("entity_hints", {})
    report.setdefault("kernel_body", None)
    report.setdefault("kernel_bodies", [])
    report.setdefault("kernel_topology", None)
    report.setdefault(
        "geometry_hints",
        {
            "point_count": 0,
            "point_samples": [],
            "preview_points": [],
            "bounds": None,
            "notable_scalar_values": [],
            "unit_inference": None,
        },
    )
    report.setdefault(
        "model_analysis",
        {
            "unique_axis_levels": {"x": [], "y": [], "z": []},
            "curve_hints": [],
            "topology": None,
            "summary": [],
        },
    )
    return report


def _structured_body_summaries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for fallback_index, body_payload in enumerate(payload.get("bodies", []), start=1):
        face_breakdown = body_payload.get("face_type_breakdown") or {}
        edge_breakdown = body_payload.get("edge_type_breakdown") or {}
        summaries.append(
            {
                "index": int(body_payload.get("index", fallback_index)),
                "name": body_payload.get("metadata", {}).get("name") or f"Body {fallback_index}",
                "shape_summary": body_payload.get("metadata", {}).get("shape_summary") or "Unknown body shape.",
                "face_count": sum(int(value) for value in face_breakdown.values()),
                "edge_count": sum(int(value) for value in edge_breakdown.values()),
            }
        )
    return summaries


def _convert_structured_body_report(
    payload: dict[str, Any],
    *,
    display_name: str,
    source_path: str | None,
    file_size_bytes: int | None,
    raw_text: str,
    source_format: str,
    application_label: str,
    schema_label: str,
) -> dict[str, Any]:
    part_summary = payload.get("part_summary") or {}
    part_name = part_summary.get("part_name") or part_summary.get("leaf") or display_name
    units = part_summary.get("units") or "Unknown"
    scale = unit_scale_to_meters(units)

    kernel_bodies: list[dict[str, Any]] = []
    imported_body_summaries = _structured_body_summaries(payload)
    bounds_list: list[dict[str, Any]] = []
    preview_points: list[list[float]] = []
    edge_lengths: list[float] = []

    for body_payload in payload.get("bodies", []):
        body_index = int(body_payload.get("index", len(kernel_bodies) + 1))
        built = _build_nx_body(body_payload, scale=scale, body_index=body_index)
        if not built:
            continue
        kernel_body, bounds, body_points, label = built
        kernel_bodies.append(kernel_body)
        bounds_list.append(bounds)
        preview_points.extend(body_points)
        edge_lengths.extend(
            [
                float(edge.get("length", 0.0)) * scale
                for edge in body_payload.get("edges", [])
                if edge.get("length") is not None
            ]
        )
        for summary in imported_body_summaries:
            if summary["index"] == body_index:
                summary["shape_summary"] = label
                break

    merged_bounds = _merge_bounds(bounds_list)
    notable_scalars: list[dict[str, Any]] = []
    scalar_counts: Counter[float] = Counter(round(length, 12) for length in edge_lengths if length > 0)
    for value, frequency in scalar_counts.most_common(10):
        notable_scalars.append(
            {
                "value": value,
                "frequency": frequency,
                "assuming_meters": {
                    "millimeters": round(value * 1000.0, 6),
                    "inches": round(value * 39.37007874015748, 6),
                },
            }
        )

    density_value = None
    if payload.get("bodies"):
        density_value = payload["bodies"][0].get("metadata", {}).get("density")

    header = {
        "PART1": {
            "APPL": application_label,
            "DATE": "",
            "UNITS": str(units),
            "PART": str(part_name),
        },
        "PART2": {
            "SCH": schema_label,
        },
    }
    if part_summary.get("full_path"):
        header["PART1"]["SOURCE_PATH"] = str(part_summary["full_path"])

    body_count = int(payload.get("body_count") or len(payload.get("bodies", [])))
    summary = [
        f"Imported {application_label} for {part_name}.",
        f"Detected {body_count} body/bodies and reconstructed {len(kernel_bodies)} previewable body preview(s).",
        f"Source units were {units}; imported geometry was converted to meters for consistent viewer measurements.",
    ]
    for body_summary in imported_body_summaries[:6]:
        summary.append(
            f"Body {body_summary['index']}: {body_summary['shape_summary']} ({body_summary['face_count']} faces, {body_summary['edge_count']} edges)."
        )

    report: dict[str, Any] = {
        "file": source_path or display_name,
        "file_name": display_name,
        "file_size_bytes": file_size_bytes if file_size_bytes is not None else len(raw_text.encode()),
        "line_count": raw_text.count("\n") + (1 if raw_text else 0),
        "header": header,
        "transmit_info": {
            "source_format": source_format,
            "original_units": str(units),
        },
        "decoded_names": unique_preserve_order(
            [value for value in [part_summary.get("leaf"), part_summary.get("part_name")] if value]
        ),
        "density": (
            {
                "value": float(density_value),
                "unit": "kg/m^3",
            }
            if density_value is not None
            else None
        ),
        "colors": [],
        "has_scale_factor_attribute": False,
        "object_state_ids": [],
        "structured_parse": None,
        "entity_hints": {
            "source": source_format,
            "body_count": body_count,
        },
        "kernel_body": kernel_bodies[0] if len(kernel_bodies) == 1 else None,
        "kernel_bodies": kernel_bodies,
        "kernel_topology": (
            build_topology_from_kernel_body(kernel_bodies[0])
            if len(kernel_bodies) == 1 and kernel_bodies[0].get("metadata", {}).get("primitive") != "compound"
            else None
        ),
        "geometry_hints": {
            "point_count": len(preview_points),
            "point_samples": preview_points[:12],
            "preview_points": preview_points[:500],
            "bounds": merged_bounds,
            "notable_scalar_values": notable_scalars,
            "unit_inference": f"Original NX report units: {units}. Geometry was normalized to meters for preview and comparison.",
        },
        "model_analysis": {
            "unique_axis_levels": summarize_unique_levels(
                [tuple(point) for point in preview_points[:500]]
            ) if preview_points else {"x": [], "y": [], "z": []},
            "curve_hints": [],
            "topology": None,
            "summary": summary,
        },
        "nx_import": {
            "part_summary": part_summary,
            "part_attributes": payload.get("part_attributes", []),
            "body_count": body_count,
            "inter_body_distances": payload.get("inter_body_distances", []),
            "body_summaries": imported_body_summaries,
        },
    }
    return report


def convert_nx_json_report(
    payload: dict[str, Any],
    *,
    display_name: str,
    source_path: str | None,
    file_size_bytes: int | None,
    raw_text: str,
) -> dict[str, Any]:
    return _convert_structured_body_report(
        payload,
        display_name=display_name,
        source_path=source_path,
        file_size_bytes=file_size_bytes,
        raw_text=raw_text,
        source_format="siemens_nx_json",
        application_label="Siemens NX JSON Import",
        schema_label="NX-JSON",
    )


def _legacy_structured_payload_from_cpp_export(payload: dict[str, Any]) -> dict[str, Any]:
    part = payload.get("part") or {}
    part_summary = {
        "part_name": part.get("name") or part.get("part_name"),
        "full_path": part.get("full_path"),
        "leaf": part.get("leaf"),
        "units": part.get("units"),
        "tag": part.get("tag"),
        "read_only": part.get("read_only"),
        "fully_loaded": part.get("fully_loaded"),
        "has_write_access": part.get("has_write_access"),
        "unique_identifier": part.get("unique_identifier"),
    }

    body_payloads = []
    for fallback_index, body in enumerate(payload.get("bodies", []), start=1):
        body_payloads.append(
            {
                "index": body.get("index", fallback_index),
                "metadata": body.get("metadata") or {},
                "measurement": body.get("measurement"),
                "bounding_box": body.get("bounding_box"),
                "attributes": body.get("attributes") or [],
                "topology": body.get("topology") or {},
                "features": body.get("features") or [],
                "edge_type_breakdown": body.get("edge_type_breakdown") or {},
                "edges": body.get("edges") or [],
                "face_type_breakdown": body.get("face_type_breakdown") or {},
                "faces": body.get("faces") or [],
            }
        )

    legacy_payload = {
        "export_metadata": payload.get("export_metadata") or {},
        "part_summary": part_summary,
        "part_attributes": part.get("attributes") or payload.get("part_attributes") or [],
        "body_count": int(payload.get("body_count") or len(body_payloads)),
        "inter_body_distances": payload.get("inter_body_distances") or [],
        "bodies": body_payloads,
    }
    return legacy_payload


def convert_nx_cpp_export_report(
    payload: dict[str, Any],
    *,
    display_name: str,
    source_path: str | None,
    file_size_bytes: int | None,
    raw_text: str,
) -> dict[str, Any]:
    legacy_payload = _legacy_structured_payload_from_cpp_export(payload)
    report = _convert_structured_body_report(
        legacy_payload,
        display_name=display_name,
        source_path=source_path,
        file_size_bytes=file_size_bytes,
        raw_text=raw_text,
        source_format="nx_brep_export_cpp_json",
        application_label="Siemens NX C++ BREP Export",
        schema_label="NX-BREP-EXPORT-v1",
    )
    report["nx_cpp_export"] = {
        "schema": payload.get("schema"),
        "generator": payload.get("generator") or {},
        "export_metadata": payload.get("export_metadata") or {},
    }
    return report


def parse_scalar_value(raw_value: str) -> Any:
    value = raw_value.strip()
    if value in {"None", "null", ""}:
        return None
    if value == "True":
        return True
    if value == "False":
        return False
    try:
        if re.fullmatch(r"[+-]?\d+", value):
            return int(value)
        if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)(?:e[+-]?\d+)?", value, flags=re.IGNORECASE):
            return float(value)
    except ValueError:
        return value
    return value


def parse_centroid_triplet(raw_value: str) -> dict[str, float] | None:
    match = re.match(
        r"\(\s*(" + NUMBER_PATTERN + r")\s*,\s*(" + NUMBER_PATTERN + r")\s*,\s*(" + NUMBER_PATTERN + r")\s*\)",
        raw_value.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return {
        "x": float(match.group(1)),
        "y": float(match.group(2)),
        "z": float(match.group(3)),
    }


def parse_parasolid_analysis_report_text(raw_text: str) -> dict[str, Any] | None:
    lines = raw_text.splitlines()
    if not any(line.strip() == "# Parasolid Analysis Report" for line in lines[:8]):
        return None

    payload: dict[str, Any] = {
        "part_summary": {},
        "part_attributes": [],
        "body_count": 0,
        "inter_body_distances": [],
        "bodies": [],
    }

    current_body: dict[str, Any] | None = None
    section = ""
    subsection = ""
    parse_mode = ""

    part_summary_map = {
        "Part Name": "part_name",
        "Full Path": "full_path",
        "Leaf": "leaf",
        "Units": "units",
        "Tag": "tag",
        "Read Only": "read_only",
        "Fully Loaded": "fully_loaded",
        "Has Write Access": "has_write_access",
        "Unique Identifier": "unique_identifier",
    }
    body_metadata_map = {
        "Tag": "tag",
        "Journal Identifier": "journal_identifier",
        "Name": "name",
        "Is Solid Body": "is_solid_body",
        "Is Sheet Body": "is_sheet_body",
        "Is Convergent Body": "is_convergent_body",
        "Density": "density",
        "Facet Count": "facet_count",
        "Vertex Count": "vertex_count",
        "Shape Summary": "shape_summary",
    }
    measurement_map = {
        "Surface Area": "surface_area",
        "Volume": "volume",
        "Mass": "mass",
        "Weight": "weight",
        "Radius Of Gyration": "radius_of_gyration",
    }

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped == "---":
            continue

        if stripped.startswith("## "):
            parse_mode = ""
            subsection = ""
            if stripped == "## Part Summary":
                section = "part_summary"
                current_body = None
                continue
            body_match = re.match(r"## Body (\d+)", stripped)
            if body_match:
                section = "body"
                current_body = {
                    "index": int(body_match.group(1)),
                    "metadata": {},
                    "measurement": {},
                    "bounding_box": None,
                    "attributes": [],
                    "features": [],
                    "edge_type_breakdown": {},
                    "edges": [],
                    "face_type_breakdown": {},
                    "faces": [],
                }
                payload["bodies"].append(current_body)
                continue

        if stripped.startswith("### "):
            subsection = stripped[4:]
            parse_mode = ""
            continue

        if stripped.startswith("**") and stripped.endswith("**"):
            parse_mode = stripped.strip("*").strip().lower()
            continue

        body_count_match = re.match(r"\*\*Number of Bodies:\*\*\s*(.+)", stripped)
        if body_count_match:
            payload["body_count"] = int(parse_scalar_value(body_count_match.group(1)))
            continue

        if section == "part_summary":
            attribute_match = re.match(r"-\s+(.+?)\s+\((.+?)\):\s*(.*)", stripped)
            if subsection == "Part Attributes" and attribute_match:
                payload["part_attributes"].append(
                    {
                        "title": attribute_match.group(1).strip(),
                        "type": attribute_match.group(2).strip(),
                        "value": parse_scalar_value(attribute_match.group(3)),
                        "is_array": False,
                        "array_index": 0,
                    }
                )
                continue

            summary_match = re.match(r"-\s+([^:]+):\s*(.*)", stripped)
            if summary_match:
                label = summary_match.group(1).strip()
                value = summary_match.group(2).strip()
                key = part_summary_map.get(label)
                if key:
                    payload["part_summary"][key] = parse_scalar_value(value)
                continue

        if section != "body" or current_body is None:
            continue

        if subsection == "Body Metadata":
            match = re.match(r"-\s+([^:]+):\s*(.*)", stripped)
            if match:
                label = match.group(1).strip()
                value = match.group(2).strip()
                key = body_metadata_map.get(label)
                if key:
                    current_body["metadata"][key] = parse_scalar_value(value)
                continue

        if subsection == "Body Measurement":
            centroid_match = re.match(r"-\s+Centroid:\s*(.*)", stripped)
            if centroid_match:
                centroid = parse_centroid_triplet(centroid_match.group(1))
                if centroid:
                    current_body["measurement"]["centroid"] = centroid
                continue

            match = re.match(r"-\s+([^:]+):\s*(.*)", stripped)
            if match:
                label = match.group(1).strip()
                value = match.group(2).strip()
                key = measurement_map.get(label)
                if key:
                    current_body["measurement"][key] = parse_scalar_value(value)
                continue

        if subsection == "Edges":
            breakdown_match = re.match(r"-\s+([^:]+):\s*(\d+)", stripped)
            if parse_mode == "edge type breakdown:" and breakdown_match:
                current_body["edge_type_breakdown"][breakdown_match.group(1)] = int(breakdown_match.group(2))
                continue

            edge_match = re.match(
                r"-\s+Edge\s+(\d+):\s+([^|]+)\|\s+Length:\s*([^|]+)\|\s+Connected Faces:\s*([^|]+)\|\s+Tag:\s*(.+)",
                stripped,
            )
            if edge_match:
                current_body["edges"].append(
                    {
                        "index": int(edge_match.group(1)),
                        "type": edge_match.group(2).strip(),
                        "length": float(edge_match.group(3).strip()),
                        "face_count": int(parse_scalar_value(edge_match.group(4).strip())),
                        "tag": int(parse_scalar_value(edge_match.group(5).strip())),
                        "start_point": None,
                        "end_point": None,
                        "curve_measurement": None,
                        "is_reference": False,
                        "attributes": [],
                        "connected_face_tags": [],
                    }
                )
                continue

            face_tags_match = re.match(r"-\s+Face Tags:\s*(.*)", stripped)
            if face_tags_match and current_body["edges"]:
                current_body["edges"][-1]["connected_face_tags"] = [
                    int(token.strip())
                    for token in face_tags_match.group(1).split(",")
                    if token.strip()
                ]
                continue

        if subsection == "Faces":
            breakdown_match = re.match(r"-\s+([^:]+):\s*(\d+)", stripped)
            if parse_mode == "face type breakdown:" and breakdown_match:
                current_body["face_type_breakdown"][breakdown_match.group(1)] = int(breakdown_match.group(2))
                continue

            face_match = re.match(
                r"-\s+Face\s+(\d+):\s+([^|]+)\|\s+Edges:\s*([^|]+)\|\s+Facets:\s*([^|]+)\|\s+Vertices:\s*([^|]+)\|\s+Tag:\s*(.+)",
                stripped,
            )
            if face_match:
                current_body["faces"].append(
                    {
                        "index": int(face_match.group(1)),
                        "type": face_match.group(2).strip(),
                        "edge_count": int(parse_scalar_value(face_match.group(3).strip())),
                        "facet_count": parse_scalar_value(face_match.group(4).strip()),
                        "vertex_count": parse_scalar_value(face_match.group(5).strip()),
                        "tag": int(parse_scalar_value(face_match.group(6).strip())),
                        "measurement": None,
                        "analytic_data": None,
                        "adjacent_face_indices": [],
                        "is_blend_face": False,
                        "blend_radius": 0.0,
                        "attributes": [],
                    }
                )
                continue

            adjacent_match = re.match(r"-\s+Adjacent Faces:\s*(.*)", stripped)
            if adjacent_match and current_body["faces"]:
                current_body["faces"][-1]["adjacent_face_indices"] = [
                    int(token.strip())
                    for token in adjacent_match.group(1).split(",")
                    if token.strip()
                ]
                continue

            blend_match = re.match(r"-\s+Blend Data:\s+Is Blend\s+(True|False)\s+\|\s+Radius\s+(.+)", stripped)
            if blend_match and current_body["faces"]:
                current_body["faces"][-1]["is_blend_face"] = blend_match.group(1) == "True"
                current_body["faces"][-1]["blend_radius"] = float(parse_scalar_value(blend_match.group(2).strip()) or 0.0)
                continue

    if not payload["part_summary"] or not payload["bodies"]:
        return None
    if not payload["body_count"]:
        payload["body_count"] = len(payload["bodies"])
    return payload


def convert_parasolid_analysis_report(
    payload: dict[str, Any],
    *,
    display_name: str,
    source_path: str | None,
    file_size_bytes: int | None,
    raw_text: str,
) -> dict[str, Any]:
    return _convert_structured_body_report(
        payload,
        display_name=display_name,
        source_path=source_path,
        file_size_bytes=file_size_bytes,
        raw_text=raw_text,
        source_format="parasolid_analysis_report",
        application_label="Parasolid Analysis Report",
        schema_label="PARASOLID-REPORT",
    )


def infer_compound_preview_from_xt_scalars(
    notable_scalars: list[dict[str, Any]],
    entity_hints: dict[str, Any],
    decoded_names: list[str],
) -> dict[str, Any] | None:
    if not (entity_hints.get("surface_candidate") or entity_hints.get("topology_candidate")):
        return None

    all_values = sorted(
        {
            round(float(item["value"]), 12)
            for item in notable_scalars
            if 0.0 < float(item["value"]) <= 0.75
        },
        reverse=True,
    )
    if len(all_values) < 4:
        return None

    scalar_values = [value for value in all_values if value <= 0.35]
    if len(scalar_values) < 4:
        return None

    radius = None
    for candidate in sorted(value for value in scalar_values if value <= 0.15):
        if any(abs((2 * math.pi * candidate) - value) <= max(candidate * 0.1, 1e-3) for value in all_values):
            radius = candidate
    if radius is None:
        return None

    linear_dims = [
        value for value in scalar_values
        if abs(value - radius) > 1e-6
        and abs(value - (radius * 3)) > 1e-3
        and abs(value - (radius * 2 * math.pi)) > 1e-3
    ]
    if len(linear_dims) < 3:
        return None

    box_length = linear_dims[0]
    common_depth = linear_dims[1]
    box_height = linear_dims[2]
    if box_height >= common_depth:
        return None

    box_center = [-(radius + box_length / 2), 0.0, box_height / 2]
    cylinder_center = [0.0, 0.0, common_depth / 2]
    box_body, _ = _box_body_from_center_and_dimensions(
        box_center,
        [box_length, common_depth, box_height],
        body_index=1,
        source="xt_scalar_compound_preview",
    )
    cylinder_body, _ = _cylinder_body_from_center_radius_height(
        cylinder_center,
        radius,
        common_depth,
        axis="z",
        body_index=1,
        source="xt_scalar_compound_preview",
    )

    return {
        "kind": "solid",
        "name": "compound cylinder + block",
        "faces": [],
        "edges": [],
        "vertices": [],
        "metadata": {
            "primitive": "compound",
            "components": [box_body, cylinder_body],
            "source": "xt_scalar_compound_preview",
            "decoded_name": decoded_names[0] if decoded_names else None,
        },
    }


def parse_imported_json_text(
    raw_text: str,
    *,
    display_name: str,
    source_path: str | None,
    file_size_bytes: int | None,
) -> list[dict[str, Any]] | None:
    stripped = raw_text.lstrip()
    if not stripped or stripped[0] not in "[{":
        return None

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return None

    def normalize_payload(item: Any, fallback_name: str) -> list[dict[str, Any]] | None:
        if isinstance(item, list):
            reports: list[dict[str, Any]] = []
            for index, child in enumerate(item, start=1):
                child_reports = normalize_payload(child, f"{fallback_name} [{index}]")
                if child_reports is None:
                    return None
                reports.extend(child_reports)
            return reports

        if not isinstance(item, dict):
            return None

        if "part_summary" in item and "bodies" in item:
            return [
                convert_nx_json_report(
                    item,
                    display_name=fallback_name,
                    source_path=source_path,
                    file_size_bytes=file_size_bytes,
                    raw_text=raw_text,
                )
            ]

        if item.get("schema") == "nx_brep_export/v1" and "part" in item and "bodies" in item:
            return [
                convert_nx_cpp_export_report(
                    item,
                    display_name=fallback_name,
                    source_path=source_path,
                    file_size_bytes=file_size_bytes,
                    raw_text=raw_text,
                )
            ]

        if "file_name" in item or "geometry_hints" in item or "kernel_body" in item:
            return [
                normalize_existing_report(
                    item,
                    display_name=fallback_name,
                    source_path=source_path,
                    file_size_bytes=file_size_bytes,
                    raw_text=raw_text,
                )
            ]

        return None

    return normalize_payload(payload, display_name)


def parse_imported_report_text(
    raw_text: str,
    *,
    display_name: str,
    source_path: str | None,
    file_size_bytes: int | None,
) -> list[dict[str, Any]] | None:
    payload = parse_parasolid_analysis_report_text(raw_text)
    if payload is None:
        return None

    return [
        convert_parasolid_analysis_report(
            payload,
            display_name=display_name,
            source_path=source_path,
            file_size_bytes=file_size_bytes,
            raw_text=raw_text,
        )
    ]


def analyze_input_text(
    raw_text: str,
    *,
    display_name: str = "<memory>",
    source_path: str | None = None,
    file_size_bytes: int | None = None,
) -> list[dict[str, Any]]:
    imported_reports = parse_imported_json_text(
        raw_text,
        display_name=display_name,
        source_path=source_path,
        file_size_bytes=file_size_bytes,
    )
    if imported_reports is not None:
        return imported_reports

    imported_reports = parse_imported_report_text(
        raw_text,
        display_name=display_name,
        source_path=source_path,
        file_size_bytes=file_size_bytes,
    )
    if imported_reports is not None:
        return imported_reports

    return [
        analyze_xt_text(
            raw_text,
            display_name=display_name,
            source_path=source_path,
            file_size_bytes=file_size_bytes,
        )
    ]


def analyze_input_file(path: Path) -> list[dict[str, Any]]:
    raw_text = path.read_text(errors="ignore")
    return analyze_input_text(
        raw_text,
        display_name=path.name,
        source_path=str(path),
        file_size_bytes=path.stat().st_size,
    )


def print_text_report(report: dict[str, Any]) -> None:
    header_part1 = report["header"].get("PART1", {})
    header_part2 = report["header"].get("PART2", {})

    print(report["file_name"])
    print(f"  Path: {report['file']}")
    print(f"  Size: {report['file_size_bytes']} bytes across {report['line_count']} lines")

    if header_part1:
        source = header_part1.get("APPL")
        key = header_part1.get("KEY")
        date = header_part1.get("DATE")
        print(f"  Source: {source or 'unknown'}")
        if key or date:
            print(f"  Key/Date: {key or 'n/a'} / {date or 'n/a'}")
        if header_part1.get("OS") or header_part1.get("OS_RELEASE"):
            print(
                "  Export Host: "
                f"{header_part1.get('OS', 'unknown')} / {header_part1.get('OS_RELEASE', 'unknown')}"
            )

    if header_part2.get("SCH"):
        print(f"  Schema: {header_part2['SCH']}")

    transmit_info = report["transmit_info"]
    if transmit_info:
        pieces = []
        if transmit_info.get("modeller_version"):
            pieces.append(f"modeller {transmit_info['modeller_version']}")
        if transmit_info.get("schema_version"):
            pieces.append(transmit_info["schema_version"])
        print(f"  Transmit: {', '.join(pieces)}")

    if report["decoded_names"]:
        print(f"  Decoded Names: {', '.join(report['decoded_names'])}")

    density = report["density"]
    if density:
        unit = density["unit"] or "unknown unit"
        print(f"  Density: {density['value']:.12g} {unit}")

    if report["colors"]:
        color_strings = [
            f"{color['hex']} rgb{tuple(round(component, 6) for component in color['rgb'])}"
            for color in report["colors"]
        ]
        print(f"  Colors: {', '.join(color_strings)}")

    if report["has_scale_factor_attribute"]:
        print("  Scale Factor Attribute: present")

    if report["object_state_ids"]:
        preview = ", ".join(report["object_state_ids"][:4])
        more = "" if len(report["object_state_ids"]) <= 4 else f" (+{len(report['object_state_ids']) - 4} more)"
        print(f"  Object State IDs: {preview}{more}")

    geometry_hints = report["geometry_hints"]
    model_analysis = report.get("model_analysis", {})
    kernel_body = report.get("kernel_body")
    if geometry_hints["bounds"]:
        bounds = geometry_hints["bounds"]
        print(
            "  Bounds Hint: "
            f"min={tuple(bounds['min'])} max={tuple(bounds['max'])} size={tuple(bounds['size'])}"
        )

    if kernel_body:
        print(f"  Kernel Body: {kernel_body['name']} ({kernel_body['kind']})")

    if model_analysis.get("summary"):
        print("  Model Analysis:")
        for line in model_analysis["summary"]:
            print(f"    - {line}")

    topology = model_analysis.get("topology")
    if topology:
        print(f"  Inferred Topology: {topology['shape_label']}")
        print(f"  Faces/Edges: {topology['face_count']} faces, {topology['edge_count']} edges")

    if geometry_hints["notable_scalar_values"]:
        print("  Notable Scalar Values:")
        for item in geometry_hints["notable_scalar_values"][:6]:
            print(
                "    - "
                + metric_and_imperial(item["value"])
                + f" [seen {item['frequency']} times]"
            )

    if geometry_hints["unit_inference"]:
        print(f"  Unit Inference: {geometry_hints['unit_inference']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract human-readable metadata and geometry hints from Parasolid .x_t files."
    )
    parser.add_argument("paths", nargs="+", help="One or more .x_t files to inspect.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    reports = [analyze_xt_file(Path(path)) for path in args.paths]

    if args.json:
        print(json.dumps(reports, indent=2))
        return 0

    for index, report in enumerate(reports):
        if index:
            print()
        print_text_report(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
