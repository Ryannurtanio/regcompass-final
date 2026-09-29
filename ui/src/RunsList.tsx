import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchEngines, fetchRuns, fetchStatus, runDownloadUrl } from './api'
import { durationText, statusWord } from './runsListText'
import type { EngineInfo, RunRecord, ServerStatus } from './types'
import ErrorNote from './ErrorNote'
import { plainError, type PlainError } from './errors'

// Short enough that the whole table fits a laptop window. The year goes,
// because it is the same for every row a reviewer will ever see here, and the
// full timestamp stays on the cell as its title for anyone who needs it.
const STAMP: Intl.DateTimeFormatOptions = {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
}

function when(ts: string | null): string {
  if (!ts) return '-'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, STAMP)
}

function fullWhen(ts: string | null): string | undefined {
  if (!ts) return undefined
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

/** The Engine concurrency this Run actually used, off its own details. It is
 *  the honest reason a Run at concurrency 4 was not four times faster. */
function concurrencyOf(r: RunRecord): number | null {
  const n = r.details?.engine_concurrency
  return typeof n === 'number' && n > 0 ? n : null
}

/** What the Run or Discovery produced, in one short phrase: Mappings for a
 *  Run, Documents fetched for a Discovery. */
function resultOf(r: RunRecord): string {
  if (r.kind === 'run') {
    const n = r.details?.n_passed
    return typeof n === 'number' ? `${n.toLocaleString()} Mapping${n === 1 ? '' : 's'}` : '-'
  }
  const n = r.documents_fetched
  return `${n} Document${n === 1 ? '' : 's'} fetched`
}

/** Each status has its own shape as well as its colour. */
function StatusGlyph({ status }: { status: RunRecord['status'] }) {
  if (status === 'completed')
    return (
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <path d="M2 6.5 L5 9 L10 3" fill="none" stroke="currentColor" strokeWidth="1.8" />
      </svg>
    )
  if (status === 'failed')
    return (
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <path d="M3 3 L9 9 M9 3 L3 9" stroke="currentColor" strokeWidth="1.8" />
      </svg>
    )
  if (status === 'running')
    return (
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="6" cy="6" r="2" fill="currentColor" />
      </svg>
    )
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
      <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray="2 2" />
    </svg>
  )
}

/** A token count short enough for a narrow column: 2.99M, 48k, 950. The
 *  exact count is the cell's title. */
function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (n >= 10_000) return `${Math.round(n / 1000)}k`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

function money(usd: number): string {
  return usd === 0 ? '0' : usd < 0.01 ? usd.toFixed(5) : usd.toFixed(3)
}

/** "Add document" files a Discovery record too, because it is the same event:
 *  one Document entering a Corpus. Its details say it came by hand, which is
 *  what a steward reading this list needs to see. */
function kindOf(r: RunRecord): string {
  if (r.kind === 'run') return 'Run'
  return r.details?.manual ? 'Discovery (manual add)' : 'Discovery'
}

export default function RunsList({
  onOpen,
  onCompare,
  onWatch,
}: {
  onOpen: (runId: string, economy: string) => void
  onCompare: (economy: string, pillar: number | null) => void
  /** Watch a Run again; offered only for a Run whose events were recorded. */
  onWatch?: (runId: string) => void
}) {
  const [runs, setRuns] = useState<RunRecord[] | null>(null)
  // How many Run Records there are in all: the list reads a page at a time,
  // and says so, rather than stopping silently at the newest page.
  const [total, setTotal] = useState(0)
  const [loadingMore, setLoadingMore] = useState(false)
  const [engines, setEngines] = useState<EngineInfo[]>([])
  const [status, setStatus] = useState<ServerStatus | null>(null)
  const [error, setError] = useState<PlainError | null>(null)
  // Narrowing the list on this page only; the server still lists them all.
  const [economyFilter, setEconomyFilter] = useState('')
  const [kindFilter, setKindFilter] = useState<'' | 'run' | 'discovery'>('')
  // A table wider than its window scrolls sideways with nothing to say so, and
  // a column cut to six pixels reads as a bug. When that happens the reader is
  // told, in words, rather than left to discover it.
  const scrollRef = useRef<HTMLDivElement>(null)
  const [scrolls, setScrolls] = useState(false)

  useEffect(() => {
    fetchRuns()
      .then((r) => {
        setRuns(r.runs)
        setTotal(r.total ?? r.runs.length)
      })
      .catch((e) => setError(plainError(e)))
    fetchEngines()
      .then((r) => setEngines(r.engines))
      .catch(() => setEngines([]))
    fetchStatus()
      .then(setStatus)
      .catch(() => setStatus(null))
  }, [])

  const measure = useCallback(() => {
    const el = scrollRef.current
    setScrolls(el !== null && el.scrollWidth > el.clientWidth + 1)
  }, [])

  useEffect(() => {
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [measure, runs, engines, economyFilter, kindFilter])

  const engineName = (name: string | null) =>
    name ? engines.find((e) => e.name === name)?.display_name ?? name : '-'
  // "Engine A" rather than the whole model name, "Fake Engine" rather than
  // its note in brackets: the full name is the title.
  const engineShort = (name: string | null) => {
    const full = engineName(name)
    const short = full.split(/:| \(/)[0].trim()
    return short || full
  }
  const economyName = (code: string) => status?.economy_names?.[code] ?? code

  const loadOlder = () => {
    if (!runs) return
    setLoadingMore(true)
    fetchRuns(runs.length)
      .then((r) => {
        const seen = new Set(runs.map((x) => x.run_id))
        setRuns([...runs, ...r.runs.filter((x) => !seen.has(x.run_id))])
        setTotal(r.total ?? total)
      })
      .catch((e) => setError(plainError(e)))
      .finally(() => setLoadingMore(false))
  }

  if (error) return <ErrorNote error={error} />
  if (!runs) return <div className="pdf-note">Loading Run Records…</div>
  if (runs.length === 0)
    return <div className="pdf-note">No Run Records yet. Start a Run to make the first one.</div>

  const economiesListed = Array.from(new Set(runs.map((r) => r.economy)))
  const shown = runs.filter(
    (r) =>
      (economyFilter === '' || r.economy === economyFilter) &&
      (kindFilter === '' || r.kind === kindFilter),
  )

  /** A Run opens on the Evidence screen. A Discovery mapped nothing, so it
   *  has no evidence to open; its Record (the link on the row) lists what it
   *  fetched. Opening it there showed an empty Run the Export then refused. */
  const openRow = (r: RunRecord) => {
    if (r.kind === 'run') onOpen(r.run_id, r.economy)
  }

  /** Up and down move between rows; Enter opens the one with focus. */
  const onRowKey = (e: React.KeyboardEvent<HTMLTableRowElement>, r: RunRecord) => {
    if (e.target !== e.currentTarget) return
    if (e.key === 'Enter') openRow(r)
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const next =
        e.key === 'ArrowDown'
          ? e.currentTarget.nextElementSibling
          : e.currentTarget.previousElementSibling
      if (next instanceof HTMLElement) next.focus()
    }
  }

  return (
    <div className="panel wide runs-screen">
      <div className="runs-filters">
        <div className="field">
          <label htmlFor="runs-economy">Economy</label>
          <select
            id="runs-economy"
            value={economyFilter}
            onChange={(e) => setEconomyFilter(e.target.value)}
          >
            <option value="">All Economies</option>
            {economiesListed.map((code) => (
              <option key={code} value={code}>
                {economyName(code)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="runs-kind">Kind</label>
          <select
            id="runs-kind"
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value as '' | 'run' | 'discovery')}
          >
            <option value="">Runs and Discoveries</option>
            <option value="run">Runs only</option>
            <option value="discovery">Discoveries only</option>
          </select>
        </div>
        <span className="hint runs-count">
          Showing {shown.length} of {runs.length}
          {total > runs.length ? ` loaded (${total} in all)` : ''}
        </span>
      </div>

      <div className={scrolls ? 'table-scroll scrolls' : 'table-scroll'} ref={scrollRef}>
        <table className="runs runs-table">
          <thead>
            <tr>
              <th>Started</th>
              <th>Kind</th>
              <th>Economy</th>
              <th>Pillar</th>
              <th>Engine</th>
              <th>Status</th>
              <th className="num">Took</th>
              <th className="num" title="Engine concurrency: how many Engine calls the Run made at once">
                At once
              </th>
              <th className="num">Cost USD</th>
              <th className="num">Result</th>
              <th className="num" title="Tokens the Engine read (in) and wrote (out)">
                Tokens
              </th>
              <th>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => {
              const conc = concurrencyOf(r)
              return (
                <tr
                  key={r.run_id}
                  className={r.kind === 'run' ? 'row' : 'row no-open'}
                  tabIndex={0}
                  title={r.kind === 'run' ? undefined : 'A Discovery maps nothing, so it has no evidence to open. Its Record lists what it fetched.'}
                  onClick={() => openRow(r)}
                  onKeyDown={(e) => onRowKey(e, r)}
                >
                  <td data-label="Started" title={fullWhen(r.started_at)}>{when(r.started_at)}</td>
                  <td data-label="Kind">{kindOf(r)}</td>
                  <td data-label="Economy" title={r.economy}>{economyName(r.economy)}</td>
                  <td data-label="Pillar">{r.pillars.join(', ') || '-'}</td>
                  <td data-label="Engine" className="ellipsis" title={engineName(r.engine)}>
                    {engineShort(r.engine)}
                  </td>
                  <td
                    data-label="Status"
                    className={`status ${r.status}`}
                    title={r.status === 'failed' && r.error ? r.error : undefined}
                  >
                    <span className="status-tag">
                      <StatusGlyph status={r.status} />
                      {statusWord(r)}
                    </span>
                  </td>
                  <td data-label="Took"
                    className="num"
                    title={r.ended_at ? `ended ${fullWhen(r.ended_at)}` : undefined}
                  >
                    {durationText(r.started_at, r.ended_at)}
                  </td>
                  <td data-label="At once" className="num" title="Engine calls made at once">
                    {conc ?? '-'}
                  </td>
                  <td data-label="Cost USD" className="num">{money(r.cost_usd)}</td>
                  <td data-label="Result" className="num">{resultOf(r)}</td>
                  <td
                    data-label="Tokens"
                    className="num quiet-cell"
                    title={`${r.prompt_tokens.toLocaleString()} in / ${r.completion_tokens.toLocaleString()} out`}
                  >
                    {compact(r.prompt_tokens)}&nbsp;in
                    <br />
                    {compact(r.completion_tokens)}&nbsp;out
                  </td>
                  <td className="row-actions">
                    {r.events_file && onWatch && (
                      <button
                        className="link-btn"
                        data-testid="watch-again"
                        title="Watch this Run again at speed, from its recorded events"
                        onClick={(e) => {
                          e.stopPropagation()
                          onWatch(r.run_id)
                        }}
                      >
                        Watch again
                      </button>
                    )}
                    {r.kind === 'run' && (
                      <button
                        className="link-btn"
                        onClick={(e) => {
                          e.stopPropagation()
                          onCompare(r.economy, r.pillars[0] ?? null)
                        }}
                      >
                        Compare
                      </button>
                    )}
                    <a
                      href={runDownloadUrl(r.run_id)}
                      download
                      title="Download this Run Record as a file"
                      onClick={(e) => e.stopPropagation()}
                    >
                      Record
                    </a>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {total > runs.length && (
        <button
          type="button"
          className="link-btn"
          data-testid="runs-load-older"
          disabled={loadingMore}
          onClick={loadOlder}
        >
          {loadingMore ? 'Loading…' : `Show older records (${total - runs.length} more)`}
        </button>
      )}
      {scrolls && (
        <span className="hint" data-testid="runs-scroll-cue">
          The table is wider than this window. Scroll it sideways for the last
          columns.
        </span>
      )}
      <p className="hint runs-foot">
        A Run's row opens its Mappings on the Evidence screen; a Discovery
        maps nothing, so its row opens nothing and its Record lists what it
        fetched. Result is the
        Mappings a Run proved, or the Documents a Discovery fetched. At once is
        how many Engine calls the Run made at the same time, which is why a Run
        at 4 is not four times faster than one at 1. Tokens are what the Engine
        read (in) and wrote (out).
      </p>
    </div>
  )
}
