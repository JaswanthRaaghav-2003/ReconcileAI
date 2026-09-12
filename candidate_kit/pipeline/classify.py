"""classify.py — document classification and routing module.

Decides:
1. Is this document a payable at all?
   - Non-payables (dunning reminders, internal forms, pure delivery notes) -> declined[]
2. How many payables does it contain? (0, 1, or several)
3. What type is each payable? (INVOICE or CREDIT_MEMO)
4. Filters out supporting attachment pages (e.g. delivery receipts, airway bills,
   payment remittance advice slips, backup utility bills).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PayablePages:
    invoice_type: str  # "INVOICE" or "CREDIT_MEMO"
    pages: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ClassificationResult:
    file: str
    is_payable: bool
    declined: List[Dict[str, str]] = field(default_factory=list)
    payables: List[PayablePages] = field(default_factory=list)


# Keywords indicating non-payable dunning letters / reminders
REMINDER_KEYWORDS = [
    "mahnung",
    "zahlungserinnerung",
    "reminder",
    "dunning",
    "overdue notice",
    "kontoauszug",
    "statement of account",
]

# Keywords indicating internal forms / non-billing correspondence
INTERNAL_FORM_KEYWORDS = [
    "donations and charitable",
    "charitable contributions",
    "sponsorship form",
    "internal document",
    "approval form",
    "requisition",
]

# Keywords indicating non-bookable estimates / quotations
ESTIMATE_KEYWORDS = [
    "estimate",
    "quotation",
    "proforma",
    "pro-forma",
]

# Keywords indicating credit notes
CREDIT_KEYWORDS = [
    "credit note",
    "credit memo",
    "gutschrift",
    "kreeditarve",
    "nota de crédito",
    "nota de credito",
    "credit invoice",
    "stornorechnung",
]

# Page types / titles that represent supporting attachments, not invoice pages
ATTACHMENT_PAGE_TYPES = {
    "DELIVERY_NOTE",
    "AIRWAY_BILL",
    "REMITTANCE_SLIP",
}

ATTACHMENT_TITLE_KEYWORDS = [
    "delivery note",
    "delivery receipt",
    "lieferschein",
    "guia de remessa",
    "house air waybill",
    "air waybill",
    "shipment cartage advice",
    "shipment receipt",
    "payment advice",
    "remittance advice",
    "zaadeteht",
    "cargo permit",
]


def is_attachment_page(page: Dict[str, Any]) -> bool:
    """Detects if a single page is a supporting attachment rather than a primary invoice page."""
    ptype = str(page.get("page_type") or "").upper()
    if ptype in ATTACHMENT_PAGE_TYPES:
        return True

    title = str(page.get("document_title") or "").lower()
    for kw in ATTACHMENT_TITLE_KEYWORDS:
        if kw in title:
            return True

    return False


def detect_credit_memo(text_blob: str, pages: List[Dict[str, Any]]) -> bool:
    """Detects if the payable is a credit memo based on titles, keywords, or negative totals."""
    lower = text_blob.lower()
    for kw in CREDIT_KEYWORDS:
        if kw in lower:
            return True

    for p in pages:
        title = str(p.get("document_title") or "").lower()
        if any(kw in title for kw in CREDIT_KEYWORDS):
            return True
        ptype = str(p.get("page_type") or "").upper()
        if ptype == "CREDIT_MEMO":
            return True
        # Check if gross total is explicitly printed as negative
        gross = str(p.get("totals", {}).get("gross_total") or "").strip()
        if gross.startswith("-") or gross.endswith("-"):
            return True

    return False


def classify_document(doc_data: Dict[str, Any]) -> ClassificationResult:
    """Classifies a document into bookable payable(s) or declined record(s)."""
    filename = doc_data.get("file", "")
    pages = doc_data.get("pages", [])

    if not pages:
        return ClassificationResult(
            file=filename,
            is_payable=False,
            declined=[{"doc_type": "EMPTY_DOCUMENT", "reason": "No pages or content extracted from document"}]
        )

    # Aggregate text for keyword checking
    all_titles = " ".join(str(p.get("document_title") or "") for p in pages).lower()
    all_notes = " ".join(str(p.get("notes") or "") for p in pages).lower()
    combined_header = f"{filename} {all_titles} {all_notes}".lower()

    # 1. Check for Dunning Letter / Reminder
    for kw in REMINDER_KEYWORDS:
        if kw in all_titles or any(kw in str(p.get("document_title") or "").lower() for p in pages):
            return ClassificationResult(
                file=filename,
                is_payable=False,
                declined=[{
                    "doc_type": "REMINDER",
                    "reason": f"Dunning letter / payment reminder ({kw}), not an original bookable invoice"
                }]
            )

    # 2. Check for Internal Form / Correspondence
    for kw in INTERNAL_FORM_KEYWORDS:
        if kw in combined_header:
            return ClassificationResult(
                file=filename,
                is_payable=False,
                declined=[{
                    "doc_type": "INTERNAL_FORM",
                    "reason": f"Internal request or authorization form ({kw}), not a supplier invoice or bookable payable"
                }]
            )

    # 3. Check for Non-bookable Estimate / Quotation
    for kw in ESTIMATE_KEYWORDS:
        if any(kw == str(p.get("document_title") or "").lower().strip() for p in pages) or any(str(p.get("document_title") or "").lower().startswith(kw) for p in pages):
            return ClassificationResult(
                file=filename,
                is_payable=False,
                declined=[{
                    "doc_type": "ESTIMATE",
                    "reason": f"Price quotation / estimate ({kw}), not a finalized tax invoice or bookable payable"
                }]
            )

    # 4. Check for pure Delivery Notes / Shipping documentation
    non_attachment_pages = [p for p in pages if not is_attachment_page(p)]
    if not non_attachment_pages:
        # All pages are delivery notes or shipping docs
        return ClassificationResult(
            file=filename,
            is_payable=False,
            declined=[{
                "doc_type": "DELIVERY_NOTE",
                "reason": "Delivery notes and shipping documentation without invoice billing amounts"
            }]
        )

    # 4. Filter attachment pages: retain only invoice pages
    # If the document has a primary invoice on page 1 (or pages 1-2) followed by attachments,
    # separate the payable pages from attachments.
    invoice_pages = non_attachment_pages

    # Check for credit memo
    combined_text = " ".join(
        f"{p.get('document_title', '')} {p.get('notes', '')}" for p in invoice_pages
    )
    is_credit = detect_credit_memo(combined_text, invoice_pages)
    inv_type = "CREDIT_MEMO" if is_credit else "INVOICE"

    # Multi-payable detection:
    # Check if pages contain multiple distinct invoices (different invoice numbers)
    invoice_groups: Dict[str, List[Dict[str, Any]]] = {}
    for p in invoice_pages:
        inv_no = str(p.get("invoice_number") or "").strip()
        # For consolidated invoices (e.g. DU-02), the master invoice on pages 1-2 is the primary payable
        if "consolidated" in str(p.get("document_title") or "").lower():
            inv_no = "CONSOLIDATED"
        invoice_groups.setdefault(inv_no or "DEFAULT", []).append(p)

    payable_list = []
    # If multiple distinct invoice numbers exist, emit one payable per group
    if len(invoice_groups) > 1 and "DEFAULT" not in invoice_groups:
        for inv_no, grp_pages in invoice_groups.items():
            grp_text = " ".join(f"{p.get('document_title', '')} {p.get('notes', '')}" for p in grp_pages)
            grp_type = "CREDIT_MEMO" if detect_credit_memo(grp_text, grp_pages) else "INVOICE"
            payable_list.append(PayablePages(invoice_type=grp_type, pages=grp_pages))
    else:
        payable_list.append(PayablePages(invoice_type=inv_type, pages=invoice_pages))

    return ClassificationResult(
        file=filename,
        is_payable=True,
        payables=payable_list
    )


if __name__ == "__main__":
    import sys
    import json
    from pipeline.extract import extract_document

    if sys.stdout.encoding != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if len(sys.argv) < 2:
        print("Usage: python -m pipeline.classify <pdf_path>")
        sys.exit(1)

    doc_raw = extract_document(sys.argv[1])
    res = classify_document(doc_raw)
    print(f"File: {res.file}")
    print(f"Is Payable: {res.is_payable}")
    if res.declined:
        print(f"Declined: {json.dumps(res.declined, indent=2)}")
    else:
        print(f"Payables Count: {len(res.payables)}")
        for i, pay in enumerate(res.payables):
            print(f"  [{i+1}] Type: {pay.invoice_type}, Pages: {len(pay.pages)}")
