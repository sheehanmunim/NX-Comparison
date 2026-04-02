#!/usr/bin/env python3

from __future__ import annotations

import math
from dataclasses import dataclass


TOLERANCE = 1e-6


def nearly_equal(a: float, b: float, tolerance: float = TOLERANCE) -> bool:
    return abs(a - b) <= tolerance


def round12(value: float) -> float:
    return round(value, 12)


@dataclass
class Vec3:
    x: float
    y: float
    z: float

    def to_list(self) -> list[float]:
        return [round12(self.x), round12(self.y), round12(self.z)]

    def __sub__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __add__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def scale(self, factor: float) -> "Vec3":
        return Vec3(self.x * factor, self.y * factor, self.z * factor)

    def length(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def normalized(self) -> "Vec3":
        length = self.length()
        if length <= TOLERANCE:
            return Vec3(0.0, 0.0, 0.0)
        return self.scale(1.0 / length)
