#!/usr/bin/env python3

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .geometry import Vec3, nearly_equal, round12


@dataclass
class TopologyVertex:
    id: str
    point: Vec3

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "point": self.point.to_list()}


@dataclass
class TopologyEdge:
    id: str
    kind: str
    vertex_ids: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "vertex_ids": self.vertex_ids,
            "data": self.data,
        }


@dataclass
class TopologyLoop:
    id: str
    edge_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "edge_ids": self.edge_ids}


@dataclass
class TopologyFace:
    id: str
    name: str
    surface_kind: str
    loop_ids: list[str]
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "surface_kind": self.surface_kind,
            "loop_ids": self.loop_ids,
            "data": self.data,
        }


@dataclass
class TopologyBody:
    id: str
    kind: str
    primitive: str | None
    vertices: list[TopologyVertex]
    edges: list[TopologyEdge]
    loops: list[TopologyLoop]
    faces: list[TopologyFace]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "primitive": self.primitive,
            "vertices": [vertex.to_dict() for vertex in self.vertices],
            "edges": [edge.to_dict() for edge in self.edges],
            "loops": [loop.to_dict() for loop in self.loops],
            "faces": [face.to_dict() for face in self.faces],
        }


def _find_or_add_vertex(vertices: list[TopologyVertex], point: Vec3) -> str:
    for vertex in vertices:
        if (
            nearly_equal(vertex.point.x, point.x)
            and nearly_equal(vertex.point.y, point.y)
            and nearly_equal(vertex.point.z, point.z)
        ):
            return vertex.id
    vertex_id = f"V{len(vertices) + 1}"
    vertices.append(TopologyVertex(id=vertex_id, point=point))
    return vertex_id


def build_topology_from_kernel_body(body_dict: dict[str, Any] | None) -> dict[str, Any] | None:
    if not body_dict:
        return None

    vertices: list[TopologyVertex] = []
    edges: list[TopologyEdge] = []
    loops: list[TopologyLoop] = []
    faces: list[TopologyFace] = []
    edge_index: dict[tuple[str, ...], str] = {}

    def edge_id_for(vertex_ids: list[str], kind: str = "line", data: dict[str, Any] | None = None) -> str:
        key = tuple(vertex_ids)
        reverse_key = tuple(reversed(vertex_ids))
        if key in edge_index:
            return edge_index[key]
        if reverse_key in edge_index:
            return edge_index[reverse_key]
        edge_id = f"E{len(edges) + 1}"
        edges.append(TopologyEdge(id=edge_id, kind=kind, vertex_ids=vertex_ids, data=data or {}))
        edge_index[key] = edge_id
        return edge_id

    def vertex_ids_from_points(points: list[list[float]]) -> list[str]:
        return [
            _find_or_add_vertex(vertices, Vec3(*map(float, point)))
            for point in points
        ]

    for face_index, face in enumerate(body_dict.get("faces", []), start=1):
        loop_ids: list[str] = []
        explicit_loops = face.get("loops") or []

        if explicit_loops:
            for loop_index, loop in enumerate(explicit_loops, start=1):
                vertex_ids = vertex_ids_from_points(loop.get("vertices", []))
                loop_edge_ids: list[str] = []
                if len(vertex_ids) >= 2:
                    for index in range(len(vertex_ids)):
                        start = vertex_ids[index]
                        end = vertex_ids[(index + 1) % len(vertex_ids)]
                        loop_edge_ids.append(edge_id_for([start, end]))
                loop_id = loop.get("id") or f"L{len(loops) + 1}"
                loops.append(TopologyLoop(id=loop_id, edge_ids=loop_edge_ids))
                loop_ids.append(loop_id)
        else:
            vertex_ids = vertex_ids_from_points(face.get("vertices", []))
            loop_edge_ids: list[str] = []
            if len(vertex_ids) >= 2:
                for index in range(len(vertex_ids)):
                    start = vertex_ids[index]
                    end = vertex_ids[(index + 1) % len(vertex_ids)]
                    loop_edge_ids.append(edge_id_for([start, end]))
            elif face.get("surface", {}).get("kind") in {"cylinder", "sphere"}:
                loop_edge_ids.append(
                    edge_id_for(
                        [f"implicit-{face_index}"],
                        kind=face["surface"]["kind"],
                        data=face.get("surface", {}),
                    )
                )

            loop_id = f"L{len(loops) + 1}"
            loops.append(TopologyLoop(id=loop_id, edge_ids=loop_edge_ids))
            loop_ids.append(loop_id)

        faces.append(
            TopologyFace(
                id=f"F{face_index}",
                name=face.get("name", f"Face {face_index}"),
                surface_kind=face.get("surface", {}).get("kind", "unknown"),
                loop_ids=loop_ids,
                data={
                    "metadata": face.get("metadata", {}),
                    "surface": face.get("surface", {}),
                    "normal": face.get("normal"),
                },
            )
        )

    for edge in body_dict.get("edges", []):
        if edge.get("kind") in {"circle", "arc"}:
            edge_id_for([f"implicit-{edge.get('name', len(edges) + 1)}"], kind=edge["kind"], data=edge)

    topology = TopologyBody(
        id="B1",
        kind=body_dict.get("kind", "unknown"),
        primitive=body_dict.get("metadata", {}).get("primitive"),
        vertices=vertices,
        edges=edges,
        loops=loops,
        faces=faces,
    )
    return topology.to_dict()
