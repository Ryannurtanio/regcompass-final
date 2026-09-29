// Watching a recorded Run again. The server streams the Run's recorded events
// in the live stream's own format (/api/runs/<id>/replay), paced at a chosen
// speed; the screen feeds them to the same reducer a live Run feeds. Pausing
// closes the stream and resuming asks for the rest from the next event, so a
// paused screen truly stands still and nothing piles up behind it.

import { RUN_EVENT_TYPES, parseRunEvent } from './api'
import type { RunEvent } from './types'

/** The speeds offered, as multiples of the Run's own pace. */
export const REPLAY_SPEEDS = [10, 20, 50, 100] as const
export const DEFAULT_REPLAY_SPEED = 20

export const replayUrl = (runId: string, speed: number, fromSeq: number) =>
  `/api/runs/${encodeURIComponent(runId)}/replay?speed=${speed}&from_seq=${fromSeq}`

/** What an EventSource offers that the replay uses; a test supplies its own. */
export interface ReplaySource {
  addEventListener(name: string, fn: (ev: MessageEvent) => void): void
  close(): void
  onerror: ((ev: Event) => void) | null
}

export type ReplayPhase = 'playing' | 'paused' | 'finished' | 'broken' | 'stopped'

export interface ReplayOptions {
  runId: string
  speed: number
  onEvent: (event: RunEvent) => void
  onPhase: (phase: ReplayPhase) => void
  open?: (url: string) => ReplaySource
}

export class ReplayController {
  private source: ReplaySource | null = null
  private nextSeq = 0
  private phase: ReplayPhase | null = null
  private speed: number
  private readonly open: (url: string) => ReplaySource

  constructor(private readonly options: ReplayOptions) {
    this.speed = options.speed
    this.open = options.open ?? ((url) => new EventSource(url))
  }

  start(): void {
    this.nextSeq = 0
    this.connect()
  }

  pause(): void {
    if (this.phase !== 'playing') return
    this.disconnect()
    this.setPhase('paused')
  }

  resume(): void {
    if (this.phase !== 'paused' && this.phase !== 'broken') return
    this.connect()
  }

  setSpeed(speed: number): void {
    this.speed = speed
    if (this.phase === 'playing') {
      this.disconnect()
      this.connect()
    }
  }

  stop(): void {
    this.disconnect()
    this.setPhase('stopped')
  }

  private setPhase(phase: ReplayPhase): void {
    this.phase = phase
    this.options.onPhase(phase)
  }

  private connect(): void {
    const source = this.open(replayUrl(this.options.runId, this.speed, this.nextSeq))
    this.source = source
    const live = () => this.source === source
    for (const name of RUN_EVENT_TYPES) {
      source.addEventListener(name, (ev) => {
        if (!live()) return
        const event = parseRunEvent(name, ev.data)
        if (!event) return
        this.nextSeq = Math.max(this.nextSeq, event.seq + 1)
        this.options.onEvent(event)
      })
    }
    source.addEventListener('end', () => {
      if (!live()) return
      this.disconnect()
      this.setPhase('finished')
    })
    source.onerror = () => {
      if (!live()) return
      this.disconnect()
      this.setPhase('broken')
    }
    this.setPhase('playing')
  }

  private disconnect(): void {
    const source = this.source
    this.source = null
    source?.close()
  }
}

const NOT_RECORDED: Record<string, string> = {
  gate_scores:
    "The Gate's scores per Candidate were not recorded for this Run, so only how many Candidates each Document kept is shown.",
  step_times:
    "Step times were not recorded either, so the times below are estimated from the Run's start, its end and the durations its log gave.",
}

/** What a Run's record never kept, in plain sentences for the screen. */
export function notRecordedSentences(keys: string[]): string[] {
  return keys.map((k) => NOT_RECORDED[k] ?? `This Run's record does not keep ${k.replace(/_/g, ' ')}.`)
}
