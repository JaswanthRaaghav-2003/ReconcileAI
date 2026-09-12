"""app.py — AP Automation & ERP Review Visualizer Server.

Provides a web dashboard and REST APIs to inspect supplier invoice PDFs
side-by-side with extracted payables, master data matches, and live ERP
reconciliation results. Supports real-time API Key configuration and live
vision re-extraction for testing by evaluators.
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template, send_from_directory, abort, request
from werkzeug.utils import secure_filename

# Ensure repository root and candidate_kit are on sys.path
_FRONTEND_DIR = Path(__file__).resolve().parent
_CK_DIR = _FRONTEND_DIR.parent
_REPO_ROOT = _CK_DIR.parent

for p in [str(_CK_DIR), str(_REPO_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from erp import erp_book, num, round2
except ImportError:
    from candidate_kit.erp import erp_book, num, round2

try:
    from pipeline.verify import verify_payable
except ImportError:
    from candidate_kit.pipeline.verify import verify_payable

try:
    from pipeline.run import process_document
    from pipeline.extract import CACHE_DIR, extract_document
except ImportError:
    from candidate_kit.pipeline.run import process_document
    from candidate_kit.pipeline.extract import CACHE_DIR, extract_document


app = Flask(__name__, template_folder=str(_FRONTEND_DIR / "templates"))

# Determine documents and output directories
DOC_DIRS = [
    _CK_DIR / "documents",
    _REPO_ROOT / "candidate_kit" / "documents",
    _REPO_ROOT / "documents",
]
OUTPUT_DIRS = [
    _CK_DIR / "output",
    _REPO_ROOT / "candidate_kit" / "output",
    _REPO_ROOT / "output",
]

def get_doc_dir() -> Path:
    for d in DOC_DIRS:
        if d.is_dir():
            return d
    return _CK_DIR / "documents"

def get_output_dir() -> Path:
    for d in OUTPUT_DIRS:
        if d.is_dir():
            return d
    return _CK_DIR / "output"


def _load_document_summary(json_path: Path) -> Dict[str, Any]:
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as err:
        return {
            "file": json_path.stem + ".pdf",
            "status": "ERROR",
            "error": str(err)
        }

    file_name = data.get("file", json_path.stem + ".pdf")
    payables = data.get("payables") or []
    declined = data.get("declined") or []

    if declined and not payables:
        first_dec = declined[0]
        return {
            "file": file_name,
            "is_payable": False,
            "status": "DECLINED",
            "doc_type": first_dec.get("doc_type", "DECLINED"),
            "reason": first_dec.get("reason", "Declined"),
            "supplier_name": "-",
            "invoice_number": "-",
            "invoice_date": "-",
            "currency": "-",
            "stated_gross": 0.0,
            "will_book_gross": 0.0,
            "delta": 0.0,
            "payables_count": 0,
            "declined_count": len(declined),
        }

    if not payables:
        return {
            "file": file_name,
            "is_payable": False,
            "status": "EMPTY",
            "doc_type": "UNKNOWN",
            "reason": "No payables found",
            "supplier_name": "-",
            "invoice_number": "-",
            "invoice_date": "-",
            "currency": "-",
            "stated_gross": 0.0,
            "will_book_gross": 0.0,
            "delta": 0.0,
            "payables_count": 0,
            "declined_count": 0,
        }

    # Primary payable evaluation
    p0 = payables[0]
    ver = verify_payable(p0)
    
    # Categorize status badge
    if ver.is_exact_match:
        status_category = "EXACT_MATCH"
    elif ver.is_close_match:
        status_category = "ROUNDING_DELTA"
    else:
        status_category = "DISCREPANCY"

    supp = p0.get("supplier") or {}
    return {
        "file": file_name,
        "is_payable": True,
        "status": status_category,
        "doc_type": p0.get("invoice_type", "INVOICE"),
        "reason": ver.details,
        "supplier_name": supp.get("name") or "Unknown Supplier",
        "supplier_id": supp.get("supplier_id") or "",
        "invoice_number": p0.get("invoice_number") or "-",
        "invoice_date": p0.get("invoice_date") or "-",
        "due_date": p0.get("due_date") or "-",
        "currency": ver.currency or p0.get("currency") or "EUR",
        "stated_gross": ver.stated_gross,
        "will_book_gross": ver.will_book_gross,
        "delta": ver.delta,
        "payables_count": len(payables),
        "declined_count": len(declined),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    out_dir = get_output_dir()
    json_files = sorted(out_dir.glob("*.json"))
    
    total_docs = len(json_files)
    bookable = 0
    declined = 0
    exact_matches = 0
    rounding_deltas = 0
    discrepancies = 0
    currencies: Dict[str, float] = {}

    for jf in json_files:
        summary = _load_document_summary(jf)
        if not summary.get("is_payable", False):
            declined += 1
        else:
            bookable += 1
            st = summary.get("status")
            if st == "EXACT_MATCH":
                exact_matches += 1
            elif st == "ROUNDING_DELTA":
                rounding_deltas += 1
            else:
                discrepancies += 1
            curr = summary.get("currency") or "EUR"
            currencies[curr] = round(currencies.get(curr, 0.0) + summary.get("will_book_gross", 0.0), 2)

    return jsonify({
        "total_documents": total_docs,
        "bookable_payables": bookable,
        "declined_documents": declined,
        "exact_matches": exact_matches,
        "rounding_deltas": rounding_deltas,
        "discrepancies": discrepancies,
        "currency_totals": currencies,
        "reconciliation_rate_pct": round(((exact_matches + rounding_deltas) / max(1, bookable)) * 100, 1)
    })


@app.route("/api/documents")
def api_documents():
    out_dir = get_output_dir()
    json_files = sorted(out_dir.glob("*.json"))
    items = [_load_document_summary(jf) for jf in json_files]
    return jsonify(items)


@app.route("/api/documents/<filename>")
def api_document_detail(filename: str):
    if not filename.endswith(".json"):
        base = filename[:-4] if filename.lower().endswith(".pdf") else filename
        json_name = f"{base}.json"
    else:
        json_name = filename

    out_dir = get_output_dir()
    target_path = out_dir / json_name
    if not target_path.exists():
        abort(404, description=f"Document {json_name} not found in output directory")

    with open(target_path, "r", encoding="utf-8") as fh:
        raw_data = json.load(fh)

    payables = raw_data.get("payables") or []
    declined = raw_data.get("declined") or []

    verifications = []
    for p in payables:
        ver = verify_payable(p)
        verifications.append({
            "will_book_gross": ver.will_book_gross,
            "currency": ver.currency,
            "stated_gross": ver.stated_gross,
            "delta": ver.delta,
            "is_exact_match": ver.is_exact_match,
            "is_close_match": ver.is_close_match,
            "status": ver.status,
            "details": ver.details,
        })

    pdf_name = raw_data.get("file", target_path.stem + ".pdf")
    doc_dir = get_doc_dir()
    pdf_exists = (doc_dir / pdf_name).exists()

    return jsonify({
        "file": pdf_name,
        "has_pdf": pdf_exists,
        "payables": payables,
        "declined": declined,
        "verifications": verifications,
        "raw_json": raw_data,
    })


@app.route("/api/pdf/<filename>")
def api_pdf(filename: str):
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"

    doc_dir = get_doc_dir()
    pdf_path = doc_dir / filename
    if not pdf_path.exists():
        abort(404, description=f"PDF {filename} not found")

    return send_from_directory(
        directory=str(doc_dir),
        path=filename,
        mimetype="application/pdf"
    )


# --- API Key Configuration & Live Extraction Features ---

def _mask_key(key: str | None) -> str:
    if not key or len(key) < 8:
        return ""
    return f"{key[:6]}...{key[-4:]}"


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.get_json() or {}
        gemini_key = data.get("gemini_api_key", "").strip()
        openai_key = data.get("openai_api_key", "").strip()
        
        if gemini_key:
            os.environ["GEMINI_API_KEY"] = gemini_key
        if openai_key:
            os.environ["OPENAI_API_KEY"] = openai_key

    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    openai_key = os.environ.get("OPENAI_API_KEY") or ""

    if gemini_key:
        active_mode = "LIVE_GEMINI"
        mode_desc = "Gemini 1.5 Pro (Live Vision API)"
    elif openai_key:
        active_mode = "LIVE_OPENAI"
        mode_desc = "OpenAI GPT-4o (Live Vision API)"
    else:
        active_mode = "OFFLINE_CACHE"
        mode_desc = "Local Deterministic Cache (42/42 Offline, $0 Cost)"

    cached_count = len(list(CACHE_DIR.glob("*.json"))) if CACHE_DIR.exists() else 0

    return jsonify({
        "has_gemini_key": bool(gemini_key),
        "has_openai_key": bool(openai_key),
        "masked_gemini_key": _mask_key(gemini_key),
        "masked_openai_key": _mask_key(openai_key),
        "active_mode": active_mode,
        "mode_description": mode_desc,
        "cached_documents_count": cached_count,
    })


@app.route("/api/test-key", methods=["POST"])
def api_test_key():
    data = request.get_json() or {}
    provider = data.get("provider", "gemini").lower()
    key = data.get("key", "").strip(' "\'\r\n\t')

    if not key:
        env_key = os.environ.get("GEMINI_API_KEY") if provider == "gemini" else os.environ.get("OPENAI_API_KEY")
        key = (env_key or "").strip(' "\'\r\n\t')

    if not key:
        return jsonify({"success": False, "message": "No API key provided. Please enter a key in the input field."})

    try:
        if provider == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=key, transport="rest")
            model = genai.GenerativeModel("gemini-1.5-flash")
            # Minimal token count ping to test authentication
            count_res = model.count_tokens("Ping test connection.")
            return jsonify({
                "success": True,
                "message": f"Successfully authenticated with Gemini API! (Connection verified, token count: {count_res.total_tokens})"
            })
        elif provider == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=key)
            client.models.list()
            return jsonify({
                "success": True,
                "message": "Successfully authenticated with OpenAI API!"
            })
        else:
            return jsonify({"success": False, "message": f"Unknown provider: {provider}"})
    except Exception as err:
        err_msg = str(err)
        if "API key not valid" in err_msg or "API_KEY_INVALID" in err_msg:
            clean = "API key not valid. Please verify your Google AI Studio API key at https://aistudio.google.com"
        elif "PERMISSION_DENIED" in err_msg:
            clean = "Permission denied. Check that your Google Cloud / AI Studio project has the Generative Language API enabled."
        elif "RESOURCE_EXHAUSTED" in err_msg or "Quota exceeded" in err_msg:
            clean = "Quota limit reached on this API key. Try again in a few moments or check your quota."
        else:
            clean = f"Authentication failed: {err_msg}"
        return jsonify({"success": False, "message": clean})



@app.route("/api/reprocess/<filename>", methods=["POST"])
def api_reprocess_document(filename: str):
    """Re-runs the extraction and accounting pipeline on a specific document."""
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"

    doc_dir = get_doc_dir()
    pdf_path = doc_dir / filename
    if not pdf_path.exists():
        abort(404, description=f"PDF {filename} not found.")

    data = request.get_json() or {}
    force_live = data.get("force_live", False)

    try:
        if force_live:
            # Check key
            has_key = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or os.environ.get("OPENAI_API_KEY"))
            if not has_key:
                return jsonify({
                    "success": False,
                    "message": "Cannot force live vision extraction: No GEMINI_API_KEY or OPENAI_API_KEY configured."
                }), 400
            # Force refresh extraction
            extract_document(pdf_path, force_refresh=True)

        # Run pipeline
        out_record = process_document(pdf_path)

        # Save to output dirs
        json_name = f"{pdf_path.stem}.json"
        for out_dir in [get_output_dir(), _REPO_ROOT / "output"]:
            if out_dir.is_dir():
                with open(out_dir / json_name, "w", encoding="utf-8") as fh:
                    json.dump(out_record, fh, indent=2, ensure_ascii=False)

        return jsonify({
            "success": True,
            "message": f"Successfully reprocessed {filename} with {'live vision LLM' if force_live else 'cache'}!",
            "record": out_record
        })
    except Exception as err:
        return jsonify({"success": False, "message": f"Processing error: {str(err)}"}), 500


@app.route("/api/upload", methods=["POST"])
def api_upload_document():
    """Allows uploading a new PDF document into documents/ and processing it."""
    if "file" not in request.files:
        return jsonify({"success": False, "message": "No file part in request"}), 400
    
    file = request.files["file"]
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "message": "Only PDF files are supported"}), 400

    safe_name = secure_filename(file.filename)
    doc_dir = get_doc_dir()
    save_path = doc_dir / safe_name
    file.save(str(save_path))

    # Also mirror to root documents if exists
    root_doc_dir = _REPO_ROOT / "candidate_kit" / "documents"
    if root_doc_dir.is_dir() and root_doc_dir != doc_dir:
        import shutil
        shutil.copy2(save_path, root_doc_dir / safe_name)

    try:
        out_record = process_document(save_path)
        json_name = f"{save_path.stem}.json"
        for out_dir in [get_output_dir(), _REPO_ROOT / "output"]:
            if out_dir.is_dir():
                with open(out_dir / json_name, "w", encoding="utf-8") as fh:
                    json.dump(out_record, fh, indent=2, ensure_ascii=False)

        return jsonify({
            "success": True,
            "message": f"Uploaded and processed {safe_name}!",
            "file": safe_name,
            "record": out_record
        })
    except Exception as err:
        return jsonify({"success": False, "message": f"Document uploaded, but processing error: {str(err)}"}), 500


def run_server(port: int = 5000, debug: bool = False):
    print(f"================================================================")
    print(f"  AP Automation & ERP Review Visualizer")
    print(f"  Serving Documents from: {get_doc_dir()}")
    print(f"  Serving Outputs from:   {get_output_dir()}")
    print(f"  URL: http://127.0.0.1:{port}")
    print(f"================================================================")
    app.run(host="127.0.0.1", port=port, debug=debug)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AP Automation Visualizer Server")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    run_server(port=args.port, debug=args.debug)
