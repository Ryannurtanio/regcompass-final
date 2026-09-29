// What the Run view shows when you click deeper: one Document's Steps and its
// Mappings so far, one Candidate's Piece and why the Gate kept it, and one
// Mapping on its page. Pure functions over the reducer's state, so every
// sentence is tested without a browser (drillDown.test.ts).
//
// Plain words first: "how close its meaning is to the Pillar" and "keyword
// match for the Indicator" are what the screen says; the raw scores may
// follow, smaller, for someone who wants the number.

import { STATIONS, STATION_LABEL, duration, num, stations, type Station, type StationState } from './runOverview'
import type { CandidateView, DocumentView, MappingRef, RunViewState } from './runViewState'
import type { CandidateOutcome, RunStep } from './types'

const plural = (n: number, one: string, many = `${one}s`) => `${num(n)} ${n === 1 ? one : many}`

const count = (d: DocumentView, step: RunStep, key: string): number | null => {
  const v = d.counts[step]?.[key]
  return typeof v === 'number' ? v : null
}

// ---------------------------------------------------------------------------
// What is open
// ---------------------------------------------------------------------------

/** The layer on top of the overview, or null for the overview itself. */
export type Drill =
  | { kind: 'document'; document_id: string }
  | { kind: 'candidate'; document_id: string; piece_id: string; indicator: string }
  | { kind: 'mapping'; document_id: string; mapping_id: string }

/** Whether this Run recorded the Gate's scores per Candidate for this
 *  Document. A Run rebuilt from an older record says it did not; a Run
 *  recorded before candidate events existed shows it by a Map that read
 *  Candidates and settled none of them. */
export function gateScoresRecorded(state: RunViewState, d: DocumentView): boolean {
  if ((state.run?.notRecorded ?? []).includes('gate_scores')) return false
  const read = count(d, 'map', 'total') ?? 0
  return !(d.finished.includes('map') && read > 0 && d.candidates.length === 0)
}

// ---------------------------------------------------------------------------
// One Document's Steps
// ---------------------------------------------------------------------------

export interface StepRow {
  station: Station
  label: string
  /** What the Step does, in one short phrase. */
  what: string
  /** What it did for this Document, or null when it has not run. */
  result: string | null
  /** How long it took, or null when not timed. */
  took: string | null
  state: StationState
  /** 0 to 1 for the small bar under Gate (kept share) and Map (mapped share). */
  bar: number | null
}

const WHAT: Record<Station, string> = {
  read: 'Takes the text out of the file.',
  scan_check: 'Checks the text is readable; scanned pages get OCR.',
  cut: 'Splits the text at its numbered sections.',
  gate: 'Keeps only the sections that look relevant to an Indicator.',
  map_prove: 'The Engine picks a quote or says there is none; each quote is checked word for word.',
  gloss: 'Adds an English gloss to quotes in other languages.',
}

function stationTook(d: DocumentView, s: Station): number | null {
  if (s === 'map_prove') {
    const start = d.times.map?.start ?? d.times.prove?.start
    const end = d.times.prove?.end ?? null
    return start !== undefined && end !== null ? end - start : null
  }
  const t = d.times[s]
  return t && t.end !== null ? t.end - t.start : null
}

function resultOf(d: DocumentView, s: Station, state: StationState): string | null {
  if (state === 'waiting') return null
  switch (s) {
    case 'read': {
      const pages = count(d, 'read', 'pages')
      const chars = count(d, 'read', 'chars')
      if (pages === null && chars === null) return state === 'active' ? 'Reading.' : null
      return [pages !== null ? plural(pages, 'page') : null, chars !== null ? `${num(chars)} characters of text` : null]
        .filter(Boolean)
        .join(', ') + '.'
    }
    case 'scan_check':
      if (state === 'active') return 'Checking.'
      if (d.flag) return `Flagged: ${d.flag}. The Run still mapped it; check its quotes against the page.`
      return count(d, 'scan_check', 'ocr_applied') ? 'Scanned pages, read by OCR.' : 'Readable text. No OCR needed.'
    case 'cut': {
      const pieces = count(d, 'cut', 'pieces')
      if (pieces === null) return state === 'active' ? 'Cutting.' : null
      return pieces <= 1
        ? 'One section: no numbered headings were found, so the text was not split.'
        : `${plural(pieces, 'section')}, split at numbered headings.`
    }
    case 'gate': {
      const pairs = count(d, 'gate', 'pairs')
      const kept = count(d, 'gate', 'candidates')
      if (kept === null) return state === 'active' ? 'Checking every section against every Indicator.' : null
      return `Kept ${num(kept)} of ${plural(pairs ?? kept, 'section and Indicator pair')}. These are the Candidates; the other ${num(Math.max(0, (pairs ?? kept) - kept))} were set aside.`
    }
    case 'map_prove': {
      const proven = count(d, 'prove', 'proven')
      if (proven === null) {
        if (d.mapTotal > 0) return `${num(d.mapDone)} of ${plural(d.mapTotal, 'Candidate')} read by the Engine so far.`
        return state === 'active' ? 'Starting.' : null
      }
      const none = count(d, 'prove', 'no_evidence') ?? 0
      const dropped = count(d, 'prove', 'dropped') ?? 0
      const total = count(d, 'map', 'total') ?? proven + none + dropped
      const tail = dropped > 0 ? `${plural(dropped, 'quote')} dropped by the proof check.` : 'No quote dropped.'
      return `${plural(total, 'Candidate')} read. ${num(proven)} gave a proven Mapping, ${num(none)} not applicable. ${tail}`
    }
    case 'gloss': {
      const g = count(d, 'gloss', 'glossed')
      if (g === null) return state === 'active' ? 'Glossing.' : null
      return g === 0 ? 'Nothing to gloss: every quote is in English, or there were none.' : `${plural(g, 'quote')} glossed in English.`
    }
  }
}

function barOf(d: DocumentView, s: Station): number | null {
  if (s === 'gate') {
    const pairs = count(d, 'gate', 'pairs')
    const kept = count(d, 'gate', 'candidates')
    return pairs && kept !== null ? kept / pairs : null
  }
  if (s === 'map_prove') {
    const proven = count(d, 'prove', 'proven')
    const total = count(d, 'map', 'total')
    if (proven !== null && total) return proven / total
    return d.mapTotal > 0 ? d.mapDone / d.mapTotal : null
  }
  return null
}

/** One Document's route through its Steps, as rows for the Steps panel. */
export function stepRows(d: DocumentView): StepRow[] {
  const states = stations(d)
  return STATIONS.map((s, i) => {
    const took = stationTook(d, s)
    return {
      station: s,
      label: STATION_LABEL[s],
      what: WHAT[s],
      result: resultOf(d, s, states[i]),
      took: states[i] === 'skipped' ? 'skipped' : took === null ? null : duration(took),
      state: states[i],
      bar: barOf(d, s),
    }
  })
}

// ---------------------------------------------------------------------------
// Candidates
// ---------------------------------------------------------------------------

export const OUTCOME_LABEL: Record<CandidateOutcome, string> = {
  mapped: 'Mapping returned',
  not_applicable: 'Not applicable',
  dropped_by_proof: 'Dropped by the proof check',
  skipped: 'Skipped',
}

export const OUTCOME_SENTENCE: Record<CandidateOutcome, string> = {
  mapped: 'The Engine picked a quote that answers the Indicator, and the quote was found word for word in the Document.',
  not_applicable: 'The Engine read the section and said it does not answer this Indicator.',
  dropped_by_proof:
    'The Engine picked a quote, but it could not be found word for word in the Document, so it was not kept.',
  skipped: "The Engine's answer never came back (a network or rate-limit problem), so this Candidate was not read.",
}

export interface CandidateCounts {
  pairs: number | null
  kept: number | null
  /** Pairs the Gate set aside: counts only, never listed. */
  setAside: number | null
  settled: number
  byOutcome: Record<CandidateOutcome, number>
}

export function candidateCounts(d: DocumentView): CandidateCounts {
  const pairs = count(d, 'gate', 'pairs')
  const kept = count(d, 'gate', 'candidates')
  const byOutcome: Record<CandidateOutcome, number> = { mapped: 0, not_applicable: 0, dropped_by_proof: 0, skipped: 0 }
  for (const c of d.candidates) byOutcome[c.outcome] += 1
  return {
    pairs,
    kept,
    setAside: pairs !== null && kept !== null ? Math.max(0, pairs - kept) : null,
    settled: d.candidates.length,
    byOutcome,
  }
}

export function findCandidate(d: DocumentView, pieceId: string, indicator: string): CandidateView | null {
  return d.candidates.find((c) => c.piece_id === pieceId && c.indicator === indicator) ?? null
}

/** The Candidate a Mapping came from: its id is "<piece>::<indicator>". */
export function candidateOfMapping(d: DocumentView, mappingId: string): CandidateView | null {
  return d.candidates.find((c) => c.mapping_id === mappingId) ?? null
}

export function pieceOfMapping(mappingId: string): string {
  const at = mappingId.lastIndexOf('::')
  return at === -1 ? mappingId : mappingId.slice(0, at)
}

export interface Scale {
  value: number
  lowest: number
  highest: number
  /** Where value sits between lowest and highest, 0 to 1. */
  position: number
}

function scale(value: number, all: number[]): Scale {
  const lowest = Math.min(value, ...all)
  const highest = Math.max(value, ...all)
  const position = highest > lowest ? (value - lowest) / (highest - lowest) : 1
  return { value, lowest, highest, position }
}

/** Too narrow a spread to rank one Candidate against the others. */
const flat = (sc: Scale, min: number) => sc.highest - sc.lowest < min

function closeness(sc: Scale, pillar: string): string {
  if (flat(sc, 0.02)) return `Close enough in meaning to ${pillar} to pass`
  if (sc.position >= 0.67) return `Among the closest in meaning to ${pillar}`
  if (sc.position >= 0.33) return `Close in meaning to ${pillar}`
  return `Close enough in meaning to ${pillar} to pass`
}

function keywordMatch(sc: Scale, indicator: string): string {
  if (flat(sc, 0.5)) return `Keyword match for Indicator ${indicator}`
  if (sc.position >= 0.67) return `Strong keyword match for Indicator ${indicator}`
  if (sc.position >= 0.33) return `Good keyword match for Indicator ${indicator}`
  return `Enough keyword match for Indicator ${indicator} to pass`
}

export interface GateReasons {
  /** One sentence: why the Gate kept this Piece for this Indicator. */
  why: string
  meaning: { headline: string; detail: string; scale: Scale }
  /** null in the meaning-only lane, where no keyword check ran. */
  keywords: { headline: string; detail: string; scale: Scale } | null
  /** Said instead of the keyword box in the meaning-only lane. */
  meaningOnlyNote: string | null
}

const two = (n: number) => n.toFixed(2)
const one = (n: number) => (n >= 10 ? n.toFixed(0) : n.toFixed(1))

/** Why the Gate kept a Candidate, in plain words, placed among the other
 *  Candidates the Gate kept from the same Document. */
export function gateReasons(c: CandidateView, d: DocumentView, pillar: number | null): GateReasons {
  const pillarName = pillar !== null ? `Pillar ${pillar}` : 'the Pillar'
  const meaningScale = scale(c.cosine, d.candidates.map((x) => x.cosine))
  const meaning = {
    headline: closeness(meaningScale, pillarName),
    detail:
      meaningScale.highest > meaningScale.lowest
        ? `Similarity ${two(c.cosine)}; the Candidates kept from this Document range from ${two(meaningScale.lowest)} to ${two(meaningScale.highest)}.`
        : `Similarity ${two(c.cosine)}, the same for every Candidate kept from this Document.`,
    scale: meaningScale,
  }
  if (c.lane === 'meaning_only') {
    return {
      why: `The keyword check does not read this Document's language, so the Gate kept this section by meaning alone: it is close enough to ${pillarName} to make the Gate's shortlist for Indicator ${c.indicator}.`,
      meaning,
      keywords: null,
      meaningOnlyNote: "No keyword match: the keyword check reads English only, so it did not run on this Document.",
    }
  }
  const same = d.candidates.filter((x) => x.indicator === c.indicator).map((x) => x.bm25)
  const kScale = scale(c.bm25, same)
  return {
    why: `Its meaning is close enough to ${pillarName}, and its wording shares enough words with Indicator ${c.indicator}'s own vocabulary to make the Gate's shortlist for it.`,
    meaning,
    keywords: {
      headline: keywordMatch(kScale, c.indicator),
      detail:
        kScale.highest > kScale.lowest
          ? `Keyword score ${one(c.bm25)}; the Candidates kept for ${c.indicator} here range from ${one(kScale.lowest)} to ${one(kScale.highest)}.`
          : `Keyword score ${one(c.bm25)}, the same for every Candidate kept for ${c.indicator} here.`,
      scale: kScale,
    },
    meaningOnlyNote: null,
  }
}

// ---------------------------------------------------------------------------
// Mappings so far
// ---------------------------------------------------------------------------

export interface IndicatorGroup<T> {
  indicator: string
  items: T[]
}

/** Items grouped by Indicator, in Indicator order (7.2 before 7.10). */
export function byIndicator<T extends { indicator: string }>(items: T[]): IndicatorGroup<T>[] {
  const groups = new Map<string, T[]>()
  for (const it of items) {
    const g = groups.get(it.indicator)
    if (g) g.push(it)
    else groups.set(it.indicator, [it])
  }
  const key = (s: string) => s.split('.').map((p) => Number(p) || 0)
  return [...groups.entries()]
    .sort(([a], [b]) => {
      const [x, y] = [key(a), key(b)]
      for (let i = 0; i < Math.max(x.length, y.length); i++) {
        if ((x[i] ?? 0) !== (y[i] ?? 0)) return (x[i] ?? 0) - (y[i] ?? 0)
      }
      return 0
    })
    .map(([indicator, list]) => ({ indicator, items: list }))
}

/** The Mappings a Document has so far: from the Run's events, or, for a Run
 *  recorded before they were, from its finished count only. */
export function mappingsSoFar(d: DocumentView): { refs: MappingRef[]; count: number } {
  return { refs: d.mappingsFound, count: Math.max(d.mappingsFound.length, d.mappings ?? 0) }
}

// ---------------------------------------------------------------------------
// The quote inside its Piece
// ---------------------------------------------------------------------------

/** The Piece's text split around the quote, for a highlight: the quote's
 *  first place in the Piece, or the whole text unmarked when it is absent. */
export function markQuote(text: string, quote: string | null): { before: string; quote: string; after: string } | null {
  if (!quote) return null
  const at = text.indexOf(quote)
  if (at === -1) return null
  return { before: text.slice(0, at), quote, after: text.slice(at + quote.length) }
}

// A quote that already opens with a quote mark, or closes with a double one.
// A closing apostrophe is left out: "the members'" ends a quote without being
// a quote mark.
const QUOTE_MARKS = /^["'“”‘’«„]|["”»]$/

/** A Verbatim Quote in quote marks for a list, unless its own text already
 *  starts or ends with one. Only the decoration changes: the text itself is
 *  shown exactly as saved. */
export function quoted(text: string): string {
  return QUOTE_MARKS.test(text) ? text : `“${text}”`
}
