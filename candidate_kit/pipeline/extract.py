"""extract.py — raw document extraction module.

Turns input PDF documents into structured raw page-level representations
faithfully recording printed text, line items, taxes, charges, and totals.
Extracts strictly what is printed on the page — no inferring, balancing, or fudging.
"""
from __future__ import annotations

import os
import json
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None


CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "extractions"


def get_cache_path(pdf_path: str | Path) -> Path:
    """Returns the cached JSON path for a given PDF file."""
    p = Path(pdf_path)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{p.name}.json"


def pdf_to_page_images(pdf_path: str | Path, dpi: int = 200) -> List[bytes]:
    """Renders each page of the PDF into PNG image bytes using PyMuPDF."""
    if fitz is None:
        raise RuntimeError("PyMuPDF (pymupdf) is required for PDF image extraction.")
    doc = fitz.open(str(pdf_path))
    images = []
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        images.append(pix.tobytes("png"))
    doc.close()
    return images


def extract_digital_text(pdf_path: str | Path) -> List[str]:
    """Extracts raw text from each page if a digital text layer exists."""
    if fitz is None:
        return []
    doc = fitz.open(str(pdf_path))
    texts = [page.get_text() for page in doc]
    doc.close()
    return texts


EXTRACTION_PROMPT = """You are a precise accounting document transcription assistant.
Transcribe strictly what is printed on this document page into JSON.
CRITICAL INSTRUCTIONS:
1. Transcribe ONLY what is visibly printed. Never guess, invent, or balance figures.
2. If a field is not printed or not applicable, use empty string "" or empty list [].
3. For numbers, preserve the exact digits. Do NOT pre-multiply, sum, or calculate totals.
4. If taxes are listed on individual lines, record them on those lines. If taxes are listed only at the header / summary table, record them in taxes[].
5. Note the page_type: "INVOICE", "CREDIT_MEMO", "DUNNING_REMINDER", "INTERNAL_FORM", "DELIVERY_NOTE", "AIRWAY_BILL", "REMITTANCE_SLIP", or "OTHER".
6. Return ONLY valid JSON matching this schema:

{
  "document_title": "string (e.g. 'Rechnung', 'Tax Invoice', 'Credit Note', 'Mahnung')",
  "invoice_number": "string",
  "invoice_date": "string (as printed)",
  "due_date": "string (as printed)",
  "currency": "string (e.g. 'EUR', 'USD', 'GBP', 'ZAR', 'THB', 'SGD', 'TRY')",
  "supplier_name": "string",
  "supplier_address": "string",
  "supplier_vat_id": "string",
  "supplier_bank_iban": "string",
  "buyer_name": "string",
  "buyer_address": "string",
  "payment_terms_text": "string (e.g. '30 days net', 'within 14 days')",
  "po_number": "string",
  "line_items": [
    {
      "line_number": "string",
      "description": "string",
      "item_code": "string",
      "uom": "string",
      "quantity": "string",
      "unit_price": "string",
      "discount": "string",
      "discount_percentage": "string",
      "tax_rate": "string",
      "tax_amount": "string",
      "total": "string"
    }
  ],
  "charges_and_discounts": {
    "discount_amount": "string",
    "freight_charges": "string",
    "insurance_charges": "string",
    "extra_charges": "string",
    "excise_duties": "string"
  },
  "taxes": [
    {
      "tax_type": "string",
      "tax_name": "string",
      "tax_rate": "string",
      "tax_amount": "string"
    }
  ],
  "totals": {
    "subtotal": "string",
    "total_tax_amount": "string",
    "gross_total": "string",
    "balance_due": "string",
    "amount_credited": "string"
  },
  "page_type": "INVOICE | CREDIT_MEMO | DUNNING_REMINDER | INTERNAL_FORM | DELIVERY_NOTE | AIRWAY_BILL | REMITTANCE_SLIP | OTHER",
  "notes": "string"
}
"""


def call_vision_llm(image_bytes: bytes) -> Dict[str, Any]:
    """Calls a vision LLM to transcribe a single page image."""
    # Check for Gemini API key
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
        os.environ.setdefault("GLOG_minloglevel", "2")
        import google.generativeai as genai
        from PIL import Image
        import io
        genai.configure(api_key=gemini_key, transport="rest")
        # Try flash first (free-tier friendly), falling back to other vision-capable models if needed
        candidate_models = ["gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-1.5-pro", "gemini-1.5-pro-latest", "gemini-2.0-flash"]
        img = Image.open(io.BytesIO(image_bytes))
        resp = None
        last_err = None
        for m_name in candidate_models:
            try:
                m = genai.GenerativeModel(m_name)
                resp = m.generate_content([EXTRACTION_PROMPT, img])
                if resp and resp.text:
                    break
            except Exception as e:
                last_err = e
                continue

        if resp is None:
            raise RuntimeError(f"Gemini vision call failed across candidate models: {last_err}")

        text = resp.text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text.strip())

    # Check for OpenAI API key
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        from openai import OpenAI
        client = OpenAI(api_key=openai_key)
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": EXTRACTION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64_img}"},
                        },
                    ],
                }
            ],
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)

    raise RuntimeError(
        "No LLM API key found (set GEMINI_API_KEY or OPENAI_API_KEY) and no cached extraction exists."
    )


def extract_document(pdf_path: str | Path, force_refresh: bool = False) -> Dict[str, Any]:
    """Extracts raw structured data from a PDF document.

    If a cached extraction is available and force_refresh is False, loads and returns
    the cached extraction. Otherwise, renders the PDF and runs extraction.
    """
    pdf_p = Path(pdf_path)
    cache_p = get_cache_path(pdf_p)

    if not force_refresh and cache_p.exists():
        try:
            with open(cache_p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    page_images = pdf_to_page_images(pdf_p)
    pages_data = []

    for i, img_bytes in enumerate(page_images):
        page_data = call_vision_llm(img_bytes)
        page_data["page_number"] = i + 1
        pages_data.append(page_data)

    doc_data = {
        "file": pdf_p.name,
        "num_pages": len(page_images),
        "pages": pages_data,
    }

    # Save to cache
    with open(cache_p, "w", encoding="utf-8") as f:
        json.dump(doc_data, f, indent=2, ensure_ascii=False)

    return doc_data


if __name__ == "__main__":
    import sys
    if sys.stdout.encoding != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(sys.argv) < 2:
        print("Usage: python -m pipeline.extract <pdf_path>")
        sys.exit(1)
    res = extract_document(sys.argv[1])
    print(json.dumps(res, indent=2, ensure_ascii=False))
