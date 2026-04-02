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
    kernel_body = build_kernel_body(geometry_points, bounds, decoded_names, structured_parse, notable_scalars, entity_hints)
    kernel_topology = build_topology_from_kernel_body(kernel_body.to_dict() if kernel_body else None)

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
        "kernel_body": kernel_body.to_dict() if kernel_body else None,
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
        },
    }
    return kernel_body, bounds, points, label


def _build_nx_body(
    body_payload: dict[str, Any],
    *,
    scale: float,
    body_index: int,
) -> tuple[dict[str, Any], dict[str, Any], list[list[float]], str] | None:
    shape_summary = str(body_payload.get("metadata", {}).get("shape_summary") or "").lower()
    face_types = {str(name).lower() for name in (body_payload.get("face_type_breakdown") or {})}
    edge_types = {str(name).lower() for name in (body_payload.get("edge_type_breakdown") or {})}

    if "cylinder" in shape_summary or "cylindrical" in face_types or "circular" in edge_types:
        built = _build_nx_cylinder_body(body_payload, scale=scale, body_index=body_index)
        if built:
            return built

    if "rectangular prism" in shape_summary or ("planar" in face_types and "linear" in edge_types):
        built = _build_nx_box_body(body_payload, scale=scale, body_index=body_index)
        if built:
            return built

    return None


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


def convert_nx_json_report(
    payload: dict[str, Any],
    *,
    display_name: str,
    source_path: str | None,
    file_size_bytes: int | None,
    raw_text: str,
) -> dict[str, Any]:
    part_summary = payload.get("part_summary") or {}
    part_name = part_summary.get("part_name") or part_summary.get("leaf") or display_name
    units = part_summary.get("units") or "Unknown"
    scale = unit_scale_to_meters(units)

    kernel_bodies: list[dict[str, Any]] = []
    imported_body_summaries: list[dict[str, Any]] = []
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
        imported_body_summaries.append(
            {
                "index": body_index,
                "name": body_payload.get("metadata", {}).get("name") or f"Body {body_index}",
                "shape_summary": label,
                "face_count": sum(int(value) for value in (body_payload.get("face_type_breakdown") or {}).values()),
                "edge_count": sum(int(value) for value in (body_payload.get("edge_type_breakdown") or {}).values()),
            }
        )

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
            "APPL": "Siemens NX JSON Import",
            "DATE": "",
            "UNITS": str(units),
            "PART": str(part_name),
        },
        "PART2": {
            "SCH": "NX-JSON",
        },
    }
    if part_summary.get("full_path"):
        header["PART1"]["SOURCE_PATH"] = str(part_summary["full_path"])

    body_count = int(payload.get("body_count") or len(payload.get("bodies", [])))
    summary = [
        f"Imported Siemens NX JSON report for {part_name}.",
        f"Detected {body_count} body/bodies and reconstructed {len(kernel_bodies)} previewable primitive shape(s).",
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
            "source_format": "siemens_nx_json",
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
            "source": "nx_json_import",
            "body_count": body_count,
        },
        "kernel_body": kernel_bodies[0] if len(kernel_bodies) == 1 else None,
        "kernel_bodies": kernel_bodies,
        "kernel_topology": build_topology_from_kernel_body(kernel_bodies[0]) if len(kernel_bodies) == 1 else None,
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
