import { describe, expect, it } from 'vitest'
import recorded from '../../tests/fixtures/run_events/run_20260923T041241Z_a07249.jsonl?raw'
import record from '../../tests/fixtures/run_events/run_20260923T041241Z_a07249.record.json'
import { emptyRunView, runViewReducer, type RunViewState } from './runViewState'
import {
  candidatesMapped,
  documentBreakdown,
  documentState,
  duration,
  failureSentence,
  flow,
  stations,
  stepsWithNoWork,
  technicalMessage,
  timing,
  zeroReason,
} from './runOverview'
import type { RunEvent, RunStep } from './types'

// Every state a Document line can be in, fed through the same reducer the
// screen uses, then read the way the screen reads it.

type Draft = RunEvent extends infer E ? (E extends RunEvent ? Omit<E, 'seq' | 'ts'> : never) : never

const T0 = Date.parse('2026-09-28T10:00:00Z')

/** Numbered events, one second apart. */
function numbered(drafts: Draft[]): RunEvent[] {
  return drafts.map((d, i) => ({ ...d, seq: i, ts: new Date(T0 + i * 1000).toISOString() }) as RunEvent)
}

function feed(events: RunEvent[], state: RunViewState = emptyRunView): RunViewState {
  return events.reduce((s, event) => runViewReducer(s, { type: 'event', event }), state)
}

const STARTED: Draft = {
  type: 'run_started',
  run_id: 'run_a',
  economy: 'MY',
  pillars: [7],
  indicators: null,
  engine: 'fake',
  documents: [{ document_id: 'doc_a', title: 'Act A', language: 'English', n_pages: 3 }],
}

function steps(
  id: string,
  opts: { pieces?: number; kept?: number; proven?: number; noEvidence?: number; dropped?: number; flag?: string } = {},
): Draft[] {
  const { pieces = 6, kept = 4, proven = 2, noEvidence = kept - proven, dropped = 0 } = opts
  const counts: Record<RunStep, Record<string, number>> = {
    read: { pages: 3, chars: 900 },
    scan_check: { ocr_applied: opts.flag ? 1 : 0 },
    cut: { pieces },
    gate: { pieces, pairs: pieces * 5, candidates: kept },
    map: { done: kept, total: kept },
    prove: { proven, no_evidence: noEvidence, dropped },
    gloss: { glossed: 0 },
    reconcile: {},
  }
  const out: Draft[] = []
  for (const step of ['read', 'scan_check', 'cut', 'gate', 'map', 'prove', 'gloss'] as RunStep[]) {
    out.push({ type: 'step_started', document_id: id, step })
    out.push({ type: 'step_finished', document_id: id, step, counts: counts[step] })
    if (step === 'scan_check' && opts.flag) out.push({ type: 'scan_flagged', document_id: id, reason: opts.flag })
  }
  out.push({ type: 'document_finished', document_id: id, mappings: proven, engine_calls: kept, cost_usd: 0.01 })
  return out
}

const doc = (s: RunViewState) => s.documents[0]

describe('each Document state', () => {
  it('waiting: listed, nothing begun', () => {
    const s = feed(numbered([STARTED]))
    expect(documentState(doc(s))).toBe('waiting')
    expect(stations(doc(s))).toEqual(['waiting', 'waiting', 'waiting', 'waiting', 'waiting', 'waiting'])
  })

  it('working: the station it is on is the active one, the ones before are done', () => {
    const s = feed(
      numbered([
        STARTED,
        ...steps('doc_a').slice(0, 8), // read .. gate finished
        { type: 'step_started', document_id: 'doc_a', step: 'map' },
        { type: 'map_progress', document_id: 'doc_a', done: 2, total: 4, engine_calls: 2, cost_usd: 0.004 },
      ]),
    )
    expect(documentState(doc(s))).toBe('working')
    expect(stations(doc(s))).toEqual(['done', 'skipped', 'done', 'done', 'active', 'waiting'])
    expect(candidatesMapped(s)).toEqual({ done: 2, kept: 4 })
    expect(s.meter).toEqual({ engine_calls: 2, cost_usd: 0.004 })
  })

  it('working on a quiet Step: the Gate is active before it reports anything', () => {
    const s = feed(numbered([STARTED, ...steps('doc_a').slice(0, 6), { type: 'step_started', document_id: 'doc_a', step: 'gate' }]))
    expect(stations(doc(s))[3]).toBe('active')
    // A live Run's open Step runs up to now, so its time keeps growing.
    const t1 = timing(s, s.lastAt! + 5_000)!
    const t2 = timing(s, s.lastAt! + 9_000)!
    const gate = (t: typeof t1) => t.byStep.find((x) => x.step === 'gate')!.ms
    expect(gate(t2) - gate(t1)).toBe(4_000)
    expect(t1.byDocument[0].gate?.open).toBe(true)
  })

  it('finished: every station done; Scan check with no OCR and Gloss with nothing to do shown as skipped', () => {
    const s = feed(numbered([STARTED, ...steps('doc_a')]))
    expect(documentState(doc(s))).toBe('finished')
    expect(stations(doc(s))).toEqual(['done', 'skipped', 'done', 'done', 'done', 'skipped'])
    expect([...stepsWithNoWork(s)].sort()).toEqual(['gloss', 'scan_check'])
    expect(zeroReason(doc(s))).toBeNull()
  })

  it('a flagged scan: marked at the Scan check, and still mapped', () => {
    const s = feed(numbered([STARTED, ...steps('doc_a', { flag: 'Read by OCR with 41% word confidence' })]))
    expect(doc(s).flag).toBe('Read by OCR with 41% word confidence')
    expect(documentState(doc(s))).toBe('finished')
    expect(stations(doc(s))[1]).toBe('flag')
    expect(doc(s).mappings).toBe(2)
    expect(stepsWithNoWork(s).has('scan_check')).toBe(false)
  })

  it('zero Mappings because the Gate kept nothing, with the reason', () => {
    const s = feed(numbered([STARTED, ...steps('doc_a', { pieces: 1, kept: 0, proven: 0, noEvidence: 0 })]))
    expect(documentState(doc(s))).toBe('zero')
    expect(stations(doc(s))).toEqual(['done', 'skipped', 'done', 'zero', 'skipped', 'skipped'])
    expect(zeroReason(doc(s))).toMatch(/No numbered headings found/)
  })

  it('zero Mappings because none was proven, with the reason', () => {
    const s = feed(numbered([STARTED, ...steps('doc_a', { kept: 75, proven: 0, noEvidence: 73, dropped: 2 })]))
    expect(documentState(doc(s))).toBe('zero')
    expect(stations(doc(s))).toEqual(['done', 'skipped', 'done', 'done', 'zero', 'skipped'])
    expect(zeroReason(doc(s))).toBe(
      '75 Candidates, none proven: 73 had no evidence, 2 quotes failed the word-for-word check.',
    )
  })

  it('failed: names the Document and the Step in plain words, no class name', () => {
    const s = feed(
      numbered([
        STARTED,
        ...steps('doc_a').slice(0, 6),
        { type: 'step_started', document_id: 'doc_a', step: 'gate' },
        { type: 'run_failed', document_id: 'doc_a', step: 'gate', message: 'RuntimeError: the embedder fell over' },
      ]),
    )
    expect(documentState(doc(s))).toBe('failed')
    expect(stations(doc(s))).toEqual(['done', 'skipped', 'done', 'fail', 'waiting', 'waiting'])
    const sentence = failureSentence(s)!
    expect(sentence).toBe('The Run stopped at Gate on Act A. Nothing after that point was run.')
    expect(sentence).not.toMatch(/Error/)
    expect(technicalMessage(s.failure!.message)).toBe('the embedder fell over')
    // A stopped Run's clock stops with it.
    const t = timing(s, s.lastAt! + 60_000)!
    expect(t.end).toBe(s.endedAt)
  })

  it('a failure before any Step names neither', () => {
    const s = feed(numbered([STARTED, { type: 'run_failed', document_id: null, step: null, message: 'OSError: the disk went away' }]))
    expect(failureSentence(s)).toBe('The Run stopped before its first Step, so no Document was read.')
  })
})

describe('Reconcile', () => {
  const tail: Draft[] = [
    { type: 'step_started', document_id: null, step: 'reconcile' },
    { type: 'step_finished', document_id: null, step: 'reconcile', counts: { passed: 2, records: 2, groups: 1 } },
  ]

  it('reads before and after from its own event', () => {
    const s = feed(
      numbered([STARTED, ...steps('doc_a'), ...tail, { type: 'reconcile', before: 2, after: 1, engine_calls: 5, cost_usd: 0.02 }]),
    )
    expect(s.reconcile).toBe('done')
    expect(s.reconciled).toEqual({ before: 2, after: 1 })
    expect(s.meter).toEqual({ engine_calls: 5, cost_usd: 0.02 })
    expect(flow(s).groups).toBe(1)
  })

  it('falls back to its Step counts on a Run recorded before the event existed', () => {
    const s = feed(numbered([STARTED, ...steps('doc_a'), ...tail]))
    expect(s.reconciled).toEqual({ before: 2, after: 1 })
  })
})

describe('the SG Pillar 7 Run, watched again', () => {
  const EVENTS: RunEvent[] = recorded
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as RunEvent)
  const s = feed(EVENTS)

  it('lists the ten Documents of its Run Record, in order', () => {
    expect(s.documents.map((d) => d.document_id)).toEqual(record.details.documents)
    expect(s.documents).toHaveLength(10)
  })

  it('every Document walks the Steps in order, then Reconcile once', () => {
    for (const d of s.documents) {
      expect(d.finished).toEqual(['read', 'scan_check', 'cut', 'gate', 'map', 'prove', 'gloss'])
      expect(documentState(d)).toBe('finished')
    }
    const reconciles = EVENTS.filter((e) => e.type === 'step_started' && e.document_id === null)
    expect(reconciles).toHaveLength(1)
    expect(s.reconcile).toBe('done')
  })

  it('its flow equals the Run Record: 2,072 Pieces, 870 kept, 148 proven, 5 groups', () => {
    const f = flow(s)
    const d = record.details
    expect(f.documents).toBe(10)
    expect(f.documentsDone).toBe(10)
    expect(f.pieces).toBe(d.n_chunks)
    expect(f.pairs).toBe(10120)
    expect(f.candidates).toBe(d.n_pairs_gated)
    expect(f.proven).toBe(d.n_passed)
    expect(f.noEvidence).toBe(d.n_no_evidence)
    expect(f.dropped).toBe(d.n_dropped)
    expect(f.proposed).toBe(148 + 9)
    expect(f.groups).toBe(d.n_groups)
    // The same numbers summed Document by Document, before the totals arrive.
    const beforeEnd = feed(EVENTS.slice(0, -1))
    const g = flow(beforeEnd)
    expect([g.pieces, g.pairs, g.candidates, g.proven, g.dropped, g.groups]).toEqual([2072, 10120, 870, 148, 9, 5])
    expect(Object.fromEntries(s.documents.map((x) => [x.document_id, x.mappings]))).toEqual(record.mappings_by_document)
  })

  it('finishes with the Run Record cost, the provider bill and Engine calls', () => {
    expect(s.status).toBe('finished')
    expect(s.providerCost).toBeCloseTo(record.provider_cost_usd)
    expect(s.meter?.engine_calls).toBe(record.details.model_calls)
    expect(s.meter?.cost_usd).toBeCloseTo(record.cost_usd)
  })

  it('times each Step from the events, says they are estimated, and never times Prove apart from Map', () => {
    const t = timing(s, Date.now())!
    expect(t.estimated).toBe(true)
    expect(t.byStep.map((x) => x.step)).toEqual([
      'read', 'scan_check', 'cut', 'gate', 'map_prove', 'gloss', 'reconcile',
    ])
    const took = t.end - t.start
    expect(duration(took)).toBe('18 min 15 s')
    const ms = (step: string) => t.byStep.find((x) => x.step === step)!.ms
    // Every moment of the Run is in exactly one Step.
    const sum = t.byStep.reduce((a, x) => a + x.ms, 0)
    expect(Math.abs(sum - took)).toBeLessThan(5)
    expect(ms('gate')).toBeGreaterThan(0)
    expect(ms('map_prove')).toBeGreaterThan(ms('gate'))
    expect(duration(ms('reconcile'))).toBe('1 min 02 s')
    expect(t.byDocument).toHaveLength(10)
    for (const d of t.byDocument) {
      expect(d.gate && d.mapProve && d.gate.end <= d.mapProve.start + 1).toBeTruthy()
    }
  })

  it('a Document breakdown says its Step times were not recorded, never zeros as measured', () => {
    const t = timing(s, Date.now())!
    const b = documentBreakdown(t.byDocument[0], t.estimated)
    expect(b.recorded).toBe(false)
    expect(b.title).toBe('BANKING ACT 1970')
    const by = Object.fromEntries(b.parts.map((p) => [p.station, p]))
    expect(by.read.status).toBe('not_recorded')
    expect(by.cut.status).toBe('not_recorded')
    // The log gave the Gate its time; Map and Prove is what is left.
    expect(by.gate.ms).toBe(16_400)
    expect(by.map_prove.status).toBe('timed')
    expect(b.parts.filter((p) => p.status === 'timed' && p.ms === 0)).toEqual([])
  })

  it('half way through, the header counts only what has happened', () => {
    const middle = EVENTS.findIndex((e) => e.type === 'map_progress' && e.document_id === record.details.documents[6])
    const h = feed(EVENTS.slice(0, middle + 1))
    expect(h.status).toBe('running')
    expect(h.documents.slice(0, 6).every((d) => d.done)).toBe(true)
    expect(documentState(h.documents[6])).toBe('working')
    expect(documentState(h.documents[7])).toBe('waiting')
    const c = candidatesMapped(h)
    expect(c.done).toBeLessThan(c.kept)
    expect(flow(h).groups).toBeNull()
    const t = timing(h, h.lastAt! + 30_000)!
    expect(t.live).toBe(true)
    expect(t.byDocument[7].span).toBeNull()
  })
})

describe("one Document's time, Step by Step", () => {
  // Events one second apart: each Step takes 1 s from its start to its finish.
  it('a recorded Run: every Step timed, the idle Scan check and Gloss left out, shares of the total', () => {
    const s = feed(numbered([STARTED, ...steps('doc_a')]))
    const t = timing(s, s.lastAt!)!
    const b = documentBreakdown(t.byDocument[0], t.estimated)
    expect(b.title).toBe('Act A')
    expect(b.recorded).toBe(true)
    expect(b.parts.map((p) => p.label)).toEqual(['Read', 'Split into sections', 'Gate', 'Map and Prove'])
    expect(b.parts.every((p) => p.status === 'timed')).toBe(true)
    // Read starts at 1 s, Gloss ends at 14 s.
    expect(b.totalMs).toBe(13_000)
    const ms = Object.fromEntries(b.parts.map((p) => [p.station, p.ms]))
    // Map and Prove: Map's start to Prove's end.
    expect(ms).toEqual({ read: 1_000, cut: 1_000, gate: 1_000, map_prove: 3_000 })
    expect(b.parts[3].share).toBeCloseTo(3 / 13)
  })

  it('a Step with work to do keeps its line, however short', () => {
    const s = feed(numbered([STARTED, ...steps('doc_a', { flag: 'Read by OCR' })]))
    const t = timing(s, s.lastAt!)!
    const b = documentBreakdown(t.byDocument[0], false)
    expect(b.parts.map((p) => p.station)).toEqual(['read', 'scan_check', 'cut', 'gate', 'map_prove'])
  })

  it('a live Document: the Step it is on runs up to now, the ones after are waiting', () => {
    const s = feed(numbered([STARTED, ...steps('doc_a').slice(0, 6), { type: 'step_started', document_id: 'doc_a', step: 'gate' }]))
    const t = timing(s, s.lastAt! + 4_000)!
    const b = documentBreakdown(t.byDocument[0], false)
    expect(b.open).toBe(true)
    const gate = b.parts.find((p) => p.station === 'gate')!
    expect(gate).toMatchObject({ status: 'timed', ms: 4_000, open: true })
    expect(b.parts.find((p) => p.station === 'map_prove')).toMatchObject({ status: 'waiting', ms: 0 })
  })

  it('not begun: no total, every Step waiting', () => {
    const s = feed(numbered([STARTED]))
    const b = documentBreakdown(timing(s, s.lastAt!)!.byDocument[0], false)
    expect(b.totalMs).toBeNull()
    expect(b.parts.every((p) => p.status === 'waiting')).toBe(true)
  })
})
