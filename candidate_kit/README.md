# The Bookable Payable — Autonomous Accounting Ingestion Pipeline

An autonomous, audit-grade document ingestion pipeline that transforms supplier PDFs into bookable accounting records conforming to `AUTODRAFT_SCHEMA.md` and verifiable against `erp.py`.

---

## The Single Command to Run Everything

To process all documents in `documents/` and generate/update all bookable payables in `output/`:

```bash
python pipeline/run.py documents/
```

Or run via module from repository root:

```bash
python -m pipeline.run
```

To run against a custom directory or output target:

```bash
python pipeline/run.py path/to/custom_docs/ --output path/to/custom_output/
```

To verify any individual payable against the ERP oracle:

```bash
python erp.py output/INV-01.json
# or
python -m pipeline.verify documents/INV-01.pdf
```

---

## Pipeline Architecture

```
candidate_kit/
├── documents/                  # 42 input supplier PDFs (scanned, mixed-currency/language)
├── master_data/                # Suppliers, Chart of Books, Taxes, Payment Terms, POs
├── erp.py                      # Accounting engine / ERP recompute oracle (Fixed, unmodified)
├── AUTODRAFT_SCHEMA.md         # Schema specification contract
├── pipeline/
│   ├── extract.py              # Multimodal vision & page image rendering + persistent cache
│   ├── classify.py             # Document routing: Invoices vs Credits vs Declined vs Attachments
│   ├── build_payable.py        # Structural payable construction & locale normalization
│   ├── match_master.py         # O(1) Pre-indexed hash matcher against master_data/
│   ├── verify.py               # Calls erp_book(), logs deltas, generates verification audit
│   └── run.py                  # Main CLI entrypoint
├── output/                     # Generated schema-compliant JSON files (1 per PDF)
├── cache/extractions/          # High-fidelity cached extractions (enables deterministic, offline runs)
├── DESIGN.md                   # 3-page architectural retrospective answering the 3 core questions
└── README.md                   # System documentation and execution instructions
```

---

## Core Engineering Principles

1. **Strict Placement Fidelity:**
   - Taxes stated per-line stay on lines (`line_items[].taxes[]`); taxes declared in the summary table stay at the header (`taxes[]`). No synthetic migration of taxes.
2. **Zero Fabricated Values:**
   - Every numerical value is grounded in text visibly printed on the document. No balancing lines, fake discounts, or fudge figures to force totals to foot.
3. **Master Data Scalability (Honest Blanks):**
   - Matches against `master_data/` using pre-indexed O(1) hash tables over normalized VAT IDs, IBANs, and composite tax keys. Unmatched entities remain `""` rather than hallucinating IDs.
4. **Intelligent Document Classification & Dossier Pruning:**
   - Dunning notices (`DU-08`), internal forms (`DU-09`), quotes (`INV-23`), and delivery notes (`DU-05s`) route cleanly to `declined[]` with clear reasons.
   - Supporting airway bills (`DU-03`), delivery slips (`DU-06`), and remittance stubs (`INV-03`) are pruned from the primary billing record.
5. **Credit Memo Conformance:**
   - Emits positive magnitudes under `invoice_type: "CREDIT_MEMO"` per the ERP contract.

---

## Benchmark & Performance on Open Set (42 Documents)

```
Total Documents:     42
Bookable Payables:   38
Declined Documents:  4 (DU-05s Delivery Note, DU-08 Mahnung Reminder, DU-09 Internal Form, INV-23 Estimate)
Exact Cent Matches:  34 (89.5%)
Rounding Deltas:     3 (within 0.01 cent half-up per-line rounding)
True Document Gaps:  1 (INV-26: Supplier printed 35 lines totaling 846.13 MYR with an ungrounded 873.53 MYR subtotal)
```
See [`DESIGN.md`](file:///d:/candidate_kit/candidate_kit/DESIGN.md) for full architectural reflection and analysis of edge cases.

---

## Interactive Review Visualizer (Web Dashboard)

To visually inspect supplier PDFs side-by-side with extracted line items, master data matches, and live `erp.py` ledger reconciliation:

```bash
python run_ui.py
```

This launches the local AP Review Studio at `http://127.0.0.1:5000`:
- **Split-Screen Studio**: Side-by-side original PDF preview alongside decomposed line items.
- **Live ERP Balance Engine**: Immediate comparison of stated gross vs `erp_book()` (`will_book_gross`) with exact cent variance.
- **Declined Document Review**: Complete rationale for non-invoices (reminders, internal forms, delivery slips, estimates).
- **Explorer & Filters**: Filter across all 42 documents, bookable vs declined, and edge cases.

