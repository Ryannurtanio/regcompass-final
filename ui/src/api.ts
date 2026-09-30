import type {
  PieceDetail,
  AcceptAllResult,
  AddedDocument,
  AuditRun,
  ClearReport,
  Comparison,
  ComparisonQuery,
  CorpusEmpty,
  CorpusListing,
  CorpusSummary,
  DocumentEdit,
  DocumentRemoval,
  DocumentSummary,
  EngineInfo,
  EvidenceEconomy,
  ExportPreview,
  ExportSummary,
  Gloss,
  IndicatorInfo,
  Politeness,
  RecordDetail,
  RecordSummary,
  QueueFilter,
  Review,
  ReviewHistoryEntry,
  ReviewQueue,
  ReviewStatus,
  RunEvent,
  RunEventType,
  DiscoveryEvent,
  DiscoveryEventType,
  RunsPage,
  RunStarted,
  RunStats,
  ServerStatus,
} from './types'
import { ApiError } from './errors'

/** fetch, except that a 401 means the session has ended (or never began) on a
 *  server with the login on: the page goes to the sign-in page and comes back
 *  here afterwards. A server with the login off never answers 401. */
async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const r = await fetch(input, init)
  if (r.status === 401) {
    const here = window.location.pathname + window.location.search
    window.location.assign(`/login?next=${encodeURIComponent(here)}`)
    throw new Error('login required')
  }
  return r
}

async function get<T>(path: string): Promise<T> {
  const r = await apiFetch(path)
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<T>
}

/** ?run_id=... on every audit read, so the view is scoped to ONE Run. Without
 * it the server falls back to the newest completed Run of the Economy. */
function scoped(path: string, runId?: string | null): string {
  return runId ? `${path}${path.includes('?') ? '&' : '?'}run_id=${encodeURIComponent(runId)}` : path
}

export const fetchDocuments = (runId?: string | null) =>
  get<DocumentSummary[]>(scoped('/api/documents', runId))

export const fetchRecords = (documentId: string, runId?: string | null) =>
  get<RecordSummary[]>(
    scoped(`/api/documents/${encodeURIComponent(documentId)}/records`, runId),
  )

/** The Review queue: every record of the Run in one list, lowest Confidence
 *  first. A sibling of the per-Document read, which answers a narrower
 *  question and carries no counts. */
export const fetchReviewQueue = (runId?: string | null, filter: QueueFilter = 'all') =>
  get<ReviewQueue>(
    scoped(
      `/api/records?sort=confidence${
        filter === 'unreviewed' ? '&unreviewed=true' : filter === 'corrected' ? '&corrected=true' : ''
      }`,
      runId,
    ),
  )

/** Every decision ever written for one Mapping, oldest first. */
export const fetchReviewHistory = (mappingId: string, runId?: string | null) =>
  get<{ run_id: string | null; mapping_id: string; history: ReviewHistoryEntry[] }>(
    scoped(`/api/reviews/history?mapping_id=${encodeURIComponent(mappingId)}`, runId),
  )

export const fetchRecordDetail = (mappingId: string, runId?: string | null) =>
  get<RecordDetail>(scoped(`/api/records/${encodeURIComponent(mappingId)}`, runId))

export const fetchPiece = (pieceId: string) =>
  get<PieceDetail>(`/api/pieces/${encodeURIComponent(pieceId)}`)

export const pdfUrl = (documentId: string, runId?: string | null) =>
  scoped(`/api/documents/${encodeURIComponent(documentId)}/pdf`, runId)

export const fetchStatus = () => get<ServerStatus>('/api/status')

export const fetchEngines = () =>
  get<{ engines: EngineInfo[]; default_engine: string }>('/api/engines')

export const fetchPoliteness = () => get<Politeness>('/api/settings/politeness')

export const fetchIndicators = (pillar: number) =>
  get<{ pillar: number; indicators: IndicatorInfo[] }>(
    `/api/indicators?pillar=${encodeURIComponent(String(pillar))}`,
  )

export const RUNS_PAGE = 50

/** One page of Run Records, newest first. `total` is how many there are, so
 *  a list can offer the older ones rather than silently stop at a page. */
export const fetchRuns = (offset = 0, limit = RUNS_PAGE) =>
  get<RunsPage>(`/api/runs?limit=${limit}&offset=${offset}`)

/** Every finished Run (the server caps a page at 500). The featured Economies,
 *  the cost estimate and the Comparison pickers are all about finished Runs,
 *  and asking for exactly those means a burst of Document adds, each of which
 *  files a record, can never push a real Run out of view. */
export const fetchFinishedRuns = () =>
  get<RunsPage>('/api/runs?kind=run&status=completed&limit=500')

/** What removing one Document would take, before anything is removed. */
export async function fetchDocumentRemoval(documentId: string): Promise<DocumentRemoval> {
  const r = await apiFetch(`/api/documents/${encodeURIComponent(documentId)}/removal`)
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<DocumentRemoval>
}

/** Remove one Document from its Corpus. The confirmation happens on the page
 *  first: this call is the reviewer's yes. */
export async function deleteDocument(documentId: string): Promise<DocumentRemoval> {
  const r = await apiFetch(`/api/documents/${encodeURIComponent(documentId)}`, {
    method: 'DELETE',
  })
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<DocumentRemoval>
}

/** Every Economy's Corpus as it is now: its Document count and languages. */
export const fetchCorpusSummary = () =>
  get<{ economies: Record<string, CorpusSummary> }>('/api/corpus/summary')

/** One Economy's Corpus: what a Run would read. Not /api/documents, which
 *  answers what a Run mapped and so cannot show an upload until one has. */
export const fetchCorpus = (economy: string) =>
  get<CorpusListing>(`/api/corpus?economy=${encodeURIComponent(economy)}`)

/** Correct an already-added Document's Source URL, its title, or both, in
 *  place. Adding it again by URL would fetch the file a second time and make a
 *  SECOND Document, which is a duplicate rather than a correction. Only what
 *  is given changes; the name is optional and recorded with the edit. */
export async function patchDocument(
  documentId: string,
  edit: { sourceUrl?: string; title?: string; reviewer?: string | null },
): Promise<DocumentEdit> {
  const r = await apiFetch(`/api/documents/${encodeURIComponent(documentId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...(edit.sourceUrl !== undefined ? { source_url: edit.sourceUrl.trim() } : {}),
      ...(edit.title !== undefined ? { title: edit.title.trim() } : {}),
      reviewer: edit.reviewer?.trim() ? edit.reviewer.trim() : null,
    }),
  })
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<DocumentEdit>
}

/** Every Economy with a finished Run, newest first, with the newest Run per
 *  Engine and set of Pillars: what the Evidence screen's pickers offer. */
export const fetchEvidenceRuns = () =>
  get<{ economies: EvidenceEconomy[] }>('/api/evidence/runs')

/** WHICH Run the audit screens are showing. With no Run named the server falls
 *  back to the newest completed one, and the Evidence header has to be able to
 *  say so rather than leaving the reviewer guessing. */
export const fetchAuditRun = (runId?: string | null) =>
  get<AuditRun>(scoped('/api/audit/run', runId))

/** The progress lines still buffered from the last job, so the Run panel can
 *  show them again after the reviewer has been away on another screen. */
/** The Run panel's funnel, stage trace and spend, for the job in flight or
 *  the last one. Never fails on a fresh server: it answers zeros. */
export const fetchStats = () => get<RunStats>('/api/stats')

export const fetchRunLog = () =>
  get<{
    lines: string[]
    // The last job's typed events, so a reload after a Run shows the same
    // picture. Absent on a server built before events existed.
    events?: (RunEvent | DiscoveryEvent)[]
    status: string
    active: boolean
    run_id: string | null
  }>('/api/run/log')

/** The named events /api/events sends beside its text lines. An EventSource
 *  only hands a named event to a listener registered for that name, so the
 *  text-line handler (onmessage) never sees one. */
export const RUN_EVENT_TYPES: readonly RunEventType[] = [
  'run_started',
  'step_started',
  'step_finished',
  'map_progress',
  'scan_flagged',
  'document_finished',
  'reconcile',
  'run_finished',
  'run_failed',
  'candidate',
  'mapping_added',
]

/** One named event's payload, or null when it is not a well-formed event of
 *  that name. */
export function parseRunEvent(name: string, data: string): RunEvent | null {
  if (!(RUN_EVENT_TYPES as readonly string[]).includes(name)) return null
  try {
    const obj = JSON.parse(data)
    if (!obj || typeof obj !== 'object') return null
    if (obj.type !== name || typeof obj.seq !== 'number') return null
    return obj as RunEvent
  } catch {
    return null
  }
}

/** Listen for every typed event on an open stream. */
export function listenForRunEvents(
  source: EventSource,
  onEvent: (event: RunEvent) => void,
): void {
  for (const name of RUN_EVENT_TYPES) {
    source.addEventListener(name, (ev) => {
      const event = parseRunEvent(name, (ev as MessageEvent).data)
      if (event) onEvent(event)
    })
  }
}

/** The named events a Discovery sends beside its text lines. */
export const DISCOVERY_EVENT_TYPES: readonly DiscoveryEventType[] = [
  'discovery_portal',
  'discovery_found',
  'discovery_fetched',
  'discovery_added',
  'discovery_skipped',
  'discovery_finished',
  'discovery_failed',
]

/** One named Discovery event's payload, or null when it is not a well-formed
 *  event of that name. */
export function parseDiscoveryEvent(name: string, data: string): DiscoveryEvent | null {
  if (!(DISCOVERY_EVENT_TYPES as readonly string[]).includes(name)) return null
  try {
    const obj = JSON.parse(data)
    if (!obj || typeof obj !== 'object') return null
    if (obj.type !== name || typeof obj.seq !== 'number') return null
    return obj as DiscoveryEvent
  } catch {
    return null
  }
}

/** Listen for every Discovery event on an open stream. */
export function listenForDiscoveryEvents(
  source: EventSource,
  onEvent: (event: DiscoveryEvent) => void,
): void {
  for (const name of DISCOVERY_EVENT_TYPES) {
    source.addEventListener(name, (ev) => {
      const event = parseDiscoveryEvent(name, (ev as MessageEvent).data)
      if (event) onEvent(event)
    })
  }
}

export const runDownloadUrl = (runId: string) =>
  `/api/runs/${encodeURIComponent(runId)}/download`

/** An Evidence Export file, streamed out of the server's output directory. On
 *  the container path that directory is inside a named volume, so this link is
 *  the only way a reviewer gets the workbook onto their own machine. */
export const outputDownloadUrl = (name: string) =>
  `/api/outputs/download?name=${encodeURIComponent(name)}`

function comparisonQuery(query: ComparisonQuery): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) params.set(key, String(value))
  return params.toString()
}

/** A Comparison, either of two named Runs or of the newest Run per Engine for
 * one Economy and Pillar. A 404 here is usually "only one Engine has run this
 * Economy and Pillar", so the server's own message is the one worth showing. */
export async function fetchComparison(query: ComparisonQuery): Promise<Comparison> {
  const r = await apiFetch(`/api/compare?${comparisonQuery(query)}`)
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<Comparison>
}

export const comparisonDownloadUrl = (
  query: ComparisonQuery,
  format: 'csv' | 'json' | 'xlsx' | 'sheet_csv',
) =>
  `/api/compare/download?${comparisonQuery(query)}&format=${format}`

/** A Corpus a Run cannot read is not a failure, it is a missing prerequisite:
 * the 409 body comes back so the Run panel can offer Discovery. */
export class CorpusEmptyError extends Error {
  constructor(public detail: CorpusEmpty) {
    super(detail.message)
    this.name = 'CorpusEmptyError'
  }
}

export async function postRun(body: {
  economy: string
  pillars: number[]
  indicators: string[] | null
  engine: string
  /** "e2e": a Discovery by the Run's one Pillar and its Indicators, then the Run. */
  mode?: 'run' | 'e2e'
  discover_by_pillar?: boolean
}): Promise<RunStarted> {
  const r = await apiFetch('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (r.status === 409) {
    const payload = await r.json().catch(() => null)
    const detail = payload?.detail
    if (detail && typeof detail === 'object' && detail.corpus_empty) {
      throw new CorpusEmptyError(detail as CorpusEmpty)
    }
    throw new ApiError(409, typeof detail === 'string' ? detail : 'A Run is already going.')
  }
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<RunStarted>
}

/** A Discovery by Pillar: the baseline laws for these Indicators (every
 *  Indicator of the Pillar when null), then the Portal crawler for the Pillar. */
export interface DiscoveryDrawRequest {
  pillar: number
  indicators: string[] | null
}

export async function postDiscover(
  economy: string,
  refresh = false,
  draw: DiscoveryDrawRequest | null = null,
): Promise<void> {
  const r = await apiFetch('/api/discover', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(draw ? { economy, refresh, ...draw } : { economy, refresh }),
  })
  if (!r.ok) throw await failure(r)
}

/** "Add document" by upload: a real file picker, so the body is multipart. */
export async function postDocumentUpload(body: {
  economy: string
  sourceUrl: string
  language: string
  title: string
  file: File
}): Promise<AddedDocument> {
  const form = new FormData()
  form.set('economy', body.economy)
  form.set('language', body.language)
  // Both optional, and both LEFT OUT rather than sent empty: an empty string
  // would be stored as the Document's address and read as a broken one.
  if (body.sourceUrl.trim()) form.set('source_url', body.sourceUrl.trim())
  if (body.title.trim()) form.set('title', body.title.trim())
  form.set('file', body.file, body.file.name)
  const r = await apiFetch('/api/documents/upload', { method: 'POST', body: form })
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<AddedDocument>
}

/** "Add document" by Source URL: one polite fetch, refused on an Economy whose
 *  Portal forbids automated collection. */
export async function postDocumentUrl(body: {
  economy: string
  sourceUrl: string
  language: string
  title: string
  allowAnyHost: boolean
}): Promise<AddedDocument> {
  const r = await apiFetch('/api/documents/add-url', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      economy: body.economy,
      source_url: body.sourceUrl,
      language: body.language,
      title: body.title.trim() || null,
      allow_any_host: body.allowAnyHost,
    }),
  })
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<AddedDocument>
}

export async function postSettingsKey(engine: string, key: string): Promise<void> {
  const r = await apiFetch('/api/settings/key', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ engine, key }),
  })
  if (!r.ok) throw await failure(r)
}

/** What a clear of this scope would remove. `null` means every Economy. */
export async function fetchClearPreview(economy: string | null): Promise<ClearReport> {
  const path = economy
    ? `/api/clear/preview?economy=${encodeURIComponent(economy)}`
    : '/api/clear/preview'
  const r = await apiFetch(path)
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<ClearReport>
}

/** Clear the downloads and the caches of this scope. The confirmation happens
 *  on the page first: this call is the operator's yes. */
export async function postClear(economy: string | null): Promise<ClearReport> {
  const r = await apiFetch('/api/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ economy, confirm: true }),
  })
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<ClearReport>
}

export async function deleteSettingsKey(engine: string): Promise<void> {
  const r = await apiFetch(`/api/settings/key/${encodeURIComponent(engine)}`, {
    method: 'DELETE',
  })
  if (!r.ok) throw await failure(r)
}

/** The failure the server sent, carrying its own `detail` when it sent one: a
 *  Review Decision refused on the frozen bundle lane explains itself, and the
 *  reviewer deserves to read that rather than a status code. */
async function failure(r: Response): Promise<ApiError> {
  const payload = await r.json().catch(() => null)
  return new ApiError(r.status, payload?.detail ?? null)
}

export async function postReview(
  runId: string | null,
  mappingId: string,
  status: ReviewStatus,
  comment?: string | null,
  correction?: { indicatorId: string; reviewer?: string | null },
): Promise<Review> {
  const r = await apiFetch('/api/reviews', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      run_id: runId,
      mapping_id: mappingId,
      review_status: status,
      comment: comment ? comment : null,
      ...(correction
        ? {
            corrected_indicator_id: correction.indicatorId,
            reviewer: correction.reviewer ? correction.reviewer : null,
          }
        : {}),
    }),
  })
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<Review>
}

/** Approve a Gloss: the text the reviewer approved, under the name they
 *  approved it with. The name is required, because it is the only thing that
 *  lets the Evidence Export ship the rendering without its AI label. */
export async function reviewGloss(
  runId: string | null,
  mappingId: string,
  english: string,
  reviewedBy: string,
): Promise<Gloss> {
  const r = await apiFetch('/api/glosses/review', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      run_id: runId,
      mapping_id: mappingId,
      english,
      reviewed_by: reviewedBy,
    }),
  })
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<Gloss>
}

export const fetchExportPreview = (runId?: string | null) =>
  get<ExportPreview>(scoped('/api/export/preview', runId))

/** Accept every verified Mapping of this Run that carries no decision yet.
 *  Decisions already made are left alone. */
export async function postAcceptAll(runId: string | null): Promise<AcceptAllResult> {
  const r = await apiFetch('/api/reviews/accept-all', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_id: runId }),
  })
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<AcceptAllResult>
}

export async function postExport(runId?: string | null): Promise<ExportSummary> {
  const r = await apiFetch(scoped('/api/export', runId), { method: 'POST' })
  // A refusal keeps its structured detail: the screen lists each gate failure
  // with the Documents named by title (see exportRefusal).
  if (!r.ok) throw await failure(r)
  return r.json() as Promise<ExportSummary>
}

/** End the browser's session and go to the sign-in page. Only offered when
 *  the server says a login is on. */
export async function logout(): Promise<void> {
  try {
    await fetch('/api/logout', { method: 'POST' })
  } finally {
    window.location.assign('/login')
  }
}
