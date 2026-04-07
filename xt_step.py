#!/usr/bin/env python3

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Any


NUMBER_PATTERN = r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:e[+-]?\d+)?"
NUMBER_RE = re.compile(NUMBER_PATTERN, flags=re.IGNORECASE)


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _parse_step_string(text: str, index: int) -> tuple[str, int]:
    result: list[str] = []
    index += 1
    while index < len(text):
        char = text[index]
        if char == "'":
            if index + 1 < len(text) and text[index + 1] == "'":
                result.append("'")
                index += 2
                continue
            return "".join(result), index + 1
        result.append(char)
        index += 1
    raise ValueError("Unterminated STEP string")


def _parse_identifier(text: str, index: int) -> tuple[str, int]:
    start = index
    while index < len(text) and (text[index].isalnum() or text[index] in {"_", "-"}):
        index += 1
    if start == index:
        raise ValueError(f"Expected identifier at offset {index}")
    return text[start:index], index


def _parse_token(text: str, index: int) -> tuple[str, int]:
    start = index
    while index < len(text) and text[index] not in {",", "(", ")", ";"} and not text[index].isspace():
        index += 1
    return text[start:index], index


def _convert_step_token(token: str) -> Any:
    if not token:
        return token
    if NUMBER_RE.fullmatch(token):
        if "." not in token and "e" not in token.lower():
            return int(token)
        return float(token)
    return token.upper()


def _parse_step_args(text: str, index: int) -> tuple[list[Any], int]:
    if text[index] != "(":
        raise ValueError(f"Expected '(' at offset {index}")
    index += 1
    values: list[Any] = []
    while index < len(text):
        index = _skip_ws(text, index)
        if index >= len(text):
            break
        if text[index] == ")":
            return values, index + 1
        value, index = _parse_step_value(text, index)
        values.append(value)
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
    raise ValueError("Unterminated STEP argument list")


def _parse_step_value(text: str, index: int) -> tuple[Any, int]:
    index = _skip_ws(text, index)
    if index >= len(text):
        raise ValueError("Unexpected end of STEP text")

    char = text[index]
    if char == "'":
        return _parse_step_string(text, index)
    if char == "#":
        index += 1
        start = index
        while index < len(text) and text[index].isdigit():
            index += 1
        return f"#{text[start:index]}", index
    if char == "(":
        return _parse_step_args(text, index)
    if char in {"$", "*"}:
        return None, index + 1
    if char == ".":
        end = text.find(".", index + 1)
        if end == -1:
            raise ValueError("Malformed STEP enum")
        return text[index : end + 1].upper(), end + 1

    token, index = _parse_token(text, index)
    lookahead = _skip_ws(text, index)
    if lookahead < len(text) and text[lookahead] == "(":
        args, next_index = _parse_step_args(text, lookahead)
        return {"type": token.upper(), "args": args}, next_index
    return _convert_step_token(token), index


def _parse_complex_entity(text: str, index: int) -> tuple[dict[str, Any], int]:
    if text[index] != "(":
        raise ValueError(f"Expected complex entity at offset {index}")
    index += 1
    constructors: dict[str, list[Any]] = {}
    order: list[str] = []
    while index < len(text):
        index = _skip_ws(text, index)
        if index >= len(text):
            break
        if text[index] == ")":
            return {"type": "COMPLEX", "complex": constructors, "complex_order": order}, index + 1
        name, index = _parse_identifier(text, index)
        index = _skip_ws(text, index)
        args, index = _parse_step_args(text, index)
        upper_name = name.upper()
        constructors[upper_name] = args
        order.append(upper_name)
    raise ValueError("Unterminated STEP complex entity")


def _parse_step_entities(data_text: str) -> dict[int, dict[str, Any]]:
    entities: dict[int, dict[str, Any]] = {}
    index = 0
    while True:
        hash_index = data_text.find("#", index)
        if hash_index == -1:
            break
        index = hash_index + 1
        start = index
        while index < len(data_text) and data_text[index].isdigit():
            index += 1
        if start == index:
            continue
        entity_id = int(data_text[start:index])
        index = _skip_ws(data_text, index)
        if index >= len(data_text) or data_text[index] != "=":
            continue
        index += 1
        index = _skip_ws(data_text, index)
        if index < len(data_text) and data_text[index] == "(":
            entity, index = _parse_complex_entity(data_text, index)
        else:
            entity_type, index = _parse_identifier(data_text, index)
            index = _skip_ws(data_text, index)
            args, index = _parse_step_args(data_text, index)
            entity = {"type": entity_type.upper(), "args": args}
        index = _skip_ws(data_text, index)
        if index < len(data_text) and data_text[index] == ";":
            index += 1
        entities[entity_id] = entity
    return entities


def _entity_has_type(entity: dict[str, Any] | None, type_name: str) -> bool:
    if not entity:
        return False
    upper_name = type_name.upper()
    if entity.get("type") == upper_name:
        return True
    return upper_name in (entity.get("complex") or {})


def _entity_args(entity: dict[str, Any] | None, type_name: str | None = None) -> list[Any] | None:
    if not entity:
        return None
    if type_name is None:
        return entity.get("args")
    upper_name = type_name.upper()
    if entity.get("type") == upper_name:
        return entity.get("args")
    return (entity.get("complex") or {}).get(upper_name)


def _args_without_name(args: list[Any] | None) -> list[Any]:
    if not args:
        return []
    if isinstance(args[0], str) and not args[0].startswith("#") and not args[0].startswith("."):
        return list(args[1:])
    return list(args)


def _ref_id(value: Any) -> int | None:
    if isinstance(value, str) and value.startswith("#") and value[1:].isdigit():
        return int(value[1:])
    return None


def _resolve_entity(entities: dict[int, dict[str, Any]], value: Any) -> dict[str, Any] | None:
    ref = _ref_id(value)
    return entities.get(ref) if ref is not None else None


def _vector_length(vector: list[float]) -> float:
    return math.sqrt(sum(component * component for component in vector))


def _normalize(vector: list[float] | None) -> list[float] | None:
    if not vector:
        return None
    magnitude = _vector_length(vector)
    if magnitude <= 1e-12:
        return None
    return [component / magnitude for component in vector]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(a[index] * b[index] for index in range(3))


def _cross(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _subtract(a: list[float], b: list[float]) -> list[float]:
    return [a[index] - b[index] for index in range(3)]


def _distance(a: list[float], b: list[float]) -> float:
    return _vector_length(_subtract(a, b))


def _point_payload(point: list[float]) -> dict[str, float]:
    return {"x": float(point[0]), "y": float(point[1]), "z": float(point[2])}


def _point_from_cartesian(entity: dict[str, Any] | None) -> list[float] | None:
    if not _entity_has_type(entity, "CARTESIAN_POINT"):
        return None
    args = _args_without_name(_entity_args(entity, "CARTESIAN_POINT"))
    if not args or not isinstance(args[0], list) or len(args[0]) < 3:
        return None
    return [float(args[0][0]), float(args[0][1]), float(args[0][2])]


def _point_from_ref(entities: dict[int, dict[str, Any]], value: Any) -> list[float] | None:
    return _point_from_cartesian(_resolve_entity(entities, value))


def _direction_from_ref(entities: dict[int, dict[str, Any]], value: Any) -> list[float] | None:
    entity = _resolve_entity(entities, value)
    if not _entity_has_type(entity, "DIRECTION"):
        return None
    args = _args_without_name(_entity_args(entity, "DIRECTION"))
    if not args or not isinstance(args[0], list) or len(args[0]) < 3:
        return None
    return _normalize([float(args[0][0]), float(args[0][1]), float(args[0][2])])


def _vector_from_ref(entities: dict[int, dict[str, Any]], value: Any) -> list[float] | None:
    entity = _resolve_entity(entities, value)
    if not _entity_has_type(entity, "VECTOR"):
        return None
    args = _args_without_name(_entity_args(entity, "VECTOR"))
    if len(args) < 2:
        return None
    direction = _direction_from_ref(entities, args[0])
    magnitude = float(args[1]) if args[1] is not None else 1.0
    if direction is None:
        return None
    return [component * magnitude for component in direction]


def _placement_from_ref(entities: dict[int, dict[str, Any]], value: Any) -> dict[str, list[float]] | None:
    entity = _resolve_entity(entities, value)
    if not _entity_has_type(entity, "AXIS2_PLACEMENT_3D"):
        return None
    args = _args_without_name(_entity_args(entity, "AXIS2_PLACEMENT_3D"))
    if not args:
        return None

    origin = _point_from_ref(entities, args[0])
    if origin is None:
        return None

    z_axis = _direction_from_ref(entities, args[1]) if len(args) > 1 else None
    if z_axis is None:
        z_axis = [0.0, 0.0, 1.0]

    x_axis = _direction_from_ref(entities, args[2]) if len(args) > 2 else None
    if x_axis is None:
        x_axis = [1.0, 0.0, 0.0] if abs(z_axis[0]) < 0.9 else [0.0, 1.0, 0.0]

    projection = _dot(x_axis, z_axis)
    x_axis = [x_axis[index] - projection * z_axis[index] for index in range(3)]
    x_axis = _normalize(x_axis) or ([1.0, 0.0, 0.0] if abs(z_axis[0]) < 0.9 else [0.0, 1.0, 0.0])
    y_axis = _normalize(_cross(z_axis, x_axis)) or [0.0, 1.0, 0.0]

    return {"origin": origin, "x_axis": x_axis, "y_axis": y_axis, "z_axis": z_axis}


def _sample_circle_like(
    center: list[float],
    x_axis: list[float],
    y_axis: list[float],
    radius_x: float,
    radius_y: float,
    start_point: list[float] | None,
    end_point: list[float] | None,
    *,
    closed: bool,
) -> tuple[list[list[float]], float | None, float | None]:
    if start_point is None or end_point is None:
        closed = True

    def angle_for_point(point: list[float]) -> float:
        delta = _subtract(point, center)
        return math.atan2(_dot(delta, y_axis) / max(radius_y, 1e-12), _dot(delta, x_axis) / max(radius_x, 1e-12))

    if closed or (start_point and end_point and _distance(start_point, end_point) <= 1e-8):
        start_angle = 0.0
        sweep = 2 * math.pi
    else:
        start_angle = angle_for_point(start_point or end_point or center)
        end_angle = angle_for_point(end_point or start_point or center)
        sweep = end_angle - start_angle
        while sweep <= 0.0:
            sweep += 2 * math.pi
        if sweep > math.pi:
            sweep -= 2 * math.pi
    segment_count = max(12, min(72, int(math.ceil(abs(sweep if not closed else 2 * math.pi) / (math.pi / 18)))))
    if closed:
        segment_count = max(segment_count, 24)

    points: list[list[float]] = []
    divisor = segment_count if not closed else segment_count
    sample_total = segment_count if closed else segment_count + 1
    for index in range(sample_total):
        t = index / divisor
        angle = start_angle + sweep * t
        points.append(
            [
                center[axis]
                + math.cos(angle) * radius_x * x_axis[axis]
                + math.sin(angle) * radius_y * y_axis[axis]
                for axis in range(3)
            ]
        )
    return points, start_angle, start_angle + sweep


def _curve_control_points(entity: dict[str, Any], entities: dict[int, dict[str, Any]]) -> list[list[float]]:
    candidate_lists: list[list[Any]] = []
    if _entity_has_type(entity, "POLYLINE"):
        args = _args_without_name(_entity_args(entity, "POLYLINE"))
        if args and isinstance(args[0], list):
            candidate_lists.append(args[0])
    for type_name in (
        "B_SPLINE_CURVE",
        "B_SPLINE_CURVE_WITH_KNOTS",
        "BEZIER_CURVE",
        "QUASI_UNIFORM_CURVE",
        "UNIFORM_CURVE",
        "RATIONAL_B_SPLINE_CURVE",
    ):
        args = _args_without_name(_entity_args(entity, type_name))
        if not args:
            continue
        for arg in args:
            if isinstance(arg, list) and arg and all(_ref_id(item) is not None for item in arg):
                candidate_lists.append(arg)
                break
    if not candidate_lists and entity.get("type", "").startswith("B_SPLINE_CURVE"):
        args = _args_without_name(entity.get("args"))
        for arg in args:
            if isinstance(arg, list) and arg and all(_ref_id(item) is not None for item in arg):
                candidate_lists.append(arg)
                break

    if not candidate_lists:
        return []
    points: list[list[float]] = []
    for point_ref in candidate_lists[0]:
        point = _point_from_ref(entities, point_ref)
        if point is not None:
            points.append(point)
    return points


def _curve_kind_label(entity: dict[str, Any], fallback: str = "SpCurve") -> str:
    if _entity_has_type(entity, "LINE"):
        return "Linear"
    if _entity_has_type(entity, "CIRCLE"):
        return "Circular"
    if _entity_has_type(entity, "ELLIPSE"):
        return "Elliptical"
    if _entity_has_type(entity, "INTERSECTION_CURVE"):
        return "Intersection"
    if _entity_has_type(entity, "SURFACE_CURVE") or _entity_has_type(entity, "SEAM_CURVE"):
        return "SpCurve"
    if any(_entity_has_type(entity, name) for name in ("POLYLINE", "B_SPLINE_CURVE", "B_SPLINE_CURVE_WITH_KNOTS", "BEZIER_CURVE")):
        return "Spline"
    return fallback


def _sample_curve(
    curve_ref: Any,
    entities: dict[int, dict[str, Any]],
    *,
    start_point: list[float] | None,
    end_point: list[float] | None,
    closed: bool,
    depth: int = 0,
) -> tuple[str, list[list[float]], dict[str, Any] | None]:
    if depth > 8:
        points = [point for point in (start_point, end_point) if point is not None]
        return "SpCurve", points, None

    entity = _resolve_entity(entities, curve_ref)
    if not entity:
        points = [point for point in (start_point, end_point) if point is not None]
        return "SpCurve", points, None

    if _entity_has_type(entity, "TRIMMED_CURVE"):
        args = _args_without_name(_entity_args(entity, "TRIMMED_CURVE"))
        basis_ref = args[0] if args else None
        return _sample_curve(
            basis_ref,
            entities,
            start_point=start_point,
            end_point=end_point,
            closed=closed,
            depth=depth + 1,
        )

    if _entity_has_type(entity, "SURFACE_CURVE") or _entity_has_type(entity, "SEAM_CURVE") or _entity_has_type(entity, "INTERSECTION_CURVE"):
        type_name = "INTERSECTION_CURVE" if _entity_has_type(entity, "INTERSECTION_CURVE") else ("SURFACE_CURVE" if _entity_has_type(entity, "SURFACE_CURVE") else "SEAM_CURVE")
        args = _args_without_name(_entity_args(entity, type_name))
        basis_ref = args[0] if args else None
        kind, points, exact = _sample_curve(
            basis_ref,
            entities,
            start_point=start_point,
            end_point=end_point,
            closed=closed,
            depth=depth + 1,
        )
        override = "Intersection" if type_name == "INTERSECTION_CURVE" else "SpCurve"
        if kind in {"Linear", "Circular", "Elliptical"}:
            return kind, points, exact
        return override, points, exact

    if _entity_has_type(entity, "LINE"):
        points = [point for point in (start_point, end_point) if point is not None]
        if len(points) < 2:
            args = _args_without_name(_entity_args(entity, "LINE"))
            base_point = _point_from_ref(entities, args[0]) if args else None
            direction = _vector_from_ref(entities, args[1]) if len(args) > 1 else None
            if base_point and direction:
                points = [base_point, [base_point[index] + direction[index] for index in range(3)]]
        exact = None
        if len(points) >= 2:
            exact = {
                "analytic_curve": {
                    "type": "Line",
                    "start_point": _point_payload(points[0]),
                    "end_point": _point_payload(points[-1]),
                }
            }
        return "Linear", points, exact

    if _entity_has_type(entity, "CIRCLE"):
        args = _args_without_name(_entity_args(entity, "CIRCLE"))
        placement = _placement_from_ref(entities, args[0]) if args else None
        radius = float(args[1]) if len(args) > 1 and args[1] is not None else None
        if placement and radius is not None:
            points, start_angle, end_angle = _sample_circle_like(
                placement["origin"],
                placement["x_axis"],
                placement["y_axis"],
                radius,
                radius,
                start_point,
                end_point,
                closed=closed,
            )
            exact = {
                "analytic_curve": {
                    "type": "Arc",
                    "center": _point_payload(placement["origin"]),
                    "radius": radius,
                    "matrix": {
                        "x_axis": _point_payload(placement["x_axis"]),
                        "y_axis": _point_payload(placement["y_axis"]),
                        "z_axis": _point_payload(placement["z_axis"]),
                    },
                    "start_angle": start_angle,
                    "end_angle": end_angle,
                }
            }
            return "Circular", points, exact

    if _entity_has_type(entity, "ELLIPSE"):
        args = _args_without_name(_entity_args(entity, "ELLIPSE"))
        placement = _placement_from_ref(entities, args[0]) if args else None
        radius_x = float(args[1]) if len(args) > 1 and args[1] is not None else None
        radius_y = float(args[2]) if len(args) > 2 and args[2] is not None else None
        if placement and radius_x is not None and radius_y is not None:
            points, _, _ = _sample_circle_like(
                placement["origin"],
                placement["x_axis"],
                placement["y_axis"],
                radius_x,
                radius_y,
                start_point,
                end_point,
                closed=closed,
            )
            return "Elliptical", points, None

    control_points = _curve_control_points(entity, entities)
    if control_points:
        points = control_points
        if start_point is not None and _distance(points[0], start_point) > 1e-8:
            points = [start_point] + points
        if end_point is not None and _distance(points[-1], end_point) > 1e-8:
            points = points + [end_point]
        return _curve_kind_label(entity, "Spline"), points, None

    points = [point for point in (start_point, end_point) if point is not None]
    return _curve_kind_label(entity), points, None


def _surface_kind_and_definition(surface_ref: Any, entities: dict[int, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    entity = _resolve_entity(entities, surface_ref)
    if not entity:
        return "Parametric", {"type": "Parametric", "source": "STEP"}

    if _entity_has_type(entity, "PLANE"):
        args = _args_without_name(_entity_args(entity, "PLANE"))
        placement = _placement_from_ref(entities, args[0]) if args else None
        if placement:
            return "Planar", {
                "type": "Planar",
                "reference_point": _point_payload(placement["origin"]),
                "direction": _point_payload(placement["z_axis"]),
                "source": "STEP",
            }

    if _entity_has_type(entity, "CYLINDRICAL_SURFACE"):
        args = _args_without_name(_entity_args(entity, "CYLINDRICAL_SURFACE"))
        placement = _placement_from_ref(entities, args[0]) if args else None
        radius = float(args[1]) if len(args) > 1 and args[1] is not None else None
        if placement:
            return "Cylindrical", {
                "type": "Cylindrical",
                "reference_point": _point_payload(placement["origin"]),
                "direction": _point_payload(placement["z_axis"]),
                "axis_point": _point_payload(placement["origin"]),
                "axis_direction": _point_payload(placement["z_axis"]),
                "radius": radius or 0.0,
                "source": "STEP",
            }

    if _entity_has_type(entity, "CONICAL_SURFACE"):
        args = _args_without_name(_entity_args(entity, "CONICAL_SURFACE"))
        placement = _placement_from_ref(entities, args[0]) if args else None
        radius = float(args[1]) if len(args) > 1 and args[1] is not None else None
        semi_angle = float(args[2]) if len(args) > 2 and args[2] is not None else None
        if placement:
            return "Conical", {
                "type": "Conical",
                "reference_point": _point_payload(placement["origin"]),
                "direction": _point_payload(placement["z_axis"]),
                "axis_point": _point_payload(placement["origin"]),
                "axis_direction": _point_payload(placement["z_axis"]),
                "radius": radius or 0.0,
                "radius_data": semi_angle or 0.0,
                "source": "STEP",
            }

    if _entity_has_type(entity, "SPHERICAL_SURFACE"):
        args = _args_without_name(_entity_args(entity, "SPHERICAL_SURFACE"))
        placement = _placement_from_ref(entities, args[0]) if args else None
        radius = float(args[1]) if len(args) > 1 and args[1] is not None else None
        if placement:
            return "Spherical", {
                "type": "Spherical",
                "reference_point": _point_payload(placement["origin"]),
                "radius": radius or 0.0,
                "source": "STEP",
            }

    if _entity_has_type(entity, "SURFACE_OF_REVOLUTION"):
        args = _args_without_name(_entity_args(entity, "SURFACE_OF_REVOLUTION"))
        placement = _placement_from_ref(entities, args[1]) if len(args) > 1 else None
        payload = {"type": "SurfaceOfRevolution", "source": "STEP"}
        if placement:
            payload["reference_point"] = _point_payload(placement["origin"])
            payload["direction"] = _point_payload(placement["z_axis"])
            payload["axis_point"] = _point_payload(placement["origin"])
            payload["axis_direction"] = _point_payload(placement["z_axis"])
        return "SurfaceOfRevolution", payload

    if _entity_has_type(entity, "SURFACE_OF_LINEAR_EXTRUSION"):
        args = _args_without_name(_entity_args(entity, "SURFACE_OF_LINEAR_EXTRUSION"))
        direction = _vector_from_ref(entities, args[1]) if len(args) > 1 else None
        payload = {"type": "Parametric", "source": "STEP"}
        if direction:
            payload["direction"] = _point_payload(_normalize(direction) or direction)
        return "Parametric", payload

    if any(_entity_has_type(entity, name) for name in ("TOROIDAL_SURFACE", "B_SPLINE_SURFACE", "B_SPLINE_SURFACE_WITH_KNOTS", "RATIONAL_B_SPLINE_SURFACE", "BEZIER_SURFACE")):
        return "Parametric", {"type": "Parametric", "source": "STEP"}

    return "Parametric", {"type": "Parametric", "source": "STEP"}


def _bounding_box(points: list[list[float]]) -> dict[str, float] | None:
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    zs = [point[2] for point in points]
    return {
        "x_min": min(xs),
        "y_min": min(ys),
        "z_min": min(zs),
        "x_max": max(xs),
        "y_max": max(ys),
        "z_max": max(zs),
    }


def _detect_units(entities: dict[int, dict[str, Any]]) -> str:
    for entity in entities.values():
        if _entity_has_type(entity, "CONVERSION_BASED_UNIT"):
            args = _args_without_name(_entity_args(entity, "CONVERSION_BASED_UNIT"))
            if args and isinstance(args[0], str):
                name = args[0].strip().lower()
                if "inch" in name or name == "in":
                    return "inch"
                if "millimeter" in name or name == "mm":
                    return "millimeter"
                if "centimeter" in name or name == "cm":
                    return "centimeter"
        if _entity_has_type(entity, "SI_UNIT"):
            args = _args_without_name(_entity_args(entity, "SI_UNIT"))
            if not args:
                continue
            enums = [value for value in args if isinstance(value, str) and value.startswith(".")]
            if any(value == ".METRE." for value in enums):
                if ".MILLI." in enums:
                    return "millimeter"
                if ".CENTI." in enums:
                    return "centimeter"
                return "meter"
    return "meter"


def _file_name_from_header(raw_text: str) -> str | None:
    match = re.search(r"FILE_NAME\s*\(\s*'([^']+)'", raw_text, flags=re.IGNORECASE)
    if match:
        return Path(match.group(1)).name
    return None


def _schema_names_from_header(raw_text: str) -> list[str]:
    match = re.search(r"FILE_SCHEMA\s*\(\s*\((.*?)\)\s*\)", raw_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    return re.findall(r"'([^']+)'", match.group(1))


def _product_name(entities: dict[int, dict[str, Any]]) -> str | None:
    for entity in entities.values():
        if _entity_has_type(entity, "PRODUCT"):
            args = _args_without_name(_entity_args(entity, "PRODUCT"))
            if args and isinstance(args[0], str):
                return args[0]
    return None


def _shell_face_refs(shell_ref: Any, entities: dict[int, dict[str, Any]]) -> list[str]:
    shell_entity = _resolve_entity(entities, shell_ref)
    if not shell_entity:
        return []
    for type_name in ("CLOSED_SHELL", "OPEN_SHELL", "CONNECTED_FACE_SET"):
        if _entity_has_type(shell_entity, type_name):
            args = _args_without_name(_entity_args(shell_entity, type_name))
            if args and isinstance(args[0], list):
                return [value for value in args[0] if _ref_id(value) is not None]
    return []


def _body_face_refs(entities: dict[int, dict[str, Any]]) -> list[tuple[str, list[str], bool]]:
    bodies: list[tuple[str, list[str], bool]] = []
    for entity_id, entity in entities.items():
        if _entity_has_type(entity, "MANIFOLD_SOLID_BREP"):
            args = _args_without_name(_entity_args(entity, "MANIFOLD_SOLID_BREP"))
            face_refs = _shell_face_refs(args[0] if args else None, entities)
            bodies.append((str(entity_id), face_refs, True))
        elif _entity_has_type(entity, "SHELL_BASED_SURFACE_MODEL"):
            args = _args_without_name(_entity_args(entity, "SHELL_BASED_SURFACE_MODEL"))
            face_refs: list[str] = []
            if args and isinstance(args[0], list):
                for shell_ref in args[0]:
                    face_refs.extend(_shell_face_refs(shell_ref, entities))
            bodies.append((str(entity_id), face_refs, False))
    if bodies:
        return bodies

    face_refs = [f"#{entity_id}" for entity_id, entity in entities.items() if _entity_has_type(entity, "ADVANCED_FACE")]
    return [("1", face_refs, True)] if face_refs else []


def _build_body_payload(
    body_name: str,
    face_refs: list[str],
    *,
    entities: dict[int, dict[str, Any]],
    body_index: int,
    is_solid: bool,
) -> dict[str, Any] | None:
    edge_map: dict[str, int] = {}
    vertex_map: dict[str, int] = {}
    edges: list[dict[str, Any]] = []
    faces: list[dict[str, Any]] = []
    topology_vertices: list[dict[str, Any]] = []
    all_points: list[list[float]] = []

    def vertex_index_for(vertex_ref: Any) -> int | None:
        ref = _ref_id(vertex_ref)
        if ref is None:
            return None
        key = f"#{ref}"
        if key in vertex_map:
            return vertex_map[key]
        entity = entities.get(ref)
        if not _entity_has_type(entity, "VERTEX_POINT"):
            return None
        args = _args_without_name(_entity_args(entity, "VERTEX_POINT"))
        point = _point_from_ref(entities, args[0]) if args else None
        if point is None:
            return None
        index = len(vertex_map) + 1
        vertex_map[key] = index
        topology_vertices.append({"index": index, "point": _point_payload(point)})
        all_points.append(point)
        return index

    def edge_index_for(edge_ref: Any) -> int | None:
        ref = _ref_id(edge_ref)
        if ref is None:
            return None
        key = f"#{ref}"
        if key in edge_map:
            return edge_map[key]
        entity = entities.get(ref)
        if not _entity_has_type(entity, "EDGE_CURVE"):
            return None
        args = _args_without_name(_entity_args(entity, "EDGE_CURVE"))
        if len(args) < 4:
            return None
        start_vertex_ref, end_vertex_ref, curve_ref, same_sense = args[:4]
        start_vertex_index = vertex_index_for(start_vertex_ref)
        end_vertex_index = vertex_index_for(end_vertex_ref)
        start_point = _point_from_ref(entities, _args_without_name(_entity_args(_resolve_entity(entities, start_vertex_ref), "VERTEX_POINT"))[0]) if start_vertex_index is not None else None
        end_point = _point_from_ref(entities, _args_without_name(_entity_args(_resolve_entity(entities, end_vertex_ref), "VERTEX_POINT"))[0]) if end_vertex_index is not None else None
        kind, preview_points, exact_geometry = _sample_curve(
            curve_ref,
            entities,
            start_point=start_point,
            end_point=end_point,
            closed=bool(same_sense == ".T." and start_vertex_index is not None and start_vertex_index == end_vertex_index),
        )
        if start_point is not None and not preview_points:
            preview_points = [start_point]
        if end_point is not None and (not preview_points or _distance(preview_points[-1], end_point) > 1e-8):
            preview_points.append(end_point)
        if start_point is not None and preview_points and _distance(preview_points[0], start_point) > 1e-8:
            preview_points = [start_point] + preview_points
        preview_payload = [_point_payload(point) for point in preview_points]
        length = 0.0
        for index in range(1, len(preview_points)):
            length += _distance(preview_points[index - 1], preview_points[index])
        all_points.extend(preview_points)

        edge_index = len(edges) + 1
        edge_map[key] = edge_index
        edges.append(
            {
                "index": edge_index,
                "type": kind,
                "length": length,
                "start_vertex_index": start_vertex_index,
                "end_vertex_index": end_vertex_index,
                "start_point": _point_payload(start_point) if start_point else None,
                "end_point": _point_payload(end_point) if end_point else None,
                "preview_points": preview_payload,
                "exact_geometry": exact_geometry,
                "exact_vertices": (
                    {
                        "vertex_count": 2,
                        "start_point": _point_payload(start_point) if start_point else None,
                        "end_point": _point_payload(end_point) if end_point else None,
                    }
                    if start_point and end_point
                    else None
                ),
                "attributes": [],
                "connected_face_tags": [],
            }
        )
        return edge_index

    for face_ref in face_refs:
        face_entity = _resolve_entity(entities, face_ref)
        if not _entity_has_type(face_entity, "ADVANCED_FACE"):
            continue
        args = _args_without_name(_entity_args(face_entity, "ADVANCED_FACE"))
        if len(args) < 3 or not isinstance(args[0], list):
            continue
        bounds = args[0]
        surface_ref = args[1]
        face_type, surface_definition = _surface_kind_and_definition(surface_ref, entities)
        loops: list[dict[str, Any]] = []
        for bound_ref in bounds:
            bound_entity = _resolve_entity(entities, bound_ref)
            if not bound_entity:
                continue
            bound_type = "FACE_OUTER_BOUND" if _entity_has_type(bound_entity, "FACE_OUTER_BOUND") else "FACE_BOUND"
            bound_args = _args_without_name(_entity_args(bound_entity, bound_type))
            if not bound_args:
                continue
            loop_entity = _resolve_entity(entities, bound_args[0])
            if not _entity_has_type(loop_entity, "EDGE_LOOP"):
                continue
            loop_args = _args_without_name(_entity_args(loop_entity, "EDGE_LOOP"))
            coedges: list[dict[str, Any]] = []
            if loop_args and isinstance(loop_args[0], list):
                for oriented_ref in loop_args[0]:
                    oriented_entity = _resolve_entity(entities, oriented_ref)
                    if not _entity_has_type(oriented_entity, "ORIENTED_EDGE"):
                        continue
                    oriented_args = _args_without_name(_entity_args(oriented_entity, "ORIENTED_EDGE"))
                    if len(oriented_args) < 4:
                        continue
                    edge_index = edge_index_for(oriented_args[2])
                    if edge_index is None:
                        continue
                    coedges.append(
                        {
                            "edge_index": edge_index,
                            "sense": "forward" if oriented_args[3] == ".T." else "backward",
                        }
                    )
            if coedges:
                loops.append(
                    {
                        "loop_index": len(loops) + 1,
                        "closed": True,
                        "coedges": coedges,
                        "source": "step_edge_loop",
                    }
                )

        if loops:
            faces.append(
                {
                    "index": len(faces) + 1,
                    "type": face_type,
                    "edge_count": sum(len(loop["coedges"]) for loop in loops),
                    "facet_count": None,
                    "vertex_count": None,
                    "loops": loops,
                    "surface_definition": surface_definition,
                    "analytic_data": {"source": "STEP"},
                    "adjacent_face_indices": [],
                    "attributes": [],
                    "trimmed_bsurface": {
                        "source": "step_topology",
                        "loop_count": len(loops),
                    },
                }
            )

    if not edges and not faces:
        return None

    bounding_box = _bounding_box(all_points)
    edge_breakdown = Counter(edge.get("type", "Unknown") for edge in edges)
    face_breakdown = Counter(face.get("type", "Unknown") for face in faces)
    shape_summary = "STEP B-rep preview reconstructed from explicit topology and curve definitions."
    if face_breakdown:
        shape_summary = (
            f"STEP B-rep body with {sum(face_breakdown.values())} faces and {sum(edge_breakdown.values())} edges."
        )

    return {
        "index": body_index,
        "metadata": {
            "name": body_name,
            "shape_summary": shape_summary,
            "is_solid_body": is_solid,
            "is_sheet_body": not is_solid,
        },
        "measurement": {},
        "bounding_box": bounding_box,
        "attributes": [],
        "features": [{"type": "STEP_BREP"}],
        "topology": {"vertices": topology_vertices},
        "edge_type_breakdown": dict(edge_breakdown),
        "edges": edges,
        "face_type_breakdown": dict(face_breakdown),
        "faces": faces,
    }


def parse_step_payload(raw_text: str, *, display_name: str) -> dict[str, Any] | None:
    if "ISO-10303-21" not in raw_text[:256]:
        return None

    match = re.search(r"\bDATA\s*;(.*)\bENDSEC\s*;", raw_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None

    data_text = match.group(1)
    entities = _parse_step_entities(data_text)
    if not entities:
        return None

    units = _detect_units(entities)
    part_name = _product_name(entities) or _file_name_from_header(raw_text) or display_name
    body_refs = _body_face_refs(entities)
    bodies: list[dict[str, Any]] = []
    for body_index, (entity_key, face_refs, is_solid) in enumerate(body_refs, start=1):
        body_name = f"Body {body_index}"
        source_entity = entities.get(int(entity_key)) if entity_key.isdigit() else None
        if source_entity:
            source_args = _entity_args(source_entity)
            if source_args and isinstance(source_args[0], str) and source_args[0]:
                body_name = source_args[0]
        built = _build_body_payload(body_name, face_refs, entities=entities, body_index=body_index, is_solid=is_solid)
        if built:
            bodies.append(built)

    if not bodies:
        return None

    return {
        "part_summary": {
            "part_name": part_name,
            "leaf": display_name,
            "units": units,
        },
        "part_attributes": [],
        "body_count": len(bodies),
        "inter_body_distances": [],
        "bodies": bodies,
        "step_metadata": {
            "schema_names": _schema_names_from_header(raw_text),
            "entity_count": len(entities),
        },
    }
