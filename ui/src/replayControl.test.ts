import { describe, expect, it } from 'vitest'
import recorded from '../../tests/fixtures/run_events/run_20260923T041241Z_a07249.jsonl?raw'
import { emptyRunView, runViewReducer, type RunViewState } from './runViewState'
import { ReplayController, notRecordedSentences, replayUrl, type ReplaySource } from './replayControl'
import type { RunEvent } from './types'

// The first replayable Run: Singapore P7 on Engine A, rebuilt from its log by
// scripts/convert_run_log.py. The Run view's reducer reads it like any Run.
const EVENTS: RunEvent[] = recorded
  .split('\n')
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as RunEvent)

function feed(events: RunEvent[], state: RunViewState = emptyRunView): RunViewState {
  return events.reduce((s, event) => runViewReducer(s, { type: 'event', event }), state)
}

describe('the converted Singapore P7 Run', () => {
  it('builds the finished picture with the Run Record totals', () => {
    const s = feed(EVENTS)
    expect(s.status).toBe('finished')
    expect(s.run?.run_id).toBe('run_20260923T041241Z_a07249')
    expect(s.documents).toHaveLength(10)
    expect(s.documents.every((d) => d.done && d.progress === 1)).toBe(true)
    expect(s.documents.reduce((n, d) => n + (d.mappings ?? 0), 0)).toBe(148)
    expect(s.reconcile).toBe('done')
    expect(s.progress).toBe(1)
    expect(s.totals).toEqual({
      documents: 10,
      pieces: 2072,
      pairs: 10120,
      candidates: 870,
      proven: 148,
      no_evidence: 713,
      dropped: 9,
      glossed: 0,
      groups: 5,
    })
  })

  it('knows the Run was rebuilt from a log and what the log never had', () => {
    const s = feed(EVENTS.slice(0, 1))
    expect(s.run?.recordedFrom).toBe('log')
    expect(s.run?.notRecorded).toEqual(['gate_scores', 'step_times'])
    const sentences = notRecordedSentences(s.run!.notRecorded)
    expect(sentences.join(' ')).toMatch(/Gate's scores per Candidate/)
    expect(sentences.join(' ')).toMatch(/estimated/)
  })

  it('a live Run names nothing as missing', () => {
    const live = { ...EVENTS[0] } as Record<string, unknown>
    delete live.recorded_from
    delete live.not_recorded
    const s = feed([live as unknown as RunEvent])
    expect(s.run?.recordedFrom).toBeNull()
    expect(s.run?.notRecorded).toEqual([])
  })

  it('the same picture whether fed one event at a time or replayed at once', () => {
    const { events: _a, ...one } = feed(EVENTS)
    const { events: _b, ...all } = runViewReducer(emptyRunView, { type: 'replay', events: EVENTS })
    expect(all).toEqual(one)
  })

  it('half way through, the Documents after the current one are still waiting', () => {
    const middle = EVENTS.findIndex((e) => e.type === 'document_finished') + 1
    const s = feed(EVENTS.slice(0, middle + 3))
    expect(s.documents[0].done).toBe(true)
    expect(s.documents[1].step).not.toBeNull()
    expect(s.documents.slice(2).every((d) => d.step === null)).toBe(true)
    expect(s.status).toBe('running')
  })
})

/** A stand-in for the browser's EventSource: the test pushes events into it. */
class FakeSource implements ReplaySource {
  closed = false
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>()
  onerror: ((ev: Event) => void) | null = null
  constructor(public url: string) {}
  addEventListener(name: string, fn: (ev: MessageEvent) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), fn])
  }
  close() {
    this.closed = true
  }
  send(event: RunEvent) {
    if (this.closed) return
    for (const fn of this.listeners.get(event.type) ?? []) fn({ data: JSON.stringify(event) } as MessageEvent)
  }
  end() {
    for (const fn of this.listeners.get('end') ?? []) fn({ data: '{"replay": true}' } as MessageEvent)
  }
}

function harness(speed = 20) {
  const sources: FakeSource[] = []
  let state = emptyRunView
  const phases: string[] = []
  const controller = new ReplayController({
    runId: 'run_20260923T041241Z_a07249',
    speed,
    open: (url) => {
      const s = new FakeSource(url)
      sources.push(s)
      return s
    },
    onEvent: (event) => {
      state = runViewReducer(state, { type: 'event', event })
    },
    onPhase: (phase) => phases.push(phase),
  })
  return { controller, sources, phases, state: () => state }
}

describe('watching a Run again', () => {
  it('asks the replay endpoint from the first event at the chosen speed', () => {
    expect(replayUrl('run_x', 20, 0)).toBe('/api/runs/run_x/replay?speed=20&from_seq=0')
    const h = harness()
    h.controller.start()
    expect(h.sources[0].url).toBe('/api/runs/run_20260923T041241Z_a07249/replay?speed=20&from_seq=0')
    expect(h.phases).toEqual(['playing'])
  })

  it('pauses by closing the stream, and resumes from the next event', () => {
    const h = harness()
    h.controller.start()
    for (const e of EVENTS.slice(0, 30)) h.sources[0].send(e)
    h.controller.pause()
    expect(h.sources[0].closed).toBe(true)
    expect(h.phases.at(-1)).toBe('paused')
    const paused = h.state()
    // Nothing moves while paused, even if the old stream still had events in flight.
    h.sources[0].send(EVENTS[30])
    expect(h.state()).toBe(paused)

    h.controller.resume()
    expect(h.sources).toHaveLength(2)
    expect(h.sources[1].url).toMatch(/from_seq=30$/)
    for (const e of EVENTS.slice(30)) h.sources[1].send(e)
    h.sources[1].end()
    expect(h.phases.at(-1)).toBe('finished')
    const { events: _a, ...watched } = h.state()
    const { events: _b, ...whole } = feed(EVENTS)
    expect(watched).toEqual(whole)
  })

  it('a new speed carries on from where the Run is, at that speed', () => {
    const h = harness()
    h.controller.start()
    for (const e of EVENTS.slice(0, 12)) h.sources[0].send(e)
    h.controller.setSpeed(100)
    expect(h.sources[0].closed).toBe(true)
    expect(h.sources[1].url).toBe(
      '/api/runs/run_20260923T041241Z_a07249/replay?speed=100&from_seq=12',
    )
  })

  it('a new speed while paused waits for resume', () => {
    const h = harness()
    h.controller.start()
    h.controller.pause()
    h.controller.setSpeed(50)
    expect(h.sources).toHaveLength(1)
    h.controller.resume()
    expect(h.sources[1].url).toMatch(/speed=50&from_seq=0$/)
  })

  it('stopping closes the stream for good', () => {
    const h = harness()
    h.controller.start()
    h.controller.stop()
    expect(h.sources[0].closed).toBe(true)
    h.controller.resume()
    expect(h.sources).toHaveLength(1)
  })

  it('a stream that breaks is reported, and resume picks up where it broke', () => {
    const h = harness()
    h.controller.start()
    for (const e of EVENTS.slice(0, 5)) h.sources[0].send(e)
    h.sources[0].onerror?.(new Event('error'))
    expect(h.phases.at(-1)).toBe('broken')
    h.controller.resume()
    expect(h.sources[1].url).toMatch(/from_seq=5$/)
  })
})
