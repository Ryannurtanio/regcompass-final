import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { fetchPiece, fetchRecordDetail, fetchRecords } from './api'
import AuditView from './AuditView'
import { Glyph } from './RunView'
import {
  OUTCOME_LABEL,
  OUTCOME_SENTENCE,
  byIndicator,
  candidateCounts,
  candidateOfMapping,
  findCandidate,
  gateReasons,
  gateScoresRecorded,
  markQuote,
  pieceOfMapping,
  quoted,
  stepRows,
  type Drill,
  type Scale,
} from './drillDown'
import { documentState, duration, num } from './runOverview'
import { documentSize, pageSpan, pageSuffix, textSize } from './sourceText'
import type { CandidateView, DocumentView, RunViewState } from './runViewState'
import type { CandidateOutcome, PieceDetail, RecordSummary } from './types'

// One level deeper than the Run's overview: one Document, one Candidate, one
// Mapping. Each layer takes the overview's place; the breadcrumb, the close
// control and Escape all go straight back to the overview.

const plural = (n: number, one: string, many = `${one}s`) => `${num(n)} ${n === 1 ? one : many}`

const NOT_RECORDED =
  "Not recorded for this Run. It was saved before the Gate's scores for each Candidate were kept, so only the counts are shown."

// ---------------------------------------------------------------------------
// The frame every layer shares
// ---------------------------------------------------------------------------

/** Bring a layer's top into view when it is above the visible area or low
 *  in it: first inside the screen's own scrolling panel (the desktop layout),
 *  then in the window (the phone layout, where the page itself scrolls). */
function showTop(el: HTMLElement) {
  const gap = 12
  let box: HTMLElement | null = el.parentElement
  while (box) {
    const y = getComputedStyle(box).overflowY
    if ((y === 'auto' || y === 'scroll') && box.scrollHeight > box.clientHeight) break
    box = box.parentElement
  }
  if (box) {
    const offset = el.getBoundingClientRect().top - box.getBoundingClientRect().top
    if (offset < 0 || offset > box.clientHeight / 2) box.scrollTop += offset - gap
  }
  const top = el.getBoundingClientRect().top
  if (top < 0 || top > window.innerHeight / 2) window.scrollBy(0, top - gap)
}

export interface DrillProps {
  drill: Drill
  state: RunViewState
  /** "Singapore, Pillar 7": the overview's own title, for the breadcrumb. */
  runTitle: string
  onOpen: (drill: Drill) => void
  onClose: () => void
  /** Open this Run's Mappings in Evidence, where they are reviewed. */
  onOpenEvidence?: () => void
}

export default function RunDrill({ drill, state, runTitle, onOpen, onClose, onOpenEvidence }: DrillProps) {
  const heading = useRef<HTMLHeadingElement>(null)
  const root = useRef<HTMLDivElement>(null)
  const key = drill.kind === 'document' ? drill.document_id : drill.kind === 'candidate' ? `${drill.piece_id}::${drill.indicator}` : drill.mapping_id
  useEffect(() => {
    // Focus goes to the layer's heading for a screen reader, but the page is
    // placed by the layer's top: the trail and "Back to the Run" stay in view
    // rather than being scrolled off above the heading.
    heading.current?.focus({ preventScroll: true })
    if (root.current) showTop(root.current)
  }, [drill.kind, key])

  const d = state.documents.find((x) => x.document_id === drill.document_id) ?? null
  if (!d) {
    return (
      <div className="rvd" data-testid="run-drill">
        <p className="rv-note">This Document is not part of this Run.</p>
        <button className="btn" onClick={onClose}>
          Back to the Run
        </button>
      </div>
    )
  }
  const position = state.documents.indexOf(d) + 1
  const crumbs: { label: string; onClick?: () => void }[] = [{ label: runTitle, onClick: onClose }]
  if (drill.kind === 'document') {
    crumbs.push({ label: `Document ${position} of ${state.documents.length}` })
  } else {
    crumbs.push({ label: d.title, onClick: () => onOpen({ kind: 'document', document_id: d.document_id }) })
    crumbs.push({ label: drill.kind === 'candidate' ? 'Candidate' : 'Mapping' })
  }

  return (
    <div className="rvd" data-testid="run-drill" data-layer={drill.kind} ref={root}>
      <div className="rvd-bar">
        <nav aria-label="Breadcrumb" className="rvd-crumbs">
          <ol>
            {crumbs.map((c, i) => (
              <li key={i}>
                {c.onClick ? (
                  <button type="button" className="rvd-crumb" onClick={c.onClick}>
                    {c.label}
                  </button>
                ) : (
                  <span aria-current="page">{c.label}</span>
                )}
              </li>
            ))}
          </ol>
        </nav>
        <button type="button" className="btn rvd-close" onClick={onClose} data-testid="run-drill-close">
          <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
            <path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" strokeWidth="1.6" />
          </svg>
          Back to the Run <kbd>Esc</kbd>
        </button>
      </div>
      {drill.kind === 'document' && <DocumentLayer d={d} state={state} heading={heading} onOpen={onOpen} />}
      {drill.kind === 'candidate' && (
        <CandidateLayer d={d} state={state} heading={heading} pieceId={drill.piece_id} indicator={drill.indicator} onOpen={onOpen} />
      )}
      {drill.kind === 'mapping' && (
        <MappingLayer
          d={d}
          state={state}
          heading={heading}
          mappingId={drill.mapping_id}
          onClose={onClose}
          onOpenEvidence={onOpenEvidence}
        />
      )}
    </div>
  )
}

type HeadingRef = React.RefObject<HTMLHeadingElement | null>

function StatusPill({ d }: { d: DocumentView }) {
  const st = documentState(d)
  if (st === 'finished' || st === 'zero')
    return (
      <span className="rv-pill done">
        <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
          <path d="M2 6.5 L5 9 L10 3" fill="none" stroke="var(--route)" strokeWidth="1.8" />
        </svg>
        Finished
      </span>
    )
  if (st === 'failed') return <span className="rv-pill fail">Stopped</span>
  if (st === 'waiting') return <span className="rv-pill done">Waiting its turn</span>
  return (
    <span className="rv-pill">
      <svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">
        <circle cx="4" cy="4" r="4" fill="var(--route)" className="rv-pulse-soft" />
      </svg>
      Working
    </span>
  )
}

function OutcomeMark({ outcome }: { outcome: CandidateOutcome }) {
  return (
    <span className={`rvd-outcome is-${outcome}`}>
      <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
        {outcome === 'mapped' ? (
          <circle cx="5" cy="5" r="4.5" fill="currentColor" />
        ) : outcome === 'not_applicable' ? (
          <circle cx="5" cy="5" r="4" fill="none" stroke="currentColor" strokeWidth="1.4" />
        ) : outcome === 'dropped_by_proof' ? (
          <path d="M1.5 1.5 L8.5 8.5 M8.5 1.5 L1.5 8.5" stroke="currentColor" strokeWidth="1.6" />
        ) : (
          <circle cx="5" cy="5" r="4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeDasharray="2 1.6" />
        )}
      </svg>
      {OUTCOME_LABEL[outcome]}
    </span>
  )
}

const LANGUAGES: Record<string, string> = {
  en: 'English', zh: 'Chinese', id: 'Indonesian', ms: 'Malay', hi: 'Hindi', lo: 'Lao', th: 'Thai',
}

// ---------------------------------------------------------------------------
// Layer 1: one Document
// ---------------------------------------------------------------------------

/** The Document's proven Mappings from the record endpoints, fetched again
 *  each time another one is saved. Null while none can be read yet. */
function useDocumentRecords(d: DocumentView, runId: string | null): RecordSummary[] | null {
  const [records, setRecords] = useState<RecordSummary[] | null>(null)
  const saved = Math.max(d.mappingsFound.length, d.mappings ?? 0)
  useEffect(() => {
    if (!runId || saved === 0) return
    let live = true
    fetchRecords(d.document_id, runId)
      .then((rs) => live && setRecords(rs))
      .catch(() => live && setRecords(null))
    return () => {
      live = false
    }
  }, [d.document_id, runId, saved])
  return saved === 0 ? null : records
}

function DocumentLayer({
  d,
  state,
  heading,
  onOpen,
}: {
  d: DocumentView
  state: RunViewState
  heading: HeadingRef
  onOpen: (drill: Drill) => void
}) {
  const rows = stepRows(d)
  const runId = state.run?.run_id ?? null
  const records = useDocumentRecords(d, runId)
  const start = d.times.read?.start
  const end = d.times.gloss?.end ?? null
  const took = start !== undefined && end !== null ? duration(end - start) : null
  const meta = [
    documentSize(d.format, d.n_pages),
    d.language ? (LANGUAGES[d.language.toLowerCase()] ?? d.language) : null,
    took ? `Took ${took} in this Run` : null,
  ].filter(Boolean)

  return (
    <>
      <header className="rvd-head">
        <StatusPill d={d} />
        <h2 className="rvd-title law" ref={heading} tabIndex={-1} data-testid="run-drill-title">
          {d.title}
        </h2>
        {meta.length > 0 && <div className="rv-k">{meta.join('. ')}.</div>}
      </header>
      <p className="rv-lead">
        What happened to this Document at each Step, the Candidates the Gate kept, and the Mappings
        {!d.done && !d.failed ? ' so far' : ''}.
      </p>

      <div className="rvd-grid">
        <section className="rv-panel rvd-steps" aria-labelledby="rvd-steps-title">
          <h3 id="rvd-steps-title">Steps</h3>
          <ol>
            {rows.map((r, i) => (
              <li key={r.station} className={`rvd-step is-${r.state}`}>
                {i < rows.length - 1 && <i className={`rvd-step-line${r.state === 'waiting' ? '' : ' on'}`} aria-hidden="true" />}
                <span className="rvd-step-glyph">
                  <Glyph state={r.state} />
                </span>
                <div className="rvd-step-body">
                  <div className="rvd-step-head">
                    <h4>{r.label}</h4>
                    <span className="rvd-step-took">{r.took ?? (r.state === 'waiting' ? 'not yet' : '')}</span>
                  </div>
                  <div className="rv-k">{r.what}</div>
                  {r.result && <p className="rvd-step-result">{r.result}</p>}
                  {r.bar !== null && r.state !== 'waiting' && (
                    <span className="rvd-minibar" aria-hidden="true">
                      <i style={{ width: `${Math.max(1, Math.round(r.bar * 100))}%` }} />
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </section>

        <MappingsPanel d={d} records={records} onOpen={onOpen} />
      </div>

      <CandidatesPanel d={d} state={state} onOpen={onOpen} />
    </>
  )
}

interface MapItem {
  mapping_id: string
  indicator: string
  name: string | null
  page: number | null
  section: string | null
  quote: string | null
  // A web page has no pages: its quotes are stored as page 1 of one.
  web?: boolean
}

function MappingsPanel({
  d,
  records,
  onOpen,
}: {
  d: DocumentView
  records: RecordSummary[] | null
  onOpen: (drill: Drill) => void
}) {
  const refs = d.mappingsFound
  const n = Math.max(refs.length, records?.length ?? 0, d.mappings ?? 0)
  // What the list shows: the saved records (with their quotes) when they can
  // be read, else what the events said (id and page).
  const items: MapItem[] = records
    ? records.map((r) => ({
        mapping_id: r.mapping_id,
        indicator: r.indicator_id,
        name: r.indicator_name,
        page: r.page_number,
        section: r.section,
        quote: r.quote_preview,
        web: r.format === 'html',
      }))
    : refs.map((r) => ({ mapping_id: r.mapping_id, indicator: r.indicator, name: null, page: r.page, section: null, quote: null }))
  const groups = byIndicator(items)
  const working = !d.done && !d.failed
  // Proven during Map, but saved (and so openable) only once the Document's
  // last Candidate is read.
  const waiting = Math.max(0, d.candidates.filter((c) => c.outcome === 'mapped').length - n)

  return (
    <section className="rv-panel rvd-maps" aria-labelledby="rvd-maps-title" data-testid="run-drill-mappings">
      <div className="rv-panel-head">
        <h3 id="rvd-maps-title">
          {n === 0 ? (waiting > 0 ? 'Mappings' : 'No Mappings') : plural(n, 'Mapping')}
          {working ? ' so far' : ''}
          {n > 0 ? ', by Indicator' : ''}
        </h3>
        {n > 0 && <span className="rv-k">Select one to see its quote in its source</span>}
      </div>
      {waiting > 0 && (
        <p className="rvd-empty" data-testid="run-drill-proven-waiting">
          {waiting === 1 ? '1 proven quote is' : `${num(waiting)} proven quotes are`} not saved yet.{' '}
          {waiting === 1 ? 'It opens' : 'They open'} here once this Document's last Candidate is read.
        </p>
      )}
      {n === 0 ? (
        waiting > 0 ? null : <p className="rvd-empty">
          {working
            ? 'Proven Mappings appear here as soon as they are saved.'
            : d.done
              ? 'None of its Candidates gave a proven quote.'
              : 'Not reached.'}
        </p>
      ) : items.length === 0 ? (
        <p className="rvd-empty">Loading the Mappings.</p>
      ) : (
        groups.map((g) => (
          <div key={g.indicator} className="rvd-group">
            <div className="rvd-group-head">
              <b>{g.indicator}</b>
              <span>{g.items[0].name ?? ''}</span>
              <span className="rv-k">{plural(g.items.length, 'Mapping')}</span>
            </div>
            <ul>
              {g.items.map((m) => (
                <li key={m.mapping_id}>
                  <button
                    type="button"
                    className="rvd-map"
                    onClick={() => onOpen({ kind: 'mapping', document_id: d.document_id, mapping_id: m.mapping_id })}
                  >
                    <span className="rv-k">
                      {m.web ? 'Web page' : m.page ? `Page ${m.page}` : 'Page not known'}
                      {m.section ? <><br />{m.section}</> : null}
                    </span>
                    <span className="rvd-map-quote law">
                      {m.quote ? quoted(m.quote) : 'Saved. Open it to read its quote.'}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
    </section>
  )
}

type Filter = 'all' | CandidateOutcome
const SHOW_FIRST = 40

function CandidatesPanel({ d, state, onOpen }: { d: DocumentView; state: RunViewState; onOpen: (drill: Drill) => void }) {
  const [filter, setFilter] = useState<Filter>('all')
  const [showAll, setShowAll] = useState(false)
  const c = candidateCounts(d)
  const recorded = gateScoresRecorded(state, d)
  const list = filter === 'all' ? d.candidates : d.candidates.filter((x) => x.outcome === filter)
  const shown = showAll ? list : list.slice(0, SHOW_FIRST)
  const filters: { key: Filter; label: string; n: number }[] = [
    { key: 'all', label: 'All', n: c.settled },
    { key: 'mapped', label: OUTCOME_LABEL.mapped, n: c.byOutcome.mapped },
    { key: 'not_applicable', label: OUTCOME_LABEL.not_applicable, n: c.byOutcome.not_applicable },
    { key: 'dropped_by_proof', label: OUTCOME_LABEL.dropped_by_proof, n: c.byOutcome.dropped_by_proof },
    { key: 'skipped', label: OUTCOME_LABEL.skipped, n: c.byOutcome.skipped },
  ].filter((f) => f.key === 'all' || f.n > 0) as { key: Filter; label: string; n: number }[]

  const summary =
    c.kept === null
      ? 'The Gate has not finished with this Document yet.'
      : `The Gate kept ${plural(c.kept, 'Candidate')} of ${plural(c.pairs ?? c.kept, 'section and Indicator pair')}. The ${num(c.setAside ?? 0)} it set aside are counted, not listed.`

  return (
    <section className="rv-panel rvd-cands" aria-labelledby="rvd-cands-title" data-testid="run-drill-candidates">
      <div className="rv-panel-head">
        <h3 id="rvd-cands-title">Candidates the Gate kept</h3>
        <span className="rv-k">{summary}</span>
      </div>
      {!recorded ? (
        <p className="rvd-not-recorded" data-testid="run-drill-not-recorded">
          {NOT_RECORDED}
        </p>
      ) : c.kept === 0 ? (
        <p className="rvd-empty">The Gate kept no Candidates, so the Engine read nothing from this Document.</p>
      ) : c.settled === 0 ? (
        <p className="rvd-empty">
          {c.kept === null ? 'Candidates appear here once the Gate has kept them and the Engine reads them.' : 'Each Candidate appears here as soon as the Engine has read it.'}
        </p>
      ) : (
        <>
          <div className="rvd-filters" role="group" aria-label="Show Candidates by outcome">
            {filters.map((f) => (
              <button
                key={f.key}
                type="button"
                className={`rvd-filter${filter === f.key ? ' on' : ''}`}
                aria-pressed={filter === f.key}
                onClick={() => {
                  setFilter(f.key)
                  setShowAll(false)
                }}
              >
                {f.label} <span>{num(f.n)}</span>
              </button>
            ))}
            {c.kept !== null && c.settled < c.kept && <span className="rv-k">{num(c.kept - c.settled)} still to read</span>}
          </div>
          <ul className="rvd-cand-list">
            {shown.map((x) => (
              <li key={`${x.piece_id}::${x.indicator}`}>
                <button
                  type="button"
                  className="rvd-cand"
                  onClick={() => onOpen({ kind: 'candidate', document_id: d.document_id, piece_id: x.piece_id, indicator: x.indicator })}
                >
                  <b>{x.indicator}</b>
                  <span className="rvd-cand-where">
                    {x.section ?? 'Section'}
                    {pageSuffix(d.format, x.page)}
                  </span>
                  <OutcomeMark outcome={x.outcome} />
                </button>
              </li>
            ))}
          </ul>
          {list.length > shown.length && (
            <button type="button" className="btn quiet rvd-more" onClick={() => setShowAll(true)}>
              Show all {num(list.length)}
            </button>
          )}
        </>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Layer 2: one Candidate
// ---------------------------------------------------------------------------

function usePiece(pieceId: string): { piece: PieceDetail | null; error: string | null } {
  const [piece, setPiece] = useState<PieceDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    setPiece(null)
    setError(null)
    fetchPiece(pieceId)
      .then((p) => live && setPiece(p))
      .catch(() => live && setError('The section could not be read from the working database.'))
    return () => {
      live = false
    }
  }, [pieceId])
  return { piece, error }
}

/** The saved quote of a Mapping. A read that failed says so, with a way to
 *  try again, rather than looking like a quote not saved yet. */
interface QuoteRead {
  quote: string | null
  failed: boolean
  retry: () => void
}

function useQuote(mappingId: string | null, runId: string | null): QuoteRead {
  const [quote, setQuote] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    setQuote(null)
    setFailed(false)
    if (!mappingId) return
    let live = true
    fetchRecordDetail(mappingId, runId)
      .then((r) => live && setQuote(r.record.verbatim_quote))
      .catch(() => live && setFailed(true))
    return () => {
      live = false
    }
  }, [mappingId, runId, attempt])
  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return { quote, failed, retry }
}

function PieceText({ pieceId, quote: read, compact = false }: { pieceId: string; quote: QuoteRead; compact?: boolean }) {
  const quote = read.quote
  const { piece, error } = usePiece(pieceId)
  const marked = piece ? markQuote(piece.text, quote) : null
  // A long Piece scrolls inside its box: bring the quote into view there.
  const box = useRef<HTMLDivElement>(null)
  const mark = useRef<HTMLElement>(null)
  useEffect(() => {
    if (box.current && mark.current) {
      box.current.scrollTop = Math.max(0, mark.current.offsetTop - 48)
    }
  }, [marked?.quote, piece?.piece_id])
  const where = piece
    ? [piece.section, pageSpan(piece.format, piece.page, piece.page_end), textSize(piece.text)]
        .filter(Boolean)
        .join(', ')
    : null
  return (
    <section className={`rv-panel rvd-piece${compact ? ' compact' : ''}`} aria-labelledby="rvd-piece-title" data-testid="run-drill-piece">
      <div className="rv-panel-head">
        <h3 id="rvd-piece-title">The section</h3>
        {where && <span className="rv-k">{where}</span>}
      </div>
      {error ? (
        <p className="rvd-empty">{error}</p>
      ) : !piece ? (
        <p className="rvd-empty">Loading the section.</p>
      ) : (
        <>
          <div className="rvd-piece-text law" ref={box} tabIndex={0} aria-label="The section's text">
            {marked ? (
              <>
                {marked.before}
                <mark ref={mark} data-testid="run-drill-quote">{marked.quote}</mark>
                {marked.after}
              </>
            ) : (
              piece.text
            )}
          </div>
          {marked && <div className="rv-k">Highlighted: the Verbatim Quote the Engine selected.</div>}
          {read.failed && (
            <div className="rv-k" role="alert" data-testid="run-drill-quote-error">
              The saved quote could not be read, so it is not highlighted.{' '}
              <button type="button" className="link-btn" onClick={read.retry}>
                Try again
              </button>
            </div>
          )}
        </>
      )}
    </section>
  )
}

function ScaleBar({ scale, label }: { scale: Scale; label: string }) {
  // Every Candidate scored the same: a marker would suggest a rank.
  if (scale.highest - scale.lowest <= 1e-9) return null
  return (
    <div className="rvd-scale">
      <div className="rvd-scale-track" role="img" aria-label={label}>
        <i style={{ left: `${Math.round(scale.position * 100)}%` }} />
      </div>
      <div className="rvd-scale-ends rv-k" aria-hidden="true">
        <span>lowest kept</span>
        <span>highest kept</span>
      </div>
    </div>
  )
}

function GateWhy({ c, d, state }: { c: CandidateView | null; d: DocumentView; state: RunViewState }) {
  if (!c) {
    return (
      <div className="rvd-why" data-testid="run-drill-gate">
        <h3>Why the Gate kept it</h3>
        <p className="rvd-not-recorded" data-testid="run-drill-not-recorded">
          {gateScoresRecorded(state, d) ? 'Its scores are not in this Run’s record.' : NOT_RECORDED}
        </p>
      </div>
    )
  }
  const pillar = state.run?.pillars.length === 1 ? state.run.pillars[0] : Number(c.indicator.split('.')[0]) || null
  const r = gateReasons(c, d, pillar)
  return (
    <div className="rvd-why" data-testid="run-drill-gate">
      <h3>Why the Gate kept it</h3>
      <p>{r.why}</p>
      <div className="rvd-score">
        <div className="rvd-score-head">{r.meaning.headline}</div>
        <ScaleBar scale={r.meaning.scale} label={r.meaning.headline} />
        <div className="rvd-score-num">{r.meaning.detail}</div>
      </div>
      {r.keywords ? (
        <div className="rvd-score">
          <div className="rvd-score-head">{r.keywords.headline}</div>
          <ScaleBar scale={r.keywords.scale} label={r.keywords.headline} />
          <div className="rvd-score-num">{r.keywords.detail}</div>
        </div>
      ) : (
        <p className="rv-k">{r.meaningOnlyNote}</p>
      )}
    </div>
  )
}

function CandidateLayer({
  d,
  state,
  heading,
  pieceId,
  indicator,
  onOpen,
}: {
  d: DocumentView
  state: RunViewState
  heading: HeadingRef
  pieceId: string
  indicator: string
  onOpen: (drill: Drill) => void
}) {
  const c = findCandidate(d, pieceId, indicator)
  const runId = state.run?.run_id ?? null
  const mappingId = c?.mapping_id ?? null
  const saved = mappingId !== null && d.mappingsFound.some((m) => m.mapping_id === mappingId)
  const quote = useQuote(saved ? mappingId : null, runId)
  return (
    <>
      <header className="rvd-head">
        <div className="rv-k">Indicator {indicator}</div>
        <h2 className="rvd-title" ref={heading} tabIndex={-1} data-testid="run-drill-title">
          {c?.section ?? 'One section'}
          {pageSuffix(d.format, c?.page)}
        </h2>
        <div className="rv-k">{d.title}</div>
      </header>
      <p className="rv-lead">One Candidate: the section, why the Gate kept it for this Indicator, and what the Engine made of it.</p>
      <div className="rvd-split">
        <PieceText pieceId={pieceId} quote={quote} />
        <aside className="rv-panel rvd-aside">
          <GateWhy c={c} d={d} state={state} />
          <div className="rvd-why" data-testid="run-drill-outcome">
            <h3>What happened</h3>
            {c ? (
              <>
                <OutcomeMark outcome={c.outcome} />
                <p>{OUTCOME_SENTENCE[c.outcome]}</p>
                {c.outcome === 'mapped' && mappingId && (
                  <button
                    type="button"
                    className="primary"
                    disabled={!saved}
                    onClick={() => onOpen({ kind: 'mapping', document_id: d.document_id, mapping_id: mappingId })}
                  >
                    {saved ? 'See the quote on its page' : 'Saved when this Document is proven'}
                  </button>
                )}
              </>
            ) : (
              <p className="rvd-not-recorded">Not recorded for this Run.</p>
            )}
          </div>
        </aside>
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// Layer 3: one Mapping, its quote on the PDF page
// ---------------------------------------------------------------------------

function MappingLayer({
  d,
  state,
  heading,
  mappingId,
  onClose,
  onOpenEvidence,
}: {
  d: DocumentView
  state: RunViewState
  heading: HeadingRef
  mappingId: string
  onClose: () => void
  onOpenEvidence?: () => void
}) {
  const runId = state.run?.run_id ?? null
  // The panes below step through this Document's Mappings; the Piece and the
  // Gate's reasons above follow whichever one is on screen.
  const start = useRef(mappingId)
  const [current, setCurrent] = useState(mappingId)
  const onCurrent = useCallback((id: string) => setCurrent(id), [])
  const quote = useQuote(current, runId)
  const c = candidateOfMapping(d, current)
  const indicator = current.slice(current.lastIndexOf('::') + 2)
  const pieceId = pieceOfMapping(current)
  const noop = useMemo(() => () => {}, [])

  return (
    <>
      <header className="rvd-head">
        <div className="rv-k">Indicator {indicator}</div>
        <h2 className="rvd-title" ref={heading} tabIndex={-1} data-testid="run-drill-title">
          One Mapping from {d.title}
        </h2>
      </header>
      <p className="rv-lead">The section it came from, why the Gate kept it, and its Verbatim Quote highlighted in the source.</p>
      <div className="rvd-split">
        <PieceText pieceId={pieceId} quote={quote} compact />
        <aside className="rv-panel rvd-aside">
          <GateWhy c={c} d={d} state={state} />
          <div className="rvd-why rvd-proven">
            <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
              <circle cx="10" cy="10" r="9" fill="var(--route)" />
              <path d="M5.5 10.5 L8.5 13.5 L14.5 7" fill="none" stroke="var(--surface)" strokeWidth="2" />
            </svg>
            <div>
              <h3>Proven</h3>
              <p>The quote appears word for word in the Document's stored text.</p>
            </div>
          </div>
        </aside>
      </div>
      <section className="rvd-page" aria-label="The quote in its source" data-testid="run-drill-page">
        {runId ? (
          <AuditView
            documentId={d.document_id}
            runId={runId}
            startMappingId={start.current}
            onBack={onClose}
            onReviewSaved={noop}
            onCurrent={onCurrent}
            readOnly
            onOpenEvidence={onOpenEvidence}
          />
        ) : (
          <p className="rvd-empty">This Run has no Run id, so its pages cannot be opened.</p>
        )}
      </section>
    </>
  )
}
