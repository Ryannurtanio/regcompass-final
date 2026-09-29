import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import AddDocument from './AddDocument'
import RunView from './RunView'
import DiscoveryView from './DiscoveryView'
import { emptyRunView, runViewReducer } from './runViewState'
import { LiveStream } from './liveStream'
import ErrorNote from './ErrorNote'
import { plainError, type PlainError } from './errors'
import { discoveryViewReducer, emptyDiscoveryView, isDiscoveryEvent } from './discoveryViewState'
import {
  CorpusEmptyError,
  fetchCorpusSummary,
  fetchEngines,
  fetchFinishedRuns,
  fetchIndicators,
  fetchRunLog,
  fetchStatus,
  postDiscover,
  postRun,
} from './api'
import type {
  CorpusDocument,
  CorpusSummary,
  EngineInfo,
  IndicatorInfo,
  RunEvent,
  RunRecord,
  ServerStatus,
} from './types'
import {
  corpusSummaryOf,
  costOf,
  economyCardLine,
  estimateCost,
  featuredEconomies,
  lastRunOnSetup,
  mappingsOf,
  pillarName,
  shortDate,
  tookOf,
  usd,
  type Estimate,
} from './runSetup'

// The box holding the Run's controls is titled "Run panel" (on the Start a
// Run screen) and the Engine switch inside it is labelled "Engine". Only the
// two declared Engines are offered; the fake Engine stays reachable through
// the API for tests.
const OFFERED_ENGINES = ['engine-a', 'engine-b']

export default function RunPanel({
  lines,
  onLines,
  reloadKey,
  onRunFinished,
  onOpenEvidence,
}: {
  // The progress log is owned by the app, not by this panel: it has to outlive
  // the panel's own unmount when a finished Run sends the reviewer to the
  // evidence, and still be there when they come back.
  lines: string[]
  onLines: (next: string[] | ((prev: string[]) => string[])) => void
  // Changes when something outside this panel has changed the Corpus, a clear
  // from Settings being the case that matters: the count below is then stale.
  reloadKey?: number
  onRunFinished: (runId: string | null, economy: string) => void
  onOpenEvidence?: (runId: string) => void
}) {
  const [status, setStatus] = useState<ServerStatus | null>(null)
  const [engines, setEngines] = useState<EngineInfo[] | null>(null)
  // How many Documents the chosen Economy's Corpus holds, per Economy, as the
  // Add document control reports it. undefined means "not counted yet", which
  // is NOT the same as zero and must not be shown as an empty Corpus.
  const [corpusCounts, setCorpusCounts] = useState<Record<string, number>>({})
  // Bumped when a job ends, because a Discovery that just fetched has changed
  // the Corpus and the empty state above must stop claiming it is empty.
  const [corpusTick, setCorpusTick] = useState(0)
  const [economy, setEconomy] = useState('')
  const [pillars, setPillars] = useState<number[]>([])
  const [indicators, setIndicators] = useState<IndicatorInfo[]>([])
  const [chosen, setChosen] = useState<string[]>([])
  const [engine, setEngine] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<PlainError | null>(null)
  const [corpusEmpty, setCorpusEmpty] = useState<string | null>(null)
  const logRef = useRef<HTMLDivElement>(null)
  // The Run's Documents and Steps, built from its typed events.
  const [runView, dispatchRunEvent] = useReducer(runViewReducer, emptyRunView)
  // A Discovery's Portal and Documents, built from its typed events.
  const [discoveryView, dispatchDiscoveryEvent] = useReducer(discoveryViewReducer, emptyDiscoveryView)

  // -- what the Start a Run screen shows around the controls -----------------
  // Every Economy's Corpus, so each Economy card can say how many Documents a
  // Run on it would read, and the chosen one can be listed beside the button.
  const [corpora, setCorpora] = useState<Record<string, CorpusDocument[]>>({})
  // Every Economy's Corpus as it is now, from the server: the cards' Document
  // count and the languages of the Corpus's own Documents.
  const [corpusSummaries, setCorpusSummaries] = useState<Record<string, CorpusSummary>>({})
  // Run Records, newest first: the last Run on the same setup, and the basis
  // for the cost estimate shown before a Run starts.
  const [runs, setRuns] = useState<RunRecord[]>([])
  // The Indicators of every chosen Pillar, with their names.
  const [pillarIndicators, setPillarIndicators] = useState<Record<number, IndicatorInfo[]>>({})
  // Economies whose Corpus read failed, so the summary does not say "Reading" forever.
  const [corpusFailed, setCorpusFailed] = useState<string[]>([])
  const [moreEconomies, setMoreEconomies] = useState(false)
  const [morePillars, setMorePillars] = useState(false)
  // Start Run and Discover ask first. Nothing is sent until the reader says yes.
  const [confirm, setConfirm] = useState<
    null | { kind: 'run'; estimate: Estimate } | { kind: 'discover' }
  >(null)
  // Bumped by the summary's Add document link: opens that control and brings it into view.
  const [addSignal, setAddSignal] = useState(0)
  const startButtonRef = useRef<HTMLButtonElement>(null)
  const confirmYesRef = useRef<HTMLButtonElement>(null)


  // A read that failed is said as such, with a way to try again: an empty
  // list here would read as "no earlier Run" or "no Indicators", which is a
  // different and false statement.
  const [runsError, setRunsError] = useState<PlainError | null>(null)
  const [indicatorsError, setIndicatorsError] = useState<PlainError | null>(null)
  const [retryRuns, setRetryRuns] = useState(0)
  const [retryIndicators, setRetryIndicators] = useState(0)

  useEffect(() => {
    fetchFinishedRuns()
      .then((r) => {
        setRuns(r.runs)
        setRunsError(null)
      })
      .catch((e) => setRunsError(plainError(e)))
  }, [corpusTick, reloadKey, retryRuns])

  useEffect(() => {
    fetchCorpusSummary()
      .then((r) => setCorpusSummaries(r.economies))
      .catch(() => setCorpusSummaries({}))
  }, [corpusTick, reloadKey])

  useEffect(() => {
    for (const p of pillars) {
      if (pillarIndicators[p]) continue
      fetchIndicators(p)
        .then((r) => setPillarIndicators((prev) => ({ ...prev, [p]: r.indicators })))
        .catch((e) => setIndicatorsError(plainError(e)))
    }
  }, [pillars.join(','), retryIndicators]) // eslint-disable-line react-hooks/exhaustive-deps

  // While the question is open, Esc closes it wherever the keyboard is. And
  // if the setup changes anyway, the question closes: its estimate was for
  // the setup it was asked about.
  useEffect(() => {
    if (!confirm) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setConfirm(null)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [confirm])
  const setupKey = [economy, pillars.join(','), chosen.join(','), engine].join('|')
  useEffect(() => {
    setConfirm(null)
  }, [setupKey])

  // The question takes the keyboard when it opens; when it closes without
  // starting anything, the keyboard goes back to Start Run.
  const hadConfirm = useRef(false)
  useEffect(() => {
    if (confirm) {
      const yes = confirmYesRef.current
      const target =
        yes && !yes.disabled
          ? yes
          : document.querySelector<HTMLElement>('[data-testid=confirm-cancel]')
      target?.focus()
    }
    else if (hadConfirm.current) startButtonRef.current?.focus()
    hadConfirm.current = confirm !== null
  }, [confirm])


  useEffect(() => {
    fetchStatus()
      .then((s) => {
        setStatus(s)
        setEconomy((e) => e || s.economies[0] || '')
        setPillars((p) => (p.length ? p : s.default_pillars))
      })
      .catch((e) => setError(plainError(e)))
    fetchEngines()
      .then((r) => {
        setEngines(r.engines)
        const offered = r.engines.filter((e) => OFFERED_ENGINES.includes(e.name))
        // The REGISTRY's default wins. Preselecting whichever Engine happens
        // to carry a key means one click on Run spends on an Engine nobody
        // chose, and the two declared Engines are not interchangeable: one is
        // open-weight and one is billed per call.
        const preferred =
          offered.find((e) => e.name === r.default_engine) ??
          offered.find((e) => e.default) ??
          offered.find((e) => e.key_set) ??
          offered[0]
        setEngine((current) => current || preferred?.name || '')
      })
      .catch((e) => setError(plainError(e)))
  }, [])

  // The Indicator list follows the Pillar selection: narrowing is optional and
  // only ever offered within the Pillars the Run covers.
  useEffect(() => {
    setIndicatorsError(null)
    if (pillars.length !== 1) {
      setIndicators([])
      setChosen([])
      return
    }
    fetchIndicators(pillars[0])
      .then((r) => setIndicators(r.indicators))
      .catch((e) => {
        setIndicators([])
        setIndicatorsError(plainError(e))
      })
    setChosen([])
  }, [pillars.join(','), retryIndicators]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [lines])

  // The live stream in use, if any. A drop on flaky wifi is not the end of
  // the job: the stream reconnects and only the server's `end` event ends it.
  const stream = useRef<LiveStream | null>(null)
  const [reconnecting, setReconnecting] = useState(false)
  useEffect(() => () => stream.current?.stop(), [])

  const follow = useCallback(
    (economyCode: string | null) => {
      setRunning(true)
      setReconnecting(false)
      // Always from empty: the stream replays every line the job has written
      // so far, so rejoining one already in flight fills the log rather than
      // doubling it.
      const reset = () => {
        onLines([])
        dispatchRunEvent({ type: 'reset' })
        dispatchDiscoveryEvent({ type: 'reset' })
      }
      reset()
      stream.current?.stop()
      const s = new LiveStream({
        onLine: (msg) => onLines((prev) => [...prev, msg]),
        onRunEvent: (event) => dispatchRunEvent({ type: 'event', event }),
        onDiscoveryEvent: (event) => dispatchDiscoveryEvent({ type: 'event', event }),
        onReset: reset,
        onPhase: (phase) => {
          if (stream.current === s) setReconnecting(phase === 'reconnecting')
        },
        // An EventSource cannot see a 401, so ask once through a guarded
        // endpoint: an ended session then goes to the sign-in page instead of
        // the log simply stopping. (/api/status is open and would never tell.)
        onDrop: () => {
          fetchRunLog().catch(() => undefined)
        },
        onEnd: (data) => {
          setRunning(false)
          let runId: string | null = null
          let ranEconomy: string | null = null
          try {
            const final = JSON.parse(data) as ServerStatus & { economy?: string }
            runId = final.run_id ?? null
            // The job's own Economy, for a panel that rejoined a Run it did
            // not start and so was never told which one it is watching.
            ranEconomy = final.economy ?? null
          } catch {
            runId = null
          }
          fetchStatus().then(setStatus).catch(() => undefined)
          setCorpusTick((n) => n + 1)
          onRunFinished(runId, economyCode ?? ranEconomy ?? '')
        },
      })
      stream.current = s
      s.start()
    },
    [onRunFinished, onLines],
  )

  // Opening the panel: the server still holds the last job's lines, so a page
  // reload or a walk through the other screens gets them back rather than the
  // placeholder. They are cleared by the next Run, not by us. A job still in
  // flight is rejoined, because the stream replays from its first line.
  useEffect(() => {
    fetchRunLog()
      .then((r) => {
        if (r.active) {
          follow(null)
          return
        }
        if (lines.length === 0 && r.lines.length > 0) onLines(r.lines)
        if (r.events?.length) {
          const runEvents = r.events.filter((e): e is RunEvent => !isDiscoveryEvent(e))
          if (runEvents.length) dispatchRunEvent({ type: 'replay', events: runEvents })
          dispatchDiscoveryEvent({ type: 'replay', events: r.events })
        }
      })
      .catch(() => undefined)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const start = useCallback(() => {
    setError(null)
    setCorpusEmpty(null)
    postRun({
      economy,
      pillars,
      indicators: chosen.length ? chosen : null,
      engine,
    })
      .then(() => follow(economy))
      .catch((e) => {
        if (e instanceof CorpusEmptyError) setCorpusEmpty(e.detail.message)
        else setError(plainError(e))
      })
  }, [economy, pillars, chosen, engine, follow])

  const discover = useCallback(() => {
    setError(null)
    setCorpusEmpty(null)
    postDiscover(economy)
      .then(() => follow(economy))
      .catch((e) => setError(plainError(e, 'discovery')))
  }, [economy, follow])

  const offered = (engines ?? []).filter((e) => OFFERED_ENGINES.includes(e.name))
  const selected = offered.find((e) => e.name === engine) ?? null
  // Counted, and zero. An Economy nobody has counted yet must not be announced
  // as empty, so undefined is not treated as 0 here.
  const corpusIsEmpty = economy !== '' && corpusCounts[economy] === 0
  const togglePillar = (p: number) =>
    setPillars((prev) => (prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p].sort((a, b) => a - b)))

  const economyName = status?.economy_names?.[economy] ?? economy
  // Discovery is impossible for a Portal whose rules forbid it, and not wired
  // for one with no strategy yet. Naming a button that cannot work would be
  // worse than naming one fewer way in.
  const canDiscover =
    economy !== '' &&
    !(status?.manual_only ?? []).includes(economy) &&
    !(status?.no_discovery ?? []).includes(economy)

  // Economies that already have a finished Run come first; on a database with
  // none, those with Documents; the rest fold behind "More Economies".
  const allEconomies = status?.economies ?? []
  // Each card's Corpus as it is now: the server's summary, overridden by the
  // chosen Economy's own listing once "Add document" has read it (fresher
  // after an upload).
  const summaries = useMemo(() => {
    const out: Record<string, CorpusSummary> = { ...corpusSummaries }
    for (const [code, documents] of Object.entries(corpora)) out[code] = corpusSummaryOf(documents)
    return out
  }, [corpusSummaries, corpora])
  const knownCounts = useMemo(() => {
    const out: Record<string, number> = {}
    for (const [code, summary] of Object.entries(summaries)) out[code] = summary.n
    return { ...out, ...corpusCounts }
  }, [summaries, corpusCounts])

  // Economies with Documents and a finished Run on BOTH declared Engines come
  // first; the rest fold behind "More Economies".
  const featured = useMemo(
    () => featuredEconomies(allEconomies, runs, knownCounts, OFFERED_ENGINES),
    [allEconomies, runs, knownCounts],
  )
  const otherEconomies = allEconomies.filter((c) => !featured.includes(c))
  const featuredPillars = (status?.default_pillars ?? []).filter((p) =>
    (status?.pillars ?? []).includes(p),
  )
  const otherPillars = (status?.pillars ?? []).filter((p) => !featuredPillars.includes(p))

  const setup = {
    economy,
    pillars,
    indicators: chosen.length ? chosen : null,
    engine,
  }
  const lastRun = economy && engine && pillars.length ? lastRunOnSetup(runs, setup) : null
  const indicatorTotal = pillars.reduce((n, p) => n + (pillarIndicators[p]?.length ?? 0), 0)
  const indicatorCount = chosen.length ? chosen.length : indicatorTotal
  const isTicked = (id: string) => chosen.length === 0 || chosen.includes(id)
  const tickIndicator = (id: string) =>
    setChosen((prev) => {
      const all = indicators.map((i) => i.id)
      const now = prev.length === 0 ? all : prev
      const next = now.includes(id) ? now.filter((x) => x !== id) : [...now, id]
      // Every box ticked is the same as no narrowing at all.
      if (next.length === 0) return prev
      return all.every((x) => next.includes(x)) ? [] : all.filter((x) => next.includes(x))
    })

  // With no Run of exactly this setup, say what the estimate will rest on
  // instead, in the same words the question before the Run uses.
  const sideEstimate =
    !lastRun && economy && engine && pillars.length
      ? estimateCost(runs, setup, corpusCounts[economy], economyName)
      : null
  const noLastRunLine =
    sideEstimate === null
      ? 'No earlier Run on this setup.'
      : confirm
        ? 'No Run of exactly this setup yet.'
        : sideEstimate.usd !== null
          ? `No Run of exactly this setup yet. Estimate: about ${usd(sideEstimate.usd)}, ${sideEstimate.basis}`
          : `No earlier Run on this setup. ${sideEstimate.basis}`

  const askStart = () => {
    setError(null)
    setCorpusEmpty(null)
    setConfirm({
      kind: 'run',
      estimate: estimateCost(runs, setup, corpusCounts[economy], economyName),
    })
  }
  const cancelConfirm = () => setConfirm(null)
  const confirmYes = () => {
    const kind = confirm?.kind
    setConfirm(null)
    if (kind === 'run') start()
    else if (kind === 'discover') discover()
  }
  const pillarLabel = (p: number) => {
    const name = pillarName(p)
    return name ? `${p}, ${name}` : String(p)
  }
  const chosenCorpus = corpora[economy] ?? null

  return (
    <div className="panel run-screen">
      {error && <ErrorNote error={error} />}

      {/* Said BEFORE Run is pressed, not after it is refused: an empty first
          screen with one button on it tells a reviewer nothing about why
          nothing happens, and the three ways out are not guessable. */}
      {corpusIsEmpty && !running && (
        <div className="notice" data-testid="corpus-empty-state">
          No Documents yet for {economyName}. A Run reads the Corpus and fetches
          nothing, so there is nothing for it to read.{' '}
          {canDiscover ? (
            <>
              Press <strong>Discover</strong> below to collect from the Portal,
              open{' '}
            </>
          ) : (
            <>
              Discovery does not run for this Economy, so open{' '}
            </>
          )}
          <strong>Add document</strong> to upload a file or name an official
          URL, or seed the bundled legislation from a terminal with{' '}
          <code>regcompass seed --economy {economy}</code>, the keyless demo
          that needs no API key.
        </div>
      )}

      {status?.bundle_mode && (
        <div className="notice">
          This server reads a frozen bundle, so a Run started here will not
          appear in the audit view. Start it without the bundle option to
          review your own Runs.
        </div>
      )}

      <section className="run-setup" aria-labelledby="run-panel-title">
        <h2 className="run-panel-head" id="run-panel-title">
          Run panel
        </h2>
        <div className="setup-steps">
          <fieldset className="setup-step" disabled={running || confirm !== null}>
            <legend className="step-title">
              <span className="step-num" aria-hidden="true">1</span>
              Economy
            </legend>
            <div className="step-body">
              <p className="step-line">The Corpus the Run will read.</p>
              <div className="card-grid economies">
                {[...featured, ...(moreEconomies ? [] : otherEconomies.filter((c) => c === economy))].map(
                  (code) => (
                    <EconomyCard
                      key={code}
                      code={code}
                      name={status?.economy_names?.[code] ?? code}
                      summary={summaries[code]}
                      checked={economy === code}
                      onPick={() => setEconomy(code)}
                    />
                  ),
                )}
              </div>
              {otherEconomies.length > 0 && (
                <div className="more">
                  <button
                    type="button"
                    className="more-btn"
                    aria-expanded={moreEconomies}
                    aria-controls="more-economies"
                    onClick={() => setMoreEconomies((v) => !v)}
                  >
                    {moreEconomies
                      ? 'Fewer Economies'
                      : `More Economies (${otherEconomies.length})`}
                  </button>
                  {moreEconomies && (
                    <div className="card-grid economies small" id="more-economies">
                      {otherEconomies.map((code) => (
                        <EconomyCard
                          key={code}
                          code={code}
                          name={status?.economy_names?.[code] ?? code}
                          summary={summaries[code]}
                          checked={economy === code}
                          onPick={() => setEconomy(code)}
                        />
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          </fieldset>

          <fieldset className="setup-step" disabled={running || confirm !== null}>
            <legend className="step-title">
              <span className="step-num" aria-hidden="true">2</span>
              Pillar
            </legend>
            <div className="step-body">
              <p className="step-line">
                What the Run looks for. Choose one or more.
              </p>
              <div className="card-grid pillars">
                {[...featuredPillars, ...(morePillars ? [] : otherPillars.filter((p) => pillars.includes(p)))].map(
                  (p) => (
                    <PillarCard
                      key={p}
                      pillar={p}
                      checked={pillars.includes(p)}
                      onToggle={() => togglePillar(p)}
                    />
                  ),
                )}
              </div>
              {otherPillars.length > 0 && (
                <div className="more">
                  <button
                    type="button"
                    className="more-btn"
                    aria-expanded={morePillars}
                    aria-controls="more-pillars"
                    onClick={() => setMorePillars((v) => !v)}
                  >
                    {morePillars ? 'Fewer Pillars' : `More Pillars (${otherPillars.length})`}
                  </button>
                  {morePillars && (
                    <div className="card-grid pillars small" id="more-pillars">
                      {otherPillars.map((p) => (
                        <PillarCard
                          key={p}
                          pillar={p}
                          checked={pillars.includes(p)}
                          onToggle={() => togglePillar(p)}
                        />
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          </fieldset>

          <fieldset className="setup-step" disabled={running || confirm !== null}>
            <legend className="step-title">
              <span className="step-num" aria-hidden="true">3</span>
              Indicators
            </legend>
            <div className="step-body">
              <p className="step-line">
                {pillars.length === 1
                  ? 'All are on. Untick one to leave it out of this Run.'
                  : pillars.length === 0
                    ? 'Choose a Pillar to see its Indicators.'
                    : 'Every Indicator of the chosen Pillars. To leave one out, choose a single Pillar.'}
              </p>
              {indicatorsError && (
                <ErrorNote
                  className="step-error"
                  testId="indicators-error"
                  error={{
                    ...indicatorsError,
                    message: `The Indicators could not be read. ${indicatorsError.message}`,
                  }}
                  onRetry={() => setRetryIndicators((n) => n + 1)}
                />
              )}
              {pillars.length === 1 ? (
                <ul className="indicator-list">
                  {indicators.map((i) => (
                    <li key={i.id}>
                      <label>
                        <input
                          type="checkbox"
                          checked={isTicked(i.id)}
                          // the last ticked box stays: a Run needs one Indicator
                          disabled={isTicked(i.id) && chosen.length === 1}
                          onChange={() => tickIndicator(i.id)}
                        />
                        <b>{i.id}</b>
                        <span>{i.name}</span>
                      </label>
                    </li>
                  ))}
                </ul>
              ) : (
                <ul className="indicator-list plain">
                  {pillars.flatMap((p) =>
                    (pillarIndicators[p] ?? []).map((i) => (
                      <li key={i.id}>
                        <b>{i.id}</b>
                        <span>{i.name}</span>
                      </li>
                    )),
                  )}
                </ul>
              )}
            </div>
          </fieldset>

          <fieldset className="setup-step" disabled={running || confirm !== null}>
            <legend className="step-title" id="engine-label">
              <span className="step-num" aria-hidden="true">4</span>
              Engine
            </legend>
            <div className="step-body">
              <p className="step-line">
                The model that picks the quotes. Switch here at any time between Runs.
              </p>
              <div className="card-grid engines" role="radiogroup" aria-labelledby="engine-label">
                {offered.map((e) => (
                  <label
                    key={e.name}
                    className={engine === e.name ? 'pick-card on' : 'pick-card'}
                  >
                    <input
                      type="radio"
                      name="engine"
                      value={e.name}
                      checked={engine === e.name}
                      onChange={() => setEngine(e.name)}
                    />
                    <b className="card-name">{e.display_name}</b>
                    <span
                      className="card-meta"
                      data-testid={engine === e.name ? 'engine-price' : undefined}
                    >
                      {e.open_weights ? 'Open weights' : 'Commercial, hosted'}.{' '}
                      {e.usd_per_million_input_tokens.toFixed(2)} in,{' '}
                      {e.usd_per_million_output_tokens.toFixed(2)} out USD per million tokens.
                      {e.name === status?.default_engine ? ' The registry default.' : ''}
                    </span>
                    <span className={e.key_set ? 'key-state set' : 'key-state missing'}>
                      <KeyGlyph set={e.key_set} />
                      {e.key_set ? 'Key set' : 'No key: add one in Settings'}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          </fieldset>
        </div>

        <aside className="setup-summary" aria-labelledby="your-run-title">
          <h2 className="panel-title" id="your-run-title">
            Your Run
          </h2>
          <dl className="summary-list">
            <dt>Economy</dt>
            <dd>
              {economyName || '-'}
              {corpusCounts[economy] !== undefined
                ? `, ${corpusCounts[economy]} Document${corpusCounts[economy] === 1 ? '' : 's'}`
                : ''}
            </dd>
            <dt>Pillar</dt>
            <dd>{pillars.length ? pillars.map(pillarLabel).join('; ') : 'none chosen'}</dd>
            <dt>Indicators</dt>
            <dd>
              {indicatorTotal ? `${indicatorCount} of ${indicatorTotal}` : '-'}
            </dd>
            <dt>Engine</dt>
            <dd>
              {selected ? selected.display_name : '-'}
              {selected && !selected.key_set ? ' (no key)' : ''}
            </dd>
          </dl>
          {runsError ? (
            <ErrorNote
              className="last-run"
              testId="last-run-error"
              error={{
                ...runsError,
                message: `Earlier Runs could not be read, so there is no estimate yet. ${runsError.message}`,
              }}
              onRetry={() => setRetryRuns((n) => n + 1)}
            />
          ) : (
          <p className="last-run" data-testid="last-run">
            {lastRun
              ? `Last time on this setup (${shortDate(lastRun.started_at)}): ${[
                  tookOf(lastRun),
                  // the provider's bill when recorded, as in the estimate
                  lastRun.provider_cost_usd
                    ? `${usd(costOf(lastRun))} billed`
                    : `${usd(costOf(lastRun))} by our meter`,
                  mappingsOf(lastRun) !== null ? `${mappingsOf(lastRun)} Mappings` : null,
                ]
                  .filter(Boolean)
                  .join(', ')}.`
              : noLastRunLine}
          </p>
          )}

          {confirm ? (
            <div
              className="start-confirm"
              role="alertdialog"
              aria-labelledby="confirm-title"
              aria-describedby="confirm-text"
              data-testid="start-confirm"
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.preventDefault()
                  e.stopPropagation()
                  cancelConfirm()
                }
              }}
            >
              {confirm.kind === 'run' ? (
                <>
                  <b id="confirm-title">Start this Run?</b>
                  <p id="confirm-text">
                    {confirm.estimate.usd !== null
                      ? `About ${usd(confirm.estimate.usd)}, ${confirm.estimate.basis}`
                      : confirm.estimate.basis}
                    {selected && !selected.key_set
                      ? ' This Engine has no key yet, so the Run cannot call it. Add the key in Settings, then start the Run.'
                      : ''}
                  </p>
                </>
              ) : (
                <>
                  <b id="confirm-title">Start Discovery for {economyName}?</b>
                  <p id="confirm-text">
                    It fetches Documents from the Portal into the Corpus. It
                    costs nothing on the Engine.
                  </p>
                </>
              )}
              <div className="confirm-actions">
                <button
                  ref={confirmYesRef}
                  className="primary"
                  data-testid="confirm-start"
                  // a Run on an Engine with no key can only fail
                  disabled={confirm.kind === 'run' && selected !== null && !selected.key_set}
                  onClick={confirmYes}
                >
                  {confirm.kind === 'run' ? 'Yes, start the Run' : 'Yes, start Discovery'}
                </button>
                <button className="btn" data-testid="confirm-cancel" onClick={cancelConfirm}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="start-actions">
              <button
                ref={startButtonRef}
                className="primary start-btn"
                disabled={running || !economy || pillars.length === 0 || !engine}
                onClick={askStart}
              >
                {running ? 'Running…' : 'Start Run'}
              </button>
              {/* Offered as soon as the Corpus is known to be empty, not only after
                  a Run has been refused: the empty state above names this button. */}
              {(corpusEmpty !== null || corpusIsEmpty) && canDiscover && (
                <button
                  className="btn"
                  disabled={running}
                  onClick={() => setConfirm({ kind: 'discover' })}
                >
                  Discover
                </button>
              )}
              <span className="hint">
                A Run spends money on the Engine. You see the estimate and
                confirm before it starts.
              </span>
            </div>
          )}

          <div className="summary-corpus">
            <div className="summary-corpus-head">
              <h3>Corpus of {economyName || '-'}</h3>
              <button type="button" className="link-btn" onClick={() => setAddSignal((n) => n + 1)}>
                Add document
              </button>
            </div>
            {chosenCorpus === null && corpusFailed.includes(economy) ? (
              <span className="hint" data-testid="summary-corpus-error">
                The Corpus could not be read. Add document below has Try again.
              </span>
            ) : chosenCorpus === null ? (
              <span className="hint">Reading the Corpus…</span>
            ) : chosenCorpus.length === 0 ? (
              <span className="hint">No Documents yet.</span>
            ) : (
              <ul>
                {chosenCorpus.slice(0, 6).map((d) => (
                  <li key={d.document_id}>
                    <span className="law" title={d.title}>
                      {d.title}
                    </span>
                    <span className="k">
                      {d.n_pages !== null ? `${d.n_pages.toLocaleString('en-US')} p.` : ''}
                    </span>
                  </li>
                ))}
                {chosenCorpus.length > 6 && (
                  <li className="k">and {chosenCorpus.length - 6} more, listed under Add document</li>
                )}
              </ul>
            )}
          </div>
        </aside>
      </section>

      {corpusEmpty && <div className="notice">{corpusEmpty}</div>}

      {/* The Run as it happens and as it ended: the flow of numbers, every
          Document's route through the Steps, where the time went, and the raw
          log behind a toggle. */}
      {reconnecting && (
        <p className="hint live-reconnecting" role="status" data-testid="live-reconnecting">
          Reconnecting to the Run. It carries on on the server meanwhile; the
          screen catches up as soon as the connection is back.
        </p>
      )}
      <RunView state={runView} live={running} lines={lines} onOpenEvidence={onOpenEvidence} />

      {/* A Discovery: the Portal it reads, and each Document found, added or
          skipped with the reason. */}
      <DiscoveryView state={discoveryView} />

      <AddDocument
        status={status}
        economy={economy}
        reloadKey={corpusTick + (reloadKey ?? 0)}
        openSignal={addSignal}
        onAdded={() => setCorpusEmpty(null)}
        onCorpusCount={(code, n) =>
          setCorpusCounts((prev) => (prev[code] === n ? prev : { ...prev, [code]: n }))
        }
        onCorpusFailed={(code) => setCorpusFailed((prev) => (prev.includes(code) ? prev : [...prev, code]))}
        onCorpus={(code, documents) => {
          setCorpusFailed((prev) => (prev.includes(code) ? prev.filter((c) => c !== code) : prev))
          setCorpora((prev) => (prev[code] === documents ? prev : { ...prev, [code]: documents }))
        }}
      />

      {/* A Run shows its raw log inside the Run view, behind a toggle. A
          Discovery has no Run view, so its lines still show here. */}
      {runView.status === 'idle' && lines.length > 0 && (
        <>
          {!running && (
            <span className="hint" data-testid="run-log-label">
              The last progress log. It stays here until the next Run starts.
            </span>
          )}
          <div className="log" ref={logRef} data-testid="run-log">
            {lines.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

/** One Economy as a card: its name and code, how many Documents its Corpus
 *  holds now, and the languages those Documents are. A radio underneath, so the keyboard
 *  and a screen reader treat the cards as one choice. */
function EconomyCard({
  code,
  name,
  summary,
  checked,
  onPick,
}: {
  code: string
  name: string
  summary: CorpusSummary | undefined
  checked: boolean
  onPick: () => void
}) {
  return (
    <label className={checked ? 'pick-card on' : 'pick-card'} data-empty={summary?.n === 0 ? '' : undefined}>
      <input type="radio" name="economy" value={code} checked={checked} onChange={onPick} />
      <span className="card-top">
        <b className="card-name">{name}</b>
        <span className="card-code">{code}</span>
      </span>
      <span className="card-meta">{economyCardLine(summary)}</span>
    </label>
  )
}

function PillarCard({
  pillar,
  checked,
  onToggle,
}: {
  pillar: number
  checked: boolean
  onToggle: () => void
}) {
  return (
    <label className={checked ? 'pick-card pillar on' : 'pick-card pillar'}>
      <input type="checkbox" checked={checked} onChange={onToggle} />
      <b className="pillar-num">{pillar}</b>
      <span className="pillar-name">{pillarName(pillar) ?? `Pillar ${pillar}`}</span>
    </label>
  )
}

/** Key set (a tick) or missing (a hollow ring with a bar): told apart by shape
 *  as well as colour. Never the key itself. */
function KeyGlyph({ set }: { set: boolean }) {
  return set ? (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
      <path d="M2.5 7.5 L5.5 10.5 L11.5 3.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
    </svg>
  ) : (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
      <circle cx="7" cy="7" r="5.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M4.5 7 H9.5" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  )
}
