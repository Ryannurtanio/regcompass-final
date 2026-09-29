import { describe, expect, it } from 'vitest'
import { durationText, statusWord } from './runsListText'
import type { RunRecord } from './types'

const record = (over: Partial<RunRecord>): RunRecord =>
  ({
    run_id: 'disc_x',
    kind: 'discovery',
    economy: 'SG',
    pillars: [],
    indicators: null,
    engine: null,
    status: 'completed',
    started_at: '2026-09-28T01:56:49.102482Z',
    ended_at: '2026-09-28T01:58:30.114947Z',
    documents_fetched: 1,
    prompt_tokens: 0,
    completion_tokens: 0,
    cost_usd: 0,
    error: null,
    details: { manual: true },
    ...over,
  }) as RunRecord

describe('durationText', () => {
  it('reads microsecond timestamps as the real time taken', () => {
    expect(durationText('2026-09-28T01:56:49.102482Z', '2026-09-28T01:58:30.114947Z')).toBe(
      '1 min 41 s',
    )
    expect(durationText('2026-09-28T01:56:22.198161Z', '2026-09-28T01:56:25.844553Z')).toBe(
      '3.6 s',
    )
  })

  it('says under 0.1 s rather than 0.0 s for an instant add', () => {
    expect(durationText('2026-09-28T01:58:43.433995Z', '2026-09-28T01:58:43.439766Z')).toBe(
      'under 0.1 s',
    )
  })

  it('shows a dash with no end', () => {
    expect(durationText('2026-09-28T01:58:43Z', null)).toBe('-')
  })
})

describe('statusWord', () => {
  it('calls a refused add Refused', () => {
    expect(statusWord(record({ status: 'failed', details: { manual: true, outcome: 'refused' } }))).toBe(
      'Refused',
    )
  })

  it('keeps Stopped for an add that broke, and for a Run', () => {
    expect(statusWord(record({ status: 'failed', details: { manual: true, outcome: 'failed' } }))).toBe(
      'Stopped',
    )
    expect(statusWord(record({ kind: 'run', status: 'failed', details: {} }))).toBe('Stopped')
    expect(statusWord(record({}))).toBe('Finished')
  })
})
