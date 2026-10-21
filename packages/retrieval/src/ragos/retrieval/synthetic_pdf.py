"""Deterministic small ASCII PDF fixture for local-only model/RAG probes.

Adapted from the existing test_pdf_byte_extraction fixture; this is NOT a
general PDF builder. All real PDF extraction still goes through Poppler.
Never pass customer document bytes into a synthetic-fixture builder.
"""

from __future__ import annotations

from ragos.core.errors import ValidationError


def make_synthetic_pdf(pages: list[str]) -> bytes:
    """Generate a bounded, text-only PDF with one physical page per string."""
    if (
        not isinstance(pages, list)
        or not 1 <= len(pages) <= 10
        or any(
            not isinstance(text, str)
            or not 1 <= len(text) <= 500
            or not text.isascii()
            or any(ord(c) < 32 or ord(c) > 126 for c in text)
            for text in pages
        )
    ):
        raise ValidationError("synthetic PDF requires 1-10 nonempty ASCII text pages")

    count = len(pages)
    font_id = 3 + 2 * count
    children = " ".join(f"{3 + 2 * i} 0 R" for i in range(count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{children}] /Count {count} >>".encode(),
    ]
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode()
        )
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 50 740 Td ({escaped}) Tj ET".encode("ascii")
        objects.append(
            b"<< /Length " + str(len(stream)).encode()
            + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, item in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{i} 0 obj\n".encode())
        pdf.extend(item)
        pdf.extend(b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(pdf)
