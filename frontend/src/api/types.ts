/** Types mirroring the backend Pydantic schemas. Kept deliberately close to
 *  the API so the mapping is obvious when reading either side. */

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export type RelevanceBand =
  | "highly_relevant"
  | "relevant"
  | "low_relevance"
  | "out_of_scope"
  | "unscored";

export interface TenderSummary {
  id: string;
  title: string;
  reference: string | null;
  buyer: string | null;
  country: string | null;
  sector: string | null;
  source_key: string;
  source_url: string | null;
  status: string;
  procurement_type: string;
  publication_date: string | null;
  deadline: string | null;
  estimated_budget: string | null;
  currency: string | null;
  relevance_score: number | null;
  relevance_band: RelevanceBand;
  pipeline_state: string;
  duplicate_hits: number;
  seen_on_sources: string[];
  created_at: string;
  days_until_deadline: number | null;
  is_urgent: boolean;
}

export interface ScoreBreakdownEntry {
  value: number | null;
  weight: number;
  weighted: number;
  explanation: string;
  details?: Record<string, unknown>;
  applicable?: boolean;
  error?: string;
}
export interface ScoreBreakdown {
  profile_version: string;
  score: number;
  band: string;
  breakdown: Record<string, ScoreBreakdownEntry>;
  weights: Record<string, number>;
  computed_at: string | null;
}

export interface TenderDocument {
  id: string;
  name: string | null;
  source_url: string | null;
  content_type: string | null;
  size_bytes: number | null;
  status: string;
  downloaded_at: string | null;
}

export interface TenderDetail extends TenderSummary {
  description: string | null;
  funding_organization: string | null;
  contact_email: string | null;
  location: string | null;
  category: string | null;
  cpv_codes: string[];
  language: string | null;
  external_id: string | null;
  canonical_url: string | null;
  entry_point: string;
  original_filename: string | null;
  content_type: string | null;
  size_bytes: number | null;
  storage_key: string | null;
  pipeline_error: string | null;
  scored_at: string | null;
  documents: TenderDocument[];
  latest_score: ScoreBreakdown | null;
  extra: Record<string, unknown>;
}

export interface ConnectorRun {
  id: string;
  connector_key: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  pages_fetched: number;
  http_requests: number;
  http_retries: number;
  items_found: number;
  items_ingested: number;
  items_duplicate: number;
  items_rejected: number;
  items_failed: number;
  error_type: string | null;
  error_message: string | null;
  extra: RunExtra;
}

/** Diagnostic counters the worker writes into `run.extra`. They explain a
 *  zero-result run: without `records_parsed` there is no way to tell working
 *  selectors plus strict filters from selectors that have silently broken. */
export interface RunExtra {
  records_parsed?: number;
  items_filtered_out?: number;
  items_duplicate_in_run?: number;
  /** Why the crawl ended: source_exhausted | results_satisfied | page_cap | deadline */
  stop_reason?: string | null;
  skip_reason?: string | null;
  filter_application?: {
    client_side?: string[];
    server_side?: string[];
    unsupported?: string[];
  };
  [key: string]: unknown;
}

export interface ScrapeJob {
  id: string;
  trigger: string;
  status: string;
  requested_connectors: string[];
  filters: Record<string, unknown>;
  requested_by: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  connectors_total: number;
  connectors_succeeded: number;
  connectors_failed: number;
  connectors_skipped: number;
  items_found: number;
  items_ingested: number;
  items_duplicate: number;
  items_rejected: number;
  errors: { connector: string; error_type: string; message: string }[];
  runs: ConnectorRun[];
  created_at: string;
  progress: number;
  is_terminal: boolean;
}

export interface Source {
  key: string;
  name: string;
  base_url: string | null;
  country: string | null;
  strategy: string | null;
  enabled: boolean;
  requires_credentials: boolean;
  health: string;
  health_reason: string | null;
  circuit_state: string;
  consecutive_failures: number;
  consecutive_empty_runs: number;
  last_run_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error_type: string | null;
  last_duration_seconds: number | null;
  last_item_count: number;
  total_runs: number;
  total_failures: number;
  total_items_found: number;
  total_items_ingested: number;
  total_duplicates: number;
  success_rate: number | null;
  duplicate_ratio: number | null;
}

export interface ConnectorInfo {
  key: string;
  name: string;
  enabled: boolean;
  available: boolean;
  strategy: string;
  base_url: string;
  country: string | null;
  requires_credentials: boolean;
  has_credentials: boolean;
  health: string;
  unavailable_reason: string | null;
  missing_credentials: string[];
  config_checksum: string;
}

export interface Schedule {
  id: string;
  name: string;
  description: string | null;
  enabled: boolean;
  kind: string;
  interval_seconds: number | null;
  cron_minute: string | null;
  cron_hour: string | null;
  cron_day_of_week: string | null;
  timezone: string;
  connectors: string[];
  filters: Record<string, unknown>;
  queue: string | null;
  skip_if_running: boolean;
  one_off: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  total_run_count: number;
  created_at: string;
  updated_at: string;
  cadence: string;
}

/** A candidate CV, imported and stored — matching against tenders is a later
 *  phase, not implemented yet. */
export interface Cv {
  id: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  source: "upload" | "link";
  source_url: string | null;
  uploaded_by: string | null;
  created_at: string;
}

export interface AcceptedResponse {
  accepted: boolean;
  message: string;
  job_id: string | null;
  tender_id: string | null;
  task_id: string | null;
  poll_url: string | null;
}

export interface DashboardStats {
  total_tenders: number;
  /** Deadline not yet passed — the actionable figure. */
  open_tenders?: number;
  /** Kept for dedup and win/loss history; never deleted on expiry. */
  archived_tenders?: number;
  ingested_last_24h: number;
  relevant_closing_within_7_days: number;
  by_band: Record<string, number>;
  by_source: Record<string, number>;
  band_metadata: Record<string, { label: string; color: string; min_score: number }>;
}

export interface Notification {
  id: string;
  tender_id: string | null;
  channel: string;
  status: string;
  subject: string | null;
  body: string | null;
  payload: Record<string, unknown>;
  match_reason: Record<string, unknown>;
  created_at: string;
  sent_at: string | null;
  read_at: string | null;
}

export interface Preference {
  id?: string;
  user_id?: string;
  email: string | null;
  display_name: string | null;
  team: string | null;
  company: string | null;
  active: boolean;
  sectors: string[];
  industries: string[];
  countries: string[];
  keywords: string[];
  excluded_keywords: string[];
  connectors: string[];
  buyers: string[];
  cpv_codes: string[];
  channels: string[];
  min_relevance_band: string;
  min_score: number | null;
  min_budget: number | null;
  digest_frequency: string;
  max_notifications_per_day: number;
}

export interface ScoringProfile {
  name: string;
  version: string;
  weights: Record<string, number>;
  bands: Record<string, { label: string; color: string; min_score: number }>;
  criteria: string[];
}

export interface Health {
  status: string;
  version: string;
  environment: string;
  uptime_seconds: number;
  degraded_components: string[];
  checks: Record<string, { ok: boolean; latency_ms: number; error?: string | null }>;
}

/** One of Inetum's service domains, served from the scoring profile. */
export interface BusinessDomain {
  name: string;
  weight: number;
  /** Inetum's own vocabulary (SAGE X3, S/4HANA). Scores tenders; never searched. */
  expertise: string[];
  /** A public buyer's vocabulary ("progiciel de gestion"). This is what portals receive. */
  search_terms: string[];
}
