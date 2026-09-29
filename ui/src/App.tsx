import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchAuditRun,
  fetchDocuments,
  fetchEngines,
  fetchStatus,
  logout,
  outputDownloadUrl,
  postExport,
} from './api'
import type { AuditRun, DocumentSummary, ExportFile } from './types'
import AuditView from './AuditView'
import Comparison from './Comparison'
import DocumentList from './DocumentList'
import ExportPreview from './ExportPreview'
import ReviewQueueList from './ReviewQueueList'
import RunPanel from './RunPanel'
import RunReplay from './RunReplay'
import RunsList from './RunsList'
import SettingsPanel from './SettingsPanel'
import { useTheme, type Theme } from './theme'
import ErrorNote from './ErrorNote'
import { exportRefusal, plainError, type ExportRefusal, type PlainError } from './errors'

type Screen = 'run' | 'runs' | 'evidence' | 'compare' | 'settings'
type Route =
  | { kind: 'screen'; screen: Screen }
  // fromQueue says which list the audit view is stepping through, and so what
  // the way out is called and which list the keys follow.
  | { kind: 'audit'; documentId: string; fromQueue?: boolean; mappingId?: string }

/** The two ways the Evidence screen lists a Run: what it found, and what is
 *  left to check. */
type EvidenceView = 'documents' | 'queue'

/** The five screens, each with the one line that says what it is for. The
 *  line shows under the screen's title, beside its name in the navigation,
 *  and in the phone menu. */
const NAV: { screen: Screen; label: string; line: string }[] = [
  {
    screen: 'run',
    label: 'Start a Run',
    line: "Choose an Economy, a Pillar and an Engine, then run it over that Economy's Corpus.",
  },
  {
    screen: 'runs',
    label: 'Run history',
    line: 'Every Run and Discovery, saved as a Run Record. Open one to review or compare it.',
  },
  {
    screen: 'evidence',
    label: 'Evidence',
    line: 'The proven Mappings of one Run. Only accepted Mappings go into the Evidence Export.',
  },
  {
    screen: 'compare',
    label: 'Comparison',
    line: 'The two Engines side by side on one Economy and Pillar, Indicator by Indicator.',
  },
  {
    screen: 'settings',
    label: 'Settings',
    line: 'The key the Engines use, and clearing downloads and cache.',
  },
]

/** Watching a recorded Run again has its own title: it starts nothing. */
const REPLAY_HEAD = {
  label: 'Watch a Run again',
  line: 'A recorded Run, played back from its events at speed. Nothing runs again and no Engine is called.',
}

/** The mark beside the name: a compass whose needle is the one accent. */
function CompassMark() {
  return (
    <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
      <circle cx="11" cy="11" r="9.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M11 4 L13.2 11 L11 18 L8.8 11 Z" fill="currentColor" opacity=".25" />
      <path d="M11 4 L13.2 11 L8.8 11 Z" fill="var(--route)" />
    </svg>
  )
}

function ThemeIcon({ theme }: { theme: Theme }) {
  // Shows the theme the switch will change TO.
  return theme === 'dark' ? (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <circle cx="9" cy="9" r="3.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M9 1.5v2M9 14.5v2M1.5 9h2M14.5 9h2M3.7 3.7l1.4 1.4M12.9 12.9l1.4 1.4M3.7 14.3l1.4-1.4M12.9 5.1l1.4-1.4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  ) : (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path
        d="M14.5 11.2A6 6 0 0 1 6.8 3.5a6 6 0 1 0 7.7 7.7Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export default function App() {
  const [docs, setDocs] = useState<DocumentSummary[] | null>(null)
  const [route, setRoute] = useState<Route>({ kind: 'screen', screen: 'run' })
  // Which Run the Evidence screen is showing. null means the newest completed
  // Run, which is what the server falls back to.
  const [runId, setRunId] = useState<string | null>(null)
  const [error, setError] = useState<PlainError | null>(null)
  const [exportNote, setExportNote] = useState<string | null>(null)
  // Why the last Export wrote nothing, shown beside the Export button and
  // gone as soon as the reviewer moves on.
  const [refusal, setRefusal] = useState<ExportRefusal | null>(null)
  // The files the last Export wrote, as download links. They are what a
  // reviewer takes away: the server's own output directory is not a place
  // anyone can reach on the supported container path.
  const [exportFiles, setExportFiles] = useState<ExportFile[]>([])
  // What the Comparison screen opens on when the Runs list sent the reviewer
  // there ("Compare with..."): that Run's own Economy and Pillar.
  const [comparePreset, setComparePreset] = useState<{
    economy: string
    pillar: number | null
  } | null>(null)
  // Bumped whenever a Review Decision changes, so the export preview recounts.
  const [reviewTick, setReviewTick] = useState(0)
  // Bumped when Settings clears the downloads and caches, so the Runs list
  // re-reads instead of showing Run Records that are no longer there.
  const [clearTick, setClearTick] = useState(0)
  // The recorded Run being watched again on the Run screen, or null for the
  // Run panel itself. Any move through the navigation ends the watching.
  const [watching, setWatching] = useState<string | null>(null)
  // WHICH Run the evidence on screen belongs to, resolved by the server the
  // same way its audit reads resolve it. Without this a plain page load showed
  // rows from the newest completed Run and named none of it.
  const [auditRun, setAuditRun] = useState<AuditRun | null>(null)
  const [engineNames, setEngineNames] = useState<Record<string, string>>({})
  // The last Run's progress lines, held HERE rather than inside the Run panel:
  // the panel unmounts the moment the app moves the reviewer to the evidence,
  // and a log that disappears on success reads as a Run that never happened.
  const [runLines, setRunLines] = useState<string[]>([])
  // Which list the Evidence screen is showing, and the queue's own filter.
  // Both live here so that coming back from the audit view lands the reviewer
  // on the list they left, in the state they left it.
  const [evidenceView, setEvidenceView] = useState<EvidenceView>('documents')
  const [queueUnreviewedOnly, setQueueUnreviewedOnly] = useState(false)
  // The Document the audit view is on. In queue order it changes as the
  // reviewer steps, so the view reports it rather than the header guessing.
  const [auditTitle, setAuditTitle] = useState<string | null>(null)
  // Whether this server has a login on. Only then is there a session to end,
  // so only then does the header offer "Log out".
  const [loginOn, setLoginOn] = useState(false)
  // The phone menu (below the width where the navigation fits in the bar).
  const [menuOpen, setMenuOpen] = useState(false)
  const { theme, toggle: toggleTheme } = useTheme()
  const menuButtonRef = useRef<HTMLButtonElement>(null)

  const runExport = useCallback(() => {
    setExportNote('exporting…')
    setExportFiles([])
    setRefusal(null)
    postExport(runId)
      .then((s) => {
        setExportFiles(s.files ?? [])
        // Plain words, in the order a reader cares about: how much shipped,
        // then what was held back and why. The numbers are the same ones the
        // export preview counted, so the two lines agree.
        const excluded = [
          s.n_rejected ? `${s.n_rejected} rejected` : '',
          s.n_flagged ? `${s.n_flagged} flagged` : '',
          s.n_unreviewed ? `${s.n_unreviewed} unreviewed` : '',
          s.rows_cut ? `${s.rows_cut} over the 101-row cap` : '',
        ].filter(Boolean)
        setExportNote(
          `${s.n_rows} rows exported, ${s.n_accepted} accepted` +
            (excluded.length ? `. Left out: ${excluded.join(', ')}` : '') +
            '. Ready to download:',
        )
      })
      .catch((e) => {
        setExportNote(null)
        // Documents by title, as the lists show them, not by the ids the
        // server's checks use.
        const titles = Object.fromEntries((docs ?? []).map((d) => [d.document_id, d.title]))
        setRefusal(exportRefusal(e, titles))
      })
  }, [runId, docs])

  const reloadDocs = useCallback(() => {
    setError(null)
    setReviewTick((n) => n + 1)
    fetchDocuments(runId)
      .then(setDocs)
      .catch((e) => setError(plainError(e)))
  }, [runId])

  const goBack = useCallback(() => {
    reloadDocs()
    setRoute({ kind: 'screen', screen: 'evidence' })
  }, [reloadDocs])

  /** After a clear the Run on screen may no longer exist, so the Evidence
   *  screen goes back to "the newest completed Run" and every list re-reads. */
  const afterClear = useCallback(() => {
    setRunId(null)
    setDocs(null)
    setClearTick((n) => n + 1)
    reloadDocs()
  }, [reloadDocs])

  const openRun = useCallback((nextRunId: string | null) => {
    setRefusal(null)
    setRunId(nextRunId)
    setDocs(null)
    setRoute({ kind: 'screen', screen: 'evidence' })
  }, [])

  useEffect(reloadDocs, [reloadDocs])

  useEffect(() => {
    fetchStatus()
      .then((s) => setLoginOn(s.login === true))
      .catch(() => setLoginOn(false))
  }, [])

  useEffect(() => {
    fetchAuditRun(runId)
      .then(setAuditRun)
      .catch(() => setAuditRun(null))
  }, [runId, reviewTick])

  useEffect(() => {
    fetchEngines()
      .then((r) =>
        setEngineNames(
          Object.fromEntries(r.engines.map((e) => [e.name, e.display_name])),
        ),
      )
      .catch(() => setEngineNames({}))
  }, [])

  const current =
    route.kind === 'audit' && !route.fromQueue && docs
      ? docs.find((d) => d.document_id === route.documentId) ?? null
      : null
  const screen = route.kind === 'screen' ? route.screen : 'evidence'
  // Which item the navigation marks. Watching a recorded Run again (and
  // drilling into it) is part of Run history, where it was opened from, not
  // of Start a Run, even though it shares that screen's space.
  const navOn: Screen = screen === 'run' && watching !== null ? 'runs' : screen
  // What the audit view is on: the Document row it was opened from, or, in
  // queue order, whichever Document the current row belongs to.
  const auditDocument = route.kind === 'audit' ? current?.title ?? auditTitle : null

  // What the Evidence screen and the audit view are showing, named in full:
  // the Run, its Economy, the Pillars it covered and the Engine that ran it.
  const record = auditRun?.record ?? null
  const runLabel = record
    ? [
        `Run ${record.run_id}`,
        record.economy,
        record.pillars.length
          ? `Pillar ${record.pillars.join(', ')}`
          : null,
        record.engine ? engineNames[record.engine] ?? record.engine : null,
      ]
        .filter(Boolean)
        .join(', ')
    : auditRun?.bundle_mode
      ? 'Frozen bundle, no Run'
      : auditRun
        ? 'No finished Run yet'
        : null

  // What the Run could not do, in its own words. A Run whose chunker found no
  // section structure completes with no Mappings and no error, so without this
  // the Evidence screen is simply empty and reads as an Engine that found
  // nothing. The Run Record carries the sentence; the screen just shows it.
  const runWarnings = Array.isArray(record?.details?.warnings)
    ? (record?.details?.warnings as unknown[]).map(String)
    : []

  /** Close the phone menu and hand the keyboard back to its button. */
  const closeMenu = useCallback(() => {
    setMenuOpen(false)
    menuButtonRef.current?.focus()
  }, [])

  // While the phone menu is open: Esc closes it, and Tab cycles between the
  // Menu button and the menu's own controls instead of wandering into the
  // screen underneath. Listened for in the capture phase and marked handled,
  // so a screen's own Esc (the audit view's "back") does not also fire.
  useEffect(() => {
    if (!menuOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        closeMenu()
      } else if (e.key === 'Tab') {
        const inMenu = Array.from(
          document.querySelectorAll<HTMLElement>('#phone-menu button'),
        )
        const ring = [menuButtonRef.current, ...inMenu].filter(
          (el): el is HTMLElement => el !== null,
        )
        if (ring.length === 0) return
        const at = ring.indexOf(document.activeElement as HTMLElement)
        const next = e.shiftKey
          ? ring[(at <= 0 ? ring.length : at) - 1]
          : ring[(at + 1) % ring.length]
        e.preventDefault()
        next.focus()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [menuOpen, closeMenu])

  const mainRef = useRef<HTMLElement>(null)
  // Where the phone menu puts the keyboard when it opens: the screen you are on.
  const menuCurrentRef = useRef<HTMLButtonElement>(null)
  // A nav tip the reader dismissed with Esc stays hidden until they move on.
  const [tipHidden, setTipHidden] = useState<Screen | null>(null)

  useEffect(() => {
    if (menuOpen) menuCurrentRef.current?.focus()
  }, [menuOpen])

  /** Change screen and hand the keyboard to the new screen. A nav button that
   *  kept focus would turn the next Enter into a second click on itself, which
   *  would undo the row the list just opened; moving focus to the screen also
   *  tells a screen reader that the screen changed. */
  const go = (next: Screen) => {
    setMenuOpen(false)
    setWatching(null)
    setRefusal(null)
    setRoute({ kind: 'screen', screen: next })
    mainRef.current?.focus({ preventScroll: true })
  }
  const watchAgain = (id: string) => {
    go('run')
    setWatching(id)
  }
  const here = NAV.find((n) => n.screen === screen) ?? NAV[0]
  const themeLabel = theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'

  const exportButton = (
    <button
      className="btn export-btn"
      onClick={(e) => {
        e.currentTarget.blur()
        runExport()
      }}
    >
      Export workbook
    </button>
  )

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to the screen
      </a>
      <header className="topbar">
        <div className="brand">
          <CompassMark />
          <span>RegCompass</span>
        </div>
        <nav className="nav" aria-label="Main">
          {NAV.map((item) => (
            <div
              className="nav-item"
              key={item.screen}
              data-tip-hidden={tipHidden === item.screen ? '' : undefined}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  // handled here: the audit view must not read it as "back"
                  e.preventDefault()
                  setTipHidden(item.screen)
                }
              }}
              onMouseLeave={() => setTipHidden(null)}
              onBlur={() => setTipHidden(null)}
            >
              <button
                className={navOn === item.screen ? 'nav-btn on' : 'nav-btn'}
                aria-current={navOn === item.screen ? 'page' : undefined}
                aria-describedby={`nav-line-${item.screen}`}
                onClick={() => go(item.screen)}
              >
                {item.label}
              </button>
              <span className="nav-tip" id={`nav-line-${item.screen}`} role="tooltip">
                {item.line}
              </span>
            </div>
          ))}
        </nav>
        <div className="spacer" />
        <button
          className="icon-btn theme-btn"
          aria-label={themeLabel}
          title={themeLabel}
          onClick={toggleTheme}
        >
          <ThemeIcon theme={theme} />
        </button>
        {loginOn && (
          <button className="btn quiet logout-btn" onClick={() => void logout()}>
            Log out
          </button>
        )}
        <button
          ref={menuButtonRef}
          className="icon-btn menu-btn"
          aria-label="Menu"
          aria-expanded={menuOpen}
          aria-controls="phone-menu"
          onClick={() => (menuOpen ? closeMenu() : setMenuOpen(true))}
        >
          <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
            {menuOpen ? (
              <path d="M5 5 L15 15 M15 5 L5 15" stroke="currentColor" strokeWidth="1.6" />
            ) : (
              <path d="M3 6 H17 M3 10 H17 M3 14 H17" stroke="currentColor" strokeWidth="1.6" />
            )}
          </svg>
        </button>
      </header>

      {menuOpen && (
        <div className="menu-scrim" aria-hidden="true" onClick={closeMenu} />
      )}
      {menuOpen && (
        <nav className="phone-menu" id="phone-menu" aria-label="Main">
          <ul>
            {NAV.map((item) => (
              <li key={item.screen}>
                <button
                  ref={navOn === item.screen ? menuCurrentRef : undefined}
                  className={navOn === item.screen ? 'menu-item on' : 'menu-item'}
                  aria-current={navOn === item.screen ? 'page' : undefined}
                  onClick={() => go(item.screen)}
                >
                  <b>{item.label}</b>
                  <span>{item.line}</span>
                </button>
              </li>
            ))}
          </ul>
          <div className="menu-foot">
            <button className="btn" onClick={toggleTheme}>
              <ThemeIcon theme={theme} />
              {theme === 'dark' ? 'Light theme' : 'Dark theme'}
            </button>
            {loginOn && (
              <button className="btn quiet" onClick={() => void logout()}>
                Log out
              </button>
            )}
          </div>
        </nav>
      )}

      <main className="main" id="main" tabIndex={-1} ref={mainRef}>
        {route.kind === 'audit' ? (
          <div className="screen-bar">
            <button className="btn back-btn" onClick={goBack}>
              {route.fromQueue ? '← Review queue' : '← Documents'}
            </button>
            <div className="context" data-testid="context">
              {auditDocument && <span className="law">{auditDocument}</span>}
              {auditDocument && runLabel ? ', ' : ''}
              {runLabel && <span className="k">{runLabel}</span>}
            </div>
            {exportButton}
          </div>
        ) : (
          <header className="screen-head">
            <div className="titles">
              <h1>{screen === 'run' && watching !== null ? REPLAY_HEAD.label : here.label}</h1>
              <p className="lead">{screen === 'run' && watching !== null ? REPLAY_HEAD.line : here.line}</p>
              {screen === 'evidence' && runLabel && (
                <p className="context" data-testid="context">
                  <span className="k">Showing </span>
                  {runLabel}
                </p>
              )}
            </div>
            {screen === 'evidence' && exportButton}
          </header>
        )}
        {refusal && (
          <ErrorNote className="export-refusal" testId="export-refusal" error={refusal}>
            {refusal.reasons.length > 0 && (
              <ul>
                {refusal.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
          </ErrorNote>
        )}
        <div className="screen-body">
        {route.kind === 'audit' ? (
          <AuditView
            documentId={route.documentId}
            runId={runId}
            queue={route.fromQueue ?? false}
            unreviewedOnly={queueUnreviewedOnly}
            startMappingId={route.mappingId ?? null}
            onBack={goBack}
            onReviewSaved={reloadDocs}
            onDocumentTitle={setAuditTitle}
          />
        ) : screen === 'run' && watching !== null ? (
          <RunReplay
            runId={watching}
            onClose={() => go('runs')}
            onOpenEvidence={(id) => openRun(id)}
          />
        ) : screen === 'run' ? (
          <RunPanel
            lines={runLines}
            onLines={setRunLines}
            // A clear empties the Corpus, so the panel must re-count it and
            // show the empty state rather than the count it read before.
            reloadKey={clearTick}
            // The finished Run stays on screen with its numbers; Evidence
            // opens on it from there, already scoped to this Run.
            onRunFinished={(finishedRunId) => {
              if (finishedRunId !== null) {
                setRunId(finishedRunId)
                setDocs(null)
              } else reloadDocs()
            }}
            onOpenEvidence={(id) => openRun(id)}
          />
        ) : screen === 'runs' ? (
          <RunsList
            key={clearTick}
            onOpen={(id) => openRun(id)}
            onWatch={watchAgain}
            onCompare={(economy, pillar) => {
              setComparePreset({ economy, pillar })
              setRoute({ kind: 'screen', screen: 'compare' })
            }}
          />
        ) : screen === 'compare' ? (
          <Comparison
            key={`${comparePreset?.economy ?? ''}:${comparePreset?.pillar ?? ''}`}
            initialEconomy={comparePreset?.economy ?? null}
            initialPillar={comparePreset?.pillar ?? null}
          />
        ) : screen === 'settings' ? (
          <SettingsPanel onCleared={afterClear} />
        ) : error ? (
          <ErrorNote error={error} onRetry={reloadDocs} />
        ) : (
          <div className="evidence">
            {runWarnings.length > 0 && (
              <div className="run-warning" data-testid="run-warnings" role="alert">
                <strong>This Run could not map every Document.</strong>
                <ul>
                  {runWarnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </div>
            )}
            <ExportPreview
              key={`${runId ?? 'latest'}:${reviewTick}`}
              runId={runId}
              onChanged={reloadDocs}
            />
            <div className="chips view-switch">
              <button
                className={evidenceView === 'documents' ? 'chip on' : 'chip'}
                data-testid="view-documents"
                onClick={(e) => {
                  // Same reason the nav buttons blur: a button holding focus
                  // turns the list's Enter into a second click on itself.
                  e.currentTarget.blur()
                  setEvidenceView('documents')
                }}
              >
                Documents
              </button>
              <button
                className={evidenceView === 'queue' ? 'chip on' : 'chip'}
                data-testid="view-queue"
                onClick={(e) => {
                  e.currentTarget.blur()
                  setEvidenceView('queue')
                }}
              >
                Review queue
              </button>
            </div>
            {evidenceView === 'documents' ? (
              <DocumentList
                docs={docs}
                onOpen={(documentId) => {
                  // The Documents lane names its own Document, so drop
                  // whatever the queue last reported rather than showing one
                  // law's name over another law's row for a frame.
                  setAuditTitle(null)
                  setRoute({ kind: 'audit', documentId })
                }}
              />
            ) : (
              <ReviewQueueList
                runId={runId}
                reviewTick={reviewTick}
                unreviewedOnly={queueUnreviewedOnly}
                onUnreviewedOnly={setQueueUnreviewedOnly}
                onOpen={(row) =>
                  setRoute({
                    kind: 'audit',
                    documentId: row.document_id,
                    fromQueue: true,
                    mappingId: row.mapping_id,
                  })
                }
              />
            )}
          </div>
        )}
        </div>
      </main>

      <footer className="bottombar">
        {/* Keys are offered only where they do something. The select-and-open
            pair belongs to the two screens that have rows; on the Run panel,
            Comparison and Settings it was advertising keys that do nothing. */}
        {route.kind === 'audit' ? (
          <span className="key-hints">
            <kbd>A</kbd> accept · <kbd>R</kbd> reject · <kbd>F</kbd> flag · <kbd>J</kbd>
            <kbd>K</kbd> Mapping · <kbd>←</kbd>
            <kbd>→</kbd> page · <kbd>Esc</kbd> {route.fromQueue ? 'queue' : 'documents'}
          </span>
        ) : screen === 'evidence' || screen === 'runs' ? (
          <span className="key-hints" data-testid="select-hint">
            <kbd>↑</kbd>
            <kbd>↓</kbd> select · <kbd>Enter</kbd> open
          </span>
        ) : null}
        <div className="spacer" />
        <span className="export-note" data-testid="export-note">
          {exportNote ??
            (screen === 'evidence' ? 'only accepted Mappings enter the Evidence Export' : '')}
        </span>
        {exportFiles.length > 0 && (
          <span className="export-downloads" data-testid="export-downloads">
            {exportFiles.map((f) => (
              <a
                key={f.name}
                className={f.primary ? 'download-primary' : 'download-link'}
                data-testid={f.primary ? 'download-workbook' : undefined}
                href={outputDownloadUrl(f.name)}
                download={f.name}
                title={f.label}
              >
                {f.primary ? 'Download workbook' : f.name}
              </a>
            ))}
          </span>
        )}
      </footer>
    </div>
  )
}
