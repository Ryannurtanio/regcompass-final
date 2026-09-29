// Following a Run or a Discovery as it happens, over /api/events. On flaky
// wifi the stream drops while the job carries on on the server, so a dropped
// stream is never read as the end of the job: the panel says it is
// reconnecting, asks again after a pause, and only the server's own `end`
// event ends it. The server replays every line and event of the job from the
// first on each connection, so a reconnected stream starts the picture over
// from empty rather than doubling it.

import {
  DISCOVERY_EVENT_TYPES,
  RUN_EVENT_TYPES,
  parseDiscoveryEvent,
  parseRunEvent,
} from './api'
import type { DiscoveryEvent, RunEvent } from './types'

export const LIVE_URL = '/api/events'

/** The pauses before each new attempt, in milliseconds; the last repeats. */
export const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000] as const

/** What an EventSource offers that the live stream uses; a test supplies its own. */
export interface LiveSource {
  addEventListener(name: string, fn: (ev: MessageEvent) => void): void
  close(): void
  onmessage: ((ev: MessageEvent) => void) | null
  onerror: ((ev: Event) => void) | null
}

export type LivePhase = 'live' | 'reconnecting' | 'ended' | 'stopped'

export interface LiveOptions {
  onLine: (msg: string) => void
  onRunEvent: (event: RunEvent) => void
  onDiscoveryEvent: (event: DiscoveryEvent) => void
  // A reconnected stream is about to replay the job from its first line.
  onReset: () => void
  // The job's real end: the server's `end` event, with its final status.
  onEnd: (data: string) => void
  onPhase: (phase: LivePhase) => void
  // Called on every drop. An EventSource cannot see a 401, so the panel asks
  // once through a guarded endpoint, and an ended session goes to sign-in.
  onDrop?: () => void
  open?: (url: string) => LiveSource
  // setTimeout, or a test's own clock. Returns a way to cancel.
  schedule?: (fn: () => void, ms: number) => () => void
}

export class LiveStream {
  private source: LiveSource | null = null
  private phase: LivePhase | null = null
  private attempts = 0
  // True from a reconnect until the new stream's first line or event.
  private replaying = false
  private cancelRetry: (() => void) | null = null
  private readonly open: (url: string) => LiveSource
  private readonly schedule: (fn: () => void, ms: number) => () => void

  constructor(private readonly options: LiveOptions) {
    this.open = options.open ?? ((url) => new EventSource(url))
    this.schedule =
      options.schedule ??
      ((fn, ms) => {
        const id = window.setTimeout(fn, ms)
        return () => window.clearTimeout(id)
      })
  }

  start(): void {
    this.attempts = 0
    this.replaying = false
    this.connect()
  }

  /** Stop following, for good: the panel is going away. */
  stop(): void {
    this.cancelRetry?.()
    this.cancelRetry = null
    this.disconnect()
    this.setPhase('stopped')
  }

  private setPhase(phase: LivePhase): void {
    if (this.phase === phase) return
    this.phase = phase
    this.options.onPhase(phase)
  }

  /** Something arrived on the live stream: it is back. */
  private arrived(): void {
    this.attempts = 0
    if (this.replaying) {
      this.replaying = false
      this.options.onReset()
    }
    this.setPhase('live')
  }

  private connect(): void {
    this.cancelRetry = null
    const source = this.open(LIVE_URL)
    this.source = source
    const live = () => this.source === source
    for (const name of RUN_EVENT_TYPES) {
      source.addEventListener(name, (ev) => {
        if (!live()) return
        const event = parseRunEvent(name, ev.data)
        if (!event) return
        this.arrived()
        this.options.onRunEvent(event)
      })
    }
    for (const name of DISCOVERY_EVENT_TYPES) {
      source.addEventListener(name, (ev) => {
        if (!live()) return
        const event = parseDiscoveryEvent(name, ev.data)
        if (!event) return
        this.arrived()
        this.options.onDiscoveryEvent(event)
      })
    }
    source.onmessage = (ev) => {
      if (!live()) return
      let msg: unknown = null
      try {
        msg = JSON.parse(ev.data)?.msg
      } catch {
        return // a keep-alive comment, not a line
      }
      if (typeof msg !== 'string' || !msg) return
      this.arrived()
      this.options.onLine(msg)
    }
    source.addEventListener('end', (ev) => {
      if (!live()) return
      this.disconnect()
      this.replaying = false
      this.setPhase('ended')
      this.options.onEnd(ev.data)
    })
    source.onerror = () => {
      if (!live()) return
      this.disconnect()
      this.replaying = true
      this.setPhase('reconnecting')
      this.options.onDrop?.()
      const delay = RECONNECT_DELAYS[Math.min(this.attempts, RECONNECT_DELAYS.length - 1)]
      this.attempts += 1
      this.cancelRetry = this.schedule(() => this.connect(), delay)
    }
    if (this.phase === null || this.phase === 'ended' || this.phase === 'stopped') this.setPhase('live')
  }

  private disconnect(): void {
    const source = this.source
    this.source = null
    source?.close()
  }
}
