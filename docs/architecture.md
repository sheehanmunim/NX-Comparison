# Architecture

## Current split

- `nx_brep_export.py`
  Export layer that runs inside NX as a Python NX Open journal using `NXOpen` and `NXOpen.UF`.
- `xt_part_report.py`
  Normalization and import layer for the NX journal export.
- `xt_part_gui.py`
  Viewer, inspection, comparison, and workflow UI.
- `xt_kernel.py`
  Lightweight preview/reconstruction utilities, not a true CAD kernel.

## Why this split

The difficult part of the project is accurate extraction from NX and exact topology/geometry handoff. In this repo, that work now lives in the NX Python journal so it stays runnable in the available NX environment.

The fast-moving part of the project is:

- loading reports
- shaping data
- comparing parts
- rendering previews
- UI/workflow iteration

That belongs in Python.

## Implementation rule

The NX Python exporter is the source of truth for:

- body topology
- edge records and connected-face relationships
- face records
- loops/coedges
- surface definitions
- face UV bounds, periodicity, topology, and local property samples when NX exposes them

The rest of the Python side should consume those records directly and only use inference as a fallback.

## Current reality

The runnable exporter in this repo is the NX Python journal:

- `nx_brep_export.py`

That keeps the export path aligned with the environment that can actually execute inside NX.
