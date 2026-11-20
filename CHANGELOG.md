# Changelog

Hardware versions live in their own folder (`v1.0/`, `v1.1/`, …). Software changes are listed here too.

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
