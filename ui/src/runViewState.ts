// The Run view's state: the Run's typed events in, the picture on screen out.
//
// One pure reducer. The live stream, a page reload (the stream replays every
// event from the first) and a recorded Run all feed it the same events, so the
// screen has one code path. The state keeps every event it has seen, in seq
// order: an event seen before is ignored, and one that arrives out of order is
// slotted into place and the picture is rebuilt from the start, so the result
// never depends on arrival order.

import type {
  CandidateLane,
  CandidateOutcome,
  RunDocumentInfo,
  RunEvent,
  RunStep,
  RunTotals,
} from './types'

/** One Candidate (a Piece and Indicator pair the Gate kept), once settled. */
export interface CandidateView {
  piece_id: string
  indicator: string
  cosine: number
  bm25: number
  lane: CandidateLane
  outcome: CandidateOutcome
  section: string | null
  page: number | null
  /** The Mapping it became, when the outcome is mapped. */
  mapping_id: string | null
}

/** One proven Mapping, saved and ready to open. */
export interface MappingRef {
  mapping_id: string
  indicator: string
  page: number | null
}

export interface DocumentView {
  document_id: string
  title: string
  language: string | null
  n_pages: number | null
  format: 'pdf' | 'html' | null
  /** The Step this Document is on: the last one started. null before Read. */
  step: RunStep | null
  /** The counts each finished Step reported. */
  counts: Partial<Record<RunStep, Record<string, number>>>
  finished: RunStep[]
  mapDone: number
  mapTotal: number
  /** 0 to 1. */
  progress: number
  done: boolean
  /** The Mappings this Document proved, once it is finished. */
  mappings: number | null
  failed: boolean
  /** Why the Scan check flagged this Document's text, or null. It is still mapped. */
  flag: string | null
  /** When each Step started and finished, in ms since 1970, from the events. */
  times: Partial<Record<RunStep, { start: number; end: number | null }>>
  /** Every Candidate settled so far, in Candidate order. Empty for a Run
   *  recorded before the Gate's scores were. */
  candidates: CandidateView[]
  /** Every proven Mapping saved so far, in order. */
  mappingsFound: MappingRef[]
}

export interface RunMeter {
  engine_calls: number | null
  cost_usd: number | null
}

export interface RunInfo {
  run_id: string | null
  economy: string
  pillars: number[]
  indicators: string[] | null
  engine: string
  /** Set when the Run was rebuilt from an older record ('log'), else null. */
  recordedFrom: string | null
  /** What that older record never kept; empty for a Run the app recorded. */
  notRecorded: string[]
}

export interface RunFailure {
  document_id: string | null
  step: RunStep | null
  message: string
}

export interface RunViewState {
  /** Every event seen, in seq order, each once. */
  events: RunEvent[]
  run: RunInfo | null
  status: 'idle' | 'running' | 'finished' | 'failed'
  documents: DocumentView[]
  reconcile: 'waiting' | 'running' | 'done'
  /** The whole Run, 0 to 1: every Document, then Reconcile. */
  progress: number
  totals: Partial<RunTotals> | null
  cost_usd: number | null
  /** The provider's own bill for the Run, when it sent one. */
  providerCost: number | null
  failure: RunFailure | null
  /** When the Run started, and the latest time any event carries (ms). */
  startedAt: number | null
  lastAt: number | null
  /** When the Run finished or stopped (ms), or null while it goes. */
  endedAt: number | null
  /** The Run's own meter, as its last metered event read it. */
  meter: RunMeter | null
  /** Reconcile's result: proven Mappings in, groups out. */
  reconciled: { before: number; after: number } | null
  reconcileTimes: { start: number; end: number | null } | null
}

export type RunViewAction =
  | { type: 'event'; event: RunEvent }
  | { type: 'replay'; events: RunEvent[] }
  | { type: 'reset' }

export const emptyRunView: RunViewState = {
  events: [],
  run: null,
  status: 'idle',
  documents: [],
  reconcile: 'waiting',
  progress: 0,
  totals: null,
  cost_usd: null,
  providerCost: null,
  failure: null,
  startedAt: null,
  lastAt: null,
  endedAt: null,
  meter: null,
  reconciled: null,
  reconcileTimes: null,
}

/** An event's time in ms. Python writes microseconds; only three digits are
 *  kept so every JavaScript engine reads it the same way. */
export function eventTime(ts: string): number | null {
  if (typeof ts !== 'string') return null
  const trimmed = ts.replace(/(\.\d{3})\d+/, '$1')
  const t = Date.parse(trimmed)
  return Number.isFinite(t) ? t : null
}

const LABELS: Record<RunStep, string> = {
  read: 'Read',
  scan_check: 'Scan check',
  cut: 'Split into sections',
  gate: 'Gate',
  // Map and Prove run pair by pair, interleaved: Prove is not a separate wait,
  // so the two are shown as one Step.
  map: 'Map and Prove',
  prove: 'Map and Prove',
  gloss: 'Gloss',
  reconcile: 'Reconcile',
}

/** The glossary name of a Step, as the screen shows it. */
export function stepLabel(step: RunStep): string {
  return LABELS[step] ?? step
}

// The parts of a Document's bar. Map and Prove count as one part, filled as
// Map's pairs are done.
const PARTS = 6

function documentProgress(d: DocumentView): number {
  if (d.done) return 1
  const has = (s: RunStep) => d.finished.includes(s)
  let parts = 0
  for (const s of ['read', 'scan_check', 'cut', 'gate', 'gloss'] as RunStep[]) {
    if (has(s)) parts += 1
  }
  if (has('prove') || has('map')) parts += 1
  else if (d.step === 'map' && d.mapTotal > 0) parts += Math.min(1, d.mapDone / d.mapTotal)
  return parts / PARTS
}

function newDocument(info: RunDocumentInfo): DocumentView {
  return {
    document_id: info.document_id,
    title: info.title || info.document_id,
    language: info.language ?? null,
    n_pages: info.n_pages ?? null,
    format: info.format ?? null,
    step: null,
    counts: {},
    finished: [],
    mapDone: 0,
    mapTotal: 0,
    progress: 0,
    done: false,
    mappings: null,
    failed: false,
    flag: null,
    times: {},
    candidates: [],
    mappingsFound: [],
  }
}

/** Change one Document, adding it first if the Run did not list it up front
 *  (a Run that starts with Discovery learns its Documents as it goes). */
function withDocument(
  state: RunViewState,
  documentId: string,
  change: (d: DocumentView) => DocumentView,
): RunViewState {
  const known = state.documents.some((d) => d.document_id === documentId)
  const documents = known
    ? state.documents
    : [...state.documents, newDocument({ document_id: documentId, title: documentId, language: null, n_pages: null })]
  return {
    ...state,
    documents: documents.map((d) => {
      if (d.document_id !== documentId) return d
      const next = change(d)
      return { ...next, progress: documentProgress(next) }
    }),
  }
}

function runProgress(state: RunViewState): number {
  if (state.status === 'finished') return 1
  const n = state.documents.length
  if (n === 0) return 0
  const docs = state.documents.reduce((sum, d) => sum + d.progress, 0)
  return (docs + (state.reconcile === 'done' ? 1 : 0)) / (n + 1)
}

function withMeter(state: RunViewState, e: { engine_calls?: number | null; cost_usd?: number | null }): RunViewState {
  const calls = typeof e.engine_calls === 'number' ? e.engine_calls : null
  const cost = typeof e.cost_usd === 'number' ? e.cost_usd : null
  if (calls === null && cost === null) return state
  return {
    ...state,
    meter: {
      engine_calls: calls ?? state.meter?.engine_calls ?? null,
      cost_usd: cost ?? state.meter?.cost_usd ?? null,
    },
  }
}

/** One event applied to the picture, assuming every earlier event already is. */
function apply(state: RunViewState, e: RunEvent): RunViewState {
  const at = eventTime(e.ts)
  if (at !== null && (state.lastAt === null || at > state.lastAt)) state = { ...state, lastAt: at }
  let next: RunViewState
  switch (e.type) {
    case 'run_started':
      next = {
        ...state,
        run: {
          run_id: e.run_id,
          economy: e.economy,
          pillars: e.pillars,
          indicators: e.indicators,
          engine: e.engine,
          recordedFrom: e.recorded_from ?? null,
          notRecorded: e.not_recorded ?? [],
        },
        status: 'running',
        documents: e.documents.map(newDocument),
        startedAt: at,
      }
      break
    case 'step_started':
      if (e.document_id === null) {
        next =
          e.step === 'reconcile'
            ? { ...state, reconcile: 'running', reconcileTimes: at === null ? null : { start: at, end: null } }
            : state
      } else {
        next = withDocument(state, e.document_id, (d) => ({
          ...d,
          step: e.step,
          times: at === null ? d.times : { ...d.times, [e.step]: { start: at, end: null } },
        }))
      }
      break
    case 'step_finished':
      if (e.document_id === null) {
        if (e.step !== 'reconcile') {
          next = state
        } else {
          const before = e.counts.passed
          const after = e.counts.groups
          next = {
            ...state,
            reconcile: 'done',
            reconcileTimes:
              state.reconcileTimes && at !== null ? { ...state.reconcileTimes, end: at } : state.reconcileTimes,
            // An older record has no reconcile event: its counts say the same.
            reconciled:
              state.reconciled ??
              (typeof before === 'number' && typeof after === 'number' ? { before, after } : null),
          }
        }
      } else {
        next = withDocument(state, e.document_id, (d) => ({
          ...d,
          times:
            at === null
              ? d.times
              : { ...d.times, [e.step]: { start: d.times[e.step]?.start ?? at, end: at } },
          counts: { ...d.counts, [e.step]: e.counts },
          finished: d.finished.includes(e.step) ? d.finished : [...d.finished, e.step],
          ...(e.step === 'map'
            ? { mapDone: e.counts.done ?? d.mapDone, mapTotal: e.counts.total ?? d.mapTotal }
            : {}),
        }))
      }
      break
    case 'map_progress':
      next = withMeter(
        withDocument(state, e.document_id, (d) => ({ ...d, mapDone: e.done, mapTotal: e.total })),
        e,
      )
      break
    case 'scan_flagged':
      next = withDocument(state, e.document_id, (d) => ({ ...d, flag: e.reason || 'Flagged by the Scan check' }))
      break
    case 'document_finished':
      next = withMeter(
        withDocument(state, e.document_id, (d) => ({ ...d, done: true, mappings: e.mappings })),
        e,
      )
      break
    case 'reconcile':
      next = withMeter({ ...state, reconciled: { before: e.before, after: e.after } }, e)
      break
    case 'run_finished':
      next = withMeter(
        {
          ...state,
          status: 'finished',
          totals: e.totals,
          cost_usd: e.cost_usd,
          providerCost: typeof e.provider_cost_usd === 'number' ? e.provider_cost_usd : null,
          endedAt: at,
        },
        { engine_calls: e.engine_calls, cost_usd: e.cost_usd },
      )
      break
    case 'candidate': {
      const c: CandidateView = {
        piece_id: e.piece_id,
        indicator: e.indicator,
        cosine: e.cosine,
        bm25: e.bm25,
        lane: e.lane,
        outcome: e.outcome,
        section: e.section ?? null,
        page: e.page ?? null,
        mapping_id: e.outcome === 'mapped' ? `${e.piece_id}::${e.indicator}` : null,
      }
      next = withDocument(state, e.document_id, (d) =>
        d.candidates.some((x) => x.piece_id === c.piece_id && x.indicator === c.indicator)
          ? d
          : { ...d, candidates: [...d.candidates, c] },
      )
      break
    }
    case 'mapping_added':
      next = withDocument(state, e.document_id, (d) =>
        d.mappingsFound.some((m) => m.mapping_id === e.mapping_id)
          ? d
          : {
              ...d,
              mappingsFound: [...d.mappingsFound, { mapping_id: e.mapping_id, indicator: e.indicator, page: e.page }],
            },
      )
      break
    case 'run_failed': {
      const failed: RunViewState = {
        ...state,
        endedAt: at,
        status: 'failed',
        failure: { document_id: e.document_id, step: e.step, message: e.message },
      }
      next =
        e.document_id === null
          ? failed
          : withDocument(failed, e.document_id, (d) => ({ ...d, failed: true, step: e.step ?? d.step }))
      break
    }
    default:
      return state
  }
  return { ...next, progress: runProgress(next) }
}

function rebuild(events: RunEvent[]): RunViewState {
  return events.reduce(apply, { ...emptyRunView, events })
}

/** Where an event with this seq sits in the seq-ordered list: its index if
 *  it is there, or the index it would be inserted at. */
function seqIndex(events: RunEvent[], seq: number): { index: number; found: boolean } {
  let lo = 0
  let hi = events.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (events[mid].seq < seq) lo = mid + 1
    else hi = mid
  }
  return { index: lo, found: lo < events.length && events[lo].seq === seq }
}

function receive(state: RunViewState, event: RunEvent): RunViewState {
  // A new Run's first event: whatever was on screen belonged to the last one.
  if (event.type === 'run_started' && state.run !== null && state.run.run_id !== event.run_id) {
    return receive(emptyRunView, event)
  }
  const events = state.events
  const last = events.length ? events[events.length - 1].seq : -1
  // The usual case, in order: one step forward, nothing rebuilt.
  if (event.seq > last) return apply({ ...state, events: events.concat(event) }, event)
  const { index, found } = seqIndex(events, event.seq)
  if (found) return state
  // Out of order: slot it in and rebuild the picture from the start.
  return rebuild([...events.slice(0, index), event, ...events.slice(index)])
}

/** A whole stream at once (a page reload): sorted and deduplicated once, and
 *  the picture built once, from the last Run's first event. */
function replay(incoming: RunEvent[]): RunViewState {
  const bySeq = new Map<number, RunEvent>()
  let runId: string | null | undefined
  let fromSeq = -Infinity
  for (const e of [...incoming].sort((a, b) => a.seq - b.seq)) {
    if (e.type === 'run_started') {
      if (runId !== undefined && e.run_id !== runId) {
        bySeq.clear()
        fromSeq = e.seq
      }
      runId = e.run_id
    }
    if (e.seq >= fromSeq && !bySeq.has(e.seq)) bySeq.set(e.seq, e)
  }
  return rebuild([...bySeq.values()])
}

export function runViewReducer(state: RunViewState, action: RunViewAction): RunViewState {
  switch (action.type) {
    case 'reset':
      return emptyRunView
    case 'replay':
      return replay(action.events)
    case 'event':
      return receive(state, action.event)
    default:
      return state
  }
}
