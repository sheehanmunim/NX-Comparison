import json
import math
import os

import NXOpen
import NXOpen.UF


FACE_TYPE_NAMES = {
    0: "Rubber",
    1: "Planar",
    2: "Cylindrical",
    3: "Conical",
    4: "Spherical",
    5: "SurfaceOfRevolution",
    6: "Parametric",
    7: "Blending",
    8: "Offset",
    9: "Swept",
    10: "Convergent",
    11: "Undefined",
}

EDGE_TYPE_NAMES = {
    0: "Rubber",
    1: "Linear",
    2: "Circular",
    3: "Elliptical",
    4: "Intersection",
    5: "Spline",
    6: "SpCurve",
    7: "Foreign",
    8: "ConstantParameter",
    9: "TrimmedCurve",
    10: "Convergent",
    11: "Undefined",
}

ATTRIBUTE_TYPE_NAMES = {
    0: "Invalid",
    1: "Null",
    2: "Boolean",
    3: "Integer",
    4: "Real",
    5: "String",
    6: "Time",
    7: "Reference",
    100: "Any",
}

PART_UNITS = {
    0: "Inches",
    1: "Millimeters",
}

CURVE_STRUCT_TYPE_NAMES = {
    1: "Line",
    2: "Arc",
    3: "Conic",
    5: "Spline",
}

FACE_TOPOLOGY_NAMES = {
    1: "Open",
    2: "Closed",
    3: "Periodic",
    4: "Degenerate",
}

RUNTIME_DIAGNOSTICS = []
SCRIPT_VERSION = "brep-export-debug-2026-04-06-v9"
DIAGNOSTIC_SEEN = set()


def safe_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception:
        return None


def add_diagnostic(scope, message, **context):
    entry = {"scope": scope, "message": message}
    if context:
        entry["context"] = {key: serialize_generic(value) for key, value in context.items()}
    RUNTIME_DIAGNOSTICS.append(entry)


def add_diagnostic_once(scope, message, **context):
    key = (scope, message)
    if key in DIAGNOSTIC_SEEN:
        return
    DIAGNOSTIC_SEEN.add(key)
    add_diagnostic(scope, message, **context)


def safe_call_with_error(func, *args, **kwargs):
    try:
        return True, func(*args, **kwargs), None
    except Exception as ex:
        return False, None, str(ex)


def resolve_member(obj, *candidate_names):
    if obj is None:
        return None
    for name in candidate_names:
        if hasattr(obj, name):
            return getattr(obj, name)
    try:
        public_names = {name.lower(): name for name in dir(obj) if not name.startswith("_")}
    except Exception:
        return None
    for name in candidate_names:
        actual_name = public_names.get(name.lower())
        if actual_name is not None:
            return getattr(obj, actual_name)
    return None


def normalize_member_name(name):
    if name is None:
        return ""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def resolve_member_relaxed(obj, *candidate_names):
    member = resolve_member(obj, *candidate_names)
    if member is not None:
        return member
    try:
        public_names = [name for name in dir(obj) if not name.startswith("_")]
    except Exception:
        return None
    normalized_lookup = {}
    for name in public_names:
        normalized_lookup.setdefault(normalize_member_name(name), name)
    normalized_candidates = [normalize_member_name(name) for name in candidate_names if name]
    for candidate in normalized_candidates:
        actual_name = normalized_lookup.get(candidate)
        if actual_name is not None:
            return getattr(obj, actual_name)
    for candidate in normalized_candidates:
        for normalized_name, actual_name in normalized_lookup.items():
            if normalized_name.startswith(candidate) or candidate.startswith(normalized_name):
                return getattr(obj, actual_name)
    return None


def get_modeling_apis(uf_session):
    apis = []
    for candidate_name in ("Modeling", "Modl"):
        api = resolve_member(uf_session, candidate_name)
        if api is None:
            continue
        if all(existing is not api for existing in apis):
            apis.append(api)
    if not apis:
        add_diagnostic_once("UFModeling", "api_unavailable", attrs=public_attrs(uf_session))
    return apis


def get_modeling_api(uf_session):
    apis = get_modeling_apis(uf_session)
    return apis[0] if apis else None


def resolve_modeling_method(uf_session, *candidate_names):
    for modeling_api in get_modeling_apis(uf_session):
        member = resolve_member_relaxed(modeling_api, *candidate_names)
        if member is not None:
            return modeling_api, member
    return None, None


def matching_public_attrs(obj, *tokens):
    normalized_tokens = [normalize_member_name(token) for token in tokens if token]
    if not normalized_tokens:
        return []
    matches = []
    for name in public_attrs(obj):
        normalized_name = normalize_member_name(name)
        if all(token in normalized_name for token in normalized_tokens):
            matches.append(name)
    return matches[:20]


def add_missing_modeling_method_diagnostic(scope, uf_session, reason, *tokens):
    apis = get_modeling_apis(uf_session)
    if not apis:
        add_diagnostic_once(scope, "api_unavailable", reason="uf_modeling_api_missing")
        return
    nearby = []
    for api in apis:
        for name in matching_public_attrs(api, *tokens):
            if name not in nearby:
                nearby.append(name)
    add_diagnostic_once(scope, "api_unavailable", reason=reason, nearby=nearby)


def get_measurement_alternate_face():
    alternate_face = resolve_member(NXOpen.Measurement, "AlternateFace")
    if alternate_face is None:
        return 0
    radius = resolve_member(alternate_face, "Radius")
    if radius is not None:
        return radius
    diameter = resolve_member(alternate_face, "Diameter")
    if diameter is not None:
        return diameter
    return 0


def get_nx_tag(nx_object):
    tag = getattr(nx_object, "Tag", None)
    if callable(tag):
        return safe_call(tag)
    return tag


def enum_key(value):
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        pass
    raw_value = getattr(value, "value", None)
    if raw_value is not None:
        try:
            return int(raw_value)
        except Exception:
            pass
    try:
        return int(str(value))
    except Exception:
        return None


def enum_name(value, mapping):
    key = enum_key(value)
    if key in mapping:
        return mapping[key]
    return str(value)


def format_float(value, digits=4):
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def format_bool(value):
    if value is None:
        return "N/A"
    return "True" if value else "False"


def format_point(point, digits=4):
    xyz = serialize_xyz(point)
    if xyz is None:
        return "N/A" if point is None else str(point)
    return f"({format_float(xyz['x'], digits)}, {format_float(xyz['y'], digits)}, {format_float(xyz['z'], digits)})"


def clean_text(value, fallback="N/A"):
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def is_inferred_source(source):
    return isinstance(source, str) and source.startswith("inferred_")


def format_source_label(source):
    if not source:
        return "native"
    if is_inferred_source(source):
        return f"inferred: {source}"
    return source


def source_suffix(source):
    return f" [{format_source_label(source)}]"


def get_unit_names(work_part):
    part_units_key = enum_key(getattr(work_part, "PartUnits", None))
    if part_units_key == 1:
        return {
            "length": "MilliMeter",
            "area": "SquareMilliMeter",
            "volume": "CubicMilliMeter",
            "mass": "Kilogram",
        }
    return {
        "length": "Inch",
        "area": "SquareInch",
        "volume": "CubicInch",
        "mass": "PoundMass",
    }


def get_unit_object(work_part, unit_key):
    unit_collection = getattr(work_part, "UnitCollection", None)
    if unit_collection is None:
        return None
    unit_names = get_unit_names(work_part)
    return safe_call(unit_collection.FindObject, unit_names[unit_key])


def attribute_value(attr):
    attr_type_key = enum_key(getattr(attr, "Type", None))
    if attr_type_key == 2:
        return getattr(attr, "BooleanValue", None)
    if attr_type_key == 3:
        return getattr(attr, "IntegerValue", None)
    if attr_type_key == 4:
        return getattr(attr, "RealValue", None)
    if attr_type_key == 5:
        return getattr(attr, "StringValue", None)
    if attr_type_key == 6:
        return getattr(attr, "TimeValue", None)
    if attr_type_key == 7:
        return getattr(attr, "ReferenceValue", None)
    return safe_call(attr.ToString) if hasattr(attr, "ToString") else None


def usable_attribute(attr):
    title = clean_text(getattr(attr, "Title", None), "")
    value = attribute_value(attr)
    if not title and value in (None, "", "None"):
        return False
    return True


def get_user_attributes(obj):
    attrs = safe_call(obj.GetUserAttributes) if hasattr(obj, "GetUserAttributes") else None
    return [attr for attr in (attrs or []) if usable_attribute(attr)]


def append_user_attributes(md_lines, obj, heading):
    attrs = get_user_attributes(obj)
    if not attrs:
        return

    md_lines.append(heading)
    append_attribute_list(md_lines, attrs)


def append_attribute_list(md_lines, attrs, prefix="- "):
    if not attrs:
        return
    for attr in attrs:
        attr_type_key = enum_key(getattr(attr, "Type", None))
        attr_type_name = ATTRIBUTE_TYPE_NAMES.get(attr_type_key, str(getattr(attr, "Type", "Unknown")))
        title = clean_text(getattr(attr, "Title", None), "Unnamed")
        value = attribute_value(attr)
        suffix = ""
        if getattr(attr, "Array", False):
            suffix = f"[{getattr(attr, 'ArrayElementIndex', 0)}]"
        md_lines.append(f"{prefix}{title}{suffix} ({attr_type_name}): {value}")


def serialize_point(point):
    return serialize_xyz(point)


def serialize_box(box):
    if box is None:
        return None
    if isinstance(box, dict):
        values = [box.get("x_min"), box.get("y_min"), box.get("z_min"), box.get("x_max"), box.get("y_max"), box.get("z_max")]
    else:
        values = list(box)
    if len(values) < 6:
        return None
    return {
        "x_min": values[0],
        "y_min": values[1],
        "z_min": values[2],
        "x_max": values[3],
        "y_max": values[4],
        "z_max": values[5],
        "dx": values[3] - values[0],
        "dy": values[4] - values[1],
        "dz": values[5] - values[2],
    }


def serialize_attributes(attrs):
    rows = []
    for attr in attrs or []:
        rows.append(
            {
                "title": clean_text(getattr(attr, "Title", None), "Unnamed"),
                "type": ATTRIBUTE_TYPE_NAMES.get(enum_key(getattr(attr, "Type", None)), "Unknown"),
                "value": attribute_value(attr),
                "is_array": bool(getattr(attr, "Array", False)),
                "array_index": getattr(attr, "ArrayElementIndex", None),
            }
        )
    return rows


def get_member_value(obj, *candidate_names):
    member = resolve_member_relaxed(obj, *candidate_names)
    if member is None:
        return None
    if callable(member):
        return safe_call(member)
    return member


def serialize_display_properties(obj):
    if obj is None:
        return None

    payload = {}
    integer_fields = {
        "color_index": ("Color", "ColorIndex", "ColorNumber"),
        "layer": ("Layer", "LayerIndex"),
        "translucency": ("Translucency", "TranslucencyValue"),
        "line_font": ("LineFont", "Font", "LineFontIndex"),
        "line_width": ("LineWidth", "Width", "LineWidthIndex"),
    }
    for key, candidate_names in integer_fields.items():
        value = get_member_value(obj, *candidate_names)
        if value is None:
            continue
        coerced = enum_key(value)
        if coerced is None:
            try:
                coerced = int(value)
            except Exception:
                continue
        payload[key] = coerced

    blanked = get_member_value(obj, "IsBlanked", "Blanked", "BlankStatus")
    if blanked is not None:
        payload["is_blanked"] = bool(blanked)

    name = clean_text(get_member_value(obj, "Name"), "")
    if name:
        payload["name"] = name

    return payload or None


def serialize_component_context(nx_object):
    if nx_object is None:
        return None

    component = get_member_value(nx_object, "OwningComponent")
    if component is None:
        return None

    payload = {
        "name": clean_text(get_member_value(component, "Name"), ""),
        "display_name": clean_text(get_member_value(component, "DisplayName"), ""),
        "journal_identifier": clean_text(get_member_value(component, "JournalIdentifier"), ""),
        "display_properties": serialize_display_properties(component),
    }

    prototype = get_member_value(component, "Prototype")
    if prototype is not None:
        payload["prototype_name"] = clean_text(get_member_value(prototype, "Name"), "")
        payload["prototype_full_path"] = clean_text(get_member_value(prototype, "FullPath"), "")

    owning_part = get_member_value(component, "OwningPart")
    if owning_part is not None:
        payload["owning_part_name"] = clean_text(get_member_value(owning_part, "Name"), "")
        payload["owning_part_full_path"] = clean_text(get_member_value(owning_part, "FullPath"), "")

    payload = {key: value for key, value in payload.items() if value not in (None, "", {})}
    return payload or None


def serialize_object_identity(nx_object):
    if nx_object is None:
        return None

    payload = {
        "tag": get_nx_tag(nx_object),
        "journal_identifier": clean_text(get_member_value(nx_object, "JournalIdentifier"), ""),
        "name": clean_text(get_member_value(nx_object, "Name"), ""),
        "class_name": clean_text(type(nx_object).__name__, ""),
    }

    owning_part = get_member_value(nx_object, "OwningPart")
    if owning_part is not None:
        payload["owning_part_name"] = clean_text(get_member_value(owning_part, "Name"), "")
        payload["owning_part_full_path"] = clean_text(get_member_value(owning_part, "FullPath"), "")

    prototype = get_member_value(nx_object, "Prototype")
    if prototype is not None:
        payload["prototype_name"] = clean_text(get_member_value(prototype, "Name"), "")
        payload["prototype_full_path"] = clean_text(get_member_value(prototype, "FullPath"), "")

    payload = {key: value for key, value in payload.items() if value not in (None, "", {})}
    return payload or None


def serialize_xyz(values):
    if values is None:
        return None
    if isinstance(values, dict):
        x = values.get("x", values.get("X"))
        y = values.get("y", values.get("Y"))
        z = values.get("z", values.get("Z"))
        if x is None or y is None or z is None:
            return None
        return {"x": x, "y": y, "z": z}

    x = getattr(values, "X", None)
    y = getattr(values, "Y", None)
    z = getattr(values, "Z", None)
    if x is not None and y is not None and z is not None:
        return {"x": x, "y": y, "z": z}

    items = to_number_list(values)
    if items is None or len(items) < 3:
        return None
    return {"x": items[0], "y": items[1], "z": items[2]}


def serialize_number_list(values):
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        return values
    try:
        return [serialize_generic(value) for value in values]
    except Exception:
        return None


def serialize_generic(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: serialize_generic(item) for key, item in value.items()}
    if isinstance(value, (bool, int, float, str)):
        return value
    point = serialize_point(value)
    if point is not None:
        return point
    xyz = serialize_xyz(value)
    if xyz is not None:
        return xyz
    number_list = serialize_number_list(value)
    if number_list is not None:
        return number_list
    return clean_text(value)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def to_number_list(value):
    if value is None or isinstance(value, (str, bytes, dict)):
        return None
    if hasattr(value, "__iter__"):
        try:
            items = list(value)
        except Exception:
            return None
        if all(is_number(item) for item in items):
            return items
    return None


def looks_like_xyz(value):
    items = to_number_list(value)
    return items is not None and len(items) == 3


def looks_like_pair(value):
    items = to_number_list(value)
    return items is not None and len(items) == 2


def looks_like_box6(value):
    items = to_number_list(value)
    return items is not None and len(items) == 6


def looks_like_uv4(value):
    items = to_number_list(value)
    return items is not None and len(items) == 4


def call_with_fallbacks(method, *variants):
    for args in variants:
        try:
            return method(*args)
        except Exception:
            continue
    return None


def call_with_diagnostics(scope, method, *variants):
    last_error = None
    for args in variants:
        ok, result, error = safe_call_with_error(method, *args)
        if ok:
            add_diagnostic(scope, "call_succeeded", args_count=len(args), returned_type=type(result).__name__)
            return result
        last_error = error
    if last_error is not None:
        add_diagnostic(scope, "call_failed", error=last_error)
    return None


def append_runtime_diagnostics_summary(md_lines, diagnostics):
    if not diagnostics:
        return
    summary = {}
    for entry in diagnostics:
        key = (entry.get("scope"), entry.get("message"))
        bucket = summary.setdefault(key, {"count": 0, "context": entry.get("context")})
        bucket["count"] += 1

    md_lines.append("\n## Runtime Diagnostics Summary")
    for (scope, message), info in sorted(summary.items()):
        line = f"- {scope}: {message} x{info['count']}"
        context = info.get("context") or {}
        if message == "call_failed" and context.get("error"):
            line += f" | example error: {context['error']}"
        if message == "api_unavailable" and context.get("reason"):
            line += f" | reason: {context['reason']}"
        md_lines.append(line)


def sequenceize(value):
    if value is None:
        return []
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return value
    return [value]


def first_scalar(values, default=None):
    for value in values:
        if is_number(value):
            return value
    return default


def first_matching(values, predicate):
    for value in values:
        if predicate(value):
            return value
    return None


def public_attrs(obj):
    try:
        names = [name for name in dir(obj) if not name.startswith("_")]
        return sorted(names)[:80]
    except Exception:
        return []


def serialize_public_object(obj, depth=1, max_fields=40):
    if obj is None or depth < 0:
        return None
    payload = {}
    for name in public_attrs(obj)[:max_fields]:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        serialized = serialize_public_value(value, depth - 1)
        if serialized in (None, {}, []):
            continue
        payload[name] = serialized
    return payload or None


def serialize_public_value(value, depth=1):
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    generic = serialize_generic(value)
    if generic is not None and not isinstance(generic, str):
        return generic
    if depth <= 0:
        return clean_text(value, "")
    return serialize_public_object(value, depth=depth)


def get_attr_candidates(obj, *names):
    if obj is None:
        return None
    candidate_names = []
    for name in names:
        parts = name.split("_")
        candidate_names.extend(
            [
                name,
                name.lower(),
                name.upper(),
                "".join(parts),
                "".join(part.capitalize() for part in parts),
                parts[0] + "".join(part.capitalize() for part in parts[1:]),
            ]
        )
    seen = set()
    for candidate in candidate_names:
        if candidate in seen:
            continue
        seen.add(candidate)
        if hasattr(obj, candidate):
            return getattr(obj, candidate)
    return None


def parse_edge_verts_result(result):
    values = sequenceize(result)
    xyzs = [value for value in values if looks_like_xyz(value)]
    scalar = first_scalar(values)
    if len(xyzs) >= 2:
        return {
            "vertex_count": int(scalar) if scalar is not None else (2 if xyzs[0] != xyzs[1] else 1),
            "start_point": serialize_xyz(xyzs[0]),
            "end_point": serialize_xyz(xyzs[1]),
        }
    return None


def parse_curve_points_result(result):
    values = sequenceize(result)
    xyzs = [serialize_xyz(value) for value in values if looks_like_xyz(value)]
    xyzs = [value for value in xyzs if value is not None]
    if len(xyzs) >= 3:
        return dedupe_point_sequence(xyzs)

    number_lists = [to_number_list(value) for value in values]
    for numbers in number_lists:
        if numbers is not None and len(numbers) >= 9 and len(numbers) % 3 == 0:
            points = []
            for index in range(0, len(numbers), 3):
                points.append({"x": numbers[index], "y": numbers[index + 1], "z": numbers[index + 2]})
            deduped = dedupe_point_sequence(points)
            if len(deduped) >= 3:
                return deduped

    flat_numbers = [value for value in values if is_number(value)]
    if len(flat_numbers) >= 9 and len(flat_numbers) % 3 == 0:
        points = []
        for index in range(0, len(flat_numbers), 3):
            points.append({"x": flat_numbers[index], "y": flat_numbers[index + 1], "z": flat_numbers[index + 2]})
        deduped = dedupe_point_sequence(points)
        if len(deduped) >= 3:
            return deduped
    return None


def parse_face_data_result(result):
    values = sequenceize(result)
    scalar_values = [value for value in values if is_number(value)]
    xyzs = [value for value in values if looks_like_xyz(value)]
    box = first_matching(values, looks_like_box6)
    if len(xyzs) >= 2 and box is not None:
        radius = None
        radius_data = None
        normal_direction = None
        if len(scalar_values) >= 2:
            radius = scalar_values[1]
        if len(scalar_values) >= 4:
            radius_data = scalar_values[2]
            normal_direction = int(scalar_values[3])
        elif len(scalar_values) >= 3:
            normal_direction = int(scalar_values[2])
        return {
            "face_type_code": int(scalar_values[0]) if len(scalar_values) >= 1 else None,
            "reference_point": xyzs[0],
            "direction": xyzs[1],
            "box": box,
            "radius": radius,
            "radius_data": radius_data,
            "normal_direction": normal_direction,
        }
    return None


def parse_face_props_result(result, uv):
    values = sequenceize(result)
    xyzs = [value for value in values if looks_like_xyz(value)]
    pairs = [value for value in values if looks_like_pair(value)]
    radii = first_matching(values, looks_like_pair)
    if len(xyzs) >= 6:
        return {
            "uv": {"u": uv[0], "v": uv[1]},
            "point": serialize_xyz(xyzs[0]),
            "u_tangent": serialize_xyz(xyzs[1]),
            "v_tangent": serialize_xyz(xyzs[2]),
            "u_curvature": serialize_xyz(xyzs[3]),
            "v_curvature": serialize_xyz(xyzs[4]),
            "unit_normal": serialize_xyz(xyzs[5]),
            "principal_radii": to_number_list(radii) if radii is not None else (to_number_list(pairs[-1]) if pairs else None),
            "source": "AskFaceProps",
        }
    return None


def point_key(point, digits=8):
    serialized = serialize_generic(point)
    if not isinstance(serialized, dict):
        return None
    x = serialized.get("x")
    y = serialized.get("y")
    z = serialized.get("z")
    if x is None or y is None or z is None:
        return None
    return (round(x, digits), round(y, digits), round(z, digits))


def resolve_attr_path(root, path):
    current = root
    for part in path.split("."):
        current = getattr(current, part, None)
        if current is None:
            return None
    return current


def instantiate_uf_class(*candidates):
    for path in candidates:
        constructor = resolve_attr_path(NXOpen.UF, path)
        if constructor is None:
            continue
        try:
            return constructor()
        except Exception:
            continue
    return None


def serialize_plane_equation(reference_point, normal):
    point = serialize_xyz(reference_point)
    direction = serialize_xyz(normal)
    if point is None or direction is None:
        return None
    a = direction["x"]
    b = direction["y"]
    c = direction["z"]
    d = -(a * point["x"] + b * point["y"] + c * point["z"])
    return {"a": a, "b": b, "c": c, "d": d}


def get_matrix_values(uf_session, matrix_tag):
    if not matrix_tag:
        return None
    csys = getattr(uf_session, "Csys", None)
    if csys is None or not hasattr(csys, "AskMatrixValues"):
        return None

    result = call_with_diagnostics("AskMatrixValues", csys.AskMatrixValues, (matrix_tag,))
    values = None
    if looks_like_box6(result):
        values = None
    sequence_values = sequenceize(result)
    number_list = to_number_list(result)
    if number_list is not None and len(number_list) == 9:
        values = number_list
    else:
        nested_numbers = first_matching(sequence_values, lambda item: to_number_list(item) is not None and len(to_number_list(item)) == 9)
        if nested_numbers is not None:
            values = to_number_list(nested_numbers)
        else:
            flat_numbers = [value for value in sequence_values if is_number(value)]
            if len(flat_numbers) >= 9:
                values = flat_numbers[:9]

    if values is not None:
        return {
            "values": values,
            "x_axis": serialize_xyz(values[0:3]),
            "y_axis": serialize_xyz(values[3:6]),
            "z_axis": serialize_xyz(values[6:9]),
        }

    matrix_values = [0.0] * 9
    try:
        csys.AskMatrixValues(matrix_tag, matrix_values)
    except Exception:
        return None
    return {
        "values": matrix_values,
        "x_axis": serialize_xyz(matrix_values[0:3]),
        "y_axis": serialize_xyz(matrix_values[3:6]),
        "z_axis": serialize_xyz(matrix_values[6:9]),
    }


def ask_curve_struct(curve_api, curve_tag):
    if curve_api is None or not hasattr(curve_api, "AskCurveStruct"):
        return None

    result = call_with_diagnostics("AskCurveStruct", curve_api.AskCurveStruct, (curve_tag,))
    if result is not None:
        values = sequenceize(result)
        for value in values:
            if hasattr(value, "crv_type"):
                return value

    struct_object = instantiate_uf_class("UFCurve.Struct", "UFCurve.UFCurveStruct", "UFCurveStruct")
    attempts = []
    if struct_object is not None:
        attempts.append(struct_object)
        attempts.append([struct_object])
    attempts.append([None])

    for output_holder in attempts:
        try:
            curve_api.AskCurveStruct(curve_tag, output_holder)
            if isinstance(output_holder, list):
                return output_holder[0]
            return output_holder
        except Exception:
            continue
    return None


def serialize_curve_struct(curve_struct):
    if curve_struct is None:
        return None
    curve_type = getattr(curve_struct, "crv_type", None)
    return {
        "curve_type_code": curve_type,
        "curve_type_name": CURVE_STRUCT_TYPE_NAMES.get(curve_type, curve_type),
        "parameter_origin": getattr(curve_struct, "crv_t0", None),
        "parameter_scale": getattr(curve_struct, "crv_tscale", None),
        "is_periodic": bool(getattr(curve_struct, "curve_periodic", 0)),
    }


def ask_struct_output(api_method, class_paths):
    struct_object = instantiate_uf_class(*class_paths)
    if struct_object is None:
        return None
    attempts = [struct_object, [struct_object]]
    for output_holder in attempts:
        try:
            api_method(output_holder)
            if isinstance(output_holder, list):
                return output_holder[0]
            return output_holder
        except Exception:
            continue
    return None


def get_curve_exact_geometry(uf_session, edge_tag, edge_type_name):
    curve_api = getattr(uf_session, "Curve", None)
    if curve_api is None or edge_tag is None:
        return None

    curve_struct = ask_curve_struct(curve_api, edge_tag)
    geometry = {
        "curve_structure": serialize_curve_struct(curve_struct),
    }

    if edge_type_name == "Linear" and hasattr(curve_api, "AskLineData"):
        line = call_with_diagnostics("AskLineData", curve_api.AskLineData, (edge_tag,))
        original_line = line
        line_start = None
        line_end = None
        if isinstance(line, (list, tuple)):
            xyzs = [item for item in line if looks_like_xyz(item)]
            if len(xyzs) >= 2:
                line_start = serialize_xyz(xyzs[0])
                line_end = serialize_xyz(xyzs[1])
            line = first_matching(line, lambda item: hasattr(item, "start_point") or hasattr(item, "end_point")) or line
        inspect_line = line if line is not None else original_line
        if inspect_line is not None:
            add_diagnostic_once("AskLineData", "line_attrs", attrs=public_attrs(inspect_line))
            if line_start is None:
                line_start = serialize_generic(get_attr_candidates(inspect_line, "start_point", "start", "point1"))
            if line_end is None:
                line_end = serialize_generic(get_attr_candidates(inspect_line, "end_point", "end", "point2"))
        if line_start is None and line_end is None:
            fallback_line = ask_struct_output(
                lambda holder: curve_api.AskLineData(edge_tag, holder),
                ("UFCurve.Line", "UFCurve.UFCurveLine", "UFCurveLine"),
            )
            if fallback_line is not None:
                add_diagnostic_once("AskLineData", "fallback_line_attrs", attrs=public_attrs(fallback_line))
                if line_start is None:
                    line_start = serialize_generic(get_attr_candidates(fallback_line, "start_point", "start", "point1"))
                if line_end is None:
                    line_end = serialize_generic(get_attr_candidates(fallback_line, "end_point", "end", "point2"))
        geometry["analytic_curve"] = {
            "type": "Line",
            "start_point": line_start,
            "end_point": line_end,
        }
    elif edge_type_name == "Circular" and hasattr(curve_api, "AskArcData"):
        arc = call_with_diagnostics("AskArcData", curve_api.AskArcData, (edge_tag,))
        original_arc = arc
        if isinstance(arc, (list, tuple)):
            arc = first_matching(arc, lambda item: hasattr(item, "arc_center") or hasattr(item, "radius")) or arc
        inspect_arc = arc if arc is not None else original_arc
        center = None
        radius = None
        start_angle = None
        end_angle = None
        matrix_tag = None
        if inspect_arc is not None:
            add_diagnostic_once("AskArcData", "arc_attrs", attrs=public_attrs(inspect_arc))
            center = serialize_generic(get_attr_candidates(inspect_arc, "arc_center", "center"))
            radius = get_attr_candidates(inspect_arc, "radius", "arc_radius")
            start_angle = get_attr_candidates(inspect_arc, "start_angle")
            end_angle = get_attr_candidates(inspect_arc, "end_angle")
            matrix_tag = get_attr_candidates(inspect_arc, "matrix_tag", "matrix")
        if center is None and radius is None:
            fallback_arc = ask_struct_output(
                lambda holder: curve_api.AskArcData(edge_tag, holder),
                ("UFCurve.Arc", "UFCurve.UFCurveArc", "UFCurveArc"),
            )
            if fallback_arc is not None:
                add_diagnostic_once("AskArcData", "fallback_arc_attrs", attrs=public_attrs(fallback_arc))
                center = center or serialize_generic(get_attr_candidates(fallback_arc, "arc_center", "center"))
                radius = radius if radius is not None else get_attr_candidates(fallback_arc, "radius", "arc_radius")
                start_angle = start_angle if start_angle is not None else get_attr_candidates(fallback_arc, "start_angle")
                end_angle = end_angle if end_angle is not None else get_attr_candidates(fallback_arc, "end_angle")
                matrix_tag = matrix_tag or get_attr_candidates(fallback_arc, "matrix_tag", "matrix")
        if inspect_arc is not None or center is not None or radius is not None:
            geometry["analytic_curve"] = {
                "type": "Arc",
                "radius": radius,
                "center": center,
                "start_angle": start_angle,
                "end_angle": end_angle,
                "matrix_tag": matrix_tag,
                "matrix": get_matrix_values(uf_session, matrix_tag),
            }
    elif edge_type_name == "Elliptical" and hasattr(curve_api, "AskConicData"):
        conic = call_with_diagnostics("AskConicData", curve_api.AskConicData, (edge_tag,))
        if isinstance(conic, (list, tuple)):
            conic = first_matching(conic, lambda item: hasattr(item, "center") or hasattr(item, "k1")) or conic
        if conic is None or (not hasattr(conic, "center") and not hasattr(conic, "k1")):
            conic = ask_struct_output(
            lambda holder: curve_api.AskConicData(edge_tag, holder),
            ("UFCurve.Conic", "UFCurve.UFCurveConic", "UFCurveConic"),
            )
        if conic is not None:
            matrix_tag = getattr(conic, "matrix_tag", None)
            geometry["analytic_curve"] = {
                "type": "Conic",
                "conic_type": getattr(conic, "conic_type", None),
                "rotation_angle": getattr(conic, "rotation_angle", None),
                "start_param": getattr(conic, "start_param", None),
                "end_param": getattr(conic, "end_param", None),
                "center": serialize_generic(getattr(conic, "center", None)),
                "k1": getattr(conic, "k1", None),
                "k2": getattr(conic, "k2", None),
                "matrix_tag": matrix_tag,
                "matrix": get_matrix_values(uf_session, matrix_tag),
            }
    elif edge_type_name in ("Spline", "TrimmedCurve", "SpCurve") and hasattr(curve_api, "AskSplineData"):
        spline = call_with_diagnostics("AskSplineData", curve_api.AskSplineData, (edge_tag,))
        if isinstance(spline, (list, tuple)):
            spline = first_matching(spline, lambda item: hasattr(item, "order") or hasattr(item, "num_poles")) or spline
        if spline is None or (not hasattr(spline, "order") and not hasattr(spline, "num_poles")):
            spline = ask_struct_output(
            lambda holder: curve_api.AskSplineData(edge_tag, holder),
            ("UFCurve.Spline", "UFCurve.UFCurveSpline", "UFCurveSpline"),
            )
        if spline is not None:
            raw_struct = serialize_public_object(spline, depth=2)
            geometry["analytic_curve"] = {
                "type": "Spline",
                "order": getattr(spline, "order", None),
                "num_poles": getattr(spline, "num_poles", None),
                "is_rational": bool(getattr(spline, "is_rational", 0)),
                "start_param": getattr(spline, "start_param", None),
                "end_param": getattr(spline, "end_param", None),
                "raw_struct": raw_struct,
            }
            sample_points = infer_curve_sample_points(geometry["analytic_curve"])
            if sample_points:
                geometry["analytic_curve"]["sample_points"] = sample_points

    if edge_type_name == "Intersection" and hasattr(curve_api, "AskIntCurveParents"):
        int_curve_object = [0]
        input_objects = []
        try:
            curve_api.AskIntCurveParents(edge_tag, int_curve_object, input_objects)
            geometry["intersection_curve_parents"] = {
                "intersection_object_tag": int_curve_object[0] if isinstance(int_curve_object, list) else int_curve_object,
                "input_object_tags": input_objects,
            }
        except Exception:
            pass

    if not geometry.get("curve_structure") and not geometry.get("analytic_curve") and not geometry.get("intersection_curve_parents"):
        return None
    return geometry


def get_edge_vertices_uf(uf_session, edge_tag):
    modl, ask_edge_verts = resolve_modeling_method(uf_session, "AskEdgeVerts")
    if modl is None or ask_edge_verts is None or edge_tag is None:
        return None

    result = call_with_diagnostics("AskEdgeVerts", ask_edge_verts, (edge_tag,))
    parsed = parse_edge_verts_result(result)
    if parsed is not None:
        return parsed

    point1 = [0.0, 0.0, 0.0]
    point2 = [0.0, 0.0, 0.0]
    vertex_count = [0]
    try:
        ask_edge_verts(edge_tag, point1, point2, vertex_count)
        return {
            "vertex_count": vertex_count[0] if isinstance(vertex_count, list) else vertex_count,
            "start_point": serialize_xyz(point1),
            "end_point": serialize_xyz(point2),
        }
    except Exception:
        return None


def get_curve_sample_points_uf(uf_session, curve_tag):
    curve_api = getattr(uf_session, "Curve", None)
    ask_curve_points = resolve_member_relaxed(curve_api, "AskCurvePoints") if curve_api is not None else None
    if ask_curve_points is None:
        add_diagnostic_once("AskCurvePoints", "api_unavailable")
        return None
    if curve_tag is None:
        return None

    result = call_with_diagnostics(
        "AskCurvePoints",
        ask_curve_points,
        (curve_tag, 0.0, 0.0, 0.0),
        (curve_tag, 0.01, 0.5, 5.0),
    )
    parsed = parse_curve_points_result(result)
    if parsed is not None:
        return parsed

    numpts = [0]
    pts = [0.0] * (3 * 2048)
    attempts = [
        (curve_tag, 0.0, 0.0, 0.0, numpts, pts),
        (curve_tag, 0.01, 0.5, 5.0, numpts, pts),
    ]
    for args in attempts:
        try:
            ask_curve_points(*args)
            count = numpts[0] if isinstance(numpts, list) and numpts else 0
            if count and count >= 3:
                numbers = pts[: count * 3]
                parsed = parse_curve_points_result(numbers)
                if parsed is not None:
                    return parsed
        except Exception:
            continue
    return None


def get_face_uv_bounds(uf_session, face_tag):
    modl, ask_face_uv = resolve_modeling_method(uf_session, "AskFaceUvMinmax", "AskFaceUVMinmax")
    if modl is None or ask_face_uv is None:
        add_missing_modeling_method_diagnostic("AskFaceUvMinmax", uf_session, "AskFaceUvMinmax_missing", "face", "uv")
        return None

    result = call_with_diagnostics("AskFaceUvMinmax", ask_face_uv, (face_tag,))
    uv_values = first_matching(sequenceize(result), looks_like_uv4)
    if uv_values is not None:
        return {
            "u_min": uv_values[0],
            "u_max": uv_values[1],
            "v_min": uv_values[2],
            "v_max": uv_values[3],
            "source": "AskFaceUvMinmax",
        }
    if result is not None:
        add_diagnostic_once("AskFaceUvMinmax", "unparsed_result", sample=serialize_generic(result))

    uv_bounds = [0.0, 0.0, 0.0, 0.0]
    try:
        ask_face_uv(face_tag, uv_bounds)
        return {
            "u_min": uv_bounds[0],
            "u_max": uv_bounds[1],
            "v_min": uv_bounds[2],
            "v_max": uv_bounds[3],
            "source": "AskFaceUvMinmax",
        }
    except Exception:
        return None


def get_face_periodicity(uf_session, face_tag):
    modl, ask_face_periodicity = resolve_modeling_method(uf_session, "AskFacePeriodicity")
    if modl is None or ask_face_periodicity is None:
        add_missing_modeling_method_diagnostic("AskFacePeriodicity", uf_session, "AskFacePeriodicity_missing", "face", "period")
        return None

    result = call_with_diagnostics("AskFacePeriodicity", ask_face_periodicity, (face_tag,))
    values = [value for value in sequenceize(result) if is_number(value)]
    if len(values) >= 4:
        return {
            "u_status": int(values[0]),
            "u_period": values[1],
            "v_status": int(values[2]),
            "v_period": values[3],
            "source": "AskFacePeriodicity",
        }
    if result is not None:
        add_diagnostic_once("AskFacePeriodicity", "unparsed_result", sample=serialize_generic(result))

    u_status = [0]
    u_period = [0.0]
    v_status = [0]
    v_period = [0.0]
    try:
        ask_face_periodicity(face_tag, u_status, u_period, v_status, v_period)
        return {
            "u_status": u_status[0],
            "u_period": u_period[0],
            "v_status": v_status[0],
            "v_period": v_period[0],
            "source": "AskFacePeriodicity",
        }
    except Exception:
        return None


def get_face_topology(uf_session, face_tag):
    modl, ask_face_topology = resolve_modeling_method(uf_session, "AskFaceTopology")
    if modl is None or ask_face_topology is None:
        add_missing_modeling_method_diagnostic("AskFaceTopology", uf_session, "AskFaceTopology_missing", "face", "top")
        return None

    result = call_with_diagnostics("AskFaceTopology", ask_face_topology, (face_tag,))
    if is_number(result):
        return {
            "topology_code": int(result),
            "topology_name": FACE_TOPOLOGY_NAMES.get(int(result)),
            "source": "AskFaceTopology",
        }
    scalar = first_scalar(sequenceize(result))
    if scalar is not None:
        return {
            "topology_code": int(scalar),
            "topology_name": FACE_TOPOLOGY_NAMES.get(int(scalar)),
            "source": "AskFaceTopology",
        }
    if result is not None:
        add_diagnostic_once("AskFaceTopology", "unparsed_result", sample=serialize_generic(result))

    topology_type = [0]
    try:
        ask_face_topology(face_tag, topology_type)
        topology_value = topology_type[0] if isinstance(topology_type, list) else topology_type
        return {
            "topology_code": topology_value,
            "topology_name": FACE_TOPOLOGY_NAMES.get(topology_value),
            "source": "AskFaceTopology",
        }
    except Exception:
        return None


def get_face_local_properties(uf_session, face_tag, uv_bounds):
    modl, ask_face_props = resolve_modeling_method(uf_session, "AskFaceProps")
    if modl is None or ask_face_props is None:
        add_missing_modeling_method_diagnostic("AskFaceProps", uf_session, "AskFaceProps_missing", "face", "prop")
        return None

    uv_candidates = []
    if uv_bounds is not None:
        uv_candidates.append(
            [
                (uv_bounds["u_min"] + uv_bounds["u_max"]) / 2.0,
                (uv_bounds["v_min"] + uv_bounds["v_max"]) / 2.0,
            ]
        )
    else:
        add_diagnostic_once("AskFaceProps", "using_default_uv", reason="uv_bounds_missing")
        uv_candidates.extend(([0.0, 0.0], [0.5, 0.5], [-0.5, -0.5]))

    for uv in uv_candidates:
        result = call_with_diagnostics("AskFaceProps", ask_face_props, (face_tag, uv))
        parsed = parse_face_props_result(result, uv)
        if parsed is not None:
            return parsed
        if result is not None:
            add_diagnostic_once("AskFaceProps", "unparsed_result", sample=serialize_generic(result))

        point = [0.0, 0.0, 0.0]
        u1 = [0.0, 0.0, 0.0]
        v1 = [0.0, 0.0, 0.0]
        u2 = [0.0, 0.0, 0.0]
        v2 = [0.0, 0.0, 0.0]
        unit_norm = [0.0, 0.0, 0.0]
        radii = [0.0, 0.0]
        try:
            ask_face_props(face_tag, uv, point, u1, v1, u2, v2, unit_norm, radii)
            return {
                "uv": {"u": uv[0], "v": uv[1]},
                "point": serialize_xyz(point),
                "u_tangent": serialize_xyz(u1),
                "v_tangent": serialize_xyz(v1),
                "u_curvature": serialize_xyz(u2),
                "v_curvature": serialize_xyz(v2),
                "unit_normal": serialize_xyz(unit_norm),
                "principal_radii": radii,
                "source": "AskFaceProps",
            }
        except Exception:
            continue
    return None


def parse_trimmed_bsurface_result(result):
    values = sequenceize(result)
    if not values:
        return None
    bsurface = first_matching(
        values,
        lambda value: hasattr(value, "num_poles_u")
        or hasattr(value, "num_poles_v")
        or hasattr(value, "order_u")
        or hasattr(value, "order_v"),
    )
    if bsurface is None:
        return None
    scalar_values = [value for value in values if is_number(value)]
    number_lists = [list(numbers) for numbers in (to_number_list(value) for value in values) if numbers is not None]
    edge_sense = next(
        (
            numbers
            for numbers in number_lists
            if any(number < 0 for number in numbers) or any(number > 1 for number in numbers)
        ),
        None,
    )
    return {
        "surface": {
            "type": clean_text(getattr(bsurface, "type", None), None),
            "num_poles_u": getattr(bsurface, "num_poles_u", None),
            "num_poles_v": getattr(bsurface, "num_poles_v", None),
            "order_u": getattr(bsurface, "order_u", None),
            "order_v": getattr(bsurface, "order_v", None),
            "is_rational": getattr(bsurface, "is_rational", None),
            "raw_struct": serialize_public_object(bsurface, depth=2),
        },
        "loop_count": int(scalar_values[0]) if len(scalar_values) >= 1 else None,
        "edge_count": int(scalar_values[1]) if len(scalar_values) >= 2 else None,
        "edge_sense": edge_sense,
        "edge_bcurve_tags": None,
        "source": "Ask2dtrimBsurf",
    }


def get_face_bsurface(uf_session, face_tag):
    modl, ask_bsurf = resolve_modeling_method(uf_session, "AskBsurf")
    if modl is None or ask_bsurf is None:
        add_missing_modeling_method_diagnostic("AskBsurf", uf_session, "AskBsurf_missing", "bsurf")
        return None

    bsurface = instantiate_uf_class("UFModl.Bsurface", "UFModl.UFModlBsurface", "UFModlBsurface")
    if bsurface is None:
        return None

    result = call_with_diagnostics("AskBsurf", ask_bsurf, (face_tag,), (face_tag, bsurface))
    parsed = parse_trimmed_bsurface_result(result)
    if parsed is not None and parsed.get("surface"):
        parsed["source"] = "AskBsurf"
        return parsed["surface"]

    try:
        ask_bsurf(face_tag, bsurface)
        return {
            "type": clean_text(getattr(bsurface, "type", None), None),
            "num_poles_u": getattr(bsurface, "num_poles_u", None),
            "num_poles_v": getattr(bsurface, "num_poles_v", None),
            "order_u": getattr(bsurface, "order_u", None),
            "order_v": getattr(bsurface, "order_v", None),
            "is_rational": getattr(bsurface, "is_rational", None),
            "raw_struct": serialize_public_object(bsurface, depth=2),
            "source": "AskBsurf",
        }
    except Exception:
        return None


def _vector_from_flat(values, offset):
    if values is None or len(values) < offset + 3:
        return None
    return {"x": values[offset], "y": values[offset + 1], "z": values[offset + 2]}


def get_face_facet_mesh(uf_session, face_tag, facet_count_hint):
    facet_api = getattr(uf_session, "Facet", None)
    if facet_api is None or face_tag is None:
        add_diagnostic_once("TessellateFace", "api_unavailable", reason="uf_facet_api_missing")
        return None

    tessellation_params = instantiate_uf_class(
        "UFFacet.TessellationParameters",
        "UFFacet.UFFacetTessellationParameters",
        "UFFacetTessellationParameters",
    )
    if tessellation_params is None:
        add_diagnostic_once("TessellateFace", "params_unavailable")
        return None

    for name, value in (
        ("specify_surface_dist_tolerance", True),
        ("surface_dist_tolerance", 0.001),
        ("specify_surface_angular_tolerance", True),
        ("surface_angular_tolerance", 15.0),
        ("specify_curve_dist_tolerance", True),
        ("curve_dist_tolerance", 0.001),
        ("specify_curve_angular_tolerance", True),
        ("curve_angular_tolerance", 15.0),
        ("specify_max_facet_size", False),
        ("max_facet_size", 0.0),
    ):
        try:
            setattr(tessellation_params, name, value)
        except Exception:
            pass

    tessellation = [0]
    try:
        facet_api.TessellateFace(face_tag, tessellation_params, tessellation)
    except Exception as ex:
        add_diagnostic_once("TessellateFace", "call_failed", error=str(ex))
        return None

    tessellation_id = tessellation[0] if isinstance(tessellation, list) else tessellation
    if not tessellation_id:
        return None

    count = [0]
    try:
        facet_api.AskNumFacetsInTessellation(tessellation_id, count)
    except Exception as ex:
        add_diagnostic_once("AskNumFacetsInTessellation", "call_failed", error=str(ex))
        safe_call(facet_api.DeleteTessellation, tessellation_id)
        return None

    facet_total = count[0] if isinstance(count, list) else count
    if not facet_total:
        safe_call(facet_api.DeleteTessellation, tessellation_id)
        return None

    triangles = []
    max_facets = facet_count_hint or facet_total
    max_facets = min(max(int(max_facets), int(facet_total)), 4000)

    try:
        for facet_id in range(1, max_facets + 1):
            vertices = [0.0] * 9
            normals = [0.0] * 9
            try:
                facet_api.AskFacetDataOfTessellation(tessellation_id, facet_id, vertices, normals)
            except Exception:
                continue
            triangle = [_vector_from_flat(vertices, 0), _vector_from_flat(vertices, 3), _vector_from_flat(vertices, 6)]
            if any(point is None for point in triangle):
                continue
            normal = _vector_from_flat(normals, 0)
            triangles.append(
                {
                    "vertices": triangle,
                    "normal": normal,
                }
            )
    finally:
        safe_call(facet_api.DeleteTessellation, tessellation_id)

    if not triangles:
        return None
    return {
        "triangle_count": len(triangles),
        "triangles": triangles,
        "source": "TessellateFace",
    }


def get_face_trimmed_bsurface(uf_session, face_tag, edge_count_hint):
    modl, ask_trimmed_bsurface = resolve_modeling_method(uf_session, "Ask2dtrimBsurf", "Ask2dTrimBsurf")
    if modl is None or ask_trimmed_bsurface is None:
        add_missing_modeling_method_diagnostic("Ask2dtrimBsurf", uf_session, "Ask2dtrimBsurf_missing", "trim", "bsurf")
    else:
        options = [0]
        result = call_with_diagnostics("Ask2dtrimBsurf", ask_trimmed_bsurface, (face_tag, options, 0.0), (face_tag, 0, 0.0))
        parsed = parse_trimmed_bsurface_result(result)
        if parsed is not None:
            return parsed

        bsurface = instantiate_uf_class("UFModl.Bsurface", "UFModl.UFModlBsurface", "UFModlBsurface")
        if bsurface is not None:
            edge_sense = [0] * max(edge_count_hint, 1)
            edge_bcurves = [0] * max(edge_count_hint, 1)
            loop_count = [0]
            edge_count = [0] * max(edge_count_hint, 1)
            attempts = [
                lambda: ask_trimmed_bsurface(face_tag, options, 0.0, bsurface, loop_count, edge_count, edge_sense, edge_bcurves),
                lambda: ask_trimmed_bsurface(face_tag, 0, 0.0, bsurface, loop_count, edge_count, edge_sense, edge_bcurves),
            ]

            for attempt in attempts:
                try:
                    attempt()
                    total_edges = sum(value for value in edge_count if is_number(value))
                    return {
                        "surface": {
                            "type": clean_text(getattr(bsurface, "type", None), None),
                            "num_poles_u": getattr(bsurface, "num_poles_u", None),
                            "num_poles_v": getattr(bsurface, "num_poles_v", None),
                            "order_u": getattr(bsurface, "order_u", None),
                            "order_v": getattr(bsurface, "order_v", None),
                            "is_rational": getattr(bsurface, "is_rational", None),
                            "raw_struct": serialize_public_object(bsurface, depth=2),
                        },
                        "loop_count": loop_count[0] if isinstance(loop_count, list) else loop_count,
                        "edge_count": total_edges if total_edges else edge_count_hint,
                        "edges_per_loop": [value for value in edge_count if is_number(value)],
                        "edge_sense": edge_sense,
                        "edge_bcurve_tags": edge_bcurves,
                        "source": "Ask2dtrimBsurf",
                    }
                except Exception:
                    continue
    bsurface = get_face_bsurface(uf_session, face_tag)
    if bsurface is not None:
        return {
            "surface": bsurface,
            "loop_count": None,
            "edge_count": edge_count_hint,
            "edge_sense": None,
            "edge_bcurve_tags": None,
            "source": "AskBsurf",
        }
    return None


def build_surface_definition(face_type_name, analytic_data, local_properties, trimmed_bsurface=None):
    if analytic_data is None:
        return None
    reference_point = analytic_data.get("reference_point")
    direction = analytic_data.get("direction")
    definition = {
        "type": face_type_name,
        "face_type_code": analytic_data.get("face_type_code"),
        "reference_point": serialize_xyz(reference_point),
        "direction": serialize_xyz(direction),
        "radius": analytic_data.get("radius"),
        "radius_data": analytic_data.get("radius_data"),
        "normal_direction": analytic_data.get("normal_direction"),
        "source": analytic_data.get("source"),
    }
    if local_properties is not None:
        definition["sampled_normal"] = local_properties.get("unit_normal")
        definition["sampled_point"] = local_properties.get("point")
        definition["u_tangent"] = local_properties.get("u_tangent")
        definition["v_tangent"] = local_properties.get("v_tangent")
        definition["principal_radii"] = local_properties.get("principal_radii")
    if trimmed_bsurface is not None:
        definition["trimmed_surface"] = trimmed_bsurface.get("surface")
    if face_type_name == "Planar":
        definition["plane_equation"] = serialize_plane_equation(reference_point, direction)
    elif face_type_name in ("Cylindrical", "Conical", "SurfaceOfRevolution"):
        definition["axis_point"] = serialize_xyz(reference_point)
        definition["axis_direction"] = serialize_xyz(direction)
    return definition


def compute_arc_point(center, radius, angle, matrix):
    center_xyz = serialize_xyz(center)
    if center_xyz is None or radius is None or angle is None:
        return None
    x_axis = ((matrix or {}).get("x_axis")) or {"x": 1.0, "y": 0.0, "z": 0.0}
    y_axis = ((matrix or {}).get("y_axis")) or {"x": 0.0, "y": 1.0, "z": 0.0}
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    return {
        "x": center_xyz["x"] + radius * (cos_a * x_axis["x"] + sin_a * y_axis["x"]),
        "y": center_xyz["y"] + radius * (cos_a * x_axis["y"] + sin_a * y_axis["y"]),
        "z": center_xyz["z"] + radius * (cos_a * x_axis["z"] + sin_a * y_axis["z"]),
    }


def transform_point_by_matrix(point, matrix):
    point_xyz = serialize_xyz(point)
    if point_xyz is None or matrix is None:
        return None
    x_axis = (matrix or {}).get("x_axis")
    y_axis = (matrix or {}).get("y_axis")
    z_axis = (matrix or {}).get("z_axis")
    if x_axis is None or y_axis is None or z_axis is None:
        return None
    return {
        "x": point_xyz["x"] * x_axis["x"] + point_xyz["y"] * y_axis["x"] + point_xyz["z"] * z_axis["x"],
        "y": point_xyz["x"] * x_axis["y"] + point_xyz["y"] * y_axis["y"] + point_xyz["z"] * z_axis["y"],
        "z": point_xyz["x"] * x_axis["z"] + point_xyz["y"] * y_axis["z"] + point_xyz["z"] * z_axis["z"],
    }


def dedupe_point_sequence(points):
    sequence = []
    for point in points or []:
        point_xyz = serialize_xyz(point)
        key = point_key(point_xyz)
        if point_xyz is None or key is None:
            continue
        if sequence and point_key(sequence[-1]) == key:
            continue
        sequence.append(point_xyz)
    return sequence


def collect_xyz_points(value, limit=256):
    points = []

    def visit(item):
        if len(points) >= limit or item is None:
            return
        xyz = serialize_xyz(item)
        if xyz is not None:
            points.append(xyz)
            return
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
                if len(points) >= limit:
                    return
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
                if len(points) >= limit:
                    return

    visit(value)
    return dedupe_point_sequence(points)


def infer_curve_sample_points(analytic_curve):
    if not isinstance(analytic_curve, dict):
        return None

    for key in ("sample_points", "fit_points", "control_points", "poles", "points"):
        points = collect_xyz_points(analytic_curve.get(key))
        if len(points) >= 3:
            return points

    raw_struct = analytic_curve.get("raw_struct")
    if isinstance(raw_struct, dict):
        preferred_keys = (
            "fit_pts",
            "fit_points",
            "control_points",
            "ctrl_pts",
            "poles",
            "pole",
            "pts",
            "points",
        )
        for key in preferred_keys:
            if key in raw_struct:
                points = collect_xyz_points(raw_struct.get(key))
                if len(points) >= 3:
                    return points
        points = collect_xyz_points(raw_struct)
        if len(points) >= 3:
            return points
    return None


def build_edge_preview_points(edge_type_name, exact_geometry, curve_measurement, start_point=None, end_point=None):
    analytic_curve = ((exact_geometry or {}).get("analytic_curve")) or {}
    start_xyz = serialize_xyz(
        ((curve_measurement or {}).get("start_point"))
        or analytic_curve.get("start_point")
        or start_point
    )
    end_xyz = serialize_xyz(
        ((curve_measurement or {}).get("end_point"))
        or analytic_curve.get("end_point")
        or end_point
    )

    if edge_type_name == "Linear":
        return dedupe_point_sequence([start_xyz, end_xyz])

    if analytic_curve.get("type") == "Arc":
        center = serialize_xyz((curve_measurement or {}).get("center") or analytic_curve.get("center"))
        radius = analytic_curve.get("radius") or ((curve_measurement or {}).get("radius"))
        start_angle = analytic_curve.get("start_angle")
        end_angle = analytic_curve.get("end_angle")
        matrix = analytic_curve.get("matrix")
        if center is not None and radius is not None and start_angle is not None and end_angle is not None:
            try:
                start_angle = float(start_angle)
                end_angle = float(end_angle)
                radius = float(radius)
                sweep = end_angle - start_angle
                is_closed = (
                    start_xyz is not None
                    and end_xyz is not None
                    and point_key(start_xyz) == point_key(end_xyz)
                )
                if sweep <= 1e-9 and is_closed:
                    sweep = 2.0 * math.pi
                elif sweep < 0.0:
                    sweep += 2.0 * math.pi
                if sweep > 1e-6:
                    segment_count = max(12, min(72, int(math.ceil(abs(sweep) / (math.pi / 18.0)))))
                    preview = [
                        compute_arc_point(center, radius, start_angle + (sweep * index / segment_count), matrix)
                        for index in range(segment_count + 1)
                    ]
                    preview = dedupe_point_sequence(preview)
                    if preview:
                        if start_xyz is not None:
                            preview[0] = start_xyz
                        if end_xyz is not None:
                            preview[-1] = end_xyz
                        return preview
            except Exception:
                pass

    if analytic_curve.get("type") == "Spline":
        sample_points = infer_curve_sample_points(analytic_curve)
        if sample_points:
            if start_xyz is not None:
                sample_points[0] = start_xyz
            if end_xyz is not None:
                sample_points[-1] = end_xyz
            return dedupe_point_sequence(sample_points)

    return dedupe_point_sequence([start_xyz, end_xyz])


def xyz_subtract(a, b):
    a_xyz = serialize_xyz(a)
    b_xyz = serialize_xyz(b)
    if a_xyz is None or b_xyz is None:
        return None
    return {
        "x": a_xyz["x"] - b_xyz["x"],
        "y": a_xyz["y"] - b_xyz["y"],
        "z": a_xyz["z"] - b_xyz["z"],
    }


def xyz_length(vector):
    vector_xyz = serialize_xyz(vector)
    if vector_xyz is None:
        return None
    return math.sqrt(vector_xyz["x"] ** 2 + vector_xyz["y"] ** 2 + vector_xyz["z"] ** 2)


def xyz_distance(a, b):
    return xyz_length(xyz_subtract(a, b))


def xyz_dot(a, b):
    a_xyz = serialize_xyz(a)
    b_xyz = serialize_xyz(b)
    if a_xyz is None or b_xyz is None:
        return None
    return a_xyz["x"] * b_xyz["x"] + a_xyz["y"] * b_xyz["y"] + a_xyz["z"] * b_xyz["z"]


def xyz_scale(vector, scalar):
    vector_xyz = serialize_xyz(vector)
    if vector_xyz is None or scalar is None:
        return None
    return {
        "x": vector_xyz["x"] * scalar,
        "y": vector_xyz["y"] * scalar,
        "z": vector_xyz["z"] * scalar,
    }


def xyz_add(a, b):
    a_xyz = serialize_xyz(a)
    b_xyz = serialize_xyz(b)
    if a_xyz is None or b_xyz is None:
        return None
    return {
        "x": a_xyz["x"] + b_xyz["x"],
        "y": a_xyz["y"] + b_xyz["y"],
        "z": a_xyz["z"] + b_xyz["z"],
    }


def xyz_midpoint(a, b):
    a_xyz = serialize_xyz(a)
    b_xyz = serialize_xyz(b)
    if a_xyz is None or b_xyz is None:
        return None
    return {
        "x": (a_xyz["x"] + b_xyz["x"]) / 2.0,
        "y": (a_xyz["y"] + b_xyz["y"]) / 2.0,
        "z": (a_xyz["z"] + b_xyz["z"]) / 2.0,
    }


def xyz_normalize(vector):
    vector_xyz = serialize_xyz(vector)
    length = xyz_length(vector_xyz)
    if vector_xyz is None or not length:
        return None
    return {
        "x": vector_xyz["x"] / length,
        "y": vector_xyz["y"] / length,
        "z": vector_xyz["z"] / length,
    }


def xyz_cross(a, b):
    a_xyz = serialize_xyz(a)
    b_xyz = serialize_xyz(b)
    if a_xyz is None or b_xyz is None:
        return None
    return {
        "x": a_xyz["y"] * b_xyz["z"] - a_xyz["z"] * b_xyz["y"],
        "y": a_xyz["z"] * b_xyz["x"] - a_xyz["x"] * b_xyz["z"],
        "z": a_xyz["x"] * b_xyz["y"] - a_xyz["y"] * b_xyz["x"],
    }


def box_from_points(points):
    serialized = [serialize_xyz(point) for point in points if serialize_xyz(point) is not None]
    if not serialized:
        return None
    xs = [point["x"] for point in serialized]
    ys = [point["y"] for point in serialized]
    zs = [point["z"] for point in serialized]
    return [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)]


def circle_box(center, radius, matrix):
    if radius is None:
        return None
    sample_points = []
    for angle in (0.0, math.pi / 2.0, math.pi, 3.0 * math.pi / 2.0):
        point = compute_arc_point(center, radius, angle, matrix)
        if point is not None:
            sample_points.append(point)
    return box_from_points(sample_points)


def get_edge_endpoint_points(edge):
    exact_vertices = edge.get("exact_vertices") or {}
    analytic_curve = ((edge.get("exact_geometry") or {}).get("analytic_curve") or {})
    curve_measurement = edge.get("curve_measurement") or {}
    start_point = (
        exact_vertices.get("start_point")
        or edge.get("start_point")
        or analytic_curve.get("start_point")
        or curve_measurement.get("start_point")
    )
    end_point = (
        exact_vertices.get("end_point")
        or edge.get("end_point")
        or analytic_curve.get("end_point")
        or curve_measurement.get("end_point")
    )
    return serialize_xyz(start_point), serialize_xyz(end_point)


def basis_from_normal(normal):
    normal_xyz = xyz_normalize(normal)
    if normal_xyz is None:
        return None, None
    references = (
        {"x": 1.0, "y": 0.0, "z": 0.0},
        {"x": 0.0, "y": 1.0, "z": 0.0},
        {"x": 0.0, "y": 0.0, "z": 1.0},
    )
    tangent_u = None
    for ref in references:
        tangent_u = xyz_normalize(xyz_cross(ref, normal_xyz))
        if tangent_u is not None and (xyz_length(tangent_u) or 0.0) > 1e-9:
            break
    tangent_v = xyz_normalize(xyz_cross(normal_xyz, tangent_u)) if tangent_u is not None else None
    return tangent_u, tangent_v


def project_points_to_basis(points, origin, u_axis, v_axis):
    origin_xyz = serialize_xyz(origin)
    u_xyz = serialize_xyz(u_axis)
    v_xyz = serialize_xyz(v_axis)
    if origin_xyz is None or u_xyz is None or v_xyz is None:
        return None
    u_values = []
    v_values = []
    for point in points:
        offset = xyz_subtract(point, origin_xyz)
        u_value = xyz_dot(offset, u_xyz)
        v_value = xyz_dot(offset, v_xyz)
        if u_value is None or v_value is None:
            continue
        u_values.append(u_value)
        v_values.append(v_value)
    if not u_values or not v_values:
        return None
    return {
        "u_min": min(u_values),
        "u_max": max(u_values),
        "v_min": min(v_values),
        "v_max": max(v_values),
    }


def infer_face_analytic_data(face, edge_lookup):
    edge_indices = face.get("edge_indices", [])
    edges = [edge_lookup.get(edge_index) for edge_index in edge_indices if edge_lookup.get(edge_index) is not None]
    if not edges:
        return None

    endpoint_points = []
    for edge in edges:
        start_point, end_point = get_edge_endpoint_points(edge)
        if start_point is not None:
            endpoint_points.append(start_point)
        if end_point is not None:
            endpoint_points.append(end_point)

    box = box_from_points(endpoint_points)
    face_type_name = face.get("type")
    face_type_code = next((code for code, name in FACE_TYPE_NAMES.items() if name == face_type_name), None)
    reference_point = ((face.get("measurement") or {}).get("anchor_point")) or (endpoint_points[0] if endpoint_points else None)

    if face_type_name == "Planar":
        vectors = []
        for edge in edges:
            if edge.get("type") != "Linear":
                continue
            start_point, end_point = get_edge_endpoint_points(edge)
            vector = xyz_subtract(end_point, start_point)
            if vector is not None and (xyz_length(vector) or 0.0) > 1e-9:
                vectors.append(vector)
        normal = None
        for i in range(len(vectors)):
            for j in range(i + 1, len(vectors)):
                candidate = xyz_normalize(xyz_cross(vectors[i], vectors[j]))
                if candidate is not None and (xyz_length(candidate) or 0.0) > 1e-9:
                    normal = candidate
                    break
            if normal is not None:
                break
        if normal is None and len(edges) == 1 and edges[0].get("type") == "Circular":
            circular_edge = edges[0]
            analytic_curve = ((circular_edge.get("exact_geometry") or {}).get("analytic_curve") or {})
            curve_measurement = circular_edge.get("curve_measurement") or {}
            matrix = analytic_curve.get("matrix")
            normal = ((matrix or {}).get("z_axis")) or {"x": 0.0, "y": 0.0, "z": 1.0}
            radius = curve_measurement.get("radius") or analytic_curve.get("radius")
            center = curve_measurement.get("center") or analytic_curve.get("center") or reference_point
            box = circle_box(center, radius, matrix) or box
            if reference_point is not None:
                return {
                    "face_type_code": face_type_code,
                    "reference_point": reference_point,
                    "direction": normal,
                    "box": box,
                    "radius": radius,
                    "radius_data": None,
                    "normal_direction": None,
                    "source": "inferred_from_edges",
                }
        if reference_point is not None and normal is not None:
            return {
                "face_type_code": face_type_code,
                "reference_point": reference_point,
                "direction": normal,
                "box": box,
                "radius": None,
                "radius_data": None,
                "normal_direction": None,
                "source": "inferred_from_edges",
            }

    if face_type_name == "Cylindrical":
        centers = []
        for edge in edges:
            if edge.get("type") != "Circular":
                continue
            center = ((edge.get("curve_measurement") or {}).get("center")) or (((edge.get("exact_geometry") or {}).get("analytic_curve") or {}).get("center"))
            center_xyz = serialize_xyz(center)
            if center_xyz is not None:
                centers.append(center_xyz)
        axis_direction = None
        if len(centers) >= 2:
            axis_direction = xyz_normalize(xyz_subtract(centers[-1], centers[0]))
        if axis_direction is None:
            for edge in edges:
                if edge.get("type") != "Linear":
                    continue
                start_point, end_point = get_edge_endpoint_points(edge)
                axis_direction = xyz_normalize(xyz_subtract(end_point, start_point))
                if axis_direction is not None:
                    break
        radius = ((face.get("measurement") or {}).get("radius_or_diameter")) or next(
            (
                ((edge.get("curve_measurement") or {}).get("radius"))
                or (((edge.get("exact_geometry") or {}).get("analytic_curve") or {}).get("radius"))
                for edge in edges
                if edge.get("type") == "Circular"
            ),
            None,
        )
        reference_point = centers[0] if centers else reference_point
        if reference_point is not None and axis_direction is not None:
            return {
                "face_type_code": face_type_code,
                "reference_point": reference_point,
                "direction": axis_direction,
                "box": box,
                "radius": radius,
                "radius_data": None,
                "normal_direction": None,
                "source": "inferred_from_edges",
            }

    return None


def enrich_curve_measurement(curve_measurement, exact_geometry):
    if curve_measurement is None:
        return None
    analytic_curve = ((exact_geometry or {}).get("analytic_curve")) or {}
    if curve_measurement.get("center") is None:
        curve_measurement["center"] = analytic_curve.get("center")
    if curve_measurement.get("start_point") is None:
        curve_measurement["start_point"] = analytic_curve.get("start_point")
    if curve_measurement.get("end_point") is None:
        curve_measurement["end_point"] = analytic_curve.get("end_point")
    if analytic_curve.get("type") == "Arc":
        matrix = analytic_curve.get("matrix")
        center = analytic_curve.get("center")
        radius = analytic_curve.get("radius")
        if curve_measurement.get("start_point") is None:
            curve_measurement["start_point"] = compute_arc_point(center, radius, analytic_curve.get("start_angle"), matrix)
        if curve_measurement.get("end_point") is None:
            curve_measurement["end_point"] = compute_arc_point(center, radius, analytic_curve.get("end_angle"), matrix)
    return curve_measurement


def infer_edge_exact_vertices(start_point, end_point):
    start_xyz = serialize_xyz(start_point)
    end_xyz = serialize_xyz(end_point)
    if start_xyz is None or end_xyz is None:
        return None
    return {
        "vertex_count": 1 if point_key(start_xyz) == point_key(end_xyz) else 2,
        "start_point": start_xyz,
        "end_point": end_xyz,
    }


def infer_linear_curve_measurement(length, start_point, end_point):
    start_xyz = serialize_xyz(start_point)
    end_xyz = serialize_xyz(end_point)
    if start_xyz is None or end_xyz is None:
        return None
    return {
        "arc_length": length if length is not None else (xyz_distance(start_xyz, end_xyz)),
        "radius": None,
        "diameter": None,
        "min_radius": None,
        "start_point": start_xyz,
        "end_point": end_xyz,
        "center": xyz_midpoint(start_xyz, end_xyz),
    }


def reconcile_arc_exact_geometry(exact_geometry, curve_measurement, fallback_start=None, fallback_end=None):
    analytic_curve = ((exact_geometry or {}).get("analytic_curve")) or {}
    if analytic_curve.get("type") != "Arc":
        return exact_geometry

    matrix = analytic_curve.get("matrix")
    radius = analytic_curve.get("radius")
    start_angle = analytic_curve.get("start_angle")
    end_angle = analytic_curve.get("end_angle")
    if radius is None or start_angle is None or end_angle is None:
        return exact_geometry

    expected_start = ((curve_measurement or {}).get("start_point")) or fallback_start
    expected_end = ((curve_measurement or {}).get("end_point")) or fallback_end
    expected_center = (curve_measurement or {}).get("center")

    candidates = []
    seen = set()

    def add_candidate(candidate):
        key = point_key(candidate)
        if key is None or key in seen:
            return
        seen.add(key)
        candidates.append(serialize_xyz(candidate))

    raw_center = analytic_curve.get("center")
    add_candidate(raw_center)
    add_candidate(transform_point_by_matrix(raw_center, matrix))
    add_candidate(expected_center)

    if not candidates:
        return exact_geometry

    def score(candidate_center):
        error = 0.0
        sample_start = compute_arc_point(candidate_center, radius, start_angle, matrix)
        sample_end = compute_arc_point(candidate_center, radius, end_angle, matrix)
        if expected_start is not None and sample_start is not None:
            error += xyz_distance(sample_start, expected_start) or 0.0
        if expected_end is not None and sample_end is not None:
            error += xyz_distance(sample_end, expected_end) or 0.0
        if expected_center is not None:
            error += (xyz_distance(candidate_center, expected_center) or 0.0) * 0.25
        return error

    best_center = min(candidates, key=score)
    analytic_curve["center"] = best_center
    if curve_measurement is not None and curve_measurement.get("center") is None:
        curve_measurement["center"] = best_center
    if analytic_curve.get("start_point") is None:
        analytic_curve["start_point"] = compute_arc_point(best_center, radius, start_angle, matrix)
    if analytic_curve.get("end_point") is None:
        analytic_curve["end_point"] = compute_arc_point(best_center, radius, end_angle, matrix)
    return exact_geometry


def infer_face_uv_bounds(face, edge_lookup):
    edge_indices = face.get("edge_indices", [])
    edges = [edge_lookup.get(edge_index) for edge_index in edge_indices if edge_lookup.get(edge_index) is not None]
    endpoint_points = []
    for edge in edges:
        start_point, end_point = get_edge_endpoint_points(edge)
        if start_point is not None:
            endpoint_points.append(start_point)
        if end_point is not None:
            endpoint_points.append(end_point)

    analytic = face.get("analytic_data") or {}
    face_type_name = face.get("type")
    reference_point = analytic.get("reference_point") or ((face.get("measurement") or {}).get("anchor_point"))
    if reference_point is None:
        return None

    if face_type_name == "Planar":
        tangent_u, tangent_v = basis_from_normal(analytic.get("direction"))
        projected = project_points_to_basis(endpoint_points, reference_point, tangent_u, tangent_v)
        if projected is not None:
            radius = analytic.get("radius")
            if radius is not None:
                u_span = abs((projected.get("u_max") or 0.0) - (projected.get("u_min") or 0.0))
                v_span = abs((projected.get("v_max") or 0.0) - (projected.get("v_min") or 0.0))
                if u_span < 1e-9 and v_span < 1e-9:
                    return {
                        "u_min": -radius,
                        "u_max": radius,
                        "v_min": -radius,
                        "v_max": radius,
                        "source": "inferred_from_edges_and_radius",
                    }
            projected["source"] = "inferred_from_edges"
            return projected

    if face_type_name == "Cylindrical":
        axis_point = analytic.get("reference_point")
        axis_direction = xyz_normalize(analytic.get("direction"))
        if axis_point is None or axis_direction is None:
            return None
        v_values = []
        for point in endpoint_points:
            offset = xyz_subtract(point, axis_point)
            v_value = xyz_dot(offset, axis_direction)
            if v_value is not None:
                v_values.append(v_value)
        if not v_values:
            return None
        return {
            "u_min": 0.0,
            "u_max": 2.0 * math.pi,
            "v_min": min(v_values),
            "v_max": max(v_values),
            "source": "inferred_from_edges",
        }
    return None


def infer_face_periodicity(face):
    if face.get("type") == "Cylindrical":
        return {
            "u_status": 1,
            "u_period": 2.0 * math.pi,
            "v_status": 0,
            "v_period": 0.0,
            "source": "inferred_from_surface_type",
        }
    return {
        "u_status": 0,
        "u_period": 0.0,
        "v_status": 0,
        "v_period": 0.0,
        "source": "inferred_from_surface_type",
    }


def infer_face_topology(face):
    loops = face.get("loops") or []
    if face.get("type") == "Cylindrical":
        return {"topology_code": 3, "topology_name": "Periodic", "source": "inferred_from_surface_type"}
    if loops and all(loop.get("closed") for loop in loops):
        return {"topology_code": 2, "topology_name": "Closed", "source": "inferred_from_loops"}
    return {"topology_code": 1, "topology_name": "Open", "source": "inferred_from_loops"}


def infer_face_local_properties(face):
    analytic = face.get("analytic_data") or {}
    measurement = face.get("measurement") or {}
    point = measurement.get("center") or analytic.get("reference_point")
    if point is None:
        return None

    if face.get("type") == "Planar":
        unit_normal = xyz_normalize(analytic.get("direction"))
        tangent_u, tangent_v = basis_from_normal(unit_normal)
        return {
            "uv": {"u": 0.0, "v": 0.0},
            "point": serialize_xyz(point),
            "u_tangent": tangent_u,
            "v_tangent": tangent_v,
            "u_curvature": {"x": 0.0, "y": 0.0, "z": 0.0},
            "v_curvature": {"x": 0.0, "y": 0.0, "z": 0.0},
            "unit_normal": unit_normal,
            "principal_radii": [None, None],
            "source": "inferred_from_surface_definition",
        }

    if face.get("type") == "Cylindrical":
        axis_point = analytic.get("reference_point")
        axis_direction = xyz_normalize(analytic.get("direction"))
        if axis_point is None or axis_direction is None:
            return None
        point_xyz = serialize_xyz(point)
        offset = xyz_subtract(point_xyz, axis_point)
        axis_component = xyz_scale(axis_direction, xyz_dot(offset, axis_direction))
        closest_axis = xyz_add(axis_point, axis_component)
        unit_normal = xyz_normalize(xyz_subtract(point_xyz, closest_axis))
        if unit_normal is None:
            return None
        u_tangent = xyz_normalize(xyz_cross(axis_direction, unit_normal))
        v_tangent = axis_direction
        return {
            "uv": {"u": 0.0, "v": xyz_dot(offset, axis_direction) or 0.0},
            "point": point_xyz,
            "u_tangent": u_tangent,
            "v_tangent": v_tangent,
            "u_curvature": xyz_scale(unit_normal, -1.0 / analytic.get("radius")) if analytic.get("radius") else None,
            "v_curvature": {"x": 0.0, "y": 0.0, "z": 0.0},
            "unit_normal": unit_normal,
            "principal_radii": [analytic.get("radius"), None],
            "source": "inferred_from_surface_definition",
        }
    return None


def infer_face_trimmed_bsurface(face):
    loops = face.get("loops") or []
    edge_indices = face.get("edge_indices") or []
    edge_sense = []
    for loop in loops:
        for coedge in loop.get("coedges", []):
            edge_sense.append(1 if coedge.get("sense") == "forward" else (-1 if coedge.get("sense") == "reversed" else 0))
    return {
        "surface": {
            "type": face.get("type"),
            "num_poles_u": None,
            "num_poles_v": None,
            "order_u": None,
            "order_v": None,
            "is_rational": None,
        },
        "loop_count": len(loops),
        "edge_count": len(edge_indices),
        "edge_sense": edge_sense,
        "edge_bcurve_tags": None,
        "source": "inferred_from_loops",
    }


def build_body_vertices(edge_details):
    vertices = []
    lookup = {}

    def register_point(point):
        key = point_key(point)
        if key is None:
            return None
        if key in lookup:
            return lookup[key]
        vertex_index = len(vertices) + 1
        lookup[key] = vertex_index
        vertices.append({"index": vertex_index, "point": serialize_generic(point)})
        return vertex_index

    for edge in edge_details:
        exact_vertices = edge.get("exact_vertices") or {}
        analytic_curve = ((edge.get("exact_geometry") or {}).get("analytic_curve") or {})
        fallback_start = analytic_curve.get("start_point")
        fallback_end = analytic_curve.get("end_point")
        edge["start_vertex_index"] = register_point(
            exact_vertices.get("start_point") or edge.get("start_point") or fallback_start
        )
        edge["end_vertex_index"] = register_point(
            exact_vertices.get("end_point") or edge.get("end_point") or fallback_end
        )

    return vertices


def infer_face_loops(face, edge_lookup):
    loops = []
    remaining = []
    for edge_index in face.get("edge_indices", []):
        edge = edge_lookup.get(edge_index)
        if edge is None:
            continue
        remaining.append(
            {
                "edge_index": edge_index,
                "start": edge.get("start_vertex_index"),
                "end": edge.get("end_vertex_index"),
            }
        )

    while remaining:
        first = remaining.pop(0)
        if first["start"] is None or first["end"] is None:
            loops.append(
                {
                    "loop_index": len(loops) + 1,
                    "closed": True,
                    "coedges": [{"edge_index": first["edge_index"], "sense": "unknown"}],
                    "source": "inferred_from_face_edge_order",
                }
            )
            continue

        coedges = [{"edge_index": first["edge_index"], "sense": "forward"}]
        loop_start = first["start"]
        current_end = first["end"]
        progress = True

        while remaining and progress:
            progress = False
            for index, candidate in enumerate(remaining):
                if candidate["start"] == current_end:
                    coedges.append({"edge_index": candidate["edge_index"], "sense": "forward"})
                    current_end = candidate["end"]
                    remaining.pop(index)
                    progress = True
                    break
                if candidate["end"] == current_end:
                    coedges.append({"edge_index": candidate["edge_index"], "sense": "reversed"})
                    current_end = candidate["start"]
                    remaining.pop(index)
                    progress = True
                    break

        loops.append(
            {
                "loop_index": len(loops) + 1,
                "closed": current_end == loop_start,
                "coedges": coedges,
                "source": "inferred_from_edge_vertices",
            }
        )

    return loops


def infer_face_vertex_count(face, edge_lookup):
    vertex_indices = set()
    for edge_index in face.get("edge_indices", []):
        edge = edge_lookup.get(edge_index) or {}
        if edge.get("start_vertex_index") is not None:
            vertex_indices.add(edge["start_vertex_index"])
        if edge.get("end_vertex_index") is not None:
            vertex_indices.add(edge["end_vertex_index"])
    if vertex_indices:
        return len(vertex_indices)
    return None


def serialize_curve_measurement(curve):
    if curve is None:
        return None
    return {
        "arc_length": curve["arc_length"],
        "radius": curve["radius"],
        "diameter": curve["diameter"],
        "min_radius": curve["min_radius"],
        "start_point": serialize_generic(curve["start_point"]),
        "end_point": serialize_generic(curve["end_point"]),
        "center": serialize_generic(curve["center"]),
    }


def serialize_face_measurement(measurement):
    if measurement is None:
        return None
    return {
        "area": measurement["area"],
        "perimeter": measurement["perimeter"],
        "radius_or_diameter": measurement["radius_or_diameter"],
        "min_radius_of_curvature": measurement["min_radius_of_curvature"],
        "area_error_estimate": measurement.get("area_error_estimate"),
        "is_approximate": measurement.get("is_approximate"),
        "center": serialize_generic(measurement["center"]),
        "anchor_point": serialize_generic(measurement["anchor_point"]),
    }


def serialize_analytic_data(analytic):
    if analytic is None:
        return None
    return {
        "face_type_code": analytic.get("face_type_code"),
        "reference_point": serialize_xyz(analytic["reference_point"]),
        "direction": serialize_xyz(analytic["direction"]),
        "box": serialize_box(analytic["box"]),
        "radius": analytic["radius"],
        "radius_data": analytic["radius_data"],
        "normal_direction": analytic["normal_direction"],
        "source": analytic.get("source"),
    }


def serialize_face_local_properties(properties):
    if properties is None:
        return None
    return {
        "uv": properties["uv"],
        "point": properties["point"],
        "u_tangent": properties["u_tangent"],
        "v_tangent": properties["v_tangent"],
        "u_curvature": properties["u_curvature"],
        "v_curvature": properties["v_curvature"],
        "unit_normal": properties["unit_normal"],
        "principal_radii": properties["principal_radii"],
        "source": properties.get("source"),
    }


def get_body_bounding_box(session, uf_session, body):
    body_tag = get_nx_tag(body)
    modl, ask_bounding_box = resolve_modeling_method(uf_session, "AskBoundingBox")

    if modl is not None and ask_bounding_box is not None:
        result = call_with_diagnostics("AskBoundingBox", ask_bounding_box, (body_tag,))
        box_values = first_matching(sequenceize(result), looks_like_box6)
        if box_values is None:
            number_list = to_number_list(result)
            if number_list is not None and len(number_list) >= 6:
                box_values = number_list[:6]
        if box_values is not None:
            return list(box_values)
        box = [0.0] * 6
        try:
            ask_bounding_box(body_tag, box)
            return box
        except Exception:
            pass

    measurement = getattr(session, "Measurement", None)
    if measurement is not None and hasattr(measurement, "GetBoundingBoxProperties"):
        anchor = NXOpen.Point3d(0.0, 0.0, 0.0)
        result = call_with_diagnostics("GetBoundingBoxProperties", measurement.GetBoundingBoxProperties, ([body], 0, anchor, True))
        values = sequenceize(result)
        point_arrays = []
        for value in values:
            if isinstance(value, (list, tuple)):
                serialized_points = [serialize_point(item) for item in value]
                if serialized_points and all(item is not None for item in serialized_points):
                    point_arrays.append(serialized_points)
        if point_arrays:
            box_points = point_arrays[0]
            xs = [point["x"] for point in box_points]
            ys = [point["y"] for point in box_points]
            zs = [point["z"] for point in box_points]
            return [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)]
        try:
            box_points = []
            directions = []
            edge_length = 0.0
            origin = NXOpen.Point3d(0.0, 0.0, 0.0)
            extreme = NXOpen.Point3d(0.0, 0.0, 0.0)
            volume = 0.0
            measurement.GetBoundingBoxProperties(
                [body],
                0,
                anchor,
                True,
                box_points,
                directions,
                edge_length,
                origin,
                extreme,
                volume,
            )
            if len(box_points) >= 2:
                xs = [point.X for point in box_points]
                ys = [point.Y for point in box_points]
                zs = [point.Z for point in box_points]
                return [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)]
        except Exception:
            pass

    return None


def get_body_measurement(work_part, body):
    measure_manager = getattr(work_part, "MeasureManager", None)
    if measure_manager is None or not hasattr(measure_manager, "NewMassProperties"):
        return None

    unit_objects = []
    for unit_key in ["area", "volume", "mass", "length"]:
        unit = get_unit_object(work_part, unit_key)
        if unit is not None:
            unit_objects.append(unit)

    if not unit_objects:
        return None

    return safe_call(measure_manager.NewMassProperties, unit_objects, 0.99, [body])


def get_expression_summary(expression):
    return {
        "equation": clean_text(getattr(expression, "Equation", None)),
        "rhs": clean_text(getattr(expression, "RightHandSide", None)),
        "value": getattr(expression, "Value", None),
        "type": clean_text(getattr(expression, "Type", None)),
        "description": clean_text(getattr(expression, "Description", None), ""),
    }


def summarize_feature(feature):
    expressions = []
    for expression in safe_call(feature.GetExpressions) or []:
        expressions.append(get_expression_summary(expression))

    parents = []
    for parent in safe_call(feature.GetParents) or []:
        parents.append(clean_text(safe_call(parent.GetFeatureName) or getattr(parent, "Name", None), "Unnamed"))

    children = []
    for child in safe_call(feature.GetChildren) or []:
        children.append(clean_text(safe_call(child.GetFeatureName) or getattr(child, "Name", None), "Unnamed"))

    return {
        "name": clean_text(safe_call(feature.GetFeatureName) or getattr(feature, "Name", None), "Unnamed"),
        "type": clean_text(getattr(feature, "FeatureType", None)),
        "timestamp": getattr(feature, "Timestamp", None),
        "warning": clean_text(safe_call(feature.GetFeatureWarningMessages), ""),
        "error": clean_text(safe_call(feature.GetFeatureErrorMessages), ""),
        "parents": parents,
        "children": children,
        "expressions": expressions,
    }


def get_edge_vertices(edge):
    if not hasattr(edge, "GetVertices"):
        return None, None
    tuple_result = safe_call(edge.GetVertices)
    if tuple_result is not None:
        values = sequenceize(tuple_result)
        points = [serialize_generic(value) for value in values if serialize_generic(value) is not None and point_key(value) is not None]
        if len(points) >= 2:
            return points[0], points[1]
    p1 = NXOpen.Point3d(0.0, 0.0, 0.0)
    p2 = NXOpen.Point3d(0.0, 0.0, 0.0)
    success = safe_call(edge.GetVertices, p1, p2)
    if success is None and p1.X == 0.0 and p1.Y == 0.0 and p1.Z == 0.0 and p2.X == 0.0 and p2.Y == 0.0 and p2.Z == 0.0:
        return None, None
    return p1, p2


def get_edge_curve_measurement(session, work_part, edge, edge_type_name=None):
    measurement = getattr(session, "Measurement", None)
    if measurement is None or not hasattr(measurement, "GetArcAndCurveProperties"):
        return None

    target_unit = get_unit_object(work_part, "length")
    if target_unit is None:
        return None

    if edge_type_name == "Linear":
        result = safe_call(measurement.GetArcAndCurveProperties, [edge], target_unit, True)
    else:
        result = call_with_diagnostics("GetArcAndCurveProperties", measurement.GetArcAndCurveProperties, ([edge], target_unit, True))
    values = sequenceize(result)
    points = [serialize_generic(value) for value in values if point_key(value) is not None]
    numbers = [value for value in values if is_number(value)]
    approx = first_matching(values, lambda value: isinstance(value, bool))
    if len(numbers) >= 4 and len(points) >= 3:
        return {
            "is_approximate": approx,
            "arc_length": numbers[0],
            "radius": numbers[1] if numbers[1] else None,
            "diameter": numbers[2] if numbers[2] else None,
            "min_radius": numbers[3] if numbers[3] else None,
            "start_point": points[0],
            "end_point": points[1],
            "center": points[2],
        }

    try:
        arc_length = 0.0
        radius = 0.0
        diameter = 0.0
        min_radius = 0.0
        start_point = NXOpen.Point3d(0.0, 0.0, 0.0)
        end_point = NXOpen.Point3d(0.0, 0.0, 0.0)
        arc_center = NXOpen.Point3d(0.0, 0.0, 0.0)
        is_approximate = False
        measurement.GetArcAndCurveProperties(
            [edge],
            target_unit,
            True,
            is_approximate,
            arc_length,
            radius,
            diameter,
            min_radius,
            start_point,
            end_point,
            arc_center,
        )
        return {
            "arc_length": arc_length,
            "radius": radius if radius else None,
            "diameter": diameter if diameter else None,
            "min_radius": min_radius if min_radius else None,
            "start_point": serialize_generic(start_point),
            "end_point": serialize_generic(end_point),
            "center": serialize_generic(arc_center),
        }
    except Exception:
        return None


def get_face_measurement(session, face):
    measurement = getattr(session, "Measurement", None)
    if measurement is None or not hasattr(measurement, "GetFaceProperties"):
        return None

    alternate_face = get_measurement_alternate_face()

    result = call_with_diagnostics("GetFaceProperties", measurement.GetFaceProperties, ([face], 0.99, alternate_face, True))
    values = sequenceize(result)
    points = [serialize_generic(value) for value in values if point_key(value) is not None]
    numbers = [value for value in values if is_number(value)]
    approx = first_matching(values, lambda value: isinstance(value, bool))
    if len(numbers) >= 5 and len(points) >= 2:
        return {
            "is_approximate": approx,
            "area": numbers[0],
            "perimeter": numbers[1],
            "radius_or_diameter": numbers[2] if numbers[2] else None,
            "center": points[0],
            "min_radius_of_curvature": numbers[3] if numbers[3] else None,
            "area_error_estimate": numbers[4] if numbers[4] else None,
            "anchor_point": points[1],
        }

    try:
        area = 0.0
        perimeter = 0.0
        radius_diameter = 0.0
        cog = NXOpen.Point3d(0.0, 0.0, 0.0)
        min_radius = 0.0
        area_error = 0.0
        anchor_point = NXOpen.Point3d(0.0, 0.0, 0.0)
        is_approximate = False
        measurement.GetFaceProperties(
            [face],
            0.99,
            alternate_face,
            True,
            area,
            perimeter,
            radius_diameter,
            cog,
            min_radius,
            area_error,
            anchor_point,
            is_approximate,
        )
        return {
            "area": area,
            "perimeter": perimeter,
            "radius_or_diameter": radius_diameter if radius_diameter else None,
            "center": serialize_generic(cog),
            "min_radius_of_curvature": min_radius if min_radius else None,
            "area_error_estimate": area_error if area_error else None,
            "anchor_point": serialize_generic(anchor_point),
            "is_approximate": is_approximate,
        }
    except Exception:
        return None


def get_face_analytic_data(uf_session, face):
    modl, ask_face_data = resolve_modeling_method(uf_session, "AskFaceData")
    if modl is None or ask_face_data is None:
        add_missing_modeling_method_diagnostic("AskFaceData", uf_session, "AskFaceData_missing", "face", "data")
        return None

    face_tag = get_nx_tag(face)
    result = call_with_diagnostics("AskFaceData", ask_face_data, (face_tag,))
    parsed = parse_face_data_result(result)
    if parsed is not None:
        parsed["source"] = "AskFaceData"
        return parsed
    if result is not None:
        add_diagnostic_once("AskFaceData", "unparsed_result", sample=serialize_generic(result))

    try:
        face_type = [0]
        point = [0.0, 0.0, 0.0]
        direction = [0.0, 0.0, 0.0]
        box = [0.0] * 6
        radius = [0.0]
        radius_data = [0.0]
        normal_direction = [0]
        ask_face_data(face_tag, face_type, point, direction, box, radius, radius_data, normal_direction)
        return {
            "face_type_code": face_type[0] if isinstance(face_type, list) else face_type,
            "reference_point": point,
            "direction": direction,
            "box": box,
            "radius": radius[0] if isinstance(radius, list) else radius,
            "radius_data": radius_data[0] if isinstance(radius_data, list) else radius_data,
            "normal_direction": normal_direction[0] if isinstance(normal_direction, list) else normal_direction,
            "source": "AskFaceData",
        }
    except Exception:
        return None


def summarize_edges(session, work_part, uf_session, body):
    edges = list(safe_call(body.GetEdges) or [])
    counts = {}
    details = []
    face_lookup = {}
    for face_index, face in enumerate(list(safe_call(body.GetFaces) or []), start=1):
        face_lookup[get_nx_tag(face)] = face_index

    for edge_index, edge in enumerate(edges, start=1):
        edge_type_name = enum_name(getattr(edge, "SolidEdgeType", None), EDGE_TYPE_NAMES)
        counts[edge_type_name] = counts.get(edge_type_name, 0) + 1

        connected_faces = list(safe_call(edge.GetFaces) or [])
        length = safe_call(edge.GetLength)
        start_point, end_point = get_edge_vertices(edge)
        edge_tag = get_nx_tag(edge)
        exact_vertices = get_edge_vertices_uf(uf_session, edge_tag)
        if exact_vertices is None:
            exact_vertices = infer_edge_exact_vertices(start_point, end_point)
        exact_geometry = get_curve_exact_geometry(uf_session, edge_tag, edge_type_name)
        curve_measurement = enrich_curve_measurement(get_edge_curve_measurement(session, work_part, edge, edge_type_name), exact_geometry)
        if curve_measurement is None and edge_type_name == "Linear":
            curve_measurement = infer_linear_curve_measurement(length, start_point, end_point)
        exact_geometry = reconcile_arc_exact_geometry(exact_geometry, curve_measurement, start_point, end_point)
        preview_points = build_edge_preview_points(edge_type_name, exact_geometry, curve_measurement, start_point, end_point)
        if (preview_points is None or len(preview_points) < 3) and edge_type_name in ("Spline", "SpCurve", "TrimmedCurve", "Intersection"):
            sampled_points = get_curve_sample_points_uf(uf_session, edge_tag)
            if sampled_points:
                preview_points = sampled_points
        connected_face_tags = [get_nx_tag(face) for face in connected_faces]

        details.append(
            {
                "index": edge_index,
                "type": edge_type_name,
                "length": length,
                "face_count": len(connected_faces),
                "tag": edge_tag,
                "start_point": start_point,
                "end_point": end_point,
                "curve_measurement": curve_measurement,
                "is_reference": getattr(edge, "IsReference", None),
                "attributes": get_user_attributes(edge),
                "object_identity": serialize_object_identity(edge),
                "display_properties": serialize_display_properties(edge),
                "component_context": serialize_component_context(edge),
                "connected_face_tags": connected_face_tags,
                "connected_face_indices": [face_lookup.get(tag, tag) for tag in connected_face_tags],
                "exact_vertices": exact_vertices,
                "exact_geometry": exact_geometry,
                "preview_points": preview_points,
            }
        )

    return edges, counts, details


def summarize_faces(session, uf_session, body):
    faces = list(safe_call(body.GetFaces) or [])
    counts = {}
    details = []
    face_lookup = {}

    for face_index, face in enumerate(faces, start=1):
        face_lookup[get_nx_tag(face)] = face_index

    for face_index, face in enumerate(faces, start=1):
        face_type_name = enum_name(getattr(face, "SolidFaceType", None), FACE_TYPE_NAMES)
        counts[face_type_name] = counts.get(face_type_name, 0) + 1

        edges = list(safe_call(face.GetEdges) or [])
        adjacent_faces = []
        for edge in edges:
            for adjacent_face in safe_call(edge.GetFaces) or []:
                adjacent_tag = get_nx_tag(adjacent_face)
                if adjacent_tag != get_nx_tag(face) and adjacent_tag not in adjacent_faces:
                    adjacent_faces.append(adjacent_tag)

        face_measurement = get_face_measurement(session, face)
        face_tag = get_nx_tag(face)
        facet_count = safe_call(face.GetNumberOfFacets)
        analytic_data = get_face_analytic_data(uf_session, face)
        uv_bounds = get_face_uv_bounds(uf_session, face_tag)
        periodicity = get_face_periodicity(uf_session, face_tag)
        topology = get_face_topology(uf_session, face_tag)
        local_properties = get_face_local_properties(uf_session, face_tag, uv_bounds)
        trimmed_bsurface = get_face_trimmed_bsurface(uf_session, face_tag, len(edges))
        facet_mesh = None
        if facet_count and facet_count > 0:
            facet_mesh = get_face_facet_mesh(uf_session, face_tag, facet_count)
        blend_radius = None
        is_blend = None
        if hasattr(face, "GetBlendData"):
            blend_radius = 0.0
            is_blend = False
            safe_call(face.GetBlendData, blend_radius, is_blend)

        details.append(
            {
                "index": face_index,
                "type": face_type_name,
                "edge_count": len(edges),
                "facet_count": facet_count,
                "vertex_count": safe_call(face.GetNumberOfVertices),
                "tag": face_tag,
                "attributes": get_user_attributes(face),
                "object_identity": serialize_object_identity(face),
                "display_properties": serialize_display_properties(face),
                "component_context": serialize_component_context(face),
                "adjacent_faces": adjacent_faces,
                "measurement": face_measurement,
                "analytic_data": analytic_data,
                "surface_definition": build_surface_definition(face_type_name, analytic_data, local_properties, trimmed_bsurface),
                "uv_bounds": uv_bounds,
                "periodicity": periodicity,
                "topology": topology,
                "local_properties": local_properties,
                "trimmed_bsurface": trimmed_bsurface,
                "facet_mesh": facet_mesh,
                "edge_tags": [get_nx_tag(edge) for edge in edges],
                "blend_radius": blend_radius,
                "is_blend_face": is_blend,
            }
        )

    for detail in details:
        detail["adjacent_face_indices"] = [face_lookup.get(tag, tag) for tag in detail["adjacent_faces"]]

    return faces, counts, details


def classify_body(body, face_counts, edge_counts, body_measurement):
    if getattr(body, "IsSheetBody", False):
        return "Likely a sheet/surface body."

    planar_faces = face_counts.get("Planar", 0)
    cylindrical_faces = face_counts.get("Cylindrical", 0)
    conical_faces = face_counts.get("Conical", 0)
    spherical_faces = face_counts.get("Spherical", 0)
    linear_edges = edge_counts.get("Linear", 0)
    circular_edges = edge_counts.get("Circular", 0)

    if planar_faces == 6 and linear_edges == 12:
        return "Likely a rectangular prism / block."
    if planar_faces == 2 and cylindrical_faces == 1 and circular_edges == 2:
        return "Likely a right circular cylinder."
    if spherical_faces == 1 and len(face_counts) == 1:
        return "Likely a sphere or spherical body."
    if conical_faces >= 1 and circular_edges >= 1:
        return "Likely contains a conical region."
    if cylindrical_faces >= 1 and planar_faces >= 1:
        return "Likely contains extruded or revolved cylindrical geometry."
    if body_measurement is not None and getattr(body_measurement, "Volume", 0):
        return "Solid body with measurable enclosed volume."
    return "General BREP body; no stronger shape classification detected."


def get_inter_body_distances(session, bodies):
    measurement = getattr(session, "Measurement", None)
    if measurement is None or not hasattr(measurement, "GetMinimumDistance"):
        return []

    distances = []
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            p1 = NXOpen.Point3d(0.0, 0.0, 0.0)
            p2 = NXOpen.Point3d(0.0, 0.0, 0.0)
            distance = safe_call(measurement.GetMinimumDistance, bodies[i], bodies[j], p1, p2, 0.99)
            distances.append(
                {
                    "body_1": i + 1,
                    "body_2": j + 1,
                    "distance": distance,
                    "point_1": p1,
                    "point_2": p2,
                }
            )
    return distances


def analyze_part_to_md(output_path="report.md"):
    session = NXOpen.Session.GetSession()
    uf_session = NXOpen.UF.UFSession.GetUFSession()
    work_part = session.Parts.Work
    output_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(output_dir, output_path)
    json_output_path = os.path.splitext(output_path)[0] + ".json"

    md_lines = []
    part_attributes = get_user_attributes(work_part)
    report_data = {
        "export_metadata": {
            "script_version": SCRIPT_VERSION,
        },
        "part_summary": {
            "part_name": clean_text(getattr(work_part, "Name", None), "Unknown"),
            "full_path": clean_text(getattr(work_part, "FullPath", None)),
            "leaf": clean_text(getattr(work_part, "Leaf", None)),
            "units": enum_name(getattr(work_part, "PartUnits", None), PART_UNITS),
            "tag": get_nx_tag(work_part),
            "read_only": getattr(work_part, "IsReadOnly", None),
            "fully_loaded": getattr(work_part, "IsFullyLoaded", None),
            "has_write_access": getattr(work_part, "HasWriteAccess", None),
            "unique_identifier": clean_text(getattr(work_part, "UniqueIdentifier", None)),
            "object_identity": serialize_object_identity(work_part),
            "display_properties": serialize_display_properties(work_part),
            "component_context": serialize_component_context(work_part),
        },
        "part_attributes": serialize_attributes(part_attributes),
        "body_count": 0,
        "inter_body_distances": [],
        "bodies": [],
    }
    md_lines.append("# Parasolid Analysis Report\n")
    md_lines.append("## Part Summary")
    md_lines.append(f"- Part Name: {clean_text(getattr(work_part, 'Name', None), 'Unknown')}")
    md_lines.append(f"- Full Path: {clean_text(getattr(work_part, 'FullPath', None))}")
    md_lines.append(f"- Leaf: {clean_text(getattr(work_part, 'Leaf', None))}")
    md_lines.append(f"- Units: {enum_name(getattr(work_part, 'PartUnits', None), PART_UNITS)}")
    md_lines.append(f"- Tag: {get_nx_tag(work_part)}")
    md_lines.append(f"- Read Only: {format_bool(getattr(work_part, 'IsReadOnly', None))}")
    md_lines.append(f"- Fully Loaded: {format_bool(getattr(work_part, 'IsFullyLoaded', None))}")
    md_lines.append(f"- Has Write Access: {format_bool(getattr(work_part, 'HasWriteAccess', None))}")
    md_lines.append(f"- Unique Identifier: {clean_text(getattr(work_part, 'UniqueIdentifier', None))}")

    if part_attributes:
        md_lines.append("\n### Part Attributes")
        append_attribute_list(md_lines, part_attributes)

    bodies = [body for body in work_part.Bodies]
    report_data["body_count"] = len(bodies)
    md_lines.append(f"\n**Number of Bodies:** {len(bodies)}\n")

    body_distance_rows = get_inter_body_distances(session, bodies)
    if body_distance_rows:
        md_lines.append("## Inter-Body Distances")
        for row in body_distance_rows:
            md_lines.append(
                f"- Body {row['body_1']} to Body {row['body_2']}: {format_float(row['distance'])} | "
                f"Closest Points: {format_point(row['point_1'])} to {format_point(row['point_2'])}"
            )
    report_data["inter_body_distances"] = [
        {
            "body_1": row["body_1"],
            "body_2": row["body_2"],
            "distance": row["distance"],
            "point_1": serialize_point(row["point_1"]),
            "point_2": serialize_point(row["point_2"]),
        }
        for row in body_distance_rows
    ]

    for i, body in enumerate(bodies, start=1):
        body_measurement = get_body_measurement(work_part, body)
        features = [summarize_feature(feature) for feature in (safe_call(body.GetFeatures) or [])]
        edges, edge_counts, edge_details = summarize_edges(session, work_part, uf_session, body)
        faces, face_counts, face_details = summarize_faces(session, uf_session, body)
        body_vertices = build_body_vertices(edge_details)
        edge_index_by_tag = {edge["tag"]: edge["index"] for edge in edge_details}
        edge_lookup = {edge["index"]: edge for edge in edge_details}
        for face in face_details:
            face["edge_indices"] = [edge_index_by_tag.get(tag, tag) for tag in face.get("edge_tags", [])]
            face["loops"] = infer_face_loops(face, edge_lookup)
            if face.get("vertex_count") is None:
                face["vertex_count"] = infer_face_vertex_count(face, edge_lookup)
            if face.get("analytic_data") is None:
                face["analytic_data"] = infer_face_analytic_data(face, edge_lookup)
            if face.get("uv_bounds") is None:
                face["uv_bounds"] = infer_face_uv_bounds(face, edge_lookup)
            if face.get("periodicity") is None:
                face["periodicity"] = infer_face_periodicity(face)
            if face.get("topology") is None:
                face["topology"] = infer_face_topology(face)
            if face.get("local_properties") is None:
                face["local_properties"] = infer_face_local_properties(face)
            if face.get("trimmed_bsurface") is None:
                face["trimmed_bsurface"] = infer_face_trimmed_bsurface(face)
            if face.get("surface_definition") is None:
                face["surface_definition"] = build_surface_definition(face.get("type"), face.get("analytic_data"), face.get("local_properties"), face.get("trimmed_bsurface"))
        body_box = get_body_bounding_box(session, uf_session, body)
        body_attributes = get_user_attributes(body)
        body_entry = {
            "index": i,
            "metadata": {
                "tag": get_nx_tag(body),
                "journal_identifier": clean_text(getattr(body, "JournalIdentifier", None)),
                "name": clean_text(getattr(body, "Name", None)),
                "is_solid_body": getattr(body, "IsSolidBody", None),
                "is_sheet_body": getattr(body, "IsSheetBody", None),
                "is_convergent_body": getattr(body, "IsConvergentBody", None),
                "density": getattr(body, "Density", None),
                "facet_count": safe_call(body.GetNumberOfFacets),
                "vertex_count": safe_call(body.GetNumberOfVertices) or len(body_vertices),
                "shape_summary": classify_body(body, face_counts, edge_counts, body_measurement),
                "object_identity": serialize_object_identity(body),
                "display_properties": serialize_display_properties(body),
                "component_context": serialize_component_context(body),
            },
            "measurement": None,
            "bounding_box": serialize_box(body_box),
            "attributes": serialize_attributes(body_attributes),
            "topology": {
                "vertex_count": len(body_vertices),
                "vertices": body_vertices,
            },
            "features": features,
            "edge_type_breakdown": edge_counts,
            "edges": [],
            "face_type_breakdown": face_counts,
            "faces": [],
        }

        md_lines.append(f"\n---\n## Body {i}\n")
        md_lines.append("### Body Metadata")
        md_lines.append(f"- Tag: {get_nx_tag(body)}")
        md_lines.append(f"- Journal Identifier: {clean_text(getattr(body, 'JournalIdentifier', None))}")
        md_lines.append(f"- Name: {clean_text(getattr(body, 'Name', None))}")
        md_lines.append(f"- Is Solid Body: {format_bool(getattr(body, 'IsSolidBody', None))}")
        md_lines.append(f"- Is Sheet Body: {format_bool(getattr(body, 'IsSheetBody', None))}")
        md_lines.append(f"- Is Convergent Body: {format_bool(getattr(body, 'IsConvergentBody', None))}")
        md_lines.append(f"- Density: {format_float(getattr(body, 'Density', None))}")
        md_lines.append(f"- Facet Count: {safe_call(body.GetNumberOfFacets)}")
        md_lines.append(f"- Vertex Count: {safe_call(body.GetNumberOfVertices)}")
        md_lines.append(f"- Shape Summary: {body_entry['metadata']['shape_summary']}")
        md_lines.append(f"- Exported Topology Vertices: {len(body_vertices)}")

        if body_measurement is not None:
            body_entry["measurement"] = {
                "surface_area": getattr(body_measurement, "Area", None),
                "volume": getattr(body_measurement, "Volume", None),
                "mass": getattr(body_measurement, "Mass", None),
                "weight": getattr(body_measurement, "Weight", None),
                "radius_of_gyration": getattr(body_measurement, "RadiusOfGyration", None),
                "centroid": serialize_point(getattr(body_measurement, "Centroid", None)),
            }
            md_lines.append("### Body Measurement")
            md_lines.append(f"- Surface Area: {format_float(getattr(body_measurement, 'Area', None))}")
            md_lines.append(f"- Volume: {format_float(getattr(body_measurement, 'Volume', None))}")
            md_lines.append(f"- Mass: {format_float(getattr(body_measurement, 'Mass', None))}")
            md_lines.append(f"- Weight: {format_float(getattr(body_measurement, 'Weight', None))}")
            md_lines.append(f"- Radius Of Gyration: {format_float(getattr(body_measurement, 'RadiusOfGyration', None))}")
            md_lines.append(f"- Centroid: {format_point(getattr(body_measurement, 'Centroid', None))}")

        md_lines.append("### Bounding Box")
        if body_box is None:
            md_lines.append("- Bounding box not available in this NX runtime.")
        else:
            x_min, y_min, z_min, x_max, y_max, z_max = body_box
            md_lines.append(f"- X Min: {format_float(x_min)}")
            md_lines.append(f"- Y Min: {format_float(y_min)}")
            md_lines.append(f"- Z Min: {format_float(z_min)}")
            md_lines.append(f"- X Max: {format_float(x_max)}")
            md_lines.append(f"- Y Max: {format_float(y_max)}")
            md_lines.append(f"- Z Max: {format_float(z_max)}")
            md_lines.append(f"- Dx (Length): {format_float(x_max - x_min)}")
            md_lines.append(f"- Dy (Width): {format_float(y_max - y_min)}")
            md_lines.append(f"- Dz (Height): {format_float(z_max - z_min)}")

        if body_attributes:
            md_lines.append("\n### Body Attributes")
            append_attribute_list(md_lines, body_attributes)

        md_lines.append("\n### Features")
        md_lines.append(f"- Total Features: {len(features)}")
        for feature in features:
            md_lines.append(
                f"- {feature['name']} | Type: {feature['type']} | Timestamp: {feature['timestamp']}"
            )
            if feature["parents"]:
                md_lines.append(f"-   Parents: {', '.join(feature['parents'])}")
            if feature["children"]:
                md_lines.append(f"-   Children: {', '.join(feature['children'])}")
            if feature["warning"]:
                md_lines.append(f"-   Warning: {feature['warning']}")
            if feature["error"]:
                md_lines.append(f"-   Error: {feature['error']}")
            for expression in feature["expressions"]:
                md_lines.append(
                    f"-   Expression: {expression['equation']} | RHS: {expression['rhs']} | "
                    f"Value: {format_float(expression['value'])} | Type: {expression['type']}"
                )

        md_lines.append("\n### Edges")
        md_lines.append(f"- Total Edges: {len(edges)}")
        md_lines.append("**Edge Type Breakdown:**")
        for edge_type_name in sorted(edge_counts):
            md_lines.append(f"- {edge_type_name}: {edge_counts[edge_type_name]}")

        md_lines.append("\n**Per-Edge Details:**")
        for edge in edge_details:
            body_entry["edges"].append(
                {
                    "index": edge["index"],
                    "type": edge["type"],
                    "length": edge["length"],
                    "face_count": edge["face_count"],
                    "tag": edge["tag"],
                    "start_point": serialize_point(edge["start_point"]),
                    "end_point": serialize_point(edge["end_point"]),
                    "curve_measurement": serialize_curve_measurement(edge["curve_measurement"]),
                    "is_reference": edge["is_reference"],
                    "attributes": serialize_attributes(edge["attributes"]),
                    "object_identity": edge.get("object_identity"),
                    "display_properties": edge.get("display_properties"),
                    "component_context": edge.get("component_context"),
                    "connected_face_tags": edge["connected_face_tags"],
                    "connected_face_indices": edge["connected_face_indices"],
                    "exact_vertices": edge["exact_vertices"],
                    "exact_geometry": edge["exact_geometry"],
                    "preview_points": [serialize_point(point) for point in (edge.get("preview_points") or [])],
                    "start_vertex_index": edge.get("start_vertex_index"),
                    "end_vertex_index": edge.get("end_vertex_index"),
                }
            )
            line = (
                f"- Edge {edge['index']}: {edge['type']} | Length: {format_float(edge['length'])} | "
                f"Connected Faces: {edge['face_count']} | Tag: {edge['tag']}"
            )
            md_lines.append(line)
            if edge["start_point"] is not None or edge["end_point"] is not None:
                md_lines.append(
                    f"-   Endpoints: {format_point(edge['start_point'])} -> {format_point(edge['end_point'])}"
                )
            if edge["curve_measurement"] is not None:
                curve = edge["curve_measurement"]
                md_lines.append(
                    f"-   Curve Props: Arc Length {format_float(curve['arc_length'])} | "
                    f"Radius {format_float(curve['radius'])} | Diameter {format_float(curve['diameter'])} | "
                    f"Min Curvature Radius {format_float(curve['min_radius'])}"
                )
                md_lines.append(
                    f"-   Curve Points: Start {format_point(curve['start_point'])} | "
                    f"End {format_point(curve['end_point'])} | Center {format_point(curve['center'])}"
                )
            if edge["connected_face_tags"]:
                md_lines.append(f"-   Face Tags: {', '.join(str(tag) for tag in edge['connected_face_tags'])}")
            if edge.get("connected_face_indices"):
                md_lines.append(f"-   Face Indices: {', '.join(str(index) for index in edge['connected_face_indices'])}")
            if edge.get("start_vertex_index") is not None or edge.get("end_vertex_index") is not None:
                md_lines.append(
                    f"-   Topology Vertices: {edge.get('start_vertex_index')} -> {edge.get('end_vertex_index')}"
                )
            if edge.get("exact_geometry") is not None:
                curve_struct = edge["exact_geometry"].get("curve_structure")
                analytic_curve = edge["exact_geometry"].get("analytic_curve")
                if curve_struct is not None:
                    md_lines.append(
                        f"-   Exact Curve: {curve_struct.get('curve_type_name')} | "
                        f"t0 {format_float(curve_struct.get('parameter_origin'))} | "
                        f"scale {format_float(curve_struct.get('parameter_scale'))}"
                    )
                if analytic_curve is not None:
                    md_lines.append(f"-   Analytic Curve Type: {analytic_curve.get('type')}")
            if edge.get("object_identity"):
                md_lines.append(f"-   Identity: {serialize_generic(edge['object_identity'])}")
            if edge.get("display_properties"):
                md_lines.append(f"-   Display: {serialize_generic(edge['display_properties'])}")
            if edge.get("component_context"):
                md_lines.append(f"-   Component: {serialize_generic(edge['component_context'])}")
            if edge["attributes"]:
                md_lines.append(f"-   Edge {edge['index']} Attributes")
                append_attribute_list(md_lines, edge["attributes"], prefix="-     ")

        md_lines.append("\n### Faces")
        md_lines.append(f"- Total Faces: {len(faces)}")
        md_lines.append("**Face Type Breakdown:**")
        for face_type_name in sorted(face_counts):
            md_lines.append(f"- {face_type_name}: {face_counts[face_type_name]}")

        md_lines.append("\n**Per-Face Details:**")
        for face in face_details:
            body_entry["faces"].append(
                {
                    "index": face["index"],
                    "type": face["type"],
                    "edge_count": face["edge_count"],
                    "facet_count": face["facet_count"],
                    "vertex_count": face["vertex_count"],
                    "tag": face["tag"],
                    "measurement": serialize_face_measurement(face["measurement"]),
                    "analytic_data": serialize_analytic_data(face["analytic_data"]),
                    "surface_definition": face["surface_definition"],
                    "uv_bounds": face["uv_bounds"],
                    "periodicity": face["periodicity"],
                    "topology": face["topology"],
                    "local_properties": serialize_face_local_properties(face["local_properties"]),
                    "trimmed_bsurface": face["trimmed_bsurface"],
                    "facet_mesh": face.get("facet_mesh"),
                    "edge_indices": face["edge_indices"],
                    "loops": face["loops"],
                    "adjacent_face_indices": face["adjacent_face_indices"],
                    "is_blend_face": face["is_blend_face"],
                    "blend_radius": face["blend_radius"],
                    "attributes": serialize_attributes(face["attributes"]),
                    "object_identity": face.get("object_identity"),
                    "display_properties": face.get("display_properties"),
                    "component_context": face.get("component_context"),
                }
            )
            md_lines.append(
                f"- Face {face['index']}: {face['type']} | Edges: {face['edge_count']} | "
                f"Facets: {face['facet_count']} | Vertices: {face['vertex_count']} | Tag: {face['tag']}"
            )
            if face["measurement"] is not None:
                md_lines.append(
                    f"-   Measure: Area {format_float(face['measurement']['area'])} | "
                    f"Perimeter {format_float(face['measurement']['perimeter'])} | "
                    f"Radius/Diameter {format_float(face['measurement']['radius_or_diameter'])} | "
                    f"Min Curvature Radius {format_float(face['measurement']['min_radius_of_curvature'])}"
                )
                md_lines.append(
                    f"-   Points: Center {format_point(face['measurement']['center'])} | "
                    f"Anchor {format_point(face['measurement']['anchor_point'])}"
                )
            if face["analytic_data"] is not None:
                analytic = face["analytic_data"]
                reference_point = analytic.get("reference_point")
                direction = analytic.get("direction")
                md_lines.append(
                    f"-   Analytic: Ref Point {format_point(reference_point)} | "
                    f"Direction {format_point(direction)} | "
                    f"Radius {format_float(analytic.get('radius'))}{source_suffix(analytic.get('source'))}"
                )
            if face.get("surface_definition") is not None:
                md_lines.append(
                    f"-   Surface Definition Source: {format_source_label(face['surface_definition'].get('source'))}"
                )
            if face.get("uv_bounds") is not None:
                uv_bounds = face["uv_bounds"]
                md_lines.append(
                    f"-   UV Bounds: U[{format_float(uv_bounds.get('u_min'))}, {format_float(uv_bounds.get('u_max'))}] | "
                    f"V[{format_float(uv_bounds.get('v_min'))}, {format_float(uv_bounds.get('v_max'))}]{source_suffix(uv_bounds.get('source'))}"
                )
            if face.get("periodicity") is not None:
                periodicity = face["periodicity"]
                md_lines.append(
                    f"-   Periodicity: U status {periodicity.get('u_status')} period {format_float(periodicity.get('u_period'))} | "
                    f"V status {periodicity.get('v_status')} period {format_float(periodicity.get('v_period'))}{source_suffix(periodicity.get('source'))}"
                )
            if face.get("topology") is not None:
                md_lines.append(
                    f"-   Face Topology: {face['topology'].get('topology_name')}{source_suffix(face['topology'].get('source'))}"
                )
            if face.get("local_properties") is not None:
                md_lines.append(
                    f"-   Local Properties Source: {format_source_label(face['local_properties'].get('source'))}"
                )
            if face.get("trimmed_bsurface") is not None:
                md_lines.append(
                    f"-   Trimmed Surface Source: {format_source_label(face['trimmed_bsurface'].get('source'))}"
                )
            if face.get("facet_mesh") is not None:
                md_lines.append(
                    f"-   Facet Mesh: {face['facet_mesh'].get('triangle_count')} triangle(s) [{format_source_label(face['facet_mesh'].get('source'))}]"
                )
            if face.get("edge_indices"):
                md_lines.append(f"-   Face Edge Indices: {', '.join(str(index) for index in face['edge_indices'])}")
            if face.get("loops"):
                md_lines.append(f"-   Loop Count: {len(face['loops'])}")
            if face["adjacent_face_indices"]:
                md_lines.append(
                    f"-   Adjacent Faces: {', '.join(str(index) for index in face['adjacent_face_indices'])}"
                )
            if face["is_blend_face"] is not None:
                md_lines.append(
                    f"-   Blend Data: Is Blend {format_bool(face['is_blend_face'])} | Radius {format_float(face['blend_radius'])}"
                )
            if face.get("object_identity"):
                md_lines.append(f"-   Identity: {serialize_generic(face['object_identity'])}")
            if face.get("display_properties"):
                md_lines.append(f"-   Display: {serialize_generic(face['display_properties'])}")
            if face.get("component_context"):
                md_lines.append(f"-   Component: {serialize_generic(face['component_context'])}")
            if face["attributes"]:
                md_lines.append(f"-   Face {face['index']} Attributes")
                append_attribute_list(md_lines, face["attributes"], prefix="-     ")

        report_data["bodies"].append(body_entry)

    append_runtime_diagnostics_summary(md_lines, RUNTIME_DIAGNOSTICS)

    with open(output_path, "w") as f:
        f.write("\n".join(md_lines))
    report_data["export_metadata"]["runtime_diagnostics"] = RUNTIME_DIAGNOSTICS
    with open(json_output_path, "w") as f:
        json.dump(report_data, f, indent=2)

    print(f"Markdown report saved to: {output_path}")
    print(f"JSON report saved to: {json_output_path}")


DEFAULT_REPORT_NAME = "parasolid_report.md"


def main():
    analyze_part_to_md(DEFAULT_REPORT_NAME)


if __name__ == "__main__":
    main()
