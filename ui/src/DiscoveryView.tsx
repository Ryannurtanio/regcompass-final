import { useEffect, useState } from 'react'
import type {
  DiscoveryDocument,
  DiscoveryPhase,
  DiscoveryViewState,
  PhaseState,
} from './discoveryViewState'
import { plainFailure } from './errors'
import './DiscoveryView.css'

// Discovery as it happens: the Portal it reads, what it has found, fetched
// and added, and each Document that was skipped with the reason in plain
// words. Built from Discovery's typed events by the reducer in
// discoveryViewState.ts, in the same look as the Run view.

type Glyph = 'done' | 'working' | 'waiting' | 'skipped' | 'fetched' | 'flag' | 'fail'

function Mark({ kind }: { kind: Glyph }) {
  const common = { width: 18, height: 18, viewBox: '0 0 18 18', 'aria-hidden': true } as const
  switch (kind) {
    case 'done':
      return (
        <svg {...common} className="dv-mark">
          <circle cx="9" cy="9" r="6" fill="var(--route)" />
        </svg>
      )
    case 'working':
      return (
        <svg {...common} className="dv-mark">
          <circle cx="9" cy="9" r="7.5" fill="var(--surface)" stroke="var(--route)" strokeWidth="2" />
          <circle className="dv-pulse" cx="9" cy="9" r="3" fill="var(--route)" />
        </svg>
      )
    case 'fetched':
      return (
        <svg {...common} className="dv-mark">
          <circle cx="9" cy="9" r="6" fill="var(--route-soft)" stroke="var(--route)" strokeWidth="1.5" />
        </svg>
      )
    case 'skipped':
      return (
        <svg {...common} className="dv-mark">
          <circle
            cx="9"
            cy="9"
            r="5.5"
            fill="var(--surface)"
            stroke="var(--route)"
            strokeWidth="1.5"
            strokeDasharray="2.2 2"
          />
        </svg>
      )
    case 'flag':
      return (
        <svg {...common} className="dv-mark">
          <path d="M9 1.5 L16.5 9 L9 16.5 L1.5 9 Z" fill="var(--flag)" />
          <rect x="8.2" y="5" width="1.6" height="5.2" fill="var(--surface)" />
          <rect x="8.2" y="11.4" width="1.6" height="1.6" fill="var(--surface)" />
        </svg>
      )
    case 'fail':
      return (
        <svg {...common} className="dv-mark">
          <circle cx="9" cy="9" r="7.5" fill="var(--fail)" />
          <path d="M6 6 L12 12 M12 6 L6 12" stroke="var(--surface)" strokeWidth="1.8" />
        </svg>
      )
    default:
      return (
        <svg {...common} className="dv-mark">
          <circle cx="9" cy="9" r="5" fill="var(--surface)" stroke="var(--wait)" strokeWidth="1.5" />
        </svg>
      )
  }
}

const PHASES: { key: DiscoveryPhase; name: string }[] = [
  { key: 'find', name: 'Find' },
  { key: 'fetch', name: 'Fetch' },
  { key: 'read', name: 'Read' },
  { key: 'add', name: 'Add' },
]

function phaseLine(key: DiscoveryPhase, spacing: number | null): string {
  switch (key) {
    case 'find':
      return 'Lists the laws on the Portal'
    case 'fetch':
      return spacing && spacing > 0
        ? `Downloads each file, ${formatSeconds(spacing)} apart`
        : 'Downloads each file, one at a time'
    case 'read':
      return 'Takes out the text; scans get OCR'
    case 'add':
      return 'Saves it to the Corpus'
  }
}

const PHASE_MARK: Record<PhaseState, Glyph> = {
  done: 'done',
  working: 'working',
  waiting: 'waiting',
  nothing: 'skipped',
}

const PHASE_WORDS: Record<PhaseState, string> = {
  done: 'done',
  working: 'working now',
  waiting: 'waiting',
  nothing: 'nothing to do',
}

function formatSeconds(s: number): string {
  return Number.isInteger(s) ? `${s} s` : `${s.toFixed(1)} s`
}

function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h} h ${m} min`
  if (m > 0) return `${m} min ${String(s).padStart(2, '0')} s`
  return `${s} s`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function clock(ts: string): string {
  const d = new Date(ts)
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
}

function day(ts: string): string {
  const d = new Date(ts)
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`
}

function when(start: string | null, end: string | null): string {
  if (!start) return ''
  if (!end) return `Started ${day(start)}, ${clock(start)} UTC.`
  const sameDay = day(start) === day(end)
  return `${day(start)}, ${clock(start)} to ${sameDay ? '' : `${day(end)}, `}${clock(end)} UTC.`
}

/** The last part of an address, for a Document that has no title yet. */
function shortName(url: string): string {
  try {
    const u = new URL(url)
    const last = u.pathname.split('/').filter(Boolean).pop()
    return decodeURIComponent(last ?? u.host)
  } catch {
    return url
  }
}

function rowMark(d: DiscoveryDocument, ended: boolean): Glyph {
  switch (d.state) {
    case 'added':
      return 'done'
    case 'skipped':
      return 'skipped'
    case 'fetched':
      return 'fetched'
    default:
      return ended ? 'skipped' : 'waiting'
  }
}

function rowStatus(d: DiscoveryDocument, ended: boolean): string {
  switch (d.state) {
    case 'added':
      return 'Added'
    case 'skipped':
      return 'Skipped'
    case 'fetched':
      return 'Fetched'
    default:
      return ended ? 'Not fetched' : 'Waiting'
  }
}

function DocumentRow({ d, ended }: { d: DiscoveryDocument; ended: boolean }) {
  const title = d.title ?? shortName(d.url)
  return (
    <li className={`dv-doc dv-${d.state}`} data-testid="discovery-document" data-state={d.state}>
      <Mark kind={rowMark(d, ended)} />
      <div className="dv-doc-main">
        <span className="dv-doc-title">{title}</span>
        <span className="dv-doc-url">{d.url}</span>
        {d.state === 'skipped' && d.reason && (
          <span className="dv-doc-reason" data-testid="discovery-skip-reason">
            {d.reason}
          </span>
        )}
      </div>
      <span className="dv-doc-pages">
        {d.n_pages ? `${d.n_pages.toLocaleString('en-US')} page${d.n_pages === 1 ? '' : 's'}` : ''}
      </span>
      <span className="dv-doc-state">
        {d.state === 'added' && d.ocr_applied ? (
          <span className="dv-scan">
            <Mark kind="flag" />
            Read from a scan
          </span>
        ) : (
          rowStatus(d, ended)
        )}
      </span>
    </li>
  )
}

const SHOWN = 12

export default function DiscoveryView({ state }: { state: DiscoveryViewState }) {
  // The failure in plain words; the exception itself behind the toggle.
  const failure = state.failure ? plainFailure(state.failure, 'discovery') : null
  const [all, setAll] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const running = state.status === 'running'

  // Only the elapsed time ticks, and only while Discovery runs.
  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [running])

  if (state.status === 'idle' || state.portal === null) return null
  const { portal, counts, documents, phases } = state
  const ended = state.status !== 'running'
  const took =
    state.started_at && state.ended_at
      ? formatDuration(Date.parse(state.ended_at) - Date.parse(state.started_at))
      : state.started_at
        ? formatDuration(now - Date.parse(state.started_at))
        : ''
  const shown = all ? documents : documents.slice(0, SHOWN)
  const pill =
    state.status === 'finished' ? 'Finished' : state.status === 'failed' ? 'Stopped' : 'Discovering'
  const host = portal.hosts[0] ?? null

  return (
    <section className="dv" data-testid="discovery-view" aria-labelledby="dv-title">
      <div className="dv-head">
        <div className="dv-head-text">
          <span className={`dv-pill dv-pill-${state.status}`}>
            {state.status === 'finished' ? (
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <path d="M2.5 6.5 L5 9 L9.5 3.5" fill="none" stroke="var(--route)" strokeWidth="1.8" />
              </svg>
            ) : state.status === 'failed' ? (
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <path d="M3 3 L9 9 M9 3 L3 9" stroke="var(--fail)" strokeWidth="1.8" />
              </svg>
            ) : null}
            {pill}
          </span>
          <h2 className="dv-title" id="dv-title">
            Discovery: {portal.name}
          </h2>
          <p className="dv-meta">
            {host ? `Portal ${host}. ` : ''}
            {when(state.started_at, state.ended_at)}
            {portal.refresh
              ? running
                ? ' Fetching every Document again.'
                : ' Asked for every Document again.'
              : ''}
          </p>
        </div>
        <dl className="dv-stats" aria-live="polite">
          <div>
            <dt>{ended ? 'Took' : 'Elapsed'}</dt>
            <dd>{took}</dd>
          </div>
          <div>
            <dt>Found</dt>
            <dd data-testid="discovery-found">{counts.found}</dd>
          </div>
          <div>
            <dt>Added</dt>
            <dd data-testid="discovery-added">{counts.added}</dd>
          </div>
          <div>
            <dt>Skipped</dt>
            <dd data-testid="discovery-skipped">{counts.skipped}</dd>
          </div>
        </dl>
      </div>

      <p className="dv-lead">
        Discovery reads the Portal's list of laws, asks for each file politely,
        one at a time, and keeps a copy of every file. Each Document it adds
        joins the Corpus, and a Run reads that Corpus without asking the Portal
        again.
      </p>

      {state.failure && (
        <div className="dv-failure" role="alert" data-testid="discovery-failure">
          <Mark kind="fail" />
          <span>
            Discovery stopped. {failure?.message} Documents already added stay
            in the Corpus; run Discovery again to pick up the rest.
            {failure?.technical && (
              <details className="dv-technical">
                <summary>Show the technical message</summary>
                <code>{failure.technical}</code>
              </details>
            )}
          </span>
        </div>
      )}

      <ol className="dv-phases" aria-label="What Discovery does">
        {PHASES.map(({ key, name }) => (
          <li key={key} className={`dv-phase dv-phase-${phases[key]}`}>
            <span className="dv-phase-name">
              <Mark kind={PHASE_MARK[phases[key]]} />
              {name}
              <span className="dv-sr">: {PHASE_WORDS[phases[key]]}</span>
            </span>
            <span className="dv-phase-line">{phaseLine(key, state.spacing_seconds)}</span>
          </li>
        ))}
      </ol>

      <div className="dv-list">
        <div className="dv-list-head">
          <h3>Documents</h3>
          <span className="dv-list-note">
            {documents.length === 0
              ? running
                ? 'Reading the Portal for its list of laws.'
                : state.status === 'failed'
                  ? 'Discovery stopped before the Portal listed any Documents.'
                  : 'The Portal listed no Documents.'
              : [
                  counts.scans > 0
                    ? `${counts.scans} of ${counts.added} added were scans, read by OCR (text from the page images).`
                    : null,
                  documents.length > shown.length
                    ? `Showing ${shown.length} of ${documents.length}.`
                    : null,
                ]
                  .filter(Boolean)
                  .join(' ')}
          </span>
        </div>
        {documents.length > 0 && (
          <ol className="dv-docs">
            {shown.map((d) => (
              <DocumentRow key={d.url} d={d} ended={ended} />
            ))}
          </ol>
        )}
        {documents.length > SHOWN && (
          <div className="dv-list-foot">
            <button type="button" className="btn quiet" onClick={() => setAll((v) => !v)}>
              {all ? `Show the first ${SHOWN}` : `Show all ${documents.length}`}
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
