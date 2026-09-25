import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from app.services.pdf_evidence import locate_pdf_evidence


def pdf(rotation=0):
    writer = PdfWriter()
    for words in ("Unrelated page", "Python developer"):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 18 Tf 50 700 Td ({words}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        page.rotate(rotation)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_locates_correct_page_and_visible_boxes(rotation):
    result = locate_pdf_evidence(pdf(rotation), "python")
    assert [page["page"] for page in result["pages"]] == [2]
    assert result["pages"][0]["image"].startswith("data:image/png;base64,")
    for x, y, w, h in result["pages"][0]["boxes"]:
        assert 0 <= x < 1 and 0 <= y < 1
        assert 0 < w < 1 and 0 < h < 1


def test_no_invented_evidence():
    assert locate_pdf_evidence(pdf(), "Java")["pages"] == []


def test_rejects_empty_query():
    with pytest.raises(ValueError):
        locate_pdf_evidence(pdf(), " ")


def test_scanned_page_ocr_boxes(monkeypatch):
    import pytesseract
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    writer.write(output)
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *args, **kwargs: {
        "text": ["Python"], "left": [100], "top": [200], "width": [60], "height": [20],
    })
    result = locate_pdf_evidence(output.getvalue(), "Python")
    assert result["pages"][0]["method"] == "ocr"
    assert result["pages"][0]["boxes"]
