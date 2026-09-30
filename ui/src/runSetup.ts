// What the Start a Run screen knows before a Run starts: the Pillar names, the
// last Run on the same setup, and what a new Run is likely to cost. Everything
// here is read from Run Records the server already lists; nothing is stored.
import pillarsConfig from '../../config/pillars.json'
import type { CorpusDocument, CorpusSummary, RunRecord } from './types'

type PillarEntry = { name: string; indicator_ids: string[] }

const PILLARS: Record<string, PillarEntry> = Object.fromEntries(
  Object.entries(pillarsConfig as Record<string, unknown>).filter(
    ([k, v]) => /^\d+$/.test(k) && typeof v === 'object' && v !== null,
  ),
) as Record<string, PillarEntry>

/** The Pillar's official name, from the same registry file the server reads.
 *  null for a Pillar the file does not name. */
export function pillarName(pillar: number): string | null {
  return PILLARS[String(pillar)]?.name ?? null
}

/** How many Indicators a Pillar holds, for scaling an estimate. */
function pillarIndicatorCount(pillar: number): number {
  return PILLARS[String(pillar)]?.indicator_ids.length ?? 0
}

export interface Setup {
  economy: string
  pillars: number[]
  /** null means every Indicator of the Pillars */
  indicators: string[] | null
  engine: string
}

function samePillars(a: number[], b: number[]): boolean {
  if (a.length !== b.length) return false
  const x = [...a].sort((p, q) => p - q)
  const y = [...b].sort((p, q) => p - q)
  return x.every((v, i) => v === y[i])
}

function sameIndicators(a: string[] | null, b: string[] | null): boolean {
  if (a === null || b === null) return a === b
  return a.length === b.length && [...a].sort().join('|') === [...b].sort().join('|')
}

/** One word per Run outcome, the same on every screen: the Run view and its
 *  replay say Finished and Stopped, so Run history and Comparison do too.
 *  Interrupted is its own outcome: the process that owned the Run ended
 *  before the Run could say how it went. */
export const RUN_STATUS_WORD: Record<RunRecord['status'], string> = {
  completed: 'Finished',
  failed: 'Stopped',
  running: 'Running',
  interrupted: 'Interrupted',
}

function isFinishedRun(r: RunRecord): boolean {
  return r.kind === 'run' && r.status === 'completed'
}

/** The newest completed Run on exactly this setup: same Economy, Pillars,
 *  Indicators and Engine. The list arrives newest first. */
export function lastRunOnSetup(runs: RunRecord[], s: Setup): RunRecord | null {
  return (
    runs.find(
      (r) =>
        isFinishedRun(r) &&
        r.economy === s.economy &&
        r.engine === s.engine &&
        samePillars(r.pillars, s.pillars) &&
        sameIndicators(r.indicators, s.indicators),
    ) ?? null
  )
}

/** Mappings a Run proved, as its Run Record counts them. */
export function mappingsOf(r: RunRecord): number | null {
  const n = r.details?.n_passed
  return typeof n === 'number' ? n : null
}

// A non-breaking space keeps a number and its unit on one line.
const NB = '\u00a0'

export function tookOf(r: RunRecord): string | null {
  if (!r.started_at || !r.ended_at) return null
  const s = (new Date(r.ended_at).getTime() - new Date(r.started_at).getTime()) / 1000
  if (!Number.isFinite(s) || s < 0) return null
  if (s < 60) return `${Math.round(s)}${NB}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}${NB}min ${Math.round(s % 60)}${NB}s`
  return `${Math.floor(m / 60)}${NB}h ${m % 60}${NB}min`
}

export function usd(x: number): string {
  if (x === 0) return `USD${NB}0`
  if (x < 0.01) return `under USD${NB}0.01`
  return `USD${NB}${x.toFixed(2)}`
}

export function shortDate(ts: string | null): string {
  if (!ts) return ''
  const d = new Date(ts)
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  return Number.isNaN(d.getTime()) ? ts : `${d.getDate()}${NB}${months[d.getMonth()]}`
}

/** How many Indicators a Run covered: its own list, or every Indicator of its
 *  Pillars. */
function indicatorsCovered(r: { pillars: number[]; indicators: string[] | null }): number {
  if (r.indicators && r.indicators.length) return r.indicators.length
  return r.pillars.reduce((n, p) => n + pillarIndicatorCount(p), 0)
}

function documentsOf(r: RunRecord): number | null {
  const docs = r.details?.documents
  return Array.isArray(docs) && docs.length > 0 ? docs.length : null
}

export interface Estimate {
  /** null when there is nothing to estimate from */
  usd: number | null
  /** What the number is based on, in plain words; follows "About USD x, ". */
  basis: string
}

/** What a Run cost: the provider's bill when the Run Record has it, else our
 *  own meter (the tokens times the declared prices). */
export function costOf(r: RunRecord): number {
  return r.provider_cost_usd !== null && r.provider_cost_usd > 0
    ? r.provider_cost_usd
    : r.cost_usd
}

/** Where a set of costs came from, as a phrase. */
function sourceOf(rs: RunRecord[]): string {
  const billed = rs.filter((r) => r.provider_cost_usd !== null && r.provider_cost_usd > 0).length
  if (billed === rs.length) return 'what the provider billed'
  if (billed === 0) return "our own meter (the provider's bill was not recorded)"
  return "what the provider billed, or our own meter where the bill was not recorded"
}

/** What a Run on this setup is likely to cost, from the Run Records of the
 *  same Engine. In order of trust: the last Run of this exact setup, the same
 *  Economy and Pillars with other Indicators, each Pillar run on its own, this
 *  Economy's other Runs per Indicator, then every Run of this Engine per
 *  Document and Indicator. */
export function estimateCost(
  runs: RunRecord[],
  s: Setup,
  corpusDocuments: number | undefined,
  economyName: string,
): Estimate {
  const paid = runs.filter((r) => isFinishedRun(r) && r.engine === s.engine && costOf(r) > 0)
  const want = indicatorsCovered(s)

  // 1. This exact setup: Economy, Pillars, Indicators and Engine. The same
  // Run the Start a Run screen names as "Last time on this setup", even when
  // it cost nothing, so the two never disagree.
  const exact = lastRunOnSetup(runs, s)
  if (exact) {
    return {
      usd: costOf(exact),
      basis: `${sourceOf([exact])} for the last Run of this setup (${shortDate(exact.started_at)}).`,
    }
  }

  // 2. The same Economy and Pillars, other Indicators: scaled per Indicator.
  const samePillar = paid.find(
    (r) => r.economy === s.economy && samePillars(r.pillars, s.pillars),
  )
  if (samePillar && indicatorsCovered(samePillar) > 0) {
    const had = indicatorsCovered(samePillar)
    return {
      usd: (costOf(samePillar) * want) / had,
      basis: `scaled from ${had} to ${want} Indicators, from ${sourceOf([samePillar])} for the last Run of this Economy and Pillar (${shortDate(samePillar.started_at)}). Treat it as rough.`,
    }
  }

  // 3. Several Pillars at once, each run before on its own: their sum.
  if (s.pillars.length > 1 && s.indicators === null) {
    const parts = s.pillars.map((p) =>
      paid.find(
        (r) => r.economy === s.economy && samePillars(r.pillars, [p]) && r.indicators === null,
      ),
    )
    if (parts.every((r) => r !== undefined)) {
      const found = parts as RunRecord[]
      return {
        usd: found.reduce((sum, r) => sum + costOf(r), 0),
        basis: `${sourceOf(found)} for the last Run of each of these Pillars on this Economy, added together.`,
      }
    }
  }

  // 4. The same Economy, other Pillars: cost per Indicator.
  const sameEconomy = paid.filter((r) => r.economy === s.economy && indicatorsCovered(r) > 0)
  if (sameEconomy.length > 0) {
    const perIndicator =
      sameEconomy.reduce((sum, r) => sum + costOf(r) / indicatorsCovered(r), 0) /
      sameEconomy.length
    const n = sameEconomy.length
    return {
      usd: perIndicator * want,
      basis: `scaled to ${want} Indicators from ${sourceOf(sameEconomy)} for ${n} earlier Run${n === 1 ? '' : 's'} of ${economyName} on this Engine. Treat it as rough.`,
    }
  }

  // 5. Any Economy on this Engine: cost per Document and Indicator.
  const scalable = paid.filter((r) => documentsOf(r) !== null && indicatorsCovered(r) > 0)
  if (scalable.length > 0 && corpusDocuments && corpusDocuments > 0) {
    const rate =
      scalable.reduce(
        (sum, r) => sum + costOf(r) / ((documentsOf(r) as number) * indicatorsCovered(r)),
        0,
      ) / scalable.length
    const n = scalable.length
    return {
      usd: rate * corpusDocuments * want,
      basis: `a rough scaling by ${corpusDocuments} Documents and ${want} Indicators from ${sourceOf(scalable)} for ${n} earlier Run${n === 1 ? '' : 's'} of this Engine on other Economies. Documents differ in length, so the real cost can be well off.`,
    }
  }

  return {
    usd: null,
    basis: 'There is no earlier Run of this Engine to estimate from, so the cost is not known in advance.',
  }
}

/** The Economies to show first: those with Documents whose Corpus has been
 *  run to completion on every one of the given Engines (the two declared
 *  Engines), so a stray test Run on another Engine never promotes one. On a
 *  database with none, the Economies with Documents; with none of those, all. */
export function featuredEconomies(
  economies: string[],
  runs: RunRecord[],
  counts: Record<string, number>,
  engines: string[],
): string[] {
  const withDocs = economies.filter((c) => (counts[c] ?? 0) > 0)
  const ranOnAll = withDocs.filter((c) =>
    engines.every((e) =>
      runs.some((r) => isFinishedRun(r) && r.economy === c && r.engine === e),
    ),
  )
  if (engines.length > 0 && ranOnAll.length > 0) return ranOnAll
  return withDocs.length > 0 ? withDocs : economies
}

/** One Economy's Corpus in the shape the server's summary gives it, from the
 *  Corpus listing itself: a fresher count once "Add document" has read it. */
export function corpusSummaryOf(documents: CorpusDocument[]): CorpusSummary {
  const counts = new Map<string, number>()
  let unrecorded = 0
  for (const d of documents) {
    const language = (d.language ?? '').trim()
    if (language) counts.set(language, (counts.get(language) ?? 0) + 1)
    else unrecorded += 1
  }
  const languages = [...counts.keys()].sort((a, b) => counts.get(b)! - counts.get(a)! || a.localeCompare(b))
  return { n: documents.length, languages, unrecorded }
}

/** What an Economy card says under its name: the Corpus now, in the languages
 *  its own Documents are. A Document with no language recorded is never given
 *  one. undefined is "not counted yet", which is not the same as empty. */
export function economyCardLine(summary: CorpusSummary | undefined): string {
  if (summary === undefined) return 'Not counted yet'
  const { n, languages, unrecorded } = summary
  if (n === 0) return 'No Documents yet'
  const docs = `${n} Document${n === 1 ? '' : 's'}`
  if (languages.length === 0) return `${docs}. Language not recorded`
  const named = languages.join(', ')
  return unrecorded > 0 ? `${docs}. ${named}; ${unrecorded} with no language recorded` : `${docs}. ${named}`
}

/** Which way in the empty-Corpus notice names. A Portal crawler Discovers
 *  whatever is ticked; an Economy with no crawler still Discovers one Pillar
 *  at a time from official addresses; a manual-only Economy has only Add
 *  document, since its Portal's rules forbid any automated request. */
export type EmptyCorpusWay = 'discover' | 'discover-pillar' | 'tick-one-pillar' | 'add-only'

export function emptyCorpusWayIn(e: {
  manualOnly: boolean
  noDiscovery: boolean
  pillarsTicked: number
}): EmptyCorpusWay {
  if (e.manualOnly) return 'add-only'
  if (!e.noDiscovery) return 'discover'
  return e.pillarsTicked === 1 ? 'discover-pillar' : 'tick-one-pillar'
}
