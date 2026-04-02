#!/usr/bin/env python3

from __future__ import annotations

import math
from typing import Any

from .geometry import TOLERANCE, Vec3, nearly_equal, round12
from .kernel import Body, Edge, Face, Surface


def _to_vec3(point: tuple[float, float, float] | list[float]) -> Vec3:
    return Vec3(float(point[0]), float(point[1]), float(point[2]))


def _unique_axis_values(points: list[Vec3]) -> dict[str, list[float]]:
    values = {"x": [], "y": [], "z": []}
    for point in points:
        for axis in ("x", "y", "z"):
            value = getattr(point, axis)
            if not any(nearly_equal(value, existing) for existing in values[axis]):
                values[axis].append(value)
    for axis in values:
        values[axis].sort()
    return values


def _build_cylinder_body(axis: str, radius: float, height: float, center: Vec3, *, source: str | None = None) -> Body:
    axis_vector = {"x": Vec3(1, 0, 0), "y": Vec3(0, 1, 0), "z": Vec3(0, 0, 1)}[axis]
    metadata = {
        "primitive": "cylinder",
        "axis": axis,
        "radius": round12(radius),
        "height": round12(height),
        "center": center.to_list(),
    }
    if source:
        metadata["source"] = source

    return Body(
        kind="solid",
        name="cylinder",
        faces=[
            Face(name=f"{axis.upper()}-start cap", surface=Surface(kind="plane", data={"radius": round12(radius)}), normal=axis_vector.scale(-1), metadata={"kind": "circular_cap", "axis": axis}),
            Face(name=f"{axis.upper()}-end cap", surface=Surface(kind="plane", data={"radius": round12(radius)}), normal=axis_vector, metadata={"kind": "circular_cap", "axis": axis}),
            Face(name="Cylindrical side face", surface=Surface(kind="cylinder", data={"radius": round12(radius), "height": round12(height), "axis": axis}), metadata={"kind": "curved_side", "axis": axis}),
        ],
        edges=[
            Edge(kind="circle", name="Start rim", data={"radius": round12(radius), "axis": axis}),
            Edge(kind="circle", name="End rim", data={"radius": round12(radius), "axis": axis}),
        ],
        vertices=[],
        metadata=metadata,
    )


def _radius_from_scalars(notable_scalars: list[dict[str, Any]], height: float) -> float | None:
    tolerance = max(height * 0.05, 1e-6)
    for item in notable_scalars:
        value = float(item["value"])
        if value <= TOLERANCE:
            continue
        if nearly_equal(value, height, tolerance):
            continue
        return value
    return None


def infer_box_body(points: list[Vec3], bounds: dict[str, Any] | None) -> Body | None:
    if not bounds:
        return None

    minimum = _to_vec3(bounds["min"])
    maximum = _to_vec3(bounds["max"])
    size = _to_vec3(bounds["size"])
    if size.x <= TOLERANCE or size.y <= TOLERANCE or size.z <= TOLERANCE:
        return None

    expected = [
        Vec3(x, y, z)
        for x in (minimum.x, maximum.x)
        for y in (minimum.y, maximum.y)
        for z in (minimum.z, maximum.z)
    ]

    matched = 0
    for corner in expected:
        if any(
            nearly_equal(point.x, corner.x)
            and nearly_equal(point.y, corner.y)
            and nearly_equal(point.z, corner.z)
            for point in points
        ):
            matched += 1
    if matched < 6:
        return None

    corners = {
        "000": Vec3(minimum.x, minimum.y, minimum.z),
        "100": Vec3(maximum.x, minimum.y, minimum.z),
        "110": Vec3(maximum.x, maximum.y, minimum.z),
        "010": Vec3(minimum.x, maximum.y, minimum.z),
        "001": Vec3(minimum.x, minimum.y, maximum.z),
        "101": Vec3(maximum.x, minimum.y, maximum.z),
        "111": Vec3(maximum.x, maximum.y, maximum.z),
        "011": Vec3(minimum.x, maximum.y, maximum.z),
    }

    face_specs = [
        ("X-min face", Vec3(-1, 0, 0), ["000", "010", "011", "001"], (size.y, size.z)),
        ("X-max face", Vec3(1, 0, 0), ["100", "101", "111", "110"], (size.y, size.z)),
        ("Y-min face", Vec3(0, -1, 0), ["000", "001", "101", "100"], (size.x, size.z)),
        ("Y-max face", Vec3(0, 1, 0), ["010", "110", "111", "011"], (size.x, size.z)),
        ("Z-min face", Vec3(0, 0, -1), ["000", "100", "110", "010"], (size.x, size.y)),
        ("Z-max face", Vec3(0, 0, 1), ["001", "011", "111", "101"], (size.x, size.y)),
    ]

    faces: list[Face] = []
    for name, normal, keys, dimensions in face_specs:
        width, height = dimensions
        faces.append(
            Face(
                name=name,
                surface=Surface(kind="plane", data={"dimensions": {"width": round12(width), "height": round12(height)}}),
                vertices=[corners[key] for key in keys],
                normal=normal,
                metadata={
                    "kind": "square" if nearly_equal(width, height) else "rectangular",
                    "area": round12(width * height),
                    "axis": name[0].lower(),
                },
            )
        )

    seen_axes: set[str] = set()
    edges: list[Edge] = []
    for axis, length in zip(("x", "y", "z"), (size.x, size.y, size.z)):
        if axis in seen_axes:
            continue
        seen_axes.add(axis)
        multiplicity = 4 * sum(1 for value in (size.x, size.y, size.z) if nearly_equal(value, length))
        edges.append(Edge(kind="line", name=f"{axis.upper()}-aligned edges", data={"axis": axis, "length": round12(length), "count": multiplicity}))

    shape = "cube-like solid"
    if not (nearly_equal(size.x, size.y) and nearly_equal(size.y, size.z)):
        shape = "square-section rectangular prism" if nearly_equal(size.x, size.y) else "rectangular prism"

    return Body(
        kind="solid",
        name=shape,
        faces=faces,
        edges=edges,
        vertices=list(corners.values()),
        metadata={"primitive": "box", "matched_corner_count": matched, "dimensions": {"x": round12(size.x), "y": round12(size.y), "z": round12(size.z)}},
    )


def infer_cylinder_body(
    points: list[Vec3],
    decoded_names: list[str],
    notable_scalars: list[dict[str, Any]],
    entity_hints: dict[str, Any],
    bounds: dict[str, Any] | None,
) -> Body | None:
    if len(points) < 6 and not bounds:
        return None

    axis_values = _unique_axis_values(points)
    axis_candidates = [axis for axis, values in axis_values.items() if len(values) == 2]
    lowered = " ".join(decoded_names).lower()
    prefer_round = any(word in lowered for word in ("arc", "circle", "cyl")) or entity_hints.get("curve_candidate")
    parser_supports_surface = entity_hints.get("surface_candidate") or entity_hints.get("topology_candidate")

    for axis in axis_candidates:
        other_axes = [value for value in ("x", "y", "z") if value != axis]
        radii: list[float] = []
        centers: list[tuple[float, float]] = []

        for level in axis_values[axis]:
            level_points = [point for point in points if nearly_equal(getattr(point, axis), level)]
            if len(level_points) < 4:
                continue

            a_values = [getattr(point, other_axes[0]) for point in level_points]
            b_values = [getattr(point, other_axes[1]) for point in level_points]
            center_a = (min(a_values) + max(a_values)) / 2
            center_b = (min(b_values) + max(b_values)) / 2
            level_radii = [
                math.sqrt((getattr(point, other_axes[0]) - center_a) ** 2 + (getattr(point, other_axes[1]) - center_b) ** 2)
                for point in level_points
            ]
            if not level_radii:
                continue
            radius = sum(level_radii) / len(level_radii)
            spread = max(abs(value - radius) for value in level_radii)
            if radius <= TOLERANCE or spread > max(radius * 0.12, 1e-4):
                break
            radii.append(radius)
            centers.append((center_a, center_b))

        if len(radii) != 2:
            continue
        if not nearly_equal(radii[0], radii[1], max(radii[0], radii[1]) * 0.12):
            continue
        if not nearly_equal(centers[0][0], centers[1][0], 1e-3) or not nearly_equal(centers[0][1], centers[1][1], 1e-3):
            continue
        if not prefer_round and not parser_supports_surface and len(points) < 12:
            continue

        height = abs(axis_values[axis][1] - axis_values[axis][0])
        radius = sum(radii) / len(radii)
        center_vals = {other_axes[0]: centers[0][0], other_axes[1]: centers[0][1], axis: sum(axis_values[axis]) / 2}
        center = Vec3(center_vals["x"], center_vals["y"], center_vals["z"])
        return _build_cylinder_body(axis, radius, height, center)

    if prefer_round and len(points) >= 2:
        for axis, levels in axis_values.items():
            if len(levels) != 2:
                continue
            other_axes = [value for value in ("x", "y", "z") if value != axis]
            if any(len(axis_values[other_axis]) != 1 for other_axis in other_axes):
                continue

            height = abs(levels[1] - levels[0])
            radius = _radius_from_scalars(notable_scalars, height)
            if height <= TOLERANCE or radius is None:
                continue

            center_vals = {
                axis: (levels[0] + levels[1]) / 2,
                other_axes[0]: getattr(points[0], other_axes[0]),
                other_axes[1]: getattr(points[0], other_axes[1]),
            }
            center = Vec3(center_vals["x"], center_vals["y"], center_vals["z"])
            return _build_cylinder_body(axis, radius, height, center, source="axis_levels_and_scalars")

    if bounds and prefer_round:
        minimum = _to_vec3(bounds["min"])
        maximum = _to_vec3(bounds["max"])
        size = _to_vec3(bounds["size"])
        dims = {"x": size.x, "y": size.y, "z": size.z}
        axis = max(dims, key=dims.get)
        radial_axes = [candidate for candidate in ("x", "y", "z") if candidate != axis]
        radial_a = dims[radial_axes[0]]
        radial_b = dims[radial_axes[1]]
        if nearly_equal(radial_a, radial_b, max(radial_a, radial_b) * 0.12) and radial_a > TOLERANCE and dims[axis] > TOLERANCE:
            radius = (radial_a + radial_b) / 4
            height = dims[axis]
            center = Vec3(
                (minimum.x + maximum.x) / 2,
                (minimum.y + maximum.y) / 2,
                (minimum.z + maximum.z) / 2,
            )
            return _build_cylinder_body(axis, radius, height, center, source="bounds_and_entity_hints")
    return None


def infer_sphere_body(points: list[Vec3], decoded_names: list[str]) -> Body | None:
    if len(points) < 12:
        return None

    lowered = " ".join(decoded_names).lower()
    if not any(word in lowered for word in ("sphere", "ball", "arc", "circle")):
        return None

    xs = [point.x for point in points]
    ys = [point.y for point in points]
    zs = [point.z for point in points]
    center = Vec3((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)
    radii = [(point - center).length() for point in points]
    if not radii:
        return None
    radius = sum(radii) / len(radii)
    spread = max(abs(value - radius) for value in radii)
    if radius <= TOLERANCE or spread > max(radius * 0.08, 1e-4):
        return None

    return Body(
        kind="solid",
        name="sphere",
        faces=[Face(name="Spherical face", surface=Surface(kind="sphere", data={"radius": round12(radius), "center": center.to_list()}), metadata={"kind": "closed_surface"})],
        edges=[],
        vertices=[],
        metadata={"primitive": "sphere", "radius": round12(radius), "center": center.to_list()},
    )


def infer_arc_curve_body(
    points: list[Vec3],
    decoded_names: list[str],
    notable_scalars: list[dict[str, Any]],
    entity_hints: dict[str, Any],
) -> Body | None:
    if not entity_hints.get("arc_like"):
        return None
    if not notable_scalars:
        return None

    radius = float(notable_scalars[0]["value"])
    if radius <= TOLERANCE:
        return None

    center = points[0] if points else Vec3(0.0, 0.0, 0.0)
    normal = Vec3(0.0, 0.0, 1.0)
    name = decoded_names[0] if decoded_names else "Arc curve"

    return Body(
        kind="wire",
        name=name,
        faces=[],
        edges=[
            Edge(
                kind="arc",
                name=name,
                data={
                    "radius": round12(radius),
                    "center": center.to_list(),
                    "normal": normal.to_list(),
                    "sweep_degrees": 360.0,
                },
            )
        ],
        vertices=[center],
        metadata={"primitive": "arc_or_circle", "radius": round12(radius), "center": center.to_list()},
    )


def build_kernel_body(
    preview_points: list[tuple[float, float, float]],
    bounds: dict[str, Any] | None,
    decoded_names: list[str],
    structured_parse: dict[str, Any] | None = None,
    notable_scalars: list[dict[str, Any]] | None = None,
    entity_hints: dict[str, Any] | None = None,
) -> Body | None:
    points = [_to_vec3(point) for point in preview_points]
    notable_scalars = notable_scalars or []
    entity_hints = entity_hints or {}
    for builder in (
        lambda: infer_box_body(points, bounds),
        lambda: infer_cylinder_body(points, decoded_names, notable_scalars, entity_hints, bounds),
        lambda: infer_sphere_body(points, decoded_names),
        lambda: infer_arc_curve_body(points, decoded_names, notable_scalars, entity_hints),
    ):
        body = builder()
        if body is not None:
            return body
    return None
