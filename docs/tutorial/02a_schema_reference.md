# Database Schema Reference

> **PostgreSQL 16** | **SQLAlchemy 2.0 ORM** | **UUID v4 primary keys** | **JSONB for semi-structured data**
>
> Source: `src/echelonos/db/models.py`

---

## Table of Contents

1. [Entity Relationship Diagram](#entity-relationship-diagram)
2. [Pipeline Stage to Table Mapping](#pipeline-stage-to-table-mapping)
3. [Table Reference](#table-reference)
   - [organizations](#1-organizations)
   - [documents](#2-documents)
   - [fingerprints](#3-fingerprints)
   - [pages](#4-pages)
   - [obligations](#5-obligations)
   - [evidence](#6-evidence-append-only)
   - [document_links](#7-document_links)
   - [flags](#8-flags)
   - [dangling_references](#9-dangling_references)
4. [All Indexes](#all-indexes)
5. [All Foreign Keys and Cascade Rules](#all-foreign-keys-and-cascade-rules)
6. [Enum-Like Column Values](#enum-like-column-values)
7. [JSONB Column Schemas](#jsonb-column-schemas)
8. [Raw DDL (CREATE TABLE Statements)](#raw-ddl)

---

## Entity Relationship Diagram

```
                          +------------------+
                          |  organizations   |
                          |------------------|
                          | id (PK, UUID)    |
                          | name             |
                          | folder_path      |
                          | created_at       |
                          | updated_at       |
                          +--------+---------+
                                   |
                                   | 1:N (CASCADE)
                                   v
+------------------+      +------------------+      +------------------+
| fingerprints     |<---->|    documents      |----->| dangling_refs    |
|------------------|  1:1 |------------------|  1:N |------------------|
| id (PK)          |      | id (PK, UUID)    |      | id (PK)          |
| doc_id (FK, UQ)  |      | org_id (FK)      |      | doc_id (FK)      |
| sha256           |      | filename         |      | reference_text   |
| content_hash     |      | file_path        |      | attempted_matches|
| simhash          |      | file_size_bytes  |      | created_at       |
| structural_fp    |      | status           |      +------------------+
| created_at       |      | doc_type         |
+------------------+      | parties (JSONB)  |
                          | effective_date   |
                          | parent_ref_raw   |
                          | classification   |
                          |   _confidence    |
                          | created_at       |      +------------------+
                          | updated_at       |      | document_links   |
                          +---+---------+----+      |------------------|
                              |    |    |           | id (PK)          |
                  +-----------+    |    +---------->| child_doc_id(FK) |
                  |                |         self   | parent_doc_id(FK)|
                  | 1:N            | 1:N     ref   | link_status      |
                  v                v                | candidates(JSONB)|
       +------------------+  +------------------+  | created_at       |
       |      pages       |  |   obligations    |  +------------------+
       |------------------|  |------------------|
       | id (PK)          |  | id (PK, UUID)    |
       | doc_id (FK)      |  | doc_id (FK)      |
       | page_number      |  | obligation_text  |      +------------------+
       | text             |  | obligation_type  |      |     flags        |
       | tables_markdown  |  | responsible_party|      |------------------|
       | ocr_confidence   |  | counterparty     |      | id (PK)          |
       | created_at       |  | frequency        |      | entity_type      |
       +------------------+  | deadline         |      | entity_id (UUID) |
                             | source_clause    |      | flag_type        |
                             | source_page      |      | message          |
                             | confidence       |      | resolved         |
                             | status           |      | created_at       |
                             | extraction_model |      | resolved_at      |
                             | verification_    |      +------------------+
                             |   model          |          ^
                             | verification_    |          | polymorphic ref
                             |   result (JSONB) |          | (entity_type +
                             | created_at       |          |  entity_id)
                             | updated_at       |----------+
                             +--------+---------+
                                      |
                                      | 1:N (CASCADE)
                                      v
                             +------------------+
                             |    evidence      |
                             |  (APPEND-ONLY)   |
                             |------------------|
                             | id (PK)          |
                             | obligation_id(FK)|
                             | doc_id (FK)      |
                             | page_number      |
                             | section_reference|
                             | source_clause    |
                             | extraction_model |
                             | verification_    |
                             |   model          |
                             | verification_    |
                             |   result         |
                             | confidence       |
                             | amendment_history|
                             |   (JSONB)        |
                             | created_at       |
                             | (NO updated_at!) |
                             +------------------+
```

---

## Pipeline Stage to Table Mapping

| Stage | Reads From | Writes To |
|-------|-----------|-----------|
| **0a** Validation | -- | `documents` (file_path, filename, status, file_size_bytes) |
| **0b** Deduplication | `documents`, `fingerprints` | `fingerprints` (sha256, content_hash, simhash, structural_fingerprint) |
| **1** OCR | `documents` | `pages` (text, tables_markdown, ocr_confidence) |
| **2** Classification | `documents`, `pages` | `documents` (doc_type, parties, effective_date, parent_reference_raw, classification_confidence) |
| **3** Extraction | `documents`, `pages` | `obligations` (all fields), `evidence` (extraction records) |
| **4** Linking | `documents` | `document_links`, `dangling_references` |
| **5** Amendment | `documents`, `obligations`, `document_links` | `obligations` (status update), `evidence` (amendment records) |
| **6** Evidence | `obligations`, `documents`, `evidence` | `evidence` (packaging + status change records) |
| **7** Report | `obligations`, `documents`, `document_links`, `flags` | `flags` |

---

## Table Reference

### 1. organizations

**Purpose:** Top-level tenant. Groups documents by client or project.

| Column | Type | Nullable | Default | Constraint | Notes |
|--------|------|----------|---------|------------|-------|
| `id` | `UUID` | NO | `uuid.uuid4` | **PK** | |
| `name` | `VARCHAR` | NO | -- | | Organization name |
| `folder_path` | `VARCHAR` | YES | `NULL` | | Filesystem path to doc folder |
| `created_at` | `TIMESTAMP` | NO | `utcnow` | | |
| `updated_at` | `TIMESTAMP` | NO | `utcnow` | | Auto-updates on row change |

**Relationships:**
- `documents` -> `Document[]` (1:N, CASCADE delete-orphan)

---

### 2. documents

**Purpose:** Central table. Every uploaded file gets one row. Enriched by Stages 0a, 2.

| Column | Type | Nullable | Default | Constraint | Notes |
|--------|------|----------|---------|------------|-------|
| `id` | `UUID` | NO | `uuid.uuid4` | **PK** | |
| `org_id` | `UUID` | NO | -- | **FK** -> `organizations.id` (CASCADE) | |
| `filename` | `VARCHAR` | NO | -- | | Original filename |
| `file_path` | `VARCHAR` | NO | -- | | Absolute path on disk |
| `file_size_bytes` | `BIGINT` | YES | `NULL` | | File size (64-bit for >2GB files) |
| `status` | `VARCHAR` | NO | `'VALID'` | | `VALID` / `INVALID` / `NEEDS_PASSWORD` |
| `doc_type` | `VARCHAR` | NO | `'UNKNOWN'` | | `MSA` / `SOW` / `Amendment` / `Addendum` / `NDA` / `Order Form` / `Other` / `UNKNOWN` |
| `parties` | `JSONB` | YES | `NULL` | **GIN index** | `["CDW Government LLC", "State of California"]` |
| `effective_date` | `TIMESTAMP` | YES | `NULL` | **B-tree index** | ISO-8601 parsed date |
| `parent_reference_raw` | `TEXT` | YES | `NULL` | | Raw parent ref string from text |
| `classification_confidence` | `FLOAT` | YES | `NULL` | | 0.0 - 1.0, from Stage 2 LLM |
| `created_at` | `TIMESTAMP` | NO | `utcnow` | | |
| `updated_at` | `TIMESTAMP` | NO | `utcnow` | | Auto-updates on row change |

**Indexes:**
- `ix_documents_parties` (GIN on `parties` JSONB)
- `ix_documents_effective_date` (B-tree)
- `ix_documents_org_id` (B-tree)
- `ix_documents_status` (B-tree)

**Relationships:**
- `organization` -> `Organization` (N:1)
- `fingerprint` -> `Fingerprint` (1:1, CASCADE, `uselist=False`)
- `pages` -> `Page[]` (1:N, CASCADE)
- `obligations` -> `Obligation[]` (1:N, CASCADE)
- `evidence_rows` -> `Evidence[]` (1:N, CASCADE)
- `child_links` -> `DocumentLink[]` (1:N, CASCADE, via `child_doc_id`)
- `parent_links` -> `DocumentLink[]` (1:N, via `parent_doc_id`)
- `dangling_references` -> `DanglingReference[]` (1:N, CASCADE)

---

### 3. fingerprints

**Purpose:** 4-layer dedup hashes from Stage 0b. One row per document.

| Column | Type | Nullable | Default | Constraint | Notes |
|--------|------|----------|---------|------------|-------|
| `id` | `UUID` | NO | `uuid.uuid4` | **PK** | |
| `doc_id` | `UUID` | NO | -- | **FK** -> `documents.id` (CASCADE), **UNIQUE** | 1:1 with document |
| `sha256` | `VARCHAR` | YES | `NULL` | **B-tree index** | Layer 1: exact byte hash |
| `content_hash` | `VARCHAR` | YES | `NULL` | **B-tree index** | Layer 2: normalized text hash |
| `simhash` | `BIGINT` | YES | `NULL` | **B-tree index** | Layer 3: 64-bit similarity fingerprint |
| `structural_fingerprint` | `VARCHAR` | YES | `NULL` | **B-tree index** | Layer 4: hash of (doc_type, date, sorted_parties) |
| `created_at` | `TIMESTAMP` | NO | `utcnow` | | |

**Constraints:**
- `uq_fingerprints_doc_id` (UNIQUE on `doc_id`)

**Indexes:**
- `ix_fingerprints_sha256`
- `ix_fingerprints_content_hash`
- `ix_fingerprints_simhash`
- `ix_fingerprints_structural_fingerprint`

**Dedup Layer Logic:**
| Layer | Column | Match Condition | Purpose |
|-------|--------|-----------------|---------|
| 1 | `sha256` | Exact match | Identical byte-for-byte copies |
| 2 | `content_hash` | Exact match | Same text content, different formatting |
| 3 | `simhash` | Hamming distance <= 3 | Near-duplicates (minor edits) |
| 4 | `structural_fingerprint` | **Veto** -- different = not duplicate | Prevents MSA/Amendment false merges |

---

### 4. pages

**Purpose:** Per-page OCR output from Stage 1. Text + tables + confidence.

| Column | Type | Nullable | Default | Constraint | Notes |
|--------|------|----------|---------|------------|-------|
| `id` | `UUID` | NO | `uuid.uuid4` | **PK** | |
| `doc_id` | `UUID` | NO | -- | **FK** -> `documents.id` (CASCADE) | |
| `page_number` | `INTEGER` | NO | -- | | 1-indexed |
| `text` | `TEXT` | YES | `NULL` | | Extracted prose text |
| `tables_markdown` | `TEXT` | YES | `NULL` | | Tables in markdown format |
| `ocr_confidence` | `FLOAT` | YES | `NULL` | | 0.0 - 1.0 per page |
| `created_at` | `TIMESTAMP` | NO | `utcnow` | | |

**Indexes:**
- `ix_pages_doc_id_page_number` (**UNIQUE** composite on `doc_id` + `page_number`)

**Confidence Thresholds:**
| Range | Flag |
|-------|------|
| < 0.50 | `LOW_OCR_QUALITY` (red) |
| 0.50 - 0.80 | `MEDIUM_OCR_QUALITY` (yellow) |
| >= 0.80 | No flag |

---

### 5. obligations

**Purpose:** Extracted contractual obligations from Stage 3. Status updated by Stage 5.

| Column | Type | Nullable | Default | Constraint | Notes |
|--------|------|----------|---------|------------|-------|
| `id` | `UUID` | NO | `uuid.uuid4` | **PK** | |
| `doc_id` | `UUID` | NO | -- | **FK** -> `documents.id` (CASCADE) | Source document |
| `obligation_text` | `TEXT` | NO | -- | | Concise summary of obligation |
| `obligation_type` | `VARCHAR` | YES | `NULL` | **B-tree index** | See allowed types below |
| `responsible_party` | `VARCHAR` | YES | `NULL` | | Role label (e.g., "Vendor") |
| `counterparty` | `VARCHAR` | YES | `NULL` | | Role label (e.g., "Client") |
| `frequency` | `VARCHAR` | YES | `NULL` | | e.g., "Quarterly", "Monthly" |
| `deadline` | `VARCHAR` | YES | `NULL` | | e.g., "30 calendar days" |
| `source_clause` | `TEXT` | YES | `NULL` | | Verbatim clause from document |
| `source_page` | `INTEGER` | YES | `NULL` | | 1-indexed page number |
| `confidence` | `FLOAT` | YES | `NULL` | | 0.0 - 1.0, LLM extraction confidence |
| `status` | `VARCHAR` | NO | `'ACTIVE'` | **B-tree index** | See allowed statuses below |
| `extraction_model` | `VARCHAR` | YES | `NULL` | | e.g., `"claude-opus-4-6"` |
| `verification_model` | `VARCHAR` | YES | `NULL` | | e.g., `"claude-opus-4-6"` |
| `verification_result` | `JSONB` | YES | `NULL` | | See JSONB schema below |
| `created_at` | `TIMESTAMP` | NO | `utcnow` | | |
| `updated_at` | `TIMESTAMP` | NO | `utcnow` | | Auto-updates on row change |

**Indexes:**
- `ix_obligations_doc_id`
- `ix_obligations_status`
- `ix_obligations_obligation_type`

**Allowed `obligation_type` values:**
- `Delivery`
- `Financial`
- `Compliance`
- `SLA`
- `Confidentiality`
- `Termination`
- `Indemnification`
- `Governance`

**Allowed `status` values:**

| Status | Meaning | Set By |
|--------|---------|--------|
| `ACTIVE` | Obligation is currently in force | Default / Stage 5 |
| `SUPERSEDED` | Replaced by a newer amendment clause | Stage 5 (`REPLACE` action) |
| `TERMINATED` | Explicitly deleted by amendment | Stage 5 (`DELETE` action) |
| `UNRESOLVED` | Document not linked; cannot determine | Stage 5 (unlinked docs) |

**Verification Pipeline (Stage 3):**

| Check | Column | Logic |
|-------|--------|-------|
| Grounding | (computed, not stored) | `source_clause in raw_text` |
| Claude Cross-Verification | `verification_result` | Claude reviews obligation vs source |
| Chain-of-Verification (CoVe) | `verification_result` | Only if confidence < 0.80 |

Final status is `VERIFIED` or `UNVERIFIED` based on: `grounded AND claude_verified AND (cove_passed OR cove_skipped)`.

---

### 6. evidence (APPEND-ONLY)

**Purpose:** Immutable audit trail. Every extraction, verification, and status change gets a new row. **No `updated_at` column -- rows are never modified.**

| Column | Type | Nullable | Default | Constraint | Notes |
|--------|------|----------|---------|------------|-------|
| `id` | `UUID` | NO | `uuid.uuid4` | **PK** | |
| `obligation_id` | `UUID` | NO | -- | **FK** -> `obligations.id` (CASCADE) | Which obligation |
| `doc_id` | `UUID` | NO | -- | **FK** -> `documents.id` (CASCADE) | Source document |
| `page_number` | `INTEGER` | YES | `NULL` | | Page where clause found |
| `section_reference` | `VARCHAR` | YES | `NULL` | | e.g., "Section 4.1" |
| `source_clause` | `TEXT` | YES | `NULL` | | Verbatim clause text |
| `extraction_model` | `VARCHAR` | YES | `NULL` | | AI model that extracted |
| `verification_model` | `VARCHAR` | YES | `NULL` | | AI model that verified |
| `verification_result` | `VARCHAR` | YES | `NULL` | | `CONFIRMED` / `DISPUTED` / `UNVERIFIED` |
| `confidence` | `FLOAT` | YES | `NULL` | | 0.0 - 1.0 |
| `amendment_history` | `JSONB` | YES | `NULL` | | Amendment chain records |
| `created_at` | `TIMESTAMP` | NO | `utcnow` | | **Only timestamp -- no updated_at** |

**Indexes:**
- `ix_evidence_obligation_id`
- `ix_evidence_doc_id`

**Design Rules:**
- NEVER UPDATE rows -- always INSERT new ones
- Status changes create new rows with `create_status_change_record()` (Stage 6)
- Full obligation history = all Evidence rows for that `obligation_id`, ordered by `created_at`

---

### 7. document_links

**Purpose:** Parent-child relationships between documents (e.g., Amendment -> MSA). Created by Stage 4.

| Column | Type | Nullable | Default | Constraint | Notes |
|--------|------|----------|---------|------------|-------|
| `id` | `UUID` | NO | `uuid.uuid4` | **PK** | |
| `child_doc_id` | `UUID` | NO | -- | **FK** -> `documents.id` (CASCADE) | The Amendment/SOW/Addendum |
| `parent_doc_id` | `UUID` | YES | `NULL` | **FK** -> `documents.id` (SET NULL) | The parent MSA (NULL if unlinked) |
| `link_status` | `VARCHAR` | NO | `'UNLINKED'` | | `LINKED` / `UNLINKED` / `AMBIGUOUS` |
| `candidates` | `JSONB` | YES | `NULL` | | Candidate matches for AMBIGUOUS links |
| `created_at` | `TIMESTAMP` | NO | `utcnow` | | |

**Indexes:**
- `ix_document_links_child_doc_id`
- `ix_document_links_parent_doc_id`
- `ix_document_links_link_status`

**Link Status Logic:**

| Status | Meaning | Condition |
|--------|---------|-----------|
| `LINKED` | Exactly one parent match found | 1 candidate with matching type + date + parties |
| `UNLINKED` | No parent match found | 0 candidates |
| `AMBIGUOUS` | Multiple possible parents | 2+ candidates; `candidates` JSONB stores them |

**Linkable Document Types** (only these get linked):
- `Amendment`
- `Addendum`
- `SOW`

**Cascade Behavior:**
- Delete child document -> link row deleted (CASCADE)
- Delete parent document -> `parent_doc_id` set to NULL (SET NULL), link row preserved

---

### 8. flags

**Purpose:** Quality and compliance flags from Stage 7. Polymorphic -- can flag obligations OR documents.

| Column | Type | Nullable | Default | Constraint | Notes |
|--------|------|----------|---------|------------|-------|
| `id` | `UUID` | NO | `uuid.uuid4` | **PK** | |
| `entity_type` | `VARCHAR` | NO | -- | | `"obligation"` or `"document"` |
| `entity_id` | `UUID` | NO | -- | | UUID of the flagged entity |
| `flag_type` | `VARCHAR` | NO | -- | | See types below |
| `message` | `TEXT` | YES | `NULL` | | Human-readable description |
| `resolved` | `BOOLEAN` | NO | `FALSE` | | Has a human addressed this? |
| `created_at` | `TIMESTAMP` | NO | `utcnow` | | |
| `resolved_at` | `TIMESTAMP` | YES | `NULL` | | When was it resolved? |

**Indexes:**
- `ix_flags_entity` (composite on `entity_type` + `entity_id`)
- `ix_flags_flag_type`
- `ix_flags_resolved`

**Flag Types and Severities:**

| Flag Type | Severity | Entity Type | Trigger |
|-----------|----------|-------------|---------|
| `UNVERIFIED` | RED | obligation | Stage 3 verification failed |
| `UNLINKED` | RED | document | Stage 4 found no parent |
| `AMBIGUOUS` | ORANGE | document | Stage 4 found multiple parents |
| `UNRESOLVED` | ORANGE | obligation | Document not linked, can't resolve |
| `LOW_CONFIDENCE` | YELLOW | obligation | Extraction confidence < 0.80 |

---

### 9. dangling_references

**Purpose:** Tracks parent references that couldn't be resolved. Enables backfill when new docs arrive.

| Column | Type | Nullable | Default | Constraint | Notes |
|--------|------|----------|---------|------------|-------|
| `id` | `UUID` | NO | `uuid.uuid4` | **PK** | |
| `doc_id` | `UUID` | NO | -- | **FK** -> `documents.id` (CASCADE) | Document with the unresolved ref |
| `reference_text` | `TEXT` | YES | `NULL` | | e.g., "MSA dated January 10, 2023" |
| `attempted_matches` | `JSONB` | YES | `NULL` | | What was tried and why it failed |
| `created_at` | `TIMESTAMP` | NO | `utcnow` | | |

**Indexes:**
- `ix_dangling_references_doc_id`

**Backfill Logic:** When a new document is ingested, `backfill_dangling_references()` (Stage 4) checks all existing dangling references to see if the new document matches as the missing parent.

---

## All Indexes

| Table | Index Name | Column(s) | Type | Unique |
|-------|-----------|-----------|------|--------|
| documents | `ix_documents_parties` | `parties` | GIN | No |
| documents | `ix_documents_effective_date` | `effective_date` | B-tree | No |
| documents | `ix_documents_org_id` | `org_id` | B-tree | No |
| documents | `ix_documents_status` | `status` | B-tree | No |
| fingerprints | `uq_fingerprints_doc_id` | `doc_id` | B-tree | **Yes** |
| fingerprints | `ix_fingerprints_sha256` | `sha256` | B-tree | No |
| fingerprints | `ix_fingerprints_content_hash` | `content_hash` | B-tree | No |
| fingerprints | `ix_fingerprints_simhash` | `simhash` | B-tree | No |
| fingerprints | `ix_fingerprints_structural_fingerprint` | `structural_fingerprint` | B-tree | No |
| pages | `ix_pages_doc_id_page_number` | `doc_id`, `page_number` | B-tree | **Yes** |
| obligations | `ix_obligations_doc_id` | `doc_id` | B-tree | No |
| obligations | `ix_obligations_status` | `status` | B-tree | No |
| obligations | `ix_obligations_obligation_type` | `obligation_type` | B-tree | No |
| evidence | `ix_evidence_obligation_id` | `obligation_id` | B-tree | No |
| evidence | `ix_evidence_doc_id` | `doc_id` | B-tree | No |
| document_links | `ix_document_links_child_doc_id` | `child_doc_id` | B-tree | No |
| document_links | `ix_document_links_parent_doc_id` | `parent_doc_id` | B-tree | No |
| document_links | `ix_document_links_link_status` | `link_status` | B-tree | No |
| flags | `ix_flags_entity` | `entity_type`, `entity_id` | B-tree | No |
| flags | `ix_flags_flag_type` | `flag_type` | B-tree | No |
| flags | `ix_flags_resolved` | `resolved` | B-tree | No |
| dangling_references | `ix_dangling_references_doc_id` | `doc_id` | B-tree | No |

**Total: 22 indexes across 9 tables**

---

## All Foreign Keys and Cascade Rules

| Child Table | Child Column | Parent Table | Parent Column | ON DELETE |
|-------------|-------------|--------------|---------------|-----------|
| documents | `org_id` | organizations | `id` | **CASCADE** |
| fingerprints | `doc_id` | documents | `id` | **CASCADE** |
| pages | `doc_id` | documents | `id` | **CASCADE** |
| obligations | `doc_id` | documents | `id` | **CASCADE** |
| evidence | `obligation_id` | obligations | `id` | **CASCADE** |
| evidence | `doc_id` | documents | `id` | **CASCADE** |
| document_links | `child_doc_id` | documents | `id` | **CASCADE** |
| document_links | `parent_doc_id` | documents | `id` | **SET NULL** |
| dangling_references | `doc_id` | documents | `id` | **CASCADE** |

**Note:** `document_links.parent_doc_id` uses SET NULL (not CASCADE). When a parent document is deleted, the link row is preserved with `parent_doc_id = NULL` to maintain linking history.

---

## Enum-Like Column Values

All status/type columns use `VARCHAR` (not PostgreSQL `ENUM`) for migration flexibility.

### documents.status

| Value | Set By | Meaning |
|-------|--------|---------|
| `VALID` | Stage 0a | File passes validation |
| `INVALID` | Stage 0a | File is corrupt, empty, or unsupported |
| `NEEDS_PASSWORD` | Stage 0a | PDF is password-protected |

### documents.doc_type

| Value | Category | Description |
|-------|----------|-------------|
| `MSA` | Base contract | Master Service Agreement |
| `SOW` | Child document | Statement of Work |
| `Amendment` | Child document | Modifies existing contract |
| `Addendum` | Child document | Adds new terms to existing contract |
| `NDA` | Standalone | Non-Disclosure Agreement |
| `Order Form` | Child document | Purchase order / order form |
| `Other` | Catch-all | Does not fit any category |
| `UNKNOWN` | Unclassified | Low confidence or not yet classified |

### obligations.status

| Value | Set By | Meaning |
|-------|--------|---------|
| `ACTIVE` | Default / Stage 5 | Currently in force |
| `SUPERSEDED` | Stage 5 | Replaced by amendment (REPLACE action) |
| `TERMINATED` | Stage 5 | Deleted by amendment (DELETE action) |
| `UNRESOLVED` | Stage 5 | Document not linked; cannot determine |

### obligations.obligation_type

| Value | Description |
|-------|-------------|
| `Delivery` | Physical or service delivery requirements |
| `Financial` | Payment, invoicing, late fees |
| `Compliance` | Regulatory or legal compliance |
| `SLA` | Service Level Agreement metrics |
| `Confidentiality` | Information protection duties |
| `Termination` | Contract exit or termination conditions |
| `Indemnification` | Liability and hold-harmless clauses |
| `Governance` | Reporting, meetings, oversight |

### document_links.link_status

| Value | Set By | Meaning |
|-------|--------|---------|
| `LINKED` | Stage 4 | Exactly one parent match found |
| `UNLINKED` | Stage 4 | No parent match found |
| `AMBIGUOUS` | Stage 4 | Multiple possible parents |

### flags.flag_type

| Value | Severity | Entity | Meaning |
|-------|----------|--------|---------|
| `UNVERIFIED` | RED | obligation | Verification pipeline failed |
| `UNLINKED` | RED | document | No parent reference resolved |
| `AMBIGUOUS` | ORANGE | document | Multiple parent candidates |
| `UNRESOLVED` | ORANGE | obligation | Obligation from unlinked doc |
| `LOW_CONFIDENCE` | YELLOW | obligation | Extraction confidence < 0.80 |

### evidence.verification_result

| Value | Meaning |
|-------|---------|
| `CONFIRMED` | All verification checks passed |
| `DISPUTED` | Claude verification disagreed |
| `UNVERIFIED` | Could not verify (e.g., grounding failed) |

---

## JSONB Column Schemas

### documents.parties

```json
["CDW Government LLC", "State of California"]
```

Array of party name strings. Indexed with GIN for `@>` containment queries:

```sql
SELECT * FROM documents WHERE parties @> '["CDW Government LLC"]'::jsonb;
```

### obligations.verification_result

```json
{
  "verified": true,
  "confidence": 0.95,
  "reason": "The source clause exists verbatim in the document and the obligation accurately reflects the contractual requirement."
}
```

Full Claude cross-verification response.

### document_links.candidates

```json
[
  {
    "id": "uuid-1",
    "doc_type": "MSA",
    "effective_date": "2025-01-15",
    "parties": ["Acme Corp", "Globex Inc"]
  },
  {
    "id": "uuid-2",
    "doc_type": "MSA",
    "effective_date": "2025-01-15",
    "parties": ["Acme Corp", "Beta LLC"]
  }
]
```

Stored only when `link_status = 'AMBIGUOUS'`. Array of candidate parent document summaries.

### evidence.amendment_history

```json
[
  {
    "amendment_obligation_text": "Vendor must deliver within 15 business days.",
    "amendment_source_clause": "Section 1.1 is hereby amended...",
    "action": "REPLACE",
    "reasoning": "Delivery timeline changed from 30 calendar days to 15 business days.",
    "confidence": 0.95
  }
]
```

Array of amendment resolution records. Each entry records one comparison from Stage 5.

### dangling_references.attempted_matches

```json
{
  "parsed_type": "MSA",
  "parsed_date": "2023-01-10",
  "parsed_parties": ["Alpha LLC"],
  "candidates_checked": 3,
  "failure_reason": "No candidate matched on both date and parties."
}
```

Records why the linking attempt failed, enabling debugging and backfill.

---

## Raw DDL

Equivalent PostgreSQL CREATE TABLE statements for all 9 tables:

```sql
-- 1. organizations
CREATE TABLE organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR NOT NULL,
    folder_path VARCHAR,
    created_at  TIMESTAMP NOT NULL DEFAULT now(),
    updated_at  TIMESTAMP NOT NULL DEFAULT now()
);

-- 2. documents
CREATE TABLE documents (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    filename                    VARCHAR NOT NULL,
    file_path                   VARCHAR NOT NULL,
    file_size_bytes             BIGINT,
    status                      VARCHAR NOT NULL DEFAULT 'VALID',       -- VALID | INVALID | NEEDS_PASSWORD
    doc_type                    VARCHAR NOT NULL DEFAULT 'UNKNOWN',     -- MSA | SOW | Amendment | ...
    parties                     JSONB,
    effective_date              TIMESTAMP,
    parent_reference_raw        TEXT,
    classification_confidence   FLOAT,
    created_at                  TIMESTAMP NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX ix_documents_parties         ON documents USING GIN (parties);
CREATE INDEX ix_documents_effective_date  ON documents (effective_date);
CREATE INDEX ix_documents_org_id          ON documents (org_id);
CREATE INDEX ix_documents_status          ON documents (status);

-- 3. fingerprints
CREATE TABLE fingerprints (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id                  UUID NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    sha256                  VARCHAR,
    content_hash            VARCHAR,
    simhash                 BIGINT,
    structural_fingerprint  VARCHAR,
    created_at              TIMESTAMP NOT NULL DEFAULT now(),

    CONSTRAINT uq_fingerprints_doc_id UNIQUE (doc_id)
);

CREATE INDEX ix_fingerprints_sha256                  ON fingerprints (sha256);
CREATE INDEX ix_fingerprints_content_hash             ON fingerprints (content_hash);
CREATE INDEX ix_fingerprints_simhash                  ON fingerprints (simhash);
CREATE INDEX ix_fingerprints_structural_fingerprint   ON fingerprints (structural_fingerprint);

-- 4. pages
CREATE TABLE pages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id          UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number     INTEGER NOT NULL,
    text            TEXT,
    tables_markdown TEXT,
    ocr_confidence  FLOAT,
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ix_pages_doc_id_page_number ON pages (doc_id, page_number);

-- 5. obligations
CREATE TABLE obligations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id              UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    obligation_text     TEXT NOT NULL,
    obligation_type     VARCHAR,                                          -- Delivery | Financial | ...
    responsible_party   VARCHAR,
    counterparty        VARCHAR,
    frequency           VARCHAR,
    deadline            VARCHAR,
    source_clause       TEXT,
    source_page         INTEGER,
    confidence          FLOAT,
    status              VARCHAR NOT NULL DEFAULT 'ACTIVE',                -- ACTIVE | SUPERSEDED | ...
    extraction_model    VARCHAR,
    verification_model  VARCHAR,
    verification_result JSONB,
    created_at          TIMESTAMP NOT NULL DEFAULT now(),
    updated_at          TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX ix_obligations_doc_id           ON obligations (doc_id);
CREATE INDEX ix_obligations_status           ON obligations (status);
CREATE INDEX ix_obligations_obligation_type  ON obligations (obligation_type);

-- 6. evidence (APPEND-ONLY -- no updated_at!)
CREATE TABLE evidence (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    obligation_id       UUID NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
    doc_id              UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number         INTEGER,
    section_reference   VARCHAR,
    source_clause       TEXT,
    extraction_model    VARCHAR,
    verification_model  VARCHAR,
    verification_result VARCHAR,                                          -- CONFIRMED | DISPUTED | UNVERIFIED
    confidence          FLOAT,
    amendment_history   JSONB,
    created_at          TIMESTAMP NOT NULL DEFAULT now()
    -- NOTE: No updated_at column. This table is append-only by design.
);

CREATE INDEX ix_evidence_obligation_id ON evidence (obligation_id);
CREATE INDEX ix_evidence_doc_id        ON evidence (doc_id);

-- 7. document_links
CREATE TABLE document_links (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    child_doc_id    UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parent_doc_id   UUID REFERENCES documents(id) ON DELETE SET NULL,
    link_status     VARCHAR NOT NULL DEFAULT 'UNLINKED',                  -- LINKED | UNLINKED | AMBIGUOUS
    candidates      JSONB,
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX ix_document_links_child_doc_id  ON document_links (child_doc_id);
CREATE INDEX ix_document_links_parent_doc_id ON document_links (parent_doc_id);
CREATE INDEX ix_document_links_link_status   ON document_links (link_status);

-- 8. flags
CREATE TABLE flags (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type VARCHAR NOT NULL,                                         -- "obligation" | "document"
    entity_id   UUID NOT NULL,
    flag_type   VARCHAR NOT NULL,                                         -- UNVERIFIED | UNLINKED | ...
    message     TEXT,
    resolved    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMP NOT NULL DEFAULT now(),
    resolved_at TIMESTAMP
);

CREATE INDEX ix_flags_entity    ON flags (entity_type, entity_id);
CREATE INDEX ix_flags_flag_type ON flags (flag_type);
CREATE INDEX ix_flags_resolved  ON flags (resolved);

-- 9. dangling_references
CREATE TABLE dangling_references (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id            UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    reference_text    TEXT,
    attempted_matches JSONB,
    created_at        TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX ix_dangling_references_doc_id ON dangling_references (doc_id);
```

---

## Summary Statistics

| Metric | Count |
|--------|-------|
| Tables | 9 |
| Total columns | 78 |
| Foreign keys | 9 |
| Indexes | 22 |
| JSONB columns | 6 |
| Append-only tables | 1 (evidence) |
| GIN indexes | 1 (documents.parties) |
