import { useCallback, useEffect, useMemo, useState } from 'react'
import { comparisonDownloadUrl, fetchComparison, fetchFinishedRuns, fetchStatus } from './api'
import OpenSource from './OpenSource'
import { RUN_STATUS_WORD, pillarName } from './runSetup'
import type {
  Agreement,
  Comparison as ComparisonPayload,
  ComparisonQuery,
  ComparisonRun,
  ComparisonSide,
  RunRecord,
  ServerStatus,
} from './types'
import ErrorNote from './ErrorNote'
import { plainError, type PlainError } from './errors'

function when(ts: string | null): string {
  if (!ts) return '-'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

function money(usd: number): string {
  return usd === 0 ? '0' : usd < 0.01 ? usd.toFixed(5) : usd.toFixed(3)
}

function seconds(value: number | null): string {
  if (value === null) return '-'
  if (value < 60) return `${value.toFixed(1)}\u00a0s`
  const m = Math.floor(value / 60)
  return `${m}\u00a0min ${Math.round(value % 60)}\u00a0s`
}

function shortWhen(ts: string | null): string {
  if (!ts) return '-'
  const d = new Date(ts)
  return Number.isNaN(d.getTime())
    ? ts
    : d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

// What the mark MEANS, in the reviewer's words. "agree" is not a judgement
// that the answer is right: it says the two Engines chose the same controlling
// provision on the same evidence.
const AGREEMENT_LABEL: Record<Agreement, string> = {
  agree: 'same provision',
  disagree: 'different provision',
  only_a: 'Run A only',
  only_b: 'Run B only',
  neither: 'neither Run',
}

function RunHeader({ side, run }: { side: 'A' | 'B'; run: ComparisonRun }) {
  const r = run.record
  const mappings = typeof r.details?.n_passed === 'number' ? (r.details.n_passed as number) : null
  return (
    <div className="compare-head">
      <div className="compare-head-title">
        {run.engine_display_name ?? r.engine ?? 'unknown Engine'}
        <span className="compare-side-tag">Run {side}</span>
      </div>
      <p className="compare-head-line">
        {[
          shortWhen(r.started_at),
          seconds(run.duration_s),
          `USD ${money(r.cost_usd)}`,
          mappings !== null ? `${mappings} Mappings` : null,
        ]
          .filter(Boolean)
          .join('. ')}
        .
      </p>
      <details className="compare-more">
        <summary>Run details</summary>
        <dl className="compare-meta">
          <dt>Run</dt>
          <dd className="mono">{r.run_id}</dd>
          <dt>Status</dt>
          <dd className={`status ${r.status}`}>{RUN_STATUS_WORD[r.status] ?? r.status}</dd>
          <dt>Started</dt>
          <dd>{when(r.started_at)}</dd>
          <dt>Duration</dt>
          <dd>{seconds(run.duration_s)}</dd>
          <dt>Documents fetched</dt>
          <dd>{r.documents_fetched}</dd>
          <dt>Tokens in / out</dt>
          <dd>
            {r.prompt_tokens.toLocaleString()} / {r.completion_tokens.toLocaleString()}
          </dd>
          <dt>Cost USD</dt>
          <dd>{money(r.cost_usd)}</dd>
        </dl>
      </details>
    </div>
  )
}

function SidePane({ side, label }: { side: ComparisonSide | null; label: string }) {
  if (side === null) {
    return (
      <div className="compare-side empty">
        <div className="compare-side-label">{label}</div>
        <span className="hint">No Mapping for this Indicator.</span>
      </div>
    )
  }
  const location = side.subsection ? `${side.section} ${side.subsection}` : side.section
  return (
    <div className="compare-side">
      <div className="compare-side-label">{label}</div>
      <div className="compare-doc law" title={side.document_id}>
        {side.document_title}
      </div>
      <div className="compare-loc">
        {location}
        {side.format !== 'html' && side.page_number !== null ? ` · p. ${side.page_number}` : ''}
        {side.confidence !== null ? ` · Confidence ${side.confidence.toFixed(2)}` : ''}
        {side.review_status ? ` · ${side.review_status}` : ''}
        {side.source_link ? ' · ' : ''}
        <OpenSource link={side.source_link} />
      </div>
      <blockquote className="compare-quote law">{side.verbatim_quote}</blockquote>
    </div>
  )
}

export default function Comparison({
  initialEconomy,
  initialPillar,
}: {
  initialEconomy?: string | null
  initialPillar?: number | null
}) {
  const [status, setStatus] = useState<ServerStatus | null>(null)
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [economy, setEconomy] = useState(initialEconomy ?? '')
  const [pillar, setPillar] = useState<number | null>(initialPillar ?? null)
  const [pairing, setPairing] = useState<'auto' | 'pick'>('auto')
  const [runA, setRunA] = useState('')
  const [runB, setRunB] = useState('')
  const [comparison, setComparison] = useState<ComparisonPayload | null>(null)
  const [error, setError] = useState<PlainError | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    // Both answers are needed before the pickers can settle, and the Run
    // Records win: opening on the Economy and Pillar the reviewer just ran is
    // the whole point of the screen, and the registry's first Economy is only
    // the fallback for a database with no Run in it yet.
    Promise.all([fetchStatus(), fetchFinishedRuns().catch(() => ({ runs: [] }))])
      .then(([s, r]) => {
        setStatus(s)
        setRuns(r.runs)
        const newest = r.runs.find(
          (run) => run.kind === 'run' && run.status === 'completed',
        )
        setEconomy((e) => e || newest?.economy || s.economies[0] || '')
        setPillar(
          (p) =>
            p ??
            newest?.pillars[0] ??
            s.default_pillars[0] ??
            s.pillars[0] ??
            null,
        )
      })
      .catch((e) => setError(plainError(e)))
  }, [])

  // Only completed Runs of this Economy that covered this Pillar can be a side:
  // a Comparison of a half-written Run would compare against nothing.
  const candidates = useMemo(
    () =>
      runs.filter(
        (r) =>
          r.kind === 'run' &&
          r.status === 'completed' &&
          r.economy === economy &&
          (pillar === null || r.pillars.includes(pillar)),
      ),
    [runs, economy, pillar],
  )

  const query: ComparisonQuery | null =
    pairing === 'pick'
      ? runA && runB
        ? { run_a: runA, run_b: runB }
        : null
      : economy && pillar !== null
        ? { economy, pillar }
        : null

  const compare = useCallback(() => {
    if (query === null) return
    setLoading(true)
    setError(null)
    fetchComparison(query)
      .then((c) => {
        setComparison(c)
        setLoading(false)
      })
      .catch((e) => {
        setComparison(null)
        setError(plainError(e))
        setLoading(false)
      })
  }, [JSON.stringify(query)]) // eslint-disable-line react-hooks/exhaustive-deps

  const runLabel = (r: RunRecord) =>
    `${r.engine ?? 'no Engine'} · ${when(r.started_at)} · ${r.run_id}`

  return (
    <div className="panel wide compare-screen">

      <div className="compare-controls">
        <div className="field">
          <label htmlFor="cmp-economy">Economy</label>
          <select
            id="cmp-economy"
            value={economy}
            onChange={(e) => {
              setEconomy(e.target.value)
              setRunA('')
              setRunB('')
            }}
          >
            {(status?.economies ?? []).map((code) => (
              <option key={code} value={code}>
                {status?.economy_names?.[code] ?? code}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="cmp-pillar">Pillar</label>
          <select
            id="cmp-pillar"
            value={pillar ?? ''}
            onChange={(e) => {
              setPillar(Number(e.target.value))
              setRunA('')
              setRunB('')
            }}
          >
            {(status?.pillars ?? []).map((p) => (
              <option key={p} value={p}>
                {pillarName(p) ? `${p}, ${pillarName(p)}` : p}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="cmp-pairing">Runs</label>
          <select
            id="cmp-pairing"
            value={pairing}
            onChange={(e) => setPairing(e.target.value as 'auto' | 'pick')}
          >
            <option value="auto">Newest Run per Engine</option>
            <option value="pick">Pick two Runs</option>
          </select>
        </div>

        {pairing === 'pick' && (
          <>
            <div className="field">
              <label htmlFor="cmp-run-a">Run A</label>
              <select
                id="cmp-run-a"
                value={runA}
                onChange={(e) => setRunA(e.target.value)}
              >
                <option value="">select a Run</option>
                {candidates.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {runLabel(r)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="cmp-run-b">Run B</label>
              <select
                id="cmp-run-b"
                value={runB}
                onChange={(e) => setRunB(e.target.value)}
              >
                <option value="">select a Run</option>
                {candidates.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {runLabel(r)}
                  </option>
                ))}
              </select>
            </div>
          </>
        )}

        <button className="primary" disabled={query === null || loading} onClick={compare}>
          {loading ? 'Comparing…' : 'Compare'}
        </button>
      </div>

      {error && <ErrorNote error={error} />}

      {comparison && query && (
        <>
          {comparison.note && <div className="notice">{comparison.note}</div>}

          <div className="compare-heads">
            <RunHeader side="A" run={comparison.run_a} />
            <RunHeader side="B" run={comparison.run_b} />
          </div>

          <div className="compare-results-head">
            <p className="compare-headline">
              <b>
                {comparison.n_indicators} Indicator{comparison.n_indicators === 1 ? '' : 's'}:{' '}
                {comparison.n_agree} same provision, {comparison.n_disagree} different.
              </b>{' '}
              <span className="hint-inline">
                {comparison.n_only_a} Run A only, {comparison.n_only_b} Run B only,{' '}
                {comparison.n_neither} neither.
              </span>
            </p>
            <div className="compare-downloads">
              <a href={comparisonDownloadUrl(query, 'xlsx')} download>
                Download sheet (xlsx)
              </a>
              <a href={comparisonDownloadUrl(query, 'sheet_csv')} download>
                Sheet (CSV)
              </a>
              <a href={comparisonDownloadUrl(query, 'csv')} download>
                Rows (CSV)
              </a>
              <a href={comparisonDownloadUrl(query, 'json')} download>
                JSON
              </a>
            </div>
            <p className="compare-sheet-line">
              Engine Comparison sheet: {comparison.n_provisions} provision
              {comparison.n_provisions === 1 ? '' : 's'} cited, {comparison.n_found_by_both} found by
              both, {comparison.n_found_by_a_only} by Engine A only,{' '}
              {comparison.n_found_by_b_only} by Engine B only.
            </p>
          </div>

          {comparison.rows.map((row) => (
            <section className="compare-row" key={row.indicator_id}>
              <div className="compare-row-head">
                <span className="compare-indicator">{row.indicator_id}</span>
                <span className="compare-indicator-name">{row.indicator_name}</span>
                <span className="spacer" />
                <span className={`badge ${row.agreement}`}>
                  {AGREEMENT_LABEL[row.agreement]}
                </span>
              </div>
              <div className="compare-sides">
                <SidePane
                  side={row.a}
                  label={comparison.run_a.engine_display_name ?? 'Run A'}
                />
                <SidePane
                  side={row.b}
                  label={comparison.run_b.engine_display_name ?? 'Run B'}
                />
              </div>
            </section>
          ))}

          <span className="hint">Agreement is matched on {comparison.match_basis}.</span>
        </>
      )}

      {!comparison && !error && (
        <span className="hint" data-testid="compare-empty">
          Press <strong>Compare</strong> to put the two Engines side by side on
          this Economy and Pillar. Each Engine needs one finished Run on them:
          run Engine A, then Engine B, then compare.
        </span>
      )}
    </div>
  )
}
