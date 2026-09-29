import { describe, expect, it } from 'vitest'
import recorded from '../../tests/fixtures/run_events/run_20260923T041241Z_a07249.jsonl?raw'
import { LiveStream, RECONNECT_DELAYS, type LivePhase, type LiveSource } from './liveStream'
import { emptyRunView, runViewReducer } from './runViewState'
import type { RunEvent } from './types'

const EVENTS: RunEvent[] = recorded
  .split('\n')
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as RunEvent)

/** A stand-in for the browser's EventSource: the test pushes into it. */
class FakeSource implements LiveSource {
  closed = false
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>()
  constructor(public url: string) {}
  addEventListener(name: string, fn: (ev: MessageEvent) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), fn])
  }
  close() {
    this.closed = true
  }
  send(event: RunEvent) {
    for (const fn of this.listeners.get(event.type) ?? []) fn({ data: JSON.stringify(event) } as MessageEvent)
  }
  line(msg: string) {
    this.onmessage?.({ data: JSON.stringify({ ts: 'x', msg }) } as MessageEvent)
  }
  end(status = 'done') {
    for (const fn of this.listeners.get('end') ?? []) fn({ data: JSON.stringify({ status, run_id: 'run_x' }) } as MessageEvent)
  }
  drop() {
    this.onerror?.(new Event('error'))
  }
}

function harness() {
  const sources: FakeSource[] = []
  const timers: { fn: () => void; ms: number; cancelled: boolean }[] = []
  let state = emptyRunView
  let lines: string[] = []
  const phases: LivePhase[] = []
  const ends: string[] = []
  let drops = 0
  const stream = new LiveStream({
    open: (url) => {
      const s = new FakeSource(url)
      sources.push(s)
      return s
    },
    schedule: (fn, ms) => {
      const t = { fn, ms, cancelled: false }
      timers.push(t)
      return () => {
        t.cancelled = true
      }
    },
    onLine: (msg) => {
      lines = [...lines, msg]
    },
    onRunEvent: (event) => {
      state = runViewReducer(state, { type: 'event', event })
    },
    onDiscoveryEvent: () => undefined,
    onReset: () => {
      lines = []
      state = runViewReducer(state, { type: 'reset' })
    },
    onEnd: (data) => ends.push(data),
    onPhase: (p) => phases.push(p),
    onDrop: () => {
      drops += 1
    },
  })
  const fire = () => {
    const t = timers.at(-1)!
    if (!t.cancelled) t.fn()
  }
  return { stream, sources, timers, fire, phases, ends, drops: () => drops, state: () => state, lines: () => lines }
}

describe('following a live Run', () => {
  it('listens on the live stream and is live at once', () => {
    const h = harness()
    h.stream.start()
    expect(h.sources[0].url).toBe('/api/events')
    expect(h.phases).toEqual(['live'])
  })

  it('a dropped stream is not the end of the Run: it says reconnecting and asks again', () => {
    const h = harness()
    h.stream.start()
    for (const e of EVENTS.slice(0, 20)) h.sources[0].send(e)
    h.sources[0].drop()
    expect(h.sources[0].closed).toBe(true)
    expect(h.phases.at(-1)).toBe('reconnecting')
    expect(h.ends).toEqual([])
    expect(h.drops()).toBe(1)
    // The picture so far stays on screen while it waits.
    expect(h.state().status).toBe('running')
    expect(h.timers[0].ms).toBe(RECONNECT_DELAYS[0])
    h.fire()
    expect(h.sources).toHaveLength(2)
    expect(h.sources[1].url).toBe('/api/events')
  })

  it('the reconnected stream replays from the first event without doubling anything', () => {
    const h = harness()
    h.stream.start()
    h.sources[0].line('M0 start')
    for (const e of EVENTS.slice(0, 20)) h.sources[0].send(e)
    h.sources[0].drop()
    h.fire()
    // The server replays the whole job, then carries on.
    h.sources[1].line('M0 start')
    for (const e of EVENTS) h.sources[1].send(e)
    expect(h.phases.at(-1)).toBe('live')
    h.sources[1].end()
    expect(h.phases.at(-1)).toBe('ended')
    expect(h.ends).toHaveLength(1)
    expect(h.lines()).toEqual(['M0 start'])
    const { events: _a, ...followed } = h.state()
    const { events: _b, ...whole } = EVENTS.reduce(
      (s, event) => runViewReducer(s, { type: 'event', event }),
      emptyRunView,
    )
    expect(followed).toEqual(whole)
  })

  it('waits longer after each failed attempt, and starts over once it is back', () => {
    const h = harness()
    h.stream.start()
    h.sources[0].drop()
    h.fire()
    h.sources[1].drop()
    h.fire()
    h.sources[2].drop()
    expect(h.timers.map((t) => t.ms)).toEqual([...RECONNECT_DELAYS.slice(0, 3)])
    h.fire()
    h.sources[3].send(EVENTS[0])
    h.sources[3].drop()
    expect(h.timers.at(-1)!.ms).toBe(RECONNECT_DELAYS[0])
  })

  it('only the end event ends the Run', () => {
    const h = harness()
    h.stream.start()
    for (let i = 0; i < 12; i++) {
      h.sources.at(-1)!.drop()
      h.fire()
    }
    expect(h.ends).toEqual([])
    expect(h.timers.at(-1)!.ms).toBe(RECONNECT_DELAYS.at(-1))
    h.sources.at(-1)!.end()
    expect(h.ends).toHaveLength(1)
    expect(h.phases.at(-1)).toBe('ended')
  })

  it('an old stream that speaks after a drop is ignored', () => {
    const h = harness()
    h.stream.start()
    h.sources[0].drop()
    h.sources[0].send(EVENTS[0])
    h.sources[0].end()
    expect(h.ends).toEqual([])
    expect(h.state()).toBe(emptyRunView)
  })

  it('stopping cancels the next attempt and closes the stream', () => {
    const h = harness()
    h.stream.start()
    h.sources[0].drop()
    h.stream.stop()
    h.fire()
    expect(h.sources).toHaveLength(1)
    expect(h.phases.at(-1)).toBe('stopped')
  })
})
