"""One place that decides what the app looks like.

Styling used to be scattered: three tabs each carried their own dark-mode CSS,
dialogs hard-coded grey hints, and nothing drew a line between the toolbars and
the content, so the window read as one undifferentiated grey field. This module
holds a small palette and one stylesheet built from it, applied once to the
window, so every tab, dialog and table matches and sections actually look like
sections.

Widgets opt into a role with ``setObjectName``:

``title``      a bold section heading
``hint``       small grey explanatory text
``status``     the footer status line (top border, dimmer background)
``banner``     the production warning strip
``toolbar``    a row of controls with a bottom border
``footerbar``  a row of controls with a top border
``sep``        a one-pixel divider (see :func:`divider`)
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QFrame, QSizePolicy, QWidget


@dataclass(frozen=True)
class Palette:
    """Every colour the app uses, so nothing invents its own."""

    bg: str
    panel: str
    panel_alt: str
    card: str
    border: str
    border_soft: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_soft: str
    accent_text: str
    selection: str
    green: str
    amber: str
    red: str
    scrollbar: str


# Graphite, both of them: every grey is a true neutral (equal RGB channels,
# no blue cast) and the accent is monochrome - a light graphite in the dark
# theme, a dark one in the light theme. Focus, selection and primary actions
# read as brightness, not as a colour; the only colours left are the
# semantic ones (green/amber/red) and they mean something every time.
DARK = Palette(
    bg="#0e0e0f",
    panel="#141415",
    panel_alt="#1a1a1c",
    card="#202023",
    border="#333338",
    border_soft="#28282c",
    text="#e6e6e8",
    text_dim="#a2a2a8",
    text_faint="#6e6e74",
    accent="#c9c9ce",
    accent_soft="#2a2a2e",
    accent_text="#141415",
    selection="#36363c",
    green="#3ecf8e",
    amber="#f0a83c",
    red="#e5484d",
    scrollbar="#3d3d42",
)

LIGHT = Palette(
    bg="#f4f4f5",
    panel="#ffffff",
    panel_alt="#f7f7f8",
    card="#ffffff",
    border="#d6d6d9",
    border_soft="#e6e6e9",
    text="#1b1b1d",
    text_dim="#5f5f66",
    text_faint="#8f8f96",
    accent="#3a3a3f",
    accent_soft="#e9e9eb",
    accent_text="#ffffff",
    selection="#dddde0",
    green="#1a7f4b",
    amber="#a15c00",
    red="#c62828",
    scrollbar="#c6c6cb",
)


def palette(dark: bool) -> Palette:
    return DARK if dark else LIGHT


def divider(*, vertical: bool = False, parent: QWidget | None = None) -> QFrame:
    """A one-pixel separator line, coloured by the stylesheet."""
    line = QFrame(parent)
    line.setObjectName("sepv" if vertical else "sep")
    line.setFrameShape(
        QFrame.Shape.VLine if vertical else QFrame.Shape.HLine
    )
    line.setFrameShadow(QFrame.Shadow.Plain)
    if vertical:
        line.setFixedWidth(1)
        # Without an explicit policy the frame has no height to paint in a
        # horizontal layout, and the separator silently does not appear.
        line.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        line.setMinimumHeight(18)
    else:
        line.setFixedHeight(1)
        line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return line


def nav_icon(kind: str, dark: bool, *, size: int = 14) -> QIcon:
    """A small navigation glyph, painted rather than typed.

    Two earlier attempts failed for opposite reasons: text arrows ("◀", "⟳")
    are missing from some Windows UI fonts and came out blank or as a stray
    letter, and Qt's own standard icons are drawn in a fixed dark grey that
    disappears against a dark window. Painting them means they always show and
    always match the theme.
    """
    colour = QColor(palette(dark).text_dim)
    scale = 2  # draw at 2x so the edges stay crisp when scaled down
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    box = pixmap.rect().adjusted(scale * 2, scale * 2, -scale * 2, -scale * 2)
    if kind == "refresh":
        _paint_refresh(painter, box, colour, scale)
    else:
        _paint_triangle(painter, box, colour, kind)
    painter.end()
    return QIcon(pixmap)


def _paint_triangle(painter: "QPainter", box, colour: QColor, kind: str) -> None:
    left, top, right, bottom = box.left(), box.top(), box.right(), box.bottom()
    middle_x = (left + right) / 2
    middle_y = (top + bottom) / 2
    points = {
        "back": ((right, top), (right, bottom), (left, middle_y)),
        "forward": ((left, top), (left, bottom), (right, middle_y)),
        "up": ((left, bottom), (right, bottom), (middle_x, top)),
        "down": ((left, top), (right, top), (middle_x, bottom)),
    }[kind]
    path = QPainterPath()
    path.moveTo(*points[0])
    for point in points[1:]:
        path.lineTo(*point)
    path.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(colour)
    painter.drawPath(path)


def _paint_refresh(painter: "QPainter", box, colour: QColor, scale: int) -> None:
    """A circular arrow: an open arc plus a small arrowhead."""
    pen = QPen(colour)
    pen.setWidth(int(2.1 * scale))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawArc(box, 70 * 16, 260 * 16)
    head = QPainterPath()
    tip_x = box.center().x() + box.width() * 0.28
    tip_y = box.top() + box.height() * 0.10
    span = 2.8 * scale
    head.moveTo(tip_x - span, tip_y)
    head.lineTo(tip_x + span, tip_y)
    head.lineTo(tip_x, tip_y + span * 1.6)
    head.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(colour)
    painter.drawPath(head)


#: Painted listing icons are cached: a directory of a thousand rows must not
#: paint a thousand pixmaps.
_entry_icon_cache: dict[tuple[str, bool, int], QIcon] = {}


def entry_icon(kind: str, dark: bool, *, size: int = 16) -> QIcon:
    """A listing glyph: a folder, or a file tinted by what kind of file it is.

    Painted for the same reason as :func:`nav_icon` - and because remote
    entries have no real path on disk for the native icon provider to look at,
    so painting is also what keeps the two panes looking the same.

    Kinds: ``folder``, ``file``, ``file-code``, ``file-image``,
    ``file-archive``.
    """
    key = (kind, dark, size)
    cached = _entry_icon_cache.get(key)
    if cached is not None:
        return cached
    c = palette(dark)
    scale = 2  # draw at 2x so the edges stay crisp when scaled down
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    box = QRectF(pixmap.rect().adjusted(scale, scale * 2, -scale, -scale * 2))
    if kind == "folder":
        _paint_folder(painter, box, QColor(c.amber))
    else:
        tint = {
            # Steel rather than the accent: the accent is a grey now, and
            # code files deserve to stand out from plain ones.
            "file-code": "#7f9db8" if dark else "#4a6d8c",
            "file-image": c.green,
            "file-archive": c.amber,
        }.get(kind, c.text_faint)
        _paint_file(painter, box, QColor(c.text_dim), QColor(tint), scale)
    painter.end()
    icon = QIcon(pixmap)
    _entry_icon_cache[key] = icon
    return icon


def _paint_folder(painter: "QPainter", box: QRectF, colour: QColor) -> None:
    """A filled folder: the tab on top of the body, drawn as one shape."""
    radius = box.height() * 0.14
    tab = QRectF(box.left(), box.top(), box.width() * 0.44, box.height() * 0.40)
    body = QRectF(
        box.left(), box.top() + box.height() * 0.18,
        box.width(), box.height() * 0.82,
    )
    shape = QPainterPath()
    shape.addRoundedRect(tab, radius, radius)
    shape.addRoundedRect(body, radius, radius)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(colour)
    painter.drawPath(shape.simplified())


def _paint_file(
    painter: "QPainter", box: QRectF, outline: QColor, tint: QColor, scale: int
) -> None:
    """A page with a folded corner and two content lines in the type's tint."""
    inset = box.width() * 0.12  # pages are taller than wide
    left, right = box.left() + inset, box.right() - inset
    fold = (right - left) * 0.38
    page = QPainterPath()
    page.moveTo(left, box.top())
    page.lineTo(right - fold, box.top())
    page.lineTo(right, box.top() + fold)
    page.lineTo(right, box.bottom())
    page.lineTo(left, box.bottom())
    page.closeSubpath()
    pen = QPen(outline)
    pen.setWidthF(1.3 * scale)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(page)
    painter.drawLine(
        QPointF(right - fold, box.top()), QPointF(right - fold, box.top() + fold)
    )
    painter.drawLine(
        QPointF(right - fold, box.top() + fold), QPointF(right, box.top() + fold)
    )
    lines = QPen(tint)
    lines.setWidthF(1.6 * scale)
    lines.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(lines)
    text_left = left + (right - left) * 0.22
    text_right = right - (right - left) * 0.22
    for fraction in (0.58, 0.78):
        y = box.top() + box.height() * fraction
        painter.drawLine(QPointF(text_left, y), QPointF(text_right, y))


#: Connection-kind glyphs are cached like the listing icons.
_kind_icon_cache: dict[tuple[str, bool, int], QIcon] = {}


def kind_icon(kind: str, dark: bool, *, size: int = 16) -> QIcon:
    """The glyph for one connection kind, painted like every other icon.

    ``phpmyadmin`` is a globe (it is a website), ``mysql`` a database
    cylinder, ``ftp`` a pair of transfer arrows, and ``ftps``/``sftp`` the
    same arrows carrying a green padlock - encryption being the difference
    worth seeing at a glance.
    """
    key = (kind, dark, size)
    cached = _kind_icon_cache.get(key)
    if cached is not None:
        return cached
    c = palette(dark)
    scale = 2  # draw at 2x so the edges stay crisp when scaled down
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    box = QRectF(pixmap.rect().adjusted(scale * 2, scale * 2, -scale * 2, -scale * 2))
    colour = QColor(c.text_dim)
    if kind == "phpmyadmin":
        _paint_globe(painter, box, colour, scale)
    elif kind == "mysql":
        _paint_database(painter, box, colour, scale)
    else:
        _paint_transfer(
            painter, box, colour, scale,
            lock=QColor(c.green) if kind in ("ftps", "sftp") else None,
        )
    painter.end()
    icon = QIcon(pixmap)
    _kind_icon_cache[key] = icon
    return icon


def _paint_globe(painter: "QPainter", box: QRectF, colour: QColor, scale: int) -> None:
    """A globe: the circle, the equator, and one vertical meridian."""
    pen = QPen(colour)
    pen.setWidthF(1.5 * scale)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(box)
    middle_y = box.center().y()
    painter.drawLine(QPointF(box.left(), middle_y), QPointF(box.right(), middle_y))
    meridian = QRectF(
        box.center().x() - box.width() * 0.19, box.top(),
        box.width() * 0.38, box.height(),
    )
    painter.drawEllipse(meridian)


def _paint_database(painter: "QPainter", box: QRectF, colour: QColor, scale: int) -> None:
    """A database cylinder: top ellipse, sides, and two belly arcs."""
    pen = QPen(colour)
    pen.setWidthF(1.5 * scale)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    lid_height = box.height() * 0.30
    inset = box.width() * 0.08
    left, right = box.left() + inset, box.right() - inset
    lid = QRectF(left, box.top(), right - left, lid_height)
    painter.drawEllipse(lid)
    bottom_arc = QRectF(left, box.bottom() - lid_height, right - left, lid_height)
    painter.drawLine(QPointF(left, lid.center().y()),
                     QPointF(left, bottom_arc.center().y()))
    painter.drawLine(QPointF(right, lid.center().y()),
                     QPointF(right, bottom_arc.center().y()))
    # Belly and base arcs: the lower half of an ellipse each.
    middle_arc = QRectF(left, box.center().y() - lid_height * 0.75,
                        right - left, lid_height)
    for rect in (middle_arc, bottom_arc):
        painter.drawArc(rect, 180 * 16, 180 * 16)


def _paint_transfer(
    painter: "QPainter", box: QRectF, colour: QColor, scale: int,
    *, lock: QColor | None = None,
) -> None:
    """Two arrows passing each other: up on the left, down on the right."""
    pen = QPen(colour)
    pen.setWidthF(1.8 * scale)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    head = box.width() * 0.17
    up_x = box.left() + box.width() * 0.30
    down_x = box.left() + box.width() * 0.70
    top, bottom = box.top(), box.bottom()
    painter.drawLine(QPointF(up_x, bottom), QPointF(up_x, top))
    painter.drawLine(QPointF(up_x - head, top + head), QPointF(up_x, top))
    painter.drawLine(QPointF(up_x + head, top + head), QPointF(up_x, top))
    painter.drawLine(QPointF(down_x, top), QPointF(down_x, bottom))
    painter.drawLine(QPointF(down_x - head, bottom - head), QPointF(down_x, bottom))
    painter.drawLine(QPointF(down_x + head, bottom - head), QPointF(down_x, bottom))
    if lock is None:
        return
    # A small padlock badges the lower-right corner on the encrypted kinds.
    # A transparent knockout is punched behind it first, so the badge sits
    # cleanly on the arrows whatever the background.
    body_w = box.width() * 0.52
    body_h = box.height() * 0.38
    body = QRectF(box.right() - body_w, box.bottom() - body_h, body_w, body_h)
    shackle = QRectF(
        body.center().x() - body_w * 0.28, body.top() - body_h * 0.55,
        body_w * 0.56, body_h * 0.80,
    )
    pad = 1.6 * scale
    knockout = body.united(shackle).adjusted(-pad, -pad, pad, pad)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0))
    painter.drawRoundedRect(knockout, pad * 2, pad * 2)
    painter.setCompositionMode(
        QPainter.CompositionMode.CompositionMode_SourceOver
    )
    pen = QPen(lock)
    pen.setWidthF(1.6 * scale)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawArc(shackle, 0, 180 * 16)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(lock)
    radius = body_h * 0.25
    painter.drawRoundedRect(body, radius, radius)


def app_stylesheet(dark: bool) -> str:
    """The whole application's look, in one string."""
    c = palette(dark)
    return f"""
/* ----- base ----- */
QWidget {{
    background: {c.bg};
    color: {c.text};
    font-size: 12px;
}}
QMainWindow, QDialog {{ background: {c.bg}; }}
QToolTip {{
    background: {c.card};
    color: {c.text};
    border: 1px solid {c.border};
    padding: 4px 6px;
}}

/* ----- menu bar: a line under it, so it is not floating ----- */
QMenuBar {{
    background: {c.panel};
    border-bottom: 1px solid {c.border};
    padding: 2px 4px;
}}
QMenuBar::item {{
    padding: 5px 10px;
    border-radius: 6px;
    color: {c.text_dim};
}}
QMenuBar::item:selected {{ background: {c.card}; color: {c.text}; }}
QMenu {{
    background: {c.panel};
    border: 1px solid {c.border};
    padding: 4px;
}}
QMenu::item {{ padding: 5px 22px 5px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
QMenu::item:disabled {{ color: {c.text_faint}; }}
QMenu::separator {{
    height: 1px;
    background: {c.border_soft};
    margin: 4px 6px;
}}

/* ----- roles ----- */
QFrame#sep {{ background: {c.border}; border: 0; max-height: 1px; }}
QFrame#sepv {{
    background: {c.border};
    border: 0;
    min-width: 1px;
    max-width: 1px;
}}
QLabel#title {{ font-weight: 600; color: {c.text}; }}
QLabel#hint {{ color: {c.text_dim}; }}
QLabel#warning {{ color: {c.red}; }}
QLabel#status {{
    color: {c.text_dim};
    background: {c.panel};
    border-top: 1px solid {c.border};
    padding: 5px 8px;
}}
QLabel#banner {{
    background: {c.red};
    color: #ffffff;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 5px;
}}
QWidget#toolbar {{
    background: {c.panel};
    border-bottom: 1px solid {c.border};
}}
QWidget#footerbar {{
    background: {c.panel};
    border-top: 1px solid {c.border};
}}
QWidget#sidebar {{
    background: {c.panel};
    border-right: 1px solid {c.border};
}}
QWidget#rail {{
    background: {c.panel};
    border-right: 1px solid {c.border};
}}

/* ----- buttons ----- */
QPushButton {{
    background: {c.card};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 7px;
    padding: 5px 11px;
    min-height: 20px;
}}
QPushButton:hover:!disabled {{ border-color: {c.accent}; }}
QPushButton:pressed {{ background: {c.panel_alt}; }}
QPushButton:disabled {{ color: {c.text_faint}; border-color: {c.border_soft}; }}
QPushButton:checked {{
    background: {c.accent_soft};
    border-color: {c.accent};
    color: {c.text};
}}
QPushButton:default {{ border-color: {c.accent}; }}
QPushButton#primary {{
    background: {c.accent};
    color: {c.accent_text};
    border-color: {c.accent};
    font-weight: 600;
}}
QPushButton#danger:hover:!disabled {{ border-color: {c.red}; color: {c.red}; }}
QToolButton {{
    background: transparent;
    color: {c.text_dim};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px 6px;
}}
QToolButton:hover:!disabled {{ background: {c.card}; color: {c.text}; }}
QToolButton:disabled {{ color: {c.text_faint}; }}
QToolButton::menu-indicator {{ image: none; }}
QToolButton#menubutton {{
    background: {c.card};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 7px;
    padding: 5px 11px;
}}
QToolButton#menubutton:hover {{ border-color: {c.accent}; }}
QToolButton#tabclose {{
    color: {c.text_faint};
    font-size: 11px;
    padding: 0;
    border-radius: 8px;
}}
QToolButton#tabclose:hover {{ background: {c.red}; color: #ffffff; }}

/* ----- text entry ----- */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {c.panel_alt};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 7px;
    padding: 4px 7px;
    selection-background-color: {c.selection};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border-color: {c.accent};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {c.text_faint};
    background: {c.panel};
}}
QComboBox::drop-down {{ border: 0; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {c.panel};
    border: 1px solid {c.border};
    selection-background-color: {c.accent_soft};
    selection-color: {c.text};
}}

/* ----- checkboxes ----- */
QCheckBox, QRadioButton {{ color: {c.text}; spacing: 6px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {c.border};
    border-radius: 4px;
    background: {c.panel_alt};
}}
QCheckBox::indicator:checked {{
    background: {c.accent};
    border-color: {c.accent};
}}
QCheckBox::indicator:disabled {{ border-color: {c.border_soft}; }}

/* ----- tabs ----- */
QTabWidget::pane {{
    border: 1px solid {c.border};
    border-radius: 8px;
    top: -1px;
    background: {c.panel};
}}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: {c.panel_alt};
    color: {c.text_dim};
    border: 1px solid {c.border_soft};
    border-bottom: 0;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 6px 12px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {c.panel};
    color: {c.text};
    border-color: {c.border};
}}
QTabBar::tab:hover:!selected {{ color: {c.text}; }}
QTabBar::close-button {{ subcontrol-position: right; }}
QTabBar::tab:!selected {{ margin-top: 2px; }}

/* ----- tables and trees ----- */
QTableWidget, QTableView, QTreeWidget, QTreeView, QListWidget {{
    background: {c.panel};
    alternate-background-color: {c.panel_alt};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 8px;
    gridline-color: {c.border_soft};
    selection-background-color: {c.selection};
    selection-color: {c.text};
}}
QTableWidget::item, QTreeWidget::item, QListWidget::item {{
    padding: 3px 4px;
    border: 0;
}}
QTableWidget::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {{
    background: {c.selection};
    color: {c.text};
}}
QHeaderView {{ background: {c.panel_alt}; }}
QHeaderView::section {{
    background: {c.panel_alt};
    color: {c.text_dim};
    border: 0;
    border-bottom: 1px solid {c.border};
    border-right: 1px solid {c.border_soft};
    padding: 5px 6px;
    font-weight: 600;
}}
QHeaderView::section:last {{ border-right: 0; }}

/* ----- group boxes ----- */
QGroupBox {{
    border: 1px solid {c.border};
    border-radius: 8px;
    margin-top: 10px;
    padding: 10px 10px 6px;
    background: {c.panel};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {c.text_dim};
    font-weight: 600;
}}

/* ----- progress ----- */
QProgressBar {{
    background: {c.panel_alt};
    border: 1px solid {c.border_soft};
    border-radius: 6px;
    height: 8px;
    text-align: center;
    color: {c.text_dim};
}}
QProgressBar::chunk {{ background: {c.accent}; border-radius: 5px; }}

/* ----- splitters, scrollbars, status bar ----- */
QSplitter::handle {{ background: {c.border_soft}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QSplitter::handle:hover {{ background: {c.accent}; }}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {c.scrollbar};
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {c.scrollbar};
    border-radius: 5px;
    min-width: 24px;
}}
QScrollBar::handle:hover {{ background: {c.text_faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QStatusBar {{
    background: {c.panel};
    border-top: 1px solid {c.border};
    color: {c.text_dim};
}}
QStatusBar::item {{ border: 0; }}
"""


def console_stylesheet(dark: bool) -> str:
    """The monospace surfaces: SQL console, shell, log viewer."""
    c = palette(dark)
    return f"""
QPlainTextEdit {{
    background: {c.bg if dark else c.panel};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 8px;
    selection-background-color: {c.selection};
}}
QLineEdit {{
    background: {c.panel_alt};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 7px;
    padding: 5px 8px;
}}
QLineEdit:focus {{ border-color: {c.accent}; }}
QLabel#prompt {{ color: {c.green}; font-weight: 600; }}
"""


def pane_stylesheet(dark: bool) -> str:
    """The file-manager panes: listing tables and their path bars."""
    c = palette(dark)
    return f"""
QTableWidget {{
    background: {c.panel};
    alternate-background-color: {c.panel_alt};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 8px;
    selection-background-color: {c.selection};
    selection-color: {c.text};
}}
QHeaderView::section {{
    background: {c.panel_alt};
    color: {c.text_dim};
    border: 0;
    border-bottom: 1px solid {c.border};
    padding: 5px 6px;
    font-weight: 600;
}}
QLineEdit {{
    background: {c.panel_alt};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 7px;
    padding: 4px 7px;
}}
QLineEdit:focus {{ border-color: {c.accent}; }}
QWidget#notice {{
    background: {c.card};
    border: 1px solid {c.border};
    border-left: 3px solid {c.amber};
    border-radius: 8px;
}}
QWidget#notice QLabel {{ background: transparent; color: {c.text}; }}
QWidget#notice QCheckBox {{ background: transparent; color: {c.text_dim}; }}
QWidget#pathbar {{
    background: {c.panel_alt};
    border: 1px solid {c.border};
    border-radius: 7px;
}}
QWidget#pathbar QToolButton {{
    background: transparent;
    color: {c.text};
    border: 0;
    border-radius: 5px;
    padding: 2px 5px;
}}
QWidget#pathbar QToolButton:hover {{ background: {c.card}; color: {c.accent}; }}
QWidget#pathbar QLabel {{
    background: transparent;
    color: {c.text_faint};
    padding: 0 1px;
}}
"""


#: Row colours used to mark comparison verdicts, per theme.
def diff_colours(dark: bool) -> dict[str, str]:
    c = palette(dark)
    return {
        "same": c.green,
        "different": c.amber,
        # Deliberately not the accent: since the palette went monochrome the
        # accent is a grey, and a grey verdict mark would vanish among the
        # text. A muted steel stays legible without shouting.
        "local_only": "#7f9db8" if dark else "#4a6d8c",
        "remote_only": "#a06bd0" if dark else "#75507b",
        "unknown": c.text_faint,
    }
