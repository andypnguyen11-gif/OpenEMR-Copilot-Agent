# Multimodal expansion — per-type cost & latency profile

This file documents the cost and latency behavior of the 5 new
document types added in the W2-MM multimodal expansion (referral
.docx, fax-packet .tiff, patient workbook .xlsx, HL7 v2 ORU-R01,
HL7 v2 ADT-A08). It complements the COST.md tracking the supervisor
/ chat-side spend.

The headline: **four of the five new types incur zero VLM cost** —
they parse structured text (DOCX/XLSX) or pipe-delimited HL7
streams deterministically. Only the fax-packet TIFF goes through
Claude vision, and only because faxes are scanned bilevel images.

## Per-type profile

| Type | Strategy | LLM calls | Cost / doc | p50 latency | p95 latency |
|---|---|---|---|---|---|
| `lab_pdf` | Claude vision (existing) | 1 per page | ~$0.05–$0.10 | ~12 s | ~28 s |
| `intake_form` | Claude vision (existing) | 1 per page | ~$0.05–$0.15 | ~14 s | ~32 s |
| `referral_docx` | python-docx + regex | 0 | $0 | ~50 ms | ~150 ms |
| `fax_tiff` | Claude vision per page | 1 per page (4–5 pages) | ~$0.20–$0.40 | ~35 s | ~80 s |
| `workbook_xlsx` | openpyxl | 0 | $0 | ~40 ms | ~120 ms |
| `hl7_oru` | Custom HL7 parser | 0 | $0 | ~5 ms | ~20 ms |
| `hl7_adt` | Custom HL7 parser | 0 | $0 | ~5 ms | ~20 ms |

Latencies are measured locally on the cohort-5 sample files (M-series
Mac, no network round-trip for the text-only extractors). The vision
extractors include the Anthropic round-trip latency, which dominates.

## Cost model — projected production volume

Assume 100 patients/day per provider, with the following rough mix:

- 1 lab PDF and 1 intake form per new patient (~20% are new) — ~20 of
  each per day.
- 5–10 referrals received per day.
- 2–4 fax packets per day (mostly cover sheet + 3 body pages).
- 30–50 lab ORU messages per day from reference labs.
- 5–10 ADT-A08 demographics updates per day.
- 1 workbook export per pre-visit prep batch (~3–5 per day).

Daily VLM cost per provider:

```
  20 lab PDFs    × $0.10  =  $2.00
  20 intake forms × $0.10  =  $2.00
   3 fax packets × $0.30  =  $0.90
  ────────────────────────
  Daily total:              ~$4.90
```

The text-only extractors (referral, workbook, HL7) are zero-marginal
because they don't call any LLM — the volume could 10× without a
budget impact.

## Bottlenecks

1. **Anthropic API round-trip** dominates every VLM extractor. A
   single-page lab PDF spends ~10× more time waiting for the API
   than Pillow spends rendering the page.
2. **Multi-page TIFF** is the worst-case document — 4–5 vision calls
   serialized. Could parallelize the per-page calls if p95 becomes
   a problem; not done in the demo cut.
3. **HL7 parsing is essentially free** — bottlenecked only by
   filesystem read.

## What this expansion did NOT add

- No async queue. Every extractor still runs synchronously on the
  ingest request. Production-scale throughput needs the W2-02 queue
  (deferred).
- No batching. Each document is its own API call (where applicable);
  Anthropic batch is a future cost optimization.
- No caching. Re-uploading the same document re-runs extraction.
  The `extracted_facts` table (W2-03 deferred) is the natural
  cache key.
