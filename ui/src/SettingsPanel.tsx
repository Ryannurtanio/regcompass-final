import { useCallback, useEffect, useState } from 'react'
import {
  deleteSettingsKey,
  fetchClearPreview,
  fetchEngines,
  fetchStatus,
  postClear,
  postSettingsKey,
} from './api'
import type { ClearReport, EngineInfo, ServerStatus } from './types'
import ErrorNote from './ErrorNote'
import PoliteCrawling from './PoliteCrawling'
import { plainError, type PlainError } from './errors'

/** The whole tool rather than one Economy. A value the select can carry, never
 *  an Economy code: the request sends null for this. */
const EVERYTHING = '*'

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

/** A size a person reads at a glance, not a byte count they have to parse. */
function readableBytes(n: number): string {
  if (n < 1024) return `${n} bytes`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

/** What a clear takes, in plain words. The same lines carry the confirmation
 *  and the receipt, so a reviewer can compare what they agreed to with what
 *  actually went. */
function clearLines(report: ClearReport): string[] {
  const lines = [
    `${plural(report.documents, 'Document')} in the Corpus`,
    `${plural(report.stored_files, 'downloaded file')} (${readableBytes(report.bytes)})`,
    `${plural(report.extractions, 'stored text stream')}`,
    `${plural(report.runs, 'Run Record')}, ${plural(report.mappings, 'Mapping')},` +
      ` ${plural(report.reviews, 'Review Decision')}`,
  ]
  if (report.refused_files > 0) {
    lines.push(
      `${plural(report.refused_files, 'stored file')} outside the data root:` +
        ' left alone',
    )
  }
  return lines
}

export default function SettingsPanel({ onCleared }: { onCleared?: () => void }) {
  const [engines, setEngines] = useState<EngineInfo[] | null>(null)
  const [target, setTarget] = useState('')
  const [value, setValue] = useState('')
  const [note, setNote] = useState<string | null>(null)
  const [error, setError] = useState<PlainError | null>(null)

  const reload = useCallback(() => {
    fetchEngines()
      .then((r) => {
        const keyed = r.engines.filter((e) => e.api_key_env)
        setEngines(keyed)
        setTarget((t) => t || keyed[0]?.name || '')
      })
      .catch((e) => setError(plainError(e)))
  }, [])

  useEffect(reload, [reload])

  const save = useCallback(() => {
    setError(null)
    postSettingsKey(target, value)
      .then(() => {
        setValue('')
        setNote('key set for this server session')
        reload()
      })
      .catch((e) => setError(plainError(e)))
  }, [target, value, reload])

  const forget = useCallback(
    (name: string) => {
      setError(null)
      deleteSettingsKey(name)
        .then(() => {
          setNote('key forgotten')
          reload()
        })
        .catch((e) => setError(plainError(e)))
    },
    [reload],
  )

  // -- Clear downloads and cache -------------------------------------------
  // The step before a sealed test, when a steward asks to see the tool start
  // empty. Nothing goes until a preview has been taken and the counts it
  // returned have been agreed to on this page.
  const [status, setStatus] = useState<ServerStatus | null>(null)
  const [scope, setScope] = useState<string>(EVERYTHING)
  const [preview, setPreview] = useState<ClearReport | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [removed, setRemoved] = useState<ClearReport | null>(null)
  const [clearBusy, setClearBusy] = useState(false)
  const [clearError, setClearError] = useState<PlainError | null>(null)

  useEffect(() => {
    fetchStatus()
      .then(setStatus)
      .catch((e) => setClearError(plainError(e)))
  }, [])

  const scopeCode = scope === EVERYTHING ? null : scope
  const scopeLabel =
    scope === EVERYTHING
      ? 'every Economy'
      : `${status?.economy_names?.[scope] ?? scope} (${scope})`

  /** A new scope invalidates the counts on screen: a reviewer must never
   *  confirm one Economy's numbers and clear another's. */
  const pickScope = useCallback((next: string) => {
    setScope(next)
    setPreview(null)
    setConfirming(false)
    setRemoved(null)
    setClearError(null)
  }, [])

  const runPreview = useCallback(() => {
    setClearBusy(true)
    setClearError(null)
    setRemoved(null)
    setConfirming(false)
    fetchClearPreview(scopeCode)
      .then(setPreview)
      .catch((e) => setClearError(plainError(e)))
      .finally(() => setClearBusy(false))
  }, [scopeCode])

  const runClear = useCallback(() => {
    setClearBusy(true)
    setClearError(null)
    postClear(scopeCode)
      .then((report) => {
        setRemoved(report)
        setPreview(null)
        setConfirming(false)
        onCleared?.()
      })
      .catch((e) => setClearError(plainError(e)))
      .finally(() => setClearBusy(false))
  }, [scopeCode, onCleared])

  return (
    <div className="panel settings-screen">
      <section className="settings-card" aria-labelledby="key-title">
        <div className="settings-card-head">
          <h2 id="key-title">Engine key</h2>
          <p className="lead">
            A key typed here is held in this server's memory for as long as it
            runs. It is never written to disk, never logged and never sent back
            to this page. Restarting the server forgets it.
          </p>
        </div>

        {error && <ErrorNote error={error} />}

        <div className="key-form">
          <div className="field">
            <label htmlFor="key-engine">Engine</label>
            <select
              id="key-engine"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            >
              {(engines ?? []).map((e) => (
                <option key={e.name} value={e.name}>
                  {e.display_name}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="key-value">Key</label>
            <input
              id="key-value"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={value}
              placeholder="Paste the OpenRouter key"
              onChange={(e) => setValue(e.target.value)}
            />
          </div>

          <div className="field key-save">
            <button className="primary" disabled={!target || !value.trim()} onClick={save}>
              Save for this session
            </button>
          </div>
        </div>
        {note && (
          <p className="hint key-note" role="status">
            {note}
          </p>
        )}

        <table className="key-table">
          <thead>
            <tr>
              <th>Engine</th>
              <th>Variable</th>
              <th>Key</th>
              <th>
                <span className="sr-only">Action</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(engines ?? []).map((e) => (
              <tr key={e.name}>
                <td>{e.display_name}</td>
                <td className="mono">{e.api_key_env}</td>
                <td>
                  <span className={e.key_set ? 'key-state set' : 'key-state missing'}>
                    {e.key_set ? (
                      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
                        <path d="M2.5 7.5 L5.5 10.5 L11.5 3.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
                      </svg>
                    ) : (
                      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
                        <circle cx="7" cy="7" r="5.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M4.5 7 H9.5" stroke="currentColor" strokeWidth="1.5" />
                      </svg>
                    )}
                    {e.key_set ? 'Set' : 'Not set'}
                  </span>
                </td>
                <td className="key-action">
                  {e.key_set && (
                    <button className="btn quiet" onClick={() => forget(e.name)}>
                      Forget
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="hint">The Engine for a Run is chosen on Start a Run.</p>
      </section>

      <PoliteCrawling />

      <section className="settings-card clear-section" aria-labelledby="clear-title">
        <div className="settings-card-head">
          <h2 id="clear-title">Clear downloads and cache</h2>
          <p className="lead">
            Removes the downloaded Documents and their files under the data
            root, the stored text a second pass would have reused, and the Run
            Records with their Mappings and Review Decisions. Nothing outside
            the data root is touched. Afterwards the next Run fetches and
            extracts again. Preview first.
          </p>
        </div>

        {clearError && <ErrorNote error={clearError} />}

        <div className="clear-form">
          <div className="field">
            <label htmlFor="clear-scope">What to clear</label>
            <select
              id="clear-scope"
              value={scope}
              onChange={(e) => pickScope(e.target.value)}
            >
              <option value={EVERYTHING}>Everything</option>
              {(status?.economies ?? []).map((code) => (
                <option key={code} value={code}>
                  {status?.economy_names?.[code] ?? code} ({code})
                </option>
              ))}
            </select>
          </div>
          <div className="field actions-row">
            <button className="btn" onClick={runPreview} disabled={clearBusy}>
              Preview
            </button>
            <button
              className="danger"
              disabled={!preview || clearBusy || confirming}
              onClick={() => setConfirming(true)}
            >
              Clear now
            </button>
          </div>
        </div>
        {!preview && !removed && (
          <p className="hint">Preview first to see what would go.</p>
        )}

        {preview && !confirming && (
          <div className="clear-counts">
            <p>
              Clearing <strong>{scopeLabel}</strong> would remove:
            </p>
            <ul>
              {clearLines(preview).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        )}

        {preview && confirming && (
          <div className="clear-confirm" role="alertdialog" aria-label="Confirm clear">
            <p>
              <strong>Clear {scopeLabel}? This cannot be undone.</strong> It
              removes:
            </p>
            <ul>
              {clearLines(preview).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <div className="field actions-row">
              <button className="danger solid" disabled={clearBusy} onClick={runClear}>
                Yes, clear now
              </button>
              <button className="btn" disabled={clearBusy} onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </div>
          </div>
        )}

        {removed && (
          <div className="clear-counts" data-testid="clear-receipt">
            <p>
              Cleared <strong>{scopeLabel}</strong>. Removed:
            </p>
            <ul>
              {clearLines(removed).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <p className="hint">
              The Corpus and the Runs screens are now empty for this scope. The
              next Run fetches and extracts again.
            </p>
          </div>
        )}
      </section>
    </div>
  )
}
