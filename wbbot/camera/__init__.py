"""Optional camera: board capture, homography, and the read-back channel.

Everything here is optional and self-contained — the rest of wbbot runs
unchanged with no camera attached.
"""

from .boardmap import BoardMap, BoardMapError
from .capture import CameraError, Camera, StillCamera
from .readback import BoardReading, FreeSpace, read_board, find_free_space

__all__ = [
    "BoardMap", "BoardMapError",
    "Camera", "StillCamera", "CameraError",
    "BoardReading", "FreeSpace", "read_board", "find_free_space",
]
