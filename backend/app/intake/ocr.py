"""OCR with Tesseract, run on the CPU, with word positions."""

import pytesseract
from PIL import Image

from app.intake.words import Word

DPI = 300


class OcrUnavailable(RuntimeError):
    pass


def _upright(image: Image.Image) -> Image.Image:
    """Turn a page that was scanned sideways or upside down (Tesseract's orientation detection). Small skews of a
    degree or two are left to Tesseract, which reads them well."""
    try:
        rotate = int(pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT).get("rotate", 0))
    except pytesseract.TesseractError:
        return image  # too little text to tell; read it as it is
    return image.rotate(-rotate, expand=True) if rotate else image


def ocr_image(image: Image.Image, scale: float = 1.0) -> list[Word]:
    """Words found on the image; coordinates multiplied by `scale` (to convert pixels to PDF points)."""
    try:
        image = _upright(image.convert("L"))
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrUnavailable("OCR is not available: the tesseract program is not installed.") from exc
    words = []
    for i, raw in enumerate(data["text"]):
        text = raw.strip()
        if text and float(data["conf"][i]) > 0:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            words.append(Word(text, x * scale, y * scale, (x + w) * scale, (y + h) * scale))
    return words
