import { useEffect, useRef, useState, type ReactNode } from 'react'
import { isInShell } from './keys'
import RunDrill from './RunDrill'
import type { Drill } from './drillDown'
import { fetchEngines, fetchStatus } from './api'
import { notRecordedSentences } from './replayControl'
import {
  STATIONS,
  STATION_LABEL,
  STATION_SHORT,
  TIMED_LABEL,
  candidatesMapped,
  documentBreakdown,
  documentState,
  duration,
  elapsed,
  failureSentence,
  flow,
  money,
  num,
  stationOf,
  stations,
  stationsLabel,
  stepsWithNoWork,
  technicalMessage,
  timing,
  utcClock,
  zeroReason,
  type DocumentBreakdown,
  type DocumentTiming,
  type Span,
  type StationState,
  type TimedStep,
  type Timing,
} from './runOverview'
import { pillarName } from './runSetup'
import { documentSize } from './sourceText'
import type { DocumentView, RunViewState } from './runViewState'
import './RunView.css'

// The Run as it happens, and as it ended: who ran what, the flow of numbers
// from Documents to Mappings proven, one line per Document with its route
// through the Steps, Reconcile once at the end, and where the time went.
// Built from the Run's typed events by the reducer in runViewState.ts; the
// words and numbers come from runOverview.ts.

const NB = ' '

// ---------------------------------------------------------------------------
// Names: the screen says Singapore and Engine A, never SG and engine-a
// ---------------------------------------------------------------------------

interface Names {
  economies: Record<string, string>
  engines: Record<string, string>
}

let namesCache: Names | null = null
let namesLoading: Promise<Names> | null = null

function loadNames(): Promise<Names> {
  if (namesCache) return Promise.resolve(namesCache)
  namesLoading ??= Promise.all([
    fetchStatus().catch(() => null),
    fetchEngines().catch(() => null),
  ]).then(([status, engines]) => {
    namesCache = {
      economies: status?.economy_names ?? {},
      engines: Object.fromEntries((engines?.engines ?? []).map((e) => [e.name, e.display_name])),
    }
    return namesCache
  })
  return namesLoading
}

function useNames(): Names {
  const [names, setNames] = useState<Names>(namesCache ?? { economies: {}, engines: {} })
  useEffect(() => {
    let alive = true
    loadNames().then((n) => alive && setNames(n))
    return () => {
      alive = false
    }
  }, [])
  return names
}

/** "Engine A: GPT-5.6 Luna" is the full name; "Engine A" the short one. */
function engineNames(names: Names, code: string): { short: string; full: string } {
  const full = names.engines[code]
  if (full) return { short: full.split(':')[0].trim(), full }
  const m = /^engine-([a-z])$/i.exec(code)
  const short = m ? `Engine ${m[1].toUpperCase()}` : code === 'fake' ? 'Fake Engine' : code
  return { short, full: short }
}

// ---------------------------------------------------------------------------
// The clock: a live Run's figures move every second, a replay's with its events
// ---------------------------------------------------------------------------

function useNow(ticking: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!ticking) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [ticking])
  return now
}

// ---------------------------------------------------------------------------
// The glyphs: every state has its own shape, not only its own colour
// ---------------------------------------------------------------------------

export function Glyph({ state }: { state: StationState }) {
  const common = { width: 18, height: 18, viewBox: '0 0 18 18', 'aria-hidden': true as const }
  switch (state) {
    case 'done':
      return (
        <svg {...common}>
          <circle cx="9" cy="9" r="6" fill="var(--route)" />
        </svg>
      )
    case 'skipped':
      return (
        <svg {...common}>
          <circle cx="9" cy="9" r="5.5" fill="var(--surface)" stroke="var(--route)" strokeWidth="1.5" strokeDasharray="2.2 2" />
        </svg>
      )
    case 'active':
      return (
        <svg {...common}>
          <circle cx="9" cy="9" r="7.5" fill="var(--surface)" stroke="var(--route)" strokeWidth="2" />
          <circle className="rv-pulse" cx="9" cy="9" r="3" fill="var(--route)" />
        </svg>
      )
    case 'flag':
      return (
        <svg {...common}>
          <path d="M9 1.5 L16.5 9 L9 16.5 L1.5 9 Z" fill="var(--flag)" />
          <rect x="8.2" y="5" width="1.6" height="5.2" fill="var(--surface)" />
          <rect x="8.2" y="11.4" width="1.6" height="1.6" fill="var(--surface)" />
        </svg>
      )
    case 'fail':
      return (
        <svg {...common}>
          <circle cx="9" cy="9" r="7.5" fill="var(--fail)" />
          <path d="M6 6 L12 12 M12 6 L6 12" stroke="var(--surface)" strokeWidth="1.8" />
        </svg>
      )
    case 'zero':
      return (
        <svg {...common}>
          <circle cx="9" cy="9" r="6" fill="none" stroke="var(--ink)" strokeWidth="2" />
          <path d="M5 13 L13 5" stroke="var(--ink)" strokeWidth="1.6" />
        </svg>
      )
    default:
      return (
        <svg {...common}>
          <circle cx="9" cy="9" r="5" fill="var(--surface)" stroke="var(--wait)" strokeWidth="1.5" />
        </svg>
      )
  }
}

const REACHED: StationState[] = ['done', 'skipped', 'active', 'flag', 'fail', 'zero']

function Rail({ doc, caption }: { doc: DocumentView; caption: string | null }) {
  const states = stations(doc)
  return (
    <div className="rv-rail" role="img" aria-label={`${doc.title}: ${stationsLabel(doc)}`}>
      {states.map((st, i) => {
        const left = i > 0 && REACHED.includes(st)
        const right = i < states.length - 1 && REACHED.includes(states[i + 1])
        return (
          <div className="rv-station" key={STATIONS[i]}>
            {i > 0 && <i className={left ? 'rv-link l on' : 'rv-link l'} />}
            {i < states.length - 1 && <i className={right ? 'rv-link r on' : 'rv-link r'} />}
            <Glyph state={st} />
            {st === 'active' && caption && <span className="rv-cap">{caption}</span>}
          </div>
        )
      })}
    </div>
  )
}

function Bar({ value, label }: { value: number; label: string }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100)
  return (
    <div className="rv-prog" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
      <i style={{ transform: `scaleX(${pct / 100})` }} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// One Document's line
// ---------------------------------------------------------------------------

const plural = (n: number, one: string, many = `${one}s`) => `${num(n)} ${n === 1 ? one : many}`

function stepSince(d: DocumentView, now: number): number | null {
  if (d.step === null) return null
  const t = d.step === 'prove' ? (d.times.map ?? d.times.prove) : d.times[d.step]
  return t ? Math.max(0, now - t.start) : null
}

function DocumentLine({
  d,
  now,
  live,
  stopped,
  onOpen,
}: {
  d: DocumentView
  now: number
  live: boolean
  stopped: boolean
  onOpen?: (documentId: string) => void
}) {
  const state = documentState(d)
  const kept = d.counts.gate?.candidates
  const pieces = d.counts.cut?.pieces
  const station = d.step ? stationOf(d.step) : null
  const onMap = station === 'map_prove' && d.mapTotal > 0
  const since = live ? stepSince(d, now) : null
  const caption = onMap ? `${d.mapDone}/${d.mapTotal}` : since !== null ? elapsed(since) : null

  let outcome: ReactNode
  if (state === 'waiting') outcome = <span className="rv-out-note">{stopped ? 'Not reached' : 'Waiting its turn'}</span>
  else if (state === 'working') {
    outcome = onMap ? (
      <>
        <span className="rv-out-now">
          Map and Prove: {d.mapDone} of {plural(d.mapTotal, 'Candidate')}
        </span>
        <Bar value={d.mapDone / d.mapTotal} label={`${d.title}: Candidates mapped`} />
      </>
    ) : (
      <span className="rv-out-now">
        {station ? STATION_LABEL[station] : 'Working'}
        {since !== null ? `, ${elapsed(since)}` : ''}
      </span>
    )
  } else if (state === 'finished') {
    outcome = (
      <>
        <b>{num(d.mappings ?? 0)}</b>
        <span className="rv-out-note">Mapping{d.mappings === 1 ? '' : 's'} from {plural(kept ?? 0, 'Candidate')}</span>
      </>
    )
  } else if (state === 'zero') {
    outcome = (
      <>
        <b>0</b>
        <span className="rv-out-note">{zeroReason(d)}</span>
      </>
    )
  } else {
    outcome = (
      <>
        <span className="rv-out-fail">Stopped at {station ? STATION_LABEL[station] : 'its first Step'}</span>
        <span className="rv-out-note">Nothing after this was run</span>
      </>
    )
  }

  const meta = [
    documentSize(d.format, d.n_pages),
    pieces !== undefined ? plural(pieces, 'section') : null,
    languageName(d.language),
  ].filter(Boolean)

  // On a phone the stations give way to one line and a bar.
  const phoneLine =
    state === 'working'
      ? `${station ? STATION_LABEL[station] : 'Working'}${onMap ? `: ${d.mapDone} of ${d.mapTotal}` : since !== null ? `, ${elapsed(since)}` : ''}`
      : state === 'waiting'
        ? stopped
          ? 'Not reached'
          : 'Waiting its turn'
        : state === 'failed'
          ? `Stopped at ${station ? STATION_LABEL[station] : 'its first Step'}`
          : state === 'zero'
            ? `Finished: 0 Mappings. ${zeroReason(d) ?? ''}`
            : `Finished: ${plural(d.mappings ?? 0, 'Mapping')}`

  return (
    <li
      className={`rv-row is-${state}${d.flag ? ' is-flagged' : ''}${onOpen ? ' is-openable' : ''}`}
      data-testid="run-view-document"
      data-state={state}
      onClick={
        onOpen
          ? (e) => {
              // The title is the button; the rest of the row is a larger
              // target for a pointer, never a second stop for the keyboard.
              if (!(e.target as HTMLElement).closest('button')) onOpen(d.document_id)
            }
          : undefined
      }
    >
      <div className="rv-doc">
        {onOpen ? (
          <button
            type="button"
            className="rv-doc-title rv-doc-open law"
            data-document-id={d.document_id}
            onClick={() => onOpen(d.document_id)}
          >
            {d.title}
          </button>
        ) : (
          <div className="rv-doc-title law">{d.title}</div>
        )}
        <div className="rv-doc-meta">{meta.join(', ')}</div>
        {d.flag && (
          <div className="rv-flag">
            <FlagMark /> Scan flagged: {d.flag}. The Run still mapped it; check its Mappings against the page.
          </div>
        )}
      </div>
      <Rail doc={d} caption={caption} />
      <div className="rv-out">{outcome}</div>
      <div className="rv-phone">
        <span className={state === 'failed' ? 'rv-out-fail' : state === 'working' ? 'rv-out-now' : 'rv-out-note'}>{phoneLine}</span>
        <Bar value={d.progress} label={d.title} />
      </div>
    </li>
  )
}

const LANGUAGES: Record<string, string> = {
  en: 'English', zh: 'Chinese', id: 'Indonesian', ms: 'Malay', hi: 'Hindi', lo: 'Lao', th: 'Thai',
}

function languageName(code: string | null): string | null {
  if (!code) return null
  return LANGUAGES[code.toLowerCase()] ?? code
}

function FlagMark() {
  return (
    <svg width="12" height="12" viewBox="0 0 18 18" aria-hidden="true" className="rv-flag-mark">
      <path d="M9 1.5 L16.5 9 L9 16.5 L1.5 9 Z" fill="var(--flag)" />
      <rect x="8.2" y="5" width="1.6" height="5.2" fill="var(--surface)" />
      <rect x="8.2" y="11.4" width="1.6" height="1.6" fill="var(--surface)" />
    </svg>
  )
}

function ReconcileLine({ state, t }: { state: RunViewState; t: Timing | null }) {
  const r = state.reconciled
  const status = state.reconcile
  const remaining = state.documents.filter((d) => !d.done).length
  const took =
    t?.reconcile && !t.reconcile.open ? timed(t.reconcile.end - t.reconcile.start, t.estimated) : null
  return (
    <li className={`rv-row rv-reconcile is-${status === 'done' ? 'finished' : status === 'running' ? 'working' : 'waiting'}`}>
      <div className="rv-doc">
        <div className="rv-doc-title">Reconcile</div>
        <div className="rv-doc-meta">Once for the whole Run, after the last Document</div>
      </div>
      <div className="rv-reconcile-what">
        <Glyph state={status === 'done' ? 'done' : status === 'running' ? 'active' : 'waiting'} />
        <span>
          {status === 'done' && r
            ? `${plural(r.before, 'Mapping')} grouped into ${plural(r.after, 'group')}, one controlling Mapping per Indicator${took ? ` (${took})` : ''}.`
            : status === 'running'
              ? 'Grouping the proven Mappings per Indicator and picking the one that controls.'
              : 'Groups the proven Mappings per Indicator and picks the one that controls.'}
        </span>
      </div>
      <div className="rv-out">
        {status === 'done' && r ? (
          <>
            <b>{num(r.after)}</b>
            <span className="rv-out-note">group{r.after === 1 ? '' : 's'}</span>
          </>
        ) : status === 'running' ? (
          <span className="rv-out-now">Working</span>
        ) : state.status === 'failed' ? (
          <span className="rv-out-note">Not reached</span>
        ) : (
          <span className="rv-out-note">
            {remaining > 0 ? `Waits for ${plural(remaining, 'more Document')}` : 'Next'}
          </span>
        )}
      </div>
      <div className="rv-phone">
        <span className="rv-out-note">
          {status === 'done' && r
            ? `${num(r.before)} Mappings into ${plural(r.after, 'group')}`
            : status === 'running'
              ? 'Working'
              : state.status === 'failed'
                ? 'Not reached'
                : 'Waiting'}
        </span>
      </div>
    </li>
  )
}

// ---------------------------------------------------------------------------
// The flow of numbers
// ---------------------------------------------------------------------------

function FlowDiagram({ state }: { state: RunViewState }) {
  const f = flow(state)
  const finished = state.status === 'finished'
  const stopped = state.status === 'failed'
  const nodes: { n: number; label: string; detail: string; on: boolean }[] = [
    {
      n: f.documents,
      label: f.documents === 1 ? 'Document' : 'Documents',
      detail: finished ? 'all read' : `${num(f.documentsDone)} finished`,
      on: f.documents > 0,
    },
    {
      n: f.pieces,
      label: 'Sections',
      detail: 'the law text cut at its numbered sections',
      on: f.reached.pieces,
    },
    {
      n: f.candidates,
      label: 'Candidates kept by the Gate',
      detail: `of ${num(f.pairs)} section and Indicator pairs checked`,
      on: f.reached.candidates,
    },
    {
      n: f.proposed,
      label: 'Mappings proposed',
      detail: `the Engine picked a quote; ${num(f.noEvidence)} Candidates had no evidence`,
      on: f.reached.proposed,
    },
    {
      n: f.proven,
      label: 'Mappings proven',
      detail: `quote found word for word; ${num(f.dropped)} dropped`,
      on: f.reached.proposed,
    },
  ]
  return (
    <section className="rv-panel rv-flow" aria-labelledby="rv-flow-title">
      <div className="rv-panel-head">
        <h3 id="rv-flow-title">
          {finished ? 'What the Run found' : stopped ? 'What the Run found before it stopped' : 'What the Run has found so far'}
        </h3>
        <span className="rv-k">
          {finished ? 'Final numbers' : stopped ? 'Nothing after the stop is counted' : 'Numbers grow as each Document goes through its Steps'}
        </span>
      </div>
      <ol className="rv-flow-nodes">
        {nodes.map((node) => (
          <li key={node.label} className={node.on ? 'rv-node on' : 'rv-node'}>
            <span className="rv-node-dot" aria-hidden="true" />
            <span className="rv-node-n">{num(node.n)}</span>
            <span className="rv-node-label">{node.label}</span>
            <span className="rv-k">{node.on ? node.detail : 'not reached yet'}</span>
          </li>
        ))}
      </ol>
      {f.groups !== null && (
        <p className="rv-flow-foot">
          Reconcile then grouped the {plural(f.proven, 'proven Mapping')} into {plural(f.groups, 'group')}, one per
          Indicator found, each with one controlling Mapping.
        </p>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Where the time went
// ---------------------------------------------------------------------------

const NO_WORK_NOTE: Partial<Record<TimedStep, string>> = {
  scan_check: 'No page needed OCR: every Document has a text layer',
  gloss: 'No quote needed an English rendering',
}

const STEP_NOTE: Record<TimedStep, string> = {
  read: "The Document's text, reused when the Corpus already read it",
  scan_check: 'Pages with no text layer are read by OCR',
  cut: 'Split at numbered sections',
  gate: 'Meaning and keyword match, on this machine',
  map_prove: 'The Engine picks a quote; each is checked word for word',
  gloss: 'English renderings of quotes in other languages',
  reconcile: 'Once, after the last Document',
}

/** The minute marks under the Document bars: at most seven, on a round step. */
function ticks(spanMs: number): number[] {
  const minutes = spanMs / 60000
  const steps = [1 / 60, 1 / 30, 1 / 12, 1 / 6, 0.25, 0.5, 1, 2, 3, 5, 10, 15, 20, 30, 60]
  const step = steps.find((s) => minutes / s <= 6) ?? 60
  const out: number[] = []
  for (let i = 0; i * step <= minutes + 1e-9; i++) out.push(i * step)
  return out
}

function tickLabel(m: number): string {
  if (m === 0) return '0'
  if (m < 1) return `${Math.round(m * 60)}${NB}s`
  if (!Number.isInteger(m)) return `${m.toFixed(1)}${NB}min`
  return `${m}${NB}min`
}

function Segment({ span, t, kind, label }: { span: Span; t: Timing; kind: 'gate' | 'map' | 'reconcile'; label?: string }) {
  const total = Math.max(1, t.end - t.start)
  const left = ((span.start - t.start) / total) * 100
  const width = Math.max(0.3, ((span.end - span.start) / total) * 100)
  return (
    <i
      className={`rv-seg rv-seg-${kind}${span.open ? ' open' : ''}`}
      style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
      title={label}
    />
  )
}

/** A duration, with "about" when the Run's Step times are estimated. */
function timed(ms: number, estimated: boolean): string {
  const d = duration(ms)
  // "under 0.01 s" is already a bound: "about" in front of it says nothing.
  return estimated && !d.startsWith('under') ? `about ${d}` : d
}

function docTimeLabel(d: DocumentTiming, stopped: boolean, estimated: boolean): string {
  if (!d.span) return stopped ? 'not reached' : 'waiting'
  const parts: string[] = []
  if (d.gate) parts.push(`Gate ${timed(d.gate.end - d.gate.start, estimated)}`)
  if (d.mapProve) parts.push(`Map and Prove ${timed(d.mapProve.end - d.mapProve.start, estimated)}`)
  return parts.length ? parts.join(', ') : timed(d.span.end - d.span.start, estimated)
}

/** One Document's time, Step by Step: shown beside its row in the chart while
 *  the row is hovered or focused. */
function LaneTip({ b, id, stopped }: { b: DocumentBreakdown; id: string; stopped: boolean }) {
  const estimated = !b.recorded
  const total =
    b.totalMs === null
      ? stopped
        ? 'Not reached'
        : 'Not started yet'
      : `${timed(b.totalMs, estimated)} ${b.open ? 'so far' : 'in total'}`
  return (
    <div className="rv-tip" role="tooltip" id={id}>
      <div className="rv-tip-title">{b.title}</div>
      <div className="rv-tip-total">{total}</div>
      {estimated && (
        <p className="rv-tip-note">
          Step times were not recorded for this Run. The Gate and Map and Prove times are estimated from its log.
        </p>
      )}
      <ul className="rv-tip-steps">
        {b.parts.map((p) => (
          <li key={p.station}>
            <span className="rv-tip-name">
              <i className={`rv-swatch step-${p.station}`} aria-hidden="true" />
              {p.label}
            </span>
            <span className="rv-tip-bar" aria-hidden="true">
              {p.status === 'timed' && (
                <i className={`step-${p.station}`} style={{ width: `${Math.max(1.5, p.share * 100)}%` }} />
              )}
            </span>
            <span className="rv-tip-time">
              {p.status === 'timed'
                ? `${timed(p.ms, estimated)}${p.open ? ' so far' : ''}`
                : p.status === 'not_recorded'
                  ? 'not recorded'
                  : stopped
                    ? 'not reached'
                    : 'not yet'}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function TimeWent({ t, stopped, noWork }: { t: Timing; stopped: boolean; noWork: Set<TimedStep> }) {
  // The Document row whose breakdown is open: the one under the pointer, else
  // the focused one. Esc hides it until the pointer or focus moves on.
  const [hovered, setHovered] = useState<string | null>(null)
  const [focused, setFocused] = useState<string | null>(null)
  const [hidden, setHidden] = useState<string | null>(null)
  const shown = hovered ?? focused
  const tipFor = shown !== null && shown !== hidden ? shown : null
  const total = t.end - t.start
  const visible = t.byStep.filter((s) => s.share >= 0.005)
  const tiny = t.byStep.filter((s) => s.reached && !noWork.has(s.step) && s.share < 0.01).map((s) => TIMED_LABEL[s.step])
  const head = t.live ? `${duration(total)} so far` : `${duration(total)} in total`
  const nowPct = t.live ? 100 : null
  return (
    <section className="rv-panel rv-time" aria-labelledby="rv-time-title">
      <div className="rv-panel-head">
        <h3 id="rv-time-title">Where the time went</h3>
        <span className="rv-k">
          {head}.{' '}
          {t.estimated
            ? 'Times estimated from the Run\'s log.'
            : 'Timed by the Run itself.'}
        </span>
      </div>

      <div className="rv-time-block">
        <div className="rv-k">By Step</div>
        <div className="rv-stack" role="img" aria-label={visible.map((s) => `${TIMED_LABEL[s.step]} ${Math.round(s.share * 100)}%`).join(', ')}>
          {visible.map((s) => (
            // data-narrow: on a phone (a bar about 310 px wide) this label would be cut, so it hides there
            <i key={s.step} className={`rv-stack-seg step-${s.step}`} style={{ width: `${s.share * 100}%` }} title={`${TIMED_LABEL[s.step]}: ${duration(s.ms)}`} data-narrow={s.share * 310 < TIMED_LABEL[s.step].length * 7 + 12 ? '' : undefined}>
              {s.share >= 0.12 && <span>{TIMED_LABEL[s.step]}</span>}
            </i>
          ))}
        </div>
      </div>

      <table className="rv-steps">
        <caption className="sr-only">Time per Step</caption>
        <thead>
          <tr>
            <th scope="col">Step</th>
            <th scope="col" className="num">
              Time
            </th>
            <th scope="col" className="note">
              What it does
            </th>
            <th scope="col" className="num">
              Share
            </th>
          </tr>
        </thead>
        <tbody>
          {t.byStep.map((s) => (
            <tr key={s.step}>
              <th scope="row">
                <i className={`rv-swatch step-${s.step}`} aria-hidden="true" />
                {TIMED_LABEL[s.step]}
              </th>
              <td className="num">
                {!s.reached ? (t.live ? 'not yet' : 'not run') : noWork.has(s.step) ? 'skipped' : timed(s.ms, t.estimated)}
              </td>
              <td className="note">{noWork.has(s.step) ? NO_WORK_NOTE[s.step] : STEP_NOTE[s.step]}</td>
              <td className="num">
                {s.reached && !noWork.has(s.step) && s.share >= 0.005 ? `${Math.round(s.share * 100)}%` : ''}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="rv-time-block">
        <div className="rv-k rv-legend">
          By Document, in Run order:
          <span>
            <i className="rv-swatch step-gate" aria-hidden="true" /> Gate (striped)
          </span>
          <span>
            <i className="rv-swatch step-map_prove" aria-hidden="true" /> Map and Prove (solid)
          </span>
          {tiny.length > 0 && <span>{tiny.join(', ')} are too short to see.</span>}
          <span>Point at a Document, or tab to it, for its time Step by Step.</span>
        </div>
        <ol className="rv-lanes">
          {t.byDocument.map((d, i) => {
            const tipId = `rv-tip-${i}`
            const open = tipFor === d.document_id
            return (
              <li
                key={d.document_id}
                className={`rv-lane rv-lane-doc${open ? ' tip-open' : ''}`}
                tabIndex={0}
                aria-describedby={open ? tipId : undefined}
                onMouseEnter={() => {
                  setHovered(d.document_id)
                  setHidden(null)
                }}
                onMouseLeave={() => setHovered((h) => (h === d.document_id ? null : h))}
                onFocus={() => {
                  setFocused(d.document_id)
                  setHidden(null)
                }}
                onBlur={() => setFocused((f) => (f === d.document_id ? null : f))}
                onKeyDown={(e) => {
                  if (e.key !== 'Escape' || !open) return
                  // Handled here: the drill-down's own Esc must not also fire.
                  e.preventDefault()
                  e.stopPropagation()
                  setHidden(d.document_id)
                }}
              >
                <span className="rv-lane-name">{d.title}</span>
                <span className="rv-track">
                  {d.gate && <Segment span={d.gate} t={t} kind="gate" />}
                  {d.mapProve && <Segment span={d.mapProve} t={t} kind="map" />}
                  {nowPct !== null && <i className="rv-now" style={{ left: `${nowPct}%` }} aria-hidden="true" />}
                </span>
                <span className="rv-lane-time">
                  {docTimeLabel(d, stopped, t.estimated)}
                  {d.span?.open ? ' so far' : ''}
                </span>
                {open && <LaneTip b={documentBreakdown(d, t.estimated)} id={tipId} stopped={stopped} />}
              </li>
            )
          })}
          <li className="rv-lane">
            <span className="rv-lane-name">Reconcile, once</span>
            <span className="rv-track">
              {t.reconcile && <Segment span={t.reconcile} t={t} kind="reconcile" label={`Reconcile ${duration(t.reconcile.end - t.reconcile.start)}`} />}
            </span>
            <span className="rv-lane-time">
              {t.reconcile
                ? `${timed(t.reconcile.end - t.reconcile.start, t.estimated)}${t.reconcile.open ? ' so far' : ''}`
                : stopped
                  ? 'not reached'
                  : 'waiting'}
            </span>
          </li>
        </ol>
        <div className="rv-lane rv-axis" aria-hidden="true">
          <span />
          <span className="rv-track">
            {ticks(total).map((m) => (
              <span key={m} style={{ left: `${Math.min(100, ((m * 60000) / Math.max(1, total)) * 100)}%` }}>
                {tickLabel(m)}
              </span>
            ))}
          </span>
          <span />
        </div>
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// The raw log: a terminal, behind a toggle
// ---------------------------------------------------------------------------

function LogLine({ line }: { line: string }) {
  const m = /^([A-Z][\w/ +]*?) \| (\S+): (.*)$/.exec(line)
  if (!m) return <div>{line}</div>
  return (
    <div>
      <span className="rv-log-tag">{m[1]} |</span> <span className="rv-log-doc">{m[2]}</span>: {m[3]}
    </div>
  )
}

function RawLog({ lines, live, onClose }: { lines: string[]; live: boolean; onClose: () => void }) {
  const body = useRef<HTMLDivElement>(null)
  const close = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    close.current?.focus()
  }, [])
  useEffect(() => {
    if (body.current) body.current.scrollTop = body.current.scrollHeight
  }, [lines.length])
  return (
    <aside
      className="rv-log"
      role="dialog"
      aria-modal="false"
      aria-labelledby="rv-log-title"
      data-testid="run-log"
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation()
          onClose()
        }
      }}
    >
      <div className="rv-log-head">
        <span id="rv-log-title" className="rv-log-title">
          Raw log
        </span>
        <span className="rv-log-key">
          The Run's own lines. M1 Read, M2 Scan check, M4 Cut, M5 Gate, M6 Map, M7 Prove, M3 Gloss, M8 Reconcile.
        </span>
        <button ref={close} className="rv-log-close" onClick={onClose}>
          Close
        </button>
      </div>
      <div className="rv-log-body" ref={body}>
        {lines.map((line, i) => (
          <LogLine key={i} line={line} />
        ))}
        {live && (
          <div>
            <span className="rv-log-tag">$</span> <span className="rv-cursor">{'▌'}</span>
          </div>
        )}
      </div>
    </aside>
  )
}

// ---------------------------------------------------------------------------
// The whole view
// ---------------------------------------------------------------------------

export interface RunViewProps {
  state: RunViewState
  /** A Run going now: its clock follows the wall clock. A replay's follows
   *  its own events. */
  live?: boolean
  /** The Run's free-text lines, for the raw log. */
  lines?: string[]
  /** Open the finished Run's Mappings in Evidence. */
  onOpenEvidence?: (runId: string) => void
}

export default function RunView({ state, live = false, lines = [], onOpenEvidence }: RunViewProps) {
  const names = useNames()
  const running = state.status === 'running'
  const wall = useNow(live && running)
  const [logOpen, setLogOpen] = useState(false)
  const logButton = useRef<HTMLButtonElement>(null)
  // One level deeper: a Document, a Candidate or a Mapping, or null for the
  // overview. Closing returns focus to the Document it was opened from.
  const [drill, setDrill] = useState<Drill | null>(null)
  const returnTo = useRef<string | null>(null)
  const openDrill = (next: Drill) => {
    if (drill === null) returnTo.current = next.document_id
    setDrill(next)
  }
  const closeDrill = () => setDrill(null)
  // A new Run on screen: whatever was open belonged to the last one.
  const runKey = state.run?.run_id ?? null
  useEffect(() => {
    setDrill(null)
  }, [runKey])
  useEffect(() => {
    if (drill !== null) return
    const id = returnTo.current
    if (id === null) return
    returnTo.current = null
    const button = Array.from(document.querySelectorAll<HTMLButtonElement>('.rv-doc-open')).find(
      (b) => b.dataset.documentId === id,
    )
    button?.focus({ preventScroll: true })
    button?.scrollIntoView({ block: 'center' })
  }, [drill])
  useEffect(() => {
    if (drill === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented || isInShell(e)) return
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return
      setDrill(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drill])
  const heading = useRef<HTMLHeadingElement>(null)
  // A Run started from the setup above: bring its view on screen once.
  const shownRun = useRef<string | null>(null)
  const liveRunKey = live && state.status === 'running' ? (state.run?.run_id ?? String(state.startedAt)) : null
  useEffect(() => {
    if (liveRunKey === null || shownRun.current === liveRunKey) return
    shownRun.current = liveRunKey
    heading.current?.scrollIntoView({ block: 'start', behavior: 'auto' })
    heading.current?.focus({ preventScroll: true })
  }, [liveRunKey])
  if (state.status === 'idle' && state.documents.length === 0) return null

  const { run, documents } = state
  // A replay's "now" is the moment its latest event was recorded.
  const now = live && running ? Math.max(wall, state.lastAt ?? wall) : (state.lastAt ?? wall)
  const t = timing(state, now)
  const economy = run ? (names.economies[run.economy] ?? run.economy) : ''
  const engine = run ? engineNames(names, run.engine) : null
  const pillars = run?.pillars ?? []
  const finishedDocs = documents.filter((d) => d.done).length
  const current = documents.find((d) => documentState(d) === 'working')
  const currentStation = current?.step ? stationOf(current.step) : null
  const started = state.startedAt !== null ? utcClock(state.startedAt) : null
  const ended = state.endedAt !== null ? utcClock(state.endedAt) : null
  const cm = candidatesMapped(state)
  const failure = failureSentence(state)
  const took = t ? t.end - t.start : 0
  const calls = state.meter?.engine_calls
  const cost = state.meter?.cost_usd ?? state.cost_usd
  // A replay rebuilt from a log: its clock between start and end is estimated.
  const estimated = t?.estimated ?? false

  const title = run
    ? `${economy}, ${pillars.length === 1 ? 'Pillar' : 'Pillars'} ${pillars.join(', ')}`
    : 'Run'
  const pillarLine =
    pillars.length === 1 && pillarName(pillars[0]) ? `${pillarName(pillars[0])}. ` : ''
  const indicatorLine = run?.indicators?.length
    ? `Indicator${run.indicators.length === 1 ? '' : 's'} ${run.indicators.join(', ')}. `
    : 'All its Indicators. '

  const pill =
    state.status === 'finished' ? (
      <span className="rv-pill done">
        <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
          <path d="M2 6.5 L5 9 L10 3" fill="none" stroke="var(--route)" strokeWidth="1.8" />
        </svg>
        Finished
      </span>
    ) : state.status === 'failed' ? (
      <span className="rv-pill fail">
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
          <path d="M2 2 L8 8 M8 2 L2 8" stroke="currentColor" strokeWidth="1.8" />
        </svg>
        Stopped
      </span>
    ) : (
      <span className="rv-pill">
        <svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">
          <circle cx="4" cy="4" r="4" fill="var(--route)" className="rv-pulse-soft" />
        </svg>
        Running
      </span>
    )

  const sub =
    state.status === 'running'
      ? `${started ? `Started ${started}. ` : ''}${finishedDocs} of ${documents.length} Documents finished.`
      : state.status === 'finished'
        ? `${started && ended ? `${started} to ${ended}. ` : ''}Saved as a Run Record.`
        : `${started ? `Started ${started}. ` : ''}${finishedDocs} of ${documents.length} Documents finished before it stopped.`

  const statItems: [string, string][] =
    state.status === 'running'
      ? [
          ['Elapsed', estimated ? `about ${elapsed(took)}` : elapsed(took)],
          ['Cost so far (our meter)', cost === null || cost === undefined ? (run?.recordedFrom ? 'not recorded' : money(0)) : money(cost)],
          ['Candidates mapped', `${num(cm.done)} of ${num(cm.kept)}`],
        ]
      : [
          [state.status === 'failed' ? 'Ran for' : 'Took', duration(took)],
          ['Cost (our meter)', money(cost ?? null)],
          ['Engine calls', calls === null || calls === undefined ? 'not recorded' : num(calls)],
        ]

  const billedLine =
    state.status === 'finished' && cost !== null && cost !== undefined && state.providerCost !== null
      ? `${money(cost)} at the Engine's declared prices. The provider billed ${money(state.providerCost)}.`
      : null

  const toggleLog = () => {
    setLogOpen((open) => {
      if (open) logButton.current?.focus()
      return !open
    })
  }

  return (
    <section className={`rv is-${state.status}`} data-testid="run-view" aria-labelledby="rv-title">
      <header className="rv-head">
        <div className="rv-id">
          <div className="rv-status" role="status">
            {pill}
            <span className="rv-k">{sub}</span>
          </div>
          <h2 className="rv-title law" id="rv-title" ref={heading} tabIndex={-1}>
            {title}
          </h2>
          <p className="rv-what">
            {pillarLine}
            {indicatorLine}
            {engine ? `${engine.full}.` : ''}
          </p>
        </div>
        <div className="rv-side">
          <div className="rv-actions">
            {lines.length > 0 && (
              <button ref={logButton} className="btn" onClick={toggleLog} aria-expanded={logOpen}>
                {logOpen ? 'Hide raw log' : 'Show raw log'}
              </button>
            )}
            {state.status === 'finished' && run?.run_id && onOpenEvidence && (
              <button className="primary" onClick={() => onOpenEvidence(run.run_id!)}>
                Open Evidence
              </button>
            )}
          </div>
          <dl className="rv-stats">
            {statItems.map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
          {billedLine && (
            <p className="rv-billed" data-testid="run-view-billed">
              {billedLine}
            </p>
          )}
        </div>
      </header>

      {drill !== null && (
        <RunDrill
          drill={drill}
          state={state}
          runTitle={title}
          onOpen={openDrill}
          onClose={closeDrill}
          onOpenEvidence={onOpenEvidence && run?.run_id ? () => onOpenEvidence(run.run_id!) : undefined}
        />
      )}

      {drill === null && (<>
      <p className="rv-lead">
        The same Steps run for each Document, then Reconcile once.
        {current && currentStation ? ` Now: ${current.title}, ${STATION_LABEL[currentStation]}.` : ''}
      </p>

      {run && run.notRecorded.length > 0 && (
        <p className="rv-note" data-testid="run-view-not-recorded">
          {run.recordedFrom === 'log' ? 'This Run was made before the app recorded its events, so it was rebuilt from its text log. ' : ''}
          {notRecordedSentences(run.notRecorded).join(' ')}
        </p>
      )}

      {failure && state.failure && (
        <div className="rv-failure" role="alert" data-testid="run-view-failure">
          <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
            <circle cx="10" cy="10" r="9" fill="var(--fail)" />
            <path d="M6.5 6.5 L13.5 13.5 M13.5 6.5 L6.5 13.5" stroke="var(--surface)" strokeWidth="2" />
          </svg>
          <div>
            <b>Run stopped</b>
            <p>
              {failure} Start the Run again once the cause is fixed
              {lines.length > 0 ? '; the raw log has the details.' : '.'}
            </p>
            <details>
              <summary>Show the technical message</summary>
              <code>{technicalMessage(state.failure.message)}</code>
            </details>
          </div>
        </div>
      )}

      <FlowDiagram state={state} />

      <section className="rv-panel rv-list" aria-labelledby="rv-docs-title">
        <div className="rv-panel-head">
          <h3 id="rv-docs-title">Documents</h3>
          <span className="rv-k">
            {state.status === 'finished'
              ? `All ${documents.length} finished`
              : `${finishedDocs} of ${documents.length} finished`}
          </span>
        </div>
        <div className="rv-cols" aria-hidden="true">
          <span>Document</span>
          <span className="rv-cols-steps">
            {STATIONS.map((s) => (
              <span key={s}>{STATION_SHORT[s]}</span>
            ))}
          </span>
          <span className="rv-cols-out">Mappings</span>
        </div>
        <ol className="rv-rows">
          {documents.map((d) => (
            <DocumentLine
              key={d.document_id}
              d={d}
              now={now}
              live={live && running}
              stopped={state.status === 'failed'}
              onOpen={(id) => openDrill({ kind: 'document', document_id: id })}
            />
          ))}
          <ReconcileLine state={state} t={t} />
        </ol>
      </section>

      {t && <TimeWent t={t} stopped={state.status === 'failed'} noWork={stepsWithNoWork(state)} />}
      </>)}

      {logOpen && <RawLog lines={lines} live={live && running} onClose={toggleLog} />}
    </section>
  )
}
