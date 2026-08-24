"""One-off generator for the application icon (icon.ico).

The mark is a folder with a key across it: the files and databases of a
site, and the app that keeps them.

Run: ``python make_icon.py``. Renders crisp frames at every standard Windows
icon size with Qt (already a dependency) and writes a real multi-resolution
.ico (PNG-compressed frames), so the icon stays sharp from 16px to 256px.
"""

from __future__ import annotations

import struct
import sys
from io import BytesIO

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QApplication

SIZES = [16, 24, 32, 48, 64, 128, 256]


def _folder_path(size: int) -> QPainterPath:
    """A folder outline: body with a raised tab on the left of the top edge."""
    left, right = size * 0.20, size * 0.80
    top, bottom = size * 0.335, size * 0.735
    tab_right, tab_top = size * 0.455, size * 0.255
    radius = size * 0.055

    path = QPainterPath()
    path.moveTo(left + radius, tab_top)
    path.lineTo(tab_right - radius * 0.6, tab_top)
    # the little diagonal shoulder from the tab down to the body edge
    path.lineTo(tab_right + radius * 0.6, top)
    path.lineTo(right - radius, top)
    path.quadTo(right, top, right, top + radius)
    path.lineTo(right, bottom - radius)
    path.quadTo(right, bottom, right - radius, bottom)
    path.lineTo(left + radius, bottom)
    path.quadTo(left, bottom, left, bottom - radius)
    path.lineTo(left, tab_top + radius)
    path.quadTo(left, tab_top, left + radius, tab_top)
    path.closeSubpath()
    return path


def _key_path(size: int) -> QPainterPath:
    """A key lying across the folder: ring on the left, teeth on the right."""
    axis = size * 0.565
    ring_cx = size * 0.345
    ring_r = size * 0.088
    shaft_to = size * 0.705
    tooth = size * 0.075

    path = QPainterPath()
    path.addEllipse(QRectF(ring_cx - ring_r, axis - ring_r, ring_r * 2, ring_r * 2))
    path.moveTo(ring_cx + ring_r * 0.6, axis)
    path.lineTo(shaft_to, axis)
    for x in (size * 0.605, size * 0.685):
        path.moveTo(x, axis)
        path.lineTo(x, axis + tooth)
    return path


def _draw_keeper(p: QPainter, size: int) -> None:
    """Draw the mark: a folder someone holds the key to.

    Below 32px the folder and the key cannot both survive - the strokes land on
    the same pixels and turn to mush - so the small frames carry the key alone,
    which is the half that still says "keeper" on its own.
    """
    white = QColor(255, 255, 255)
    line = max(1.0, size * 0.052)
    small = size < 32

    if not small:
        folder = _folder_path(size)
        p.setPen(QPen(QColor(255, 255, 255, 235), max(1.0, size * 0.038),
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.setBrush(QBrush(QColor(255, 255, 255, 40)))
        p.drawPath(folder)

        # A dark pass under the key lifts it off the folder it sits on.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(30, 27, 75, 150), line * 1.9,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.drawPath(_key_path(size))

    key = _key_path(size) if not small else _small_key_path(size)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(white, line, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    p.drawPath(key)


def _small_key_path(size: int) -> QPainterPath:
    """The 16px key: one bar, one ring, no teeth to lose."""
    axis = size * 0.5
    ring_cx = size * 0.33
    ring_r = size * 0.13
    path = QPainterPath()
    path.addEllipse(QRectF(ring_cx - ring_r, axis - ring_r, ring_r * 2, ring_r * 2))
    path.moveTo(ring_cx + ring_r, axis)
    path.lineTo(size * 0.78, axis)
    path.moveTo(size * 0.66, axis)
    path.lineTo(size * 0.66, axis + size * 0.16)
    return path


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    # Rounded gradient background.
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#38bdf8"))   # sky
    grad.setColorAt(0.55, QColor("#2563eb"))  # blue
    grad.setColorAt(1.0, QColor("#1e1b4b"))   # indigo
    radius = size * 0.24
    margin = max(0.5, size * 0.045)
    p.setBrush(QBrush(grad))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(
        QRectF(margin, margin, size - 2 * margin, size - 2 * margin),
        radius, radius,
    )

    _draw_keeper(p, size)
    p.end()
    return img


def _png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def write_ico(path: str) -> None:
    frames = [(s, _png_bytes(render(s))) for s in SIZES]

    out = BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(frames)))  # ICONDIR
    offset = 6 + 16 * len(frames)
    for size, data in frames:
        dim = 0 if size >= 256 else size
        out.write(struct.pack(
            "<BBBBHHII",
            dim, dim, 0, 0, 1, 32, len(data), offset,
        ))
        offset += len(data)
    for _size, data in frames:
        out.write(data)

    with open(path, "wb") as fh:
        fh.write(out.getvalue())


def main() -> int:
    QApplication(sys.argv)  # needed for QImage/QPainter
    write_ico("icon.ico")
    print("Wrote icon.ico with sizes:", ", ".join(str(s) for s in SIZES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
