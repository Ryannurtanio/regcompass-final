import type { EvidenceEconomy, RunRecord } from './types'
import {
  bothEngines,
  enginesFor,
  pillarKey,
  pillarSetsFor,
  pillarText,
  resolveRun,
  switchEngine,
} from './evidenceChoice'

/** The Evidence screen's "Showing" line: which Economy, which Engine and which
 *  Pillars the evidence on screen belongs to, each one a choice. Picking an
 *  Economy opens its newest Run; picking an Engine or Pillars narrows within
 *  it. The Run id stays on the line, quiet, because it is what Run history and
 *  the export name. When both Engines ran the Economy, one line offers the
 *  Comparison. */
export default function EvidenceShowing({
  listing,
  record,
  engineNames,
  onChoose,
  onCompare,
}: {
  listing: EvidenceEconomy[]
  record: RunRecord
  engineNames: Record<string, string>
  onChoose: (runId: string) => void
  onCompare: (economy: string, pillar: number | null) => void
}) {
  const economy = record.economy
  const engines = enginesFor(listing, economy)
  const pillarSets = pillarSetsFor(listing, economy, record.engine)
  const here = pillarKey(record.pillars)
  // The Run on screen may be an older one opened from Run history: it is
  // still offered, so the pickers never claim to show something they do not.
  const economies = listing.some((e) => e.economy === economy)
    ? listing
    : [...listing, { economy, name: economy, newest_run_id: record.run_id, runs: [] }]
  const engineList = engines.includes(record.engine) ? engines : [record.engine, ...engines]
  const pillarList = pillarSets.some((p) => pillarKey(p) === here)
    ? pillarSets
    : [record.pillars, ...pillarSets]
  const engineLabel = (e: string | null) => (e ? engineNames[e] ?? e : 'no Engine')
  const economyName =
    economies.find((e) => e.economy === economy)?.name ?? economy

  const choose = (runId: string | null) => {
    if (runId && runId !== record.run_id) onChoose(runId)
  }

  return (
    <>
      <div className="showing" data-testid="context">
        <span className="k">Showing</span>
        <select
          aria-label="Economy"
          data-testid="showing-economy"
          value={economy}
          onChange={(e) => choose(resolveRun(listing, { economy: e.target.value }))}
        >
          {economies.map((e) => (
            <option key={e.economy} value={e.economy}>
              {e.name}
            </option>
          ))}
        </select>
        <select
          aria-label="Engine"
          data-testid="showing-engine"
          value={record.engine ?? ''}
          disabled={engineList.length < 2}
          onChange={(e) =>
            choose(
              switchEngine(listing, { economy, pillars: record.pillars }, e.target.value || null),
            )
          }
        >
          {engineList.map((e) => (
            <option key={e ?? ''} value={e ?? ''}>
              {engineLabel(e)}
            </option>
          ))}
        </select>
        {pillarList.length > 1 ? (
          <select
            aria-label="Pillars"
            data-testid="showing-pillars"
            value={here}
            onChange={(e) =>
              choose(
                resolveRun(listing, {
                  economy,
                  engine: record.engine,
                  pillars: e.target.value,
                }),
              )
            }
          >
            {pillarList.map((p) => (
              <option key={pillarKey(p)} value={pillarKey(p)}>
                {pillarText(p)}
              </option>
            ))}
          </select>
        ) : (
          <span data-testid="showing-pillars">{pillarText(record.pillars)}</span>
        )}
        <span className="k" title={record.started_at}>
          Run {record.run_id}
        </span>
      </div>
      {bothEngines(listing, economy) && (
        <p className="showing-compare">
          Both Engines have a Run for {economyName}.{' '}
          <button
            type="button"
            className="link-btn"
            data-testid="showing-compare"
            onClick={() => onCompare(economy, record.pillars[0] ?? null)}
          >
            Compare them
          </button>
        </p>
      )}
    </>
  )
}
