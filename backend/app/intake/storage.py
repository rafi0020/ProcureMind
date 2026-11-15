"""Files on disk, by organisation and content hash. The same file is stored once per organisation."""

import hashlib
import io
import os
import zipfile
from pathlib import Path
from uuid import UUID

import pymupdf

from app.config import get_settings

PDF, PNG, JPEG, TIFF = "application/pdf", "image/png", "image/jpeg", "image/tiff"
XLSX, CSV = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/csv"


class Rejected(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def sniff(data: bytes, filename: str) -> str | None:
    """The type from the bytes themselves; the file name and the declared type are not trusted."""
    if data.startswith(b"%PDF-"):
        return PDF
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return TIFF
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return XLSX if "xl/workbook.xml" in z.namelist() else None
        except zipfile.BadZipFile:
            return None
    if filename.lower().endswith(".csv"):
        try:
            sample = data[:4096].decode("utf-8")
        except UnicodeDecodeError:
            return None
        return CSV if "," in sample and "\x00" not in sample else None
    return None


def check(data: bytes, filename: str) -> tuple[str, int | None]:
    """Content type and page count, or Rejected with the HTTP status to answer."""
    settings = get_settings()
    if len(data) > settings.max_file_mb * 1024 * 1024:
        raise Rejected(413, f"{filename} is larger than {settings.max_file_mb} MB.")
    if not data:
        raise Rejected(422, f"{filename} is empty.")
    content_type = sniff(data, filename)
    if content_type is None:
        raise Rejected(415, f"{filename} is not a PDF, PNG, JPEG, TIFF, XLSX or CSV file.")
    pages = None
    if content_type == PDF:
        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                if doc.needs_pass:
                    raise Rejected(422, f"{filename} is password-protected.")
                pages = doc.page_count
        except (pymupdf.FileDataError, RuntimeError) as exc:
            raise Rejected(422, f"{filename} is not a readable PDF.") from exc
        if pages > settings.max_pdf_pages:
            raise Rejected(422, f"{filename} has {pages} pages; the limit is {settings.max_pdf_pages}.")
    return content_type, pages


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def path_for(org_id: UUID, digest: str) -> Path:
    return Path(get_settings().files_dir) / str(org_id) / digest


def save(org_id: UUID, data: bytes) -> str:
    digest = sha256(data)
    path = path_for(org_id, digest)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # never a half-written file under the final name
    return digest


def read(org_id: UUID, digest: str) -> bytes:
    return path_for(org_id, digest).read_bytes()
