export interface AmendmentHistoryEntry {
  amendment_obligation_text: string;
  amendment_source_clause: string;
  action: 'REPLACE' | 'MODIFY' | 'DELETE' | 'UNCHANGED';
  reasoning: string;
  confidence: number;
  doc_id?: string;
  doc_filename?: string;
  amendment_number?: number;
}

export interface ObligationRow {
  number: number;
  obligation_text: string;
  obligation_type: string;
  responsible_party: string;
  counterparty: string;
  source: string;
  status: 'ACTIVE' | 'SUPERSEDED' | 'TERMINATED' | 'UNRESOLVED';
  frequency: string | null;
  deadline: string | null;
  confidence: number;
  source_clause?: string;
  source_page?: number;
  doc_filename?: string;
  amendment_history?: AmendmentHistoryEntry[];
}

export interface FlagItem {
  flag_type: string;
  severity: 'RED' | 'ORANGE' | 'YELLOW' | 'WHITE';
  entity_type: string;
  entity_id: string;
  message: string;
}

export interface SummaryData {
  by_type: Record<string, number>;
  by_status: Record<string, number>;
  by_responsible_party: Record<string, number>;
}

export interface ObligationReport {
  org_name: string;
  generated_at: string;
  total_obligations: number;
  active_obligations: number;
  superseded_obligations: number;
  unresolved_obligations: number;
  obligations: ObligationRow[];
  flags: FlagItem[];
  summary: SummaryData;
}

export interface PipelineStatus {
  state: 'idle' | 'processing' | 'done' | 'error' | 'cancelled';
  org_name: string | null;
  current_stage: string | null;
  current_stage_label: string | null;
  total_docs: number;
  processed_docs: number;
  stages_completed: string[];
  elapsed_seconds: number | null;
  error: string | null;
}
