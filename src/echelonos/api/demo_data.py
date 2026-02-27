"""Demo data for the Echelonos API.

Provides realistic sample obligation dicts, document dicts, and link dicts
that simulate a vendor contract scenario between Acme Corp (the buyer) and
Nexus Solutions (the vendor) under a Master Services Agreement with two
amendments.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

DEMO_DOCUMENTS: dict[str, dict[str, Any]] = {
    "doc-001": {
        "id": "doc-001",
        "doc_type": "MSA",
        "filename": "Acme_NexusSolutions_MSA_2024.pdf",
        "org_name": "Acme Corp",
        "upload_date": "2024-06-15T10:00:00Z",
        "classification_confidence": 0.97,
    },
    "doc-002": {
        "id": "doc-002",
        "doc_type": "Amendment",
        "filename": "Acme_NexusSolutions_Amendment1_2024.pdf",
        "org_name": "Acme Corp",
        "upload_date": "2024-09-01T14:30:00Z",
        "classification_confidence": 0.94,
    },
    "doc-003": {
        "id": "doc-003",
        "doc_type": "Amendment",
        "filename": "Acme_NexusSolutions_Amendment2_2025.pdf",
        "org_name": "Acme Corp",
        "upload_date": "2025-01-20T09:15:00Z",
        "classification_confidence": 0.91,
    },
}

# ---------------------------------------------------------------------------
# Links (Stage 4 output)
# ---------------------------------------------------------------------------

DEMO_LINKS: list[dict[str, Any]] = [
    {
        "child_doc_id": "doc-002",
        "parent_doc_id": "doc-001",
        "status": "LINKED",
        "confidence": 0.96,
    },
    {
        "child_doc_id": "doc-003",
        "parent_doc_id": "doc-001",
        "status": "LINKED",
        "confidence": 0.89,
    },
]

# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------

DEMO_OBLIGATIONS: list[dict[str, Any]] = [
    # --- ACTIVE obligations ------------------------------------------------
    {
        "id": "obl-001",
        "doc_id": "doc-001",
        "obligation_text": (
            "Vendor shall deliver monthly status reports to Buyer no later "
            "than the 5th business day of each calendar month."
        ),
        "obligation_type": "Delivery",
        "responsible_party": "Nexus Solutions",
        "counterparty": "Acme Corp",
        "source_clause": "Section 4.2 - Reporting Requirements",
        "status": "ACTIVE",
        "frequency": "Monthly",
        "deadline": "5th business day",
        "confidence": 0.95,
        "verification_result": {"verified": True},
    },
    {
        "id": "obl-002",
        "doc_id": "doc-001",
        "obligation_text": (
            "Buyer shall pay Vendor the agreed service fees within 30 days "
            "of receipt of a valid invoice."
        ),
        "obligation_type": "Financial",
        "responsible_party": "Acme Corp",
        "counterparty": "Nexus Solutions",
        "source_clause": "Section 6.1 - Payment Terms",
        "status": "ACTIVE",
        "frequency": "Per invoice",
        "deadline": "Net 30",
        "confidence": 0.98,
        "verification_result": {"verified": True},
    },
    {
        "id": "obl-003",
        "doc_id": "doc-001",
        "obligation_text": (
            "Vendor shall maintain SOC 2 Type II certification and provide "
            "an updated audit report annually."
        ),
        "obligation_type": "Compliance",
        "responsible_party": "Nexus Solutions",
        "counterparty": "Acme Corp",
        "source_clause": "Section 9.3 - Security Compliance",
        "status": "ACTIVE",
        "frequency": "Annually",
        "deadline": None,
        "confidence": 0.93,
        "verification_result": {"verified": True},
    },
    {
        "id": "obl-004",
        "doc_id": "doc-001",
        "obligation_text": (
            "Vendor guarantees 99.9% uptime for the hosted platform, measured "
            "on a monthly basis, excluding scheduled maintenance windows."
        ),
        "obligation_type": "SLA",
        "responsible_party": "Nexus Solutions",
        "counterparty": "Acme Corp",
        "source_clause": "Section 7.1 - Service Level Agreement",
        "status": "ACTIVE",
        "frequency": "Monthly",
        "deadline": None,
        "confidence": 0.97,
        "verification_result": {"verified": True},
    },
    {
        "id": "obl-005",
        "doc_id": "doc-001",
        "obligation_text": (
            "Both parties shall maintain the confidentiality of all proprietary "
            "information exchanged under this Agreement for a period of five (5) "
            "years following termination."
        ),
        "obligation_type": "Confidentiality",
        "responsible_party": "Both Parties",
        "counterparty": "Both Parties",
        "source_clause": "Section 11.1 - Confidentiality",
        "status": "ACTIVE",
        "frequency": None,
        "deadline": "5 years post-termination",
        "confidence": 0.96,
        "verification_result": {"verified": True},
    },
    # --- SUPERSEDED (replaced by amendment) --------------------------------
    {
        "id": "obl-006",
        "doc_id": "doc-002",
        "obligation_text": (
            "Vendor shall deliver quarterly business review presentations "
            "to Buyer's executive team."
        ),
        "obligation_type": "Delivery",
        "responsible_party": "Nexus Solutions",
        "counterparty": "Acme Corp",
        "source_clause": "Section 4.5 - Business Reviews",
        "status": "SUPERSEDED",
        "frequency": "Quarterly",
        "deadline": None,
        "confidence": 0.88,
        "verification_result": {"verified": True},
    },
    # --- UNRESOLVED (verification failed, from amendment with issues) ------
    {
        "id": "obl-007",
        "doc_id": "doc-003",
        "obligation_text": (
            "Vendor shall provide on-site support staff at Buyer's headquarters "
            "during the migration period, not to exceed 120 calendar days."
        ),
        "obligation_type": "Delivery",
        "responsible_party": "Nexus Solutions",
        "counterparty": "Acme Corp",
        "source_clause": "Section 5.8 - Migration Support",
        "status": "UNRESOLVED",
        "frequency": None,
        "deadline": "120 calendar days",
        "confidence": 0.72,
        "verification_result": {"verified": False, "reason": "Source clause reference not grounded in document text"},
    },
    # --- ACTIVE with low confidence ---------------------------------------
    {
        "id": "obl-008",
        "doc_id": "doc-003",
        "obligation_text": (
            "Buyer shall reimburse Vendor for pre-approved travel expenses "
            "incurred in connection with on-site support activities."
        ),
        "obligation_type": "Financial",
        "responsible_party": "Acme Corp",
        "counterparty": "Nexus Solutions",
        "source_clause": "Section 6.4 - Expense Reimbursement",
        "status": "ACTIVE",
        "frequency": "As incurred",
        "deadline": "Net 45",
        "confidence": 0.65,
        "verification_result": {"verified": False, "reason": "Clause boundaries unclear in OCR output"},
    },
    # --- TERMINATED --------------------------------------------------------
    {
        "id": "obl-009",
        "doc_id": "doc-001",
        "obligation_text": (
            "Vendor shall provide a dedicated account manager for the first "
            "12 months of the engagement."
        ),
        "obligation_type": "Delivery",
        "responsible_party": "Nexus Solutions",
        "counterparty": "Acme Corp",
        "source_clause": "Section 3.2 - Account Management",
        "status": "TERMINATED",
        "frequency": None,
        "deadline": "12 months",
        "confidence": 0.91,
        "verification_result": {"verified": True},
    },
    # --- ACTIVE SLA with tight threshold -----------------------------------
    {
        "id": "obl-010",
        "doc_id": "doc-002",
        "obligation_text": (
            "Vendor shall respond to Severity 1 incidents within 15 minutes "
            "and provide a root cause analysis within 48 hours of resolution."
        ),
        "obligation_type": "SLA",
        "responsible_party": "Nexus Solutions",
        "counterparty": "Acme Corp",
        "source_clause": "Section 7.3 - Incident Response",
        "status": "ACTIVE",
        "frequency": "Per incident",
        "deadline": "15 min response / 48 hr RCA",
        "confidence": 0.94,
        "verification_result": {"verified": True},
    },
]
