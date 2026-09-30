// The Discovery view's state: Discovery's typed events in, the picture on
// screen out.
//
// One pure reducer, the same shape as the Run view's (runViewState.ts): the
// live stream and a page reload (the stream replays every event from the
// first) feed it the same events, so the screen has one code path. Every event
// is kept once, in seq order; one that arrives out of order is slotted into
// place and the picture rebuilt, so the result never depends on arrival order.
// Events of any other kind (a Run's) are ignored.

import type { BaselineSkip, DiscoveryCounts, DiscoveryEvent } from './types'

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
  /** A Discovery by Pillar: why it came in ("baseline 6.1", "portal crawler"). */
  found_by: string | null
  /** A Discovery by Pillar: every Indicator it was found for, from all of its
   *  found_by entries together, in order ("6.1", "6.4"). */
  indicators: string[]
}

/** What a Discovery by Pillar was asked for, and what it left for the
 *  operator to upload by hand. */
export interface DiscoveryDraw {
  pillar: number
  indicators: string[] | null
  max_documents: number | null
  baseline_skipped: BaselineSkip[]
  notes: string[]
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
  /** Live while it runs; the Discovery's own numbers once it has finished.
   *  skipped counts the laws not added, not the ones already in the Corpus. */
  counts: { found: number; fetched: number; added: number; skipped: number; scans: number }
  phases: Record<DiscoveryPhase, PhaseState>
  /** Seconds between two requests to the Portal, once Discovery reports it. */
  spacing_seconds: number | null
  started_at: string | null
  ended_at: string | null
  failure: string | null
  /** Set once a Discovery by Pillar finishes; null for any other. */
  drawn: DiscoveryDraw | null
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
  drawn: null,
}

/** The draw a finished Discovery's counts carry, or null when it had none. */
export function drawOf(counts: Partial<DiscoveryCounts>): DiscoveryDraw | null {
  if (typeof counts.pillar !== 'number') return null
  return {
    pillar: counts.pillar,
    indicators: counts.indicators ?? null,
    max_documents: counts.max_documents ?? null,
    baseline_skipped: counts.baseline_skipped ?? [],
    notes: counts.notes ?? [],
  }
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
    found_by: null,
    indicators: [],
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

/** In the Corpus from an earlier Discovery: found, not left out. */
export function inCorpus(d: DiscoveryDocument): boolean {
  return d.state === 'skipped' && d.code === 'in_corpus'
}

function notAdded(d: DiscoveryDocument): boolean {
  return d.state === 'skipped' && !inCorpus(d)
}

function liveCounts(docs: DiscoveryDocument[], fetched: number) {
  return {
    found: docs.length,
    fetched,
    added: docs.filter((d) => d.state === 'added').length,
    skipped: docs.filter((d) => notAdded(d)).length,
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
        // A law this Discovery already added stays added, whatever a later
        // skip says (asked for again under another Indicator, say), as the log.
        documents: withDocument(state.documents, e.url, (d) => d.state === 'added' ? d : ({
          ...d,
          state: 'skipped',
          code: e.code,
          reason: e.reason,
          // Already in the Corpus: its Corpus title, not the Portal's file name.
          title: e.title || d.title,
        })),
      }
      break
    case 'discovery_finished': {
      // One law can come in for several Indicators, one entry each: every
      // entry for the same address counts, not only the last.
      const why = new Map<string, string[]>()
      for (const f of e.counts.found_by ?? []) why.set(f.url, [...(why.get(f.url) ?? []), f.found_by])
      next = {
        ...state,
        status: 'finished',
        ended_at: e.ts,
        spacing_seconds: e.counts.spacing_seconds ?? state.spacing_seconds,
        drawn: drawOf(e.counts),
        documents: why.size
          ? state.documents.map((d) => {
              const entries = why.get(d.url)
              if (!entries) return d
              return {
                ...d,
                found_by: [...new Set(entries)].join('; '),
                indicators: sortIds(entries.flatMap(indicatorIdsIn)),
              }
            })
          : state.documents,
      }
      break
    }
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

/** The Discovery's own numbers, except where the Documents on screen say
 *  otherwise: its skipped count also counts a law it added and was then asked
 *  for again under another Indicator, which the list shows once, as added. */
function finalCounts(live: DiscoveryViewState['counts'], c: Partial<DiscoveryCounts>) {
  const listed = live.found > 0
  return {
    found: listed ? live.found : (c.found ?? live.found),
    fetched: c.fetched ?? live.fetched,
    added: listed ? live.added : (c.added ?? live.added),
    skipped: listed ? live.skipped : (c.skipped ?? live.skipped),
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

// ---------------------------------------------------------------------------
// What the view draws from the state: the log while Discovery runs, and the
// Documents against the drawn Pillar's Indicators once it has finished.
// ---------------------------------------------------------------------------

/** The Indicator ids a found_by entry names ("baseline 7.2, 7.3" gives
 *  7.2 and 7.3; "portal crawler" gives none). */
export function indicatorIdsIn(text: string): string[] {
  return text.match(/\d+\.\d+/g) ?? []
}

/** Unique ids in Indicator order: 7.2 before 7.10. */
export function sortIds(ids: string[]): string[] {
  const part = (id: string) => id.split('.').map(Number)
  return [...new Set(ids)].sort((a, b) => {
    const [a1, a2] = part(a)
    const [b1, b2] = part(b)
    return a1 - b1 || a2 - b2
  })
}

/** The site a Document came from, without the rest of its address. */
export function siteOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return ''
  }
}

/** The last part of an address, for a Document that has no title yet. */
export function shortName(url: string): string {
  try {
    const u = new URL(url)
    const last = u.pathname.split('/').filter(Boolean).pop()
    return decodeURIComponent(last ?? u.host)
  } catch {
    return url
  }
}

function laws(n: number): string {
  return n === 1 ? '1 law' : `${n} laws`
}

export type LogKind = 'access' | 'found' | 'added' | 'corpus' | 'left' | 'done' | 'stop'

export interface LogLine {
  key: number
  kind: LogKind
  tag: string
  text: string
  /** The site, or the running count of Documents added. */
  right: string
}

/** The Discovery log: one line per law found, added or left out, in the
 *  order it happened, closed by a line once Discovery ends. */
export function discoveryLog(state: DiscoveryViewState): LogLine[] {
  const name = state.portal?.name ?? ''
  const lines: LogLine[] = []
  const titles = new Map<string, string | null>()
  const added = new Set<string>()
  for (const e of state.events) {
    const key = e.seq
    switch (e.type) {
      case 'discovery_portal':
        lines.push({ key, kind: 'access', tag: '>', text: `Accessing the official legal portals for ${e.name}…`, right: '' })
        break
      case 'discovery_found':
        titles.set(e.url, e.name)
        lines.push({ key, kind: 'found', tag: 'found', text: e.name || shortName(e.url), right: siteOf(e.url) })
        break
      case 'discovery_added':
        if (added.has(e.url)) break
        added.add(e.url)
        lines.push({ key, kind: 'added', tag: 'added', text: 'to the Corpus', right: `Added ${added.size}` })
        break
      case 'discovery_skipped': {
        // Asked for again under another Indicator: the law is already on screen.
        if (added.has(e.url)) break
        const title = e.title || titles.get(e.url) || shortName(e.url)
        lines.push(
          e.code === 'in_corpus'
            ? { key, kind: 'corpus', tag: 'in Corpus', text: `${title}, already in the Corpus`, right: siteOf(e.url) }
            : { key, kind: 'left', tag: 'not added', text: `${title}, not added`, right: siteOf(e.url) },
        )
        break
      }
      case 'discovery_finished': {
        const pillar = state.drawn ? `, Pillar ${state.drawn.pillar}` : ''
        lines.push({ key, kind: 'done', tag: 'done', text: `${laws(state.counts.added)} added to the Corpus for ${name}${pillar}.`, right: '' })
        break
      }
      case 'discovery_failed':
        lines.push({ key, kind: 'stop', tag: 'stop', text: 'Discovery stopped.', right: '' })
        break
      default:
        break
    }
  }
  return lines
}

export interface PillarRow {
  url: string
  title: string
  site: string
  added: boolean
  /** Already in the Corpus from an earlier Discovery. */
  inCorpus: boolean
  /** The drawn Pillar's Indicators this Document was found for. */
  ids: string[]
}

export interface PillarPanel {
  pillar: number
  /** One column per Indicator: the ones asked for, else every one found. */
  columns: string[]
  rows: PillarRow[]
}

/** The finished Discovery's Documents against the drawn Pillar's Indicators,
 *  or null for a Discovery with no Pillar. */
export function pillarPanel(state: DiscoveryViewState): PillarPanel | null {
  if (!state.drawn) return null
  const pillar = state.drawn.pillar
  const mine = (id: string) => id.startsWith(`${pillar}.`)
  const rows = state.documents.map((d) => ({
    url: d.url,
    title: d.title ?? shortName(d.url),
    site: siteOf(d.url),
    added: d.state === 'added',
    inCorpus: inCorpus(d),
    ids: d.indicators.filter(mine),
  }))
  const asked = (state.drawn.indicators ?? []).filter(mine)
  const columns = sortIds(asked.length ? asked : rows.flatMap((r) => r.ids))
  return { pillar, columns, rows }
}

/** The reveal's pace: a first beat, then one Document at a time, the whole of
 *  it inside about six seconds however many there are. */
export const REVEAL_FIRST_MS = 700
export function revealStepMs(total: number): number {
  return total <= 1 ? 0 : Math.min(1000, Math.floor(5200 / total))
}

/** Reveals `total` rows one by one through `show`, up to `limit`, then the
 *  rest at once. Returns the function that stops it. */
export function startReveal(total: number, limit: number, show: (n: number) => void): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null
  const paced = Math.min(total, limit)
  const step = revealStepMs(paced)
  const next = (n: number) => {
    show(n >= paced ? total : n)
    timer = n < paced ? setTimeout(() => next(n + 1), step) : null
  }
  show(0)
  if (total > 0) timer = setTimeout(() => next(1), REVEAL_FIRST_MS)
  return () => {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }
}
