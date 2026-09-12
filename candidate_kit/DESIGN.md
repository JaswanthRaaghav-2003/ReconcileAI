# DESIGN.md: The Bookable Payable System Architecture

---

## 1. What did you eventually understand about these documents that you did not understand on day one?

On day one, this problem masquerades as document extraction: run OCR or a multimodal model over a PDF, locate the standard key-value pairs (supplier, dates, line items, totals), and serialize to JSON. That approach stalls almost immediately. Feed those naive extractions to `erp.py`, and only a fraction book correctly. Worse, the failures look completely uncorrelated: some documents book for double their tax, some drop discounts, others miss gross totals by bizarre amounts.

The fundamental shift occurred when moving from **document transcription** to **accounting structure**. `erp.py` does not grade textual resemblance; it executes a strict, deterministic algebraic recomputation:
$$\text{gross} = \text{line\_bases} - \text{header\_discount} + \text{line\_taxes} + \text{header\_taxes} + \text{other\_charges}$$

Understanding this revealed four core insights that unified what initially seemed like unrelated bugs:

### A. Structural Placement Defines the Arithmetic Base
A tax stated in an invoice is not simply a rate and a figure; its location dictating what base it applies to is an architectural decision:
- **Per-Line Taxes:** In multi-rate invoices (e.g., Portuguese `DU-06` with 6% and 23% IVA, or Estonian `INV-13` with 0% and 24% KM), each item carries its own tax. Placing these on `line_items[].taxes[]` allows the ERP to compute taxes line-by-line. If those taxes are also summarized in header `taxes[]`, the ERP sums both, adding the tax twice.
- **Header Taxes:** On documents where taxes are declared once in a summary footer (e.g., German `INV-01` with Reverse Charge 0% VAT, or South African `INV-03` with 15% VAT), lines do not have per-line tax rates. Inventing per-line tax rates on those lines corrupts the structural shape.

### B. Compound Bases, Non-Line Levies, and Withholding Deductions
Real supplier invoices do not conform to simple `quantity × price + 15% VAT`:
- **Compound Bases:** On Thai invoice `HLD-01`, VAT 7% (549.36 THB) is levied on the subtotal *including* a 9% agency management fee (648.00 THB), not the bare line item total. On Ghanaian invoice `INV-19`, statutory levies (NHIL 2.5%, GETFund 2.5%, COVID-19 1%) are calculated first, and standard VAT 15% is applied on the levy-inclusive base.
- **Withholding Taxes:** In `HLD-01`, a 3% withholding tax (-235.44 THB) is deducted from the payable. The ERP schema supports negative `tax_amount` values at the header specifically to model withholding deductions.
- **Excise & Returnable Charges:** On Portuguese beverage invoice `HLD-05`, alcohol excise duty (IEC) is incorporated directly into the net taxable base (`Base de Incidência`), while returnable bottle deposits (`Depósito SDR`, 25.80 EUR) represent non-taxable charges printed alongside goods.

### C. Documents are Multi-Page Packets (Dossiers), Not Single Invoices
Real AP document batches include supporting evidentiary documents:
- `DU-06` is an invoice on page 1 attached to a delivery receipt on page 2.
- `DU-03` is an international freight invoice on pages 1–2 followed by 10 pages of House Airway Bills and cargo permits.
- `INV-03` has an attached payment remittance advice slip on page 2.
- `INV-31` is a tenant utility charge on page 1 backed by a multi-page PECO electric utility bill on pages 2–3.
- `INV-25` contains an "Original" invoice followed by an identical "Duplicado" copy.

Treating every page as an invoice or concatenating lines from supporting slips destroys payable validity. The pipeline must filter out attachments and isolate the primary billing instrument.

### D. Credit Memos and Sign Conventions
Documents like `DU-10` ("Credit Note") and `DU-11` ("Kreeditarve") represent credit payables. On physical documents, credit notes frequently print negative quantities, negative unit prices, and negative totals (e.g., `-400.00 EUR`). However, the ERP contract explicitly specifies that credit memos share the exact same schema keys, delivered as **positive magnitudes** with `invoice_type: "CREDIT_MEMO"`. Mistaking a negative credit memo tax for a withholding tax causes the tax to be subtracted instead of added, failing the recompute.

---

## 2. When your system hits an unfamiliar document, what does it actually do, and why does that generalize rather than guess?

When faced with an unseen document (such as those in the held-back test set), the system follows a deterministic five-stage pipeline designed around strict traceability and pre-indexed resolution:

```
PDF Document
    │
    ▼
[1. extract.py]       ── Render 200 DPI images -> Multimodal vision extraction (transcription only)
    │
    ▼
[2. classify.py]      ── Rule-based routing: Payable vs Declined, Credit vs Invoice, Attachment filtering
    │
    ▼
[3. build_payable.py] ── Number/Date normalization, Structural tax placement (Line vs Header)
    │
    ▼
[4. match_master.py]  ── O(1) Indexed matching against master_data/ (Honest blanks on no-match)
    │
    ▼
[5. verify.py]        ── Recomputation via erp_book(), delta logging, output/<file>.json emission
```

### Stage 1: Faithful Vision Extraction (Zero Calculation)
The extraction prompt forbids the LLM from balancing figures, inventing missing lines, or pre-computing totals. It extracts what is visually present: printed numbers, line item tables, currency symbols, and text blocks. If a text layer is present (`fitz`), digital text is extracted alongside rendered page images.

### Stage 2: Structural Classification & Dossier Pruning
The classifier determines document category before attempting to build payables:
- **Declined Routing:** Dunning letters / payment reminders (`mahnung`, `reminder`), internal request forms (`donations and charitable contributions`), and pure delivery/shipping packets (`delivery note`, `airway bill`) are immediately routed to `declined[]` with clear structural justifications.
- **Attachment Pruning:** For multi-page documents, pages matching attachment signatures (packing slips, customs declarations, remittance stubs) are separated from the primary invoice pages.
- **Type Assignment:** Credit keywords (`credit note`, `kreeditarve`, `gutschrift`, negative gross) designate `invoice_type: "CREDIT_MEMO"`.

### Stage 3: Deterministic Construction & Structural Placement
- **Locale Normalization:** Converts European dot/comma conventions (`1.234,56 €` $\rightarrow$ `1234.56`) and calendar formats (including Thai Buddhist calendar BE 2569 $\rightarrow$ 2026 CE) into ISO standards without altering magnitudes.
- **Tax Placement Rule:** If lines contain dedicated per-line tax columns, taxes remain strictly on `line_items[].taxes[]`, leaving header `taxes[]` empty (unless non-line withholdings exist). If taxes are stated exclusively in the summary block, they are bound at the header level.

### Stage 4: Scalable Master Data Matching (Honest Blanks Over Hallucinations)
To generalize to production enterprise catalogs with hundreds of thousands of rows:
- `match_master.py` builds in-memory hash tables on startup:
  - **Suppliers:** Primary O(1) lookup on normalized VAT ID / tax ID (e.g., `DE209177122`, `PT502345678`), secondary lookup on cleaned IBAN, and tertiary token-intersection on normalized entity names.
  - **Buyer Organization:** Matches location and company codes against `chart_of_books.json` using country codes and address keywords (e.g., Tallinn HQ $\rightarrow$ `BOLTGROUP / EE001 / LOC_EE_001`).
  - **Taxes:** O(1) lookup against `tax_master.json` indexed by `(country_code, rate_float, tax_type)` (e.g., `('EE', 24.0, 'VAT')` $\rightarrow$ `EST_240_VAT`, `('PT', 13.0, 'IVA')` $\rightarrow$ `PT_130_IVA`, `('TH', 7.0, 'VAT')` $\rightarrow$ `TH_070_VAT`).
  - **Payment Terms:** Maps aliases ("30 days", "net 10", "immediate") and computes date deltas `(due_date - invoice_date)`.
  - **Purchase Orders:** Direct lookup against `po_master.json`.
- **The Honest Blank Rule:** If an entity is not in master data, its ID is set to `""`. The system never invents a code.

### Stage 5: Verification & Auditability
Every generated payable is passed to `erp.erp_book()`. The returned `will_book_gross` is compared to the printed `gross_total`, and any delta is logged.

---

## 3. Which document genuinely can't be solved the way the others were — and how did you determine that, rather than faking an answer?

### The Document: `INV-26.pdf` (Meridian Print Sdn Bhd, Malaysia)

### How It Was Determined
`INV-26.pdf` is a two-page Malaysian retail invoice issued in Malaysian Ringgit (`MYR`) with 0% SST (`MY_000_SST`).

Page 1 itemizes 35 individual lines of food, beverage, and packaging supplies (Wipes, Juices, Chips, Oats, Tea, etc.). Each line has an explicitly printed quantity, unit price, and extended line total:
- Line 1: `2 × 14.20 = 28.40`
- Line 2: `2 × 10.95 = 21.90`
- Line 3: `2 × 6.90 = 13.80`
- ...
- Line 29: `2 × 6.60 = 13.20`
- *(Between Line 29 and 31: a row with description "Northwind Consulting SA" and blank numerical columns)*
- Line 31: `1 × 33.88 = 33.88`
- Line 32: `1 × 12.60 = 12.60`
- Line 33: `1 × 12.60 = 12.60`
- Line 34: `10 × 0.20 = 2.00`
- Line 35: `4 × 0.50 = 2.00`

Summing every single printed line item on the invoice:
$$\sum_{i=1}^{35} \text{line\_total}_i = 846.13\text{ MYR}$$

However, at the foot of page 1 and in the tax specification table on page 2, the document explicitly prints:
- `Subtotal: 873.53`
- `Total Excl. SST: 873.53`
- `Total Incl. SST: 873.53`
- `SST Base: 873.53, SST Amount: 0.00`

The discrepancy is exactly:
$$873.53 - 846.13 = 27.40\text{ MYR}$$

### Why It Cannot Be Solved Like the Others
There is no printed line item, freight charge, insurance fee, or extra charge of 27.40 MYR anywhere on the document. High-resolution cropping of the region between Line 29 and Line 31 reveals that Line 30 contains the supplier/client text "Northwind Consulting SA", but the quantity, rate, and amount columns are completely blank. 

The vendor's billing system either:
1. Truncated Line 30's numbers (which should have been 27.40 MYR), or
2. Carried an unitemized surcharge or rounding error of 27.40 MYR.

### Refusing to Fake the Answer
Rule 1 of the mandate states:
> *"Every value you emit must appear on the document. A number that is in your output only because it made the total come out right disqualifies that payable. If you are ever tempted to invent a figure to balance the books, the temptation is telling you something true about the document — listen to it instead of acting on it."*

It would have been trivial to insert a fictitious line item or invent an `extra_charges: "27.40"` to force `will_book_gross` to foot to 873.53. Doing so would violate the core engineering mandate. 

Our pipeline extracts all 35 visible line items totaling 846.13 MYR, records the stated gross as 873.53 MYR, and logs the 27.40 MYR delta. This discrepancy is not a pipeline defect — it is a genuine, unresolvable vendor error that an automated accounting gateway must flag for human review rather than silently falsifying records to balance the ledger.
