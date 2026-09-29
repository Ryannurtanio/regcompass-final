import { describe, expect, it } from 'vitest'
import recorded from '../../tests/fixtures/run_events/run_20260923T041241Z_a07249.jsonl?raw'
import {
  OUTCOME_LABEL,
  OUTCOME_SENTENCE,
  byIndicator,
  candidateCounts,
  candidateOfMapping,
  gateReasons,
  gateScoresRecorded,
  markQuote,
  pieceOfMapping,
  quoted,
  stepRows,
} from './drillDown'
import { emptyRunView, runViewReducer, type RunViewState } from './runViewState'
import type { CandidateOutcome, RunEvent } from './types'

type Draft = RunEvent extends infer E ? (E extends RunEvent ? Omit<E, 'seq' | 'ts'> : never) : never

function numbered(drafts: Draft[]): RunEvent[] {
  return drafts.map(
    (d, i) => ({ ...d, seq: i, ts: `2026-09-28T10:${String(Math.floor(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}+00:00` }) as RunEvent,
  )
}

function feed(events: RunEvent[], state: RunViewState = emptyRunView): RunViewState {
  return events.reduce((s, event) => runViewReducer(s, { type: 'event', event }), state)
}

const cand = (piece: string, indicator: string, cosine: number, bm25: number, outcome: CandidateOutcome): Draft => ({
  type: 'candidate',
  document_id: 'doc_a',
  piece_id: piece,
  indicator,
  cosine,
  bm25,
  lane: 'meaning_and_keywords',
  outcome,
  section: `s. ${piece.split(':')[1]}`,
  page: 3,
})

function drillRun(): RunEvent[] {
  return numbered([
    {
      type: 'run_started',
      run_id: 'run_a',
      economy: 'SG',
      pillars: [7],
      indicators: null,
      engine: 'fake',
      documents: [{ document_id: 'doc_a', title: 'Act A', language: 'en', n_pages: 9 }],
    },
    { type: 'step_started', document_id: 'doc_a', step: 'read' },
    { type: 'step_finished', document_id: 'doc_a', step: 'read', counts: { pages: 9, chars: 5000 } },
    { type: 'step_started', document_id: 'doc_a', step: 'scan_check' },
    { type: 'step_finished', document_id: 'doc_a', step: 'scan_check', counts: { ocr_applied: 0 } },
    { type: 'step_started', document_id: 'doc_a', step: 'cut' },
    { type: 'step_finished', document_id: 'doc_a', step: 'cut', counts: { pieces: 10 } },
    { type: 'step_started', document_id: 'doc_a', step: 'gate' },
    { type: 'step_finished', document_id: 'doc_a', step: 'gate', counts: { pieces: 10, pairs: 50, candidates: 4 } },
    { type: 'step_started', document_id: 'doc_a', step: 'map' },
    cand('doc_a:1', '7.4', 0.6, 12, 'mapped'),
    cand('doc_a:2', '7.4', 0.5, 4, 'not_applicable'),
    cand('doc_a:3', '7.2', 0.7, 8, 'dropped_by_proof'),
    cand('doc_a:4', '7.10', 0.45, 3, 'mapped'),
    { type: 'map_progress', document_id: 'doc_a', done: 4, total: 4 },
    { type: 'step_finished', document_id: 'doc_a', step: 'map', counts: { done: 4, total: 4 } },
    { type: 'step_started', document_id: 'doc_a', step: 'prove' },
    { type: 'mapping_added', document_id: 'doc_a', mapping_id: 'doc_a:1::7.4', indicator: '7.4', page: 3 },
    { type: 'mapping_added', document_id: 'doc_a', mapping_id: 'doc_a:4::7.10', indicator: '7.10', page: 5 },
    { type: 'step_finished', document_id: 'doc_a', step: 'prove', counts: { proven: 2, no_evidence: 1, dropped: 1 } },
    { type: 'step_started', document_id: 'doc_a', step: 'gloss' },
    { type: 'step_finished', document_id: 'doc_a', step: 'gloss', counts: { glossed: 0 } },
    { type: 'document_finished', document_id: 'doc_a', mappings: 2 },
  ])
}

describe('the reducer keeps each Candidate and Mapping', () => {
  it('lists Candidates in order with their scores, lane and outcome', () => {
    const d = feed(drillRun()).documents[0]
    expect(d.candidates.map((c) => [c.piece_id, c.indicator, c.outcome])).toEqual([
      ['doc_a:1', '7.4', 'mapped'],
      ['doc_a:2', '7.4', 'not_applicable'],
      ['doc_a:3', '7.2', 'dropped_by_proof'],
      ['doc_a:4', '7.10', 'mapped'],
    ])
    expect(d.candidates[0]).toMatchObject({ cosine: 0.6, bm25: 12, lane: 'meaning_and_keywords', section: 's. 1', page: 3 })
  })

  it('links a mapped Candidate to its Mapping, and no other', () => {
    const d = feed(drillRun()).documents[0]
    expect(d.candidates.map((c) => c.mapping_id)).toEqual(['doc_a:1::7.4', null, null, 'doc_a:4::7.10'])
    expect(candidateOfMapping(d, 'doc_a:4::7.10')?.piece_id).toBe('doc_a:4')
    expect(pieceOfMapping('doc_a:4::7.10')).toBe('doc_a:4')
  })

  it('lists the Mappings so far as they are saved, with their page', () => {
    const events = drillRun()
    const firstAdded = events.findIndex((e) => e.type === 'mapping_added')
    expect(feed(events.slice(0, firstAdded)).documents[0].mappingsFound).toEqual([])
    expect(feed(events.slice(0, firstAdded + 1)).documents[0].mappingsFound).toEqual([
      { mapping_id: 'doc_a:1::7.4', indicator: '7.4', page: 3 },
    ])
    expect(feed(events).documents[0].mappingsFound).toHaveLength(2)
  })

  it('ignores a repeated event and survives a reload and an out-of-order arrival', () => {
    const events = drillRun()
    const once = feed(events).documents[0]
    const twice = feed(events, feed(events)).documents[0]
    expect(twice.candidates).toEqual(once.candidates)
    const reloaded = runViewReducer(emptyRunView, { type: 'replay', events: [...events].reverse() }).documents[0]
    expect(reloaded.candidates).toEqual(once.candidates)
    expect(reloaded.mappingsFound).toEqual(once.mappingsFound)
  })
})

describe('plain words', () => {
  it('counts the Candidates by outcome and the pairs set aside', () => {
    const d = feed(drillRun()).documents[0]
    expect(candidateCounts(d)).toEqual({
      pairs: 50,
      kept: 4,
      setAside: 46,
      settled: 4,
      byOutcome: { mapped: 2, not_applicable: 1, dropped_by_proof: 1, skipped: 0 },
    })
  })

  it('says why the Gate kept a Candidate without leading with jargon', () => {
    const d = feed(drillRun()).documents[0]
    const r = gateReasons(d.candidates[0], d, 7)
    for (const text of [r.why, r.meaning.headline, r.keywords!.headline]) {
      expect(text).not.toMatch(/cosine|bm25/i)
    }
    expect(r.meaning.headline).toMatch(/meaning to Pillar 7/)
    expect(r.keywords!.headline).toMatch(/keyword match for Indicator 7\.4/)
    // Placed among the Document's Candidates: 0.45 to 0.70 in meaning, 4 to
    // 12 in keywords for 7.4 (this one is the top).
    expect(r.meaning.scale).toMatchObject({ lowest: 0.45, highest: 0.7 })
    expect(r.keywords!.scale).toMatchObject({ lowest: 4, highest: 12, position: 1 })
    expect(r.meaning.detail).toContain('0.60')
  })

  it('never claims a rank the scores cannot support', () => {
    const d = feed(drillRun()).documents[0]
    const same = { ...d, candidates: d.candidates.map((c) => ({ ...c, cosine: 0.6, bm25: 5 })) }
    const r = gateReasons(same.candidates[0], same, 7)
    expect(r.meaning.headline).toBe('Close enough in meaning to Pillar 7 to pass')
    expect(r.keywords!.headline).toBe('Keyword match for Indicator 7.4')
    expect(r.meaning.detail).toMatch(/the same for every Candidate/)
    // The sentence agrees with a weak keyword match: it only says it passed.
    const weak = gateReasons(d.candidates[1], d, 7)
    expect(weak.keywords!.headline).toBe('Enough keyword match for Indicator 7.4 to pass')
    expect(weak.why).not.toMatch(/most|closest|strongest/)
  })

  it('says the keyword check did not run for a meaning-only Document', () => {
    const d = feed(drillRun()).documents[0]
    const c = { ...d.candidates[1], lane: 'meaning_only' as const, bm25: 0 }
    const r = gateReasons(c, d, 7)
    expect(r.keywords).toBeNull()
    expect(r.meaningOnlyNote).toMatch(/keyword check/)
    expect(r.why).toMatch(/meaning alone/)
  })

  it('names every outcome', () => {
    for (const o of ['mapped', 'not_applicable', 'dropped_by_proof', 'skipped'] as const) {
      expect(OUTCOME_LABEL[o]).toBeTruthy()
      expect(OUTCOME_SENTENCE[o]).not.toMatch(/cosine|bm25/i)
    }
  })

  it("walks a Document's Steps with counts, Map and Prove as one", () => {
    const d = feed(drillRun()).documents[0]
    const rows = stepRows(d)
    expect(rows.map((r) => r.label)).toEqual(['Read', 'Scan check', 'Split into sections', 'Gate', 'Map and Prove', 'Gloss'])
    expect(rows[3].result).toBe('Kept 4 of 50 section and Indicator pairs. These are the Candidates; the other 46 were set aside.')
    expect(rows[3].bar).toBeCloseTo(4 / 50)
    expect(rows[4].result).toBe('4 Candidates read. 2 gave a proven Mapping, 1 not applicable. 1 quote dropped by the proof check.')
    expect(rows[5].took).toBe('skipped')
    expect(rows.every((r) => r.took !== null)).toBe(true)
  })

  it('shows a Document mid-Map with its count so far', () => {
    const events = drillRun()
    const tick = events.findIndex((e) => e.type === 'map_progress')
    const d = feed(events.slice(0, tick + 1)).documents[0]
    const row = stepRows(d)[4]
    expect(row.state).toBe('active')
    expect(row.result).toBe('4 of 4 Candidates read by the Engine so far.')
    expect(stepRows(d)[5].result).toBeNull()
  })

  it('groups by Indicator in number order', () => {
    const groups = byIndicator([
      { indicator: '7.10' },
      { indicator: '7.2' },
      { indicator: '7.4' },
      { indicator: '7.2' },
    ])
    expect(groups.map((g) => [g.indicator, g.items.length])).toEqual([
      ['7.2', 2],
      ['7.4', 1],
      ['7.10', 1],
    ])
  })

  it('marks the quote inside its Piece, or nothing when it is not there', () => {
    expect(markQuote('a b c d', 'b c')).toEqual({ before: 'a ', quote: 'b c', after: ' d' })
    expect(markQuote('a b c d', 'x')).toBeNull()
    expect(markQuote('a b c d', null)).toBeNull()
  })
})

describe('a Run recorded before the Gate scores were', () => {
  const events = recorded
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as RunEvent)
  const s = runViewReducer(emptyRunView, { type: 'replay', events })

  it('says the scores were not recorded rather than showing empty boxes', () => {
    const pdpa = s.documents.find((d) => d.document_id.endsWith('PDPA2012'))!
    expect(pdpa.candidates).toEqual([])
    expect(gateScoresRecorded(s, pdpa)).toBe(false)
    // Its counts are still there.
    expect(candidateCounts(pdpa)).toMatchObject({ pairs: 520, kept: 97, setAside: 423 })
    expect(stepRows(pdpa)[3].result).toMatch(/Kept 97 of 520/)
  })

  it('knows a Run that sent no candidate events even without the note', () => {
    const noNote = runViewReducer(emptyRunView, {
      type: 'replay',
      events: events.map((e) => (e.type === 'run_started' ? { ...e, not_recorded: [] } : e)),
    })
    const pdpa = noNote.documents.find((d) => d.document_id.endsWith('PDPA2012'))!
    expect(gateScoresRecorded(noNote, pdpa)).toBe(false)
    const fresh = feed(drillRun())
    expect(gateScoresRecorded(fresh, fresh.documents[0])).toBe(true)
  })
})

describe('quoted', () => {
  it('wraps a quote in quote marks', () => {
    expect(quoted('Every bank must provide')).toBe('“Every bank must provide”')
  })
  it('adds none to a quote that already opens or closes with one, and keeps its text exact', () => {
    const own = '“customer”, in relation to a bank, includes the Authority or any'
    expect(quoted(own)).toBe(own)
    expect(quoted('"personal data" means')).toBe('"personal data" means')
    expect(quoted('as defined in “data”')).toBe('as defined in “data”')
  })
  it('treats a closing apostrophe as part of the text', () => {
    expect(quoted("the members'")).toBe("“the members'”")
  })
})
