// Mirrors the backend models in src/regcompass/audit.py (the U0 API contract).

export type ReviewStatus = 'accepted' | 'rejected' | 'flagged' | 'corrected'

/** Which rows the Review queue shows. The counts above it never change. */
export type QueueFilter = 'all' | 'unreviewed' | 'corrected'

/** One Indicator a reviewer may correct a Mapping to. */
export interface IndicatorChoice {
  id: string
  title: string
  pillar: number
}

/** One decision as it was written, kept after a later one replaced it. */
export interface ReviewHistoryEntry {
  history_id: number
  run_id: string
  mapping_id: string
  review_status: ReviewStatus
  corrected_indicator_id: string | null
  reviewer: string | null
  decided_at: string
  comment: string | null
}

export interface DocumentSummary {
  document_id: string
  title: string
  // An Economy code from the Portal registry, not a closed union: the list is
  // configuration and the interface reads it from the API.
  economy: string
  n_pages: number
  ocr_applied: boolean
  // How the Document entered the Corpus: 'discovery' (a Portal fetch) or
  // 'manual' (a reviewer added it by upload or by Source URL).
  source_kind: string
  n_records: number
  n_accepted: number
  n_rejected: number
  n_flagged: number
  n_corrected: number
}

/** Where "Open source" takes a reviewer for one Mapping row. kind is
 *  'official' for the Document's own Portal address and 'local' for the copy
 *  this app is serving, which is all a Document with no http(s) Source URL can
 *  offer; the label says which. page is the PDF page the link opens at. */
export interface SourceLink {
  href: string
  kind: 'official' | 'local'
  page: number | null
}

/** One mechanical signal behind a Confidence: its raw input, weight, 0-1
 *  score and contribution (weight x score). */
export interface ConfidencePart {
  signal: 'similarity' | 'quote_length' | 'specificity' | 'attempts'
  label: string
  raw: number
  weight: number
  score: number
  contribution: number
}

export interface RecordSummary {
  mapping_id: string
  indicator_id: string
  indicator_name: string
  section: string
  subsection: string | null
  page_number: number | null
  quote_preview: string
  confidence: number | null
  /** The four signals the Confidence was computed from; null when the stored
   *  record already carried its number. */
  confidence_parts?: ConfidencePart[] | null
  controlling_evidence: boolean
  review_status: ReviewStatus | null
  // The reviewer's note on this Mapping, null when there is none.
  review_note: string | null
  // Where this row came from, as the Evidence Export states it, plus the link
  // the "Open source" control follows.
  source_url: string | null
  location_reference: string
  source_link: SourceLink | null
  /** 'html' when the Document has no pages, so page_number is not a page. */
  format: 'pdf' | 'html'
  /** A reviewer's correction: the Indicator this Mapping belongs under in
   *  their judgement. indicator_id stays the Engine's own. */
  corrected_indicator_id: string | null
  corrected_indicator_title: string | null
}

/** One row of the Review queue: a record row plus the Document it came from,
 *  because the queue spans every Document of the Run. */
export interface QueueRecord extends RecordSummary {
  document_id: string
  document_title: string
}

/** The Run's records as one list, lowest Confidence first, with the counts the
 *  screen states above it. The three counts describe the whole Run and not the
 *  filtered view, so the line reads the same with the filter on and off. */
export interface ReviewQueue {
  run_id: string | null
  threshold: number
  total: number
  below_threshold: number
  unreviewed: number
  records: QueueRecord[]
  corrected: number
}

export interface HighlightRect {
  page: number
  x0: number
  y0: number
  x1: number
  y1: number
}

/** A Review Decision: one accept, reject or flag on one Mapping of one Run,
 *  with an optional note. A later decision on the same Mapping replaces it. */
export interface Review {
  review_id: string
  run_id: string
  mapping_id: string
  review_status: ReviewStatus
  reviewer: string | null
  reviewed_at: string
  comment: string | null
  corrected_indicator_id: string | null
}

export interface MappingRecord {
  mapping_id: string
  document_id: string
  chunk_id: string
  economy: string
  indicator_id: string
  indicator_name: string
  section: string
  subsection: string | null
  verbatim_quote: string
  page_number: number | null
  impact: string | null
  confidence: number | null
  discovery_tag: string | null
  measure_type: string | null
  controlling_evidence: boolean
  relationship_to_group: string | null
  extraction_attempts: number
}

/** An English Gloss of one Verbatim Quote of one Run: the Engine's rendering
 *  beside the original, never in place of it. It carries `label` until a named
 *  person approves the text, which is what makes it a Reviewed Gloss. */
export interface Gloss {
  run_id: string
  mapping_id: string
  english: string | null
  label: string
  reviewed: boolean
  reviewed_by: string | null
  reviewed_at: string | null
  source_language: string
  engine: string
  uncertainty_flag: string | null
  drafted_at: string
}

/** What a Document's stored file is, as far as the audit view's left pane is
 *  concerned: only a PDF is drawn as pages. */
export type SourceFormat = 'pdf' | 'html' | 'other' | 'missing'

export interface RecordDetail {
  record: MappingRecord
  /** The rationale in the RDTII's own words ("RDTII criterion 2 (score 0.5)"
   *  where the Engine wrote "rung 2"); record.impact stays as written. */
  impact_display: string | null
  section_label: string | null
  highlights: HighlightRect[]
  highlight_available: boolean
  quote_char_start: number | null
  quote_char_end: number | null
  review: Review | null
  gloss: Gloss | null
  source_format: SourceFormat
  /** The Piece's text, sent only when the source is not a PDF. */
  source_text: string | null
  corrected_indicator_id: string | null
  corrected_indicator_title: string | null
  /** The Indicators Correct may choose: the Run's Pillars, less this one's. */
  correction_choices: IndicatorChoice[]
}

/** One file an Evidence Export produced. `name` is a bare download name, never
 *  a path: the output directory is not somewhere a reviewer can reach. */
export interface ExportFile {
  name: string
  label: string
  primary: boolean
}

export interface ExportSummary {
  n_records_total: number
  n_accepted: number
  n_rejected: number
  n_flagged: number
  n_corrected?: number
  n_unreviewed: number
  n_rows: number
  csv_path: string
  /** The organizers' filled workbook, and the rows their 101-row entry area
   *  could not hold. */
  xlsx_path: string | null
  rows_cut: number
  supplementary_path: string
  /** Every file the Export wrote, workbook first, as download links. */
  files: ExportFile[]
}

/** What the Evidence Export would contain right now, counted off the database
 *  without writing a file. gated is false on the frozen bundle lane, which
 *  carries no Review Decisions at all. */
export interface ExportPreview {
  run_id: string | null
  gated: boolean
  n_verified: number
  n_accepted: number
  n_rejected: number
  n_flagged: number
  n_corrected?: number
  n_unreviewed: number
  accepted_mapping_ids: string[]
}

export interface AcceptAllResult {
  run_id: string
  n_newly_accepted: number
  n_verified: number
  n_accepted: number
  n_rejected: number
  n_flagged: number
  n_unreviewed: number
}

// ---------------------------------------------------------------------------
// Run panel, Runs list and Settings (the registries the server publishes)
// ---------------------------------------------------------------------------

export interface ServerStatus {
  active: boolean
  status: 'idle' | 'running' | 'done' | 'error'
  error: string | null
  // Economy codes and Pillar numbers are CONFIGURATION: the interface builds
  // its selects from what the server sends and carries no list of its own.
  economies: string[]
  economy_names: Record<string, string>
  // The organizer Languages to expect per Economy; the first is its default.
  economy_languages: Record<string, string[]>
  // Economies whose Portal's own rules do not permit automated collection:
  // Discovery never runs for them and "Add document" is upload only.
  manual_only: string[]
  // Economies with no Discovery plan wired yet. A different statement: they
  // keep the add-by-URL lane, and they may gain a strategy later.
  no_discovery: string[]
  pillars: number[]
  default_pillars: number[]
  // The Engine the registry declares as its default. The Run panel opens on
  // it, so one click on Run never spends on an Engine nobody chose.
  default_engine: string | null
  bundle_mode: boolean
  // Whether a login guards this server (never the login itself).
  login?: boolean
  economy?: string
  mode?: string
  run_id?: string | null
}

export interface EngineInfo {
  name: string
  display_name: string
  open_weights: boolean
  usd_per_million_input_tokens: number
  usd_per_million_output_tokens: number
  api_key_env: string | null
  key_set: boolean
  default: boolean
}

/** One Portal's politeness limits, read-only, as the crawler applies them. */
export interface PortalPoliteness {
  economy: string
  name: string
  host: string
  hosts: string[]
  min_interval_seconds: number
  connections_per_host: number
  robots_respected: boolean
  robots_unavailable_setting: string
  robots_unavailable_policy: string
}

export interface Politeness {
  connections_per_host: number
  robots_respected: boolean
  crawl_delay_rule: string
  robots_unreachable_rule: string
  portals: PortalPoliteness[]
}

export interface IndicatorInfo {
  id: string
  name: string
  pillar: number
}

export interface RunRecord {
  run_id: string
  kind: 'run' | 'discovery'
  economy: string
  pillars: number[]
  indicators: string[] | null
  engine: string | null
  // interrupted: the process that opened this Run died before closing it, and
  // the server's next start said so. Never a Run that is still going.
  status: 'running' | 'completed' | 'failed' | 'interrupted'
  started_at: string
  ended_at: string | null
  documents_fetched: number
  prompt_tokens: number
  completion_tokens: number
  cost_usd: number
  provider_cost_usd: number | null
  error: string | null
  // The lane's own counters. A Discovery record written by "Add document"
  // carries manual: true, which is how the Runs list tells the two apart.
  details: Record<string, unknown>
  // Where the Run's events were recorded (run_events/<run_id>.jsonl, beside
  // the working database), or null for a Run recorded before events existed.
  events_file?: string | null
}

/** One Document in an Economy's Corpus: what a Run would read, whether or not
 *  anything has mapped it yet. `source_url` is null for a file uploaded
 *  without one; its rows then link out as a local copy. */
export interface CorpusDocument {
  document_id: string
  title: string
  source_kind: string
  language: string | null
  source_url: string | null
  n_pages: number | null
  ocr_applied: boolean
  added_at: string | null
  // Whether the stored file is on disk, so the row can offer "our copy".
  has_copy?: boolean
  // Who last corrected the title or Source URL (a name is optional), and when.
  edited_by?: string | null
  edited_at?: string | null
}

/** One Economy's Corpus as it is now: how many Documents, the languages they
 *  are (most common first), and how many have no language recorded. */
export interface CorpusSummary {
  n: number
  languages: string[]
  unrecorded: number
}

export interface CorpusListing {
  economy: string
  n: number
  documents: CorpusDocument[]
}

/** Which Run the audit screens are showing. `record` is null on the frozen
 *  bundle lane and on a database that holds no completed Run yet. */
/** A Document's metadata after a correction, and what it was before. */
export interface DocumentEdit {
  document_id: string
  title: string | null
  source_url: string | null
  previous_title: string | null
  previous_source_url: string | null
  edited_by: string | null
  edited_at: string | null
}

/** One finished Run the Evidence screen can open: the newest for its
 *  Economy, Engine and set of Pillars. */
export interface EvidenceRunChoice {
  run_id: string
  engine: string | null
  pillars: number[]
  started_at: string | null
}

/** An Economy with at least one finished Run, as the Evidence screen's
 *  pickers list it. Newest Run first. */
export interface EvidenceEconomy {
  economy: string
  name: string
  newest_run_id: string
  runs: EvidenceRunChoice[]
}

export interface AuditRun {
  run_id: string | null
  record: RunRecord | null
  bundle_mode: boolean
}

/** What one "Add document" produced, as the server reports it back. */
export interface AddedDocument {
  status: string
  run_id: string
  document_id: string
  title: string
  economy: string
  language: string
  source_url: string
  source_kind: string
  n_pages: number
  n_low_yield_pages: number
  ocr_applied: boolean
  corpus_documents: number
  /** Set when a PDF read to almost no text and was kept anyway. */
  warning?: string
}

export interface RunStarted {
  status: string
  economy: string
  pillars: number[]
  indicators: string[] | null
  engine: string
  mode: string
  corpus_empty: boolean
  corpus_documents: number
}

// ---------------------------------------------------------------------------
// Comparison: two Runs on one Economy and Pillar, one per Engine
// (mirrors src/regcompass/compare.py)
// ---------------------------------------------------------------------------

export type Agreement = 'agree' | 'disagree' | 'only_a' | 'only_b' | 'neither'

export interface ComparisonSide {
  mapping_id: string
  document_id: string
  document_title: string
  section: string
  subsection: string | null
  page_number: number | null
  verbatim_quote: string
  confidence: number | null
  controlling_evidence: boolean
  /** Filled once a Review Decision exists for that Mapping. */
  review_status: string | null
  /** Where this side's Mapping came from: the same link an audit row follows. */
  source_url: string | null
  location_reference: string
  source_link: SourceLink | null
  /** 'html' when the Document has no pages, so page_number is not a page. */
  format: 'pdf' | 'html'
}

export interface ComparisonRow {
  indicator_id: string
  indicator_name: string
  agreement: Agreement
  a: ComparisonSide | null
  b: ComparisonSide | null
}

export interface ComparisonRun {
  record: RunRecord
  engine_display_name: string | null
  duration_s: number | null
}

export interface Comparison {
  economy: string
  pillars: number[]
  match_basis: string
  run_a: ComparisonRun
  run_b: ComparisonRun
  n_indicators: number
  n_agree: number
  n_disagree: number
  n_only_a: number
  n_only_b: number
  n_neither: number
  /** Set when the pairing reached outside the two declared Engines. */
  note: string | null
  rows: ComparisonRow[]
  /** The organizers' Engine Comparison sheet, block 2: every provision either
   *  Engine cited. Only the counts are shown here; the rows travel in the
   *  sheet download. */
  n_provisions: number
  n_found_by_a_only: number
  n_found_by_b_only: number
  n_found_by_both: number
}

/** How a Comparison is asked for: two Run ids, or an Economy and a Pillar the
 * server pairs itself. */
export type ComparisonQuery =
  | { run_a: string; run_b: string }
  | { economy: string; pillar: number }

/** What a clear removed, or what a preview says it would remove. One shape for
 * both, so the confirmation and the receipt are the same counts. */
/** One page of Run Records and how many there are in all. */
export interface RunsPage {
  runs: RunRecord[]
  total: number
  offset: number
}

/** What removing one Document takes (or took), in counts and one sentence. */
export interface DocumentRemoval {
  document_id: string
  economy: string
  title: string
  mappings: number
  runs: number
  reviews: number
  stored_files: number
  corpus_documents: number
  summary: string
}

export interface ClearReport {
  economy: string | null
  documents: number
  stored_files: number
  bytes: number
  extractions: number
  runs: number
  mappings: number
  reviews: number
  /** Stored paths that resolve outside the data root: left alone, never
   *  deleted, and counted so the operator can see they were skipped. */
  refused_files: number
}

/** The 409 a Run gets when its Economy has no Corpus yet. */
export interface CorpusEmpty {
  corpus_empty: true
  economy: string
  message: string
}

/** GET /api/stats: the Run panel's live funnel, stage trace and spend. */
export interface StageTime {
  stage: string
  ms: number
  first_ts: string | null
}

export interface EnginePricing {
  engine: string
  display_name: string
  model: string
  open_weights: boolean
  usd_per_million_input_tokens: number
  usd_per_million_output_tokens: number
  // The shared embedder runs on this machine and is never billed.
  embedder: string | null
}

export interface UsageMeter {
  prompt_tokens: number
  completion_tokens: number
  calls: number
  cost_usd: number
  provider_cost_usd: number | null
}

export interface RunStats {
  active: boolean
  status: 'idle' | 'running' | 'done' | 'error'
  elapsed_s: number
  run: {
    economy: string | null
    engine: string | null
    mode: string | null
    n_chunks: number
    n_pairs_considered: number
    n_pairs_gated: number
    n_passed: number
    n_no_evidence: number
    n_dropped: number
    n_groups: number
    pairs_done: number
    pairs_total: number
    n_documents: number
  }
  rows_exported: number | null
  run_id: string | null
  stages: StageTime[]
  // session: this server's current or last job; last_run: the newest
  // completed Run Record, traced over its own window after a restart.
  stages_scope: 'session' | 'last_run' | null
  models: { engine: string | null; names: string[] }
  pricing: EnginePricing | null
  meter: UsageMeter | null
  last_run: RunRecord | null
  last_run_pricing: EnginePricing | null
}

// ---------------------------------------------------------------------------
// The Run's typed events, as the live stream sends them (named events beside
// the text lines) and as the Run's event file records them. Mirrors
// src/regcompass/run_events.py.
// ---------------------------------------------------------------------------

/** A Step's stable name on the wire. The screen shows the glossary name:
 *  Read, Scan check, Cut into Pieces, Gate, Map, Prove, Gloss, Reconcile. */
export type RunStep =
  | 'read'
  | 'scan_check'
  | 'cut'
  | 'gate'
  | 'map'
  | 'prove'
  | 'gloss'
  | 'reconcile'

/** One Document a Run reads, as run_started lists it. */
export interface RunDocumentInfo {
  document_id: string
  title: string
  language: string | null
  n_pages: number | null
  /** 'html' for a web page, which has no pages; absent in older recordings. */
  format?: 'pdf' | 'html' | null
}

export interface RunTotals {
  documents: number
  pieces: number
  pairs: number
  candidates: number
  proven: number
  no_evidence: number
  dropped: number
  glossed: number
  groups: number
}

interface RunEventBase {
  seq: number
  ts: string
}

/** The Run's own meter at that moment: Engine calls so far and their cost at
 *  the Engine's declared prices. Absent on a Run recorded before they were. */
interface RunMeterFields {
  engine_calls?: number
  cost_usd?: number
}

export type RunEvent =
  | (RunEventBase & {
      type: 'run_started'
      run_id: string | null
      economy: string
      pillars: number[]
      indicators: string[] | null
      engine: string
      documents: RunDocumentInfo[]
      /** Set when the Run was rebuilt from an older record, e.g. 'log'. */
      recorded_from?: string | null
      /** What that older record never kept, e.g. 'gate_scores'. */
      not_recorded?: string[]
    })
  | (RunEventBase & {
      type: 'step_started'
      document_id: string | null
      step: RunStep
      /** On a Document's first Step when run_started did not list it (a Run
       *  that starts with Discovery): what run_started would have said. */
      title?: string | null
      language?: string | null
      n_pages?: number | null
      format?: 'pdf' | 'html' | null
    })
  | (RunEventBase & {
      type: 'step_finished'
      document_id: string | null
      step: RunStep
      counts: Record<string, number>
    })
  | (RunEventBase &
      RunMeterFields & { type: 'map_progress'; document_id: string; done: number; total: number })
  | (RunEventBase & { type: 'scan_flagged'; document_id: string; reason: string })
  | (RunEventBase &
      RunMeterFields & { type: 'document_finished'; document_id: string; mappings: number })
  | (RunEventBase & RunMeterFields & { type: 'reconcile'; before: number; after: number })
  | (RunEventBase & {
      type: 'run_finished'
      status: string
      totals: Partial<RunTotals>
      cost_usd: number | null
      /** What the provider itself billed, when it sent a figure. */
      provider_cost_usd?: number | null
      /** Absent on a Run recorded before the count was kept. */
      engine_calls?: number | null
    })
  | (RunEventBase & {
      type: 'run_failed'
      document_id: string | null
      step: RunStep | null
      message: string
    })
  | (RunEventBase & {
      type: 'candidate'
      document_id: string
      piece_id: string
      indicator: string
      /** Closeness of meaning to the Pillar, -1 to 1. */
      cosine: number
      /** Keyword score for the Indicator; 0 in the meaning_only lane. */
      bm25: number
      lane: CandidateLane
      outcome: CandidateOutcome
      section?: string | null
      page?: number | null
    })
  | (RunEventBase & {
      type: 'mapping_added'
      document_id: string
      mapping_id: string
      indicator: string
      page: number | null
    })

export type RunEventType = RunEvent['type']

/** What happened to one Candidate. Mirrors regcompass.run_progress. */
export type CandidateOutcome = 'mapped' | 'not_applicable' | 'dropped_by_proof' | 'skipped'

/** How the Gate judged a Document: meaning and keywords, or meaning alone. */
export type CandidateLane = 'meaning_and_keywords' | 'meaning_only'

/** One Piece, as /api/pieces/<id> serves it: a slice of its Document's
 *  stored text at the Piece's offsets. */
export interface PieceDetail {
  piece_id: string
  document_id: string
  document_title: string | null
  section: string | null
  page: number | null
  page_end: number | null
  char_start: number
  char_end: number
  text: string
  /** 'html' when the Document is a web page. */
  format?: 'pdf' | 'html'
}

// ---------------------------------------------------------------------------
// Discovery's typed events, as the live stream sends them (named events beside
// the text lines). Mirrors src/regcompass/discovery_progress.py.
// ---------------------------------------------------------------------------

/** Why a Document was not added, as a stable code. The event also carries the
 *  reason in plain words, which is what the screen shows. */
export type DiscoverySkipCode =
  | 'in_corpus'
  | 'off_whitelist'
  | 'robots'
  | 'http_error'
  | 'fetch_error'
  | 'duplicate'
  | 'unchanged'
  | 'not_added'
  | 'limit'
  | 'failed_before'
  | 'not_fetched'

export interface DiscoveryCounts {
  run_id: string | null
  found: number
  fetched: number
  added: number
  skipped: number
  stored: number
  already_in_corpus: number
  failed: number
  disallowed: number
  duplicates: number
  off_whitelist: number
  spacing_seconds: number
  /** A Discovery by Pillar only: the draw, why each Document came in, and
   *  each baseline law it did not fetch. */
  pillar?: number | null
  indicators?: string[] | null
  max_documents?: number | null
  found_by?: DiscoveryFoundBy[]
  baseline_skipped?: BaselineSkip[]
  notes?: string[]
}

/** Why a Document came in: "baseline 6.1, 6.4" or "portal crawler". */
export interface DiscoveryFoundBy {
  url: string
  document_id: string | null
  title: string | null
  found_by: string
  /** "fetched", or "already in the Corpus". */
  status: string
}

/** A law the baseline cites for the drawn Indicators that was not fetched. */
export interface BaselineSkip {
  law: string
  indicators: string[]
  urls: string[]
  code: string
  reason: string
}

export type DiscoveryEvent =
  | (RunEventBase & {
      type: 'discovery_portal'
      economy: string
      name: string
      hosts: string[]
      strategy: string
      refresh: boolean
      run_id: string
    })
  | (RunEventBase & { type: 'discovery_found'; url: string; name: string | null })
  | (RunEventBase & { type: 'discovery_fetched'; url: string; size_bytes: number; method: string })
  | (RunEventBase & {
      type: 'discovery_added'
      url: string
      document_id: string
      title: string
      n_pages: number
      ocr_applied: boolean
    })
  | (RunEventBase & {
      type: 'discovery_skipped'
      url: string
      code: DiscoverySkipCode | string
      reason: string
      /** The Corpus title, when the Document is already in the Corpus. */
      title?: string | null
    })
  | (RunEventBase & { type: 'discovery_finished'; counts: Partial<DiscoveryCounts> })
  | (RunEventBase & { type: 'discovery_failed'; message: string })

export type DiscoveryEventType = DiscoveryEvent['type']
