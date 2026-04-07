#!/usr/bin/env python3

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def _round_point(point: list[float], digits: int = 6) -> list[float]:
    return [round(float(value), digits) for value in point[:3]]


def _bounds_from_points(points: list[list[float]]) -> dict[str, list[float]] | None:
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


def _emit(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(payload))
    return 0


def _curve_type_name(curve_type: Any) -> str:
    text = str(curve_type)
    if "." in text:
        text = text.split(".")[-1]
    return text.replace("GeomAbs_", "")


def _occ_display_properties(color_tool: Any, shape: Any) -> dict[str, Any] | None:
    from OCP.Quantity import Quantity_Color
    from OCP.XCAFDoc import XCAFDoc_ColorCurv, XCAFDoc_ColorGen, XCAFDoc_ColorSurf

    if shape is None or shape.IsNull():
        return None

    for color_type in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen, XCAFDoc_ColorCurv):
        color = Quantity_Color()
        try:
            has_color = color_tool.GetColor(shape, color_type, color)
        except Exception:
            has_color = False
        if not has_color:
            continue
        return {
            "rgb": [
                max(0, min(255, int(round(float(color.Red()) * 255)))),
                max(0, min(255, int(round(float(color.Green()) * 255)))),
                max(0, min(255, int(round(float(color.Blue()) * 255)))),
            ]
        }
    return None


def _step_occ_preview(payload: dict[str, Any]) -> dict[str, Any]:
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.Bnd import Bnd_Box
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    step_path = Path(str(payload["path"]))
    display_name = str(payload.get("display_name") or step_path.name)
    linear_deflection = float(payload.get("linear_deflection", 0.5))
    angular_deflection = float(payload.get("angular_deflection", 0.35))

    app = XCAFApp_Application.GetApplication_s()
    document = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), document)
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(document.Main())

    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    status = reader.ReadFile(str(step_path))
    if status != IFSelect_RetDone:
        raise RuntimeError(f"OpenCascade could not read STEP file: {display_name}")

    if not reader.Transfer(document):
        raise RuntimeError(f"OpenCascade could not transfer STEP file: {display_name}")

    shape = shape_tool.GetOneShape()
    mesh = BRepMesh_IncrementalMesh(shape, linear_deflection, False, angular_deflection, True)
    mesh.Perform()
    if not mesh.IsDone():
        raise RuntimeError(f"OpenCascade meshing failed for {display_name}")

    body_shapes: list[tuple[Any, str]] = []
    for kind, kind_name in ((TopAbs_SOLID, "solid"), (TopAbs_SHELL, "shell")):
        explorer = TopExp_Explorer(shape, kind)
        while explorer.More():
            current = explorer.Current()
            body_shape = TopoDS.Solid_s(current) if kind == TopAbs_SOLID else TopoDS.Shell_s(current)
            body_shapes.append((body_shape, kind_name))
            explorer.Next()
        if body_shapes:
            break

    bodies: list[dict[str, Any]] = []
    total_triangle_faces = 0
    total_edges = 0

    for body_index, (body_shape, body_kind) in enumerate(body_shapes, start=1):
        body_points: list[list[float]] = []
        faces: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        edge_seen: set[str] = set()
        body_display_properties = _occ_display_properties(color_tool, body_shape)

        face_explorer = TopExp_Explorer(body_shape, TopAbs_FACE)
        face_index = 0
        while face_explorer.More():
            face_index += 1
            face = TopoDS.Face_s(face_explorer.Current())
            location = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation_s(face, location)
            if triangulation is not None and triangulation.NbTriangles() > 0:
                transform = location.Transformation()
                triangle_index = 0
                for triangle_number in range(1, triangulation.NbTriangles() + 1):
                    triangle_index += 1
                    node_indexes = triangulation.Triangle(triangle_number).Get()
                    triangle_vertices: list[list[float]] = []
                    for node_index in node_indexes:
                        point = triangulation.Node(node_index).Transformed(transform)
                        triangle_vertices.append(_round_point(list(point.Coord())))
                    if face.Orientation() == TopAbs_REVERSED:
                        triangle_vertices = [triangle_vertices[0], triangle_vertices[2], triangle_vertices[1]]
                    body_points.extend(triangle_vertices)
                    faces.append(
                        {
                            "name": f"Face {face_index} triangle {triangle_index}",
                            "vertices": triangle_vertices,
                            "metadata": {
                                "kind": "occ_triangle_mesh",
                                "source_face_type": "occ_triangle_mesh",
                                "source_face_index": face_index,
                                "display_properties": body_display_properties,
                            },
                        }
                    )
            face_explorer.Next()

        edge_explorer = TopExp_Explorer(body_shape, TopAbs_EDGE)
        edge_index = 0
        while edge_explorer.More():
            edge_index += 1
            edge = TopoDS.Edge_s(edge_explorer.Current())
            curve = BRepAdaptor_Curve(edge)
            first = float(curve.FirstParameter())
            last = float(curve.LastParameter())
            if not math.isfinite(first) or not math.isfinite(last) or abs(last - first) <= 1e-9:
                edge_explorer.Next()
                continue

            curve_type = _curve_type_name(curve.GetType())
            sample_count = 2
            if "BSpline" in curve_type or "Bezier" in curve_type:
                sample_count = 18
            elif "Circle" in curve_type or "Ellipse" in curve_type:
                sample_count = 24
            elif "Hyperbola" in curve_type or "Parabola" in curve_type:
                sample_count = 16
            else:
                sample_count = 8

            points: list[list[float]] = []
            for sample_index in range(sample_count):
                parameter = first if sample_count == 1 else first + ((last - first) * sample_index / (sample_count - 1))
                point = curve.Value(parameter)
                points.append(_round_point(list(point.Coord())))

            if len(points) < 2:
                edge_explorer.Next()
                continue

            marker = json.dumps([points[0], points[-1], len(points)], separators=(",", ":"))
            reverse_marker = json.dumps([points[-1], points[0], len(points)], separators=(",", ":"))
            if marker in edge_seen or reverse_marker in edge_seen:
                edge_explorer.Next()
                continue
            edge_seen.add(marker)
            body_points.extend(points)
            edges.append(
                {
                    "kind": curve_type.lower(),
                    "points": points,
                    "source_edge_index": edge_index,
                }
            )
            edge_explorer.Next()

        body_bounds = _bounds_from_points(body_points)
        body_box = Bnd_Box()
        BRepBndLib.Add_s(body_shape, body_box)
        try:
            xmin, ymin, zmin, xmax, ymax, zmax = body_box.Get()
            body_bounds = body_bounds or {
                "min": [xmin, ymin, zmin],
                "max": [xmax, ymax, zmax],
                "size": [xmax - xmin, ymax - ymin, zmax - zmin],
            }
        except Exception:
            pass

        total_triangle_faces += len(faces)
        total_edges += len(edges)
        bodies.append(
            {
                "kind": "solid",
                "name": f"Body {body_index}",
                "faces": faces,
                "edges": edges,
                "vertices": [],
                "metadata": {
                    "name": f"Body {body_index}",
                    "primitive": "occ_mesh",
                    "shape_summary": f"OpenCascade tessellated {body_kind} preview with {len(faces)} triangles and {len(edges)} edges.",
                    "source": "opencascade_occ_preview",
                    "nx_body_index": body_index,
                    "display_properties": body_display_properties,
                },
                "bounding_box": body_bounds,
            }
        )

    preview_points = [point for body in bodies for face in body["faces"] for point in face.get("vertices", [])]
    bounds = _bounds_from_points(preview_points)
    return {
        "ok": True,
        "backend": "opencascade_occ",
        "display_name": display_name,
        "kernel_bodies": bodies,
        "triangle_face_count": total_triangle_faces,
        "edge_count": total_edges,
        "body_count": len(bodies),
        "preview_points": preview_points[:1200],
        "bounds": bounds,
    }


def _ml_match_probabilities(payload: dict[str, Any]) -> dict[str, Any]:
    from sklearn.ensemble import ExtraTreesClassifier, GradientBoostingClassifier, RandomForestClassifier

    training_features = payload.get("training_features") or []
    training_labels = payload.get("training_labels") or []
    predict_features = payload.get("predict_features") or []
    model_name = str(payload.get("model_name") or "RandomForestClassifier")

    if not training_features or not predict_features:
        return {"ok": False, "error": "Missing ML feature payload."}
    if len(training_features) != len(training_labels):
        return {"ok": False, "error": "Training feature/label size mismatch."}
    if len(set(int(label) for label in training_labels)) < 2:
        return {"ok": False, "error": "Need at least two classes for ML matching."}

    labels = [int(label) for label in training_labels]
    models = [
        (
            "RandomForestClassifier",
            RandomForestClassifier(
                n_estimators=220,
                random_state=7,
                class_weight="balanced_subsample",
                min_samples_leaf=1,
                max_features="sqrt",
            ),
        ),
        (
            "ExtraTreesClassifier",
            ExtraTreesClassifier(
                n_estimators=260,
                random_state=11,
                class_weight="balanced",
                min_samples_leaf=1,
                max_features="sqrt",
            ),
        ),
        (
            "GradientBoostingClassifier",
            GradientBoostingClassifier(
                n_estimators=140,
                learning_rate=0.06,
                max_depth=3,
                random_state=13,
            ),
        ),
    ]
    probability_rows: list[list[float]] = []
    ensemble_members: list[dict[str, Any]] = []
    for estimator_name, model in models:
        model.fit(training_features, labels)
        probability_rows.append(
            [
                float(row[1]) if len(row) > 1 else 0.0
                for row in model.predict_proba(predict_features)
            ]
        )
        ensemble_members.append({"type": estimator_name})

    probabilities = []
    for index in range(len(predict_features)):
        member_values = [row[index] for row in probability_rows]
        average_probability = sum(member_values) / len(member_values)
        spread = max(member_values) - min(member_values) if len(member_values) > 1 else 0.0
        probabilities.append(max(0.0, min(1.0, average_probability - (spread * 0.08))))
    return {
        "ok": True,
        "model": {
            "name": model_name,
            "type": "EnsembleClassifier",
            "members": ensemble_members,
            "feature_count": len(training_features[0]) if training_features else 0,
        },
        "probabilities": probabilities,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return _emit({"ok": False, "error": "Missing backend action."})

    action = argv[1]
    payload = json.loads(sys.stdin.read() or "{}")

    try:
        if action == "step_occ_preview":
            return _emit(_step_occ_preview(payload))
        if action == "ml_match_probabilities":
            return _emit(_ml_match_probabilities(payload))
        return _emit({"ok": False, "error": f"Unknown backend action: {action}"})
    except Exception as exc:
        return _emit({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
