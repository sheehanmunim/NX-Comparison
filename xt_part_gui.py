#!/usr/bin/env python3

import json
import os
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from xt_part_report import analyze_xt_file, analyze_xt_text, metric_and_imperial


HOST = "127.0.0.1"
PORT = 8765


def workspace_sample_reports() -> list[dict]:
    return [analyze_xt_file(path) for path in sorted(Path.cwd().glob("*.x_t"))]


def analyze_paths(paths: list[str]) -> list[dict]:
    reports: list[dict] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        reports.append(analyze_xt_file(path))
    return reports


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
  <title>Parasolid XT Part Inspector</title>
  <style>
    * { box-sizing: border-box; }
    html, body { margin: 0; height: 100%; }
    body {
      font-family: Arial, Helvetica, sans-serif;
      background: #f3f3f3;
      color: #222;
    }
    .app {
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      min-height: 100vh;
    }
    .header, .toolbar, .sidebar, .main, .status {
      background: #fff;
      border: 1px solid #cfcfcf;
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
      color: #555;
      font-size: 14px;
      line-height: 1.4;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .toolbar input[type="text"] {
      flex: 1 1 320px;
      min-width: 220px;
      padding: 8px;
      border: 1px solid #bdbdbd;
      background: #fff;
      color: #222;
    }
    button, .file-upload {
      padding: 8px 12px;
      border: 1px solid #bdbdbd;
      background: #efefef;
      color: #222;
      cursor: pointer;
      font: inherit;
    }
    button:hover, .file-upload:hover {
      background: #e5e5e5;
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
    }
    .sidebar h2, .panel h2 {
      margin: 0;
      font-size: 16px;
    }
    .hint {
      color: #666;
      font-size: 13px;
      line-height: 1.4;
    }
    .file-list {
      overflow: auto;
      min-height: 0;
      border: 1px solid #d8d8d8;
      background: #fafafa;
    }
    .file-item {
      padding: 10px;
      border-bottom: 1px solid #d8d8d8;
      cursor: pointer;
      background: #fff;
    }
    .file-item:last-child { border-bottom: 0; }
    .file-item:hover { background: #f0f0f0; }
    .file-item.selected { background: #dcdcdc; }
    .file-item strong {
      display: block;
      margin-bottom: 4px;
      font-size: 14px;
    }
    .file-item span {
      display: block;
      color: #666;
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
      border: 1px solid #d8d8d8;
      background: #fff;
      padding: 10px;
      min-height: 0;
    }
    .summary-grid {
      display: grid;
      gap: 8px;
      font-size: 14px;
    }
    .summary-grid .muted { color: #666; }
    .preview-wrap {
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      gap: 8px;
      min-height: 320px;
    }
    .preview-toolbar {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .preview-mode {
      padding: 6px 10px;
      border: 1px solid #bdbdbd;
      background: #efefef;
      cursor: pointer;
      font-size: 12px;
    }
    .preview-mode.active {
      background: #dcdcdc;
      font-weight: 700;
    }
    canvas {
      width: 100%;
      height: 100%;
      min-height: 320px;
      border: 1px solid #bdbdbd;
      background: #fff;
      display: block;
    }
    .preview-note {
      font-size: 12px;
      color: #666;
      line-height: 1.4;
    }
    .tabs {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .tab {
      padding: 8px 10px;
      border: 1px solid #bdbdbd;
      background: #efefef;
      cursor: pointer;
      user-select: none;
      font-size: 13px;
    }
    .tab.active {
      background: #dcdcdc;
      font-weight: 700;
    }
    .tab-panels {
      border: 1px solid #d8d8d8;
      background: #fff;
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
      border: 1px dashed #c8c8c8;
      padding: 16px;
      color: #666;
      background: #fafafa;
      font-size: 14px;
    }
    .compare-layout {
      display: grid;
      gap: 10px;
    }
    .compare-controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      padding: 10px 12px;
      border: 1px solid #d8d8d8;
      background: #fafafa;
      font-size: 13px;
      color: #444;
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
      border: 1px solid #d8d8d8;
      background: #fff;
      padding: 10px;
    }
    .compare-preview-card h3 {
      margin: 0 0 8px 0;
      font-size: 14px;
    }
    .compare-canvas {
      width: 100%;
      height: 320px;
      min-height: 320px;
      border: 1px solid #bdbdbd;
      background: #fff;
      display: block;
    }
    .compare-note {
      margin-top: 8px;
      color: #666;
      font-size: 12px;
      line-height: 1.4;
    }
    @media (max-width: 980px) {
      .content { grid-template-columns: 1fr; }
      .top-row { grid-template-columns: 1fr; }
      .compare-preview-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app">
    <div class="header">
      <h1>Parasolid XT Part Inspector</h1>
      <p>Load `.x_t` files, inspect extracted metadata, and view an interactive point-based preview from the geometry records found in the file.</p>
    </div>

    <div class="toolbar">
      <button id="load-samples">Load Workspace Samples</button>
      <label class="file-upload">Upload .x_t Files<input id="file-input" type="file" accept=".x_t" multiple></label>
      <input id="path-input" type="text" placeholder="Enter one or more file paths separated by commas">
      <button id="load-paths">Analyze Paths</button>
      <button id="export-json">Export Current JSON</button>
      <button id="reset-view">Reset Preview View</button>
    </div>

    <div class="content">
      <div class="sidebar">
        <h2>Loaded Parts</h2>
        <div class="hint">Click one part to inspect it. Use Ctrl-click, Cmd-click, or Shift-click to add a second part for comparison.</div>
        <div class="file-list" id="file-list"></div>
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
            <div class="preview-toolbar" id="preview-modes"></div>
            <canvas id="preview-canvas"></canvas>
            <div class="preview-note">Drag to rotate. Shift + drag to pan. Mouse wheel to zoom. Double-click to reset. Solid mode uses inferred faces when the part can be reconstructed confidently; otherwise the app falls back to points and edges decoded from the `.x_t` data.</div>
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

    <div class="status" id="status">Ready.</div>
  </div>

  <script>
    function defaultViewerState() {
      return {
        yaw: -0.7,
        pitch: 0.5,
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
      compareSync: false,
      compareViewers: {
        left: defaultViewerState(),
        right: defaultViewerState()
      }
    };

    const tabs = [
      ["overview", "Overview"],
      ["notes", "Part Notes"],
      ["header", "Header"],
      ["topology", "Topology"],
      ["geometry", "Geometry"],
      ["compare", "Compare"],
      ["json", "JSON"]
    ];

    const canvas = document.getElementById("preview-canvas");
    const ctx = canvas.getContext("2d");
    let dragState = null;

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

    function activeReport() {
      if (!state.selected.length) return null;
      return state.reports.find((report) => reportKey(report) === state.selected[0]) || null;
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
      return state.selected
        .map((key) => state.reports.find((report) => reportKey(report) === key))
        .filter(Boolean);
    }

    function formatDensity(report) {
      if (!report || !report.density) return "Not found";
      return `${report.density.value} ${report.density.unit || ""}`.trim();
    }

    function formatColors(report) {
      if (!report || !report.colors || !report.colors.length) return "Not found";
      return report.colors.map((color) => color.hex).join(", ");
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

    function compareAxisDimensions(left, right) {
      const axes = ["x", "y", "z"];
      const leftBounds = left?.geometry_hints?.bounds;
      const rightBounds = right?.geometry_hints?.bounds;
      if (!leftBounds || !rightBounds) return [];

      return axes.map((axis, index) => {
        const leftValue = Number(leftBounds.size?.[index] || 0);
        const rightValue = Number(rightBounds.size?.[index] || 0);
        const delta = rightValue - leftValue;
        return {
          axis,
          leftValue,
          rightValue,
          delta,
          changed: Math.abs(delta) > 1e-6
        };
      });
    }

    function compareChangedFaces(left, right) {
      const axes = ["x", "y", "z"];
      const leftBounds = left?.geometry_hints?.bounds;
      const rightBounds = right?.geometry_hints?.bounds;
      const result = { left: [], right: [] };
      if (!leftBounds || !rightBounds) return result;

      axes.forEach((axis, index) => {
        if (Math.abs(Number(leftBounds.min?.[index] || 0) - Number(rightBounds.min?.[index] || 0)) > 1e-6) {
          result.left.push({ axis, side: "min" });
          result.right.push({ axis, side: "min" });
        }
        if (Math.abs(Number(leftBounds.max?.[index] || 0) - Number(rightBounds.max?.[index] || 0)) > 1e-6) {
          result.left.push({ axis, side: "max" });
          result.right.push({ axis, side: "max" });
        }
      });

      return result;
    }

    function topologyLabel(report) {
      const topology = report?.model_analysis?.topology;
      if (topology?.shape_label) return topology.shape_label;

      const primitive = report?.kernel_body?.metadata?.primitive;
      if (primitive === "box") return report?.kernel_body?.name || "box";
      if (primitive === "cylinder") return "cylinder";
      if (primitive === "sphere") return "sphere";
      if (primitive === "arc_or_circle") return "arc / circle";

      return report?.kernel_body?.name || "Not inferred";
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
        return `${kernelBody.faces?.length || 0} faces / ${kernelBody.edges?.length || 0} edges`;
      }

      return "Not inferred";
    }

    function kernelLabel(report) {
      return report?.kernel_body?.name || "Not reconstructed";
    }

    function compareDiffInfo(left, right) {
      const info = {
        changedAxes: [],
        changedShape: false,
        summary: [],
        dimensions: compareAxisDimensions(left, right),
        faceChanges: compareChangedFaces(left, right)
      };

      if (!left || !right) {
        return info;
      }

      info.changedAxes = info.dimensions.filter((item) => item.changed).map((item) => item.axis);

      const leftShape = topologyLabel(left);
      const rightShape = topologyLabel(right);
      info.changedShape = leftShape !== rightShape;

      if (info.changedShape && !info.changedAxes.length) {
        info.changedAxes = ["x", "y", "z"];
      }

      if (info.changedAxes.length) {
        info.summary.push(`Changed axes: ${info.changedAxes.map((axis) => axis.toUpperCase()).join(", ")}`);
        for (const item of info.dimensions.filter((dimension) => dimension.changed)) {
          const minMoved = info.faceChanges.left.some((face) => face.axis === item.axis && face.side === "min");
          const maxMoved = info.faceChanges.left.some((face) => face.axis === item.axis && face.side === "max");
          const movedSide = minMoved && maxMoved ? "both faces" : (maxMoved ? "max face" : (minMoved ? "min face" : "face"));
          info.summary.push(
            `${item.axis.toUpperCase()} ${movedSide} changed from ${formatLength(item.leftValue)} to ${formatLength(item.rightValue)} (${formatSignedInches(item.delta)})`
          );
        }
      } else {
        info.summary.push("No axis-size difference was inferred from the available bounds.");
      }

      if (info.changedShape) {
        info.summary.push(`Shape changed: ${leftShape || "unknown"} -> ${rightShape || "unknown"}`);
      }

      return info;
    }

    function projectModelPoint(point, preview, viewer, scale, canvasEl) {
      return projectPoint(
        rotatePoint(
          [
            point[0] - preview.center[0],
            point[1] - preview.center[1],
            point[2] - preview.center[2]
          ],
          viewer.yaw,
          viewer.pitch
        ),
        scale,
        canvasEl,
        viewer
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

    function faceMatchesHighlight(face, highlightFaces) {
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
      if (!compareAnnotation?.dimensions?.length || !preview?.bounds) return;

      const changed = compareAnnotation.dimensions.filter((item) => item.changed);
      if (!changed.length) return;

      const color = "#ef6c00";
      const shadow = "rgba(255,255,255,0.95)";
      const roleKey = compareAnnotation.role === "right" ? "rightValue" : "leftValue";
      const deltaSuffix = compareAnnotation.role === "right" ? " vs left" : "";
      const labelStack = [];

      for (const item of changed) {
        const [start3d, end3d] = axisDimensionEndpoints(preview.bounds, item.axis);
        const start = projectModelPoint(start3d, preview, viewer, scale, canvasEl);
        const end = projectModelPoint(end3d, preview, viewer, scale, canvasEl);
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

        const label = `${item.axis.toUpperCase()}: ${(item[roleKey] * 39.37007874015748).toFixed(3)} in${compareAnnotation.role === "right" ? ` (${formatSignedInches(item.delta)}${deltaSuffix})` : ""}`;
        labelStack.push(label);
      }

      if (!labelStack.length) return;

      ctx2d.font = "bold 12px Arial";
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
        yaw: viewer.yaw,
        pitch: viewer.pitch,
        zoom: viewer.zoom,
        panX: viewer.panX,
        panY: viewer.panY,
        mode: viewer.mode || "solid"
      };
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

      canvasEl.addEventListener("mousedown", (event) => {
        dragState = {
          x: event.clientX,
          y: event.clientY,
          pan: event.shiftKey,
          viewers: normalizeViewerList(viewerAccessor()),
          redraw
        };
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
        setStatus(`${label} reset.`);
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
      root.innerHTML = "";
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

    function renderFileList() {
      const list = byId("file-list");
      list.innerHTML = "";

      if (!state.reports.length) {
        list.innerHTML = '<div class="empty">No reports loaded.</div>';
        byId("sidebar-foot").textContent = "Use the toolbar to load files.";
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
          <span>${escapeHtml(report.decoded_names?.join(", ") || "No decoded names")}</span>
          <span>${escapeHtml(bounds ? formatBounds(bounds) : "No bounds inferred")}</span>
        `;
        item.onclick = (event) => handleSelectionClick(key, event);
        list.appendChild(item);
      }

      byId("sidebar-foot").textContent =
        state.selected.length === 2
          ? "Two parts selected. Use Compare tab to inspect both."
          : `${state.reports.length} report(s) loaded.`;
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
        ["Decoded Names", report.decoded_names?.join(", ") || "None"],
        ["Source App", report.header?.PART1?.APPL || "Unknown"],
        ["Date", report.header?.PART1?.DATE || "Unknown"],
        ["Schema", report.header?.PART2?.SCH || "Unknown"],
        ["Kernel Body", kernelLabel(report)],
        ["Inferred Shape", topologyLabel(report)],
        ["Faces / Edges", topologyCounts(report)],
        ["Density", formatDensity(report)],
        ["Colors", formatColors(report)],
        ["Bounds", formatBounds(report.geometry_hints?.bounds)],
        ["Preview Points", String(report.geometry_hints?.point_count || 0)],
        ["Scale Factor Attribute", report.has_scale_factor_attribute ? "Present" : "Not found"]
      ];

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
        ["Date", report.header?.PART1?.DATE || "Unknown"],
        ["Decoded Names", report.decoded_names?.join(", ") || "None"],
        ["Kernel Body", kernelLabel(report)],
        ["Inferred Shape", topologyLabel(report)],
        ["Faces / Edges", topologyCounts(report)],
        ["Density", formatDensity(report)],
        ["Colors", formatColors(report)],
        ["Bounds", formatBounds(report.geometry_hints?.bounds)],
        ["Preview Point Count", String(report.geometry_hints?.point_count || 0)],
        ["Object State IDs", String(report.object_state_ids?.length || 0)]
      ];

      root.innerHTML = `
        <table>
          <thead><tr><th>Field</th><th>Value</th></tr></thead>
          <tbody>${rows.map(([label, value]) => `<tr><td>${escapeHtml(label)}</td><td>${escapeHtml(value)}</td></tr>`).join("")}</tbody>
        </table>
      `;
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
        panel.textContent = "Select a part to inspect inferred faces and edges.";
        return;
      }

      const analysis = report.model_analysis || {};
      const topology = analysis.topology;
      const kernelBody = report.kernel_body || null;
      const kernelTopology = report.kernel_topology || null;
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
        "",
        "Axis Levels",
        "-----------",
        `X : ${(levels.x || []).join(", ") || "None"}`,
        `Y : ${(levels.y || []).join(", ") || "None"}`,
        `Z : ${(levels.z || []).join(", ") || "None"}`
      );

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
      const rows = [
        ["Decoded Names", left.decoded_names?.join(", ") || "None", right.decoded_names?.join(", ") || "None"],
        ["Inferred Shape", topologyLabel(left), topologyLabel(right)],
        ["Faces / Edges", topologyCounts(left), topologyCounts(right)],
        ["Density", formatDensity(left), formatDensity(right)],
        ["Colors", formatColors(left), formatColors(right)],
        ["Bounds", formatBounds(left.geometry_hints?.bounds), formatBounds(right.geometry_hints?.bounds)],
        ["Preview Points", String(left.geometry_hints?.point_count || 0), String(right.geometry_hints?.point_count || 0)],
        ["Object State IDs", String(left.object_state_ids?.length || 0), String(right.object_state_ids?.length || 0)]
      ];

      root.innerHTML = `
        <div class="compare-layout">
          <div class="empty">${escapeHtml(diff.summary.join(" | "))}</div>
          <div class="compare-controls">
            <label>
              <input type="checkbox" id="compare-sync-toggle" ${state.compareSync ? "checked" : ""}>
              Move both models together
            </label>
          </div>
          <div class="compare-preview-grid">
            <div class="compare-preview-card">
              <h3>${escapeHtml(left.file_name)}</h3>
              <canvas id="compare-left-canvas" class="compare-canvas"></canvas>
              <div class="compare-note">Drag to rotate. Shift + drag to pan. Wheel to zoom. Double-click to reset. Orange arrows show changed dimensions on this model.</div>
            </div>
            <div class="compare-preview-card">
              <h3>${escapeHtml(right.file_name)}</h3>
              <canvas id="compare-right-canvas" class="compare-canvas"></canvas>
              <div class="compare-note">Drag to rotate. Shift + drag to pan. Wheel to zoom. Double-click to reset. Orange arrows show changed dimensions; labels include the delta versus the left model.</div>
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
            <tbody>${rows.map(([label, a, b]) => `<tr><td>${escapeHtml(label)}</td><td>${escapeHtml(a)}</td><td>${escapeHtml(b)}</td></tr>`).join("")}</tbody>
          </table>
        </div>
      `;

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
        highlightFaces: diff.faceChanges.left,
        compareAnnotation: {
          role: "left",
          dimensions: diff.dimensions
        }
      });
      drawModelPreview(rightCanvas, right, state.compareViewers.right, {
        mode: "solid",
        highlightAxes: diff.changedAxes,
        highlightFaces: diff.faceChanges.right,
        compareAnnotation: {
          role: "right",
          dimensions: diff.dimensions
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

    function rotatePoint(point, yaw, pitch) {
      const [x, y, z] = point;
      const cy = Math.cos(yaw);
      const sy = Math.sin(yaw);
      const cp = Math.cos(pitch);
      const sp = Math.sin(pitch);

      const x1 = x * cy - z * sy;
      const z1 = x * sy + z * cy;
      const y1 = y;

      const y2 = y1 * cp - z1 * sp;
      const z2 = y1 * sp + z1 * cp;

      return [x1, y2, z2];
    }

    function projectPoint(point, scale, canvasEl, viewer) {
      const viewerDistance = 4;
      const depth = viewerDistance / (viewerDistance + point[2]);
      return [
        point[0] * scale * depth + canvasEl.width / 2 + viewer.panX,
        -point[1] * scale * depth + canvasEl.height / 2 + viewer.panY,
        depth
      ];
    }

    function drawCornerTriad(canvasEl, ctx2d, viewer) {
      const centerX = 72;
      const centerY = canvasEl.height - 72;
      const triadScale = 34;

      function projectTriad(point) {
        const rotated = rotatePoint(point, viewer.yaw, viewer.pitch);
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

      ctx2d.fillStyle = "#ffffff";
      ctx2d.strokeStyle = "#bdbdbd";
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
        ctx2d.font = "bold 12px Arial";
        ctx2d.fillText(axis.label, end[0] + 7, end[1] - 4);
      }

      ctx2d.fillStyle = "#555555";
      ctx2d.beginPath();
      ctx2d.arc(origin[0], origin[1], 3, 0, Math.PI * 2);
      ctx2d.fill();
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

    function boundsFromPoints(points) {
      if (!points.length) return null;
      const xs = points.map((point) => point[0]);
      const ys = points.map((point) => point[1]);
      const zs = points.map((point) => point[2]);
      const min = [Math.min(...xs), Math.min(...ys), Math.min(...zs)];
      const max = [Math.max(...xs), Math.max(...ys), Math.max(...zs)];
      return {
        min,
        max,
        size: [max[0] - min[0], max[1] - min[1], max[2] - min[2]]
      };
    }

    function previewGeometryFromKernelBody(body) {
      if (!body) return { faces: [], points: [], edges: [] };

      if (body.faces?.some((face) => face.vertices?.length)) {
        const faces = body.faces
          .filter((face) => face.vertices?.length)
          .map((face) => ({
            name: face.name,
            axis: face.metadata?.axis || null,
            side: inferFaceSide(face),
            normal: face.normal || null,
            vertices: face.vertices.map((vertex) => vertex.map(Number))
          }));
        const points = faces.flatMap((face) => face.vertices);
        return { faces, points, edges: [] };
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
            vertices: [startRing[i], startRing[next], endRing[next], endRing[i]]
          });
        }

        faces.push({
          name: "Start cap",
          axis,
          side: "min",
          normal: scaleVec(axisVec, -1),
          vertices: [...startRing].reverse()
        });
        faces.push({
          name: "End cap",
          axis,
          side: "max",
          normal: axisVec,
          vertices: endRing
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
              vertices: quad
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
            points: [points[i], points[(i + 1) % points.length]]
          });
        }
        return { faces: [], points, edges };
      }

      return { faces: [], points: [], edges: [] };
    }

    function getPreviewData(report) {
      if (!report) return null;
      const points = (report.geometry_hints?.preview_points || []).map((point) => point.map(Number));
      const kernelBody = report.kernel_body || null;
      const kernelGeometry = previewGeometryFromKernelBody(kernelBody);
      const cloud = [...points, ...kernelGeometry.points];
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

      const xs = cloud.map((point) => point[0]);
      const ys = cloud.map((point) => point[1]);
      const zs = cloud.map((point) => point[2]);
      const center = [
        (Math.min(...xs) + Math.max(...xs)) / 2,
        (Math.min(...ys) + Math.max(...ys)) / 2,
        (Math.min(...zs) + Math.max(...zs)) / 2
      ];
      const span = Math.max(
        Math.max(...xs) - Math.min(...xs),
        Math.max(...ys) - Math.min(...ys),
        Math.max(...zs) - Math.min(...zs),
        1e-6
      );

      return {
        points,
        bounds,
        kernelBody,
        kernelGeometry,
        center,
        span
      };
    }

    function shadeForNormal(normal) {
      const light = [0.45, -0.35, 0.82];
      const len = Math.hypot(...light) || 1;
      const unitLight = light.map((value) => value / len);
      const dot = Math.max(0, normal[0] * unitLight[0] + normal[1] * unitLight[1] + normal[2] * unitLight[2]);
      const shade = Math.round(160 + dot * 70);
      return `rgb(${shade}, ${shade}, ${shade})`;
    }

    function drawModelPreview(canvasEl, report, viewer, options = {}) {
      const ctx2d = canvasEl.getContext("2d");
      const highlightAxes = options.highlightAxes || [];
      const highlightFaces = options.highlightFaces || [];
      const compareAnnotation = options.compareAnnotation || null;
      const forceMode = options.mode || viewer.mode || "solid";

      resizeCanvasElement(canvasEl);
      ctx2d.clearRect(0, 0, canvasEl.width, canvasEl.height);
      ctx2d.fillStyle = "#ffffff";
      ctx2d.fillRect(0, 0, canvasEl.width, canvasEl.height);

      const preview = getPreviewData(report);
      if (!preview) {
        ctx2d.fillStyle = "#666666";
        ctx2d.font = "14px Arial";
        ctx2d.fillText("No previewable solid geometry was decoded from this file.", 16, 24);
        if (report?.decoded_names?.length) {
          ctx2d.fillText(`Named entities: ${report.decoded_names.join(", ")}`, 16, 48);
        }
        ctx2d.fillText("This app currently renders inferred geometry, not the full Parasolid B-rep.", 16, 72);
        drawCornerTriad(canvasEl, ctx2d, viewer);
        return;
      }

      const scale = (Math.min(canvasEl.width, canvasEl.height) * 0.32 / preview.span) * viewer.zoom;
      const centeredPoints = preview.points.map((point) => [
        point[0] - preview.center[0],
        point[1] - preview.center[1],
        point[2] - preview.center[2]
      ]);

      const projected = centeredPoints.map((point) =>
        projectPoint(rotatePoint(point, viewer.yaw, viewer.pitch), scale, canvasEl, viewer)
      );

      const projectedFaces = (preview.kernelGeometry?.faces || []).map((face) => {
        const rotatedVertices = face.vertices.map((point) => {
          return rotatePoint([
            point[0] - preview.center[0],
            point[1] - preview.center[1],
            point[2] - preview.center[2]
          ], viewer.yaw, viewer.pitch);
        });
        const projectedVertices = rotatedVertices.map((point) => projectPoint(point, scale, canvasEl, viewer));
        const rotatedNormal = face.normal ? rotatePoint(face.normal.map(Number), viewer.yaw, viewer.pitch) : null;
        const averageDepth = rotatedVertices.reduce((sum, point) => sum + point[2], 0) / rotatedVertices.length;
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
          projectedVertices
        };
      // Painter's algorithm: draw farther faces first so nearer caps/walls
      // remain visible instead of being painted over by back faces.
      }).sort((a, b) => b.averageDepth - a.averageDepth);

      if (forceMode === "solid" && projectedFaces.length) {
        for (const face of projectedFaces) {
          if (face.rotatedNormal && face.rotatedNormal[2] >= 0.15) {
            continue;
          }
          ctx2d.beginPath();
          ctx2d.moveTo(face.projectedVertices[0][0], face.projectedVertices[0][1]);
          for (const vertex of face.projectedVertices.slice(1)) {
            ctx2d.lineTo(vertex[0], vertex[1]);
          }
          ctx2d.closePath();
          const faceHighlighted = faceMatchesHighlight(face, highlightFaces) || (!highlightFaces.length && highlightAxes.includes(face.axis));
          const fillColor = faceHighlighted
            ? "#ffcc80"
            : (face.rotatedNormal ? shadeForNormal(face.rotatedNormal) : "#d9d9d9");
          ctx2d.fillStyle = fillColor;
          ctx2d.fill();
          ctx2d.strokeStyle = faceHighlighted ? "#ef6c00" : "#5f5f5f";
          ctx2d.lineWidth = faceHighlighted ? 2 : 1;
          ctx2d.stroke();
        }
      }

      if (preview.bounds && forceMode !== "points") {
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
          projectPoint(rotatePoint(point, viewer.yaw, viewer.pitch), scale, canvasEl, viewer)
        );

        const edges = [
          [0,1,"x"],[1,2,"y"],[2,3,"x"],[3,0,"y"],
          [4,5,"x"],[5,6,"y"],[6,7,"x"],[7,4,"y"],
          [0,4,"z"],[1,5,"z"],[2,6,"z"],[3,7,"z"]
        ];

        for (const [a, b, axis] of edges) {
          ctx2d.strokeStyle = highlightAxes.includes(axis)
            ? "#ef6c00"
            : (forceMode === "solid" ? "#6a6a6a" : "#4a4a4a");
          ctx2d.lineWidth = highlightAxes.includes(axis) ? 2 : 1;
          ctx2d.beginPath();
          ctx2d.moveTo(projectedCorners[a][0], projectedCorners[a][1]);
          ctx2d.lineTo(projectedCorners[b][0], projectedCorners[b][1]);
          ctx2d.stroke();
        }
      }

      if (preview.kernelGeometry?.edges?.length && forceMode !== "points") {
        for (const edge of preview.kernelGeometry.edges) {
          const projectedEdge = edge.points.map((point) =>
            projectPoint(
              rotatePoint(
                [
                  point[0] - preview.center[0],
                  point[1] - preview.center[1],
                  point[2] - preview.center[2]
                ],
                viewer.yaw,
                viewer.pitch
              ),
              scale,
              canvasEl,
              viewer
            )
          );
          ctx2d.strokeStyle = highlightAxes.includes(edge.axis) ? "#ef6c00" : "#2f2f2f";
          ctx2d.lineWidth = highlightAxes.includes(edge.axis) ? 2 : 1.5;
          ctx2d.beginPath();
          ctx2d.moveTo(projectedEdge[0][0], projectedEdge[0][1]);
          ctx2d.lineTo(projectedEdge[1][0], projectedEdge[1][1]);
          ctx2d.stroke();
        }
      }

      const ordered = projected
        .map((point, index) => ({ point, index }))
        .sort((a, b) => a.point[2] - b.point[2]);

      if (forceMode !== "solid" || !projectedFaces.length) {
        ctx2d.fillStyle = highlightAxes.length ? "#ef6c00" : "#333333";
        for (const item of ordered) {
          const [x, y, depth] = item.point;
          const radius = Math.max(3, ordered.length <= 2 ? 7 : 4 * depth);
          ctx2d.beginPath();
          ctx2d.arc(x, y, radius, 0, Math.PI * 2);
          ctx2d.fill();
        }
      }

      if (ordered.length === 1) {
        ctx2d.fillStyle = "#555555";
        ctx2d.font = "13px Arial";
        ctx2d.fillText("Only one geometric point was decoded from this file.", 16, 24);
      }

      drawCompareAnnotations(ctx2d, canvasEl, preview, viewer, scale, compareAnnotation);
      drawCornerTriad(canvasEl, ctx2d, viewer);
    }

    function drawPreview() {
      drawModelPreview(canvas, activeReport(), state.viewer, {
        mode: state.viewer.mode
      });
    }

    function render() {
      renderTabs();
      renderPreviewModes();
      renderFileList();
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
      setStatus(`Loaded ${state.reports.length} workspace sample(s).`);
    }

    async function loadPaths() {
      const raw = byId("path-input").value.trim();
      if (!raw) {
        setStatus("Enter one or more file paths first.");
        return;
      }
      const paths = raw.split(",").map((item) => item.trim()).filter(Boolean);
      setStatus("Analyzing file paths...");
      const data = await postJson("/api/analyze-paths", { paths });
      mergeReports(data.reports || []);
      setStatus(`Analyzed ${data.reports.length} path-based file(s).`);
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
      setStatus(`Analyzed ${data.reports.length} uploaded file(s).`);
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
      anchor.download = `${report.file_name.replace(/\\.x_t$/i, "") || "report"}_report.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      setStatus(`Exported ${report.file_name} as JSON.`);
    }

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
          viewer.yaw += dx * 0.01;
          viewer.pitch += dy * 0.01;
        }
      });
      dragState.redraw();
    });

    window.addEventListener("mouseup", () => {
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
    byId("export-json").onclick = exportCurrentJson;
    byId("reset-view").onclick = () => {
      resetPreviewIfNeeded();
      drawPreview();
      setStatus("Preview view reset.");
    };
    byId("file-input").addEventListener("change", (event) => {
      loadUploads(event.target.files).catch((error) => setStatus(error.message));
      event.target.value = "";
    });

    render();
    loadSamples().catch((error) => setStatus(error.message));
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
                reports = []
                for file_payload in payload.get("files", []):
                    report = analyze_xt_text(
                        file_payload.get("text", ""),
                        display_name=file_payload.get("name", "<upload>"),
                        source_path=f"uploaded:{file_payload.get('name', '<upload>')}",
                        file_size_bytes=file_payload.get("size"),
                    )
                    reports.append(enrich_report(report))
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
