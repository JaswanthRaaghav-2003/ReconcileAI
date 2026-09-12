"""verify.py — ERP verification and audit logging module.

Feeds payables to erp.py's erp_book(), compares will_book_gross with the document's
own stated gross_total, and logs exact matches, rounding deltas, and discrepancies.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from erp import erp_book


@dataclass
class VerificationResult:
    will_book_gross: float
    currency: str
    stated_gross: float
    delta: float
    is_exact_match: bool
    is_close_match: bool
    status: str
    details: str = ""


def verify_payable(payable: Dict[str, Any]) -> VerificationResult:
    """Invokes the ERP oracle on one payable and compares against stated gross."""
    erp_res = erp_book(payable)
    will_book = erp_res.get("will_book_gross", 0.0)
    curr = erp_res.get("currency", "")

    stated_str = str(payable.get("gross_total") or "").strip()
    try:
        stated = float(stated_str) if stated_str else 0.0
    except ValueError:
        stated = 0.0

    delta = round(will_book - stated, 2)
    is_exact = abs(delta) < 1e-4
    is_close = abs(delta) <= 0.02

    if is_exact:
        status = "EXACT_MATCH"
        details = f"Recomputed gross matches stated total exactly ({will_book:.2f} {curr})."
    elif is_close:
        status = "ROUNDING_DELTA"
        details = f"Minor rounding delta of {delta:+.2f} {curr} (will_book={will_book:.2f}, stated={stated:.2f})."
    else:
        status = "DISCREPANCY"
        details = f"Gross delta of {delta:+.2f} {curr} (will_book={will_book:.2f}, stated={stated:.2f})."

    return VerificationResult(
        will_book_gross=will_book,
        currency=curr,
        stated_gross=stated,
        delta=delta,
        is_exact_match=is_exact,
        is_close_match=is_close,
        status=status,
        details=details,
    )


if __name__ == "__main__":
    import sys
    from pipeline.extract import extract_document
    from pipeline.classify import classify_document
    from pipeline.build_payable import build_payable

    if sys.stdout.encoding != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    targets = sys.argv[1:] if len(sys.argv) > 1 else [
        "documents/INV-01.pdf",
        "documents/HLD-01.pdf",
        "documents/DU-10.pdf",
        "documents/DU-11.pdf",
        "documents/DU-06.pdf",
    ]

    print(f"{'Document':<20} | {'Type':<12} | {'Stated':<10} | {'ERP Book':<10} | {'Delta':<8} | {'Status'}")
    print("-" * 75)

    for path in targets:
        p = Path(path)
        doc = extract_document(p)
        cl = classify_document(doc)
        if not cl.is_payable:
            print(f"{p.name:<20} | {'DECLINED':<12} | {'-':<10} | {'-':<10} | {'-':<8} | {cl.declined[0]['doc_type']}")
            continue
        for pay_pages in cl.payables:
            pay = build_payable(pay_pages)
            v = verify_payable(pay)
            print(f"{p.name:<20} | {pay['invoice_type']:<12} | {v.stated_gross:<10.2f} | {v.will_book_gross:<10.2f} | {v.delta:<+8.2f} | {v.status}")
