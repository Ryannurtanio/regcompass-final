import { describe, expect, it } from 'vitest'
import {
  bothEngines,
  choiceFor,
  choiceSearch,
  enginesFor,
  hasChoice,
  parseChoice,
  pillarKey,
  pillarSetsFor,
  pillarText,
  resolveRun,
  switchEngine,
} from './evidenceChoice'
import type { EvidenceEconomy } from './types'

// As the server lists them: newest Economy first, and within each the newest
// Run per Engine and set of Pillars.
const listing: EvidenceEconomy[] = [
  {
    economy: 'MY',
    name: 'Malaysia',
    newest_run_id: 'run_my_b',
    runs: [{ run_id: 'run_my_b', engine: 'b', pillars: [6, 7], started_at: '2026-09-24' }],
  },
  {
    economy: 'ID',
    name: 'Indonesia',
    newest_run_id: 'run_id_a',
    runs: [
      { run_id: 'run_id_a', engine: 'a', pillars: [6, 7], started_at: '2026-09-23' },
      { run_id: 'run_id_b', engine: 'b', pillars: [6, 7], started_at: '2026-09-22' },
      { run_id: 'run_id_b7', engine: 'b', pillars: [7], started_at: '2026-09-21' },
    ],
  },
]

describe('resolveRun', () => {
  it('opens on the newest Run of all when nothing is chosen', () => {
    expect(resolveRun(listing, {})).toBe('run_my_b')
  })
  it("opens on the chosen Economy's newest Run", () => {
    expect(resolveRun(listing, { economy: 'ID' })).toBe('run_id_a')
  })
  it('narrows by Engine, then by Pillars', () => {
    expect(resolveRun(listing, { economy: 'ID', engine: 'b' })).toBe('run_id_b')
    expect(resolveRun(listing, { economy: 'ID', engine: 'b', pillars: '7' })).toBe('run_id_b7')
  })
  it('lets go of a part that matches nothing rather than showing nothing', () => {
    expect(resolveRun(listing, { economy: 'MY', engine: 'a' })).toBe('run_my_b')
    expect(resolveRun(listing, { economy: 'ID', engine: 'a', pillars: '7' })).toBe('run_id_a')
    expect(resolveRun(listing, { economy: 'ZZ' })).toBe('run_my_b')
  })
  it('keeps an explicit Run from Run history', () => {
    expect(resolveRun(listing, { economy: 'ID', run: 'run_older' })).toBe('run_older')
  })
  it('has nothing to open with no finished Runs', () => {
    expect(resolveRun([], { economy: 'ID' })).toBeNull()
  })
})

describe('switchEngine', () => {
  const china: EvidenceEconomy[] = [
    {
      economy: 'CN',
      name: 'China',
      newest_run_id: 'run_cn_a67',
      runs: [
        { run_id: 'run_cn_a67', engine: 'a', pillars: [6, 7], started_at: '2026-09-25' },
        { run_id: 'run_cn_a7', engine: 'a', pillars: [7], started_at: '2026-09-24' },
        { run_id: 'run_cn_b67', engine: 'b', pillars: [6, 7], started_at: '2026-09-23' },
        { run_id: 'run_cn_b7', engine: 'b', pillars: [7], started_at: '2026-09-22' },
        { run_id: 'run_cn_b6', engine: 'b', pillars: [6], started_at: '2026-09-21' },
      ],
    },
  ]
  it('keeps the Pillars when the other Engine has a Run for them', () => {
    expect(switchEngine(china, { economy: 'CN', pillars: [7] }, 'b')).toBe('run_cn_b7')
    expect(switchEngine(china, { economy: 'CN', pillars: [6, 7] }, 'b')).toBe('run_cn_b67')
  })
  it("falls back to the Engine's newest Run when the Pillars have none", () => {
    expect(switchEngine(china, { economy: 'CN', pillars: [6] }, 'a')).toBe('run_cn_a67')
  })
})

describe('the pickers', () => {
  it('lists the Engines and Pillars an Economy has Runs for', () => {
    expect(enginesFor(listing, 'ID')).toEqual(['a', 'b'])
    expect(enginesFor(listing, 'MY')).toEqual(['b'])
    expect(pillarSetsFor(listing, 'ID', 'b')).toEqual([[6, 7], [7]])
    expect(pillarSetsFor(listing, 'ID', 'a')).toEqual([[6, 7]])
  })
  it('offers the Comparison only when both Engines ran the Economy', () => {
    expect(bothEngines(listing, 'ID')).toBe(true)
    expect(bothEngines(listing, 'MY')).toBe(false)
  })
  it('spells Pillars the same way everywhere', () => {
    expect(pillarKey([7, 6])).toBe('6,7')
    expect(pillarText([7, 6])).toBe('Pillar 6, 7')
  })
})

describe('the address', () => {
  const record = (run_id: string, engine: string, pillars: number[]) => ({
    run_id,
    economy: 'ID',
    engine,
    pillars,
  })

  it('round-trips a choice through the query string', () => {
    const choice = choiceFor(listing, record('run_id_b7', 'b', [7]))
    expect(choice).toEqual({ economy: 'ID', engine: 'b', pillars: '7' })
    const search = choiceSearch('', choice)
    expect(search).toBe('?economy=ID&engine=b&pillars=7')
    expect(resolveRun(listing, parseChoice(search))).toBe('run_id_b7')
  })

  it('keeps the Run id only for a Run that is not the newest of its kind', () => {
    const older = choiceFor(listing, record('run_id_b_old', 'b', [6, 7]))
    expect(older.run).toBe('run_id_b_old')
    expect(resolveRun(listing, parseChoice(choiceSearch('', older)))).toBe('run_id_b_old')
  })

  it('leaves other parameters alone and can take the choice out again', () => {
    const search = choiceSearch('?next=1', { economy: 'MY' })
    expect(search).toBe('?next=1&economy=MY')
    expect(choiceSearch(search, null)).toBe('?next=1')
    expect(choiceSearch('?economy=MY', null)).toBe('')
  })

  it('says whether an address names any evidence', () => {
    expect(hasChoice(parseChoice('?economy=ID'))).toBe(true)
    expect(hasChoice(parseChoice('?run=run_x'))).toBe(true)
    expect(hasChoice(parseChoice('?next=1'))).toBe(false)
  })
})
