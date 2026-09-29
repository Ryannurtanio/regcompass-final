// The Discovery view's state: Discovery's typed events in, the picture on
// screen out.
//
// One pure reducer, the same shape as the Run view's (runViewState.ts): the
// live stream and a page reload (the stream replays every event from the
// first) feed it the same events, so the screen has one code path. Every event
// is kept once, in seq order; one that arrives out of order is slotted into
// place and the picture rebuilt, so the result never depends on arrival order.
// Events of any other kind (a Run's) are ignored.

import type { DiscoveryCounts, DiscoveryEvent } from './types'

export type DiscoveryDocumentState = 'found' | 'fetched' | 'added' | 'skipped'

export interface DiscoveryDocument {
  url: string
  /** The Document's title once it is in the Corpus, else the file name the
   *  Portal listed, else null (the screen then shows the address). */
  title: string | null
  state: DiscoveryDocumentState
  document_id: string | null
  n_pages: number | null
  size_bytes: number | null
  /** Read from a scan (the text came from page images). */
  ocr_applied: boolean
  /** Why it was skipped: a stable code and the reason in plain words. */
  code: string | null
  reason: string | null
}

export interface DiscoveryPortal {
  run_id: string
  economy: string
  name: string
  hosts: string[]
  strategy: string
  refresh: boolean
}

/** The four things a Discovery does, in order. Read and Add happen together,
 *  per file, once every file is fetched. */
export type DiscoveryPhase = 'find' | 'fetch' | 'read' | 'add'
export type PhaseState = 'waiting' | 'working' | 'done' | 'nothing'

export interface DiscoveryViewState {
  events: DiscoveryEvent[]
  portal: DiscoveryPortal | null
  status: 'idle' | 'running' | 'finished' | 'failed'
  documents: DiscoveryDocument[]
  /** Live while it runs; the Discovery's own numbers once it has finished. */
  counts: { found: number; fetched: number; added: number; skipped: number; scans: number }
  phases: Record<DiscoveryPhase, PhaseState>
  /** Seconds between two requests to the Portal, once Discovery reports it. */
  spacing_seconds: number | null
  started_at: string | null
  ended_at: string | null
  failure: string | null
}

export type DiscoveryViewAction =
  | { type: 'event'; event: DiscoveryEvent }
  | { type: 'replay'; events: { type: string; seq: number }[] }
  | { type: 'reset' }

const NO_PHASES: Record<DiscoveryPhase, PhaseState> = {
  find: 'waiting',
  fetch: 'waiting',
  read: 'waiting',
  add: 'waiting',
}

export const emptyDiscoveryView: DiscoveryViewState = {
  events: [],
  portal: null,
  status: 'idle',
  documents: [],
  counts: { found: 0, fetched: 0, added: 0, skipped: 0, scans: 0 },
  phases: NO_PHASES,
  spacing_seconds: null,
  started_at: null,
  ended_at: null,
  failure: null,
}

const DISCOVERY_TYPES = new Set<string>([
  'discovery_portal',
  'discovery_found',
  'discovery_fetched',
  'discovery_added',
  'discovery_skipped',
  'discovery_finished',
  'discovery_failed',
])

export function isDiscoveryEvent(e: { type: string }): e is DiscoveryEvent {
  return DISCOVERY_TYPES.has(e.type)
}

function newDocument(url: string, title: string | null): DiscoveryDocument {
  return {
    url,
    title,
    state: 'found',
    document_id: null,
    n_pages: null,
    size_bytes: null,
    ocr_applied: false,
    code: null,
    reason: null,
  }
}

/** Change one Document, adding it first if it was never announced (a file an
 *  earlier Discovery fetched and this one added, say). */
function withDocument(
  docs: DiscoveryDocument[],
  url: string,
  change: (d: DiscoveryDocument) => DiscoveryDocument,
): DiscoveryDocument[] {
  const known = docs.some((d) => d.url === url)
  const all = known ? docs : [...docs, newDocument(url, null)]
  return all.map((d) => (d.url === url ? change(d) : d))
}

function liveCounts(docs: DiscoveryDocument[], fetched: number) {
  return {
    found: docs.length,
    fetched,
    added: docs.filter((d) => d.state === 'added').length,
    skipped: docs.filter((d) => d.state === 'skipped').length,
    scans: docs.filter((d) => d.state === 'added' && d.ocr_applied).length,
  }
}

function phasesOf(state: DiscoveryViewState): Record<DiscoveryPhase, PhaseState> {
  if (state.status === 'idle') return NO_PHASES
  const has = (t: DiscoveryEvent['type']) => state.events.some((e) => e.type === t)
  const finished = state.status === 'finished'
  const running = state.status === 'running'
  const anyFetched = has('discovery_fetched')
  const anyAdded = has('discovery_added')
  // Found events come in one batch once the Portal's listing is read; any
  // Document on screen means the listing is done.
  const listed = state.documents.length > 0
  const find: PhaseState = listed || finished ? 'done' : running ? 'working' : 'waiting'
  const fetch: PhaseState = finished
    ? anyFetched ? 'done' : 'nothing'
    : anyAdded ? 'done' : listed && running ? 'working' : 'waiting'
  // Each file is read and added to the Corpus in one pass.
  const read: PhaseState = finished
    ? anyAdded ? 'done' : 'nothing'
    : anyAdded && running ? 'working' : 'waiting'
  return { find, fetch, read, add: read }
}

/** One event applied to the picture, assuming every earlier event already is. */
function apply(state: DiscoveryViewState, e: DiscoveryEvent): DiscoveryViewState {
  let next: DiscoveryViewState
  switch (e.type) {
    case 'discovery_portal':
      next = {
        ...state,
        portal: {
          run_id: e.run_id,
          economy: e.economy,
          name: e.name,
          hosts: e.hosts,
          strategy: e.strategy,
          refresh: e.refresh,
        },
        status: 'running',
        started_at: e.ts,
      }
      break
    case 'discovery_found':
      next = state.documents.some((d) => d.url === e.url)
        ? state
        : { ...state, documents: [...state.documents, newDocument(e.url, e.name)] }
      break
    case 'discovery_fetched':
      next = {
        ...state,
        documents: withDocument(state.documents, e.url, (d) => ({
          ...d,
          state: d.state === 'found' ? 'fetched' : d.state,
          size_bytes: e.size_bytes,
        })),
      }
      break
    case 'discovery_added':
      next = {
        ...state,
        documents: withDocument(state.documents, e.url, (d) => ({
          ...d,
          state: 'added',
          title: e.title || d.title,
          document_id: e.document_id,
          n_pages: e.n_pages,
          ocr_applied: e.ocr_applied,
        })),
      }
      break
    case 'discovery_skipped':
      next = {
        ...state,
        documents: withDocument(state.documents, e.url, (d) => ({
          ...d,
          state: 'skipped',
          code: e.code,
          reason: e.reason,
          // Already in the Corpus: its Corpus title, not the Portal's file name.
          title: e.title || d.title,
        })),
      }
      break
    case 'discovery_finished':
      next = {
        ...state,
        status: 'finished',
        ended_at: e.ts,
        spacing_seconds: e.counts.spacing_seconds ?? state.spacing_seconds,
      }
      break
    case 'discovery_failed':
      next = { ...state, status: 'failed', ended_at: e.ts, failure: e.message }
      break
    default:
      return state
  }
  const fetched = next.events.filter((x) => x.type === 'discovery_fetched').length
  const live = liveCounts(next.documents, fetched)
  const counts = e.type === 'discovery_finished' ? finalCounts(live, e.counts) : live
  const withCounts = { ...next, counts }
  return { ...withCounts, phases: phasesOf(withCounts) }
}

function finalCounts(live: DiscoveryViewState['counts'], c: Partial<DiscoveryCounts>) {
  return {
    found: c.found ?? live.found,
    fetched: c.fetched ?? live.fetched,
    added: c.added ?? live.added,
    skipped: c.skipped ?? live.skipped,
    scans: live.scans,
  }
}

function rebuild(events: DiscoveryEvent[]): DiscoveryViewState {
  let state: DiscoveryViewState = { ...emptyDiscoveryView, events: [] }
  for (const e of events) state = apply({ ...state, events: state.events.concat(e) }, e)
  return state
}

function seqIndex(events: DiscoveryEvent[], seq: number): { index: number; found: boolean } {
  let lo = 0
  let hi = events.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (events[mid].seq < seq) lo = mid + 1
    else hi = mid
  }
  return { index: lo, found: lo < events.length && events[lo].seq === seq }
}

function receive(state: DiscoveryViewState, event: DiscoveryEvent): DiscoveryViewState {
  // A new Discovery's first event: whatever was on screen belonged to the last one.
  if (event.type === 'discovery_portal' && state.portal !== null && state.portal.run_id !== event.run_id) {
    return receive(emptyDiscoveryView, event)
  }
  const events = state.events
  const last = events.length ? events[events.length - 1].seq : -1
  if (event.seq > last) return apply({ ...state, events: events.concat(event) }, event)
  const { index, found } = seqIndex(events, event.seq)
  if (found) return state
  return rebuild([...events.slice(0, index), event, ...events.slice(index)])
}

/** A whole stream at once (a page reload): only Discovery's events, sorted
 *  and deduplicated once, from the last Discovery's first event. */
function replay(incoming: { type: string; seq: number }[]): DiscoveryViewState {
  const bySeq = new Map<number, DiscoveryEvent>()
  let runId: string | undefined
  let fromSeq = -Infinity
  const mine = incoming.filter(isDiscoveryEvent).sort((a, b) => a.seq - b.seq)
  for (const e of mine) {
    if (e.type === 'discovery_portal') {
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

export function discoveryViewReducer(
  state: DiscoveryViewState,
  action: DiscoveryViewAction,
): DiscoveryViewState {
  switch (action.type) {
    case 'reset':
      return emptyDiscoveryView
    case 'replay':
      return replay(action.events)
    case 'event':
      return isDiscoveryEvent(action.event) ? receive(state, action.event) : state
    default:
      return state
  }
}
