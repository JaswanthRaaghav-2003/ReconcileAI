"""build_payable.py — payable record construction module.

Transforms raw extracted page data and classification into AUTODRAFT_SCHEMA-compliant
payable JSON objects.
Enforces all engineering constraints:
1. Faithfully preserves document structure (header taxes vs line taxes, decomposed lines).
2. Clean dot-decimal number formatting (converting European locale 1.234,56 -> 1234.56).
3. ISO YYYY-MM-DD dates (including Thai Buddhist calendar 2569 -> 2026).
4. Credit memos emit positive magnitudes per spec.
5. Resolves master data codes via match_master.py, leaving "" when no match exists.
6. Zero fabricated figures or balancing items.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pipeline.classify import PayablePages
from pipeline.match_master import get_matcher


def clean_decimal(v: Any, make_positive: bool = False) -> str:
    """Converts localized number strings (e.g. '1.234,56 €', '438,00', '-400,00')
    into a clean dot-decimal string (e.g. '1234.56', '438.00', '400.00').
    Empty or unparseable input becomes ''.
    """
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""

    # Check negative
    is_neg = s.startswith("-") or s.endswith("-") or (s.startswith("(") and s.endswith(")"))

    # Remove currency symbols, %, and whitespace
    s = re.sub(r"[€$£¥₹%() \t]", "", s)
    s = s.strip("-").strip()
    if not s:
        return ""

    # Detect European format: 1.234,56 or 123,45
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            # 1.234,56 -> 1234.56
            s = s.replace(".", "").replace(",", ".")
        else:
            # 1,234.56 -> 1234.56
            s = s.replace(",", "")
    elif "," in s:
        # Check if comma is decimal separator (e.g. 438,00)
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2, 3, 4, 6):
            s = parts[0] + "." + parts[1]
        else:
            s = s.replace(",", "")

    # Validate float
    try:
        val = float(s)
        if make_positive:
            val = abs(val)
        elif is_neg and val > 0:
            val = -val
        if val == int(val) and "." not in s:
            return str(int(val))
        return f"{val:.2f}" if abs(val - round(val, 2)) < 1e-4 else str(val)
    except ValueError:
        return ""


def to_iso_date(s: str) -> str:
    """Converts dates to ISO YYYY-MM-DD.

    Handles European formats, Thai Buddhist calendar (BE 2569 -> 2026 CE), and English text dates.
    """
    if not s:
        return ""
    s = s.strip()

    # Match YYYY-MM-DD
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)
    if m:
        y, mth, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y > 2400:  # Thai Buddhist era
            y -= 543
        return f"{y:04d}-{mth:02d}-{d:02d}"

    # Match DD.MM.YYYY or DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", s)
    if m:
        d, mth, y_str = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y_str)
        if y < 100:
            y += 2000
        elif y > 2400:
            y -= 543
        return f"{y:04d}-{mth:02d}-{d:02d}"

    # Text formats like '18-Jun-2018' or '30 Apr 2025' or '4. Juni 2025'
    month_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "juni": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }
    s_lower = s.lower()
    for mname, mnum in month_map.items():
        if mname in s_lower:
            nums = re.findall(r"\d+", s)
            if len(nums) >= 2:
                d = int(nums[0])
                y = int(nums[1])
                if y < 100:
                    y += 2000
                elif y > 2400:
                    y -= 543
                return f"{y:04d}-{mnum:02d}-{d:02d}"

    return s


def infer_country(vat_id: str = "", currency: str = "", address: str = "") -> str:
    """Infers the country code from VAT prefix, currency, or address keywords."""
    norm_vat = vat_id.strip().upper()
    for prefix in ["DE", "EE", "GB", "PT", "ZA", "MY", "GH", "KE", "SG", "TH", "US"]:
        if norm_vat.startswith(prefix):
            return prefix

    addr_upper = address.upper()
    if "ESTONIA" in addr_upper or "TALLINN" in addr_upper or "EESTI" in addr_upper:
        return "EE"
    if "GERMANY" in addr_upper or "DEUTSCHLAND" in addr_upper:
        return "DE"
    if "PORTUGAL" in addr_upper or "LISBOA" in addr_upper:
        return "PT"
    if "SOUTH AFRICA" in addr_upper or "JOHANNESBURG" in addr_upper or "SANDTON" in addr_upper:
        return "ZA"
    if "MALAYSIA" in addr_upper or "KUALA LUMPUR" in addr_upper:
        return "MY"
    if "GHANA" in addr_upper or "ACCRA" in addr_upper:
        return "GH"
    if "SINGAPORE" in addr_upper:
        return "SG"
    if "THAILAND" in addr_upper or "BANGKOK" in addr_upper:
        return "TH"
    if "UNITED KINGDOM" in addr_upper or "LONDON" in addr_upper:
        return "GB"

    curr_map = {
        "ZAR": "ZA", "GBP": "GB", "THB": "TH", "MYR": "MY", "GHS": "GH", "KES": "KE", "SGD": "SG"
    }
    return curr_map.get(currency.upper(), "")


def build_payable(payable_pages: PayablePages) -> Dict[str, Any]:
    """Constructs one complete payable object matching AUTODRAFT_SCHEMA.md."""
    pages = payable_pages.pages
    if not pages:
        return {}

    first_page = pages[0]
    inv_type = payable_pages.invoice_type
    is_credit = inv_type == "CREDIT_MEMO"

    # Merge header info from pages
    inv_num = str(first_page.get("invoice_number") or "").strip()
    inv_date = to_iso_date(str(first_page.get("invoice_date") or ""))
    due_date = to_iso_date(str(first_page.get("due_date") or ""))
    currency = str(first_page.get("currency") or "EUR").upper()
    po_num = str(first_page.get("po_number") or "").strip()

    # Raw supplier and buyer
    raw_supp_name = str(first_page.get("supplier_name") or "").strip()
    raw_supp_addr = str(first_page.get("supplier_address") or "").strip()
    raw_supp_vat = str(first_page.get("supplier_vat_id") or "").strip()
    raw_supp_iban = str(first_page.get("supplier_bank_iban") or "").strip()

    raw_buyer_name = str(first_page.get("buyer_name") or "").strip()
    raw_buyer_addr = str(first_page.get("buyer_address") or "").strip()
    buyer_full_text = f"{raw_buyer_name} {raw_buyer_addr}"

    country = infer_country(vat_id=raw_supp_vat, currency=currency, address=f"{raw_supp_addr} {raw_supp_name}")

    # Resolve master data
    matcher = get_matcher()
    supplier_id, master_supp = matcher.match_supplier(
        name=raw_supp_name,
        vat_id=raw_supp_vat,
        bank_iban=raw_supp_iban,
        country=country,
    )
    # If master supplier matched, use canonical address/vat if missing from raw extraction
    supp_addr = raw_supp_addr or master_supp.get("address", "")
    supp_vat = raw_supp_vat or master_supp.get("vat_id", "")

    buyer_codes = matcher.match_buyer(buyer_text=buyer_full_text, country_code=country)

    raw_terms_text = str(first_page.get("payment_terms_text") or "").strip()
    payment_term_id = matcher.match_payment_term(
        text=raw_terms_text,
        invoice_date_str=first_page.get("invoice_date", ""),
        due_date_str=first_page.get("due_date", ""),
    )

    po_id = matcher.match_po(po_num)

    # Line items across all pages belonging to this payable
    all_line_items: List[Dict[str, Any]] = []
    has_line_level_taxes = False

    for page in pages:
        for li in page.get("line_items", []):
            qty_str = clean_decimal(li.get("quantity"), make_positive=is_credit) or "1"
            unit_price_str = clean_decimal(li.get("unit_price"), make_positive=is_credit)
            total_str = clean_decimal(li.get("total"), make_positive=is_credit)

            tax_rate_str = clean_decimal(li.get("tax_rate"), make_positive=True)
            tax_amt_str = clean_decimal(li.get("tax_amount"), make_positive=is_credit)

            line_taxes = []
            if tax_rate_str or tax_amt_str:
                has_line_level_taxes = True
                rate_f = float(tax_rate_str) if tax_rate_str else 0.0
                tt_code = matcher.match_tax_code(country=country, rate=rate_f)
                line_taxes.append({
                    "tax_type": "VAT",
                    "tax_name": f"VAT {tax_rate_str}%" if tax_rate_str else "VAT",
                    "tax_rate": tax_rate_str,
                    "tax_amount": tax_amt_str,
                    "tax_type_code": tt_code,
                })

            all_line_items.append({
                "description": str(li.get("description") or "").strip(),
                "item_type": "GOODS" if li.get("item_code") else "SERVICE",
                "uom": str(li.get("uom") or "").strip(),
                "quantity": qty_str,
                "unit_price": unit_price_str,
                "total": total_str,
                "discount": clean_decimal(li.get("discount"), make_positive=True),
                "discount_percentage": clean_decimal(li.get("discount_percentage"), make_positive=True),
                "tax_rate": tax_rate_str,
                "tax_amount": tax_amt_str,
                "taxes": line_taxes,
            })

    # Header charges and discounts
    charges = first_page.get("charges_and_discounts", {})
    disc_amt = clean_decimal(charges.get("discount_amount"), make_positive=True)
    freight = clean_decimal(charges.get("freight_charges"), make_positive=True)
    insurance = clean_decimal(charges.get("insurance_charges"), make_positive=True)
    extra = clean_decimal(charges.get("extra_charges"), make_positive=True)
    excise = clean_decimal(charges.get("excise_duties"), make_positive=True)

    # Header taxes
    header_taxes: List[Dict[str, Any]] = []
    raw_taxes = first_page.get("taxes", [])
    is_reverse_charge = (
        "reverse charge" in str(first_page.get("notes") or "").lower()
        or "reverse charge" in str(first_page.get("document_title") or "").lower()
    )

    if raw_taxes:
        for t in raw_taxes:
            t_name = str(t.get("tax_name") or "")
            t_type_raw = str(t.get("tax_type") or "")
            t_amt_raw = str(t.get("tax_amount") or "")

            # Check if this is an explicit withholding tax
            is_wht = (
                "withholding" in t_name.lower()
                or "wht" in t_type_raw.lower()
                or "retencao" in t_name.lower()
                or "retención" in t_name.lower()
                or (t_amt_raw.strip().startswith("-") and not is_credit)
            )

            # If the document has taxes charged at the line, normal VAT/IVA belongs on the line,
            # not duplicated at the header. Only withholding or non-line taxes stay at the header.
            if has_line_level_taxes and not is_wht:
                continue

            t_rate_str = clean_decimal(t.get("tax_rate"), make_positive=True)
            t_amt_str = clean_decimal(t_amt_raw, make_positive=not is_wht)
            if is_wht and t_amt_str and not t_amt_str.startswith("-"):
                t_amt_str = f"-{t_amt_str}"

            rate_f = float(t_rate_str) if t_rate_str else 0.0
            ttype = "WHT" if is_wht else (t_type_raw.upper() or "VAT")
            tt_code = matcher.match_tax_code(
                country=country,
                rate=rate_f,
                tax_type=ttype,
                is_reverse_charge=is_reverse_charge and rate_f == 0.0
            )

            header_taxes.append({
                "tax_type": ttype,
                "tax_name": t_name or (f"VAT {t_rate_str}%" if t_rate_str else "VAT"),
                "tax_rate": t_rate_str,
                "tax_amount": t_amt_str,
                "tax_type_code": tt_code,
            })
    elif not has_line_level_taxes and is_reverse_charge:
        tt_code = matcher.match_tax_code(country=country, rate=0.0, is_reverse_charge=True)
        header_taxes.append({
            "tax_type": "VAT",
            "tax_name": "VAT Reverse Charge",
            "tax_rate": "0",
            "tax_amount": "0.00",
            "tax_type_code": tt_code,
        })

    # Printed Totals
    totals = first_page.get("totals", {})
    gross_total = clean_decimal(totals.get("gross_total"), make_positive=is_credit)
    subtotal = clean_decimal(totals.get("subtotal"), make_positive=is_credit)
    total_tax = clean_decimal(totals.get("total_tax_amount"), make_positive=is_credit)

    return {
        "invoice_number": inv_num,
        "invoice_date": inv_date,
        "due_date": due_date,
        "invoice_type": inv_type,
        "currency": currency,

        "supplier": {
            "name": raw_supp_name,
            "supplier_id": supplier_id,
            "address": supp_addr,
            "vat_id": supp_vat,
        },
        "buyer": {
            "company_code": buyer_codes.get("company_code", ""),
            "business_unit_code": buyer_codes.get("business_unit_code", ""),
            "location_code": buyer_codes.get("location_code", ""),
        },
        "payment_term_id": payment_term_id,
        "po_number": po_num,
        "po_id": po_id,

        "gross_total": gross_total,
        "subtotal": subtotal,
        "total_tax_amount": total_tax,

        "discount_amount": disc_amt,
        "freight_charges": freight,
        "insurance_charges": insurance,
        "extra_charges": extra,
        "excise_duties": excise,

        "taxes": header_taxes,
        "line_items": all_line_items,
    }


if __name__ == "__main__":
    import sys
    import json
    from pipeline.extract import extract_document
    from pipeline.classify import classify_document

    if sys.stdout.encoding != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if len(sys.argv) < 2:
        print("Usage: python -m pipeline.build_payable <pdf_path>")
        sys.exit(1)

    doc_raw = extract_document(sys.argv[1])
    class_res = classify_document(doc_raw)
    if not class_res.is_payable:
        print(f"File {class_res.file} is declined:")
        print(json.dumps(class_res.declined, indent=2))
    else:
        for i, pay_pages in enumerate(class_res.payables):
            p = build_payable(pay_pages)
            print(f"--- Payable #{i+1} ({p.get('invoice_type')}) ---")
            print(json.dumps(p, indent=2, ensure_ascii=False))
