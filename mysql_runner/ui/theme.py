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

from PyQt6.QtCore import Qt
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


DARK = Palette(
    bg="#0f1216",
    panel="#14181e",
    panel_alt="#1a1f27",
    card="#1e242d",
    border="#2b3240",
    border_soft="#232935",
    text="#e3e8ef",
    text_dim="#9aa4b2",
    text_faint="#6b7480",
    accent="#4a9eff",
    accent_soft="#1b2a3d",
    accent_text="#ffffff",
    selection="#24405f",
    green="#3ecf8e",
    amber="#f0a83c",
    red="#e5484d",
    scrollbar="#39414f",
)

LIGHT = Palette(
    bg="#f4f5f7",
    panel="#ffffff",
    panel_alt="#f7f8fa",
    card="#ffffff",
    border="#d3d8e0",
    border_soft="#e4e8ee",
    text="#1c2028",
    text_dim="#5b6472",
    text_faint="#8a929e",
    accent="#1f6feb",
    accent_soft="#e8f0fe",
    accent_text="#ffffff",
    selection="#cfe1fb",
    green="#1a7f4b",
    amber="#a15c00",
    red="#c62828",
    scrollbar="#c3c9d2",
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
"""


#: Row colours used to mark comparison verdicts, per theme.
def diff_colours(dark: bool) -> dict[str, str]:
    c = palette(dark)
    return {
        "same": c.green,
        "different": c.amber,
        "local_only": c.accent,
        "remote_only": "#a06bd0" if dark else "#75507b",
        "unknown": c.text_faint,
    }
