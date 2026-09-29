import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchExportPreview, postAcceptAll } from './api'
import type { ExportPreview as Preview } from './types'
import { DecisionGlyph } from './Decision'
import ErrorNote from './ErrorNote'
import { plainError, type PlainError } from './errors'

// Set when Accept all has just run, so the keyboard lands on the new count.
// Module level on purpose: the screen remounts this panel when the decisions
// change, which is exactly what Accept all does.
let landOnSummary = false

/** What the Evidence Export would contain right now: only accepted Mappings
 *  ship, and the excluded ones are named before the file exists rather than
 *  discovered afterwards. The bulk accept is there for an operator against the
 *  clock, and it never overrides a decision already made. It says how many
 *  Mappings it will accept and waits for a second press before it does. */
export default function ExportPreview({
  runId,
  onChanged,
}: {
  runId: string | null
  onChanged: () => void
}) {
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState<PlainError | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const summaryRef = useRef<HTMLParagraphElement>(null)
  const openRef = useRef<HTMLButtonElement>(null)

  const reload = useCallback(() => {
    fetchExportPreview(runId)
      .then((p) => {
        setPreview(p)
        setError(null)
      })
      .catch((e) => setError(plainError(e)))
  }, [runId])

  useEffect(reload, [reload])

  // The question takes the keyboard, on its safe answer.
  useEffect(() => {
    if (confirming) cancelRef.current?.focus()
  }, [confirming])

  const cancel = useCallback(() => {
    setConfirming(false)
    // after the render that brings the button back
    requestAnimationFrame(() => openRef.current?.focus())
  }, [])

  // While the question is open, Esc answers it wherever the keyboard is.
  useEffect(() => {
    if (!confirming) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      e.preventDefault()
      e.stopPropagation()
      cancel()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [confirming, cancel])

  // After Accept all, the keyboard lands on the line that now says how many
  // Mappings will ship.
  useEffect(() => {
    if (landOnSummary && preview) {
      landOnSummary = false
      summaryRef.current?.focus()
    }
  }, [preview])

  const acceptAll = useCallback(() => {
    setConfirming(false)
    setBusy(true)
    postAcceptAll(runId)
      .then(() => {
        landOnSummary = true
        reload()
        onChanged()
      })
      .catch((e) => setError(plainError(e)))
      .finally(() => setBusy(false))
  }, [runId, reload, onChanged])

  if (error) return <ErrorNote className="ev-preview ev-error" error={error} onRetry={reload} />
  if (!preview) return null
  if (!preview.gated) {
    return (
      <div className="ev-preview">
        <p className="ev-summary">
          Frozen bundle: {preview.n_verified} proven Mappings, no Review Decisions on this lane.{' '}
          <span className="ev-muted">Start a Run to review and gate an export.</span>
        </p>
      </div>
    )
  }

  const n = preview.n_unreviewed
  const plural = (k: number) => (k === 1 ? 'Mapping' : 'Mappings')

  return (
    <div className="ev-preview" data-testid="export-preview">
      <p className="ev-summary" ref={summaryRef} tabIndex={-1}>
        {preview.n_accepted} of {preview.n_verified} proven {plural(preview.n_verified)} will
        go into the Evidence Export.{' '}
        <span className="ev-muted">Only accepted Mappings ship.</span>
      </p>
      <div className="ev-tally">
        <dl className="ev-stats">
          <div className="ev-stat accepted">
            <dt>
              <DecisionGlyph status="accepted" />
              accepted, will export
            </dt>
            <dd data-testid="export-accepted-count">{preview.n_accepted}</dd>
          </div>
          <div className="ev-stat rejected">
            <dt>
              <DecisionGlyph status="rejected" />
              rejected
            </dt>
            <dd>{preview.n_rejected}</dd>
          </div>
          <div className="ev-stat flagged">
            <dt>
              <DecisionGlyph status="flagged" />
              flagged
            </dt>
            <dd>{preview.n_flagged}</dd>
          </div>
          <div className="ev-stat unreviewed">
            <dt>
              <DecisionGlyph status={null} />
              not reviewed yet
            </dt>
            <dd>{n}</dd>
          </div>
        </dl>
        {n > 0 && !confirming && (
          <div className="ev-bulk">
            <span className="ev-warn" data-testid="export-preview-warning">
              {preview.n_accepted === 0
                ? 'Nothing exports until you accept.'
                : 'Mappings not reviewed do not ship.'}
            </span>
            <button
              ref={openRef}
              type="button"
              className="btn ev-accept-all"
              onClick={() => setConfirming(true)}
              disabled={busy}
            >
              {busy ? 'Accepting…' : `Accept all ${n}…`}
            </button>
          </div>
        )}
      </div>
      {n > 0 && confirming && (
        <div
          className="ev-confirm"
          role="group"
          aria-labelledby="ev-confirm-q"
          aria-describedby="ev-confirm-d"
          data-testid="accept-all-confirm"
        >
          <div className="ev-confirm-words">
            <p id="ev-confirm-q" className="ev-confirm-q">
              Accept all {n} {plural(n)} not reviewed yet?
            </p>
            <p id="ev-confirm-d" className="ev-muted">
              They will go into the Evidence Export. Mappings you already accepted, rejected or
              flagged stay as they are, and you can still reject or flag any of these later.
            </p>
          </div>
          <div className="ev-confirm-actions">
            <button type="button" className="btn" ref={cancelRef} onClick={cancel}>
              Cancel
            </button>
            <button
              type="button"
              className="btn ev-primary"
              data-testid="accept-all-confirm-yes"
              onClick={acceptAll}
            >
              Accept {n} {plural(n)}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
