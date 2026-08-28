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
from PyQt6.QtWidgets import QFrame, QLabel, QSizePolicy, QWidget


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
    #: The one action worth taking here. Loud on purpose, and rationed to one
    #: control at a time - a second primary button is two, which is none.
    primary: str
    primary_hover: str
    primary_text: str
    #: The same hue at a whisper: the tint behind whatever the primary action
    #: is about to act on, so the button and its target are visibly a pair.
    primary_soft: str
    primary_edge: str
    #: Surface for a control that has lost its outline but must still look
    #: pressable. It cannot be ``card``: in the light theme card and panel are
    #: both white, so a chip painted in it disappears into the toolbar.
    chip: str
    chip_hover: str


# Graphite, both of them: every grey is a true neutral (equal RGB channels,
# no blue cast), and focus and selection read as brightness rather than as a
# colour. That much monochrome is deliberate and stays.
#
# What did not work was extending it to *actions*. When the primary action is
# also a grey, a toolbar of eight controls offers eight identical rectangles
# and says nothing about which one you came here to press - people click the
# first thing they recognise, which is how "Compare" became the first thing a
# new user did to a production server. So exactly one hue is allowed back in,
# and only ever on the action a screen is for. Everything else stays neutral,
# which is what keeps the one blue thing meaning "this one".
#
# The test it has to pass is the one an ATM passes: cover the text, and the
# screen should still tell you where to press. Colour is doing that job here,
# so it cannot be spent on decoration.
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
    primary="#3d7eff",
    primary_hover="#5590ff",
    primary_text="#ffffff",
    primary_soft="#141c2e",
    primary_edge="#2b4a86",
    chip="#202023",
    chip_hover="#2a2a2e",
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
    primary="#1b62e0",
    primary_hover="#1552c4",
    primary_text="#ffffff",
    primary_soft="#eaf1ff",
    primary_edge="#b7ceff",
    chip="#ececed",
    chip_hover="#e0e0e3",
)


def palette(dark: bool) -> Palette:
    return DARK if dark else LIGHT


def production_badge(explanation: str, parent: QWidget | None = None) -> QLabel:
    """The "PRODUCTION" marker: a badge, not a banner.

    It used to be a full-width red bar above everything, which spent a whole
    row and a great deal of colour restating something that does not change
    while you look at it. Loud is not the same as large: this is the only
    filled red in the window, it sits with the rest of the connection's status,
    and what it means goes in the tooltip where it can be read once.
    """
    label = QLabel("PRODUCTION", parent)
    label.setObjectName("badge")
    label.setToolTip(explanation)
    return label


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

    Kinds: ``back``, ``forward``, ``up``, ``down``, ``refresh``, ``browse``.
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
    elif kind == "browse":
        # The same folder the listing draws, in the navigation grey: a browse
        # button that looked like an arrow read as one more way to move up.
        _paint_folder(painter, QRectF(box).adjusted(0, scale, 0, -scale), colour)
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
    """The whole application's look, in one string.

    Three rules hold the sheet together, and everything below is an
    application of one of them:

    * **One radius scale.** 6px on anything you click or type into, 10px on
      anything that contains other things, and a pill is a pill. Mixed radii
      were most of why the window read as assembled rather than designed.
    * **Colour is a signal, never decoration.** Blue is the action a screen is
      for and appears at most once. Red is destruction or production. Green and
      amber are states. Everything else is a true neutral, which is what leaves
      those four able to mean something.
    * **A control shows its state by weight, not by moving.** Hover, checked
      and focus change fill or border on a box whose size never changes, so
      nothing under the pointer ever shifts.
    """
    c = palette(dark)
    return f"""
/* ----- base ----------------------------------------------------------- */
QWidget {{
    background: {c.bg};
    color: {c.text};
    font-size: 12px;
}}
QMainWindow, QDialog {{ background: {c.bg}; }}
/* Labels and tick-boxes must never paint a surface of their own. The base
   rule above applies to every QWidget, which includes them, so without this
   each one draws a band of window background across whatever it is sitting
   on - most visible as a stripe behind every row of a settings page. The
   three labels that *are* surfaces (status, badge, pill) name themselves and
   are matched more specifically further down. */
QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
QToolTip {{
    background: {c.card};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 6px;
    padding: 5px 8px;
}}

/* ----- menus ---------------------------------------------------------- */
QMenuBar {{
    background: {c.panel};
    border-bottom: 1px solid {c.border_soft};
    padding: 2px 4px;
}}
QMenuBar::item {{
    padding: 5px 10px;
    border-radius: 6px;
    color: {c.text_dim};
}}
QMenuBar::item:selected {{ background: {c.chip}; color: {c.text}; }}
QMenu {{
    background: {c.panel};
    border: 1px solid {c.border};
    border-radius: 10px;
    padding: 5px;
}}
QMenu::item {{
    padding: 6px 24px 6px 12px;
    border-radius: 6px;
    color: {c.text};
}}
QMenu::item:selected {{ background: {c.chip_hover}; color: {c.text}; }}
QMenu::item:disabled {{ color: {c.text_faint}; }}
QMenu::separator {{
    height: 1px;
    background: {c.border_soft};
    margin: 5px 8px;
}}

/* ----- text roles ----------------------------------------------------- */
QFrame#sep {{ background: {c.border_soft}; border: 0; max-height: 1px; }}
QFrame#sepv {{
    background: {c.border_soft};
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
    border-top: 1px solid {c.border_soft};
    padding: 5px 10px;
}}

/* A production marking is a fact about the connection, not an announcement
   that deserves a row of its own. It sits with the other status: small,
   filled - the only filled red in the window - and never repeated. */
QLabel#badge {{
    background: {c.red};
    color: #ffffff;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 9px;
}}
/* The connection's state. Three values, one place, one colour each; nothing
   else in the window may use these colours as decoration. */
QLabel#pill {{
    border: 1px solid {c.border};
    border-radius: 9px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
    color: {c.text_dim};
}}
QLabel#pill[state="busy"] {{ color: {c.amber}; border-color: {c.amber}; }}
QLabel#pill[state="ok"] {{ color: {c.green}; border-color: {c.green}; }}
QLabel#pill[state="fail"] {{ color: {c.red}; border-color: {c.red}; }}

/* ----- buttons -------------------------------------------------------- */
QPushButton {{
    background: {c.card};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 6px;
    padding: 5px 12px;
    min-height: 20px;
}}
QPushButton:hover:!disabled {{ background: {c.chip_hover}; border-color: {c.accent}; }}
QPushButton:pressed {{ background: {c.panel_alt}; }}
QPushButton:disabled {{ color: {c.text_faint}; border-color: {c.border_soft}; }}
QPushButton:checked {{
    background: {c.accent_soft};
    border-color: {c.accent};
    color: {c.text};
}}
QPushButton:focus {{ border-color: {c.primary}; }}
/* A dialog's accept button is by definition the action the dialog is for, so
   it gets the action colour without every dialog having to say so - but only
   inside a button box. Qt gives plain QPushButtons autoDefault inside a
   dialog, so a looser rule paints whichever one Qt happened to pick, which on
   the git-history window meant two blue buttons and therefore none. */
QDialogButtonBox QPushButton:default {{
    background: {c.primary};
    color: {c.primary_text};
    border-color: {c.primary};
    font-weight: 600;
}}
QDialogButtonBox QPushButton:default:hover:!disabled {{
    background: {c.primary_hover};
    border-color: {c.primary_hover};
}}
QDialogButtonBox QPushButton:default:disabled {{
    background: transparent;
    color: {c.text_faint};
    border: 1px dashed {c.primary_edge};
}}

/* The action a screen is for. At most one at a time - a second is two, which
   is none. */
QPushButton#primary {{
    background: {c.primary};
    color: {c.primary_text};
    border-color: {c.primary};
    font-weight: 600;
    padding: 5px 15px;
}}
QPushButton#primary:hover:!disabled {{
    background: {c.primary_hover};
    border-color: {c.primary_hover};
}}
QPushButton#primary:pressed {{ background: {c.primary}; }}
/* A disabled primary stays recognisably the primary: still where the eye
   should go, it just says why it cannot be pressed yet. */
QPushButton#primary:disabled {{
    background: transparent;
    color: {c.text_faint};
    border: 1px dashed {c.primary_edge};
}}
/* The same action when it is not the one being offered. */
QPushButton#secondary {{
    background: {c.chip};
    color: {c.text};
    border: 1px solid {c.primary_edge};
    border-radius: 6px;
    padding: 5px 15px;
}}
QPushButton#secondary:hover:!disabled {{
    background: {c.chip_hover};
    border-color: {c.primary};
}}
QPushButton#secondary:disabled {{
    background: transparent;
    color: {c.text_faint};
    border-color: {c.border_soft};
}}
/* Destructive things look destructive before they are hovered, not after. */
QPushButton#danger {{ color: {c.red}; border-color: {c.border}; }}
QPushButton#danger:hover:!disabled {{
    background: {c.red};
    border-color: {c.red};
    color: #ffffff;
}}
QPushButton#danger:disabled {{ color: {c.text_faint}; }}

QToolButton {{
    background: transparent;
    color: {c.text_dim};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 3px 6px;
}}
QToolButton:hover:!disabled {{ background: {c.chip_hover}; color: {c.text}; }}
QToolButton:disabled {{ color: {c.text_faint}; }}
QToolButton::menu-indicator {{ image: none; }}
QToolButton#menubutton {{
    background: {c.card};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 6px;
    padding: 5px 12px;
}}
QToolButton#menubutton:hover {{ background: {c.chip_hover}; border-color: {c.accent}; }}
QToolButton#tabclose {{
    color: {c.text_faint};
    font-size: 11px;
    padding: 0;
    border-radius: 8px;
}}
QToolButton#tabclose:hover {{ background: {c.red}; color: #ffffff; }}

/* ----- toolbars ------------------------------------------------------- */
QWidget#toolbar {{
    background: {c.panel};
    border-bottom: 1px solid {c.border_soft};
}}
QWidget#footerbar {{
    background: {c.panel};
    border-top: 1px solid {c.border_soft};
}}
/* Toolbar controls lose their outline but keep a surface. A row of eight
   outlined rectangles is eight things shouting equally, so the border goes -
   but taking the background as well leaves text that no longer looks
   pressable, and a control nobody can see is not an improvement on one that
   shouts. A quiet chip says "button" without competing with the one button
   that is the point of the screen. */
QWidget#toolbar QPushButton, QWidget#footerbar QPushButton,
QWidget#toolbar QToolButton#menubutton, QWidget#footerbar QToolButton#menubutton {{
    background: {c.chip};
    border-color: transparent;
}}
QWidget#toolbar QPushButton:hover:!disabled,
QWidget#footerbar QPushButton:hover:!disabled,
QWidget#toolbar QToolButton#menubutton:hover,
QWidget#footerbar QToolButton#menubutton:hover {{
    background: {c.chip_hover};
    border-color: {c.border};
}}
QWidget#toolbar QPushButton:checked, QWidget#footerbar QPushButton:checked {{
    background: {c.accent_soft};
    border-color: {c.border};
}}
QWidget#toolbar QPushButton:disabled, QWidget#footerbar QPushButton:disabled {{
    background: transparent;
    color: {c.text_faint};
}}
/* ...except the three that mean something. Restated here, and more
   specifically, so the quietening above cannot reach them. */
QWidget#toolbar QPushButton#primary, QWidget#footerbar QPushButton#primary {{
    background: {c.primary};
    border-color: {c.primary};
    color: {c.primary_text};
}}
QWidget#toolbar QPushButton#primary:hover:!disabled,
QWidget#footerbar QPushButton#primary:hover:!disabled {{
    background: {c.primary_hover};
    border-color: {c.primary_hover};
}}
QWidget#toolbar QPushButton#primary:disabled,
QWidget#footerbar QPushButton#primary:disabled {{
    background: transparent;
    color: {c.text_faint};
    border: 1px dashed {c.primary_edge};
}}
QWidget#toolbar QPushButton#secondary, QWidget#footerbar QPushButton#secondary {{
    background: {c.chip};
    border-color: {c.primary_edge};
}}
QWidget#toolbar QPushButton#secondary:hover:!disabled,
QWidget#footerbar QPushButton#secondary:hover:!disabled {{
    border-color: {c.primary};
}}
QWidget#toolbar QPushButton#secondary:disabled,
QWidget#footerbar QPushButton#secondary:disabled {{
    background: transparent;
    color: {c.text_faint};
    border-color: {c.border_soft};
}}
QWidget#toolbar QPushButton#danger, QWidget#footerbar QPushButton#danger {{
    color: {c.red};
    background: {c.chip};
    border-color: transparent;
}}
QWidget#toolbar QPushButton#danger:hover:!disabled,
QWidget#footerbar QPushButton#danger:hover:!disabled {{
    background: {c.red};
    border-color: {c.red};
    color: #ffffff;
}}

/* ----- the sidebar ---------------------------------------------------- */
/* The first thing anyone sees, and until now the least designed thing in the
   window: a bordered tree with 16px rows and a grey band for the selection.
   It is a list of places you go, so it is laid out like one - room to read,
   a rounded highlight that does not touch the edges, and group headings that
   look like headings rather than like the connections under them. */
QWidget#sidebarhost {{ background: {c.panel}; }}
QWidget#sidebar {{
    background: {c.panel};
    border-right: 1px solid {c.border_soft};
}}
QWidget#rail {{
    background: {c.panel};
    border-right: 1px solid {c.border_soft};
}}
QWidget#sidebar QLineEdit {{
    background: {c.panel_alt};
    border: 1px solid {c.border_soft};
    border-radius: 6px;
    padding: 5px 8px;
}}
QWidget#sidebar QLineEdit:focus {{ border-color: {c.primary}; }}
QWidget#sidebar QTreeWidget {{
    background: {c.panel};
    border: 0;
    outline: 0;
}}
QWidget#sidebar QTreeWidget::item {{
    border-radius: 6px;
    padding: 5px 6px 5px 10px;
    margin: 1px 4px;
    color: {c.text_dim};
}}
QWidget#sidebar QTreeWidget::item:hover {{
    background: {c.chip};
    color: {c.text};
}}
QWidget#sidebar QTreeWidget::item:selected {{
    background: {c.selection};
    color: {c.text};
}}
/* A group heading is not one of the things in the group. */
QWidget#sidebar QTreeWidget::item:has-children {{
    color: {c.text_faint};
    font-weight: 700;
    padding-left: 6px;
    margin-top: 8px;
}}
QWidget#sidebar QTreeWidget::item:has-children:selected {{
    background: transparent;
    color: {c.text_faint};
}}
/* The strip to the left of a row is the branch column, and Qt paints it
   with the row's own hover and selection colours - which showed up as a
   detached grey tab floating beside the current connection, belonging to
   nothing. It is never anything but background here: this tree is two levels
   deep, has no expander arrows worth drawing, and its rounded highlight is
   supposed to be one shape. */
QWidget#sidebar QTreeWidget::branch,
QWidget#sidebar QTreeWidget::branch:hover,
QWidget#sidebar QTreeWidget::branch:selected,
QWidget#sidebar QTreeWidget::branch:has-siblings,
QWidget#sidebar QTreeWidget::branch:has-children,
QWidget#sidebar QTreeWidget::branch:!has-children {{
    background: transparent;
    border-image: none;
    image: none;
}}
/* The sidebar's own buttons follow the toolbar rule: quiet chips, with the
   one that opens a connection wearing the action colour. */
QWidget#sidebar QPushButton {{
    background: {c.chip};
    border-color: transparent;
    padding: 5px 10px;
}}
QWidget#sidebar QPushButton:hover:!disabled {{
    background: {c.chip_hover};
    border-color: {c.border};
}}
QWidget#sidebar QPushButton:disabled {{
    background: transparent;
    color: {c.text_faint};
    border-color: transparent;
}}
QWidget#sidebar QPushButton#primary {{
    background: {c.primary};
    color: {c.primary_text};
    border-color: {c.primary};
    padding: 7px 12px;
}}
QWidget#sidebar QPushButton#primary:hover:!disabled {{
    background: {c.primary_hover};
    border-color: {c.primary_hover};
}}
QWidget#sidebar QPushButton#primary:disabled {{
    background: transparent;
    color: {c.text_faint};
    border: 1px dashed {c.primary_edge};
}}
QWidget#sidebar QPushButton#danger {{ color: {c.red}; background: {c.chip}; }}
QWidget#sidebar QPushButton#danger:hover:!disabled {{
    background: {c.red};
    border-color: {c.red};
    color: #ffffff;
}}
QWidget#sidebar QPushButton#danger:disabled {{
    background: transparent;
    color: {c.text_faint};
}}

/* ----- text entry ----------------------------------------------------- */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {c.panel_alt};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {c.selection};
    selection-color: {c.text};
}}
/* Focus is the one place a neutral was doing a job colour does better: on a
   form you need to see where the keyboard is without hunting. */
QLineEdit:focus, QSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {c.primary};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {c.text_faint};
    border-color: {c.border_soft};
}}
QComboBox::drop-down {{ border: 0; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {c.panel};
    border: 1px solid {c.border};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {c.chip_hover};
    selection-color: {c.text};
}}
/* A spin box has to look like one. With its steppers hidden it reads as a
   free text field, which is what made "30 days" ambiguous - a field you can
   type a sentence into invites you to try. Visible steppers, and the unit
   lives outside the box (see SettingsDialog._with_unit). */
QSpinBox::up-button, QSpinBox::down-button {{
    background: {c.chip};
    border: 0;
    border-left: 1px solid {c.border_soft};
    width: 16px;
}}
QSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    border-top-right-radius: 5px;
}}
QSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    border-bottom-right-radius: 5px;
    border-top: 1px solid {c.border_soft};
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background: {c.chip_hover};
}}
QSpinBox::up-arrow {{
    width: 0; height: 0;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid {c.text_dim};
}}
QSpinBox::down-arrow {{
    width: 0; height: 0;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid {c.text_dim};
}}

/* ----- checkboxes and radios ------------------------------------------ */
QCheckBox, QRadioButton {{ color: {c.text}; spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {c.border};
    background: {c.panel_alt};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {c.accent};
}}
/* Ticked is the affirmative answer, so it gets the affirmative colour. */
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {c.primary};
    border-color: {c.primary};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {c.border_soft};
    background: transparent;
}}

/* ----- tabs ----------------------------------------------------------- */
/* Tabs used to be little boxes with their own borders and radii sitting on a
   bordered pane - four edges to say one thing. A tab strip is a row of labels
   with one of them current; the underline says which, and the pane below is
   simply the background. */
QTabWidget::pane {{
    border: 0;
    border-top: 1px solid {c.border_soft};
    top: -1px;
    background: {c.bg};
}}
QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {c.text_faint};
    border: 0;
    border-bottom: 2px solid transparent;
    padding: 7px 14px;
    margin: 0 1px;
}}
QTabBar::tab:hover:!selected {{ color: {c.text_dim}; }}
QTabBar::tab:selected {{
    color: {c.text};
    border-bottom: 2px solid {c.primary};
    font-weight: 600;
}}
QTabBar::close-button {{ subcontrol-position: right; }}

/* ----- tables and trees ----------------------------------------------- */
/* No zebra striping: it is a spreadsheet tic that bands every row whether or
   not anything is happening in it, and it competes with the one row actually
   selected. Rows are separated by space and by the selection alone. */
QTableWidget, QTableView, QTreeWidget, QTreeView, QListWidget {{
    background: {c.panel};
    alternate-background-color: {c.panel};
    color: {c.text};
    border: 1px solid {c.border_soft};
    border-radius: 10px;
    gridline-color: transparent;
    selection-background-color: {c.selection};
    selection-color: {c.text};
    outline: 0;
}}
QTableWidget::item, QTreeWidget::item, QListWidget::item {{
    padding: 4px;
    border: 0;
}}
QTableWidget::item:selected, QTreeWidget::item:selected,
QListWidget::item:selected {{
    background: {c.selection};
    color: {c.text};
}}
QHeaderView {{ background: {c.panel}; }}
QHeaderView::section {{
    background: {c.panel};
    color: {c.text_faint};
    border: 0;
    border-bottom: 1px solid {c.border_soft};
    padding: 6px;
    font-weight: 600;
}}
QHeaderView::section:hover {{ color: {c.text_dim}; }}
QHeaderView::section:last {{ border-right: 0; }}

/* ----- group boxes ---------------------------------------------------- */
/* A settings page is a stack of these, so their borders are most of what it
   looks like. Soft edge, roomier inside, and the legend reads as a label
   rather than as a notch cut out of a frame. */
QGroupBox {{
    border: 1px solid {c.border_soft};
    border-radius: 10px;
    margin-top: 12px;
    padding: 14px 12px 10px;
    background: {c.panel};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
    color: {c.text_faint};
    font-weight: 700;
}}

/* ----- progress ------------------------------------------------------- */
QProgressBar {{
    background: {c.panel_alt};
    border: 1px solid {c.border_soft};
    border-radius: 6px;
    height: 8px;
    text-align: center;
    color: {c.text_dim};
}}
/* Work in flight is the action of the moment, so it wears the action colour. */
QProgressBar::chunk {{ background: {c.primary}; border-radius: 5px; }}

/* ----- splitters, scrollbars, status bar ------------------------------ */
QSplitter::handle {{ background: {c.border_soft}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QSplitter::handle:hover {{ background: {c.accent}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {c.scrollbar};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {c.scrollbar};
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::handle:hover {{ background: {c.text_faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QStatusBar {{
    background: {c.panel};
    border-top: 1px solid {c.border_soft};
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
QLineEdit:focus {{ border-color: {c.primary}; }}
QLabel#prompt {{ color: {c.green}; font-weight: 600; }}
"""


def pane_stylesheet(dark: bool) -> str:
    """The file-manager panes: listing tables and their path bars."""
    c = palette(dark)
    return f"""
QTableWidget {{
    background: {c.panel};
    alternate-background-color: {c.panel};
    color: {c.text};
    border: 1px solid {c.border_soft};
    border-radius: 10px;
    selection-background-color: {c.selection};
    selection-color: {c.text};
}}
QHeaderView::section {{
    background: {c.panel};
    color: {c.text_faint};
    border: 0;
    border-bottom: 1px solid {c.border_soft};
    padding: 6px 6px;
    font-weight: 600;
}}
QLineEdit {{
    background: {c.panel_alt};
    color: {c.text};
    border: 1px solid {c.border_soft};
    border-radius: 6px;
    padding: 4px 7px;
}}
QLineEdit:focus {{ border-color: {c.primary}; }}
QWidget#notice {{
    background: {c.card};
    border: 1px solid {c.border_soft};
    border-left: 3px solid {c.amber};
    border-radius: 10px;
}}
QWidget#notice QLabel {{ background: transparent; color: {c.text}; }}
QWidget#notice QCheckBox {{ background: transparent; color: {c.text_dim}; }}
QWidget#pathbar {{
    background: {c.panel_alt};
    border: 1px solid {c.border_soft};
    border-radius: 6px;
}}
/* The server side carries the same colour as the button that sends things
   there, so the loud thing in the corner and the place it aims at are
   visibly one pair - and on a production connection that edge is the red
   one instead, because the answer to "which pane is live?" should not
   depend on reading anything. */
QWidget#pathbar[side="remote"] {{ border-left: 3px solid {c.primary}; }}
QWidget#pathbar[side="remote"][live="true"] {{ border-left: 3px solid {c.red}; }}
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
