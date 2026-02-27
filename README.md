# Echelon OS

Automated contract obligation extraction pipeline with LLM-powered analysis and evidence trails.

Echelon OS ingests legal documents (PDFs, DOCX, emails, images), extracts contractual obligations using dual-ensemble AI verification, resolves amendment chains, and produces auditable obligation reports with full evidence packaging.

## Architecture

```
Documents ──► Stage 0a/0b ──► Stage 1 ──► Stage 2 ──► Stage 3 ──► Stage 4 ──► Stage 5 ──► Stage 6 ──► Stage 7
               Validate &       OCR      Classify     Extract      Link       Resolve     Package     Generate
               Dedup                                  Obligations  Documents  Amendments   Evidence    Report
```

### 8-Stage Pipeline

| Stage | Name | Description |
|-------|------|-------------|
| **0a** | File Validation | Validates MIME types, handles containers (MSG, EML, ZIP), converts to PDF. Rejects unsupported formats. |
| **0b** | Deduplication | 4-layer hash pipeline: SHA-256, content hash, MinHash+LSH near-duplicates, blocking keys to protect amendments. |
| **1** | OCR / Ingestion | Mistral OCR extracts per-page text with table structures as markdown. Includes confidence quality gates. |
| **2** | Classification | Claude classifies documents (MSA, SOW, Amendment, Addendum, NDA, Order Form). Extracts parties, dates, parent references. |
| **3** | Obligation Extraction | Dual independent Claude extractions, programmatic matching, grounding checks, and Chain-of-Verification (CoVe). |
| **4** | Document Linking | Links amendments/addendums/SOWs to parent contracts by parsing references and matching against corpus. |
| **5** | Amendment Resolution | Walks chronological amendment chains to determine obligation status: ACTIVE, SUPERSEDED, or TERMINATED. |
| **6** | Evidence Packaging | Creates immutable audit trail records tracing obligations to source clauses, models, and amendment history. |
| **7** | Report Generation | Builds obligation matrix, flag report (RED/ORANGE/YELLOW/WHITE severity), and summary statistics. |

## Tech Stack

**Backend:** Python 3.11+, FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Alembic
**AI/ML:** Anthropic Claude (extraction & verification), Mistral OCR (document ingestion)
**Frontend:** React 19, TypeScript, Vite, Tailwind CSS 4
**Infrastructure:** Docker Compose, Prefect 3.0 (orchestration)

## Project Structure

```
echelonos/
├── src/echelonos/
│   ├── api/              # FastAPI app, endpoints, demo data
│   ├── db/               # SQLAlchemy models, session, persistence
│   ├── flows/            # Prefect pipeline orchestration
│   ├── llm/              # Claude client wrapper
│   ├── ocr/              # Mistral OCR client
│   ├── stages/           # 8-stage pipeline (stage_0a through stage_7)
│   └── config.py         # Pydantic settings
├── frontend/             # React/TypeScript UI
│   ├── src/components/   # StatsCards, ObligationTable, EvidenceDrawer, etc.
│   └── vite.config.ts
├── tests/
│   ├── e2e/              # End-to-end tests
│   └── unit/             # Unit tests
├── alembic/              # Database migrations
├── docker-compose.yml    # PostgreSQL + Prefect services
└── pyproject.toml
```

## Setup

### Prerequisites

- Python 3.11+
- Node.js 18+
- Docker & Docker Compose

### 1. Environment Variables

```bash
cp .env.example .env
```

Edit `.env` and set your API keys:

```
ANTHROPIC_API_KEY=sk-ant-...
MISTRAL_API_KEY=...
```

### 2. Start Services

```bash
docker-compose up -d
```

This starts PostgreSQL (port 5432) and Prefect Server (port 4200).

### 3. Backend

```bash
pip install -e ".[dev]"
alembic upgrade head
```

### 4. Frontend

```bash
cd frontend
npm install
```

## Running

### API Server

```bash
uvicorn echelonos.api.app:app --reload
# Runs on http://localhost:8000
```

### Frontend Dev Server

```bash
cd frontend
npm run dev
# Runs on http://localhost:5173 (proxies /api to :8000)
```

### Production Build

```bash
cd frontend
npm run build
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/organizations` | List all organizations |
| `GET` | `/api/report/{org_name}` | Full obligation report |
| `GET` | `/api/report/{org_name}/obligations` | Obligation matrix |
| `GET` | `/api/report/{org_name}/flags` | Flag report |
| `GET` | `/api/report/{org_name}/summary` | Summary statistics |
| `POST` | `/api/upload` | Upload documents (runs stages 0a-0b) |
| `POST` | `/api/pipeline/run` | Run pipeline stages 1-7 |
| `GET` | `/api/pipeline/status` | Pipeline processing status |
| `POST` | `/api/pipeline/stop` | Cancel running pipeline |
| `DELETE` | `/api/database` | Clear all data |

Swagger UI available at `http://localhost:8000/docs`.

## Testing

```bash
# All tests
pytest

# End-to-end tests only
pytest -m e2e

# Unit tests only
pytest -m unit

# With coverage
pytest --cov=src/echelonos
```

## Database

PostgreSQL 16 with the following tables:

- `organizations` - Organization records
- `documents` - Document metadata, classification, parties
- `pages` - Per-page OCR text and confidence
- `obligations` - Extracted obligations with type, party, status
- `fingerprints` - Deduplication hash records
- `document_links` - Parent-child document relationships
- `evidence` - Immutable audit trail records
- `flags` - QA flags (unverified, unlinked, ambiguous)
- `dangling_references` - Unresolved parent references

Run migrations:

```bash
alembic upgrade head
```
