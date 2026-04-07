#!/usr/bin/env python3

import json
import os
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from xt_backend_setup import ensure_backend_runtimes
from xt_part_report import (
    _analyze_input_file_single,
    _fused_report_from_step_and_json,
    analyze_input_text,
    metric_and_imperial,
)


HOST = "127.0.0.1"
PORT = 8765


def _is_workspace_sample_path(path: Path) -> bool:
    ignored_parts = {"__pycache__", ".git", ".hg", ".svn", "node_modules", "schemas"}
    for part in path.parts:
        if part in ignored_parts:
            return False
        if part.startswith(".venv"):
            return False
    return path.is_file()


def workspace_sample_reports() -> list[dict]:
    reports: list[dict] = []
    seen: set[Path] = set()
    for pattern in ("*.x_t", "*.stp", "*.step", "*.json"):
        for path in sorted(candidate for candidate in Path.cwd().rglob(pattern) if _is_workspace_sample_path(candidate)):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            reports.extend(enrich_report(report) for report in _analyze_input_file_single(resolved))
    return reports


def analyze_paths(paths: list[str]) -> list[dict]:
    reports: list[dict] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if path in seen:
            continue
        seen.add(path)
        reports.extend(enrich_report(report) for report in _analyze_input_file_single(path))
    return reports


def analyze_uploaded_files(file_payloads: list[dict]) -> list[dict]:
    reports: list[dict] = []
    for file_payload in file_payloads:
        imported_reports = analyze_input_text(
            file_payload.get("text", ""),
            display_name=file_payload.get("name", "<upload>"),
            source_path=f"uploaded:{file_payload.get('name', '<upload>')}",
            file_size_bytes=file_payload.get("size"),
        )
        reports.extend(enrich_report(report) for report in imported_reports)
    return reports


def create_fused_report(left_report: dict, right_report: dict) -> dict | None:
    fused_report = _fused_report_from_step_and_json(left_report, right_report)
    if fused_report is None:
        return None

    formats = {
        left_report.get("transmit_info", {}).get("source_format"): left_report,
        right_report.get("transmit_info", {}).get("source_format"): right_report,
    }
    step_report = formats.get("step_file")
    json_report = formats.get("siemens_nx_json")
    if not step_report or not json_report:
        return None

    base_name = (
        (json_report.get("decoded_names") or [None])[0]
        or (step_report.get("decoded_names") or [None])[0]
        or Path(str(json_report.get("file_name") or step_report.get("file_name") or "Fusion")).stem
    )
    fused_report["file_name"] = f"{base_name} STEP+JSON"
    fused_report["file"] = (
        f"fusion:{json_report.get('file_name', 'json')}+{step_report.get('file_name', 'step')}"
    )
    fused_report["fusion"] = {
        **dict(fused_report.get("fusion") or {}),
        "display_name": "STEP+JSON",
    }
    model_summary = list((fused_report.get("model_analysis") or {}).get("summary") or [])
    if model_summary:
        model_summary[0] = f"Explicit STEP + JSON fusion for {base_name}."
        fused_report["model_analysis"] = {
            **dict(fused_report.get("model_analysis") or {}),
            "summary": model_summary,
        }
    return enrich_report(fused_report)


def preview_score(report: dict) -> tuple[int, int]:
    geometry = report.get("geometry_hints", {})
    topology = report.get("model_analysis", {}).get("topology")
    return (
        int(geometry.get("point_count", 0)),
        1 if topology else 0,
    )


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CAD Part Viewer & Compare</title>
  <style>
    * { box-sizing: border-box; }
    html, body { margin: 0; height: 100%; }
    :root {
      --app-bg: #d9d9d9;
      --chrome-bg: #efefef;
      --panel-bg: #f7f7f7;
      --panel-border: #b8b8b8;
      --panel-shadow: 0 14px 32px rgba(0, 0, 0, 0.08);
      --text-main: #1f1f1f;
      --text-muted: #5f5f5f;
      --accent: #ff9f1c;
      --accent-strong: #ff7a00;
      --viewport-top: #252525;
      --viewport-bottom: #1a1a1a;
      --viewport-grid: rgba(255, 255, 255, 0.06);
      --viewport-grid-strong: rgba(255, 255, 255, 0.12);
      --viewport-frame: #5a5a5a;
    }
    body {
      font-family: "Segoe UI", "Aptos", "Helvetica Neue", Arial, sans-serif;
      background: var(--app-bg);
      color: var(--text-main);
    }
    .app {
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      min-height: 100vh;
    }
    .header, .toolbar, .sidebar, .main, .status {
      background: rgba(247, 247, 247, 0.96);
      border: 1px solid var(--panel-border);
      box-shadow: var(--panel-shadow);
    }
    .header, .toolbar, .status {
      margin: 10px 10px 0 10px;
      padding: 12px;
    }
    .header h1 {
      margin: 0 0 6px 0;
      font-size: 24px;
      font-weight: 700;
    }
    .header p, .status {
      margin: 0;
      color: var(--text-muted);
      font-size: 14px;
      line-height: 1.4;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      background: rgba(239, 239, 239, 0.96);
    }
    .toolbar input[type="text"] {
      flex: 1 1 320px;
      min-width: 220px;
      padding: 10px 12px;
      border: 1px solid #b8b8b8;
      border-radius: 0;
      background: #fff;
      color: var(--text-main);
    }
    button, .file-upload {
      padding: 8px 12px;
      border: 1px solid #ababab;
      border-radius: 0;
      background: #e6e6e6;
      color: var(--text-main);
      cursor: pointer;
      font: inherit;
      transition: background 120ms ease, transform 120ms ease, border-color 120ms ease;
    }
    button:hover, .file-upload:hover {
      background: #dcdcdc;
      border-color: #8d8d8d;
      transform: translateY(-1px);
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
      transform: none;
      background: #ececec;
      border-color: #c5c5c5;
    }
    .file-upload input { display: none; }
    .content {
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr);
      gap: 10px;
      padding: 10px;
      min-height: 0;
    }
    .sidebar {
      padding: 10px;
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      gap: 10px;
      min-height: 0;
      background: rgba(242, 242, 242, 0.97);
    }
    .sidebar h2, .panel h2 {
      margin: 0;
      font-size: 16px;
    }
    .hint {
      color: var(--text-muted);
      font-size: 13px;
      line-height: 1.4;
    }
    .file-list {
      overflow: auto;
      min-height: 0;
      border: 1px solid #d0d0d0;
      border-radius: 0;
      background: #f2f2f2;
    }
    .file-item {
      padding: 10px;
      border-bottom: 1px solid #d7d7d7;
      cursor: pointer;
      background: transparent;
      transition: background 120ms ease, border-color 120ms ease;
    }
    .file-item:last-child { border-bottom: 0; }
    .file-item:hover { background: rgba(210, 210, 210, 0.35); }
    .file-item.selected {
      background: #e2e2e2;
      box-shadow: inset 3px 0 0 var(--accent);
    }
    .sidebar-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .file-item strong {
      display: block;
      margin-bottom: 4px;
      font-size: 14px;
    }
    .file-item span {
      display: block;
      color: var(--text-muted);
      font-size: 12px;
      line-height: 1.4;
      word-break: break-word;
    }
    .main {
      padding: 10px;
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 10px;
      min-height: 0;
    }
    .top-row {
      display: grid;
      grid-template-columns: minmax(300px, 420px) minmax(0, 1fr);
      gap: 10px;
    }
    .panel {
      border: 1px solid #d2d2d2;
      border-radius: 0;
      background: #f5f5f5;
      padding: 10px;
      min-height: 0;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.7);
    }
    .summary-grid {
      display: grid;
      gap: 8px;
      font-size: 14px;
    }
    .summary-grid .muted { color: var(--text-muted); }
    .preview-wrap {
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 8px;
      min-height: 320px;
    }
    .preview-toolbar {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
    }
    .toolbar-cluster {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }
    .toolbar-label {
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-right: 2px;
    }
    .preview-mode, .view-preset {
      padding: 6px 10px;
      border: 1px solid #9a9a9a;
      border-radius: 0;
      background: #e6e6e6;
      cursor: pointer;
      font-size: 12px;
      font-weight: 600;
    }
    .preview-mode.active, .view-preset.active {
      background: #ffd8a8;
      border-color: #f59e0b;
      color: #7c3f00;
      font-weight: 700;
    }
    .canvas-frame {
      position: relative;
      min-height: 320px;
      border: 0;
      border-radius: 0;
      overflow: hidden;
      background: #1d1d1d;
      box-shadow: none;
    }
    canvas {
      width: 100%;
      height: 100%;
      min-height: 320px;
      background: transparent;
      display: block;
    }
    .viewport-overlay {
      position: absolute;
      inset: 0;
      display: none;
      align-items: flex-start;
      justify-content: flex-start;
      padding: 14px 16px;
      pointer-events: none;
      font-size: 13px;
      line-height: 1.45;
      color: #343434;
      white-space: pre-wrap;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.22), rgba(255, 255, 255, 0));
    }
    .viewport-overlay.visible {
      display: flex;
    }
    .viewport-overlay[data-tone="error"] {
      color: #7f1d1d;
      background: linear-gradient(180deg, rgba(255, 245, 245, 0.85), rgba(255, 255, 255, 0));
    }
    .viewport-footer {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      flex-wrap: wrap;
      padding: 10px 12px 0 12px;
    }
    .preview-note {
      font-size: 12px;
      color: var(--text-muted);
      line-height: 1.4;
      max-width: 700px;
    }
    .viewport-badges {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      justify-content: flex-end;
    }
    .viewport-badge {
      padding: 5px 9px;
      border-radius: 0;
      border: 1px solid #c8c8c8;
      background: rgba(255,255,255,0.86);
      color: var(--text-main);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .tabs {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .tab {
      padding: 8px 10px;
      border: 1px solid #b3b3b3;
      border-radius: 0;
      background: #e6e6e6;
      cursor: pointer;
      user-select: none;
      font-size: 13px;
      font-weight: 600;
    }
    .tab.active {
      background: #d8d8d8;
      border-color: #8c8c8c;
      font-weight: 700;
    }
    .tab-panels {
      border: 1px solid #d2d2d2;
      border-radius: 0;
      background: #f5f5f5;
      overflow: auto;
      min-height: 0;
    }
    .tab-panel {
      display: none;
      padding: 10px;
      min-height: 100%;
    }
    .tab-panel.active { display: block; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th, td {
      border: 1px solid #d8d8d8;
      padding: 8px;
      text-align: left;
      vertical-align: top;
    }
    th {
      background: #efefef;
      font-weight: 700;
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: Consolas, Menlo, monospace;
      font-size: 13px;
      line-height: 1.5;
    }
    .empty {
      border: 1px dashed #bdbdbd;
      border-radius: 0;
      padding: 16px;
      color: var(--text-muted);
      background: rgba(255,255,255,0.78);
      font-size: 14px;
    }
    .compare-layout {
      display: grid;
      gap: 10px;
    }
    .compare-summary {
      border: 1px solid #d0d0d0;
      border-radius: 0;
      background: #ededed;
      padding: 12px;
    }
    .compare-summary h3 {
      margin: 0 0 10px 0;
      font-size: 14px;
    }
    .compare-summary-list {
      display: grid;
      gap: 8px;
    }
    .compare-summary-item {
      padding: 10px 12px;
      border-radius: 0;
      border: 1px solid rgba(140, 140, 140, 0.32);
      background: rgba(255,255,255,0.9);
      font-size: 13px;
      line-height: 1.45;
      color: var(--text-main);
    }
    .compare-change-row td {
      background: rgba(30,136,229,0.08);
    }
    .compare-change-cell {
      display: grid;
      gap: 8px;
    }
    .compare-change-pill {
      padding: 8px 10px;
      border: 1px solid rgba(30,136,229,0.28);
      background: rgba(255,255,255,0.94);
      color: var(--text-main);
      font-size: 13px;
      line-height: 1.45;
    }
    .compare-table-row-changed td {
      background: rgba(30,136,229,0.05);
    }
    .compare-controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      padding: 10px 12px;
      border: 1px solid #d2d2d2;
      border-radius: 0;
      background: #efefef;
      font-size: 13px;
      color: #333333;
    }
    .compare-controls label {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      user-select: none;
    }
    .compare-preview-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .compare-preview-card {
      border: 1px solid #d2d2d2;
      border-radius: 0;
      background: #f5f5f5;
      padding: 10px;
    }
    .compare-preview-card h3 {
      margin: 0 0 8px 0;
      font-size: 14px;
    }
    .compare-canvas {
      width: 100%;
      height: 360px;
      min-height: 360px;
      display: block;
    }
    .compare-note {
      margin-top: 8px;
      color: var(--text-muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .body-visibility {
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid #d5d5d5;
      display: grid;
      gap: 10px;
    }
    .body-visibility-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .body-visibility-list {
      display: grid;
      gap: 6px;
    }
    .body-visibility-item {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--text-main);
    }
    .body-visibility-item small {
      color: var(--text-muted);
    }
    @media (max-width: 980px) {
      .content { grid-template-columns: 1fr; }
      .top-row { grid-template-columns: 1fr; }
      .compare-preview-grid { grid-template-columns: 1fr; }
    }
  </style>
  <script type="importmap">
    {
      "imports": {
        "three": "/vendor/three/build/three.module.js"
      }
    }
  </script>
</head>
<body>
  <div class="app">
    <div class="header">
      <h1>CAD Part Viewer & Compare</h1>
      <p>Load a `.step` or `.stp` file to preview it like a typical CAD model, then select two loaded parts to compare what changed. `.x_t`, compatible JSON, and text reports are also supported.</p>
    </div>

    <div class="toolbar">
      <button id="load-samples">Load Workspace Samples</button>
      <label class="file-upload">Upload CAD Files<input id="file-input" type="file" accept=".x_t,.step,.stp,.json,.txt,.md,text/plain,application/json" multiple></label>
      <input id="path-input" type="text" placeholder="Enter one or more file paths separated by commas">
      <button id="load-paths">Load Paths</button>
      <button id="export-json">Export Current JSON</button>
      <button id="reset-view">Reset Preview View</button>
    </div>

    <div class="content">
      <div class="sidebar">
        <h2>Loaded Parts</h2>
        <div class="hint">Click one part to preview it. Use Ctrl-click, Cmd-click, or Shift-click to add a second part, then compare what changed.</div>
        <div class="file-list" id="file-list"></div>
        <div class="sidebar-actions">
          <button id="compare-selected" disabled>Compare Selected Parts</button>
          <button id="create-fusion" disabled>Improve JSON With STEP</button>
        </div>
        <div class="hint" id="sidebar-foot">No parts loaded yet.</div>
      </div>

      <div class="main">
        <div class="top-row">
          <div class="panel">
            <h2 id="part-title">No part selected</h2>
            <div class="summary-grid" id="summary-grid">
              <div class="muted">Load a file to see part information.</div>
            </div>
          </div>

          <div class="panel preview-wrap">
            <h2>Preview</h2>
            <div class="preview-toolbar">
              <div class="toolbar-cluster" id="preview-modes"></div>
              <div class="toolbar-cluster" id="preview-toolbar-right"></div>
            </div>
            <div class="canvas-frame">
              <canvas id="preview-canvas"></canvas>
              <div id="preview-overlay" class="viewport-overlay"></div>
            </div>
            <div class="viewport-footer">
            <div class="preview-note">Orbit with drag. Pan with Shift + drag or right-drag. Mouse wheel zooms. Double-click fits the model. Solid mode uses STEP face tessellation when available and otherwise falls back to the best reconstructed preview geometry.</div>
              <div class="viewport-badges">
                <div class="viewport-badge">Orbit</div>
                <div class="viewport-badge">Pan</div>
                <div class="viewport-badge">Inspect</div>
              </div>
            </div>
          </div>
        </div>

        <div class="tabs" id="tabs"></div>

        <div class="tab-panels">
          <div class="tab-panel active" data-panel="overview"><div id="overview-panel"></div></div>
          <div class="tab-panel" data-panel="notes"><pre id="notes-panel"></pre></div>
          <div class="tab-panel" data-panel="header"><pre id="header-panel"></pre></div>
          <div class="tab-panel" data-panel="topology"><pre id="topology-panel"></pre></div>
          <div class="tab-panel" data-panel="geometry"><pre id="geometry-panel"></pre></div>
          <div class="tab-panel" data-panel="compare"><div id="compare-panel"></div></div>
          <div class="tab-panel" data-panel="json"><pre id="json-panel"></pre></div>
        </div>
      </div>
    </div>

    <div class="status" id="status">Load a STEP file to preview it, or select two parts to compare them.</div>
  </div>

  <script>
    function identityRotation() {
      return [
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1]
      ];
    }

    function multiplyRotationMatrices(a, b) {
      return a.map((row, rowIndex) => ([
        row[0] * b[0][0] + row[1] * b[1][0] + row[2] * b[2][0],
        row[0] * b[0][1] + row[1] * b[1][1] + row[2] * b[2][1],
        row[0] * b[0][2] + row[1] * b[1][2] + row[2] * b[2][2]
      ]));
    }

    function applyRotationMatrix(matrix, point) {
      return [
        matrix[0][0] * point[0] + matrix[0][1] * point[1] + matrix[0][2] * point[2],
        matrix[1][0] * point[0] + matrix[1][1] * point[1] + matrix[1][2] * point[2],
        matrix[2][0] * point[0] + matrix[2][1] * point[1] + matrix[2][2] * point[2]
      ];
    }

    function rotationAroundX(angle) {
      const c = Math.cos(angle);
      const s = Math.sin(angle);
      return [
        [1, 0, 0],
        [0, c, -s],
        [0, s, c]
      ];
    }

    function rotationAroundY(angle) {
      const c = Math.cos(angle);
      const s = Math.sin(angle);
      return [
        [c, 0, -s],
        [0, 1, 0],
        [s, 0, c]
      ];
    }

    function vectorLength(vector) {
      return Math.hypot(vector[0], vector[1], vector[2]);
    }

    function normalizeVector(vector) {
      const length = vectorLength(vector);
      if (!length) return [0, 0, 0];
      return vector.map((value) => value / length);
    }

    function crossProduct(a, b) {
      return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0]
      ];
    }

    function normalizeRotationMatrix(matrix) {
      const xAxis = normalizeVector(matrix[0]);
      let yAxis = matrix[1];
      let zAxis = normalizeVector(crossProduct(xAxis, yAxis));
      if (!vectorLength(zAxis)) {
        zAxis = [0, 0, 1];
      }
      yAxis = normalizeVector(crossProduct(zAxis, xAxis));
      return [xAxis, yAxis, zAxis];
    }

    function rotationFromYawPitch(yaw, pitch) {
      return multiplyRotationMatrices(rotationAroundX(pitch), rotationAroundY(yaw));
    }

    function defaultRotation() {
      return rotationFromYawPitch(-0.7, 0.5);
    }

    function defaultViewerState() {
      return {
        rotation: defaultRotation(),
        zoom: 1,
        panX: 0,
        panY: 0,
        mode: "solid"
      };
    }

    const state = {
      reports: [],
      selected: [],
      activeTab: "overview",
      viewer: defaultViewerState(),
      viewportBackground: "gray",
      compareSync: false,
      hiddenPreviewBodies: {},
      compareViewers: {
        left: defaultViewerState(),
        right: defaultViewerState()
      }
    };

    const tabs = [
      ["overview", "Overview"],
      ["compare", "Compare"],
      ["notes", "Part Notes"],
      ["header", "Header"],
      ["topology", "Topology"],
      ["geometry", "Geometry"],
      ["json", "JSON"]
    ];

    const AXES = ["x", "y", "z"];
    const DIMENSION_TOLERANCE = 1e-6;
    const MAX_RENDER_FACES = 3200;
    const MAX_RENDER_OCC_FACES = 2200;
    const VIEW_PRESETS = [
      ["iso", "ISO"],
      ["front", "Front"],
      ["top", "Top"],
      ["right", "Right"],
      ["fit", "Fit"]
    ];
    const VIEWPORT_BACKGROUNDS = [
      ["white", "White", "#ffffff"],
      ["gray", "Gray", "#d9d9d9"],
      ["dark", "Dark", "#1d1d1d"]
    ];
    const THREE_MODULE_URL = "/vendor/three/build/three.module.js";
    const ORBIT_CONTROLS_MODULE_URL = "/vendor/three/examples/jsm/controls/OrbitControls.js";

    const canvas = document.getElementById("preview-canvas");
    const previewOverlay = document.getElementById("preview-overlay");
    let dragState = null;
    let previewViewport = null;
    let previewViewportPromise = null;
    let previewViewportDisabled = false;
    let previewViewportFailure = "";
    let previewCanvasFallbackBound = false;

    function byId(id) {
      return document.getElementById(id);
    }

    function reportKey(report) {
      return report.file + "::" + report.file_name;
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function setStatus(message) {
      byId("status").textContent = message;
    }

    function setPreviewOverlay(message = "", tone = "info") {
      if (!previewOverlay) return;
      previewOverlay.textContent = message || "";
      previewOverlay.dataset.tone = tone;
      previewOverlay.classList.toggle("visible", Boolean(message));
    }

    function activeReport() {
      if (!state.selected.length) return null;
      return state.reports.find((report) => reportKey(report) === state.selected[0]) || null;
    }

    function selectedReports() {
      return state.selected
        .map((key) => state.reports.find((report) => reportKey(report) === key))
        .filter(Boolean);
    }

    function kernelBodies(report) {
      if (!report) return [];
      if (Array.isArray(report.kernel_bodies) && report.kernel_bodies.length) {
        return report.kernel_bodies.filter(Boolean);
      }
      return report.kernel_body ? [report.kernel_body] : [];
    }

    function previewBodyKey(body, bodyIndex = 0) {
      return rawIdentityKey(
        body?.metadata?.object_identity,
        `preview-body:${Number(body?.metadata?.nx_body_index || body?.index || bodyIndex + 1)}`
      );
    }

    function hiddenPreviewBodiesForReport(report) {
      if (!report) return [];
      return state.hiddenPreviewBodies[reportKey(report)] || [];
    }

    function visiblePreviewBodyCount(report) {
      const bodies = kernelBodies(report);
      if (!bodies.length) return 0;
      const hidden = new Set(hiddenPreviewBodiesForReport(report));
      return bodies.filter((body, bodyIndex) => !hidden.has(previewBodyKey(body, bodyIndex))).length;
    }

    function setHiddenPreviewBodies(report, hiddenKeys) {
      if (!report) return;
      state.hiddenPreviewBodies = {
        ...state.hiddenPreviewBodies,
        [reportKey(report)]: dedupeList(hiddenKeys)
      };
    }

    function togglePreviewBody(report, bodyKey, visible) {
      const hidden = new Set(hiddenPreviewBodiesForReport(report));
      if (visible) hidden.delete(bodyKey);
      else hidden.add(bodyKey);
      setHiddenPreviewBodies(report, [...hidden]);
      renderOverview();
      drawPreview();
      if (state.activeTab === "compare") {
        drawComparePreviews();
      }
    }

    function inferredTopology(report) {
      return report?.model_analysis?.topology || report?.kernel_topology || null;
    }

    function reportPreviewScore(report) {
      return (report.geometry_hints?.point_count || 0) + ((inferredTopology(report) ? 1 : 0) * 1000);
    }

    function bestReportKey(reports) {
      if (!reports.length) return null;
      const best = [...reports].sort((a, b) => reportPreviewScore(b) - reportPreviewScore(a))[0];
      return reportKey(best);
    }

    function comparisonReports() {
      if (state.selected.length !== 2) return [];
      return selectedReports();
    }

    function canCompareSelection() {
      return state.selected.length === 2;
    }

    function formatDensity(report) {
      if (!report || !report.density) return "Not found";
      return `${report.density.value} ${report.density.unit || ""}`.trim();
    }

    function formatColors(report) {
      if (!report || !report.colors || !report.colors.length) return "Not found";
      return report.colors.map((color) => color.hex).join(", ");
    }

    function sourceFormatLabel(report) {
      const format = report?.transmit_info?.source_format || "";
      const labels = {
        fused_step_json: "Fused STEP + JSON",
        siemens_nx_json: "Siemens NX JSON",
        step_file: "STEP",
        parasolid_analysis_report: "Parasolid analysis report"
      };
      if (labels[format]) return labels[format];
      const fileName = String(report?.file_name || report?.file || "").toLowerCase();
      if (fileName.endsWith(".x_t")) return "Parasolid XT";
      return format ? format.replaceAll("_", " ") : "Unknown";
    }

    function stepBackendLabel(report) {
      const sourceFormat = report?.transmit_info?.source_format || "";
      if (!["step_file", "fused_step_json"].includes(sourceFormat)) return "Not applicable";
      const backend = report?.step_import?.backend || report?.entity_hints?.step_backend || "unknown";
      if (backend === "opencascade_occ") return "Open Cascade";
      if (backend === "native_step_parser") return "Native parser fallback";
      return backend.replaceAll("_", " ");
    }

    function fusionLabel(report) {
      if (!report?.fusion?.enabled) return "Off";
      const mlPrefix = report?.fusion?.ml_assisted ? "ML-assisted " : "";
      const faceRegionCounts = report.fusion?.face_region_source_counts || {};
      const edgeRegionCounts = report.fusion?.edge_region_source_counts || {};
      if (Object.keys(faceRegionCounts).length || Object.keys(edgeRegionCounts).length) {
        return `${mlPrefix}On (${faceRegionCounts.json || 0} JSON face regions, ${faceRegionCounts.step || 0} STEP face regions; ${edgeRegionCounts.json || 0} JSON edge regions, ${edgeRegionCounts.step || 0} STEP edge regions)`;
      }
      const faceCounts = report.fusion?.face_source_counts || {};
      const edgeCounts = report.fusion?.edge_source_counts || {};
      return `${mlPrefix}On (${faceCounts.json || 0} JSON face bodies, ${faceCounts.step || 0} STEP face bodies; ${edgeCounts.json || 0} JSON edge bodies, ${edgeCounts.step || 0} STEP edge bodies)`;
    }

    function fusionMlLabel(report) {
      if (!report?.fusion?.ml_assisted) return "Off";
      const model = report?.fusion?.ml_model?.name || "Unknown";
      const decisions = report?.fusion?.body_decisions || [];
      const confidences = decisions
        .map((item) => Number(item?.ml_match_confidence))
        .filter((value) => Number.isFinite(value));
      if (!confidences.length) return model;
      const average = confidences.reduce((sum, value) => sum + value, 0) / confidences.length;
      return `${model} (avg confidence ${average.toFixed(2)})`;
    }

    function fusionDecisionLines(report, limit = 4) {
      const decisions = report?.fusion?.body_decisions || [];
      return decisions.slice(0, limit).map((item) => {
        const bodyIndex = item?.body_index ?? "?";
        const matchReason = item?.match_reason || "unknown";
        const confidence = Number(item?.ml_match_confidence);
        const confidenceText = Number.isFinite(confidence) ? confidence.toFixed(2) : "n/a";
        return `Body ${bodyIndex}: ${matchReason}, confidence ${confidenceText}, faces ${item?.face_source || "?"}, edges ${item?.edge_source || "?"}`;
      });
    }

    function canFuseSelection() {
      const reports = selectedReports();
      if (reports.length !== 2) return false;
      const formats = new Set(reports.map((report) => report?.transmit_info?.source_format || ""));
      return formats.has("siemens_nx_json") && formats.has("step_file");
    }

    function formatBounds(bounds) {
      if (!bounds) return "Not inferred";
      return bounds.size.map((value) => Number(value).toFixed(6)).join(" x ");
    }

    function formatLength(value) {
      const numeric = Number(value || 0);
      return `${(numeric * 1000).toFixed(3)} mm (${(numeric * 39.37007874015748).toFixed(3)} in)`;
    }

    function formatSignedInches(value) {
      const inches = Number(value || 0) * 39.37007874015748;
      return `${inches >= 0 ? "+" : ""}${inches.toFixed(3)} in`;
    }

    function formatInches(value) {
      return `${(Number(value || 0) * 39.37007874015748).toFixed(3)} in`;
    }

    function changeVerb(delta) {
      return delta >= 0 ? "increased" : "decreased";
    }

    function capitalizeLabel(value) {
      return String(value || "")
        .replaceAll("_", " ")
        .replace(/\\b\\w/g, (letter) => letter.toUpperCase());
    }

    function componentIdentity(component, bodyIndex, componentIndex) {
      const metadata = component?.metadata || {};
      const primitive = metadata.primitive || component?.name || "component";
      const axis = metadata.axis || "na";
      return {
        id: `b${bodyIndex}-c${componentIndex}-${primitive}-${axis}`,
        primitive,
        axis: metadata.axis || null,
        bodyIndex,
        componentIndex,
      };
    }

    function componentLabel(component, identity, siblingCount = 1) {
      const componentContext = component?.metadata?.component_context || {};
      const explicitName = componentContext.display_name || componentContext.name || component?.name;
      if (explicitName) return capitalizeLabel(explicitName);
      const primitive = identity.primitive;
      if (primitive === "box") return siblingCount > 1 ? "Base block" : "Block";
      if (primitive === "cylinder") return "Cylinder";
      if (primitive === "sphere") return "Sphere";
      return component?.name ? capitalizeLabel(component.name) : `Component ${identity.componentIndex + 1}`;
    }

    function hashString(value) {
      let hash = 0;
      for (const char of String(value || "")) {
        hash = ((hash << 5) - hash) + char.charCodeAt(0);
        hash |= 0;
      }
      return Math.abs(hash);
    }

    function inferredComponentColor(component, componentName, identity) {
      const text = `${componentName || ""} ${component?.name || ""} ${identity?.primitive || ""}`.toLowerCase();

      if (/(seal|o-ring|oring|gasket)/.test(text)) return [42, 42, 46];
      if (/(body|housing|case|barrel)/.test(text)) return [188, 154, 126];
      if (/(lever|arm|bracket|link)/.test(text)) return [206, 210, 216];
      if (/(piston|pin|rod|shaft|cotter)/.test(text)) return [214, 217, 222];
      if (/(washer|spacer|bushing)/.test(text)) return [196, 199, 205];
      if (/(spring|clip)/.test(text)) return [122, 128, 138];

      const palette = [
        [206, 210, 216],
        [188, 154, 126],
        [214, 217, 222],
        [176, 182, 192],
        [158, 166, 178],
      ];
      return palette[hashString(text) % palette.length];
    }

    function nxColorIndexToRgb(index) {
      const palette = {
        1: [0, 0, 255],
        2: [255, 0, 0],
        3: [0, 255, 0],
        4: [0, 255, 255],
        5: [255, 255, 0],
        6: [255, 0, 255],
        7: [255, 255, 255],
        8: [80, 80, 80],
        9: [168, 168, 168],
        10: [122, 184, 255],
        11: [255, 160, 122],
        12: [144, 238, 144],
        13: [255, 214, 102],
        14: [214, 170, 122],
        15: [196, 199, 205],
        16: [160, 164, 172],
        17: [92, 96, 104],
        18: [42, 42, 46],
        186: [188, 154, 126],
      };
      return palette[index] || null;
    }

    function displayColorFromProperties(displayProperties, fallback = null) {
      if (!displayProperties) return fallback;
      if (Array.isArray(displayProperties.rgb) && displayProperties.rgb.length >= 3) {
        return displayProperties.rgb.slice(0, 3).map((value) => Number(value));
      }
      if (Number.isFinite(Number(displayProperties.color_index))) {
        return nxColorIndexToRgb(Number(displayProperties.color_index)) || fallback;
      }
      return fallback;
    }

    function displayOpacityFromProperties(displayProperties, fallback = 1) {
      if (!displayProperties) return fallback;
      const translucency = Number(displayProperties.translucency);
      if (!Number.isFinite(translucency)) return fallback;
      return Math.max(0.16, Math.min(1, 1 - (translucency / 100)));
    }

    function isBlankedFromDisplay(displayProperties) {
      return Boolean(displayProperties?.is_blanked);
    }

    function boundsFromCenterSize(center, size) {
      return {
        min: center.map((value, index) => value - size[index] / 2),
        max: center.map((value, index) => value + size[index] / 2),
        size: [...size],
      };
    }

    function cylinderBounds(axis, center, radius, height) {
      const extents = [radius, radius, radius];
      const axisIndex = AXES.indexOf(axis || "z");
      extents[Math.max(axisIndex, 0)] = height / 2;
      return {
        min: center.map((value, index) => value - extents[index]),
        max: center.map((value, index) => value + extents[index]),
        size: extents.map((value) => value * 2),
      };
    }

    function perpendicularAxisForCylinder(axis) {
      if (axis === "x") return "y";
      if (axis === "y") return "x";
      return "x";
    }

    function describeComponent(component, identity, siblingCount = 1) {
      const metadata = component?.metadata || {};
      const primitive = identity.primitive;
      const label = componentLabel(component, identity, siblingCount);

      if (primitive === "box") {
        const dimensions = metadata.dimensions || {};
        const center = (metadata.center || [0, 0, 0]).map(Number);
        const size = AXES.map((axis) => Number(dimensions[axis] || 0));
        return {
          ...identity,
          label,
          bounds: boundsFromCenterSize(center, size),
          center,
          measurements: Object.fromEntries(AXES.map((axis, index) => [axis, size[index]])),
        };
      }

      if (primitive === "cylinder") {
        const axis = metadata.axis || "z";
        const radius = Number(metadata.radius || 0);
        const height = Number(metadata.height || 0);
        const center = (metadata.center || [0, 0, 0]).map(Number);
        return {
          ...identity,
          label,
          axis,
          center,
          bounds: cylinderBounds(axis, center, radius, height),
          measurements: {
            height,
            radius,
            diameter: radius * 2,
          },
        };
      }

      if (primitive === "sphere") {
        const radius = Number(metadata.radius || 0);
        const center = (metadata.center || [0, 0, 0]).map(Number);
        const size = [radius * 2, radius * 2, radius * 2];
        return {
          ...identity,
          label,
          center,
          bounds: boundsFromCenterSize(center, size),
          measurements: {
            diameter: radius * 2,
            radius,
          },
        };
      }

      const pointCloud = [
        ...(component?.faces || []).flatMap((face) => face.vertices || []),
        ...(component?.edges || []).flatMap((edge) => edge.points || []),
      ].map((point) => point.map(Number));
      const bounds = pointCloud.length ? boundsFromPoints(pointCloud) : null;
      return {
        ...identity,
        label,
        center: bounds
          ? bounds.min.map((value, index) => (value + bounds.max[index]) / 2)
          : [0, 0, 0],
        bounds,
        measurements: bounds
          ? Object.fromEntries(AXES.map((axis, index) => [axis, Number(bounds.size[index] || 0)]))
          : {},
      };
    }

    function reportComponents(report) {
      const results = [];
      kernelBodies(report).forEach((body, bodyIndex) => {
        if (body?.metadata?.primitive === "compound" && Array.isArray(body.metadata.components)) {
          const componentCount = body.metadata.components.length;
          body.metadata.components.forEach((component, componentIndex) => {
            results.push(describeComponent(component, componentIdentity(component, bodyIndex, componentIndex), componentCount));
          });
          return;
        }
        results.push(describeComponent(body, componentIdentity(body, bodyIndex, 0), 1));
      });
      return results.filter((component) => component && component.bounds);
    }

    function compareAxisDimensions(left, right) {
      const leftBounds = left?.geometry_hints?.bounds;
      const rightBounds = right?.geometry_hints?.bounds;
      if (!leftBounds || !rightBounds) return [];

      return AXES.map((axis, index) => {
        const leftValue = Number(leftBounds.size?.[index] || 0);
        const rightValue = Number(rightBounds.size?.[index] || 0);
        const delta = rightValue - leftValue;
        return {
          axis,
          leftValue,
          rightValue,
          delta,
          changed: Math.abs(delta) > DIMENSION_TOLERANCE
        };
      });
    }

    function matchRightComponent(leftComponent, rightComponents, used) {
      let match = rightComponents.find((candidate) => !used.has(candidate.id) && candidate.id === leftComponent.id);
      if (match) return match;

      match = rightComponents.find((candidate) =>
        !used.has(candidate.id)
        && candidate.primitive === leftComponent.primitive
        && (candidate.axis || null) === (leftComponent.axis || null)
      );
      if (match) return match;

      return rightComponents.find((candidate) => !used.has(candidate.id) && candidate.primitive === leftComponent.primitive) || null;
    }

    function componentPropertyChanges(leftComponent, rightComponent) {
      const primitive = leftComponent.primitive;
      const properties = [];

      if (primitive === "box") {
        AXES.forEach((axis) => {
          const leftValue = Number(leftComponent.measurements[axis] || 0);
          const rightValue = Number(rightComponent.measurements[axis] || 0);
          const delta = rightValue - leftValue;
          if (Math.abs(delta) > DIMENSION_TOLERANCE) {
            properties.push({ kind: "size", axis, label: `${axis.toUpperCase()} size`, leftValue, rightValue, delta });
          }
        });
        return properties;
      }

      if (primitive === "cylinder") {
        const heightDelta = Number(rightComponent.measurements.height || 0) - Number(leftComponent.measurements.height || 0);
        if (Math.abs(heightDelta) > DIMENSION_TOLERANCE) {
          properties.push({
            kind: "height",
            axis: leftComponent.axis || rightComponent.axis || "z",
            label: `${(leftComponent.axis || rightComponent.axis || "z").toUpperCase()} height`,
            leftValue: Number(leftComponent.measurements.height || 0),
            rightValue: Number(rightComponent.measurements.height || 0),
            delta: heightDelta,
          });
        }

        const diameterLeft = Number(leftComponent.measurements.diameter || 0);
        const diameterRight = Number(rightComponent.measurements.diameter || 0);
        const diameterDelta = diameterRight - diameterLeft;
        if (Math.abs(diameterDelta) > DIMENSION_TOLERANCE) {
          properties.push({
            kind: "diameter",
            axis: perpendicularAxisForCylinder(leftComponent.axis || rightComponent.axis || "z"),
            label: "Diameter",
            leftValue: diameterLeft,
            rightValue: diameterRight,
            delta: diameterDelta,
          });
        }
        return properties;
      }

      AXES.forEach((axis, index) => {
        const leftValue = Number(leftComponent.bounds?.size?.[index] || 0);
        const rightValue = Number(rightComponent.bounds?.size?.[index] || 0);
        const delta = rightValue - leftValue;
        if (Math.abs(delta) > DIMENSION_TOLERANCE) {
          properties.push({ kind: "size", axis, label: `${axis.toUpperCase()} size`, leftValue, rightValue, delta });
        }
      });
      return properties;
    }

    function cylinderDiameterEndpoints(component, axis) {
      const center = component.center.map(Number);
      const radius = Number(component.measurements.radius || component.measurements.diameter / 2 || 0);
      const alongAxis = component.axis || "z";
      const capCoordinate = component.bounds.max[AXES.indexOf(alongAxis)];
      if (alongAxis === "x") {
        if (axis === "z") return [[center[0], center[1], capCoordinate - radius], [center[0], center[1], capCoordinate + radius]];
        return [[center[0], center[1] - radius, capCoordinate], [center[0], center[1] + radius, capCoordinate]];
      }
      if (alongAxis === "y") {
        if (axis === "z") return [[center[0], center[1], capCoordinate - radius], [center[0], center[1], capCoordinate + radius]];
        return [[center[0] - radius, center[1], capCoordinate], [center[0] + radius, center[1], capCoordinate]];
      }
      return [[center[0] - radius, center[1], capCoordinate], [center[0] + radius, center[1], capCoordinate]];
    }

    function annotationEndpointsForProperty(component, property) {
      if (!component?.bounds) return null;
      if (property.kind === "diameter" && component.primitive === "cylinder") {
        return cylinderDiameterEndpoints(component, property.axis);
      }
      return axisDimensionEndpoints(component.bounds, property.axis || "z");
    }

    function measurementLineForProperty(component, property) {
      const endpoints = annotationEndpointsForProperty(component, property);
      const axis = property.axis || "z";
      const axisIndex = AXES.indexOf(axis);
      if (!endpoints || axisIndex < 0) return null;
      const start = endpoints[0].map(Number);
      const end = endpoints[1].map(Number);
      return {
        axis,
        axisIndex,
        start,
        end,
        minCoord: Math.min(start[axisIndex], end[axisIndex]),
        maxCoord: Math.max(start[axisIndex], end[axisIndex]),
      };
    }

    function pointOnMeasurementLine(line, coordinate) {
      const point = line.start.map(Number);
      point[line.axisIndex] = Number(coordinate);
      return point;
    }

    function measurementDifferenceSegments(referenceLine, changedLine) {
      if (!referenceLine || !changedLine) return [];
      const overlapMin = Math.max(referenceLine.minCoord, changedLine.minCoord);
      const overlapMax = Math.min(referenceLine.maxCoord, changedLine.maxCoord);

      if (overlapMax - overlapMin <= DIMENSION_TOLERANCE) {
        return [{ start: changedLine.start, end: changedLine.end }];
      }

      const segments = [];
      if (changedLine.minCoord < overlapMin - DIMENSION_TOLERANCE) {
        segments.push({
          start: pointOnMeasurementLine(changedLine, changedLine.minCoord),
          end: pointOnMeasurementLine(changedLine, overlapMin),
        });
      }
      if (changedLine.maxCoord > overlapMax + DIMENSION_TOLERANCE) {
        segments.push({
          start: pointOnMeasurementLine(changedLine, overlapMax),
          end: pointOnMeasurementLine(changedLine, changedLine.maxCoord),
        });
      }
      return segments;
    }

    function focusSegmentsForProperty(leftComponent, rightComponent, property) {
      const leftLine = measurementLineForProperty(leftComponent, property);
      const rightLine = measurementLineForProperty(rightComponent, property);
      if (!leftLine || !rightLine) {
        return { left: [], right: [] };
      }

      const leftSpan = leftLine.maxCoord - leftLine.minCoord;
      const rightSpan = rightLine.maxCoord - rightLine.minCoord;
      if (Math.abs(rightSpan - leftSpan) <= DIMENSION_TOLERANCE) {
        return { left: [], right: [] };
      }

      if (rightSpan > leftSpan) {
        return {
          left: [],
          right: measurementDifferenceSegments(leftLine, rightLine),
        };
      }

      return {
        left: measurementDifferenceSegments(rightLine, leftLine),
        right: [],
      };
    }

    function summarizeComponentChange(change) {
      const propertyDescriptions = change.properties.map((property) =>
        `${property.label} ${changeVerb(property.delta)} from ${formatLength(property.leftValue)} to ${formatLength(property.rightValue)} (${formatSignedInches(property.delta)})`
      );

      if (change.primitive === "cylinder" && change.properties.length === 1 && change.properties[0].kind === "height") {
        const radiusValue = Number(change.left.measurements.radius || change.right.measurements.radius || 0);
        return `${change.label} height ${changeVerb(change.properties[0].delta)} from ${formatLength(change.properties[0].leftValue)} to ${formatLength(change.properties[0].rightValue)} (${formatSignedInches(change.properties[0].delta)}). Radius stayed ${formatLength(radiusValue)}.`;
      }

      return `${change.label}: ${propertyDescriptions.join("; ")}.`;
    }

    function topologyLabel(report) {
      const topology = report?.model_analysis?.topology;
      if (topology?.shape_label) return topology.shape_label;

      const bodies = kernelBodies(report);
      if (bodies.length > 1) return `${bodies.length}-body import`;

      const primaryBody = bodies[0];
      const primitive = primaryBody?.metadata?.primitive;
      if (primitive === "box") return primaryBody?.name || "box";
      if (primitive === "cylinder") return "cylinder";
      if (primitive === "sphere") return "sphere";
      if (primitive === "arc_or_circle") return "arc / circle";

      return primaryBody?.name || "Not inferred";
    }

    function topologyCounts(report) {
      const topology = report?.model_analysis?.topology;
      if (topology) {
        return `${topology.face_count} faces / ${topology.edge_count} edges`;
      }

      const kernelTopology = report?.kernel_topology;
      if (kernelTopology) {
        return `${kernelTopology.faces?.length || 0} faces / ${kernelTopology.edges?.length || 0} edges`;
      }

      const kernelBody = report?.kernel_body;
      if (kernelBody) {
        if (kernelBody?.metadata?.primitive === "compound" && Array.isArray(kernelBody?.metadata?.components)) {
          const faceCount = kernelBody.metadata.components.reduce((sum, body) => sum + (body.faces?.length || 0), 0);
          const edgeCount = kernelBody.metadata.components.reduce((sum, body) => sum + (body.edges?.length || 0), 0);
          return `${faceCount} faces / ${edgeCount} edges across ${kernelBody.metadata.components.length} preview primitives`;
        }
        return `${kernelBody.faces?.length || 0} faces / ${kernelBody.edges?.length || 0} edges`;
      }

      const bodies = kernelBodies(report);
      if (bodies.length) {
        const faceCount = bodies.reduce((sum, body) => sum + (body.faces?.length || 0), 0);
        const edgeCount = bodies.reduce((sum, body) => sum + (body.edges?.length || 0), 0);
        return `${faceCount} faces / ${edgeCount} edges across ${bodies.length} bodies`;
      }

      return "Not inferred";
    }

    function kernelLabel(report) {
      const bodies = kernelBodies(report);
      if (!bodies.length) return "Not reconstructed";
      if (bodies.length === 1) return bodies[0]?.name || "Not reconstructed";
      return `multiple bodies (${bodies.length})`;
    }

    function rawIdentityKey(objectIdentity, fallback = null) {
      if (objectIdentity?.journal_identifier) return `jid:${objectIdentity.journal_identifier}`;
      if (Number.isFinite(Number(objectIdentity?.tag))) return `tag:${Number(objectIdentity.tag)}`;
      return fallback;
    }

    function structuredBodies(report) {
      if (Array.isArray(report?.source_bodies) && report.source_bodies.length) {
        return report.source_bodies.filter(Boolean);
      }
      if (Array.isArray(report?.bodies) && report.bodies.length) {
        return report.bodies.filter(Boolean);
      }
      return [];
    }

    function structuredTopologyCounts(report) {
      const bodies = structuredBodies(report);
      const bodyCount = bodies.length || Number(report?.source_body_count || 0);
      const faceCount = bodies.reduce((sum, body) => sum + (body?.faces?.length || 0), 0);
      const edgeCount = bodies.reduce((sum, body) => sum + (body?.edges?.length || 0), 0);
      return { bodyCount, faceCount, edgeCount };
    }

    function structuredTopologyLabel(report) {
      const counts = structuredTopologyCounts(report);
      if (!counts.bodyCount && !counts.faceCount && !counts.edgeCount) return "Not available";
      return `${counts.faceCount} faces / ${counts.edgeCount} edges across ${counts.bodyCount} bodies`;
    }

    function structuredBodyName(body, fallbackIndex = 0) {
      return body?.metadata?.name || body?.name || `Body ${fallbackIndex + 1}`;
    }

    function structuredBodyIdentity(body, fallbackIndex = 0) {
      const objectIdentity = body?.metadata?.object_identity || body?.object_identity || {};
      const name = structuredBodyName(body, fallbackIndex);
      const bodyIndex = Number(body?.index || (fallbackIndex + 1));
      return {
        id: rawIdentityKey(objectIdentity, `${name.toLowerCase()}::${bodyIndex}`),
        name,
        index: bodyIndex,
      };
    }

    function structuredEdgeIdentity(body, edge, bodyIndex = 0, edgeIndex = 0) {
      return rawIdentityKey(
        edge?.object_identity,
        `body:${Number(body?.index || bodyIndex + 1)}:edge:${Number(edge?.index || edgeIndex + 1)}`
      );
    }

    function structuredFaceIdentity(body, face, bodyIndex = 0, faceIndex = 0) {
      return rawIdentityKey(
        face?.object_identity,
        `body:${Number(body?.index || bodyIndex + 1)}:face:${Number(face?.index || faceIndex + 1)}`
      );
    }

    function matchStructuredBody(leftBody, rightBodies, used) {
      const leftIdentity = structuredBodyIdentity(leftBody);

      let match = rightBodies.find((candidate, candidateIndex) =>
        !used.has(structuredBodyIdentity(candidate, candidateIndex).id)
        && structuredBodyIdentity(candidate, candidateIndex).id === leftIdentity.id
      );
      if (match) return match;

      match = rightBodies.find((candidate, candidateIndex) =>
        !used.has(structuredBodyIdentity(candidate, candidateIndex).id)
        && structuredBodyIdentity(candidate, candidateIndex).name === leftIdentity.name
      );
      if (match) return match;

      match = rightBodies.find((candidate, candidateIndex) =>
        !used.has(structuredBodyIdentity(candidate, candidateIndex).id)
        && Number(candidate?.index || (candidateIndex + 1)) === leftIdentity.index
      );
      if (match) return match;

      return null;
    }

    function finiteNumber(value) {
      const numeric = Number(value);
      return Number.isFinite(numeric) ? numeric : null;
    }

    function pointArray(value) {
      if (Array.isArray(value) && value.length >= 3) {
        return value.slice(0, 3).map((item) => Number(item));
      }
      if (value && typeof value === "object") {
        const x = finiteNumber(value.x);
        const y = finiteNumber(value.y);
        const z = finiteNumber(value.z);
        if (x !== null && y !== null && z !== null) return [x, y, z];
      }
      return null;
    }

    function dedupeList(values) {
      return [...new Set((values || []).filter(Boolean))];
    }

    function formatArea(value) {
      return `${(Number(value || 0) * 1000000).toFixed(3)} mm²`;
    }

    function formatVolume(value) {
      return `${(Number(value || 0) * 1000000000).toFixed(3)} mm³`;
    }

    function metricValueList(values) {
      return values
        .map((value) => finiteNumber(value))
        .filter((value) => value !== null)
        .sort((a, b) => a - b);
    }

    function compareNumericLists(leftValues, rightValues, tolerance = DIMENSION_TOLERANCE, maxChanges = 3) {
      const left = metricValueList(leftValues);
      const right = metricValueList(rightValues);
      const changes = [];
      let leftIndex = 0;
      let rightIndex = 0;

      while (leftIndex < left.length || rightIndex < right.length) {
        const leftValue = leftIndex < left.length ? left[leftIndex] : null;
        const rightValue = rightIndex < right.length ? right[rightIndex] : null;

        if (leftValue !== null && rightValue !== null && Math.abs(rightValue - leftValue) <= tolerance) {
          leftIndex += 1;
          rightIndex += 1;
          continue;
        }

        if (leftValue !== null && rightValue !== null) {
          changes.push({ kind: "changed", leftValue, rightValue, delta: rightValue - leftValue });
          leftIndex += 1;
          rightIndex += 1;
        } else if (leftValue !== null) {
          changes.push({ kind: "removed", leftValue });
          leftIndex += 1;
        } else if (rightValue !== null) {
          changes.push({ kind: "added", rightValue });
          rightIndex += 1;
        }

        if (changes.length >= maxChanges) break;
      }

      return {
        changes,
        leftCount: left.length,
        rightCount: right.length,
        truncated: leftIndex < left.length || rightIndex < right.length,
      };
    }

    function compareValueEntries(leftEntries, rightEntries, tolerance = DIMENSION_TOLERANCE, maxChanges = 3) {
      const left = [...(leftEntries || [])]
        .filter((entry) => entry && finiteNumber(entry.value) !== null)
        .sort((a, b) => a.value - b.value);
      const right = [...(rightEntries || [])]
        .filter((entry) => entry && finiteNumber(entry.value) !== null)
        .sort((a, b) => a.value - b.value);

      const changes = [];
      let leftIndex = 0;
      let rightIndex = 0;

      while (leftIndex < left.length || rightIndex < right.length) {
        const leftEntry = leftIndex < left.length ? left[leftIndex] : null;
        const rightEntry = rightIndex < right.length ? right[rightIndex] : null;

        if (leftEntry && rightEntry && Math.abs(rightEntry.value - leftEntry.value) <= tolerance) {
          leftIndex += 1;
          rightIndex += 1;
          continue;
        }

        if (leftEntry && rightEntry) {
          changes.push({ kind: "changed", left: leftEntry, right: rightEntry, delta: rightEntry.value - leftEntry.value });
          leftIndex += 1;
          rightIndex += 1;
        } else if (leftEntry) {
          changes.push({ kind: "removed", left: leftEntry });
          leftIndex += 1;
        } else if (rightEntry) {
          changes.push({ kind: "added", right: rightEntry });
          rightIndex += 1;
        }

        if (changes.length >= maxChanges) break;
      }

      return { changes, truncated: leftIndex < left.length || rightIndex < right.length };
    }

    function circularEdgeDiameters(body) {
      return (body?.edges || [])
        .filter((edge) => edge?.type === "Circular")
        .map((edge) =>
          finiteNumber(edge?.curve_measurement?.diameter)
          ?? (
            finiteNumber(edge?.curve_measurement?.radius) !== null
              ? finiteNumber(edge?.curve_measurement?.radius) * 2
              : null
          )
          ?? (
            finiteNumber(edge?.exact_geometry?.analytic_curve?.radius) !== null
              ? finiteNumber(edge?.exact_geometry?.analytic_curve?.radius) * 2
              : null
          )
        )
        .filter((value) => value !== null);
    }

    function circularEdgeEntries(body, bodyIndex = 0) {
      return (body?.edges || [])
        .filter((edge) => edge?.type === "Circular")
        .map((edge, edgeIndex) => {
          const diameter =
            finiteNumber(edge?.curve_measurement?.diameter)
            ?? (
              finiteNumber(edge?.curve_measurement?.radius) !== null
                ? finiteNumber(edge?.curve_measurement?.radius) * 2
                : null
            )
            ?? (
              finiteNumber(edge?.exact_geometry?.analytic_curve?.radius) !== null
                ? finiteNumber(edge?.exact_geometry?.analytic_curve?.radius) * 2
                : null
            );
          if (diameter === null) return null;
          return {
            key: structuredEdgeIdentity(body, edge, bodyIndex, edgeIndex),
            value: diameter,
            center: pointArray(
              edge?.curve_measurement?.center
              || edge?.start_point
              || (Array.isArray(edge?.preview_points) && edge.preview_points.length ? edge.preview_points[0] : null)
            ),
            label: structuredBodyName(body, bodyIndex),
          };
        })
        .filter(Boolean);
    }

    function faceRadiusValues(body) {
      return (body?.faces || [])
        .map((face) =>
          finiteNumber(face?.measurement?.radius_or_diameter)
          ?? finiteNumber(face?.analytic_data?.radius)
          ?? finiteNumber(face?.blend_radius)
        )
        .filter((value) => value !== null && value > 0);
    }

    function faceRadiusEntries(body, bodyIndex = 0) {
      return (body?.faces || [])
        .map((face, faceIndex) => {
          const radius =
            finiteNumber(face?.measurement?.radius_or_diameter)
            ?? finiteNumber(face?.analytic_data?.radius)
            ?? finiteNumber(face?.blend_radius);
          if (radius === null || radius <= 0) return null;
          return {
            key: structuredFaceIdentity(body, face, bodyIndex, faceIndex),
            value: radius,
            center: pointArray(
              face?.measurement?.center
              || face?.analytic_data?.reference_point
              || null
            ),
            label: structuredBodyName(body, bodyIndex),
          };
        })
        .filter(Boolean);
    }

    function summarizeBreakdownDiff(kind, leftBreakdown, rightBreakdown) {
      const keys = [...new Set([
        ...Object.keys(leftBreakdown || {}),
        ...Object.keys(rightBreakdown || {}),
      ])];
      const changes = keys
        .map((key) => ({
          key,
          leftValue: Number(leftBreakdown?.[key] || 0),
          rightValue: Number(rightBreakdown?.[key] || 0),
        }))
        .filter((entry) => entry.leftValue !== entry.rightValue)
        .slice(0, 3);

      if (!changes.length) return null;
      const parts = changes.map((entry) => `${entry.key} ${entry.leftValue} -> ${entry.rightValue}`);
      return `${kind} changed (${parts.join(", ")}${keys.length > changes.length ? ", ..." : ""}).`;
    }

    function summarizeMetricSetDiff(bodyLabel, metricLabel, leftValues, rightValues, formatter = formatLength) {
      const diff = compareNumericLists(leftValues, rightValues);
      if (!diff.changes.length) return null;

      const parts = diff.changes.map((change) => {
        if (change.kind === "changed") {
          return `${formatter(change.leftValue)} -> ${formatter(change.rightValue)}`;
        }
        if (change.kind === "removed") {
          return `removed ${formatter(change.leftValue)}`;
        }
        return `added ${formatter(change.rightValue)}`;
      });

      return `${bodyLabel}: ${metricLabel} changed (${parts.join(", ")}${diff.truncated ? ", ..." : ""}).`;
    }

    function summarizeScalarChange(bodyLabel, metricLabel, leftValue, rightValue, formatter) {
      const leftNumeric = finiteNumber(leftValue);
      const rightNumeric = finiteNumber(rightValue);
      if (leftNumeric === null || rightNumeric === null) return null;
      if (Math.abs(rightNumeric - leftNumeric) <= DIMENSION_TOLERANCE) return null;
      return `${bodyLabel}: ${metricLabel} changed from ${formatter(leftNumeric)} to ${formatter(rightNumeric)}.`;
    }

    function compareStructuredReportData(left, right) {
      const leftBodies = structuredBodies(left);
      const rightBodies = structuredBodies(right);
      if (!leftBodies.length || !rightBodies.length) {
        return {
          summary: [],
          highlightBodyKeys: { left: [], right: [] },
          highlightRawFaces: { left: [], right: [] },
          highlightRawEdges: { left: [], right: [] },
          annotations: { left: [], right: [] },
        };
      }

      const summary = [];
      const highlightBodyKeys = { left: [], right: [] };
      const highlightRawFaces = { left: [], right: [] };
      const highlightRawEdges = { left: [], right: [] };
      const annotations = { left: [], right: [] };
      const usedRightBodyIds = new Set();

      const leftCounts = structuredTopologyCounts(left);
      const rightCounts = structuredTopologyCounts(right);
      if (
        leftCounts.bodyCount !== rightCounts.bodyCount
        || leftCounts.faceCount !== rightCounts.faceCount
        || leftCounts.edgeCount !== rightCounts.edgeCount
      ) {
        summary.push(
          `Raw NX topology changed from ${leftCounts.faceCount} faces / ${leftCounts.edgeCount} edges across ${leftCounts.bodyCount} bodies to ${rightCounts.faceCount} faces / ${rightCounts.edgeCount} edges across ${rightCounts.bodyCount} bodies.`
        );
      }

      leftBodies.forEach((leftBody, leftIndex) => {
        const rightBody = matchStructuredBody(leftBody, rightBodies, usedRightBodyIds);
        if (!rightBody) {
          summary.push(`Removed raw NX body: ${structuredBodyName(leftBody, leftIndex)}.`);
          highlightBodyKeys.left.push(structuredBodyIdentity(leftBody, leftIndex).id);
          return;
        }

        const rightIndex = rightBodies.indexOf(rightBody);
        const leftIdentity = structuredBodyIdentity(leftBody, leftIndex);
        const rightIdentity = structuredBodyIdentity(rightBody, rightIndex);
        usedRightBodyIds.add(rightIdentity.id);

        const bodyLabel = structuredBodyName(leftBody, leftIndex);
        const leftFaceCount = leftBody?.faces?.length || 0;
        const rightFaceCount = rightBody?.faces?.length || 0;
        const leftEdgeCount = leftBody?.edges?.length || 0;
        const rightEdgeCount = rightBody?.edges?.length || 0;
        if (leftFaceCount !== rightFaceCount || leftEdgeCount !== rightEdgeCount) {
          summary.push(
            `${bodyLabel}: raw topology changed from ${leftFaceCount} faces / ${leftEdgeCount} edges to ${rightFaceCount} faces / ${rightEdgeCount} edges.`
          );
          highlightBodyKeys.left.push(leftIdentity.id);
          highlightBodyKeys.right.push(rightIdentity.id);
        }

        const faceBreakdownSummary = summarizeBreakdownDiff(
          "face types",
          leftBody?.face_type_breakdown || {},
          rightBody?.face_type_breakdown || {}
        );
        if (faceBreakdownSummary) {
          summary.push(`${bodyLabel}: ${faceBreakdownSummary}`);
          highlightBodyKeys.left.push(leftIdentity.id);
          highlightBodyKeys.right.push(rightIdentity.id);
        }

        const edgeBreakdownSummary = summarizeBreakdownDiff(
          "edge types",
          leftBody?.edge_type_breakdown || {},
          rightBody?.edge_type_breakdown || {}
        );
        if (edgeBreakdownSummary) {
          summary.push(`${bodyLabel}: ${edgeBreakdownSummary}`);
          highlightBodyKeys.left.push(leftIdentity.id);
          highlightBodyKeys.right.push(rightIdentity.id);
        }

        const circularDiameterSummary = summarizeMetricSetDiff(
          bodyLabel,
          "circular diameters",
          circularEdgeDiameters(leftBody),
          circularEdgeDiameters(rightBody),
          formatLength
        );
        if (circularDiameterSummary) summary.push(circularDiameterSummary);

        const circularEntryDiff = compareValueEntries(
          circularEdgeEntries(leftBody, leftIndex),
          circularEdgeEntries(rightBody, rightIndex)
        );
        circularEntryDiff.changes.forEach((change) => {
          if (change.left?.key) highlightRawEdges.left.push(change.left.key);
          if (change.right?.key) highlightRawEdges.right.push(change.right.key);
          if (change.left?.center) {
            annotations.left.push({
              point: change.left.center,
              lines: [
                change.kind === "changed"
                  ? `${bodyLabel} circular diameter: ${formatInches(change.left.value)} -> ${formatInches(change.right.value)}`
                  : `${bodyLabel} circular diameter removed: ${formatInches(change.left.value)}`
              ],
            });
          }
          if (change.right?.center) {
            annotations.right.push({
              point: change.right.center,
              lines: [
                change.kind === "changed"
                  ? `${bodyLabel} circular diameter: ${formatInches(change.right.value)} (${formatSignedInches(change.delta)})`
                  : `${bodyLabel} circular diameter added: ${formatInches(change.right.value)}`
              ],
            });
          }
        });

        const faceRadiusSummary = summarizeMetricSetDiff(
          bodyLabel,
          "face radii",
          faceRadiusValues(leftBody),
          faceRadiusValues(rightBody),
          formatLength
        );
        if (faceRadiusSummary) summary.push(faceRadiusSummary);

        const faceEntryDiff = compareValueEntries(
          faceRadiusEntries(leftBody, leftIndex),
          faceRadiusEntries(rightBody, rightIndex)
        );
        faceEntryDiff.changes.forEach((change) => {
          if (change.left?.key) highlightRawFaces.left.push(change.left.key);
          if (change.right?.key) highlightRawFaces.right.push(change.right.key);
          if (change.left?.center) {
            annotations.left.push({
              point: change.left.center,
              lines: [
                change.kind === "changed"
                  ? `${bodyLabel} face radius: ${formatInches(change.left.value)} -> ${formatInches(change.right.value)}`
                  : `${bodyLabel} face radius removed: ${formatInches(change.left.value)}`
              ],
            });
          }
          if (change.right?.center) {
            annotations.right.push({
              point: change.right.center,
              lines: [
                change.kind === "changed"
                  ? `${bodyLabel} face radius: ${formatInches(change.right.value)} (${formatSignedInches(change.delta)})`
                  : `${bodyLabel} face radius added: ${formatInches(change.right.value)}`
              ],
            });
          }
        });

        const areaSummary = summarizeScalarChange(
          bodyLabel,
          "surface area",
          leftBody?.measurement?.surface_area,
          rightBody?.measurement?.surface_area,
          formatArea
        );
        if (areaSummary) {
          summary.push(areaSummary);
          highlightBodyKeys.left.push(leftIdentity.id);
          highlightBodyKeys.right.push(rightIdentity.id);
        }

        const volumeSummary = summarizeScalarChange(
          bodyLabel,
          "volume",
          leftBody?.measurement?.volume,
          rightBody?.measurement?.volume,
          formatVolume
        );
        if (volumeSummary) {
          summary.push(volumeSummary);
          highlightBodyKeys.left.push(leftIdentity.id);
          highlightBodyKeys.right.push(rightIdentity.id);
        }
      });

      const unmatchedRightBodies = rightBodies.filter((body, index) => !usedRightBodyIds.has(structuredBodyIdentity(body, index).id));
      unmatchedRightBodies.slice(0, 2).forEach((body, index) => {
        summary.push(`Added raw NX body: ${structuredBodyName(body, index)}.`);
        highlightBodyKeys.right.push(structuredBodyIdentity(body, index).id);
      });

      return {
        summary: summary.slice(0, 12),
        highlightBodyKeys: {
          left: dedupeList(highlightBodyKeys.left),
          right: dedupeList(highlightBodyKeys.right),
        },
        highlightRawFaces: {
          left: dedupeList(highlightRawFaces.left),
          right: dedupeList(highlightRawFaces.right),
        },
        highlightRawEdges: {
          left: dedupeList(highlightRawEdges.left),
          right: dedupeList(highlightRawEdges.right),
        },
        annotations: {
          left: (annotations.left || []).slice(0, 6),
          right: (annotations.right || []).slice(0, 6),
        },
      };
    }

    function compareDiffInfo(left, right) {
      const info = {
        changedAxes: [],
        changedShape: false,
        summary: [],
        dimensions: compareAxisDimensions(left, right),
        highlightComponents: { left: [], right: [] },
        highlightRawFaces: { left: [], right: [] },
        highlightRawEdges: { left: [], right: [] },
        annotations: { left: [], right: [] }
      };

      if (!left || !right) {
        return info;
      }

      const structuredDiff = compareStructuredReportData(left, right);

      const leftComponents = reportComponents(left);
      const rightComponents = reportComponents(right);
      const usedRightComponents = new Set();
      const componentChanges = [];

      for (const leftComponent of leftComponents) {
        const rightComponent = matchRightComponent(leftComponent, rightComponents, usedRightComponents);
        if (!rightComponent) continue;
        usedRightComponents.add(rightComponent.id);
        const properties = componentPropertyChanges(leftComponent, rightComponent);
        if (!properties.length) {
          componentChanges.push({
            primitive: leftComponent.primitive,
            label: leftComponent.label,
            left: leftComponent,
            right: rightComponent,
            properties: [],
            changed: false,
          });
          continue;
        }

        componentChanges.push({
          primitive: leftComponent.primitive,
          label: leftComponent.label,
          left: leftComponent,
          right: rightComponent,
          properties,
          changed: true,
        });
      }

      const leftShape = topologyLabel(left);
      const rightShape = topologyLabel(right);
      info.changedShape = leftShape !== rightShape;

      const changedComponents = componentChanges.filter((change) => change.changed);
      if (changedComponents.length) {
        info.changedAxes = [...new Set(changedComponents.flatMap((change) => change.properties.map((property) => property.axis).filter(Boolean)))];
        changedComponents.forEach((change) => {
          info.summary.push(summarizeComponentChange(change));
          info.highlightComponents.left.push(change.left.id);
          info.highlightComponents.right.push(change.right.id);
          change.properties.forEach((property) => {
            const leftEndpoints = annotationEndpointsForProperty(change.left, property);
            const rightEndpoints = annotationEndpointsForProperty(change.right, property);
            const focusSegments = focusSegmentsForProperty(change.left, change.right, property);
            if (leftEndpoints) {
              info.annotations.left.push({
                start: leftEndpoints[0],
                end: leftEndpoints[1],
                lines: [`${change.label} ${property.label}: ${formatInches(property.leftValue)}`],
                focusSegments: focusSegments.left,
                deltaLabel: focusSegments.left.length ? formatSignedInches(property.delta) : null,
              });
            }
            if (rightEndpoints) {
              info.annotations.right.push({
                start: rightEndpoints[0],
                end: rightEndpoints[1],
                lines: [`${change.label} ${property.label}: ${formatInches(property.rightValue)} (${formatSignedInches(property.delta)})`],
                focusSegments: focusSegments.right,
                deltaLabel: focusSegments.right.length ? formatSignedInches(property.delta) : null,
              });
            }
          });
        });

        const unchangedComponents = componentChanges.filter((change) => !change.changed).slice(0, 2);
        unchangedComponents.forEach((change) => {
          info.summary.push(`${change.label} stayed unchanged.`);
        });
      } else if (info.dimensions.some((item) => item.changed)) {
        info.changedAxes = info.dimensions.filter((item) => item.changed).map((item) => item.axis);
        info.summary.push(`Changed axes: ${info.changedAxes.map((axis) => axis.toUpperCase()).join(", ")}`);
        info.dimensions.filter((dimension) => dimension.changed).forEach((item) => {
          info.summary.push(
            `${item.axis.toUpperCase()} span changed from ${formatLength(item.leftValue)} to ${formatLength(item.rightValue)} (${formatSignedInches(item.delta)})`
          );
          const leftBounds = left?.geometry_hints?.bounds;
          const rightBounds = right?.geometry_hints?.bounds;
          const focusSegments = leftBounds && rightBounds
            ? focusSegmentsForProperty(
                { bounds: leftBounds },
                { bounds: rightBounds },
                { kind: "size", axis: item.axis }
              )
            : { left: [], right: [] };
          if (leftBounds) {
            const leftEndpoints = axisDimensionEndpoints(leftBounds, item.axis);
            info.annotations.left.push({
              start: leftEndpoints[0],
              end: leftEndpoints[1],
              lines: [`${item.axis.toUpperCase()} span: ${formatInches(item.leftValue)}`],
              focusSegments: focusSegments.left,
              deltaLabel: focusSegments.left.length ? formatSignedInches(item.delta) : null,
            });
          }
          if (rightBounds) {
            const rightEndpoints = axisDimensionEndpoints(rightBounds, item.axis);
            info.annotations.right.push({
              start: rightEndpoints[0],
              end: rightEndpoints[1],
              lines: [`${item.axis.toUpperCase()} span: ${formatInches(item.rightValue)} (${formatSignedInches(item.delta)})`],
              focusSegments: focusSegments.right,
              deltaLabel: focusSegments.right.length ? formatSignedInches(item.delta) : null,
            });
          }
        });
      } else if (!structuredDiff.summary.length) {
        info.summary.push("No geometric size change was inferred from the current preview data.");
      }

      if (info.changedShape) {
        info.summary.push(`Shape changed: ${leftShape || "unknown"} -> ${rightShape || "unknown"}`);
      }

      if (structuredDiff.summary.length) {
        info.summary = [...structuredDiff.summary, ...info.summary];
        info.highlightComponents.left.push(...structuredDiff.highlightBodyKeys.left);
        info.highlightComponents.right.push(...structuredDiff.highlightBodyKeys.right);
        info.highlightRawFaces = structuredDiff.highlightRawFaces;
        info.highlightRawEdges = structuredDiff.highlightRawEdges;
        info.annotations.left.push(...structuredDiff.annotations.left);
        info.annotations.right.push(...structuredDiff.annotations.right);
      }

      return info;
    }

    function previewViewerDistance(preview) {
      return Math.max(4, Number(preview?.span || 0) * 2.5);
    }

    function projectModelPoint(point, preview, viewer, scale, canvasEl) {
      return projectPoint(
        rotatePoint(
          [
            point[0] - preview.center[0],
            point[1] - preview.center[1],
            point[2] - preview.center[2]
          ],
          viewer
        ),
        scale,
        canvasEl,
        viewer,
        previewViewerDistance(preview)
      );
    }

    function axisDimensionEndpoints(bounds, axis) {
      const min = bounds.min.map(Number);
      const max = bounds.max.map(Number);
      if (axis === "x") return [[min[0], max[1], max[2]], [max[0], max[1], max[2]]];
      if (axis === "y") return [[max[0], min[1], max[2]], [max[0], max[1], max[2]]];
      return [[max[0], min[1], min[2]], [max[0], min[1], max[2]]];
    }

    function inferFaceSide(face) {
      const name = String(face?.name || "").toLowerCase();
      if (name.includes("-min face") || name.includes("start cap")) return "min";
      if (name.includes("-max face") || name.includes("end cap")) return "max";
      return face?.side || null;
    }

    function normalFromVertices(vertices) {
      if (!Array.isArray(vertices) || vertices.length < 3) return null;
      const anchor = vertices[0];
      for (let index = 1; index < vertices.length - 1; index++) {
        const u = [
          vertices[index][0] - anchor[0],
          vertices[index][1] - anchor[1],
          vertices[index][2] - anchor[2],
        ];
        const v = [
          vertices[index + 1][0] - anchor[0],
          vertices[index + 1][1] - anchor[1],
          vertices[index + 1][2] - anchor[2],
        ];
        const normal = [
          u[1] * v[2] - u[2] * v[1],
          u[2] * v[0] - u[0] * v[2],
          u[0] * v[1] - u[1] * v[0],
        ];
        const magnitude = Math.hypot(normal[0], normal[1], normal[2]);
        if (magnitude > 1e-8) {
          return normal.map((value) => value / magnitude);
        }
      }
      return null;
    }

    function faceMatchesHighlight(face, highlightFaces, highlightComponents, highlightBodyKeys, highlightRawFaces) {
      if (highlightComponents?.length && face.componentId && highlightComponents.includes(face.componentId)) {
        return true;
      }
      if (highlightBodyKeys?.length && face.bodyRawKey && highlightBodyKeys.includes(face.bodyRawKey)) {
        return true;
      }
      if (highlightRawFaces?.length && face.rawFaceKey && highlightRawFaces.includes(face.rawFaceKey)) {
        return true;
      }
      if (!highlightFaces?.length) return false;
      return highlightFaces.some((item) => item.axis === face.axis && item.side === face.side);
    }

    function drawArrow(ctx2d, start, end, color) {
      const dx = end[0] - start[0];
      const dy = end[1] - start[1];
      const length = Math.hypot(dx, dy);
      if (length < 1e-6) return;

      const ux = dx / length;
      const uy = dy / length;
      const head = Math.max(8, Math.min(14, length * 0.12));
      const wing = head * 0.55;

      ctx2d.strokeStyle = color;
      ctx2d.lineWidth = 2;
      ctx2d.beginPath();
      ctx2d.moveTo(start[0], start[1]);
      ctx2d.lineTo(end[0], end[1]);
      ctx2d.stroke();

      ctx2d.fillStyle = color;
      for (const tip of [start, end]) {
        const direction = tip === start ? -1 : 1;
        const backX = tip[0] - ux * head * direction;
        const backY = tip[1] - uy * head * direction;
        ctx2d.beginPath();
        ctx2d.moveTo(tip[0], tip[1]);
        ctx2d.lineTo(backX - uy * wing, backY + ux * wing);
        ctx2d.lineTo(backX + uy * wing, backY - ux * wing);
        ctx2d.closePath();
        ctx2d.fill();
      }
    }

    function drawCompareAnnotations(ctx2d, canvasEl, preview, viewer, scale, compareAnnotation) {
      if (!compareAnnotation?.annotations?.length || !preview) return;
      const color = "#ef6c00";
      const focusColor = "#1e88e5";
      const shadow = "rgba(255,255,255,0.94)";
      const labelStack = [];

      for (const annotation of compareAnnotation.annotations) {
        if (annotation?.point) {
          const point = projectModelPoint(annotation.point, preview, viewer, scale, canvasEl);
          ctx2d.save();
          ctx2d.fillStyle = "rgba(239,108,0,0.18)";
          ctx2d.strokeStyle = color;
          ctx2d.lineWidth = 2;
          ctx2d.beginPath();
          ctx2d.arc(point[0], point[1], 9, 0, Math.PI * 2);
          ctx2d.fill();
          ctx2d.stroke();
          ctx2d.restore();
          labelStack.push(...(annotation.lines || []));
          continue;
        }
        if (!annotation?.start || !annotation?.end) continue;
        const start = projectModelPoint(annotation.start, preview, viewer, scale, canvasEl);
        const end = projectModelPoint(annotation.end, preview, viewer, scale, canvasEl);
        const dx = end[0] - start[0];
        const dy = end[1] - start[1];
        const length = Math.hypot(dx, dy);
        if (length < 18) {
          continue;
        }

        const nx = -dy / length;
        const ny = dx / length;
        const offset = 18;
        const arrowStart = [start[0] + nx * offset, start[1] + ny * offset];
        const arrowEnd = [end[0] + nx * offset, end[1] + ny * offset];
        drawArrow(ctx2d, arrowStart, arrowEnd, color);

        ctx2d.strokeStyle = color;
        ctx2d.lineWidth = 1.5;
        ctx2d.beginPath();
        ctx2d.moveTo(start[0], start[1]);
        ctx2d.lineTo(arrowStart[0], arrowStart[1]);
        ctx2d.moveTo(end[0], end[1]);
        ctx2d.lineTo(arrowEnd[0], arrowEnd[1]);
        ctx2d.stroke();

        if (annotation.focusSegments?.length) {
          for (const segment of annotation.focusSegments) {
            const segmentStart = projectModelPoint(segment.start, preview, viewer, scale, canvasEl);
            const segmentEnd = projectModelPoint(segment.end, preview, viewer, scale, canvasEl);
            const highlightedStart = [segmentStart[0] + nx * offset, segmentStart[1] + ny * offset];
            const highlightedEnd = [segmentEnd[0] + nx * offset, segmentEnd[1] + ny * offset];
            const highlightedLength = Math.hypot(highlightedEnd[0] - highlightedStart[0], highlightedEnd[1] - highlightedStart[1]);
            if (highlightedLength < 8) continue;

            ctx2d.strokeStyle = "rgba(30,136,229,0.24)";
            ctx2d.lineWidth = 10;
            ctx2d.lineCap = "round";
            ctx2d.beginPath();
            ctx2d.moveTo(highlightedStart[0], highlightedStart[1]);
            ctx2d.lineTo(highlightedEnd[0], highlightedEnd[1]);
            ctx2d.stroke();

            ctx2d.strokeStyle = focusColor;
            ctx2d.lineWidth = 4;
            ctx2d.beginPath();
            ctx2d.moveTo(highlightedStart[0], highlightedStart[1]);
            ctx2d.lineTo(highlightedEnd[0], highlightedEnd[1]);
            ctx2d.stroke();
          }

          if (annotation.deltaLabel) {
            const labelX = (arrowStart[0] + arrowEnd[0]) / 2;
            const labelY = (arrowStart[1] + arrowEnd[1]) / 2 - 22;
            ctx2d.save();
            ctx2d.font = "bold 11px 'Segoe UI'";
            ctx2d.textAlign = "center";
            ctx2d.textBaseline = "middle";
            const deltaText = annotation.focusSegments.length > 1
              ? `${annotation.deltaLabel} total`
              : annotation.deltaLabel;
            const horizontalPadding = 8;
            const verticalPadding = 6;
            const deltaWidth = ctx2d.measureText(deltaText).width + horizontalPadding * 2;
            const deltaHeight = 11 + verticalPadding * 2;

            ctx2d.fillStyle = "rgba(255,255,255,0.96)";
            ctx2d.fillRect(labelX - deltaWidth / 2, labelY, deltaWidth, deltaHeight);
            ctx2d.strokeStyle = focusColor;
            ctx2d.lineWidth = 1;
            ctx2d.strokeRect(labelX - deltaWidth / 2, labelY, deltaWidth, deltaHeight);
            ctx2d.fillStyle = focusColor;
            ctx2d.fillText(deltaText, labelX, labelY + deltaHeight / 2);
            ctx2d.restore();
          }

          ctx2d.lineCap = "butt";
        }

        labelStack.push(...(annotation.lines || []));
      }

      if (!labelStack.length) return;

      ctx2d.font = "bold 12px 'Segoe UI'";
      ctx2d.textBaseline = "top";
      const width = Math.max(...labelStack.map((line) => ctx2d.measureText(line).width)) + 18;
      const height = labelStack.length * 16 + 14;
      const x = canvasEl.width - width - 14;
      const y = 14;

      ctx2d.fillStyle = shadow;
      ctx2d.fillRect(x, y, width, height);
      ctx2d.strokeStyle = color;
      ctx2d.lineWidth = 1;
      ctx2d.strokeRect(x, y, width, height);

      ctx2d.fillStyle = color;
      labelStack.forEach((line, index) => {
        ctx2d.fillText(line, x + 9, y + 7 + index * 16);
      });
    }

    function cloneViewerState(viewer) {
      return {
        rotation: (viewer.rotation || defaultRotation()).map((row) => [...row]),
        zoom: viewer.zoom,
        panX: viewer.panX,
        panY: viewer.panY,
        mode: viewer.mode || "solid"
      };
    }

    function presetRotation(name) {
      if (name === "front") return rotationFromYawPitch(0, 0);
      if (name === "top") return rotationFromYawPitch(0, -Math.PI / 2);
      if (name === "right") return rotationFromYawPitch(-Math.PI / 2, 0);
      return defaultRotation();
    }

    function applyViewPreset(viewers, preset) {
      normalizeViewerList(viewers).forEach((viewer) => {
        if (preset === "fit") {
          viewer.zoom = 1;
          viewer.panX = 0;
          viewer.panY = 0;
          return;
        }
        viewer.rotation = presetRotation(preset);
        viewer.zoom = 1;
        viewer.panX = 0;
        viewer.panY = 0;
      });
    }

    function compareInteractionViewers(side) {
      if (!state.compareSync) {
        return [state.compareViewers[side]];
      }
      return side === "left"
        ? [state.compareViewers.left, state.compareViewers.right]
        : [state.compareViewers.right, state.compareViewers.left];
    }

    function normalizeViewerList(viewers) {
      return Array.isArray(viewers) ? viewers : [viewers];
    }

    function bindCanvasInteractions(canvasEl, viewerAccessor, redraw, reset, label) {
      if (!canvasEl) return;
      canvasEl.style.cursor = "grab";
      canvasEl.addEventListener("contextmenu", (event) => event.preventDefault());

      canvasEl.addEventListener("mousedown", (event) => {
        event.preventDefault();
        dragState = {
          x: event.clientX,
          y: event.clientY,
          pan: event.shiftKey || event.button === 1 || event.button === 2,
          viewers: normalizeViewerList(viewerAccessor()),
          redraw,
          canvasEl,
        };
        canvasEl.style.cursor = "grabbing";
        setStatus(`${label}: drag to rotate, Shift + drag to pan.`);
      });

      canvasEl.addEventListener("wheel", (event) => {
        event.preventDefault();
        const viewers = normalizeViewerList(viewerAccessor());
        const factor = event.deltaY > 0 ? 0.9 : 1.1;
        viewers.forEach((viewer) => {
          viewer.zoom = Math.min(10, Math.max(0.2, viewer.zoom * factor));
        });
        redraw();
      }, { passive: false });

      canvasEl.addEventListener("dblclick", () => {
        reset();
        redraw();
        setStatus(`${label} fit to view.`);
      });
    }

    function formatKeyValues(values) {
      const entries = Object.entries(values || {});
      if (!entries.length) return "No data found.";
      const width = Math.max(...entries.map(([key]) => key.length));
      return entries.map(([key, value]) => `${key.padEnd(width)} : ${value}`).join("\\n");
    }

    function setPrimarySelection(key) {
      state.selected = [key];
    }

    function addComparisonSelection(key) {
      state.selected = state.selected.filter((value) => value !== key);
      state.selected.push(key);
      if (state.selected.length > 2) {
        state.selected = state.selected.slice(-2);
      }
    }

    function handleSelectionClick(key, event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey) {
        addComparisonSelection(key);
      } else {
        setPrimarySelection(key);
      }
      if (state.selected.length === 2) {
        state.activeTab = "compare";
      } else if (state.activeTab === "compare") {
        state.activeTab = "overview";
      }
      resetPreviewIfNeeded();
      render();
    }

    function resetPreviewIfNeeded() {
      state.viewer = {
        ...defaultViewerState(),
        mode: state.viewer?.mode || "solid"
      };
      state.compareViewers = {
        left: {
          ...defaultViewerState(),
          mode: "solid"
        },
        right: {
          ...defaultViewerState(),
          mode: "solid"
        }
      };
    }

    function enrichReports(reports) {
      return reports.map((report) => {
        const geometry = report.geometry_hints || {};
        const scalars = geometry.notable_scalar_values || [];
        geometry.notable_scalar_values = scalars.map((item) => ({
          ...item,
          pretty_metric_and_imperial:
            item.pretty_metric_and_imperial ||
            `${item.value} (${item.assuming_meters.millimeters.toFixed(3)} mm, ${item.assuming_meters.inches.toFixed(3)} in)`
        }));
        return report;
      });
    }

    function mergeReports(incoming) {
      const reportMap = new Map(state.reports.map((report) => [reportKey(report), report]));
      for (const report of enrichReports(incoming)) {
        reportMap.set(reportKey(report), report);
      }
      state.reports = [...reportMap.values()];
      if (incoming.length) {
        setPrimarySelection(bestReportKey(incoming));
      }
      resetPreviewIfNeeded();
      render();
    }

    function renderFusionButton() {
      const button = byId("create-fusion");
      if (!button) return;
      button.disabled = !canFuseSelection();
    }

    function renderCompareButton() {
      const button = byId("compare-selected");
      if (!button) return;
      button.disabled = !canCompareSelection();
    }

    function renderTabs() {
      const root = byId("tabs");
      root.innerHTML = "";
      for (const [id, label] of tabs) {
        const tab = document.createElement("div");
        tab.className = "tab" + (state.activeTab === id ? " active" : "");
        tab.textContent = label;
        tab.onclick = () => {
          state.activeTab = id;
          render();
        };
        root.appendChild(tab);
      }

      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.dataset.panel === state.activeTab);
      });
    }

    function renderPreviewModes() {
      const root = byId("preview-modes");
      const modes = [
        ["solid", "Solid"],
        ["wireframe", "Wireframe"],
        ["points", "Points"]
      ];
      root.innerHTML = '<span class="toolbar-label">Display</span>';
      for (const [mode, label] of modes) {
        const button = document.createElement("button");
        button.className = "preview-mode" + (state.viewer.mode === mode ? " active" : "");
        button.textContent = label;
        button.onclick = () => {
          state.viewer.mode = mode;
          renderPreviewModes();
          drawPreview();
        };
        root.appendChild(button);
      }
    }

    function renderPreviewPresets() {
      const root = byId("preview-toolbar-right");
      root.innerHTML = "";

      const viewGroup = document.createElement("div");
      viewGroup.className = "toolbar-cluster";
      viewGroup.innerHTML = '<span class="toolbar-label">Views</span>';
      VIEW_PRESETS.forEach(([preset, label]) => {
        const button = document.createElement("button");
        button.className = "view-preset";
        button.textContent = label;
        button.onclick = () => {
          applyPreviewPreset(preset);
          drawPreview();
          setStatus(`Preview set to ${label.toLowerCase()} view.`);
        };
        viewGroup.appendChild(button);
      });

      const backgroundGroup = document.createElement("div");
      backgroundGroup.className = "toolbar-cluster";
      backgroundGroup.innerHTML = '<span class="toolbar-label">Background</span>';
      VIEWPORT_BACKGROUNDS.forEach(([value, label]) => {
        const button = document.createElement("button");
        button.className = "view-preset" + (state.viewportBackground === value ? " active" : "");
        button.textContent = label;
        button.onclick = () => {
          state.viewportBackground = value;
          renderPreviewPresets();
          if (state.activeTab === "compare") {
            renderCompare();
          } else {
            drawPreview();
          }
          setStatus(`Viewport background set to ${label.toLowerCase()}.`);
        };
        backgroundGroup.appendChild(button);
      });

      root.appendChild(viewGroup);
      root.appendChild(backgroundGroup);
    }

    function renderFileList() {
      const list = byId("file-list");
      list.innerHTML = "";

      if (!state.reports.length) {
        list.innerHTML = '<div class="empty">No reports loaded.</div>';
        byId("sidebar-foot").textContent = "Use the toolbar to load a STEP file or another supported CAD file.";
        return;
      }

      const sorted = [...state.reports].sort((a, b) => a.file_name.localeCompare(b.file_name));
      for (const report of sorted) {
        const key = reportKey(report);
        const item = document.createElement("div");
        item.className = "file-item" + (state.selected.includes(key) ? " selected" : "");
        const bounds = report.geometry_hints?.bounds;
        item.innerHTML = `
          <strong>${escapeHtml(report.file_name)}</strong>
          <span>${escapeHtml(sourceFormatLabel(report))}</span>
          <span>${escapeHtml(report.decoded_names?.join(", ") || "No decoded names")}</span>
          <span>${escapeHtml(bounds ? formatBounds(bounds) : "No bounds inferred")}</span>
        `;
        item.onclick = (event) => handleSelectionClick(key, event);
        list.appendChild(item);
      }

      byId("sidebar-foot").textContent =
        canFuseSelection()
          ? "Two parts are selected. Compare them now, or use Improve JSON With STEP to build a better hybrid reconstruction."
          : state.selected.length === 2
            ? "Two parts selected. The Compare view is ready."
            : `${state.reports.length} part report(s) loaded.`;
    }

    function renderSummary() {
      const report = activeReport();
      byId("part-title").textContent = report ? report.file_name : "No part selected";

      const summary = byId("summary-grid");
      summary.innerHTML = "";

      if (!report) {
        summary.innerHTML = '<div class="muted">Load a file to see part information.</div>';
        return;
      }

      const rows = [
        ["Path", report.file],
        ["Source Format", sourceFormatLabel(report)],
        ["STEP Importer", stepBackendLabel(report)],
        ["Decoded Names", report.decoded_names?.join(", ") || "None"],
        ["Source App", report.header?.PART1?.APPL || "Unknown"],
        ["Date", report.header?.PART1?.DATE || "Unknown"],
        ["Schema", report.header?.PART2?.SCH || "Unknown"],
        ["Kernel Body", kernelLabel(report)],
        ["Inferred Shape", topologyLabel(report)],
        ["Faces / Edges", topologyCounts(report)],
        ["Raw NX Faces / Edges", structuredTopologyLabel(report)],
        ["Density", formatDensity(report)],
        ["Colors", formatColors(report)],
        ["Bounds", formatBounds(report.geometry_hints?.bounds)],
        ["Preview Points", String(report.geometry_hints?.point_count || 0)],
        ["Scale Factor Attribute", report.has_scale_factor_attribute ? "Present" : "Not found"]
      ];

      if (report?.fusion?.enabled) {
        rows.splice(2, 0, ["Fusion", fusionLabel(report)], ["Fusion ML", fusionMlLabel(report)]);
      }

      for (const [label, value] of rows) {
        const row = document.createElement("div");
        row.innerHTML = `<strong>${escapeHtml(label)}:</strong> <span>${escapeHtml(value)}</span>`;
        summary.appendChild(row);
      }
    }

    function renderOverview() {
      const root = byId("overview-panel");
      const report = activeReport();
      if (!report) {
        root.innerHTML = '<div class="empty">Select a part to inspect it.</div>';
        return;
      }

      const rows = [
        ["File", report.file_name],
        ["Path", report.file],
        ["Source Format", sourceFormatLabel(report)],
        ["STEP Importer", stepBackendLabel(report)],
        ["Date", report.header?.PART1?.DATE || "Unknown"],
        ["Decoded Names", report.decoded_names?.join(", ") || "None"],
        ["Kernel Body", kernelLabel(report)],
        ["Inferred Shape", topologyLabel(report)],
        ["Faces / Edges", topologyCounts(report)],
        ["Raw NX Faces / Edges", structuredTopologyLabel(report)],
        ["Density", formatDensity(report)],
        ["Colors", formatColors(report)],
        ["Bounds", formatBounds(report.geometry_hints?.bounds)],
        ["Preview Point Count", String(report.geometry_hints?.point_count || 0)],
        ["Visible Preview Bodies", String(visiblePreviewBodyCount(report) || 0)],
        ["Object State IDs", String(report.object_state_ids?.length || 0)]
      ];

      if (report?.fusion?.enabled) {
        rows.splice(3, 0, ["Fusion", fusionLabel(report)], ["Fusion ML", fusionMlLabel(report)]);
      }

      const bodies = kernelBodies(report);
      const hiddenBodyKeys = new Set(hiddenPreviewBodiesForReport(report));
      const bodyVisibilityHtml = bodies.length > 1
        ? `
          <div class="body-visibility">
            <div><strong>Preview Bodies</strong></div>
            <div class="body-visibility-actions">
              <button type="button" id="show-all-bodies">Show All</button>
              <button type="button" id="hide-all-bodies">Hide All</button>
            </div>
            <div class="body-visibility-list">
              ${bodies.map((body, bodyIndex) => {
                const bodyKey = previewBodyKey(body, bodyIndex);
                const bodyName = escapeHtml(body?.name || body?.metadata?.name || `Body ${bodyIndex + 1}`);
                const checked = hiddenBodyKeys.has(bodyKey) ? "" : "checked";
                const faceCount = Number(body?.faces?.length || 0);
                const edgeCount = Number(body?.edges?.length || 0);
                return `
                  <label class="body-visibility-item">
                    <input type="checkbox" class="body-toggle" data-body-key="${escapeHtml(bodyKey)}" ${checked}>
                    <span>${bodyName}</span>
                    <small>${faceCount} faces / ${edgeCount} edges</small>
                  </label>
                `;
              }).join("")}
            </div>
          </div>
        `
        : "";

      root.innerHTML = `
        <table>
          <thead><tr><th>Field</th><th>Value</th></tr></thead>
          <tbody>${rows.map(([label, value]) => `<tr><td>${escapeHtml(label)}</td><td>${escapeHtml(value)}</td></tr>`).join("")}</tbody>
        </table>
        ${bodyVisibilityHtml}
      `;

      if (bodies.length > 1) {
        byId("show-all-bodies").onclick = () => {
          setHiddenPreviewBodies(report, []);
          renderOverview();
          drawPreview();
          if (state.activeTab === "compare") drawComparePreviews();
        };
        byId("hide-all-bodies").onclick = () => {
          setHiddenPreviewBodies(report, bodies.map((body, bodyIndex) => previewBodyKey(body, bodyIndex)));
          renderOverview();
          drawPreview();
          if (state.activeTab === "compare") drawComparePreviews();
        };
        root.querySelectorAll(".body-toggle").forEach((input) => {
          input.onchange = (event) => {
            togglePreviewBody(report, event.target.dataset.bodyKey, event.target.checked);
          };
        });
      }
    }

    function renderNotes() {
      const panel = byId("notes-panel");
      const report = activeReport();
      if (!report) {
        panel.textContent = "Select a part to inspect notes.";
        return;
      }

      const lines = [
        "Part Summary",
        "",
        `Decoded entity names: ${report.decoded_names?.join(", ") || "None"}`,
        `Kernel body: ${kernelLabel(report)}`,
        `STEP importer: ${stepBackendLabel(report)}`,
        `Inferred shape: ${topologyLabel(report)}`,
        `Faces / edges: ${topologyCounts(report)}`,
        `Material density: ${formatDensity(report)}`,
        `Color swatches: ${formatColors(report)}`,
        `Scale factor attribute: ${report.has_scale_factor_attribute ? "Present" : "Not found"}`,
        `Object state IDs found: ${report.object_state_ids?.length || 0}`,
        `Preview point count: ${report.geometry_hints?.point_count || 0}`,
        "",
        "Notable dimensions and scalar values",
        ""
      ];

      if (report?.fusion?.enabled) {
        lines.push("");
        lines.push("Fusion");
        lines.push("");
        lines.push(`Fusion status: ${fusionLabel(report)}`);
        lines.push(`Fusion ML: ${fusionMlLabel(report)}`);
        for (const line of fusionDecisionLines(report, 6)) {
          lines.push(line);
        }
      }

      const scalars = report.geometry_hints?.notable_scalar_values || [];
      if (scalars.length) {
        for (const item of scalars.slice(0, 8)) {
          lines.push(`- ${item.pretty_metric_and_imperial} [seen ${item.frequency} times]`);
        }
      } else {
        lines.push("No notable repeated scalar values were inferred.");
      }

      if (report.geometry_hints?.unit_inference) {
        lines.push("", "Unit inference", "", report.geometry_hints.unit_inference);
      }

      const modelSummary = report.model_analysis?.summary || [];
      if (modelSummary.length) {
        lines.push("", "Model-based inference", "");
        for (const line of modelSummary) {
          lines.push(`- ${line}`);
        }
      }

      const curveHints = report.model_analysis?.curve_hints || [];
      if (curveHints.length) {
        lines.push("", "Curve/entity hints", "");
        for (const line of curveHints) {
          lines.push(`- ${line}`);
        }
      }

      panel.textContent = lines.join("\\n");
    }

    function renderHeader() {
      const panel = byId("header-panel");
      const report = activeReport();
      if (!report) {
        panel.textContent = "Select a part to inspect header metadata.";
        return;
      }

      const lines = [];
      for (const [name, values] of Object.entries(report.header || {})) {
        lines.push(name, "-".repeat(name.length), formatKeyValues(values), "");
      }
      lines.push("Transmit Info", "-------------", formatKeyValues(report.transmit_info || {}));
      panel.textContent = lines.join("\\n").trim();
    }

    function renderGeometry() {
      const panel = byId("geometry-panel");
      const report = activeReport();
      if (!report) {
        panel.textContent = "Select a part to inspect geometry hints.";
        return;
      }

      const bounds = report.geometry_hints?.bounds;
      const points = report.geometry_hints?.preview_points || [];
      const ids = report.object_state_ids || [];

      panel.textContent = [
        "Bounds",
        "------",
        bounds
          ? `Min  : ${JSON.stringify(bounds.min)}\\nMax  : ${JSON.stringify(bounds.max)}\\nSize : ${JSON.stringify(bounds.size)}`
          : "No reliable bounding box was inferred from the text records in this file.",
        "",
        "Preview Points",
        "--------------",
        points.length ? points.map((point) => JSON.stringify(point)).join("\\n") : "No preview points were extracted.",
        "",
        "Object State IDs",
        "----------------",
        ids.length ? ids.join("\\n") : "None"
      ].join("\\n");
    }

    function renderTopology() {
      const panel = byId("topology-panel");
      const report = activeReport();
      if (!report) {
        panel.textContent = "Select a part to inspect imported or reconstructed faces and edges.";
        return;
      }

      const analysis = report.model_analysis || {};
      const topology = analysis.topology;
      const kernelBodiesList = kernelBodies(report);
      const kernelBody = kernelBodiesList.length === 1 ? kernelBodiesList[0] : null;
      const kernelTopology = report.kernel_topology || null;
      const importedBodies = report.nx_import?.body_summaries || [];
      const levels = analysis.unique_axis_levels || {};
      const lines = [
        "Model Analysis",
        "--------------",
      ];

      if (analysis.summary?.length) {
        for (const line of analysis.summary) {
          lines.push(`- ${line}`);
        }
      } else {
        lines.push("No high-confidence model summary was inferred.");
      }

      lines.push(
        "",
        "Kernel Body",
        "-----------",
        `Name : ${kernelBody?.name || "None"}`,
        `Kind : ${kernelBody?.kind || "None"}`,
        `Primitive : ${kernelBody?.metadata?.primitive || "None"}`,
        `Topology vertices : ${kernelTopology?.vertices?.length || 0}`,
        `Topology edges : ${kernelTopology?.edges?.length || 0}`,
        `Topology loops : ${kernelTopology?.loops?.length || 0}`,
        `Topology faces : ${kernelTopology?.faces?.length || 0}`,
        `Imported bodies : ${kernelBodiesList.length || 0}`,
        "",
        "Axis Levels",
        "-----------",
        `X : ${(levels.x || []).join(", ") || "None"}`,
        `Y : ${(levels.y || []).join(", ") || "None"}`,
        `Z : ${(levels.z || []).join(", ") || "None"}`
      );

      if (importedBodies.length) {
        lines.push("", "Imported Body Summary", "---------------------");
        for (const body of importedBodies) {
          lines.push(`Body ${body.index} | ${body.shape_summary} | ${body.face_count} faces | ${body.edge_count} edges`);
        }
      }

      if (!topology) {
        if (analysis.curve_hints?.length) {
          lines.push("", "Curve / Entity Hints", "--------------------");
          for (const hint of analysis.curve_hints) {
            lines.push(`- ${hint}`);
          }
        }
        if (kernelTopology) {
          lines.push("", "Kernel Topology", "---------------");
          for (const face of kernelTopology.faces || []) {
            lines.push(`${face.id} | ${face.name} | ${face.surface_kind} | loops ${face.loop_ids.join(", ")}`);
          }
        }
        panel.textContent = lines.join("\\n");
        return;
      }

      lines.push(
        "",
        "Topology",
        "--------",
        `Kind : ${topology.kind}`,
        `Shape: ${topology.shape_label}`,
        `Faces: ${topology.face_count}`,
        `Edges: ${topology.edge_count}`,
        `Matched corners: ${topology.matched_corner_count} of ${topology.corner_count}`
      );

      lines.push("", "Faces", "-----");
      for (const face of topology.faces || []) {
        lines.push(
          `${face.name} | ${face.kind} | dims ${face.dimensions.first.model_units} x ${face.dimensions.second.model_units} | area ${face.area.model_units}`
        );
      }

      lines.push("", "Edge Families", "-------------");
      for (const edge of topology.edge_families || []) {
        lines.push(
          `Length ${edge.length.model_units} (${edge.length.millimeters} mm, ${edge.length.inches} in) | count ${edge.count}`
        );
      }

      if (kernelTopology) {
        lines.push("", "Kernel Topology", "---------------");
        for (const edge of kernelTopology.edges || []) {
          lines.push(`${edge.id} | ${edge.kind} | vertices ${edge.vertex_ids.join(" -> ")}`);
        }
        for (const face of kernelTopology.faces || []) {
          lines.push(`${face.id} | ${face.name} | ${face.surface_kind} | loops ${face.loop_ids.join(", ")}`);
        }
      }

      if (analysis.curve_hints?.length) {
        lines.push("", "Curve / Entity Hints", "--------------------");
        for (const hint of analysis.curve_hints) {
          lines.push(`- ${hint}`);
        }
      }

      panel.textContent = lines.join("\\n");
    }

    function renderCompare() {
      const root = byId("compare-panel");
      const reports = comparisonReports();
      if (reports.length !== 2) {
        root.innerHTML = '<div class="empty">Select exactly two parts from the left list to compare them.</div>';
        return;
      }

      const [left, right] = reports;
      const diff = compareDiffInfo(left, right);
      const changeLines = diff.summary.length
        ? diff.summary
        : ["No geometric size change was inferred from the current preview data."];
      const rows = [
        ["Decoded Names", left.decoded_names?.join(", ") || "None", right.decoded_names?.join(", ") || "None"],
        ["Inferred Shape", topologyLabel(left), topologyLabel(right)],
        ["Faces / Edges", topologyCounts(left), topologyCounts(right)],
        ["Raw NX Faces / Edges", structuredTopologyLabel(left), structuredTopologyLabel(right)],
        ["Density", formatDensity(left), formatDensity(right)],
        ["Colors", formatColors(left), formatColors(right)],
        ["Bounds", formatBounds(left.geometry_hints?.bounds), formatBounds(right.geometry_hints?.bounds)],
        ["Preview Points", String(left.geometry_hints?.point_count || 0), String(right.geometry_hints?.point_count || 0)],
        ["Object State IDs", String(left.object_state_ids?.length || 0), String(right.object_state_ids?.length || 0)]
      ];
      const rowMarkup = rows.map(([label, a, b]) => {
        const rowClass = a !== b ? ' class="compare-table-row-changed"' : "";
        return `<tr${rowClass}><td>${escapeHtml(label)}</td><td>${escapeHtml(a)}</td><td>${escapeHtml(b)}</td></tr>`;
      }).join("");
      const changeMarkup = changeLines
        .map((line) => `<div class="compare-change-pill">${escapeHtml(line)}</div>`)
        .join("");

      root.innerHTML = `
        <div class="compare-layout">
          <div class="compare-summary">
            <h3>What Changed</h3>
            <div class="compare-summary-list">
              ${changeLines.map((line) => `<div class="compare-summary-item">${escapeHtml(line)}</div>`).join("")}
            </div>
          </div>
          <div class="compare-controls">
            <label>
              <input type="checkbox" id="compare-sync-toggle" ${state.compareSync ? "checked" : ""}>
              Move both models together
            </label>
            <div class="toolbar-cluster" id="compare-view-presets"></div>
            <div class="toolbar-cluster" id="compare-background-presets"></div>
          </div>
          <div class="compare-preview-grid">
            <div class="compare-preview-card">
              <h3>${escapeHtml(left.file_name)}</h3>
              <div class="canvas-frame">
                <canvas id="compare-left-canvas" class="compare-canvas"></canvas>
              </div>
              <div class="compare-note">Orbit with drag. Pan with Shift + drag or right-drag. Blue callouts stay attached to the changed feature on this model.</div>
            </div>
            <div class="compare-preview-card">
              <h3>${escapeHtml(right.file_name)}</h3>
              <div class="canvas-frame">
                <canvas id="compare-right-canvas" class="compare-canvas"></canvas>
              </div>
              <div class="compare-note">The right-side labels include the delta versus the left model, so feature-level changes read more like a CAD inspection view.</div>
            </div>
          </div>
          <table>
            <thead>
              <tr>
                <th>Field</th>
                <th>${escapeHtml(left.file_name)}</th>
                <th>${escapeHtml(right.file_name)}</th>
              </tr>
            </thead>
            <tbody>
              <tr class="compare-change-row">
                <td>What Changed</td>
                <td colspan="2">
                  <div class="compare-change-cell">${changeMarkup}</div>
                </td>
              </tr>
              ${rowMarkup}
            </tbody>
          </table>
        </div>
      `;

      const presetRoot = byId("compare-view-presets");
      presetRoot.innerHTML = '<span class="toolbar-label">Views</span>';
      VIEW_PRESETS.forEach(([preset, label]) => {
        const button = document.createElement("button");
        button.className = "view-preset";
        button.textContent = label;
        button.onclick = () => {
          const targets = state.compareSync
            ? [state.compareViewers.left, state.compareViewers.right]
            : [state.compareViewers.left, state.compareViewers.right];
          applyViewPreset(targets, preset);
          drawComparePreviews();
          setStatus(`Comparison previews set to ${label.toLowerCase()} view.`);
        };
        presetRoot.appendChild(button);
      });

      const backgroundRoot = byId("compare-background-presets");
      backgroundRoot.innerHTML = '<span class="toolbar-label">Background</span>';
      VIEWPORT_BACKGROUNDS.forEach(([value, label]) => {
        const button = document.createElement("button");
        button.className = "view-preset" + (state.viewportBackground === value ? " active" : "");
        button.textContent = label;
        button.onclick = () => {
          state.viewportBackground = value;
          renderPreviewPresets();
          renderCompare();
          setStatus(`Viewport background set to ${label.toLowerCase()}.`);
        };
        backgroundRoot.appendChild(button);
      });

      byId("compare-sync-toggle").addEventListener("change", (event) => {
        state.compareSync = event.target.checked;
        if (state.compareSync) {
          state.compareViewers.right = cloneViewerState(state.compareViewers.left);
          setStatus("Comparison sync enabled. Moving either model now moves both.");
        } else {
          setStatus("Comparison sync disabled. Each model moves independently.");
        }
        drawComparePreviews();
      });

      bindCanvasInteractions(
        byId("compare-left-canvas"),
        () => compareInteractionViewers("left"),
        drawComparePreviews,
        () => {
          if (state.compareSync) {
            state.compareViewers.left = { ...defaultViewerState(), mode: "solid" };
            state.compareViewers.right = cloneViewerState(state.compareViewers.left);
          } else {
            state.compareViewers.left = { ...defaultViewerState(), mode: "solid" };
          }
        },
        "Left comparison preview"
      );
      bindCanvasInteractions(
        byId("compare-right-canvas"),
        () => compareInteractionViewers("right"),
        drawComparePreviews,
        () => {
          if (state.compareSync) {
            state.compareViewers.right = { ...defaultViewerState(), mode: "solid" };
            state.compareViewers.left = cloneViewerState(state.compareViewers.right);
          } else {
            state.compareViewers.right = { ...defaultViewerState(), mode: "solid" };
          }
        },
        "Right comparison preview"
      );
      drawComparePreviews();
    }

    function drawComparePreviews() {
      const reports = comparisonReports();
      if (reports.length !== 2) return;

      const leftCanvas = byId("compare-left-canvas");
      const rightCanvas = byId("compare-right-canvas");
      if (!leftCanvas || !rightCanvas) return;

      const [left, right] = reports;
      const diff = compareDiffInfo(left, right);
      drawModelPreview(leftCanvas, left, state.compareViewers.left, {
        mode: "solid",
        highlightAxes: diff.changedAxes,
        highlightComponents: diff.highlightComponents.left,
        highlightBodyKeys: diff.highlightComponents.left,
        highlightRawFaces: diff.highlightRawFaces.left,
        highlightRawEdges: diff.highlightRawEdges.left,
        compareAnnotation: {
          role: "left",
          annotations: diff.annotations.left
        }
      });
      drawModelPreview(rightCanvas, right, state.compareViewers.right, {
        mode: "solid",
        highlightAxes: diff.changedAxes,
        highlightComponents: diff.highlightComponents.right,
        highlightBodyKeys: diff.highlightComponents.right,
        highlightRawFaces: diff.highlightRawFaces.right,
        highlightRawEdges: diff.highlightRawEdges.right,
        compareAnnotation: {
          role: "right",
          annotations: diff.annotations.right
        }
      });
    }

    function renderJson() {
      const panel = byId("json-panel");
      const report = activeReport();
      panel.textContent = report ? JSON.stringify(report, null, 2) : "Select a part to inspect its JSON.";
    }

    function resizeCanvasElement(canvasEl) {
      const rect = canvasEl.getBoundingClientRect();
      canvasEl.width = Math.max(320, Math.floor(rect.width));
      canvasEl.height = Math.max(320, Math.floor(rect.height));
    }

    function rotatePoint(point, viewer) {
      return applyRotationMatrix(viewer?.rotation || defaultRotation(), point);
    }

    function projectPoint(point, scale, canvasEl, viewer, viewerDistance = 4) {
      const safeDistance = Math.max(4, Number(viewerDistance) || 4);
      const denominator = Math.max(safeDistance * 0.2, safeDistance + point[2]);
      const depth = safeDistance / denominator;
      return [
        point[0] * scale * depth + canvasEl.width / 2 + viewer.panX,
        -point[1] * scale * depth + canvasEl.height / 2 + viewer.panY,
        depth
      ];
    }

    function drawCornerTriad(canvasEl, ctx2d, viewer) {
      const theme = currentViewportTheme();
      const centerX = 72;
      const centerY = canvasEl.height - 72;
      const triadScale = 34;

      function projectTriad(point) {
        const rotated = rotatePoint(point, viewer);
        return [
          centerX + rotated[0] * triadScale,
          centerY - rotated[1] * triadScale,
          rotated[2]
        ];
      }

      const origin = projectTriad([0, 0, 0]);
      const axes = [
        { point: [1, 0, 0], color: "#c62828", label: "X" },
        { point: [0, 1, 0], color: "#2e7d32", label: "Y" },
        { point: [0, 0, 1], color: "#1565c0", label: "Z" }
      ];

      ctx2d.fillStyle = theme.hudFill;
      ctx2d.strokeStyle = theme.hudStroke;
      ctx2d.lineWidth = 1;
      ctx2d.beginPath();
      ctx2d.rect(16, canvasEl.height - 128, 112, 112);
      ctx2d.fill();
      ctx2d.stroke();

      for (const axis of axes) {
        const end = projectTriad(axis.point);
        ctx2d.strokeStyle = axis.color;
        ctx2d.lineWidth = 2;
        ctx2d.beginPath();
        ctx2d.moveTo(origin[0], origin[1]);
        ctx2d.lineTo(end[0], end[1]);
        ctx2d.stroke();
        ctx2d.fillStyle = axis.color;
        ctx2d.beginPath();
        ctx2d.arc(end[0], end[1], 4, 0, Math.PI * 2);
        ctx2d.fill();
        ctx2d.font = "bold 12px 'Segoe UI'";
        ctx2d.fillText(axis.label, end[0] + 7, end[1] - 4);
      }

      ctx2d.fillStyle = theme.hudText;
      ctx2d.beginPath();
      ctx2d.arc(origin[0], origin[1], 3, 0, Math.PI * 2);
      ctx2d.fill();
    }

    function currentViewportTheme() {
      if (state.viewportBackground === "white") {
        return {
          background: "#ffffff",
          hudFill: "rgba(245, 245, 245, 0.92)",
          hudStroke: "rgba(70, 70, 70, 0.22)",
          hudText: "#333333",
          secondaryText: "#4a4a4a",
          point: "#444444",
          faceStroke: "rgba(58, 62, 70, 0.34)",
          bounds: "rgba(110, 110, 110, 0.5)",
          wireframe: "rgba(70, 70, 70, 0.68)",
          emptyText: "rgba(70, 70, 70, 0.92)",
          surfaceShadow: [76, 80, 88],
          surfaceHighlight: [156, 162, 172],
          surfaceAmbient: 0.36,
        };
      }
      if (state.viewportBackground === "dark") {
        return {
          background: "#1d1d1d",
          hudFill: "rgba(24, 24, 24, 0.72)",
          hudStroke: "rgba(220, 220, 220, 0.15)",
          hudText: "rgba(242, 242, 242, 0.95)",
          secondaryText: "rgba(241, 241, 241, 0.92)",
          point: "rgba(241, 241, 241, 0.86)",
          faceStroke: "rgba(208, 214, 224, 0.18)",
          bounds: "rgba(215, 215, 215, 0.4)",
          wireframe: "rgba(226, 226, 226, 0.6)",
          emptyText: "rgba(241, 241, 241, 0.92)",
          surfaceShadow: [68, 72, 80],
          surfaceHighlight: [146, 152, 164],
          surfaceAmbient: 0.3,
        };
      }
      return {
        background: "#d9d9d9",
        hudFill: "rgba(238, 238, 238, 0.9)",
        hudStroke: "rgba(80, 80, 80, 0.18)",
        hudText: "#323232",
        secondaryText: "#404040",
        point: "#404040",
        faceStroke: "rgba(54, 58, 66, 0.3)",
        bounds: "rgba(95, 95, 95, 0.45)",
        wireframe: "rgba(70, 70, 70, 0.62)",
        emptyText: "rgba(55, 55, 55, 0.92)",
        surfaceShadow: [72, 76, 84],
        surfaceHighlight: [148, 154, 164],
        surfaceAmbient: 0.34,
      };
    }

    function drawViewportBackground(ctx2d, canvasEl) {
      ctx2d.fillStyle = currentViewportTheme().background;
      ctx2d.fillRect(0, 0, canvasEl.width, canvasEl.height);
    }

    function hexToRgb(value) {
      const text = String(value || "").trim();
      if (/^#([0-9a-f]{3})$/i.test(text)) {
        const [, short] = text.match(/^#([0-9a-f]{3})$/i);
        return short.split("").map((char) => parseInt(char + char, 16));
      }
      if (/^#([0-9a-f]{6})$/i.test(text)) {
        const [, full] = text.match(/^#([0-9a-f]{6})$/i);
        return [0, 2, 4].map((index) => parseInt(full.slice(index, index + 2), 16));
      }
      return [200, 200, 200];
    }

    function rgbCss(rgb) {
      if (typeof rgb === "string") return rgb;
      return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
    }

    function previewEdgePalette() {
      if (state.viewportBackground === "dark") {
        return {
          edgeColor: [224, 229, 236],
          pointColor: [241, 241, 241],
        };
      }
      if (state.viewportBackground === "white") {
        return {
          edgeColor: [86, 94, 106],
          pointColor: [48, 55, 65],
        };
      }
      return {
        edgeColor: [78, 84, 94],
        pointColor: [56, 60, 68],
      };
    }

    function previewPresetDirection(preset) {
      const directions = {
        iso: [1.2, 1, 0.85],
        front: [0, 0, 1],
        top: [0, 1, 0],
        right: [1, 0, 0],
      };
      return normalizeVector(directions[preset] || directions.iso);
    }

    function disposeThreeObject(object3d) {
      if (!object3d) return;
      object3d.traverse((child) => {
        if (child.geometry?.dispose) {
          child.geometry.dispose();
        }
        if (Array.isArray(child.material)) {
          child.material.forEach((material) => material?.dispose?.());
        } else if (child.material?.dispose) {
          child.material.dispose();
        }
      });
    }

    function clearPreviewViewportContent(viewport) {
      if (!viewport?.contentGroup) return;
      const children = [...viewport.contentGroup.children];
      for (const child of children) {
        viewport.contentGroup.remove(child);
        disposeThreeObject(child);
      }
      viewport.previewObjects = null;
      viewport.currentPreview = null;
      viewport.currentReportKey = null;
      viewport.currentHiddenKey = "";
    }

    function resizePreviewViewport(viewport) {
      if (!viewport?.renderer || !viewport?.camera) return;
      const rect = canvas.getBoundingClientRect();
      const width = Math.max(320, Math.floor(rect.width));
      const height = Math.max(320, Math.floor(rect.height));
      viewport.renderer.setSize(width, height, false);
      viewport.camera.aspect = width / height;
      viewport.camera.updateProjectionMatrix();
    }

    function setPreviewViewportBackground(viewport) {
      if (!viewport?.renderer || !viewport?.THREE) return;
      const background = VIEWPORT_BACKGROUNDS.find(([value]) => value === state.viewportBackground)?.[2] || "#d9d9d9";
      viewport.renderer.setClearColor(new viewport.THREE.Color(background), 1);
    }

    function renderPreviewViewport(viewport) {
      if (!viewport?.renderer || !viewport?.scene || !viewport?.camera) return;
      resizePreviewViewport(viewport);
      viewport.renderer.render(viewport.scene, viewport.camera);
    }

    function fitPreviewViewport(viewport, preset = "iso") {
      if (!viewport?.camera || !viewport?.controls || !viewport?.currentPreview) return;
      const direction = previewPresetDirection(preset);
      const preview = viewport.currentPreview;
      const radius = Math.max(
        preview?.bounds?.size ? Math.hypot(...preview.bounds.size) * 0.5 : 0,
        preview?.span ? preview.span * 0.6 : 0,
        1e-3
      );
      const fovRadians = (viewport.camera.fov * Math.PI) / 180;
      const distance = Math.max(radius / Math.sin(Math.max(fovRadians / 2, 1e-3)), radius * 1.75);
      viewport.controls.target.set(0, 0, 0);
      viewport.camera.position.set(
        direction[0] * distance,
        direction[1] * distance,
        direction[2] * distance
      );
      viewport.camera.near = Math.max(0.001, distance / 1000);
      viewport.camera.far = Math.max(10, distance + radius * 8);
      viewport.camera.updateProjectionMatrix();
      viewport.controls.update();
      viewport.currentPreset = preset === "fit" ? (viewport.currentPreset || "iso") : preset;
      renderPreviewViewport(viewport);
    }

    function applyPreviewViewportMode(viewport, mode) {
      if (!viewport?.previewObjects) return;
      const { solidMesh, wireframe, pointCloud, edgeLines } = viewport.previewObjects;
      const hasSurfaceGeometry = Boolean(solidMesh || wireframe || edgeLines);
      if (solidMesh) solidMesh.visible = mode === "solid";
      if (pointCloud) pointCloud.visible = mode === "points" || !hasSurfaceGeometry;
      if (wireframe) {
        wireframe.visible = mode !== "points";
        wireframe.material.opacity = mode === "solid" ? 0.22 : 1;
      }
      if (edgeLines) {
        edgeLines.visible = mode !== "points";
        edgeLines.material.opacity = mode === "solid" ? 0.26 : 0.78;
      }
    }

    function buildPreviewViewportObjects(viewport, preview) {
      const { THREE } = viewport;
      const palette = previewEdgePalette();
      const group = new THREE.Group();
      const solidPositions = [];
      const solidNormals = [];
      const solidColors = [];
      const edgePositions = [];
      const pointPositions = [];
      const pointSeen = new Set();

      function pushPoint(position) {
        const centered = [
          Number(position[0]) - preview.center[0],
          Number(position[1]) - preview.center[1],
          Number(position[2]) - preview.center[2],
        ];
        const marker = centered.map((value) => value.toFixed(6)).join("|");
        if (pointSeen.has(marker)) return;
        pointSeen.add(marker);
        pointPositions.push(...centered);
      }

      for (const face of preview.kernelGeometry?.faces || []) {
        const vertices = Array.isArray(face.vertices) ? face.vertices : [];
        if (vertices.length < 3) continue;
        const normal = (face.normal || normalFromVertices(vertices) || [0, 0, 1]).map(Number);
        const baseColor = (face.baseColor || [196, 199, 205]).map((value) => Number(value) / 255);

        vertices.forEach(pushPoint);
        for (let index = 1; index < vertices.length - 1; index++) {
          const triangle = [vertices[0], vertices[index], vertices[index + 1]];
          for (const vertex of triangle) {
            solidPositions.push(
              Number(vertex[0]) - preview.center[0],
              Number(vertex[1]) - preview.center[1],
              Number(vertex[2]) - preview.center[2]
            );
            solidNormals.push(normal[0], normal[1], normal[2]);
            solidColors.push(baseColor[0], baseColor[1], baseColor[2]);
          }
        }
      }

      for (const edge of preview.kernelGeometry?.edges || []) {
        const points = Array.isArray(edge.points) ? edge.points : [];
        for (let index = 1; index < points.length; index++) {
          const start = points[index - 1];
          const end = points[index];
          edgePositions.push(
            Number(start[0]) - preview.center[0],
            Number(start[1]) - preview.center[1],
            Number(start[2]) - preview.center[2],
            Number(end[0]) - preview.center[0],
            Number(end[1]) - preview.center[1],
            Number(end[2]) - preview.center[2]
          );
          pushPoint(start);
          pushPoint(end);
        }
      }

      for (const point of preview.points || []) {
        pushPoint(point);
      }

      let solidMesh = null;
      let wireframe = null;
      let edgeLines = null;
      let pointCloud = null;

      if (solidPositions.length) {
        const solidGeometry = new THREE.BufferGeometry();
        solidGeometry.setAttribute("position", new THREE.Float32BufferAttribute(solidPositions, 3));
        solidGeometry.setAttribute("normal", new THREE.Float32BufferAttribute(solidNormals, 3));
        solidGeometry.setAttribute("color", new THREE.Float32BufferAttribute(solidColors, 3));
        solidMesh = new THREE.Mesh(
          solidGeometry,
          new THREE.MeshStandardMaterial({
            vertexColors: true,
            side: THREE.DoubleSide,
            roughness: 0.58,
            metalness: 0.08,
            transparent: true,
            opacity: 0.98,
            polygonOffset: true,
            polygonOffsetFactor: 1,
            polygonOffsetUnits: 1,
          })
        );
        group.add(solidMesh);

        wireframe = new THREE.LineSegments(
          new THREE.WireframeGeometry(solidGeometry),
          new THREE.LineBasicMaterial({
            color: new THREE.Color(rgbCss(palette.edgeColor)),
            transparent: true,
            opacity: 0.22,
          })
        );
        group.add(wireframe);
      }

      if (edgePositions.length) {
        const edgeGeometry = new THREE.BufferGeometry();
        edgeGeometry.setAttribute("position", new THREE.Float32BufferAttribute(edgePositions, 3));
        edgeLines = new THREE.LineSegments(
          edgeGeometry,
          new THREE.LineBasicMaterial({
            color: new THREE.Color(rgbCss(palette.edgeColor)),
            transparent: true,
            opacity: 0.26,
          })
        );
        group.add(edgeLines);
      }

      if (pointPositions.length) {
        const pointGeometry = new THREE.BufferGeometry();
        pointGeometry.setAttribute("position", new THREE.Float32BufferAttribute(pointPositions, 3));
        pointCloud = new THREE.Points(
          pointGeometry,
          new THREE.PointsMaterial({
            color: new THREE.Color(rgbCss(palette.pointColor)),
            size: Math.max((preview.span || 0.1) * 0.018, 0.0015),
            sizeAttenuation: true,
          })
        );
        group.add(pointCloud);
      }

      return { group, solidMesh, wireframe, edgeLines, pointCloud };
    }

    async function ensurePreviewViewport() {
      if (previewViewportDisabled) return null;
      if (previewViewport) return previewViewport;
      if (!previewViewportPromise) {
        setPreviewOverlay("Loading interactive 3D preview...");
        previewViewportPromise = Promise.all([
          import(THREE_MODULE_URL),
          import(ORBIT_CONTROLS_MODULE_URL),
        ]).then(([THREE, controlsModule]) => {
          const renderer = new THREE.WebGLRenderer({
            canvas,
            antialias: true,
            alpha: false,
            powerPreference: "high-performance",
          });
          if ("outputColorSpace" in renderer && THREE.SRGBColorSpace) {
            renderer.outputColorSpace = THREE.SRGBColorSpace;
          }
          renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

          const scene = new THREE.Scene();
          const camera = new THREE.PerspectiveCamera(45, 1, 0.001, 1000);
          const controls = new controlsModule.OrbitControls(camera, canvas);
          controls.enableDamping = false;
          controls.screenSpacePanning = true;
          controls.addEventListener("change", () => renderPreviewViewport(previewViewport));

          const hemiLight = new THREE.HemisphereLight(0xffffff, 0x4f5660, 1.18);
          scene.add(hemiLight);

          const keyLight = new THREE.DirectionalLight(0xffffff, 1.25);
          keyLight.position.set(2.2, 3.1, 4.4);
          scene.add(keyLight);

          const fillLight = new THREE.DirectionalLight(0xffffff, 0.48);
          fillLight.position.set(-2.1, -1.8, -3.4);
          scene.add(fillLight);

          const contentGroup = new THREE.Group();
          scene.add(contentGroup);

          canvas.style.cursor = "grab";
          canvas.addEventListener("contextmenu", (event) => event.preventDefault());
          controls.addEventListener("start", () => {
            canvas.style.cursor = "grabbing";
          });
          controls.addEventListener("end", () => {
            canvas.style.cursor = "grab";
          });
          canvas.addEventListener("dblclick", () => {
            fitPreviewViewport(previewViewport, "fit");
            setStatus("Preview fit to view.");
          });

          previewViewport = {
            THREE,
            renderer,
            scene,
            camera,
            controls,
            contentGroup,
            previewObjects: null,
            currentPreview: null,
            currentPreset: "iso",
            currentReportKey: null,
            currentHiddenKey: "",
          };

          setPreviewViewportBackground(previewViewport);
          resizePreviewViewport(previewViewport);
          setPreviewOverlay("");
          return previewViewport;
        }).catch((error) => {
          previewViewportDisabled = true;
          previewViewportFailure = error.message;
          setPreviewOverlay("");
          return null;
        });
      }
      return previewViewportPromise;
    }

    function ensurePreviewCanvasFallbackInteractions() {
      if (previewCanvasFallbackBound) return;
      bindCanvasInteractions(
        canvas,
        () => state.viewer,
        drawPreview,
        () => {
          state.viewer = {
            ...defaultViewerState(),
            mode: state.viewer?.mode || "solid"
          };
        },
        "Preview"
      );
      previewCanvasFallbackBound = true;
    }

    function drawPreviewCanvasFallback() {
      ensurePreviewCanvasFallbackInteractions();
      drawModelPreview(canvas, activeReport(), state.viewer, {
        mode: state.viewer.mode
      });
    }

    function rebuildPreviewViewport(viewport, report, options = {}) {
      clearPreviewViewportContent(viewport);
      if (!report) {
        setPreviewOverlay("Load a STEP or JSON file to preview it here.");
        renderPreviewViewport(viewport);
        return;
      }

      const preview = getPreviewData(report);
      if (!preview) {
        const names = report?.decoded_names?.length ? `\nNamed entities: ${report.decoded_names.join(", ")}` : "";
        setPreviewOverlay(`No previewable solid geometry was decoded from this file.${names}`);
        renderPreviewViewport(viewport);
        return;
      }

      const objects = buildPreviewViewportObjects(viewport, preview);
      viewport.contentGroup.add(objects.group);
      viewport.previewObjects = objects;
      viewport.currentPreview = preview;
      viewport.currentReportKey = reportKey(report);
      viewport.currentHiddenKey = JSON.stringify(hiddenPreviewBodiesForReport(report));
      setPreviewOverlay("");
      applyPreviewViewportMode(viewport, state.viewer.mode);
      if (options.fitView) {
        fitPreviewViewport(viewport, viewport.currentPreset || "iso");
      }
      renderPreviewViewport(viewport);
    }

    function drawPreviewWebGl() {
      ensurePreviewViewport()
        .then((viewport) => {
          if (!viewport) {
            if (previewViewportFailure) {
              setStatus(`Preview compatibility mode: ${previewViewportFailure}`);
            }
            drawPreviewCanvasFallback();
            return;
          }
          const report = activeReport();
          const nextReportKey = report ? reportKey(report) : null;
          const nextHiddenKey = report ? JSON.stringify(hiddenPreviewBodiesForReport(report)) : "";
          const fitView = nextReportKey !== viewport.currentReportKey;
          setPreviewViewportBackground(viewport);
          if (fitView || nextHiddenKey !== viewport.currentHiddenKey || !viewport.previewObjects) {
            rebuildPreviewViewport(viewport, report, { fitView });
            return;
          }
          applyPreviewViewportMode(viewport, state.viewer.mode);
          setPreviewOverlay("");
          renderPreviewViewport(viewport);
        })
        .catch((error) => {
          setStatus(`Preview error: ${error.message}`);
          drawPreviewCanvasFallback();
        });
    }

    function applyPreviewPreset(preset) {
      if (previewViewportDisabled) {
        applyViewPreset(state.viewer, preset);
        return;
      }
      ensurePreviewViewport()
        .then((viewport) => {
          if (!viewport) {
            applyViewPreset(state.viewer, preset);
            return;
          }
          if (!viewport.currentPreview) return;
          if (preset !== "fit") {
            viewport.currentPreset = preset;
          }
          fitPreviewViewport(viewport, preset === "fit" ? (viewport.currentPreset || "iso") : preset);
        })
        .catch((error) => setStatus(`Preview error: ${error.message}`));
    }

    function drawViewportHud(ctx2d, canvasEl, report, preview, viewer, options = {}) {
      const theme = currentViewportTheme();
      const lines = [
        report?.file_name || "Preview",
        `${capitalizeLabel(options.mode || viewer.mode || "solid")} view`,
      ];
      if (preview?.bounds?.size) {
        lines.push(`Size ${preview.bounds.size.map((value) => formatInches(value)).join(" x ")}`);
      }
      if (options.highlightComponents?.length) {
        lines.push(`${options.highlightComponents.length} feature${options.highlightComponents.length === 1 ? "" : "s"} changed`);
      }

      ctx2d.save();
      ctx2d.font = "600 12px 'Segoe UI'";
      ctx2d.textBaseline = "top";
      const width = Math.max(...lines.map((line) => ctx2d.measureText(line).width)) + 24;
      const height = lines.length * 16 + 16;
      ctx2d.fillStyle = theme.hudFill;
      ctx2d.strokeStyle = theme.hudStroke;
      ctx2d.lineWidth = 1;
      ctx2d.beginPath();
      ctx2d.roundRect(16, 16, width, height, 12);
      ctx2d.fill();
      ctx2d.stroke();
      ctx2d.fillStyle = theme.hudText;
      lines.forEach((line, index) => {
        ctx2d.fillText(line, 28, 24 + index * 16);
      });
      ctx2d.restore();
    }

    function axisVector(axis) {
      if (axis === "x") return [1, 0, 0];
      if (axis === "y") return [0, 1, 0];
      return [0, 0, 1];
    }

    function orthogonalBasis(axis) {
      if (axis === "x") return [[0, 1, 0], [0, 0, 1]];
      if (axis === "y") return [[1, 0, 0], [0, 0, 1]];
      return [[1, 0, 0], [0, 1, 0]];
    }

    function addVec(a, b) {
      return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
    }

    function scaleVec(v, factor) {
      return [v[0] * factor, v[1] * factor, v[2] * factor];
    }

    function circlePoints(center, axis, radius, segmentCount, offsetAlongAxis = 0) {
      const axisVec = axisVector(axis);
      const [u, v] = orthogonalBasis(axis);
      const shiftedCenter = addVec(center, scaleVec(axisVec, offsetAlongAxis));
      const points = [];
      for (let i = 0; i < segmentCount; i++) {
        const angle = (i / segmentCount) * Math.PI * 2;
        const radial = addVec(scaleVec(u, Math.cos(angle) * radius), scaleVec(v, Math.sin(angle) * radius));
        points.push(addVec(shiftedCenter, radial));
      }
      return points;
    }

    function pointExtents(points) {
      if (!Array.isArray(points) || !points.length) return null;

      const first = points[0].map(Number);
      const min = [...first];
      const max = [...first];

      for (const point of points.slice(1)) {
        for (let index = 0; index < 3; index++) {
          const value = Number(point[index]);
          if (value < min[index]) min[index] = value;
          if (value > max[index]) max[index] = value;
        }
      }

      return { min, max };
    }

    function boundsFromPoints(points) {
      const extents = pointExtents(points);
      if (!extents) return null;
      const { min, max } = extents;
      return {
        min,
        max,
        size: [max[0] - min[0], max[1] - min[1], max[2] - min[2]]
      };
    }

    function previewGeometryFromKernelBody(body, context = {}) {
      if (!body) return { faces: [], points: [], edges: [] };

      const bodyKey = previewBodyKey(body, context.bodyIndex || 0);
      if (context.hiddenBodyKeys?.has(bodyKey)) {
        return { faces: [], points: [], edges: [] };
      }

      const bodyDisplay = body?.metadata?.display_properties || null;
      const componentDisplay = body?.metadata?.component_context?.display_properties || null;
      if (isBlankedFromDisplay(bodyDisplay) || isBlankedFromDisplay(componentDisplay)) {
        return { faces: [], points: [], edges: [] };
      }

      if (body.metadata?.primitive === "compound" && Array.isArray(body.metadata?.components)) {
        return body.metadata.components
          .map((component, componentIndex) =>
            previewGeometryFromKernelBody(component, {
              bodyIndex: context.bodyIndex || 0,
              componentIndex,
            })
          )
          .reduce(
            (combined, geometry) => ({
              faces: combined.faces.concat(geometry.faces || []),
              points: combined.points.concat(geometry.points || []),
              edges: combined.edges.concat(geometry.edges || []),
            }),
            { faces: [], points: [], edges: [] }
          );
      }

      const identity = componentIdentity(
        body,
        context.bodyIndex || 0,
        context.componentIndex || 0
      );
      const componentId = identity.id;
      const bodyRawKey = rawIdentityKey(
        body?.metadata?.object_identity,
        `body:${Number(body?.metadata?.nx_body_index || context.bodyIndex || 0)}`
      );
      const componentName = componentLabel(body, identity, 1);
      const baseColor = displayColorFromProperties(
        bodyDisplay,
        displayColorFromProperties(
          componentDisplay,
          inferredComponentColor(body, componentName, identity)
        )
      );
      const baseOpacity = displayOpacityFromProperties(
        bodyDisplay,
        displayOpacityFromProperties(componentDisplay, 1)
      );

      if (body.faces?.some((face) => face.vertices?.length)) {
        const faces = body.faces
          .filter((face) => face.vertices?.length && !isBlankedFromDisplay(face.metadata?.display_properties))
          .map((face) => ({
            name: face.name,
            axis: face.metadata?.axis || null,
            side: inferFaceSide(face),
            normal: face.normal || null,
            vertices: face.vertices.map((vertex) => vertex.map(Number)),
            componentId,
            bodyRawKey,
            rawFaceKey: rawIdentityKey(
              face.metadata?.object_identity,
              `body:${Number(face.metadata?.source_body_index || body?.metadata?.nx_body_index || context.bodyIndex || 0)}:face:${Number(face.metadata?.source_face_index || 0)}`
            ),
            componentName,
            // Face-level NX color indices often vary across analytic/trim patches.
            // Keep the preview visually coherent by preferring the body/component color.
            baseColor,
            opacity: displayOpacityFromProperties(face.metadata?.display_properties, baseOpacity),
          }));
        const edges = (body.edges || [])
          .filter((edge) => edge.points?.length >= 2 && !isBlankedFromDisplay(edge.display_properties))
          .map((edge) => ({
            kind: edge.kind || "edge",
            axis: edge.axis || null,
            points: edge.points.map((point) => point.map(Number)),
            componentId,
            bodyRawKey,
            rawEdgeKey: rawIdentityKey(
              edge.object_identity,
              `body:${Number(body?.metadata?.nx_body_index || context.bodyIndex || 0)}:edge:${Number(edge.source_edge_index || 0)}`
            ),
            componentName,
            strokeColor: displayColorFromProperties(edge.display_properties, baseColor),
            opacity: displayOpacityFromProperties(edge.display_properties, 1),
          }));
        const points = faces.flatMap((face) => face.vertices);
        return { faces, points, edges };
      }

      if (body.metadata?.primitive === "cylinder") {
        const axis = body.metadata.axis;
        const radius = Number(body.metadata.radius);
        const height = Number(body.metadata.height);
        const center = body.metadata.center.map(Number);
        const segmentCount = 28;
        const axisVec = axisVector(axis);
        const startCenter = addVec(center, scaleVec(axisVec, -height / 2));
        const endCenter = addVec(center, scaleVec(axisVec, height / 2));
        const startRing = circlePoints(startCenter, axis, radius, segmentCount, 0);
        const endRing = circlePoints(endCenter, axis, radius, segmentCount, 0);
        const faces = [];

        for (let i = 0; i < segmentCount; i++) {
          const next = (i + 1) % segmentCount;
          faces.push({
            name: `Cylinder side ${i}`,
            axis,
            normal: null,
            vertices: [startRing[i], startRing[next], endRing[next], endRing[i]],
            componentId,
            bodyRawKey,
            componentName,
            baseColor,
            opacity: baseOpacity,
          });
        }

        faces.push({
          name: "Start cap",
          axis,
          side: "min",
          normal: scaleVec(axisVec, -1),
          vertices: [...startRing].reverse(),
          componentId,
          bodyRawKey,
          componentName,
          baseColor,
          opacity: baseOpacity,
        });
        faces.push({
          name: "End cap",
          axis,
          side: "max",
          normal: axisVec,
          vertices: endRing,
          componentId,
          bodyRawKey,
          componentName,
          baseColor,
          opacity: baseOpacity,
        });

        return {
          faces,
          points: [...startRing, ...endRing],
          edges: []
        };
      }

      if (body.metadata?.primitive === "sphere") {
        const center = body.metadata.center.map(Number);
        const radius = Number(body.metadata.radius);
        const latSegments = 10;
        const lonSegments = 20;
        const faces = [];
        const points = [];

        function spherePoint(phi, theta) {
          return [
            center[0] + radius * Math.sin(phi) * Math.cos(theta),
            center[1] + radius * Math.sin(phi) * Math.sin(theta),
            center[2] + radius * Math.cos(phi)
          ];
        }

        for (let lat = 0; lat < latSegments; lat++) {
          const phi1 = (lat / latSegments) * Math.PI;
          const phi2 = ((lat + 1) / latSegments) * Math.PI;
          for (let lon = 0; lon < lonSegments; lon++) {
            const theta1 = (lon / lonSegments) * Math.PI * 2;
            const theta2 = ((lon + 1) / lonSegments) * Math.PI * 2;
            const quad = [
              spherePoint(phi1, theta1),
              spherePoint(phi1, theta2),
              spherePoint(phi2, theta2),
              spherePoint(phi2, theta1)
            ];
            faces.push({
              name: `Sphere patch ${lat}-${lon}`,
              axis: null,
              normal: null,
              vertices: quad,
              componentId,
              bodyRawKey,
              componentName,
              baseColor,
              opacity: baseOpacity,
            });
            points.push(...quad);
          }
        }

        return { faces, points, edges: [] };
      }

      if (body.metadata?.primitive === "arc_or_circle") {
        const edge = body.edges?.[0];
        if (!edge) return { faces: [], points: [], edges: [] };
        const center = edge.center.map(Number);
        const radius = Number(edge.radius);
        const normal = edge.normal?.map(Number) || [0, 0, 1];
        const segmentCount = 48;
        const axis = Math.abs(normal[2]) >= Math.abs(normal[0]) && Math.abs(normal[2]) >= Math.abs(normal[1])
          ? "z"
          : (Math.abs(normal[1]) > Math.abs(normal[0]) ? "y" : "x");
        const points = circlePoints(center, axis, radius, segmentCount, 0);
        const edges = [];
        for (let i = 0; i < points.length; i++) {
          edges.push({
            kind: "arc_segment",
            axis,
            points: [points[i], points[(i + 1) % points.length]],
            componentId,
            bodyRawKey,
            componentName,
            strokeColor: baseColor,
            opacity: 1,
          });
        }
        return { faces: [], points, edges };
      }

      if (body.metadata?.primitive === "wireframe" || body.edges?.some((edge) => edge.points?.length >= 2)) {
        const edges = (body.edges || [])
          .filter((edge) => edge.points?.length >= 2 && !isBlankedFromDisplay(edge.display_properties))
          .map((edge) => ({
            kind: edge.kind || "edge",
            axis: edge.axis || null,
            points: edge.points.map((point) => point.map(Number)),
            componentId,
            bodyRawKey,
            rawEdgeKey: rawIdentityKey(
              edge.object_identity,
              `body:${Number(body?.metadata?.nx_body_index || context.bodyIndex || 0)}:edge:${Number(edge.source_edge_index || 0)}`
            ),
            componentName,
            strokeColor: displayColorFromProperties(edge.display_properties, baseColor),
            opacity: displayOpacityFromProperties(edge.display_properties, 1),
          }));
        const points = [
          ...(body.vertices || []).map((point) => point.map(Number)),
          ...edges.flatMap((edge) => edge.points),
        ];
        return { faces: [], points, edges };
      }

      return { faces: [], points: [], edges: [] };
    }

    function simplifyFacesForPreview(faces) {
      if (!Array.isArray(faces) || !faces.length) return [];

      const preserved = [];
      const reducible = [];
      for (const face of faces) {
        const kind = String(face?.surface?.kind || face?.metadata?.kind || "").toLowerCase();
        const isTriangleMesh = kind === "occ_triangle_mesh" || kind === "facet_triangle";
        if (isTriangleMesh && Array.isArray(face.vertices) && face.vertices.length === 3) {
          reducible.push(face);
        } else {
          preserved.push(face);
        }
      }

      const totalLimit = Math.max(400, MAX_RENDER_FACES - preserved.length);
      const occLimit = Math.max(300, Math.min(MAX_RENDER_OCC_FACES, totalLimit));
      if (reducible.length <= occLimit && (preserved.length + reducible.length) <= MAX_RENDER_FACES) {
        return faces;
      }

      const target = Math.max(1, Math.min(occLimit, MAX_RENDER_FACES - preserved.length));
      const sampled = [];
      const step = reducible.length / target;
      for (let index = 0; index < target; index++) {
        sampled.push(reducible[Math.min(reducible.length - 1, Math.floor(index * step))]);
      }
      return preserved.concat(sampled);
    }

    function simplifyKernelGeometryForPreview(kernelGeometry) {
      const originalFaces = kernelGeometry?.faces || [];
      const faces = simplifyFacesForPreview(originalFaces);
      return {
        ...kernelGeometry,
        faces,
        renderStats: {
          originalFaceCount: originalFaces.length,
          renderedFaceCount: faces.length,
          faceReductionApplied: faces.length < originalFaces.length,
        }
      };
    }

    function getPreviewData(report) {
      if (!report) return null;
      const bodies = kernelBodies(report);
      const points = (!bodies.length ? (report.geometry_hints?.preview_points || []) : []).map((point) => point.map(Number));
      const hiddenBodyKeys = new Set(hiddenPreviewBodiesForReport(report));
      const kernelGeometry = bodies
        .map((body, bodyIndex) => previewGeometryFromKernelBody(body, { bodyIndex, hiddenBodyKeys }))
        .reduce(
          (combined, geometry) => ({
            faces: combined.faces.concat(geometry.faces || []),
            points: combined.points.concat(geometry.points || []),
            edges: combined.edges.concat(geometry.edges || []),
          }),
          { faces: [], points: [], edges: [] }
        );
      const simplifiedKernelGeometry = simplifyKernelGeometryForPreview(kernelGeometry);
      const cloud = [...points, ...kernelGeometry.points];
      const kernelBody = bodies.length === 1 ? bodies[0] : null;
      let bounds = report.geometry_hints?.bounds
        ? {
            min: report.geometry_hints.bounds.min.map(Number),
            max: report.geometry_hints.bounds.max.map(Number),
            size: report.geometry_hints.bounds.size.map(Number)
          }
        : null;

      if (!bounds && kernelGeometry.points.length) {
        bounds = boundsFromPoints(kernelGeometry.points);
      }

      if (bounds) {
        const min = bounds.min;
        const max = bounds.max;
        cloud.push(
          [min[0], min[1], min[2]],
          [max[0], min[1], min[2]],
          [max[0], max[1], min[2]],
          [min[0], max[1], min[2]],
          [min[0], min[1], max[2]],
          [max[0], min[1], max[2]],
          [max[0], max[1], max[2]],
          [min[0], max[1], max[2]]
        );
      }

      if (!cloud.length) return null;

      const extents = pointExtents(cloud);
      if (!extents) return null;
      const { min, max } = extents;
      const center = [
        (min[0] + max[0]) / 2,
        (min[1] + max[1]) / 2,
        (min[2] + max[2]) / 2
      ];
      const span = Math.max(
        max[0] - min[0],
        max[1] - min[1],
        max[2] - min[2],
        1e-6
      );

      return {
        points,
        bounds,
        kernelBody,
        kernelGeometry: simplifiedKernelGeometry,
        renderStats: simplifiedKernelGeometry.renderStats,
        center,
        span
      };
    }

    function shadeRgbForNormal(normal, theme, baseColor = null) {
      const light = [0.38, -0.28, 0.88];
      const len = Math.hypot(...light) || 1;
      const unitLight = light.map((value) => value / len);
      const nLen = Math.hypot(normal[0], normal[1], normal[2]) || 1;
      const unitNormal = normal.map((value) => value / nLen);
      const diffuse = Math.max(0, unitNormal[0] * unitLight[0] + unitNormal[1] * unitLight[1] + unitNormal[2] * unitLight[2]);
      const ambient = theme?.surfaceAmbient ?? 0.38;
      const mix = Math.min(1, ambient + diffuse * (1 - ambient));
      const shadow = Array.isArray(baseColor)
        ? baseColor.map((value) => Math.max(0, Math.round(value * 0.48)))
        : (theme?.surfaceShadow ?? [160, 168, 180]);
      const highlight = Array.isArray(baseColor)
        ? baseColor.map((value) => Math.min(255, Math.round(value * 1.06 + 22)))
        : (theme?.surfaceHighlight ?? [232, 236, 242]);
      return shadow.map((value, index) => {
        return Math.round(value + (highlight[index] - value) * mix);
      });
    }

    function shadeForNormal(normal, theme) {
      return rgbCss(shadeRgbForNormal(normal, theme));
    }

    function edgeFunction(ax, ay, bx, by, px, py) {
      return (px - ax) * (by - ay) - (py - ay) * (bx - ax);
    }

    function triangulateProjectedFace(face) {
      if (!face?.projectedVertices || face.projectedVertices.length < 3) return [];
      const triangles = [];
      for (let index = 1; index < face.projectedVertices.length - 1; index++) {
        triangles.push([
          face.projectedVertices[0],
          face.projectedVertices[index],
          face.projectedVertices[index + 1],
        ]);
      }
      return triangles;
    }

    function rasterizeSolidFaces(ctx2d, canvasEl, faces, theme) {
      if (!faces.length) return;
      const width = canvasEl.width;
      const height = canvasEl.height;
      const imageData = ctx2d.getImageData(0, 0, width, height);
      const pixels = imageData.data;
      const depthBuffer = new Float32Array(width * height);
      depthBuffer.fill(-Infinity);

      for (const face of faces) {
        const fillRgb = face.highlighted
          ? [255, 191, 102]
          : shadeRgbForNormal(face.rotatedNormal || [0, 0, -1], theme, face.baseColor);
        const fillAlpha = Math.max(0.18, Math.min(1, Number(face.opacity ?? 1)));

        for (const triangle of triangulateProjectedFace(face)) {
          const [a, b, c] = triangle;
          const area = edgeFunction(a[0], a[1], b[0], b[1], c[0], c[1]);
          if (Math.abs(area) <= 1e-5) continue;

          const minX = Math.max(0, Math.floor(Math.min(a[0], b[0], c[0])));
          const maxX = Math.min(width - 1, Math.ceil(Math.max(a[0], b[0], c[0])));
          const minY = Math.max(0, Math.floor(Math.min(a[1], b[1], c[1])));
          const maxY = Math.min(height - 1, Math.ceil(Math.max(a[1], b[1], c[1])));

          for (let y = minY; y <= maxY; y++) {
            for (let x = minX; x <= maxX; x++) {
              const px = x + 0.5;
              const py = y + 0.5;
              const w0 = edgeFunction(b[0], b[1], c[0], c[1], px, py);
              const w1 = edgeFunction(c[0], c[1], a[0], a[1], px, py);
              const w2 = edgeFunction(a[0], a[1], b[0], b[1], px, py);

              const inside = area < 0
                ? (w0 <= 0 && w1 <= 0 && w2 <= 0)
                : (w0 >= 0 && w1 >= 0 && w2 >= 0);
              if (!inside) continue;

              const depth = (w0 * a[2] + w1 * b[2] + w2 * c[2]) / area;
              const offset = (y * width + x);
              if (depth <= depthBuffer[offset]) continue;
              depthBuffer[offset] = depth;

              const pixelOffset = offset * 4;
              const existingAlpha = pixels[pixelOffset + 3] / 255;
              const blendedAlpha = fillAlpha + existingAlpha * (1 - fillAlpha);
              const existingWeight = blendedAlpha > 0 ? (existingAlpha * (1 - fillAlpha)) / blendedAlpha : 0;
              const fillWeight = blendedAlpha > 0 ? fillAlpha / blendedAlpha : 0;
              pixels[pixelOffset] = Math.round((pixels[pixelOffset] * existingWeight) + (fillRgb[0] * fillWeight));
              pixels[pixelOffset + 1] = Math.round((pixels[pixelOffset + 1] * existingWeight) + (fillRgb[1] * fillWeight));
              pixels[pixelOffset + 2] = Math.round((pixels[pixelOffset + 2] * existingWeight) + (fillRgb[2] * fillWeight));
              pixels[pixelOffset + 3] = Math.round(blendedAlpha * 255);
            }
          }
        }
      }

      ctx2d.putImageData(imageData, 0, 0);
    }

    function paintSolidFacesNative(ctx2d, faces, theme) {
      for (const face of faces) {
        if (!face.projectedVertices?.length) continue;
        const fillRgb = face.highlighted
          ? [255, 191, 102]
          : shadeRgbForNormal(face.rotatedNormal || [0, 0, -1], theme, face.baseColor);
        ctx2d.fillStyle = rgbCss(fillRgb);
        ctx2d.globalAlpha = Math.max(0.2, Math.min(1, Number(face.opacity ?? 1)));
        ctx2d.beginPath();
        ctx2d.moveTo(face.projectedVertices[0][0], face.projectedVertices[0][1]);
        for (const vertex of face.projectedVertices.slice(1)) {
          ctx2d.lineTo(vertex[0], vertex[1]);
        }
        ctx2d.closePath();
        ctx2d.fill();
      }
      ctx2d.globalAlpha = 1;
    }

    function drawModelPreview(canvasEl, report, viewer, options = {}) {
      const ctx2d = canvasEl.getContext("2d");
      const theme = currentViewportTheme();
      const highlightAxes = options.highlightAxes || [];
      const highlightFaces = options.highlightFaces || [];
      const highlightComponents = options.highlightComponents || [];
      const highlightBodyKeys = options.highlightBodyKeys || [];
      const highlightRawFaces = options.highlightRawFaces || [];
      const highlightRawEdges = options.highlightRawEdges || [];
      const compareAnnotation = options.compareAnnotation || null;
      const forceMode = options.mode || viewer.mode || "solid";

      resizeCanvasElement(canvasEl);
      ctx2d.clearRect(0, 0, canvasEl.width, canvasEl.height);
      drawViewportBackground(ctx2d, canvasEl);

      const preview = getPreviewData(report);
      if (!preview) {
        ctx2d.fillStyle = theme.emptyText;
        ctx2d.font = "14px 'Segoe UI'";
        ctx2d.fillText("No previewable solid geometry was decoded from this file.", 16, 24);
        if (report?.decoded_names?.length) {
          ctx2d.fillText(`Named entities: ${report.decoded_names.join(", ")}`, 16, 48);
        }
        ctx2d.fillText("This viewport renders the app's imported/custom-kernel geometry from the available B-rep data.", 16, 72);
        drawCornerTriad(canvasEl, ctx2d, viewer);
        return;
      }

      const scale = (Math.min(canvasEl.width, canvasEl.height) * 0.32 / preview.span) * viewer.zoom;
      const viewerDistance = previewViewerDistance(preview);
      const centeredPoints = preview.points.map((point) => [
        point[0] - preview.center[0],
        point[1] - preview.center[1],
        point[2] - preview.center[2]
      ]);

      const projected = centeredPoints.map((point) =>
        projectPoint(rotatePoint(point, viewer), scale, canvasEl, viewer, viewerDistance)
      );

      const projectedFaces = (preview.kernelGeometry?.faces || [])
        .filter((face) => face?.vertices?.length >= 3)
        .map((face) => {
        const rotatedVertices = face.vertices.map((point) => {
          return rotatePoint([
            point[0] - preview.center[0],
            point[1] - preview.center[1],
            point[2] - preview.center[2]
          ], viewer);
        });
        const projectedVertices = rotatedVertices.map((point) => projectPoint(point, scale, canvasEl, viewer, viewerDistance));
        const sourceNormal = face.normal ? face.normal.map(Number) : normalFromVertices(face.vertices.map((vertex) => vertex.map(Number)));
        const rotatedNormal = sourceNormal ? rotatePoint(sourceNormal, viewer) : normalFromVertices(rotatedVertices);
        const averageDepth = rotatedVertices.reduce((sum, point) => sum + point[2], 0) / rotatedVertices.length;
        const minDepth = rotatedVertices.reduce((value, point) => Math.min(value, point[2]), Infinity);
        const maxDepth = rotatedVertices.reduce((value, point) => Math.max(value, point[2]), -Infinity);
        let inferredAxis = face.axis;
        if (!inferredAxis && face.vertices.length >= 2) {
          const xs = face.vertices.map((vertex) => vertex[0]);
          const ys = face.vertices.map((vertex) => vertex[1]);
          const zs = face.vertices.map((vertex) => vertex[2]);
          if (Math.max(...xs) - Math.min(...xs) < 1e-6) inferredAxis = "x";
          else if (Math.max(...ys) - Math.min(...ys) < 1e-6) inferredAxis = "y";
          else if (Math.max(...zs) - Math.min(...zs) < 1e-6) inferredAxis = "z";
        }
        return {
          ...face,
          axis: inferredAxis,
          side: face.side || inferFaceSide(face),
          rotatedNormal,
          averageDepth,
          minDepth,
          maxDepth,
          highlighted: faceMatchesHighlight(face, highlightFaces, highlightComponents, highlightBodyKeys, highlightRawFaces)
            || (!highlightFaces.length && !highlightComponents.length && highlightAxes.includes(inferredAxis)),
          projectedVertices
        };
      // Use a more stable back-to-front ordering for trimmed patches by
      // preferring the farthest depth extent first, then average depth.
      }).sort((a, b) => {
        if (Math.abs(b.maxDepth - a.maxDepth) > 1e-6) return b.maxDepth - a.maxDepth;
        if (Math.abs(b.averageDepth - a.averageDepth) > 1e-6) return b.averageDepth - a.averageDepth;
        return b.minDepth - a.minDepth;
      });

      if (forceMode === "solid" && projectedFaces.length) {
        const useNativeFill = projectedFaces.length > 1400;
        if (useNativeFill) {
          paintSolidFacesNative(ctx2d, projectedFaces, theme);
        } else {
          rasterizeSolidFaces(ctx2d, canvasEl, projectedFaces, theme);
        }
        for (const face of projectedFaces) {
          ctx2d.beginPath();
          ctx2d.moveTo(face.projectedVertices[0][0], face.projectedVertices[0][1]);
          for (const vertex of face.projectedVertices.slice(1)) {
            ctx2d.lineTo(vertex[0], vertex[1]);
          }
          ctx2d.closePath();
          if (face.highlighted) {
            ctx2d.strokeStyle = "#ef6c00";
            ctx2d.lineWidth = 2.2;
            ctx2d.stroke();
          }
        }
      }

      if (preview.bounds && forceMode !== "points" && (forceMode !== "solid" || !projectedFaces.length)) {
        const min = preview.bounds.min.map(Number);
        const max = preview.bounds.max.map(Number);
        const corners = [
          [min[0], min[1], min[2]],
          [max[0], min[1], min[2]],
          [max[0], max[1], min[2]],
          [min[0], max[1], min[2]],
          [min[0], min[1], max[2]],
          [max[0], min[1], max[2]],
          [max[0], max[1], max[2]],
          [min[0], max[1], max[2]]
        ].map((point) => [
          point[0] - preview.center[0],
          point[1] - preview.center[1],
          point[2] - preview.center[2]
        ]);

        const projectedCorners = corners.map((point) =>
          projectPoint(rotatePoint(point, viewer), scale, canvasEl, viewer, viewerDistance)
        );

        const edges = [
          [0,1,"x"],[1,2,"y"],[2,3,"x"],[3,0,"y"],
          [4,5,"x"],[5,6,"y"],[6,7,"x"],[7,4,"y"],
          [0,4,"z"],[1,5,"z"],[2,6,"z"],[3,7,"z"]
        ];

        for (const [a, b, axis] of edges) {
          ctx2d.strokeStyle = highlightAxes.includes(axis)
            ? "#ef6c00"
            : (forceMode === "solid" ? theme.bounds : theme.wireframe);
          ctx2d.lineWidth = highlightAxes.includes(axis) ? 2 : 1;
          ctx2d.beginPath();
          ctx2d.moveTo(projectedCorners[a][0], projectedCorners[a][1]);
          ctx2d.lineTo(projectedCorners[b][0], projectedCorners[b][1]);
          ctx2d.stroke();
        }
      }

      if (preview.kernelGeometry?.edges?.length && forceMode !== "points") {
        for (const edge of preview.kernelGeometry.edges) {
          const edgeHighlighted =
            highlightComponents.includes(edge.componentId)
            || highlightBodyKeys.includes(edge.bodyRawKey)
            || highlightRawEdges.includes(edge.rawEdgeKey)
            || highlightAxes.includes(edge.axis);
          const projectedEdge = edge.points.map((point) =>
            projectPoint(
              rotatePoint(
                [
                  point[0] - preview.center[0],
                  point[1] - preview.center[1],
                  point[2] - preview.center[2]
                ],
                viewer
              ),
              scale,
              canvasEl,
              viewer,
              viewerDistance
            )
          );
          const edgeKind = String(edge.kind || "").toLowerCase();
          const detailEdge = /spcurve|intersection|spline|trimmedcurve|circular|arc/.test(edgeKind);
          ctx2d.strokeStyle = edgeHighlighted ? "#ef6c00" : rgbCss(edge.strokeColor || theme.wireframe);
          ctx2d.lineWidth = edgeHighlighted ? 2 : (forceMode === "solid" ? (detailEdge ? 0.95 : 0.7) : 1.5);
          const solidAlpha = detailEdge ? 0.48 : 0.2;
          ctx2d.globalAlpha = edgeHighlighted ? 1 : ((forceMode === "solid" ? solidAlpha : 1) * Math.max(0.2, Math.min(1, Number(edge.opacity ?? 1))));
          ctx2d.beginPath();
          ctx2d.moveTo(projectedEdge[0][0], projectedEdge[0][1]);
          for (const point of projectedEdge.slice(1)) {
            ctx2d.lineTo(point[0], point[1]);
          }
          ctx2d.stroke();
          ctx2d.globalAlpha = 1;
        }
      }

      const ordered = projected
        .map((point, index) => ({ point, index }))
        .sort((a, b) => a.point[2] - b.point[2]);

      if (forceMode !== "solid" || !projectedFaces.length) {
        ctx2d.fillStyle = highlightAxes.length || highlightComponents.length ? "#ef6c00" : theme.point;
        for (const item of ordered) {
          const [x, y, depth] = item.point;
          const radius = Math.max(3, ordered.length <= 2 ? 7 : 4 * depth);
          ctx2d.beginPath();
          ctx2d.arc(x, y, radius, 0, Math.PI * 2);
          ctx2d.fill();
        }
      }

      if (ordered.length === 1) {
        ctx2d.fillStyle = theme.secondaryText;
        ctx2d.font = "13px 'Segoe UI'";
        ctx2d.fillText("Only one geometric point was decoded from this file.", 16, 24);
      }

      if (preview?.renderStats?.faceReductionApplied) {
        ctx2d.fillStyle = theme.secondaryText;
        ctx2d.font = "12px 'Segoe UI'";
        ctx2d.fillText(
          `Preview mesh simplified: ${preview.renderStats.renderedFaceCount} of ${preview.renderStats.originalFaceCount} faces shown`,
          16,
          canvasEl.height - 18
        );
      }

      drawCompareAnnotations(ctx2d, canvasEl, preview, viewer, scale, compareAnnotation);
      drawViewportHud(ctx2d, canvasEl, report, preview, viewer, {
        mode: forceMode,
        highlightComponents: dedupeList([...highlightComponents, ...highlightBodyKeys, ...highlightRawFaces, ...highlightRawEdges]),
      });
      drawCornerTriad(canvasEl, ctx2d, viewer);
    }

    function drawPreview() {
      drawPreviewWebGl();
    }

    function render() {
      renderTabs();
      renderPreviewModes();
      renderPreviewPresets();
      renderFileList();
      renderCompareButton();
      renderFusionButton();
      renderSummary();
      renderOverview();
      renderNotes();
      renderHeader();
      renderTopology();
      renderGeometry();
      renderCompare();
      renderJson();
      drawPreview();
    }

    async function postJson(url, payload) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({ error: "Unknown error" }));
        throw new Error(error.error || `Request failed: ${response.status}`);
      }
      return response.json();
    }

    async function loadSamples() {
      setStatus("Loading workspace samples...");
      const response = await fetch("/api/samples");
      const data = await response.json();
      state.reports = enrichReports(data.reports || []);
      state.selected = state.reports.length ? [bestReportKey(state.reports)] : [];
      resetPreviewIfNeeded();
      render();
      setStatus(`Loaded ${state.reports.length} workspace sample(s). Select one to preview it or two to compare.`);
    }

    async function loadPaths() {
      const raw = byId("path-input").value.trim();
      if (!raw) {
        setStatus("Enter one or more file paths first.");
        return;
      }
      const paths = raw.split(",").map((item) => item.trim()).filter(Boolean);
      setStatus("Loading file paths...");
      const data = await postJson("/api/analyze-paths", { paths });
      mergeReports(data.reports || []);
      if (data.reports.length === 1) {
        setStatus(`Loaded ${data.reports[0].file_name}. Preview ready.`);
      } else {
        setStatus(`Loaded ${data.reports.length} files. Select two to compare them.`);
      }
    }

    async function loadUploads(files) {
      if (!files.length) return;
      setStatus(`Reading ${files.length} uploaded file(s)...`);
      const payload = await Promise.all([...files].map(async (file) => ({
        name: file.name,
        text: await file.text(),
        size: file.size
      })));
      const data = await postJson("/api/analyze-text", { files: payload });
      mergeReports(data.reports || []);
      if (data.reports.length === 1) {
        setStatus(`Loaded ${data.reports[0].file_name}. Preview ready.`);
      } else {
        setStatus(`Loaded ${data.reports.length} files. Select two to compare them.`);
      }
    }

    function openCompareFromSelection() {
      if (!canCompareSelection()) {
        setStatus("Select exactly two parts first.");
        return;
      }
      state.activeTab = "compare";
      render();
      setStatus("Comparing the two selected parts.");
    }

    async function createFusionFromSelection() {
      const reports = selectedReports();
      if (reports.length !== 2) {
        setStatus("Select exactly two parts first.");
        return;
      }
      if (!canFuseSelection()) {
        setStatus("Select one Siemens NX JSON report and one matching STEP report to improve the JSON reconstruction.");
        return;
      }
      setStatus("Building an improved JSON reconstruction using the selected STEP file...");
      const data = await postJson("/api/fuse-reports", { reports });
      mergeReports(data.reports || []);
      const created = data.reports?.[0];
      setStatus(created ? `Created improved fused model: ${created.file_name}.` : "Created improved fused model.");
    }

    function exportCurrentJson() {
      const report = activeReport();
      if (!report) {
        setStatus("Select a part first, then export its JSON.");
        return;
      }
      const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${report.file_name.replace(/\\.(x_t|step|stp)$/i, "") || "report"}_report.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      setStatus(`Exported ${report.file_name} as JSON.`);
    }

    window.addEventListener("mousemove", (event) => {
      if (!dragState) return;
      const dx = event.clientX - dragState.x;
      const dy = event.clientY - dragState.y;
      dragState.x = event.clientX;
      dragState.y = event.clientY;
      const viewers = dragState.viewers || [];

      viewers.forEach((viewer) => {
        if (dragState.pan) {
          viewer.panX += dx;
          viewer.panY += dy;
        } else {
          const yawRotation = rotationAroundY(-dx * 0.01);
          const pitchRotation = rotationAroundX(-dy * 0.01);
          viewer.rotation = normalizeRotationMatrix(
            multiplyRotationMatrices(
              pitchRotation,
              multiplyRotationMatrices(yawRotation, viewer.rotation || defaultRotation())
            )
          );
        }
      });
      dragState.redraw();
    });

    window.addEventListener("mouseup", () => {
      if (dragState?.canvasEl) {
        dragState.canvasEl.style.cursor = "grab";
      }
      dragState = null;
    });

    window.addEventListener("resize", () => {
      drawPreview();
      if (state.activeTab === "compare") {
        drawComparePreviews();
      }
    });

    byId("load-samples").onclick = () => loadSamples().catch((error) => setStatus(error.message));
    byId("load-paths").onclick = () => loadPaths().catch((error) => setStatus(error.message));
    byId("compare-selected").onclick = openCompareFromSelection;
    byId("export-json").onclick = exportCurrentJson;
    byId("create-fusion").onclick = () => createFusionFromSelection().catch((error) => setStatus(error.message));
    byId("reset-view").onclick = () => {
      resetPreviewIfNeeded();
      if (previewViewport) {
        previewViewport.currentPreset = "iso";
        fitPreviewViewport(previewViewport, "iso");
      }
      drawPreview();
      setStatus("Preview view reset.");
    };
    byId("file-input").addEventListener("change", (event) => {
      loadUploads(event.target.files).catch((error) => setStatus(error.message));
      event.target.value = "";
    });

    render();
  </script>
</body>
</html>
"""


def enrich_report(report: dict) -> dict:
    geometry = report.get("geometry_hints", {})
    for item in geometry.get("notable_scalar_values", []):
        item["pretty_metric_and_imperial"] = metric_and_imperial(item["value"])
    return report


class XTPartRequestHandler(BaseHTTPRequestHandler):
    def _send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b"{}"
        return json.loads(body.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HTML)
            return

        if parsed.path.startswith("/vendor/"):
            repo_root = Path(__file__).resolve().parent
            relative = Path(parsed.path.lstrip("/"))
            file_path = (repo_root / relative).resolve()
            vendor_root = (repo_root / "vendor").resolve()
            if vendor_root not in file_path.parents and file_path != vendor_root:
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if not file_path.exists() or not file_path.is_file():
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return

            suffix = file_path.suffix.lower()
            content_type = {
                ".js": "text/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".map": "application/json; charset=utf-8",
            }.get(suffix, "application/octet-stream")
            self._send_bytes(file_path.read_bytes(), content_type)
            return

        if parsed.path == "/api/samples":
            try:
                reports = [enrich_report(report) for report in workspace_sample_reports()]
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json({"reports": reports})
            return

        if parsed.path == "/api/ping":
            self._send_json({"ok": True})
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        try:
            payload = self._read_json()
            if parsed.path == "/api/analyze-paths":
                reports = [enrich_report(report) for report in analyze_paths(payload.get("paths", []))]
                self._send_json({"reports": reports})
                return

            if parsed.path == "/api/analyze-text":
                reports = [enrich_report(report) for report in analyze_uploaded_files(payload.get("files", []))]
                self._send_json({"reports": reports})
                return

            if parsed.path == "/api/fuse-reports":
                selected_reports = payload.get("reports", [])
                if len(selected_reports) != 2:
                    raise ValueError("Select exactly two reports to fuse.")
                fused_report = create_fused_report(selected_reports[0], selected_reports[1])
                if fused_report is None:
                    raise ValueError("Selected reports are not a compatible STEP + JSON fusion pair.")
                reports = [enrich_report(fused_report)]
                self._send_json({"reports": reports})
                return
        except FileNotFoundError as exc:
            self._send_json({"error": f"File not found: {exc}"}, status=HTTPStatus.NOT_FOUND)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        return


def open_browser() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}/")


def create_server(start_port: int) -> ThreadingHTTPServer:
    global PORT
    for port in range(start_port, start_port + 25):
        try:
            server = ThreadingHTTPServer((HOST, port), XTPartRequestHandler)
            PORT = port
            return server
        except OSError:
            continue
    raise OSError(f"Could not bind any port in range {start_port}-{start_port + 24}")


def main() -> int:
    bootstrap_status = ensure_backend_runtimes(verbose=print)
    if not bootstrap_status.get("message_already_reported"):
        print(bootstrap_status.get("message"))

    server = create_server(PORT)
    print(f"Serving {HOST}:{PORT}")
    print("Open this in your browser if it does not launch automatically:")
    print(f"http://{HOST}:{PORT}/")

    if not os.environ.get("CODEX_NO_BROWSER"):
        try:
            open_browser()
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
