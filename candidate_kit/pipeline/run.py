"""run.py — the main execution entrypoint for the Bookable Payable ingestion pipeline.

Single-command execution over an input directory of PDFs (default: documents/)
producing output/<file>.json for every PDF conforming strictly to AUTODRAFT_SCHEMA.md.

Usage:
    python pipeline/run.py [documents_dir] [--output output_dir]
or
    python -m pipeline.run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
import sys

# Ensure repository root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.build_payable import build_payable
from pipeline.classify import classify_document
from pipeline.extract import extract_document
from pipeline.verify import verify_payable


def process_document(pdf_path: Path) -> Dict[str, Any]:
    """Processes a single PDF through extraction, classification, payable construction, and verification."""
    # 1. Extraction (cached or vision-extracted)
    raw_doc = extract_document(pdf_path)

    # 2. Classification & Attachment filtering
    class_res = classify_document(raw_doc)

    out_record: Dict[str, Any] = {
        "file": pdf_path.name,
        "payables": [],
        "declined": [],
    }

    if not class_res.is_payable:
        out_record["declined"] = class_res.declined
        return out_record

    # 3. Build each bookable payable
    for pay_pages in class_res.payables:
        payable_obj = build_payable(pay_pages)
        out_record["payables"].append(payable_obj)

    return out_record


def run_pipeline(documents_dir: str | Path, output_dir: str | Path) -> None:
    """Runs the entire pipeline over all PDFs in documents_dir and writes output JSONs."""
    docs_path = Path(documents_dir)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(docs_path.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDF files found in {docs_path.resolve()}")
        return

    print(f"Processing {len(pdf_files)} documents from {docs_path} -> {out_path}...\n")
    print(f"{'#':<3} | {'Document':<14} | {'Type':<12} | {'Stated':<10} | {'ERP Book':<10} | {'Delta':<7} | {'Status'}")
    print("-" * 75)

    stats = {
        "total": len(pdf_files),
        "payables": 0,
        "declined": 0,
        "exact_matches": 0,
        "rounding_deltas": 0,
        "discrepancies": 0,
    }

    for idx, pdf in enumerate(pdf_files, 1):
        try:
            result = process_document(pdf)

            # Write JSON output matching AUTODRAFT_SCHEMA exactly
            out_file = out_path / f"{pdf.stem}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

            if result["declined"]:
                stats["declined"] += 1
                dec_type = result["declined"][0].get("doc_type", "DECLINED")
                print(f"{idx:<3} | {pdf.name:<14} | {'DECLINED':<12} | {'-':<10} | {'-':<10} | {'-':<7} | {dec_type}")
            else:
                for pay in result["payables"]:
                    stats["payables"] += 1
                    v = verify_payable(pay)
                    if v.is_exact_match:
                        stats["exact_matches"] += 1
                    elif v.is_close_match:
                        stats["rounding_deltas"] += 1
                    else:
                        stats["discrepancies"] += 1

                    print(f"{idx:<3} | {pdf.name:<14} | {pay['invoice_type']:<12} | {v.stated_gross:<10.2f} | {v.will_book_gross:<10.2f} | {v.delta:<+7.2f} | {v.status}")

        except Exception as e:
            print(f"{idx:<3} | {pdf.name:<14} | ERROR        | -          | -          | -       | {e}")

    print("\n" + "=" * 75)
    print(f"Execution Summary:")
    print(f"  Total Documents:  {stats['total']}")
    print(f"  Bookable Payables: {stats['payables']}")
    print(f"  Declined:         {stats['declined']}")
    print(f"  Exact Matches:    {stats['exact_matches']}")
    print(f"  Rounding Deltas:  {stats['rounding_deltas']} (<= 0.02)")
    print(f"  Discrepancies:    {stats['discrepancies']}")
    print("=" * 75)


def main():
    if sys.stdout.encoding != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Bookable Payable Ingestion Pipeline")
    parser.add_argument("documents", nargs="?", default="documents", help="Path to documents folder")
    parser.add_argument("--output", "-o", default="output", help="Path to output folder")
    args = parser.parse_args()

    run_pipeline(args.documents, args.output)


if __name__ == "__main__":
    main()
