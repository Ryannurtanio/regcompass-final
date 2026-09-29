import { describe, expect, it } from 'vitest'
import {
  RUN_STATUS_WORD,
  corpusSummaryOf,
  economyCardLine,
  estimateCost,
  lastRunOnSetup,
  type Setup,
} from './runSetup'
import type { CorpusDocument, RunRecord } from './types'

function run(over: Partial<RunRecord>): RunRecord {
  return {
    run_id: 'r',
    kind: 'run',
    economy: 'SG',
    pillars: [7],
    indicators: null,
    engine: 'engine_b',
    status: 'completed',
    started_at: '2026-09-23T08:00:00Z',
    ended_at: '2026-09-23T08:30:00Z',
    documents_fetched: 0,
    prompt_tokens: 0,
    completion_tokens: 0,
    cost_usd: 0,
    provider_cost_usd: null,
    error: null,
    details: {},
    ...over,
  }
}

const setup: Setup = { economy: 'SG', pillars: [6, 7], indicators: null, engine: 'engine_b' }

describe('the estimate and the last Run on this setup', () => {
  it('rest on the same Run when this exact setup has run, even at no cost', () => {
    const runs = [
      run({ run_id: 'both', pillars: [6, 7], cost_usd: 0 }),
      run({ run_id: 'p7', pillars: [7], cost_usd: 0.2 }),
    ]
    expect(lastRunOnSetup(runs, setup)?.run_id).toBe('both')
    const e = estimateCost(runs, setup, 10, 'Singapore')
    expect(e.usd).toBe(0)
    expect(e.basis).toContain('the last Run of this setup')
  })

  it('say there is no Run of this setup when the estimate is added up from others', () => {
    const runs = [
      run({ run_id: 'p6', pillars: [6], provider_cost_usd: 0.1, cost_usd: 0.07 }),
      run({ run_id: 'p7', pillars: [7], provider_cost_usd: 0.18, cost_usd: 0.12 }),
    ]
    expect(lastRunOnSetup(runs, setup)).toBeNull()
    const e = estimateCost(runs, setup, 10, 'Singapore')
    expect(e.usd).toBeCloseTo(0.28)
    expect(e.basis).toContain('each of these Pillars')
  })
})

describe('Run outcome words', () => {
  it('match the Run view: Finished and Stopped, and Interrupted kept apart', () => {
    expect(RUN_STATUS_WORD.completed).toBe('Finished')
    expect(RUN_STATUS_WORD.failed).toBe('Stopped')
    expect(RUN_STATUS_WORD.interrupted).toBe('Interrupted')
  })
})

describe('an Economy card tells the Corpus as it is now', () => {
  const docOf = (language: string | null): CorpusDocument =>
    ({ document_id: `d${Math.random()}`, title: 't', language }) as CorpusDocument

  it("counts the Corpus's own Documents and lists their languages, most common first", () => {
    const s = corpusSummaryOf([docOf('Hindi'), docOf('English'), docOf('English'), docOf(' ')])
    expect(s).toEqual({ n: 4, languages: ['English', 'Hindi'], unrecorded: 1 })
  })

  it('says the count and the languages, and nothing about a last Run', () => {
    expect(economyCardLine({ n: 4, languages: ['English'], unrecorded: 0 })).toBe('4 Documents. English')
    expect(economyCardLine({ n: 1, languages: ['Chinese'], unrecorded: 0 })).toBe('1 Document. Chinese')
    expect(economyCardLine({ n: 3, languages: ['English', 'Malay'], unrecorded: 0 })).toBe('3 Documents. English, Malay')
  })

  it('never gives a Document a language it was not recorded with', () => {
    expect(economyCardLine({ n: 2, languages: [], unrecorded: 2 })).toBe('2 Documents. Language not recorded')
    expect(economyCardLine({ n: 3, languages: ['English'], unrecorded: 1 })).toBe(
      '3 Documents. English; 1 with no language recorded',
    )
  })

  it('an empty Corpus, and one not counted yet, are told apart', () => {
    expect(economyCardLine({ n: 0, languages: [], unrecorded: 0 })).toBe('No Documents yet')
    expect(economyCardLine(undefined)).toBe('Not counted yet')
  })
})
