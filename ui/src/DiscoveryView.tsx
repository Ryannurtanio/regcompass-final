import { useEffect, useRef, useState, type CSSProperties } from 'react'
import {
  discoveryLog,
  inCorpus,
  pillarPanel,
  shortName,
  siteOf,
  startReveal,
  type DiscoveryDocument,
  type DiscoveryViewState,
  type PillarPanel,
} from './discoveryViewState'
import { plainFailure } from './errors'
import './DiscoveryView.css'

// Discovery as it happens: what it has found, fetched and added, and the laws
// it could not add. Built from Discovery's typed events by the reducer in
// discoveryViewState.ts, in the same look as the Run view. The screen says
// what Discovery did, not how: skip reasons, notes and where each law came
// from stay in the events and the logs, for the audit.

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

function rowMark(d: DiscoveryDocument, ended: boolean): Glyph {
  if (inCorpus(d)) return 'done'
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
  if (inCorpus(d)) return 'In the Corpus'
  switch (d.state) {
    case 'added':
      return 'Added'
    case 'skipped':
      return 'Not added'
    case 'fetched':
      return 'Fetched'
    default:
      return ended ? 'Not fetched' : 'Waiting'
  }
}

function DocumentRow({ d, ended }: { d: DiscoveryDocument; ended: boolean }) {
  const title = d.title ?? shortName(d.url)
  const shows = inCorpus(d) ? 'corpus' : d.state === 'skipped' ? 'left' : d.state
  return (
    <li className={`dv-doc dv-${shows}`} data-testid="discovery-document" data-state={shows}>
      <Mark kind={rowMark(d, ended)} />
      <div className="dv-doc-main">
        <span className="dv-doc-title">{title}</span>
        <span className="dv-doc-url">{siteOf(d.url)}</span>
      </div>
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

// The one note worth a line on screen: nothing at all could be fetched. The
// server words it for the log ("no Document was fetched for ..."); every
// other note describes how Discovery works and stays in the log.
const NOTHING_FETCHED = /^no Document was fetched\b/

/** How long the finished log stays up before the Documents take its place. */
const HOLD_MS = 1500

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

function laws(n: number): string {
  return n === 1 ? '1 law' : `${n} laws`
}

/** The log as Discovery writes it: each law as it is found and added. */
function DiscoveryLog({ state, live }: { state: DiscoveryViewState; live: boolean }) {
  const lines = discoveryLog(state)
  const body = useRef<HTMLDivElement>(null)

  // The newest line stays in view.
  useEffect(() => {
    const el = body.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines.length])

  return (
    <div className="dv-log" role="log" aria-live="polite" aria-label="Discovery log" data-testid="discovery-log">
      <div className="dv-log-head">
        <span>
          Discovery log, {state.portal?.name}
          {state.drawn ? `, Pillar ${state.drawn.pillar}` : ''}
        </span>
        <span className="dv-log-tally" data-testid="discovery-log-tally">
          <span>Found {state.counts.found}</span>
          <span>Added {state.counts.added}</span>
        </span>
      </div>
      <div className="dv-log-body" ref={body}>
        {lines.map((l, i) => (
          <div key={l.key} className={`dv-line dv-line-${l.kind}`} data-testid="discovery-log-line">
            <span className="dv-line-tag">{l.tag}</span>
            <span className="dv-line-text">{l.text}</span>
            <span className="dv-line-right">
              {l.right}
              {live && i === lines.length - 1 ? <i className="dv-cursor" aria-hidden="true" /> : null}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** The finished Discovery's Documents against the drawn Pillar's Indicators,
 *  placed one by one. */
function PillarFilter({ panel, reduced }: { panel: PillarPanel; reduced: boolean }) {
  const { pillar, columns, rows } = panel
  const total = rows.length
  const [lit, setLit] = useState(reduced ? total : 0)
  const [all, setAll] = useState(false)

  useEffect(() => {
    if (reduced) return
    return startReveal(total, SHOWN, setLit)
  }, [reduced, total])

  const done = lit >= total
  const shown = all ? rows : rows.slice(0, SHOWN)
  const covered = columns.filter((id) => rows.some((r) => r.ids.includes(id)))
  const chipCols = { '--dv-cols': columns.length } as CSSProperties

  return (
    <div className="dv-list dv-filter" data-testid="discovery-filter">
      <div className="dv-list-head dv-filter-head">
        <h3>Filter by Pillar {pillar}</h3>
        <span className="dv-list-note">
          {columns.length
            ? `Each law found, against the Pillar ${pillar} Indicators it was found for.`
            : `Each law found for Pillar ${pillar}.`}
        </span>
      </div>
      {total > 0 && (
        <div className="dv-frow dv-frow-head" aria-hidden="true">
          <span />
          <span className="dv-k">Document</span>
          <span className="dv-chips" style={chipCols}>
            {columns.map((id) => {
              const n = rows.slice(0, lit).filter((r) => r.ids.includes(id)).length
              return (
                <span key={id} className="dv-col" data-testid="discovery-column">
                  <b>{id}</b>
                  <span className="dv-k">{laws(n)}</span>
                </span>
              )
            })}
          </span>
          <span className="dv-k dv-frow-state">Status</span>
        </div>
      )}
      <ol className="dv-docs">
        {shown.map((r, k) => {
          const isLit = k < lit
          const isCur = !done && k === lit - 1
          const label = r.ids.length ? `Found for Indicators ${r.ids.join(', ')}` : `Found for Pillar ${pillar}`
          return (
            <li
              key={r.url}
              className={`dv-frow${isCur ? ' dv-frow-cur' : ''}${isLit ? '' : ' dv-frow-wait'}`}
              data-testid="discovery-filter-row"
              data-lit={isLit}
            >
              <Mark kind={isCur ? 'working' : isLit ? 'done' : 'waiting'} />
              <div className="dv-doc-main">
                <span className="dv-doc-title">{r.title}</span>
                <span className="dv-doc-url">{r.site}</span>
              </div>
              {r.ids.length && columns.length ? (
                <span className="dv-chips" style={chipCols} role="img" aria-label={label}>
                  {columns.map((id) => {
                    const on = isLit && r.ids.includes(id)
                    return (
                      <span
                        key={id}
                        className={`dv-chip ${on ? 'dv-chip-on' : isLit ? 'dv-chip-off' : 'dv-chip-wait'}`}
                        style={on ? { animationDelay: `${r.ids.indexOf(id) * 0.14}s` } : undefined}
                        data-testid={on ? 'discovery-chip' : undefined}
                      >
                        {id}
                      </span>
                    )
                  })}
                </span>
              ) : (
                <span className="dv-chips dv-chips-none">{isLit ? label : ''}</span>
              )}
              <span className="dv-frow-state">{r.added ? 'Added' : r.inCorpus ? 'In the Corpus' : 'Not added'}</span>
            </li>
          )
        })}
      </ol>
      {total > SHOWN && (
        <div className="dv-list-foot">
          <button type="button" className="btn quiet" onClick={() => setAll((v) => !v)}>
            {all ? `Show the first ${SHOWN}` : `Show all ${total}`}
          </button>
        </div>
      )}
      <div className="dv-filter-foot" data-testid="discovery-filter-summary">
        {total === 0 ? (
          <span className="dv-k">No laws were found for Pillar {pillar}.</span>
        ) : done ? (
          <>
            <Mark kind="done" />
            <b>
              {laws(total)} found for Pillar {pillar}
              {covered.length ? `, covering Indicators ${covered.join(', ')}` : ''}.
            </b>
          </>
        ) : (
          <span className="dv-k">
            {lit === 0
              ? `Placing each law against the Pillar ${pillar} Indicators…`
              : `${lit} of ${total} laws placed…`}
          </span>
        )}
      </div>
    </div>
  )
}

/** A Discovery with no Pillar: the Documents it found, as a plain list. */
function DocumentList({ documents, ended }: { documents: DiscoveryDocument[]; ended: boolean }) {
  const [all, setAll] = useState(false)
  const shown = all ? documents : documents.slice(0, SHOWN)
  const scans = documents.filter((d) => d.state === 'added' && d.ocr_applied).length
  const added = documents.filter((d) => d.state === 'added').length
  return (
    <div className="dv-list">
      <div className="dv-list-head">
        <h3>Documents</h3>
        <span className="dv-list-note">
          {documents.length === 0
            ? 'No Documents were found.'
            : [
                scans > 0 ? `${scans} of ${added} added were read from scans.` : null,
                documents.length > shown.length ? `Showing ${shown.length} of ${documents.length}.` : null,
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
  )
}

export default function DiscoveryView({ state }: { state: DiscoveryViewState }) {
  if (state.status === 'idle' || state.portal === null) return null
  // A new Discovery starts clean: its own hold, its own reveal, its own timers.
  return <DiscoveryBody key={state.portal.run_id} state={state} />
}

function DiscoveryBody({ state }: { state: DiscoveryViewState }) {
  const [reduced] = useState(prefersReducedMotion)
  // Seen finishing live: the log's last line stays up for a moment first.
  const [seen, setSeen] = useState(state.status)
  const [holding, setHolding] = useState(false)
  if (seen !== state.status) {
    setSeen(state.status)
    if (seen === 'running' && state.status === 'finished' && !reduced) setHolding(true)
  }

  useEffect(() => {
    if (!holding) return
    const id = window.setTimeout(() => setHolding(false), HOLD_MS)
    return () => window.clearTimeout(id)
  }, [holding])

  const portal = state.portal!
  const { counts, documents } = state
  const running = state.status === 'running'
  const finished = state.status === 'finished'
  // The failure in plain words; the exception itself behind the toggle.
  const failure = state.failure ? plainFailure(state.failure, 'discovery') : null
  const panel = finished ? pillarPanel(state) : null
  const showLog = !finished || holding
  const pill = finished ? 'Finished' : state.status === 'failed' ? 'Stopped' : 'Discovering'
  const pillarWords = state.drawn ? ` (Pillar ${state.drawn.pillar})` : ''
  const nothingFetched = state.drawn?.notes.some((n) => NOTHING_FETCHED.test(n)) ?? false

  return (
    <section className="dv" data-testid="discovery-view" aria-labelledby="dv-title">
      <div className="dv-head">
        <div className="dv-head-text">
          <span className={`dv-pill dv-pill-${state.status}`}>
            {finished ? (
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <path d="M2.5 6.5 L5 9 L9.5 3.5" fill="none" stroke="var(--route)" strokeWidth="1.8" />
              </svg>
            ) : state.status === 'failed' ? (
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <path d="M3 3 L9 9 M9 3 L3 9" stroke="var(--fail)" strokeWidth="1.8" />
              </svg>
            ) : (
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <circle className="dv-live" cx="6" cy="6" r="3" fill="var(--route)" />
              </svg>
            )}
            {pill}
          </span>
          <h2 className="dv-title" id="dv-title">
            Discovery: {portal.name}
          </h2>
          <div className="dv-meta-block">
            <p className="dv-meta" data-testid="discovery-meta">
              {running ? (
                <>
                  Accessing the official legal portals for {portal.name}
                  <span className="dv-dots" aria-hidden="true">
                    <i>.</i>
                    <i>.</i>
                    <i>.</i>
                  </span>
                  {portal.refresh ? ' Fetching every Document again.' : ''}
                </>
              ) : state.status === 'failed' ? (
                `Search stopped for ${portal.name}${pillarWords}.`
              ) : (
                `Search complete for ${portal.name}${pillarWords}.`
              )}
            </p>
            {showLog && !failure && (
              <div className={`dv-sweep${running ? '' : ' dv-sweep-full'}`} aria-hidden="true" data-testid="discovery-sweep">
                <i />
              </div>
            )}
          </div>
        </div>
        <dl className="dv-stats" aria-live="polite">
          <div>
            <dt>Found</dt>
            <dd data-testid="discovery-found">{counts.found}</dd>
          </div>
          <div>
            <dt>Added</dt>
            <dd data-testid="discovery-added">{counts.added}</dd>
          </div>
          <div>
            <dt>Not added</dt>
            <dd data-testid="discovery-not-added">{counts.skipped}</dd>
          </div>
        </dl>
      </div>

      <p className="dv-lead" data-testid="discovery-lead">
        Discovery searches the official legal portals and adds the laws it
        finds to the Corpus.
      </p>

      {state.failure && (
        <div className="dv-failure" role="alert" data-testid="discovery-failure">
          <Mark kind="fail" />
          <span>
            Discovery stopped. {failure?.message} Documents already added stay
            in the Corpus; run Discovery again to pick up the rest.
          </span>
        </div>
      )}

      {showLog ? (
        <DiscoveryLog state={state} live={running} />
      ) : panel ? (
        <PillarFilter panel={panel} reduced={reduced} />
      ) : (
        <DocumentList documents={documents} ended />
      )}

      {state.drawn && (
        <div className="dv-list" data-testid="discovery-laws-not-found">
          <div className="dv-list-head">
            <h3>Laws not found</h3>
            <span className="dv-list-note">
              {state.drawn.baseline_skipped.length > 0 || nothingFetched
                ? ''
                : 'Every law for this search was found or is already in the Corpus.'}
            </span>
          </div>
          {nothingFetched && (
            <p className="dv-note" data-testid="discovery-nothing-fetched">
              No laws could be fetched for this search. Add them with Add document.
            </p>
          )}
          {state.drawn.baseline_skipped.length > 0 && (
            <ol className="dv-docs">
              {state.drawn.baseline_skipped.map((s) => (
                <li key={`${s.law}|${s.urls.join(' ')}`} className="dv-doc dv-left">
                  <Mark kind="skipped" />
                  <div className="dv-doc-main">
                    <span className="dv-doc-title">{s.law}</span>
                    <span className="dv-doc-reason">Add this law with Add document.</span>
                  </div>
                  <span className="dv-doc-state">{s.indicators.join(', ')}</span>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </section>
  )
}
