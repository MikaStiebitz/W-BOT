"""MCP integration: geometry generators plus the tool server."""

from .plotting import (PlotError, arc, bezier, circle, draw_axes, ellipse,
                       plot_data, plot_function, plot_parametric, plot_polar,
                       polygon, rectangle, text, write_equation)

__all__ = [
    "PlotError", "circle", "ellipse", "arc", "rectangle", "polygon", "bezier",
    "text", "plot_function", "plot_parametric", "plot_polar", "draw_axes",
    "plot_data", "write_equation",
]
