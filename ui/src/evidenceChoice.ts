// Which Run the Evidence screen shows, chosen the way a reviewer thinks about
// it: an Economy, then an Engine, then (only when they differ) the Pillars.
// The server lists every Economy with a finished Run, newest first, and within
// it the newest Run per Engine and set of Pillars. These helpers turn a choice
// into a Run id, a Run back into a choice, and a choice into the page address,
// so a reload or a shared link opens the same evidence.

import type { EvidenceEconomy, RunRecord } from './types'

/** What the address can carry. `run` is only there when the reviewer opened
 *  one particular Run (from Run history) that is not the newest of its kind. */
export interface EvidenceChoice {
  economy?: string | null
  engine?: string | null
  pillars?: string | null
  run?: string | null
}

const KEYS = ['economy', 'engine', 'pillars', 'run'] as const

/** Pillars as the address and the pickers spell them: "6,7". */
export function pillarKey(pillars: readonly number[]): string {
  return [...pillars].sort((a, b) => a - b).join(',')
}

/** "Pillar 6, 7", as the rest of the interface names a Run's Pillars. */
export function pillarText(pillars: readonly number[]): string {
  return pillars.length ? `Pillar ${[...pillars].sort((a, b) => a - b).join(', ')}` : 'no Pillar'
}

function economyOf(listing: readonly EvidenceEconomy[], code?: string | null) {
  return (code ? listing.find((e) => e.economy === code) : undefined) ?? null
}

/** The Engines that have a finished Run for this Economy, newest first. */
export function enginesFor(
  listing: readonly EvidenceEconomy[],
  economy: string | null | undefined,
): (string | null)[] {
  const entry = economyOf(listing, economy)
  if (!entry) return []
  const out: (string | null)[] = []
  for (const r of entry.runs) if (!out.includes(r.engine)) out.push(r.engine)
  return out
}

/** The sets of Pillars this Economy's Engine has a Run for, newest first. */
export function pillarSetsFor(
  listing: readonly EvidenceEconomy[],
  economy: string | null | undefined,
  engine: string | null | undefined,
): number[][] {
  const entry = economyOf(listing, economy)
  if (!entry) return []
  return entry.runs.filter((r) => r.engine === (engine ?? null)).map((r) => r.pillars)
}

/** Both Engines have a Run for this Economy, so the two can be compared. */
export function bothEngines(
  listing: readonly EvidenceEconomy[],
  economy: string | null | undefined,
): boolean {
  return enginesFor(listing, economy).filter((e) => e !== null).length >= 2
}

/** The Run a choice means. An explicit Run wins. Otherwise the Economy's
 *  newest Run, narrowed by Engine and then Pillars where those are given and
 *  still exist; a part that no longer matches anything is let go rather than
 *  leaving the screen empty. No Economy named means the newest Run of all. */
export function resolveRun(
  listing: readonly EvidenceEconomy[],
  choice: EvidenceChoice,
): string | null {
  if (choice.run) return choice.run
  const entry = economyOf(listing, choice.economy) ?? listing[0] ?? null
  if (!entry || entry.runs.length === 0) return null
  let runs = entry.runs
  if (choice.engine !== undefined && choice.engine !== null) {
    const same = runs.filter((r) => r.engine === choice.engine)
    if (same.length) runs = same
  }
  if (choice.pillars) {
    const same = runs.filter((r) => pillarKey(r.pillars) === choice.pillars)
    if (same.length) runs = same
  }
  return runs[0].run_id
}

/** The Run to open when the reviewer switches Engine: the same Economy and,
 *  where the new Engine has a Run for them, the same Pillars. */
export function switchEngine(
  listing: readonly EvidenceEconomy[],
  current: { economy: string; pillars: readonly number[] },
  engine: string | null,
): string | null {
  return resolveRun(listing, {
    economy: current.economy,
    engine,
    pillars: pillarKey(current.pillars),
  })
}

/** The choice a Run stands for, for the pickers and the address. The Run id
 *  is only kept when the choice alone would open a different, newer Run. */
export function choiceFor(
  listing: readonly EvidenceEconomy[],
  record: Pick<RunRecord, 'run_id' | 'economy' | 'engine' | 'pillars'>,
): EvidenceChoice {
  const choice: EvidenceChoice = {
    economy: record.economy,
    engine: record.engine,
    pillars: pillarKey(record.pillars),
  }
  if (resolveRun(listing, choice) !== record.run_id) choice.run = record.run_id
  return choice
}

/** The choice carried by a page address (its query string). */
export function parseChoice(search: string): EvidenceChoice {
  const params = new URLSearchParams(search)
  const choice: EvidenceChoice = {}
  for (const k of KEYS) {
    const v = params.get(k)
    if (v) choice[k] = v
  }
  return choice
}

/** Whether an address names any evidence at all. */
export function hasChoice(choice: EvidenceChoice): boolean {
  return KEYS.some((k) => Boolean(choice[k]))
}

/** The query string with this choice written in (or, given null, taken out),
 *  leaving any other parameter as it was. Empty when nothing is left. */
export function choiceSearch(search: string, choice: EvidenceChoice | null): string {
  const params = new URLSearchParams(search)
  for (const k of KEYS) {
    const v = choice?.[k]
    if (v) params.set(k, v)
    else params.delete(k)
  }
  const out = params.toString()
  return out ? `?${out}` : ''
}
