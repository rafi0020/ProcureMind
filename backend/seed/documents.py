"""Renders demo documents (invoices, credit notes, purchase orders, goods receipts) as real files.

Digital PDFs are drawn with reportlab, so they have a text layer. Scans and photos are those PDFs rendered to
an image and degraded (rotation, blur, noise, JPEG compression, down-scaling), so they must go through OCR.
Four layouts use different labels for the same fields, as real suppliers do.
"""

import io
import random
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pymupdf
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

LABELS = [
    {"number": "Invoice No", "date": "Invoice Date", "due": "Due Date", "po": "PO Number", "total": "Total Due", "subtotal": "Subtotal", "tax": "VAT"},
    {"number": "Invoice #", "date": "Date", "due": "Payment Due", "po": "Your Order", "total": "Amount Due", "subtotal": "Net", "tax": "Tax"},
    {"number": "Bill No", "date": "Issued", "due": "Pay By", "po": "Purchase Order", "total": "Grand Total", "subtotal": "Sub-total", "tax": "Sales Tax"},
    {"number": "Invoice Number", "date": "Invoice Date", "due": "Due", "po": "P.O.", "total": "Balance Due", "subtotal": "Subtotal", "tax": "GST"},
]  # fmt: skip
TITLES = {
    "invoice": "INVOICE",
    "credit_note": "CREDIT NOTE",
    "purchase_order": "PURCHASE ORDER",
    "goods_receipt": "GOODS RECEIVED NOTE",
}
QUALITY = {
    "clean": {"angle": 0.0, "blur": 0.0, "noise": 0, "jpeg": 92, "scale": 1.0},
    "medium": {"angle": 1.2, "blur": 0.6, "noise": 10, "jpeg": 70, "scale": 1.0},
    "poor": {"angle": 2.5, "blur": 1.1, "noise": 18, "jpeg": 35, "scale": 0.6},
}


@dataclass
class Line:
    item_code: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    printed_amount: Decimal | None = None  # set to plant a line whose amount is not quantity x price

    @property
    def amount(self) -> Decimal:
        return (
            self.printed_amount
            if self.printed_amount is not None
            else (self.quantity * self.unit_price).quantize(Decimal("0.01"))
        )


@dataclass
class DocSpec:
    kind: str  # invoice | credit_note | purchase_order | goods_receipt
    number: str
    doc_date: date
    vendor_name: str
    buyer_name: str
    currency: str = "USD"
    lines: list[Line] = field(default_factory=list)
    po_number: str | None = None
    due_date: date | None = None
    tax_rate: Decimal = Decimal("0.10")
    vendor_tax_id: str | None = None
    bank_account: str | None = None
    note: str | None = None  # printed text, e.g. a planted instruction
    layout: int = 0

    @property
    def subtotal(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), Decimal("0.00"))

    @property
    def tax(self) -> Decimal:
        return (self.subtotal * self.tax_rate).quantize(Decimal("0.01"))

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax


def money(value: Decimal, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def render_pdf(spec: DocSpec) -> bytes:
    """A digital PDF with a text layer."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4, invariant=1)  # invariant: the same spec gives the same bytes
    width, height = A4
    labels = LABELS[spec.layout % len(LABELS)]
    y = height - 60
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, y, TITLES[spec.kind])
    c.setFont("Helvetica", 10)
    c.drawRightString(width - 50, y, spec.vendor_name)
    y -= 16
    if spec.vendor_tax_id:
        c.drawRightString(width - 50, y, f"Tax ID {spec.vendor_tax_id}")
    y -= 30
    number_label = (
        labels["number"]
        if spec.kind in ("invoice", "credit_note")
        else {"purchase_order": "PO Number", "goods_receipt": "GRN No"}[spec.kind]
    )
    rows = [(number_label, spec.number), (labels["date"], spec.doc_date.strftime("%d %b %Y"))]
    if spec.due_date and spec.kind == "invoice":
        rows.append((labels["due"], spec.due_date.strftime("%d %b %Y")))
    if spec.po_number and spec.kind != "purchase_order":
        rows.append((labels["po"], spec.po_number))
    rows.append(
        (
            "Bill To" if spec.kind != "purchase_order" else "Supplier",
            spec.buyer_name if spec.kind != "purchase_order" else spec.vendor_name,
        )
    )
    for label, value in rows:
        c.drawString(50, y, f"{label}:")
        c.drawString(170, y, value)
        y -= 16
    y -= 20
    c.setFont("Helvetica-Bold", 10)
    for x, head in ((50, "Item"), (120, "Description"), (330, "Qty"), (390, "Unit Price"), (480, "Amount")):
        c.drawString(x, y, head)
    c.setFont("Helvetica", 10)
    y -= 16
    for ln in spec.lines:
        c.drawString(50, y, ln.item_code)
        c.drawString(120, y, ln.description[:34])
        c.drawString(330, y, f"{ln.quantity.normalize():f}")
        c.drawString(390, y, f"{ln.unit_price:,.2f}")
        c.drawString(480, y, f"{ln.amount:,.2f}")
        y -= 15
    if spec.kind in ("invoice", "credit_note", "purchase_order"):
        y -= 14
        for label, value in (
            (labels["subtotal"], spec.subtotal),
            (f"{labels['tax']} {spec.tax_rate * 100:.0f}%", spec.tax),
            (labels["total"], spec.total),
        ):
            c.drawString(330, y, f"{label}:")
            c.drawRightString(width - 50, y, money(value, spec.currency))
            y -= 15
    if spec.bank_account and spec.kind == "invoice":
        y -= 20
        c.drawString(50, y, f"Pay to account: {spec.bank_account}")
    if spec.note:
        y -= 24
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(50, y, spec.note[:110])
    c.showPage()
    c.save()
    return out.getvalue()


def degrade(png: bytes, quality: str, seed: int) -> Image.Image:
    q, rng = QUALITY[quality], random.Random(seed)
    image = Image.open(io.BytesIO(png)).convert("L")
    if q["angle"]:
        image = image.rotate(rng.uniform(-q["angle"], q["angle"]), expand=True, fillcolor=255)
    if q["blur"]:
        image = image.filter(ImageFilter.GaussianBlur(q["blur"]))
    if q["noise"]:
        noise = Image.effect_noise(image.size, q["noise"])
        image = Image.blend(image, noise, 0.08)
    if q["scale"] != 1.0:
        image = image.resize((int(image.width * q["scale"]), int(image.height * q["scale"])))
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=q["jpeg"])
    return Image.open(io.BytesIO(out.getvalue()))


def render_scan(spec: DocSpec, quality: str = "medium", *, as_pdf: bool = True, seed: int = 0, dpi: int = 200) -> bytes:
    """The document as a scanned PDF (image only, no text layer) or as a JPEG photo."""
    with pymupdf.open(stream=render_pdf(spec), filetype="pdf") as doc:
        png = doc[0].get_pixmap(dpi=dpi).tobytes("png")
    image = degrade(png, quality, seed)
    out = io.BytesIO()
    if as_pdf:
        image.save(out, "PDF", resolution=dpi)
    else:
        image.save(out, "JPEG", quality=QUALITY[quality]["jpeg"])
    return out.getvalue()
