#!/usr/bin/env python3

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any


HEADER_END_MARKER = "**END_OF_HEADER*****************************************************************"
INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
LABEL_PATTERN = re.compile(r"[A-Za-z_/]")


@dataclass
class XTRecord:
    index: int
    opcode: int
    tokens: list[str]

    @property
    def probable_entity_id(self) -> int | None:
        for token in self.tokens[2:6]:
            if INTEGER_PATTERN.match(token):
                return int(token)
        return None

    @property
    def labels(self) -> list[str]:
        return [token for token in self.tokens if LABEL_PATTERN.search(token)]


def split_header_and_body(normalized_text: str) -> tuple[str, str]:
    if HEADER_END_MARKER not in normalized_text:
        return normalized_text, ""
    return normalized_text.split(HEADER_END_MARKER, 1)


def tokenize_body(body_text: str) -> list[str]:
    return body_text.split()


def scan_records(tokens: list[str]) -> list[XTRecord]:
    record_starts: list[int] = []
    for index in range(len(tokens) - 1):
        if INTEGER_PATTERN.match(tokens[index]) and tokens[index + 1] == "255":
            record_starts.append(index)

    records: list[XTRecord] = []
    for record_index, start in enumerate(record_starts):
        end = record_starts[record_index + 1] if record_index + 1 < len(record_starts) else len(tokens)
        records.append(XTRecord(index=record_index, opcode=int(tokens[start]), tokens=tokens[start:end]))
    return records


def summarize_records(records: list[XTRecord]) -> dict[str, Any]:
    opcode_histogram = Counter(record.opcode for record in records)
    label_histogram = Counter(label for record in records for label in record.labels)

    labeled_records = []
    for record in records[:120]:
        labels = record.labels[:6]
        if not labels:
            continue
        labeled_records.append(
            {
                "record_index": record.index,
                "opcode": record.opcode,
                "probable_entity_id": record.probable_entity_id,
                "labels": labels,
                "token_count": len(record.tokens),
            }
        )

    return {
        "record_count": len(records),
        "top_opcodes": [
            {"opcode": opcode, "count": count}
            for opcode, count in opcode_histogram.most_common(20)
        ],
        "top_labels": [
            {"label": label, "count": count}
            for label, count in label_histogram.most_common(30)
        ],
        "sample_labeled_records": labeled_records[:40],
    }


def classify_record(record: XTRecord) -> str:
    label_text = " ".join(record.labels).lower()
    if "tysa_uname" in label_text:
        return "name_attribute"
    if "colour" in label_text:
        return "color_attribute"
    if "density" in label_text:
        return "density_attribute"
    if "scale_factor" in label_text:
        return "scale_attribute"
    if "polyline" in label_text or "line" in label_text:
        return "curve_candidate"
    if "mesh" in label_text or "lattice" in label_text:
        return "display_or_mesh"
    if record.opcode in {50, 51, 52}:
        return "surface_or_curve_geometry"
    if record.opcode in {29, 30, 31}:
        return "topology_or_transform"
    return "unclassified"


def map_records_to_entities(records: list[XTRecord]) -> dict[str, Any]:
    entities = []
    class_histogram = Counter()
    for record in records:
        entity_class = classify_record(record)
        class_histogram[entity_class] += 1
        entities.append(
            {
                "record_index": record.index,
                "opcode": record.opcode,
                "entity_class": entity_class,
                "probable_entity_id": record.probable_entity_id,
                "labels": record.labels[:8],
            }
        )

    return {
        "top_entity_classes": [
            {"entity_class": entity_class, "count": count}
            for entity_class, count in class_histogram.most_common(12)
        ],
        "sample_entities": entities[:50],
    }


def parse_xt_structure(normalized_text: str) -> dict[str, Any]:
    _header_text, body_text = split_header_and_body(normalized_text)
    tokens = tokenize_body(body_text)
    records = scan_records(tokens)
    return {
        "token_count": len(tokens),
        "records": summarize_records(records),
        "entities": map_records_to_entities(records),
    }


def infer_entity_hints(structured_parse: dict[str, Any], decoded_names: list[str]) -> dict[str, Any]:
    lowered_names = " ".join(decoded_names).lower()
    top_labels = [item["label"].lower() for item in structured_parse.get("records", {}).get("top_labels", [])]
    label_text = " ".join(top_labels)
    entity_classes = [item["entity_class"] for item in structured_parse.get("entities", {}).get("top_entity_classes", [])]

    arc_like = any(word in lowered_names for word in ("arc", "circle")) or "polyline" in label_text
    line_like = "line" in lowered_names or "polyline" in label_text
    colour_present = any("colour" in label for label in top_labels)
    density_present = any("density" in label for label in top_labels)
    curve_candidate = "curve_candidate" in entity_classes or "surface_or_curve_geometry" in entity_classes
    surface_candidate = "surface_or_curve_geometry" in entity_classes
    topology_candidate = "topology_or_transform" in entity_classes

    return {
        "arc_like": arc_like,
        "line_like": line_like,
        "colour_present": colour_present,
        "density_present": density_present,
        "curve_candidate": curve_candidate,
        "surface_candidate": surface_candidate,
        "topology_candidate": topology_candidate,
    }
