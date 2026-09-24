"""Locate evidence on original PDF pages, with bounded OCR for scanned CVs.

Run in a worker process: PDFium is not thread safe. Coordinates are normalized
against the rendered page, including the PDF's intrinsic rotation.
"""
import base64
import ctypes
import io
from contextlib import closing


def locate_pdf_evidence(content: bytes, query: str) -> dict:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    import pytesseract

    if len(content) > 30 * 1024 * 1024:
        raise ValueError("Le PDF dépasse la limite de consultation de 30 Mo.")
    query = query.strip()
    if not query or len(query) > 600:
        raise ValueError("Le texte recherché doit contenir entre 1 et 600 caractères.")
    pages = []
    ocr_count = 0
    partial = False
    with pdfium.PdfDocument(content) as doc:
        total = len(doc)
        partial = total > 30
        for number in range(min(total, 30)):
            with closing(doc[number]) as page:
                boxes = []
                scale = min(2, 1600 / max(page.get_size()))
                with closing(page.get_textpage()) as text:
                    searchable = bool(text.get_text_range().strip())
                    search = text.search(query, match_case=False, match_whole_word=True)
                    try:
                        while len(boxes) < 100:
                            match = search.get_next()
                            if match is None:
                                break
                            for index in range(match[0], match[0] + match[1]):
                                boxes.append(text.get_charbox(index))
                    finally:
                        search.close()
                if not boxes and searchable:
                    continue
                if not searchable and ocr_count >= 5:
                    partial = True
                    continue
                with closing(page.render(scale=scale)) as bitmap:
                    picture = bitmap.to_pil()
                    width, height = picture.size
                    highlights = []
                    for left, bottom, right, top in boxes:
                        corners = []
                        for x, y in ((left, bottom), (right, top)):
                            dx, dy = ctypes.c_int(), ctypes.c_int()
                            raw.FPDF_PageToDevice(page, 0, 0, width, height, 0, x, y,
                                                  ctypes.byref(dx), ctypes.byref(dy))
                            corners.append((dx.value / width, dy.value / height))
                        x1, x2 = sorted(p[0] for p in corners)
                        y1, y2 = sorted(p[1] for p in corners)
                        highlights.append([x1, y1, x2 - x1, y2 - y1])
                    method = "text"
                    if not searchable:
                        ocr_count += 1
                        method = "ocr"
                        try:
                            data = pytesseract.image_to_data(picture, output_type=pytesseract.Output.DICT, timeout=4)
                            words = [(i, word.casefold().strip(",;:()")) for i, word in enumerate(data["text"]) if word.strip()]
                            wanted = [w.casefold().strip(",;:()") for w in query.split()]
                            for start in range(len(words) - len(wanted) + 1):
                                group = words[start:start + len(wanted)]
                                if [word for _, word in group] == wanted:
                                    for i, _ in group:
                                        highlights.append([data["left"][i] / width, data["top"][i] / height,
                                                           data["width"][i] / width, data["height"][i] / height])
                        except (RuntimeError, pytesseract.TesseractError):
                            partial = True
                    if highlights:
                        output = io.BytesIO()
                        picture.save(output, format="PNG")
                        pages.append({"page": number + 1, "method": method, "boxes": highlights,
                                      "image": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()})
                if len(pages) >= 3:
                    partial = partial or number + 1 < total
                    break
    return {"pages": pages, "total_pages": total, "partial": partial, "query": query}
