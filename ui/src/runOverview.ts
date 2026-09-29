// What the Run view shows, worked out from the reducer's state: each
// Document's state and its row of Step stations, the flow of numbers from
// Documents to Mappings proven, the header's figures and where the time went.
// Pure functions, so every state the screen can be in is tested without a
// browser (runOverview.test.ts).

import type { DocumentView, RunViewState } from './runViewState'
import type { RunStep } from './types'

const NB = ' '

// ---------------------------------------------------------------------------
// A Document's state
// ---------------------------------------------------------------------------

export type DocumentState = 'waiting' | 'working' | 'finished' | 'zero' | 'failed'

export function documentState(d: DocumentView): DocumentState {
  if (d.failed) return 'failed'
  if (d.done) return (d.mappings ?? 0) === 0 ? 'zero' : 'finished'
  if (d.step === null) return 'waiting'
  return 'working'
}

const count = (d: DocumentView, step: RunStep, key: string): number | null => {
  const v = d.counts[step]?.[key]
  return typeof v === 'number' ? v : null
}

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`

/** Why a finished Document has no Mappings, in plain words, or null. */
export function zeroReason(d: DocumentView): string | null {
  if (!d.done || (d.mappings ?? 0) > 0) return null
  const pieces = count(d, 'cut', 'pieces') ?? count(d, 'gate', 'pieces')
  const kept = count(d, 'gate', 'candidates')
  if (kept === 0) {
    if (pieces !== null && pieces <= 1) {
      return 'No numbered headings found, so the whole text was one section and the Gate kept nothing. Check the file is the law text.'
    }
    return pieces !== null
      ? `The Gate kept none of its ${plural(pieces, 'section')} for this Pillar.`
      : 'The Gate kept no Candidates for this Pillar.'
  }
  const noEvidence = count(d, 'prove', 'no_evidence') ?? 0
  const dropped = count(d, 'prove', 'dropped') ?? 0
  const parts: string[] = []
  if (noEvidence > 0) parts.push(`${noEvidence.toLocaleString('en-US')} had no evidence`)
  if (dropped > 0) parts.push(`${plural(dropped, 'quote')} failed the word-for-word check`)
  const head = kept !== null ? `${plural(kept, 'Candidate')}, none proven` : 'None proven'
  return parts.length ? `${head}: ${parts.join(', ')}.` : `${head}.`
}

// ---------------------------------------------------------------------------
// The Step stations on a Document's row
// ---------------------------------------------------------------------------

/** Map and Prove run pair by pair, interleaved, so they are one station and
 *  one timed span. */
export type Station = 'read' | 'scan_check' | 'cut' | 'gate' | 'map_prove' | 'gloss'

export const STATIONS: Station[] = ['read', 'scan_check', 'cut', 'gate', 'map_prove', 'gloss']

export const STATION_LABEL: Record<Station, string> = {
  read: 'Read',
  scan_check: 'Scan check',
  cut: 'Split into sections',
  gate: 'Gate',
  map_prove: 'Map and Prove',
  gloss: 'Gloss',
}

/** Short enough for the column heads over the stations. */
export const STATION_SHORT: Record<Station, string> = {
  read: 'Read',
  scan_check: 'Scan check',
  cut: 'Cut',
  gate: 'Gate',
  map_prove: 'Map and Prove',
  gloss: 'Gloss',
}

export function stationOf(step: RunStep): Station | null {
  if (step === 'map' || step === 'prove') return 'map_prove'
  if (step === 'reconcile') return null
  return step
}

export type StationState = 'done' | 'active' | 'waiting' | 'skipped' | 'flag' | 'zero' | 'fail'

function stationFinished(d: DocumentView, s: Station): boolean {
  if (s === 'map_prove') return d.finished.includes('prove')
  return d.finished.includes(s)
}

export function stations(d: DocumentView): StationState[] {
  const current = d.step === null ? null : stationOf(d.step)
  const currentIndex = current === null ? -1 : STATIONS.indexOf(current)
  const kept = count(d, 'gate', 'candidates')
  const zero = d.done && (d.mappings ?? 0) === 0
  return STATIONS.map((s, i) => {
    if (d.failed) {
      if (i === currentIndex) return 'fail'
      if (i > currentIndex) return 'waiting'
    }
    if (zero && kept === 0) {
      if (s === 'gate') return 'zero'
      if (s === 'map_prove' || s === 'gloss') return 'skipped'
    }
    if (zero && kept !== 0) {
      if (s === 'map_prove') return 'zero'
      if (s === 'gloss') return 'skipped'
    }
    if (stationFinished(d, s)) {
      if (s === 'scan_check' && d.flag) return 'flag'
      // No page needed OCR: the Scan check had nothing to do.
      if (s === 'scan_check' && (count(d, 'scan_check', 'ocr_applied') ?? 0) === 0) return 'skipped'
      if (s === 'gloss' && (count(d, 'gloss', 'glossed') ?? 0) === 0) return 'skipped'
      return 'done'
    }
    if (!d.done && i === currentIndex) return 'active'
    return 'waiting'
  })
}

const STATE_WORD: Record<StationState, string> = {
  done: 'done',
  active: 'working',
  waiting: 'not started',
  skipped: 'nothing to do',
  flag: 'flagged',
  zero: 'nothing kept',
  fail: 'stopped here',
}

/** The row of stations as one sentence, for a screen reader. */
export function stationsLabel(d: DocumentView): string {
  return stations(d)
    .map((st, i) => `${STATION_LABEL[STATIONS[i]]} ${STATE_WORD[st]}`)
    .join(', ')
}

// ---------------------------------------------------------------------------
// The flow: Documents, Pieces, Candidates kept, Mappings proposed, proven
// ---------------------------------------------------------------------------

export interface Flow {
  documents: number
  documentsDone: number
  pieces: number
  pairs: number
  candidates: number
  /** The Engine returned a quote: proven ones plus the ones the proof dropped. */
  proposed: number
  proven: number
  noEvidence: number
  dropped: number
  /** Reconcile's groups, once it has run. */
  groups: number | null
  /** How far the Run has got: which numbers have started to fill in. */
  reached: { pieces: boolean; candidates: boolean; proposed: boolean }
}

export function flow(state: RunViewState): Flow {
  let pieces = 0
  let pairs = 0
  let candidates = 0
  let proven = 0
  let noEvidence = 0
  let dropped = 0
  const reached = { pieces: false, candidates: false, proposed: false }
  for (const d of state.documents) {
    const cut = count(d, 'cut', 'pieces')
    if (cut !== null) {
      pieces += cut
      reached.pieces = true
    }
    const gatePairs = count(d, 'gate', 'pairs')
    const kept = count(d, 'gate', 'candidates')
    if (kept !== null) {
      pairs += gatePairs ?? 0
      candidates += kept
      reached.candidates = true
    }
    const p = count(d, 'prove', 'proven')
    if (p !== null) {
      proven += p
      noEvidence += count(d, 'prove', 'no_evidence') ?? 0
      dropped += count(d, 'prove', 'dropped') ?? 0
      reached.proposed = true
    }
  }
  const t = state.status === 'finished' ? state.totals : null
  // At the end the Run's own totals are the word; they equal the sums.
  if (t) {
    pieces = t.pieces ?? pieces
    pairs = t.pairs ?? pairs
    candidates = t.candidates ?? candidates
    proven = t.proven ?? proven
    noEvidence = t.no_evidence ?? noEvidence
    dropped = t.dropped ?? dropped
  }
  return {
    documents: state.documents.length,
    documentsDone: state.documents.filter((d) => d.done).length,
    pieces,
    pairs,
    candidates,
    proposed: proven + dropped,
    proven,
    noEvidence,
    dropped,
    groups: state.reconciled?.after ?? (t?.groups ?? null),
    reached,
  }
}

/** Candidates the Engine has read so far, and how many the Gate has kept so
 *  far (the total grows as each Document is gated). */
export function candidatesMapped(state: RunViewState): { done: number; kept: number } {
  let done = 0
  let kept = 0
  for (const d of state.documents) {
    const k = count(d, 'gate', 'candidates')
    if (k !== null) kept += k
    done += d.finished.includes('map') ? (count(d, 'map', 'done') ?? d.mapDone) : d.mapDone
  }
  return { done, kept }
}

// ---------------------------------------------------------------------------
// Where the time went
// ---------------------------------------------------------------------------

export type TimedStep = Station | 'reconcile'

export const TIMED_STEPS: TimedStep[] = [...STATIONS, 'reconcile']

export const TIMED_LABEL: Record<TimedStep, string> = { ...STATION_LABEL, reconcile: 'Reconcile' }

export interface Span {
  start: number
  end: number
  /** Still going: its end is "now". */
  open: boolean
}

export interface DocumentTiming {
  document_id: string
  title: string
  state: DocumentState
  span: Span | null
  gate: Span | null
  mapProve: Span | null
  /** Every station this Document reached, timed. */
  steps: Partial<Record<Station, Span>>
  /** The Scan check and Gloss when they had nothing to do for this Document. */
  idle: Station[]
}

export interface Timing {
  /** The Run's times were rebuilt from an older log, not recorded live. */
  estimated: boolean
  start: number
  end: number
  live: boolean
  byStep: { step: TimedStep; ms: number; share: number; reached: boolean }[]
  byDocument: DocumentTiming[]
  reconcile: Span | null
}

/** How long each Step took, from the Step times in the Run's events. `now`
 *  is the clock a live Run's open Steps run up to; a finished Run ignores it. */
export function timing(state: RunViewState, now: number): Timing | null {
  if (state.startedAt === null) return null
  const live = state.status === 'running'
  const end = live ? Math.max(now, state.lastAt ?? now) : (state.endedAt ?? state.lastAt ?? now)
  const clock = live ? end : (state.endedAt ?? state.lastAt ?? end)
  const totals: Record<TimedStep, number> = {
    read: 0,
    scan_check: 0,
    cut: 0,
    gate: 0,
    map_prove: 0,
    gloss: 0,
    reconcile: 0,
  }
  const reached: Record<TimedStep, boolean> = {
    read: false,
    scan_check: false,
    cut: false,
    gate: false,
    map_prove: false,
    gloss: false,
    reconcile: false,
  }
  const byDocument: DocumentTiming[] = state.documents.map((d) => {
    const one = (step: RunStep): Span | null => {
      const t = d.times[step]
      if (!t) return null
      if (t.end !== null) return { start: t.start, end: Math.max(t.start, t.end), open: false }
      return d.failed || !live
        ? { start: t.start, end: Math.max(t.start, clock), open: false }
        : { start: t.start, end: Math.max(t.start, clock), open: true }
    }
    for (const step of ['read', 'scan_check', 'cut', 'gate', 'gloss'] as RunStep[]) {
      const s = one(step)
      if (s) {
        totals[step as TimedStep] += s.end - s.start
        reached[step as TimedStep] = true
      }
    }
    let mapProve: Span | null = null
    const map = one('map')
    if (map) {
      const prove = one('prove')
      mapProve = prove
        ? { start: map.start, end: Math.max(map.start, prove.end), open: prove.open }
        : map.open || d.times.map?.end == null
          ? map
          : // Between Map's end and Prove's start the span is still open.
            live && !d.done
            ? { start: map.start, end: Math.max(map.end, clock), open: true }
            : map
      totals.map_prove += mapProve.end - mapProve.start
      reached.map_prove = true
    }
    const steps: Partial<Record<Station, Span>> = {}
    for (const station of STATIONS) {
      const s = station === 'map_prove' ? mapProve : one(station as RunStep)
      if (s) steps[station] = s
    }
    const idle: Station[] = []
    if (d.finished.includes('scan_check') && (count(d, 'scan_check', 'ocr_applied') ?? 0) === 0) idle.push('scan_check')
    if (d.finished.includes('gloss') && (count(d, 'gloss', 'glossed') ?? 0) === 0) idle.push('gloss')
    const read = d.times.read
    let span: Span | null = null
    if (read) {
      const g = d.times.gloss
      if (g && g.end !== null) span = { start: read.start, end: Math.max(read.start, g.end), open: false }
      else span = { start: read.start, end: Math.max(read.start, clock), open: live && !d.failed }
    }
    return {
      document_id: d.document_id,
      title: d.title,
      state: documentState(d),
      span,
      gate: one('gate'),
      mapProve,
      steps,
      idle,
    }
  })
  let reconcile: Span | null = null
  if (state.reconcileTimes) {
    const r = state.reconcileTimes
    reconcile =
      r.end !== null
        ? { start: r.start, end: Math.max(r.start, r.end), open: false }
        : { start: r.start, end: Math.max(r.start, clock), open: live }
    totals.reconcile = reconcile.end - reconcile.start
    reached.reconcile = true
  }
  const measured = TIMED_STEPS.reduce((sum, s) => sum + totals[s], 0)
  return {
    estimated: state.run?.notRecorded.includes('step_times') ?? false,
    start: state.startedAt,
    end: Math.max(state.startedAt, end),
    live,
    byStep: TIMED_STEPS.map((step) => ({
      step,
      ms: totals[step],
      share: measured > 0 ? totals[step] / measured : 0,
      reached: reached[step],
    })),
    byDocument,
    reconcile,
  }
}

/** One Step of a Document's time, as the breakdown shows it. */
export interface StepPart {
  station: Station
  label: string
  /** 'timed': ms is the Step's time. 'waiting': not reached yet. 'not_recorded':
   *  the Run's record never kept this Step's time, so there is no number. */
  status: 'timed' | 'waiting' | 'not_recorded'
  ms: number
  /** Its share of the Document's time, 0 to 1, for the mini bar. */
  share: number
  open: boolean
}

export interface DocumentBreakdown {
  document_id: string
  title: string
  /** From the start of Read to now or the end of Gloss; null before Read. */
  totalMs: number | null
  open: boolean
  /** False for a Run rebuilt from its log: only the Gate and Map and Prove
   *  times it gave are shown, as estimates. */
  recorded: boolean
  parts: StepPart[]
}

// The Steps a Run rebuilt from its log still has estimates for: the ones the
// chart draws. The rest were made up to keep the order, so they get no number.
const ESTIMATED: Station[] = ['gate', 'map_prove']

/** One Document's time, Step by Step. Read, Cut into Pieces, Gate and Map and
 *  Prove always have a line; the Scan check and Gloss only when they did work
 *  for this Document. */
export function documentBreakdown(d: DocumentTiming, estimated: boolean): DocumentBreakdown {
  const totalMs = d.span ? d.span.end - d.span.start : null
  const parts: StepPart[] = STATIONS.filter(
    (s) => !((s === 'scan_check' || s === 'gloss') && (d.idle.includes(s) || !d.steps[s])),
  ).map((station) => {
    const span = d.steps[station]
    const label = STATION_LABEL[station]
    if (!span) return { station, label, status: 'waiting', ms: 0, share: 0, open: false }
    if (estimated && !ESTIMATED.includes(station)) {
      return { station, label, status: 'not_recorded', ms: 0, share: 0, open: false }
    }
    const ms = span.end - span.start
    return { station, label, status: 'timed', ms, share: totalMs ? Math.min(1, ms / totalMs) : 0, open: span.open }
  })
  return {
    document_id: d.document_id,
    title: d.title,
    totalMs,
    open: d.span?.open ?? false,
    recorded: !estimated,
    parts,
  }
}

// ---------------------------------------------------------------------------
// Words and numbers as the screen prints them
// ---------------------------------------------------------------------------

/** A duration in plain words: "under 0.01 s", "0.50 s", "16.4 s", "4 min 13 s". */
export function duration(ms: number): string {
  const s = ms / 1000
  if (s < 0.01) return `under${NB}0.01${NB}s`
  if (s < 1) return `${s.toFixed(2)}${NB}s`
  if (s < 60) return `${s.toFixed(1)}${NB}s`
  const whole = Math.round(s)
  const m = Math.floor(whole / 60)
  if (m < 60) return `${m}${NB}min ${String(whole % 60).padStart(2, '0')}${NB}s`
  return `${Math.floor(m / 60)}${NB}h ${String(m % 60).padStart(2, '0')}${NB}min`
}

/** The Steps no Document needed: the Scan check when no page was read by
 *  OCR, Gloss when no quote needed an English rendering. Their time is
 *  bookkeeping, not work, so the screen says "skipped" instead. */
export function stepsWithNoWork(state: RunViewState): Set<TimedStep> {
  const out = new Set<TimedStep>()
  const docs = state.documents.filter((d) => d.finished.length > 0)
  if (docs.length === 0) return out
  const scanned = docs.filter((d) => d.finished.includes('scan_check'))
  if (scanned.length && scanned.every((d) => (count(d, 'scan_check', 'ocr_applied') ?? 0) === 0)) out.add('scan_check')
  const glossed = docs.filter((d) => d.finished.includes('gloss'))
  if (glossed.length && glossed.every((d) => (count(d, 'gloss', 'glossed') ?? 0) === 0)) out.add('gloss')
  return out
}

/** A running clock: "45 s", "12 min 05 s". */
export function elapsed(ms: number): string {
  const whole = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(whole / 60)
  if (m === 0) return `${whole}${NB}s`
  if (m < 60) return `${m}${NB}min ${String(whole % 60).padStart(2, '0')}${NB}s`
  return `${Math.floor(m / 60)}${NB}h ${String(m % 60).padStart(2, '0')}${NB}min`
}

/** A time of day in UTC, "04:12 UTC". */
export function utcClock(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}${NB}UTC`
}

export function money(x: number | null): string {
  if (x === null) return 'not recorded'
  if (x === 0) return `USD${NB}0.00`
  if (x < 0.01) return `under USD${NB}0.01`
  return `USD${NB}${x.toFixed(2)}`
}

export const num = (n: number) => n.toLocaleString('en-US')

/** A failure in plain words: which Document, which Step. The exception
 *  itself stays in the raw log and behind "Show the technical message". */
export function failureSentence(state: RunViewState): string | null {
  const f = state.failure
  if (!f) return null
  const doc = f.document_id ? state.documents.find((d) => d.document_id === f.document_id) : null
  const step = f.step ? stationOf(f.step) : null
  const stepName = f.step === 'reconcile' ? 'Reconcile' : step ? STATION_LABEL[step] : null
  if (doc && stepName) return `The Run stopped at ${stepName} on ${doc.title}. Nothing after that point was run.`
  if (doc) return `The Run stopped on ${doc.title}. Nothing after that point was run.`
  if (stepName) return `The Run stopped at ${stepName}. Nothing after that point was run.`
  return 'The Run stopped before its first Step, so no Document was read.'
}

/** The technical message without its leading exception class name. */
export function technicalMessage(message: string): string {
  return message.replace(/^[A-Za-z_][\w.]*(Error|Exception|Warning|Interrupt|Exit)\s*:\s*/, '')
}
