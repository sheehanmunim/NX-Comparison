#!/usr/bin/env python3

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .geometry import Vec3


@dataclass
class Surface:
    kind: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.data}


@dataclass
class Edge:
    kind: str
    name: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, **self.data}


@dataclass
class Face:
    name: str
    surface: Surface
    vertices: list[Vec3] = field(default_factory=list)
    normal: Vec3 | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "surface": self.surface.to_dict(),
            "vertices": [vertex.to_list() for vertex in self.vertices],
            "metadata": self.metadata,
        }
        if self.normal is not None:
            payload["normal"] = self.normal.to_list()
        return payload


@dataclass
class Body:
    kind: str
    name: str
    faces: list[Face] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    vertices: list[Vec3] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "faces": [face.to_dict() for face in self.faces],
            "edges": [edge.to_dict() for edge in self.edges],
            "vertices": [vertex.to_list() for vertex in self.vertices],
            "metadata": self.metadata,
        }
