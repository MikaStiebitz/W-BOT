# Changelog

Hardware versions live in their own folder (`v1.0/`, `v1.1/`, …). Software changes are listed here too.

## Unreleased — software

- English everywhere: UI, backend messages, pattern names, CLI text, config comments
- Simplified web UI: Draw, Pens, Setup, Move and More (advanced panels collapsed); dismissible, non-stacking error notices
- Image import: drop PNG, JPEG, WebP or SVG; images are traced into pen paths (lines, outline, fill) and previewed (`POST /api/import`)
- Per-pen colour, name and line width, shown on an interactive 3D revolver; the preview and trace use the real width
- One-click setup: camera, whiteboard, motors and gondola position (camera finds where the belts meet); measured motors are applied after confirmation, the drawing area follows them
- Camera: board and motor detection, movement and object guard, gondola finder, quieter errors
- Live draw: draw on the board view and the plotter follows (queued strokes, speed slider, Stop)
- Photo style for image import (edge-based outlines for photos and renders)
- The drawing (image, trace options, size and position, live strokes) survives a page reload
- Time estimate that learns from finished drawings and a better remaining-time display while drawing
- Whiteboard quiz over MCP and a check against writing already on the board (camera)
- New slider look; tests run against the built-in defaults instead of the local `config/wbot.yaml`

## v1.0 — 2026-11-20

**Hardware** (`v1.0/`)
- Gondola V2: park ring instead of a ramp, crank-and-rod pen lifter, servo inside the drum, steel-rod guides
- Motor mounts, idler pulleys, DLC32 holder, fillable counterweights
- Complete Bambu Studio project (`WBOT_Complete_v1.0.3mf`) plus 0.8 mm / 0.4 mm variants and presets
- Fusion archive, STL files, bill of materials, assembly, wiring and printing guides

**Software**
- Host rewrite: exact belt kinematics with pre-distortion, gondola tilt model, FluidNC client, simulator, planner, web UI
- Wi-Fi transport (Telnet / WebSocket) and a fake board for testing
- MCP server for AI-driven drawing
- Camera support (experimental): board overlay, tape-measure and automatic calibration, gondola tracking
