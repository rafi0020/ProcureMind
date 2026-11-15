from dataclasses import dataclass, field


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class PageText:
    """One page's words with their boxes. Units: PDF points for PDFs, pixels for images, cells for sheets."""

    page: int
    words: list[Word] = field(default_factory=list)
    source: str = "text_layer"  # text_layer | ocr | sheet
    width: float = 0
    height: float = 0

    @property
    def text(self) -> str:
        """Words in reading order, a line break where the vertical position jumps."""
        out, last_y = [], None
        for w in sorted(self.words, key=lambda w: (round(w.y0 / 4), w.x0)):
            if last_y is not None and abs(w.y0 - last_y) > 4:
                out.append("\n")
            elif out:
                out.append(" ")
            out.append(w.text)
            last_y = w.y0
        return "".join(out)
