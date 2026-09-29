"""Draws an ``InvoiceDocument`` as Figma 09-A · Invoice PDF (frame ``1410:2043``).

COORDINATES ARE THE FRAME'S. ``FPDF(unit=0.75)`` makes one user unit 0.75pt — one CSS px at 96
dpi — so the A4 page is the frame's 794 x 1123 and every number below is read off it. Font SIZES
are still points to fpdf2 (px x 0.75). Figma gives a text box's TOP; with Inter's "normal"
leading (1.21 em) the baseline sits one ascent (0.969 em) below it, which is where
``_Canvas.text`` puts it.

READINGS OF THE FRAME (told to the user, 2026-09-29):

* "Description" / "Amount (HK$)" share a baseline, and so does each row's text and figure — the
  frame's 17px and 4px drift is not copied;
* every figure is right-aligned to x=716 (the frame's total sits 5px further right);
* the company rows under a plan are this code's own (09-A has none): indented, in the address
  grey, their figures in an inner column so they never read as a second total;
* the bill-to block, the table, the total and the note FLOW — a long address or many companies
  push them down, and the table never starts above the frame's y=439. Only the footer is fixed,
  and it is on every page. A table that runs into the footer band continues on a new page under
  its repeated header, a split plan repeating its heading as "<label> (continued)".

GLYPHS FAIL LOUDLY. fpdf2 silently skips a character no font can draw (it warns, naming no
invoice). Every dynamic string is checked first, and a miss is logged at ERROR with the invoice
and the code points. Chinese — HK company names and addresses — draws in the Noto Sans HK
fallback, loaded only for a document that needs it: it is a 5.7 MB font.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

from billing.services._log import logger
from billing.services.invoice_document import CONTINUED, InvoiceDocument

ASSETS = Path(__file__).resolve().parent.parent / "static" / "invoice"
FONTS = ASSETS / "fonts"
LOGO = ASSETS / "minty-logo.svg"  # the frame's own vector (09-A "Group 210")
FONT_FILES = {
    ("Inter", ""): FONTS / "Inter-Regular.ttf",
    ("Inter", "B"): FONTS / "Inter-Bold.ttf",
    # fpdf2 knows regular / B / I / BI only, so SemiBold is a family of its own.
    ("InterSemiBold", ""): FONTS / "Inter-SemiBold.ttf",
}
FALLBACK = ("NotoSansHK", "")
FALLBACK_FILE = FONTS / "NotoSansHK-Regular.otf"

REGULAR, BOLD, SEMIBOLD = ("Inter", ""), ("Inter", "B"), ("InterSemiBold", "")

#: Inter's hhea ascender over its units per em; its "normal" leading is (1984 + 494) / 2048.
ASCENT = 1984 / 2048
LEADING = 1.21

# Colours, as the frame sets them.
BLACK = (0x00, 0x00, 0x00)
GREY = (0x73, 0x7A, 0x87)    # the address, the email, the note, the company rows
INK = (0x33, 0x3B, 0x45)     # the plan lines
HEAD = (0x16, 0x20, 0x2E)    # "Description" / "Amount (HK$)"
TEAL = (0x4F, 0xC7, 0xC7)    # the total
LINK = (0x2E, 0x9B, 0x9B)    # the footer's mailbox
RULE = (0xE6, 0xE6, 0xE6)

# Geometry, px.
LEFT = 55
RIGHT = 716                   # every figure's right edge
RULE_RIGHT = 734
LABEL_X = 457                 # "Invoice #", "Bill Date"
BILL_TO_WIDTH = LABEL_X - 24 - LEFT
VALUE_GAP = 16                # the least space between a label and its value
MIN_VALUE_SIZE = 11           # the Invoice # shrinks to fit, this far and no further
ROW_X = 79                    # company rows sit under their plan, indented
ROW_FIGURE_RIGHT = 600        # ...with their figures in an inner column
FIGURE_GAP = 24               # the least space between a line's words and its figure
TOTAL_LABEL_X = 490
NOTE_WIDTH = 661
TABLE_TOP = 439               # the frame's; a long bill-to pushes it down, never up
PAGE_TOP = 57                 # where a continuation page starts
FOOTER_X = 52
FOOTER_TOP = 1039
FLOW_BOTTOM = FOOTER_TOP - 32  # nothing that flows goes below this

# Vertical rhythm, px, measured off the frame.
TITLE_TOP = 57
LABEL_TOPS = (194, 244)       # "Invoice #" / "Bill Date" (and "Bill to", the name)
NAME_TO_ADDRESS = 32          # 261 -> 293
ADDRESS_TO_EMAIL = 27         # 353 -> 380
BILL_TO_TO_TABLE = 44         # 395 -> 439
HEAD_TO_FIRST_LINE = 72       # 439 -> 511
LINE_BOX = 17                 # a 14px line
SMALL_BOX = 15                # a 12px line
ROW_GAP = 5                   # between a plan line and each company row under it
TEXT_TO_RULE = 19             # 528 -> 547
RULE_TO_LINE = 18             # 547 -> 565
RULE_TO_TOTAL = 32            # 655 -> 687
TOTAL_TO_NOTE = 69            # 687 -> 756


@dataclass(frozen=True)
class Mark:
    """One thing drawn — text by its baseline, a rule, or an image — for tests to read back."""

    kind: str
    page: int
    x: float
    y: float
    text: str = ""
    font: tuple = ()
    size: float = 0.0
    color: tuple = ()
    width: float = 0.0


class _Canvas:
    """Every mark goes through here, so a test can read the page back without parsing a PDF
    (fpdf2 writes Unicode-font text as glyph ids, and ``multi_cell`` never passes through
    ``cell``, so a subclass would see half the text)."""

    def __init__(self, pdf):
        self.pdf = pdf
        self.marks: list[Mark] = []

    def width(self, text: str, font: tuple, size: float) -> float:
        self._font(font, size)
        return self.pdf.get_string_width(text)

    def text(self, x: float, top: float, text: str, font: tuple = REGULAR, size: float = 14,
             color: tuple = BLACK, *, right: bool = False) -> float:
        """Draw ``text`` with its box's TOP at ``top`` (Figma's y). ``right``: x is its right
        edge. Always through ``cell`` — ``text()`` skips the fallback font."""
        self._font(font, size)
        self.pdf.set_text_color(*color)
        width = self.pdf.get_string_width(text)
        left = x - width if right else x
        baseline = top + ASCENT * size
        # fpdf2 sets a cell's text at y + h/2 + 0.3 x the font size: with h = the size, that is
        # 0.8 x the size below the cell's top.
        self.pdf.set_xy(left, baseline - 0.8 * size)
        self.pdf.cell(width, size, text)
        self.marks.append(Mark("text", self.pdf.page, left, baseline, text, font, size, color,
                               width))
        return width

    def wrap(self, text: str, font: tuple, size: float, width: float) -> list[str]:
        """``text`` broken into lines no wider than ``width``, as fpdf2 would break them."""
        if not text:
            return []
        self._font(font, size)
        return list(self.pdf.multi_cell(width, size, text, dry_run=True, output="LINES"))

    def rule(self, x1: float, top: float, x2: float) -> None:
        self.pdf.set_draw_color(*RULE)
        self.pdf.set_line_width(1)
        self.pdf.line(x1, top + 0.5, x2, top + 0.5)  # a 1px line centred in its 1px box
        self.marks.append(Mark("rule", self.pdf.page, x1, top, color=RULE, width=x2 - x1))

    def image(self, path: Path, x: float, top: float, width: float) -> None:
        self.pdf.image(str(path), x=x, y=top, w=width)
        self.marks.append(Mark("image", self.pdf.page, x, top, text=path.name, width=width))

    def _font(self, font: tuple, size: float) -> None:
        family, style = font
        self.pdf.set_font(family, style, size * 0.75)


def render_invoice_pdf(doc: InvoiceDocument) -> bytes:
    """The PDF's bytes."""
    return render(doc)[0]


def render(doc: InvoiceDocument) -> tuple[bytes, list[Mark]]:
    """The PDF's bytes and every mark drawn on it, in order."""
    needs_fallback = _check_glyphs(doc)
    canvas = _Canvas(_new_pdf(doc, fallback=needs_fallback))
    canvas.pdf.add_page()
    _draw_header(canvas, doc)
    y = _draw_bill_to(canvas, doc)
    last_rule = _draw_table(canvas, doc, max(TABLE_TOP, y + BILL_TO_TO_TABLE))
    _draw_total_and_note(canvas, doc, last_rule)
    _draw_footer(canvas, doc)
    return bytes(canvas.pdf.output()), canvas.marks


def _new_pdf(doc: InvoiceDocument, *, fallback: bool):
    from fpdf import FPDF  # imported here: only this route needs it

    pdf = FPDF(unit=0.75, format="A4")
    pdf.set_auto_page_break(False)  # paging is ours: the footer band and repeated heads
    pdf.set_margins(0, 0, 0)
    pdf.c_margin = 0  # or every right-aligned figure shifts by the cell's padding
    pdf.set_title(f"Invoice {doc.reference}")
    pdf.set_author("DailyMinty Limited")
    pdf.set_creator("Minty")
    for (family, style), path in FONT_FILES.items():
        pdf.add_font(family, style, str(path))
    if fallback:
        pdf.add_font(*FALLBACK, str(FALLBACK_FILE))
        pdf.set_fallback_fonts([FALLBACK[0]], exact_match=False)
    return pdf


# --- The page --------------------------------------------------------------------------


def _draw_header(canvas: _Canvas, doc: InvoiceDocument) -> None:
    canvas.text(LEFT, TITLE_TOP, "Invoice", BOLD, 20)
    canvas.image(LOGO, 646, TITLE_TOP, 78)

    canvas.text(LEFT, LABEL_TOPS[0], "Bill to", BOLD)
    for top, label, value in ((LABEL_TOPS[0], "Invoice #", doc.reference),
                              (LABEL_TOPS[1], "Bill Date", doc.bill_date)):
        label_width = canvas.text(LABEL_X, top, label, BOLD)
        # A Stripe id is ~214px at 14px and the frame leaves ~180: shrink it to fit on the
        # label's baseline rather than run it into the label.
        room = RIGHT - (LABEL_X + label_width + VALUE_GAP)
        size = 14.0
        natural = canvas.width(value, REGULAR, size)
        if natural > room:
            size = max(MIN_VALUE_SIZE, size * room / natural)
        baseline = top + ASCENT * 14
        canvas.text(RIGHT, baseline - ASCENT * size, value, REGULAR, size, right=True)


def _draw_bill_to(canvas: _Canvas, doc: InvoiceDocument) -> float:
    """The bill-to block, flowing from the name down. Returns its bottom."""
    y = LABEL_TOPS[1]
    for line in canvas.wrap(doc.bill_to_name, REGULAR, 14, BILL_TO_WIDTH):
        canvas.text(LEFT, y, line)
        y += LINE_BOX
    gap = NAME_TO_ADDRESS
    if doc.bill_to_address:
        y += gap
        for address_line in doc.bill_to_address:
            for line in canvas.wrap(address_line, REGULAR, 12, BILL_TO_WIDTH):
                canvas.text(LEFT, y, line, REGULAR, 12, GREY)
                y += SMALL_BOX
        gap = ADDRESS_TO_EMAIL
    if doc.bill_to_email:
        y += gap
        for line in _email_lines(canvas, doc.bill_to_email):
            canvas.text(LEFT, y, line, REGULAR, 12, GREY)
            y += SMALL_BOX
    return y


def _email_lines(canvas: _Canvas, email: str) -> list[str]:
    """An address has no spaces to break at: break after the "@", as 08-B does, and by
    character only if a half still cannot fit."""
    if canvas.width(email, REGULAR, 12) <= BILL_TO_WIDTH:
        return [email]
    local, at, domain = email.partition("@")
    halves = [local + at, domain] if at else [email]
    lines: list[str] = []
    for half in halves:
        lines.extend(_char_wrap(canvas, half, 12, BILL_TO_WIDTH) if half else [])
    return lines


def _char_wrap(canvas: _Canvas, text: str, size: float, width: float) -> list[str]:
    lines, current = [], ""
    for char in text:
        if current and canvas.width(current + char, REGULAR, size) > width:
            lines.append(current)
            current = ""
        current += char
    return lines + ([current] if current else [])


def _draw_heads(canvas: _Canvas, doc: InvoiceDocument, top: float) -> None:
    canvas.text(LEFT, top, "Description", SEMIBOLD, 14, HEAD)
    canvas.text(RIGHT, top, doc.amount_header, SEMIBOLD, 14, HEAD, right=True)


def _draw_table(canvas: _Canvas, doc: InvoiceDocument, head_top: float) -> float:
    """The plan lines and their company rows. Returns the last rule's top."""
    _draw_heads(canvas, doc, head_top)
    y = head_top + HEAD_TO_FIRST_LINE
    last_rule = y - RULE_TO_LINE

    def next_page() -> float:
        _draw_footer(canvas, doc)
        canvas.pdf.add_page()
        _draw_heads(canvas, doc, PAGE_TOP)
        return PAGE_TOP + HEAD_TO_FIRST_LINE

    for group in doc.groups:
        figure_width = canvas.width(group.amount, REGULAR, 14)
        label_lines = canvas.wrap(group.label, REGULAR, 14,
                                  RIGHT - figure_width - FIGURE_GAP - LEFT)
        rows = [(row, _row_lines(canvas, row)) for row in group.rows]
        # A heading never ends a page alone: it needs room for its first company row too.
        first_row = (ROW_GAP + len(rows[0][1]) * SMALL_BOX) if rows else 0
        if y + len(label_lines) * LINE_BOX + first_row + TEXT_TO_RULE > FLOW_BOTTOM:
            y = next_page()
        for index, line in enumerate(label_lines):
            canvas.text(LEFT, y + index * LINE_BOX, line, REGULAR, 14, INK)
        canvas.text(RIGHT, y, group.amount, REGULAR, 14, INK, right=True)
        y += len(label_lines) * LINE_BOX

        for row, lines in rows:
            height = ROW_GAP + len(lines) * SMALL_BOX
            if y + height + TEXT_TO_RULE > FLOW_BOTTOM:
                y = next_page()
                canvas.text(LEFT, y, group.label + CONTINUED, REGULAR, 14, INK)
                y += LINE_BOX
            y += ROW_GAP
            for index, line in enumerate(lines):
                canvas.text(ROW_X, y + index * SMALL_BOX, line, REGULAR, 12, GREY)
            canvas.text(ROW_FIGURE_RIGHT, y, row.amount, REGULAR, 12, GREY, right=True)
            y += len(lines) * SMALL_BOX

        last_rule = y + TEXT_TO_RULE
        canvas.rule(LEFT, last_rule, RULE_RIGHT)
        y = last_rule + RULE_TO_LINE
    return last_rule


def _row_lines(canvas: _Canvas, row) -> list[str]:
    figure_width = canvas.width(row.amount, REGULAR, 12)
    return canvas.wrap(row.text, REGULAR, 12, ROW_FIGURE_RIGHT - figure_width - FIGURE_GAP - ROW_X)


def _draw_total_and_note(canvas: _Canvas, doc: InvoiceDocument, last_rule: float) -> None:
    note_lines = canvas.wrap(doc.note, REGULAR, 12, NOTE_WIDTH)
    top = last_rule + RULE_TO_TOTAL
    if top + TOTAL_TO_NOTE + len(note_lines) * SMALL_BOX > FLOW_BOTTOM:
        # The total and the sentence about it stay together, on a page of their own if need be.
        _draw_footer(canvas, doc)
        canvas.pdf.add_page()
        top = PAGE_TOP

    figure_width = canvas.width(doc.total, BOLD, 20)
    baseline = top + ASCENT * 20
    canvas.text(RIGHT, top, doc.total, BOLD, 20, TEAL, right=True)
    label_width = canvas.width("Amount", BOLD, 18)
    label_x = min(TOTAL_LABEL_X, RIGHT - figure_width - FIGURE_GAP - label_width)
    canvas.text(label_x, baseline - ASCENT * 18, "Amount", BOLD, 18)

    y = top + TOTAL_TO_NOTE
    for line in note_lines:
        canvas.text(LEFT, y, line, REGULAR, 12, GREY)
        y += SMALL_BOX


def _draw_footer(canvas: _Canvas, doc: InvoiceDocument) -> None:
    canvas.text(FOOTER_X, FOOTER_TOP, doc.seller_line, REGULAR, 12)
    x = FOOTER_X
    top = FOOTER_TOP + SMALL_BOX
    for text, color in (("Please contact ", BLACK), (doc.contact_email, LINK),
                        (" for inquiry.", BLACK)):
        x += canvas.text(x, top, text, REGULAR, 12, color)


# --- Glyphs ------------------------------------------------------------------------------


@functools.cache
def _cmap(path: str) -> frozenset[int]:
    from fontTools.ttLib import TTFont

    return frozenset(TTFont(path, lazy=True).getBestCmap())


def _dynamic_texts(doc: InvoiceDocument) -> list[str]:
    """Every string on the page that the invoice supplies (the rest is fixed copy)."""
    texts = [doc.reference, doc.bill_date, doc.bill_to_name, doc.bill_to_email,
             doc.amount_header, doc.total, *doc.bill_to_address]
    for group in doc.groups:
        texts += [group.label, group.amount]
        texts += [text for row in group.rows for text in (row.text, row.amount)]
    return texts


def _check_glyphs(doc: InvoiceDocument) -> bool:
    """Log, loudly, any character no font on the page can draw. Returns whether the CJK
    fallback is needed at all."""
    inter = _cmap(str(FONT_FILES[REGULAR]))
    outside = {char for text in _dynamic_texts(doc) for char in text
               if not char.isspace() and ord(char) not in inter}
    if not outside:
        return False
    fallback = _cmap(str(FALLBACK_FILE))
    missing = sorted(char for char in outside if ord(char) not in fallback)
    if missing:
        logger.error(
            "invoice pdf: invoice {} has characters no font can draw, left out of the PDF: {}",
            doc.invoice_id, ", ".join(f"U+{ord(char):04X}" for char in missing),
        )
    return True
