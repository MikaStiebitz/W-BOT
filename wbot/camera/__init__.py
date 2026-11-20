"""Camera support: live image of the board, board <-> image mapping, overlays, tracking, auto-calibration.

* ``geometry``  board <-> image mapping (homography, optional lens model, camera centre for parallax)
* ``detect``    ArUco markers, board outline, drawn crosses, chessboard (lens calibration)
* ``sources``   frame sources: OpenCV device (DirectShow, also OBS Virtual Camera), URL, simulator, still image
* ``simcam``    synthetic camera that films the simulator's real (plant) trace in perspective
* ``overlay``   server-side overlay drawing (MCP snapshot)
* ``service``   ``CameraService``: settings, frames, rectified view, tracking (one per app)
* ``autocal``   draw crosses -> park -> measure -> geometry fit
"""
