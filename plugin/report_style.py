"""
report_style.py
---------------
The review deck's presentation layer: the palette, the type scale and the
slides that carry no measured data of their own — cover, agenda, section
dividers, the study/process card, and the closing "Points to highlight".

WHAT THIS IS NOT
----------------
It is not a new report generator. `cad_diagnostics.build_pptx_report()` still
owns the pipeline, the slide ORDER, the screenshots and — critically — the
embedded animations, and it calls in here only for the slides that are pure
presentation. Nothing in this file touches a picture, a movie or a poster
frame, so the animation path cannot be affected by anything here.

WHY A SEPARATE MODULE
---------------------
These slides are the ones worth iterating on visually, and they are the only
ones that can be rendered without Synergy, COM or a solved study. Keeping them
here means the look can be checked offline against real report images (see
`__main__`) instead of only after a full analysis run.

The design follows a reference deck an Autodesk engineer wrote by hand: a dark
cover and dark section dividers against light content slides, an eyebrow label
above each heading, numbered circles as the one repeated motif, and figures
given room rather than packed into a grid. None of its content is reused — only
its structure and its restraint.
"""

from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

# -- palette ---------------------------------------------------------------- #
# Unchanged from the deck this replaces, deliberately: Moldflow plots are
# full-spectrum rainbow, so the deck around them stays achromatic and lets the
# screenshots carry the colour. One accent, used for labels and markers only.
INK = RGBColor(0x1F, 0x29, 0x37)      # headings, dark slide ground
BODY = RGBColor(0x37, 0x41, 0x51)     # body copy
MUTED = RGBColor(0x6B, 0x72, 0x80)    # captions, secondary
ACCENT = RGBColor(0x0F, 0x76, 0x6E)   # eyebrow labels, markers
PAPER = RGBColor(0xFF, 0xFF, 0xFF)
PANEL = RGBColor(0xF4, 0xF5, 0xF7)    # card fill on light slides
DARK_PANEL = RGBColor(0x2A, 0x35, 0x45)   # card fill on dark slides
DIM = RGBColor(0x9C, 0xA3, 0xAF)      # body copy on dark slides

LEVEL_INK = {"red": RGBColor(0xB9, 0x1C, 0x1C),
             "amber": RGBColor(0xB4, 0x53, 0x09),
             "green": RGBColor(0x15, 0x6F, 0x3B)}
LEVEL_TINT = {"red": RGBColor(0xFE, 0xE2, 0xE2),
              "amber": RGBColor(0xFE, 0xF3, 0xC7),
              "green": RGBColor(0xE6, 0xF4, 0xEA)}

FONT = "Calibri"

SW, SH = Inches(13.333), Inches(7.5)      # 16:9
M = Inches(0.55)                          # page margin, as the deck already uses
CONTENT_W = SW - 2 * M


# --------------------------------------------------------------------------- #
#  Primitives
# --------------------------------------------------------------------------- #

def txbox(slide, x, y, w, h):
    """A text box with no internal padding, so text aligns with a shape at the
    same x rather than sitting a few points inside it."""
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = 0
    tf.margin_top = tf.margin_bottom = 0
    return tf


def para(tf, first, text="", size=11, bold=False, color=BODY,
         space_before=0, space_after=4, bullet=False, italic=False,
         align=None, line_spacing=None):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.space_before = Pt(space_before)
    p.space_after = Pt(space_after)
    if align is not None:
        p.alignment = align
    if line_spacing:
        p.line_spacing = line_spacing
    r = p.add_run()
    r.text = ("•  " + text) if bullet else text
    f = r.font
    f.name, f.size, f.bold, f.italic = FONT, Pt(size), bold, italic
    f.color.rgb = color
    return p


def rect(slide, x, y, w, h, fill, shape=MSO_SHAPE.RECTANGLE, radius=None):
    sh = slide.shapes.add_shape(shape, x, y, w, h)
    sh.fill.solid()
    sh.fill.fore_color.rgb = fill
    sh.line.fill.background()
    sh.shadow.inherit = False
    if radius is not None and shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        sh.adjustments[0] = radius
    sh.text_frame.text = ""
    return sh


def circle(slide, x, y, d, label, fill=ACCENT, color=PAPER, size=12):
    """The deck's one repeated motif: a filled circle carrying a number or a
    letter. Used for the agenda, the section dividers and the recommendations,
    so those three read as the same document."""
    sh = slide.shapes.add_shape(MSO_SHAPE.OVAL, x, y, d, d)
    sh.fill.solid()
    sh.fill.fore_color.rgb = fill
    sh.line.fill.background()
    sh.shadow.inherit = False
    tf = sh.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = str(label)
    r.font.name, r.font.size, r.font.bold = FONT, Pt(size), True
    r.font.color.rgb = color
    return sh


def fit(text, budget, base, floor=9):
    """Step a font down rather than let text spill its box. `budget` is roughly
    how many characters fit at `base` pt."""
    n = len(text or "")
    return base if n <= budget else max(floor, int(base * budget / float(n)))


def heading(slide, text, sub=None, eyebrow=None, size=28):
    """The standard light-slide heading: small accent eyebrow, big title, one
    muted line of context. No rule under the title — whitespace separates."""
    y = Inches(0.34)
    if eyebrow:
        tf = txbox(slide, M, y, CONTENT_W, Inches(0.3))
        para(tf, True, eyebrow.upper(), size=10, bold=True, color=ACCENT,
             space_after=0)
        y = Inches(0.62)
    tf = txbox(slide, M, y, CONTENT_W, Inches(0.75))
    para(tf, True, text, size=size, bold=True, color=INK, space_after=2)
    if sub:
        para(tf, False, sub, size=10, color=MUTED, italic=True, space_before=2)
    # Where content may start without colliding with the block above.
    return Inches(1.62) if (eyebrow or sub) else Inches(1.35)


# --------------------------------------------------------------------------- #
#  Slides
# --------------------------------------------------------------------------- #

def cover(slide, title, subtitle, facts, footnote="", prepared_by=""):
    """Dark cover. `facts` is [(label, value)] — study, sequence, material,
    mesh, date, what was exported."""
    rect(slide, 0, 0, SW, SH, INK)
    # One soft mark, bleeding off the TOP corner. It used to sit bottom-right,
    # where it landed under the footer logo and swallowed the strapline.
    rect(slide, SW - Inches(2.0), -Inches(1.4), Inches(3.6), Inches(3.6),
         DARK_PANEL, MSO_SHAPE.OVAL)

    tf = txbox(slide, Inches(0.9), Inches(1.15), Inches(11.0), Inches(0.35))
    para(tf, True, "MOLDFLOW SIMULATION REPORT", size=12, bold=True,
         color=ACCENT, space_after=0)

    tf = txbox(slide, Inches(0.9), Inches(1.7), Inches(10.6), Inches(1.5))
    para(tf, True, title, size=fit(title, 44, 40), bold=True, color=PAPER,
         space_after=6)
    para(tf, False, subtitle, size=15, color=DIM)

    # Facts as a two-column grid rather than a stacked list: the same six lines
    # read faster side by side and leave the lower third clear.
    top, col_w, row_h = Inches(3.45), Inches(5.3), Inches(0.62)
    for i, (label, value) in enumerate(facts[:8]):
        x = Inches(0.9) + col_w * (i % 2)
        y = top + row_h * (i // 2)
        tf = txbox(slide, x, y, col_w - Inches(0.3), Inches(0.24))
        para(tf, True, str(label).upper(), size=8.5, bold=True, color=ACCENT,
             space_after=0)
        tf = txbox(slide, x, y + Inches(0.22), col_w - Inches(0.3), Inches(0.32))
        v = str(value)
        para(tf, True, v, size=fit(v, 40, 13), color=PAPER, space_after=0)

    if prepared_by:
        tf = txbox(slide, Inches(0.9), SH - Inches(1.45), Inches(8.0),
                   Inches(0.3))
        para(tf, True, prepared_by, size=11, color=DIM, space_after=0)
    if footnote:
        tf = txbox(slide, Inches(0.9), SH - Inches(1.05), Inches(9.6),
                   Inches(0.6))
        para(tf, True, footnote, size=9, color=MUTED, italic=True)


def agenda(slide, items):
    """`items` is [(title, detail)]. Numbered circles, one row each."""
    top = heading(slide, "Contents",
                  sub="What this report covers, in order.")
    # Nine, not seven: once the results are split into their own per-phase
    # sections a full Cool+Fill+Pack+Warp run genuinely has that many entries,
    # and silently dropping the last two would leave the contents page
    # describing a shorter deck than the one it opens. Past seven the row
    # shrinks, so the type shrinks with it.
    items = list(items)[:9]
    tight = len(items) > 7
    row_h = int((SH - top - Inches(0.7)) / max(1, len(items)))
    d = Inches(0.44) if not tight else Inches(0.38)
    for i, (title, detail) in enumerate(items):
        y = top + row_h * i
        circle(slide, M, y + int((row_h - d) / 2) - Inches(0.05), d, i + 1,
               size=12 if not tight else 10)
        tf = txbox(slide, M + Inches(0.72), y + Inches(0.06),
                   CONTENT_W - Inches(0.72), Inches(0.34))
        para(tf, True, title, size=15 if not tight else 13, bold=True,
             color=INK, space_after=1)
        if detail:
            tf = txbox(slide, M + Inches(0.72),
                       y + (Inches(0.42) if not tight else Inches(0.36)),
                       CONTENT_W - Inches(0.72), Inches(0.3))
            para(tf, True, detail, size=10.5 if not tight else 9.5,
                 color=MUTED, space_after=0)


def divider(slide, number, title, subtitle="", points=()):
    """Dark section divider, matching the cover so the deck has a rhythm."""
    rect(slide, 0, 0, SW, SH, INK)
    rect(slide, SW - Inches(2.0), -Inches(1.2), Inches(3.4), Inches(3.4),
         DARK_PANEL, MSO_SHAPE.OVAL)

    tf = txbox(slide, Inches(0.9), Inches(2.55), Inches(1.2), Inches(0.5))
    para(tf, True, "{0:02d}".format(number), size=30, bold=True, color=ACCENT,
         space_after=0)

    tf = txbox(slide, Inches(0.9), Inches(3.15), Inches(10.0), Inches(1.0))
    para(tf, True, title, size=fit(title, 40, 36), bold=True, color=PAPER,
         space_after=6)
    if subtitle:
        para(tf, False, subtitle, size=14, color=DIM)

    if points:
        tf = txbox(slide, Inches(0.9), Inches(4.6), Inches(10.6), Inches(1.4))
        for i, p in enumerate(points[:4]):
            para(tf, i == 0, p, size=11, color=DIM, bullet=True, space_after=6)


def study_setup(slide, context_rows, process_rows, notes=()):
    """The study's inputs on one slide: material and mesh on the left, the
    process settings the solver ran with on the right.

    Both are [(label, value)]. Either may be empty — an unmeshed study or a
    machine that reports nothing is normal, and the column simply does not
    appear rather than showing a table of dashes.
    """
    top = heading(slide, "Study setup", eyebrow="Inputs",
                  sub="Material, mesh and the process conditions this analysis "
                      "was run with.")
    gap = Inches(0.4)
    col_w = int((CONTENT_W - gap) / 2)
    # Both cards are as tall as the space allows and their rows are spread to
    # fill it, so a five-row card does not leave the bottom third of the slide
    # empty. Pitch is clamped: rows that drift too far apart stop reading as a
    # list.
    avail = SH - top - (Inches(1.35) if notes else Inches(0.55))
    n_rows = max(len(context_rows or ()), len(process_rows or ()), 1)
    pitch = min(Inches(0.78), max(Inches(0.46),
                                  int((avail - Inches(1.0)) / n_rows)))
    card_h = int(Inches(0.68) + pitch * n_rows + Inches(0.24))

    def _card(x, title, rows):
        if not rows:
            return
        rect(slide, x, top, col_w, card_h, PANEL, MSO_SHAPE.ROUNDED_RECTANGLE,
             0.03)
        tf = txbox(slide, x + Inches(0.3), top + Inches(0.24),
                   col_w - Inches(0.6), Inches(0.3))
        para(tf, True, title.upper(), size=10, bold=True, color=ACCENT,
             space_after=0)
        for i, (label, value) in enumerate(rows[:9]):
            y = top + Inches(0.72) + pitch * i
            tf = txbox(slide, x + Inches(0.3), y, col_w - Inches(0.6),
                       Inches(0.34))
            p = tf.paragraphs[0]
            p.space_after = Pt(0)
            r = p.add_run()
            r.text = str(label)
            r.font.name, r.font.size, r.font.bold = FONT, Pt(11), False
            r.font.color.rgb = MUTED
            r2 = p.add_run()
            r2.text = "   " + str(value)
            r2.font.name, r2.font.size, r2.font.bold = FONT, Pt(11.5), True
            r2.font.color.rgb = INK

    _card(M, "Material & model", context_rows)
    _card(M + col_w + gap, "Process settings", process_rows)

    if notes:
        y = SH - Inches(1.15)
        tf = txbox(slide, M, y, CONTENT_W, Inches(0.7))
        for i, n in enumerate(notes[:2]):
            para(tf, i == 0, n, size=9.5, color=MUTED, italic=True,
                 space_after=3)


def _two_card_slide(slide, title, sub, eyebrow, left, right, notes=()):
    """Heading + up to two labelled cards of (label, value) rows + a note line.

    This is the layout `study_setup()` established, generalised so the slides
    added after it look like it rather than merely near it. `study_setup()`
    itself is deliberately NOT routed through here: it works, it is exercised
    on every run, and a shared helper is not worth the risk of changing it.

    `left` and `right` are each (card title, [(label, value)]). A card with no
    rows is not drawn, and its neighbour keeps its own half of the slide — a
    single card stretched across the full width reads as a different slide.
    """
    top = heading(slide, title, eyebrow=eyebrow, sub=sub)
    gap = Inches(0.4)
    col_w = int((CONTENT_W - gap) / 2)
    cards = [c for c in (left, right) if c and c[1]]
    if not cards:
        return

    avail = SH - top - (Inches(1.35) if notes else Inches(0.55))
    n_rows = max([len(rows) for _t, rows in cards] + [1])
    # The pitch floor has to give way before the card does. A full process
    # table is eleven rows, and a fixed floor would size the card past the
    # notes line and print the last rows over them.
    pitch = min(Inches(0.78), max(Inches(0.30),
                                  int((avail - Inches(0.92)) / n_rows)))
    card_h = min(int(Inches(0.68) + pitch * n_rows + Inches(0.24)), int(avail))

    # Long values (a machine name, a material grade) step down a point rather
    # than run past the card edge. Tight rows step down too: 11.5pt in a 0.30in
    # pitch touches the row below it.
    def _row_size(value):
        base = 11.5 if len(str(value)) <= 26 else (10 if len(str(value)) <= 38
                                                    else 9)
        if pitch < Inches(0.36):
            base = min(base, 9.5)
        return base

    for idx, (card_title, rows) in enumerate(cards):
        x = M + (col_w + gap) * idx
        rect(slide, x, top, col_w, card_h, PANEL, MSO_SHAPE.ROUNDED_RECTANGLE,
             0.03)
        tf = txbox(slide, x + Inches(0.3), top + Inches(0.24),
                   col_w - Inches(0.6), Inches(0.3))
        para(tf, True, str(card_title).upper(), size=10, bold=True,
             color=ACCENT, space_after=0)
        for i, (label, value) in enumerate(rows[:12]):
            y = top + Inches(0.72) + pitch * i
            tf = txbox(slide, x + Inches(0.3), y, col_w - Inches(0.6),
                       Inches(0.34))
            p = tf.paragraphs[0]
            p.space_after = Pt(0)
            r = p.add_run()
            r.text = str(label)
            r.font.name, r.font.size, r.font.bold = FONT, Pt(11), False
            r.font.color.rgb = MUTED
            r2 = p.add_run()
            r2.text = "   " + str(value)
            r2.font.name, r2.font.bold = FONT, True
            r2.font.size = Pt(_row_size(value))
            r2.font.color.rgb = INK

    if notes:
        y = SH - Inches(1.15)
        tf = txbox(slide, M, y, CONTENT_W, Inches(0.7))
        for i, n in enumerate(notes[:2]):
            para(tf, i == 0, n, size=9.5, color=MUTED, italic=True,
                 space_after=3)


def component_details(slide, geometry_rows, mesh_rows, notes=()):
    """What the part IS, before any result: envelope and volume on the left,
    the FEA model it was converted to on the right.

    Both are [(label, value)] and either may be empty — an unmeshed study, or
    one whose model could not be exported, simply loses that column.
    """
    _two_card_slide(
        slide, "Component details", eyebrow="The part",
        sub="Size, envelope and the finite-element model these results were "
            "computed on.",
        left=("Geometry", geometry_rows), right=("FEA model", mesh_rows),
        notes=notes)


def process_parameters(slide, configured_rows, achieved_rows, notes=()):
    """The moulding conditions: what was set going in, and what the solver
    actually did.

    Two columns rather than one table on purpose. Moldflow's process settings
    are mostly MODE selectors — an automatic run reports "Automatic" for
    filling control, V/P switchover, pack/hold and cooling time, and a table of
    those alone tells the reader nothing. The achieved column carries the
    numbers a reader of the reference deck expects to see, taken from the
    results rather than from the form.
    """
    _two_card_slide(
        slide, "Process parameters", eyebrow="Moulding conditions",
        sub="Left: the settings the study was run with. Right: what the "
            "analysis actually produced.",
        left=("As configured", configured_rows),
        right=("As achieved", achieved_rows), notes=notes)


def highlights(slide, groups, footnote=""):
    """"Points to highlight": recommendations grouped by what they are about.

    `groups` is [(heading, [line, ...])]. Two columns of cards, because a flat
    list of eight sentences is the slide everyone skips.
    """
    top = heading(slide, "Points to highlight", eyebrow="Recommendations",
                  sub="Actions to consider before the next iteration.")
    groups = [(g, [l for l in lines if l]) for g, lines in groups if lines]
    if not groups:
        return
    groups = groups[:4]
    cols = 2 if len(groups) > 1 else 1
    gap = Inches(0.4)
    col_w = int((CONTENT_W - gap * (cols - 1)) / cols)
    rows = (len(groups) + cols - 1) // cols
    avail = SH - top - (Inches(0.95) if footnote else Inches(0.55))

    # Height per ROW of cards, from the wordiest card in that row rather than
    # one uniform block: two short recommendations should not be given the same
    # half-slide as four long ones, which is what left the cards half empty.
    chars_per_line = 62.0
    row_h, y_of = [], []
    for r in range(rows):
        need = 0
        for c in range(cols):
            i = r * cols + c
            if i >= len(groups):
                continue
            lines = groups[i][1][:4]
            est = sum(1 + int(len(l) / chars_per_line) for l in lines)
            need = max(need, Inches(0.95) + Inches(0.30) * est)
        row_h.append(int(need))
    slack = avail - sum(row_h) - gap * (rows - 1)
    if slack > 0:                      # spread what is left over, evenly
        row_h = [h + int(slack / rows) for h in row_h]
    elif slack < 0:                    # scale down rather than run off the slide
        f = float(avail - gap * (rows - 1)) / sum(row_h)
        row_h = [int(h * f) for h in row_h]
    y = top
    for h in row_h:
        y_of.append(y)
        y += h + gap

    for i, (title, lines) in enumerate(groups):
        x = M + (col_w + gap) * (i % cols)
        y = y_of[i // cols]
        card_h = row_h[i // cols]
        # An odd last card spans the full width rather than leaving a hole
        # beside it — three recommendations should not look like a missing
        # fourth.
        w = col_w
        if cols == 2 and i == len(groups) - 1 and i % cols == 0:
            w = CONTENT_W
        rect(slide, x, y, w, card_h, PANEL, MSO_SHAPE.ROUNDED_RECTANGLE,
             0.03)
        col_w_here = w
        d = Inches(0.34)
        circle(slide, x + Inches(0.28), y + Inches(0.26), d,
               chr(ord("A") + i), fill=INK, size=11)
        tf = txbox(slide, x + Inches(0.74), y + Inches(0.3),
                   col_w_here - Inches(1.04), Inches(0.32))
        para(tf, True, title, size=13, bold=True, color=INK, space_after=0)

        body_top = y + Inches(0.78)
        tf = txbox(slide, x + Inches(0.3), body_top, col_w_here - Inches(0.6),
                   card_h - Inches(1.0))
        # Shrink to the card rather than overflow it: a long recommendation is
        # still a recommendation and must not be cut off.
        bulk = sum(len(l) for l in lines)
        size = 11 if bulk < 260 else (10 if bulk < 400 else 9)
        for j, line in enumerate(lines[:4]):
            para(tf, j == 0, line, size=size, color=BODY, bullet=True,
                 space_after=6, line_spacing=1.05)

    if footnote:
        tf = txbox(slide, M, SH - Inches(0.85), CONTENT_W, Inches(0.5))
        para(tf, True, footnote, size=9, color=MUTED, italic=True)


def study_note(tf, first, text, size=11):
    """The Assistant's sentence about THIS study's figure, for the panel on a
    result slide. Marked with the accent so a reader can tell at a glance which
    line is about their study and which is the reference material."""
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.space_before, p.space_after = Pt(0), Pt(7)
    r = p.add_run()
    r.text = "In this study:  "
    r.font.name, r.font.size, r.font.bold = FONT, Pt(size), True
    r.font.color.rgb = ACCENT
    r2 = p.add_run()
    r2.text = str(text)
    r2.font.name, r2.font.size = FONT, Pt(size)
    r2.font.color.rgb = INK
    return p


# --------------------------------------------------------------------------- #
#  Offline preview: python report_style.py [<report folder>]
# --------------------------------------------------------------------------- #

def _demo(out_path, image=None):
    from pptx import Presentation
    prs = Presentation()
    prs.slide_width, prs.slide_height = SW, SH
    blank = prs.slide_layouts[6]

    def _s():
        return prs.slides.add_slide(blank)

    cover(_s(), "unoteam_study~18.sdy",
          "Engineering review of the Top 12 results",
          [("Analysis sequence", "Fill"),
           ("Material", "POLYFLAM RIPP 3625 CS1 : A Schulman GMBH"),
           ("Mesh type", "3D"),
           ("Generated", "2026-08-04 10:08"),
           ("Results exported", "8 of the Top 12, plus 2 supporting investigations"),
           ("Analysis status", "Solved")],
          footnote="Result categories, interpretation and targets per Autodesk, "
                   "“Top 12 Results to View from a Cool + Flow + Warp Simulation”.",
          prepared_by="Generated automatically from the open Moldflow study")

    agenda(_s(), [
        ("Study setup", "Material, mesh and process conditions"),
        ("Engineering results", "8 of the Top 12 results, each with its plot and animation"),
        ("Supporting investigations", "Air traps and weld lines behind the headline results"),
        ("Results not produced", "What this sequence could not supply, and why"),
        ("Analysis summary", "Problematic results, key concerns and statistics"),
        ("Points to highlight", "Recommended actions before the next iteration"),
    ])

    divider(_s(), 1, "Study setup",
            "The inputs the solver ran with",
            ["Material grade and its published limits",
             "Mesh type and element count",
             "Melt and mold temperatures, machine limits"])

    study_setup(_s(),
                [("Material", "POLYFLAM RIPP 3625 CS1"),
                 ("Family", "PP"),
                 ("Fillers", "Unfilled"),
                 ("Mesh type", "3D"),
                 ("Analysis sequence", "Fill")],
                [("Melt temperature", "200 °C"),
                 ("Mold surface temperature", "60 °C"),
                 ("Machine", "Generic"),
                 ("Max clamp force", "7000 tonne"),
                 ("Max injection pressure", "180 MPa")],
                notes=["Process settings are the grade's own recommended values "
                       "as read from the study file."])

    highlights(_s(), [
        ("Flow", ["Reduce injection speed or redesign the gate/runner to bring "
                  "shear rate and shear stress within material limits "
                  "(≤100,000 1/s and ≤0.25 MPa).",
                  "Review gate position to shorten the flow length and lower "
                  "end-of-fill pressure."]),
        ("Tooling", ["Investigate the 26 air trap locations and add vents or "
                     "reposition gates so trapped air can escape during filling."]),
        ("Cooling", ["Review and optimise the cooling system — the ejection "
                     "temperature time (avg 421.6 s) is excessively long and "
                     "will severely impact cycle time."]),
        ("Next run", ["Run a Packing simulation to obtain accurate clamp force "
                      "data; the current fill-only figure is likely "
                      "underestimated."]),
    ], footnote="Generated by Moldflow's AI Assistant from this study's own "
                "results. Review against the plots in this deck before acting.")

    # A result slide, to check the new study note beside the reference text.
    s = _s()
    top = heading(s, "Shear rate", eyebrow="Engineering result 06",
                  sub="Moldflow plot: Shear rate, maximum")
    if image:
        s.shapes.add_picture(str(image), M, top, width=Inches(6.95))
    else:
        rect(s, M, top, Inches(6.95), Inches(5.2), PANEL)
    x = Inches(7.75)
    rect(s, x, top, Inches(5.05), Inches(5.2), PANEL,
         MSO_SHAPE.ROUNDED_RECTANGLE, 0.02)
    tf = txbox(s, x + Inches(0.28), top + Inches(0.24), Inches(4.5),
               Inches(4.7))
    study_note(tf, True,
               "Maximum shear rate reaches 281,178 1/s against the grade's "
               "100,000 1/s limit — a critical flow problem.")
    para(tf, False, "Value range in this study:  0 to 281,178 1/s", size=11)
    para(tf, False, "Description:  A measure of how quickly the layers of "
                    "plastic are sliding past each other.", size=11)
    para(tf, False, "Engineering purpose:  Prevent polymer chain breakage and "
                    "material degradation during filling.", size=11)
    para(tf, False, "Recommended target:  Below the grade's published maximum "
                    "shear rate.", size=11)

    prs.save(str(out_path))
    return out_path, len(prs.slides._sldIdLst)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("style_preview.pptx")
    img = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    path, n = _demo(out, img)
    print("{0} slides -> {1}".format(n, path))
