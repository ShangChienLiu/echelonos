"""Pipeline stages for contract obligation extraction."""

from echelonos.stages.stage_0a_validation import (
    convert_to_pdf,
    validate_file,
    validate_folder,
)
from echelonos.stages.stage_2_classification import (
    ClassificationResult,
    classify_document,
    classify_with_cross_check,
)
from echelonos.stages.stage_3_extraction import (
    ExtractionResult,
    Obligation,
    extract_and_verify,
    extract_obligations,
    extract_party_roles,
    run_cove,
    verify_grounding,
    verify_with_claude,
)
from echelonos.stages.stage_5_amendment import (
    ResolutionResult,
    build_amendment_chain,
    compare_clauses,
    resolve_all,
    resolve_amendment_chain,
    resolve_obligation,
)
from echelonos.stages.stage_6_evidence import (
    EvidenceRecord,
    VerificationResult,
    create_evidence_record,
    create_status_change_record,
    package_evidence,
    validate_evidence_chain,
)
from echelonos.stages.stage_7_report import (
    FlagItem,
    ObligationReport,
    ObligationRow,
    build_flag_report,
    build_obligation_matrix,
    build_summary,
    export_to_json,
    export_to_markdown,
    generate_report,
)
