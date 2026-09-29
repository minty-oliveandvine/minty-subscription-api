"""The invoice PDF drawn to Figma 09-A (``invoice_pdf.render``).

Read back from the canvas's own record of every mark (text by its baseline, rules, the logo),
so positions are asserted in the frame's px without parsing a PDF. What is pinned:

* **The frame's coordinates** — the title, the labels, the table, the teal total, the footer —
  and every figure's right edge on x=716.
* **Flow and paging** — a long bill-to pushes the table down; many companies run onto more
  pages, each with its footer and repeated column heads, nothing entering the footer band, and a
  plan split across pages repeating its heading "(continued)".
* **Glyphs** — a Chinese company name is drawn (the Noto Sans HK fallback, loaded only then);
  a character no font has is logged at ERROR with the invoice, never silently dropped.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from billing.services import invoice_pdf
from billing.services.invoice_document import CompanyRow, InvoiceDocument, PlanGroup
from billing.services.invoice_pdf import ASCENT, FLOW_BOTTOM, RIGHT

STRIPE_ID = "in_1Q8xZ2AbCdEfGhIjKlMnOpQr"  # a real-length Stripe invoice id: 27 characters


def _row(company, plan="Petty Cash", amount="HK$280", minor=28000, suffix=""):
    return CompanyRow(f"{company}{suffix}", f"{company} - {plan}{suffix}", amount, minor)


def _doc(**overrides) -> InvoiceDocument:
    """09-A's own content, with Option B's companies under each plan."""
    doc = InvoiceDocument(
        invoice_id="00000000-0000-0000-0000-00000000a09a",
        reference="#13512413",
        bill_date="14 Sep 2026",
        bill_to_name="Company A Limited",
        bill_to_address=("Unit 10, 1/F", "ABC Building", "2 ABC Street", "Quarry Bay, Hong Kong"),
        bill_to_email="billing@companyalimited.com",
        amount_header="Amount (HK$)",
        groups=(
            PlanGroup("Petty cash module only", "HK$650.32", 65032, (
                _row("Company E Limited"),
                _row("Company F Limited", amount="HK$90.32", minor=9032,
                     suffix=" (access after cancellation)"),
                _row("Company G Limited"),
            )),
            PlanGroup("Payment request module only", "HK$280", 28000,
                      (_row("Aetheria Capital Limited", plan="Payment Request"),)),
            PlanGroup("SuperMinty", "HK$400", 40000,
                      (_row("Nexora Health Limited", plan="Super Minty", amount="HK$400",
                            minor=40000),)),
        ),
        total="HK$1,330.32",
        total_minor=133032,
    )
    return replace(doc, **overrides)


def _many(companies: int) -> InvoiceDocument:
    rows = tuple(_row(f"Company {i:02d} Trading Limited") for i in range(1, companies + 1))
    total = 28000 * companies
    return _doc(groups=(PlanGroup("Petty cash module only", f"HK${total // 100:,}", total, rows),),
                total=f"HK${total // 100:,}", total_minor=total)


def _text(marks, text, page=1):
    return next(m for m in marks if m.kind == "text" and m.text == text and m.page == page)


def _top(mark) -> float:
    """The Figma y of a text mark: its box's top, one ascent above the baseline."""
    return mark.y - ASCENT * mark.size


# --- the frame ---------------------------------------------------------------------------


def test_the_output_is_a_one_page_pdf():
    data, marks = invoice_pdf.render(_doc())

    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    assert {mark.page for mark in marks} == {1}


def test_the_page_is_drawn_where_the_frame_draws_it():
    _data, marks = invoice_pdf.render(_doc())

    title = _text(marks, "Invoice")
    assert (title.x, _top(title), title.size, title.font) == (55, pytest.approx(57), 20, ("Inter", "B"))
    logo = next(m for m in marks if m.kind == "image")
    assert (logo.x, logo.y, logo.width, logo.text) == (646, 57, 78, "minty-logo.svg")

    for label, x, top in (("Bill to", 55, 194), ("Invoice #", 457, 194), ("Bill Date", 457, 244)):
        mark = _text(marks, label)
        assert (mark.x, _top(mark)) == (x, pytest.approx(top)), label
    bill_date = _text(marks, "14 Sep 2026")
    assert bill_date.x + bill_date.width == pytest.approx(RIGHT)
    assert _top(_text(marks, "Company A Limited")) == pytest.approx(244)
    address = _text(marks, "Unit 10, 1/F")
    assert (_top(address), address.size, address.color) == (pytest.approx(293), 12, (0x73, 0x7A, 0x87))
    assert _top(_text(marks, "billing@companyalimited.com")) == pytest.approx(380)

    description, header = _text(marks, "Description"), _text(marks, "Amount (HK$)")
    assert (_top(description), description.font) == (pytest.approx(439), ("InterSemiBold", ""))
    assert header.y == description.y  # one baseline: the frame's 17px drift is not copied
    assert header.x + header.width == pytest.approx(RIGHT)

    first = _text(marks, "Petty cash module only")
    assert (first.x, _top(first), first.color) == (55, pytest.approx(511), (0x33, 0x3B, 0x45))
    figure = _text(marks, "HK$650.32")
    assert (figure.y, figure.x + figure.width) == (first.y, pytest.approx(RIGHT))

    rules = [m for m in marks if m.kind == "rule"]
    assert len(rules) == 3
    assert all((r.x, r.x + r.width, r.color) == (55, 734, (0xE6, 0xE6, 0xE6)) for r in rules)

    total = _text(marks, "HK$1,330.32")
    assert (total.size, total.color, total.font) == (20, (0x4F, 0xC7, 0xC7), ("Inter", "B"))
    assert total.x + total.width == pytest.approx(RIGHT)
    assert _text(marks, "Amount").y == total.y  # the label shares the figure's baseline

    footer = _text(marks, "DailyMinty Limited, Level 5, K11 Atelier, 728 King’s Road, Quarry Bay, Hong Kong.")
    assert (footer.x, _top(footer)) == (52, pytest.approx(1039))
    mailbox = _text(marks, "billing@dailyminty.com")
    assert mailbox.color == (0x2E, 0x9B, 0x9B)


def test_each_company_sits_under_its_plan_with_its_figure_in_the_inner_column():
    _data, marks = invoice_pdf.render(_doc())

    heading = _text(marks, "Petty cash module only")
    company = _text(marks, "Company F Limited (access after cancellation)")
    figure = _text(marks, "HK$90.32")
    assert company.x > heading.x and company.y > heading.y
    assert company.size == 12 and figure.y == company.y
    assert figure.x + figure.width == pytest.approx(invoice_pdf.ROW_FIGURE_RIGHT)


def test_a_stripe_length_reference_shrinks_onto_the_labels_baseline():
    """A Stripe id is ~214px at 14px; the frame leaves ~180 beside "Invoice #"."""
    _data, marks = invoice_pdf.render(_doc(reference=STRIPE_ID))

    label, value = _text(marks, "Invoice #"), _text(marks, STRIPE_ID)
    assert invoice_pdf.MIN_VALUE_SIZE <= value.size < 14
    assert value.y == pytest.approx(label.y)
    assert value.x + value.width == pytest.approx(RIGHT)
    assert value.x >= label.x + label.width + invoice_pdf.VALUE_GAP - 0.01


def test_a_long_bill_to_pushes_the_table_down_and_a_short_one_never_pulls_it_up():
    long_name = "Hong Kong Fine Foods Holdings International Trading Company Limited"
    _data, pushed = invoice_pdf.render(_doc(bill_to_name=long_name))
    _data, short = invoice_pdf.render(_doc(bill_to_address=()))

    assert _top(_text(pushed, "Description")) > 439
    assert _top(_text(short, "Description")) == pytest.approx(439)


# --- paging --------------------------------------------------------------------------


def test_many_companies_run_onto_more_pages_each_with_its_footer_and_heads():
    _data, marks = invoice_pdf.render(_many(60))

    pages = sorted({mark.page for mark in marks})
    assert len(pages) >= 2
    for page in pages:
        assert _text(marks, "Please contact ", page=page)
    with_rows = sorted({m.page for m in marks if m.text.startswith("Company ")})
    assert len(with_rows) >= 2
    for page in with_rows[1:]:
        assert _text(marks, "Description", page=page)
        # The plan carries on under its own name, marked as continued.
        assert _text(marks, "Petty cash module only (continued)", page=page)
    footer_top = invoice_pdf.FOOTER_TOP
    for mark in marks:
        if mark.kind == "text" and _top(mark) < footer_top:
            assert mark.y <= FLOW_BOTTOM + ASCENT * mark.size, mark
    # The total and its note stay together, after every company.
    total = _text(marks, _many(60).total, page=pages[-1])
    last_company = next(m for m in reversed(marks) if m.text.startswith("Company 60"))
    assert (total.page, total.y) > (last_company.page, last_company.y)


def test_a_heading_never_ends_a_page_without_its_first_company():
    _data, marks = invoice_pdf.render(_many(60))

    for mark in marks:
        if mark.text == "Petty cash module only":
            assert any(m.page == mark.page and m.text.startswith("Company 01") for m in marks)


# --- glyphs ------------------------------------------------------------------------------


def test_a_chinese_company_name_is_drawn_in_the_fallback_font(caplog):
    name = "香港美食有限公司 Hong Kong Fine Foods Limited"
    with caplog.at_level(logging.ERROR, logger="billing-api"):
        data, marks = invoice_pdf.render(_doc(bill_to_name=name))

    assert b"NotoSansHK" in data
    assert any(mark.text.startswith("香港美食有限公司") for mark in marks)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_a_plain_invoice_never_loads_the_cjk_font():
    data, _marks = invoice_pdf.render(_doc())

    assert b"NotoSansHK" not in data


def test_a_character_no_font_can_draw_is_logged_with_the_invoice(caplog):
    doc = _doc(bill_to_name="Lemon Ltd \U0001F34B")
    with caplog.at_level(logging.ERROR, logger="billing-api"):
        invoice_pdf.render(doc)

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(doc.invoice_id in message and "U+1F34B" in message for message in messages)
