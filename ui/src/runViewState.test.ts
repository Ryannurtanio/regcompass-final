import { describe, expect, it } from 'vitest'
import { emptyRunView, runViewReducer, stepLabel, type RunViewState } from './runViewState'
import type { RunEvent, RunStep } from './types'

// A hand-made Run: two Documents, every Step, Map ticking, then Reconcile.
// Events are written without seq/ts and numbered in order by `numbered`.
type Draft = RunEvent extends infer E ? (E extends RunEvent ? Omit<E, 'seq' | 'ts'> : never) : never

const DOC_STEPS: RunStep[] = ['read', 'scan_check', 'cut', 'gate', 'map', 'prove', 'gloss']

function numbered(drafts: Draft[], runStart = 0): RunEvent[] {
  return drafts.map(
    (d, i) => ({ ...d, seq: runStart + i, ts: `2026-09-28T10:00:${String(i).padStart(2, '0')}+00:00` }) as RunEvent,
  )
}

function documentDrafts(id: string, proven: number): Draft[] {
  const out: Draft[] = []
  for (const step of DOC_STEPS) {
    out.push({ type: 'step_started', document_id: id, step })
    if (step === 'map') {
      out.push({ type: 'map_progress', document_id: id, done: 2, total: 4 })
      out.push({ type: 'map_progress', document_id: id, done: 4, total: 4 })
    }
    const counts: Record<string, number> =
      step === 'read'
        ? { pages: 3, chars: 900 }
        : step === 'cut'
          ? { pieces: 6 }
          : step === 'gate'
            ? { pieces: 6, pairs: 30, candidates: 4 }
            : step === 'map'
              ? { done: 4, total: 4 }
              : step === 'prove'
                ? { proven, no_evidence: 4 - proven, dropped: 0 }
                : step === 'gloss'
                  ? { glossed: 0 }
                  : { ocr_applied: 0 }
    out.push({ type: 'step_finished', document_id: id, step, counts })
  }
  out.push({ type: 'document_finished', document_id: id, mappings: proven })
  return out
}

const STARTED: Draft = {
  type: 'run_started',
  run_id: 'run_a',
  economy: 'SG',
  pillars: [7],
  indicators: null,
  engine: 'fake',
  documents: [
    { document_id: 'doc_a', title: 'Act A', language: 'English', n_pages: 3 },
    { document_id: 'doc_b', title: 'Act B', language: 'English', n_pages: 5 },
  ],
}

function normalRun(): RunEvent[] {
  return numbered([
    STARTED,
    ...documentDrafts('doc_a', 3),
    ...documentDrafts('doc_b', 0),
    { type: 'step_started', document_id: null, step: 'reconcile' },
    { type: 'step_finished', document_id: null, step: 'reconcile', counts: { passed: 3, records: 3, groups: 2 } },
    {
      type: 'run_finished',
      status: 'completed',
      totals: { documents: 2, pieces: 12, pairs: 60, candidates: 8, proven: 3, no_evidence: 5, dropped: 0, glossed: 0, groups: 2 },
      cost_usd: 0,
    },
  ])
}

function feed(events: RunEvent[], state: RunViewState = emptyRunView): RunViewState {
  return events.reduce((s, event) => runViewReducer(s, { type: 'event', event }), state)
}

/** The picture on screen, without the event log the state keeps. */
function picture(state: RunViewState) {
  const { events: _events, ...rest } = state
  return rest
}

describe('a normal Run', () => {
  it('lists every Document as soon as the Run starts, none begun yet', () => {
    const s = feed(normalRun().slice(0, 1))
    expect(s.status).toBe('running')
    expect(s.run?.economy).toBe('SG')
    expect(s.documents.map((d) => d.document_id)).toEqual(['doc_a', 'doc_b'])
    expect(s.documents.every((d) => d.step === null && !d.done && d.progress === 0)).toBe(true)
  })

  it('follows the Step each Document is on', () => {
    const events = normalRun()
    const gateStarted = events.findIndex(
      (e) => e.type === 'step_started' && e.document_id === 'doc_a' && e.step === 'gate',
    )
    const s = feed(events.slice(0, gateStarted + 1))
    const [a, b] = s.documents
    expect(a.step).toBe('gate')
    expect(a.counts.cut).toEqual({ pieces: 6 })
    expect(a.progress).toBeGreaterThan(0)
    expect(a.progress).toBeLessThan(1)
    expect(b.step).toBeNull()
  })

  it('moves the bar with Map and shows Map and Prove as one Step', () => {
    const events = normalRun()
    const halfway = events.findIndex((e) => e.type === 'map_progress' && e.document_id === 'doc_a')
    const before = feed(events.slice(0, halfway))
    const after = feed(events.slice(0, halfway + 1))
    expect(after.documents[0].mapDone).toBe(2)
    expect(after.documents[0].mapTotal).toBe(4)
    expect(after.documents[0].progress).toBeGreaterThan(before.documents[0].progress)
    expect(stepLabel('map')).toBe(stepLabel('prove'))
  })

  it('ticks every Document and finishes the Run with its totals', () => {
    const s = feed(normalRun())
    expect(s.status).toBe('finished')
    expect(s.documents.map((d) => [d.done, d.mappings, d.progress])).toEqual([
      [true, 3, 1],
      [true, 0, 1],
    ])
    expect(s.reconcile).toBe('done')
    expect(s.totals?.proven).toBe(3)
    expect(s.progress).toBe(1)
    expect(s.failure).toBeNull()
  })
})

describe('a reload in the middle of a Run', () => {
  it('rebuilds the same picture from the replayed events', () => {
    const events = normalRun()
    const cut = 20
    const live = feed(events.slice(0, cut))
    const reloaded = runViewReducer(emptyRunView, { type: 'replay', events: events.slice(0, cut) })
    expect(picture(reloaded)).toEqual(picture(live))
    // The stream replays from the first event, so the rest arrives on top.
    const finished = feed(events, reloaded)
    expect(picture(finished)).toEqual(picture(feed(events)))
  })

  it('ignores the events a reconnecting stream sends twice', () => {
    const events = normalRun()
    const once = feed(events)
    const twice = feed(events, feed(events.slice(0, 30)))
    expect(picture(twice)).toEqual(picture(once))
    expect(twice.events.length).toBe(events.length)
  })

  it('replays a shuffled stream with repeats to the same picture, once', () => {
    const events = normalRun()
    const messy = [...events.slice(10), ...events, ...events.slice(0, 10)].reverse()
    const s = runViewReducer(emptyRunView, { type: 'replay', events: messy })
    expect(picture(s)).toEqual(picture(feed(events)))
    expect(s.events.map((e) => e.seq)).toEqual(events.map((e) => e.seq))
  })
})

describe('events out of order', () => {
  it('reach the same picture whatever order they arrive in', () => {
    const events = normalRun()
    const shuffled = [...events].reverse()
    // Swap neighbours as well, a milder disorder than a full reversal.
    const swapped = [...events]
    for (let i = 1; i < swapped.length; i += 2) {
      ;[swapped[i - 1], swapped[i]] = [swapped[i], swapped[i - 1]]
    }
    const inOrder = picture(feed(events))
    expect(picture(feed(shuffled))).toEqual(inOrder)
    expect(picture(feed(swapped))).toEqual(inOrder)
  })

  it('never shows a Document on an earlier Step because a late event came in', () => {
    const events = normalRun()
    const gloss = events.findIndex(
      (e) => e.type === 'step_started' && e.document_id === 'doc_a' && e.step === 'gloss',
    )
    const late = events.findIndex(
      (e) => e.type === 'step_started' && e.document_id === 'doc_a' && e.step === 'cut',
    )
    const withoutLate = events.slice(0, gloss + 1).filter((_, i) => i !== late)
    const s = feed([...withoutLate, events[late]])
    expect(s.documents[0].step).toBe('gloss')
  })
})

describe('a failure', () => {
  it('names the Document and the Step, and ends the Run', () => {
    const drafts: Draft[] = [
      STARTED,
      ...documentDrafts('doc_a', 3),
      { type: 'step_started', document_id: 'doc_b', step: 'read' },
      { type: 'step_finished', document_id: 'doc_b', step: 'read', counts: { pages: 5, chars: 10 } },
      { type: 'step_started', document_id: 'doc_b', step: 'gate' },
      { type: 'run_failed', document_id: 'doc_b', step: 'gate', message: 'RuntimeError: the embedder fell over' },
    ]
    const s = feed(numbered(drafts))
    expect(s.status).toBe('failed')
    expect(s.failure).toEqual({
      document_id: 'doc_b',
      step: 'gate',
      message: 'RuntimeError: the embedder fell over',
    })
    const [a, b] = s.documents
    expect(a.done).toBe(true)
    expect(b.failed).toBe(true)
    expect(b.done).toBe(false)
    expect(b.step).toBe('gate')
  })

  it('reports a failure that happened before any Step', () => {
    const s = feed(
      numbered([{ type: 'run_failed', document_id: null, step: null, message: 'OSError: the disk went away' }]),
    )
    expect(s.status).toBe('failed')
    expect(s.failure?.message).toBe('OSError: the disk went away')
    expect(s.documents).toEqual([])
  })
})

describe('the next Run', () => {
  it('starts from nothing, not on top of the last one', () => {
    const first = feed(normalRun())
    const next = feed(
      numbered([
        {
          ...STARTED,
          run_id: 'run_b',
          documents: [{ document_id: 'doc_b', title: 'Act B', language: 'English', n_pages: 5 }],
        } as Draft,
      ]),
      first,
    )
    expect(next.run?.run_id).toBe('run_b')
    expect(next.status).toBe('running')
    expect(next.documents.map((d) => d.document_id)).toEqual(['doc_b'])
    expect(next.documents[0].done).toBe(false)
  })

  it('a reset clears the screen', () => {
    expect(runViewReducer(feed(normalRun()), { type: 'reset' })).toEqual(emptyRunView)
  })

  it('a Document the Run did not list up front appears with its first Step', () => {
    const s = feed(
      numbered([
        { ...STARTED, run_id: null, documents: [] } as Draft,
        { type: 'step_started', document_id: 'doc_z', step: 'read' },
      ]),
    )
    expect(s.documents.map((d) => [d.document_id, d.title, d.step])).toEqual([['doc_z', 'doc_z', 'read']])
  })
})
