import { useCallback, useEffect, useState } from 'react'
import { fetchReviewQueue } from './api'
import type { QueueFilter, QueueRecord, ReviewQueue } from './types'
import { useRowKeys } from './rowKeys'
import Decision from './Decision'
import ErrorNote from './ErrorNote'
import { plainError, type PlainError } from './errors'

/** The rows of one Run in the order a person should read them: lowest
 *  Confidence first, because the calibration rule says to hand-check the low
 *  ones. The Documents table answers what the Run found; this answers what is
 *  left to look at.
 *
 *  The list is re-read whenever a decision lands (reviewTick), so a row's
 *  decision shows here without a reload. The filter never removes the row a
 *  reviewer has just decided on while they are still inside the audit view:
 *  that list belongs to the audit view and is held there. */
export default function ReviewQueueList({
  runId,
  reviewTick,
  filter,
  onFilter,
  onOpen,
}: {
  runId: string | null
  reviewTick: number
  // Not reviewed only and Corrected only are two views of one list, so
  // turning one on turns the other off.
  filter: QueueFilter
  onFilter: (filter: QueueFilter) => void
  onOpen: (row: QueueRecord) => void
}) {
  const [queue, setQueue] = useState<ReviewQueue | null>(null)
  const [error, setError] = useState<PlainError | null>(null)
  // Bumped by Try again, to read the queue once more.
  const [retry, setRetry] = useState(0)
  const rows = queue?.records ?? null

  const open = useCallback(
    (i: number) => {
      if (rows?.[i]) onOpen(rows[i])
    },
    [rows, onOpen],
  )
  const { focus, setFocus, listRef, rowProps } = useRowKeys(rows?.length ?? 0, open)

  useEffect(() => {
    setError(null)
    fetchReviewQueue(runId, filter)
      .then((q) => {
        setQueue(q)
        setFocus(0)
      })
      .catch((e) => setError(plainError(e)))
  }, [runId, filter, reviewTick, setFocus, retry])

  if (error) return <ErrorNote className="ev-error" error={error} onRetry={() => setRetry((n) => n + 1)} />
  if (!queue || !rows) return <div className="ev-note">Loading the queue…</div>

  const threshold = queue.threshold.toFixed(2)

  return (
    <section className="ev-list" aria-labelledby="ev-queue-title">
      <h2 id="ev-queue-title" className="ev-sr">
        Review queue
      </h2>
      <div className="ev-summary-row">
        <p className="ev-summary">
          <span data-testid="queue-counts">
            {queue.below_threshold} of {queue.total} below {threshold} Confidence,{' '}
            {queue.unreviewed} not reviewed yet.
          </span>{' '}
          <span className="ev-muted">Lowest Confidence first: check these by hand.</span>
        </p>
        <span className="ev-toggles">
          <label className="ev-toggle">
            <input
              type="checkbox"
              data-testid="queue-unreviewed-only"
              checked={filter === 'unreviewed'}
              onChange={(e) => {
                const on = e.currentTarget.checked
                // Hand the keyboard back to the list: a checkbox that keeps focus
                // swallows the next Enter, which is how a row is opened.
                e.currentTarget.blur()
                onFilter(on ? 'unreviewed' : 'all')
              }}
            />
            Not reviewed only
          </label>
          <label className="ev-toggle">
            <input
              type="checkbox"
              data-testid="queue-corrected-only"
              checked={filter === 'corrected'}
              onChange={(e) => {
                const on = e.currentTarget.checked
                e.currentTarget.blur()
                onFilter(on ? 'corrected' : 'all')
              }}
            />
            Corrected only{queue.corrected > 0 ? ` (${queue.corrected})` : ''}
          </label>
        </span>
      </div>
      <div className="ev-card">
        <div className="ev-head ev-queue-grid" aria-hidden="true">
          <span>Document</span>
          <span>Provision</span>
          <span>Indicator</span>
          <span className="num">Confidence</span>
          <span>Decision</span>
        </div>
        <ol className="ev-rows" ref={listRef}>
          {rows.map((r, i) => {
            const low = r.confidence !== null && r.confidence < queue.threshold
            return (
              <li key={r.mapping_id}>
                <button
                  type="button"
                  className={`ev-row ev-queue-grid${i === focus ? ' is-focus' : ''}`}
                  data-testid={`queue-row-${r.mapping_id}`}
                  {...rowProps(i)}
                  onClick={() => onOpen(r)}
                >
                  <span className="ev-law ev-queue-doc">{r.document_title}</span>
                  <span className="ev-muted">
                    {r.section}
                    {r.subsection ? ` / ${r.subsection}` : ''}
                  </span>
                  <span className="ev-ind">
                    <span className="ev-unit">Indicator </span>
                    {r.review_status === 'corrected' && r.corrected_indicator_id ? (
                      <>
                        <s>{r.indicator_id}</s>
                        <span className="ev-sr"> corrected to </span>
                        <span aria-hidden="true"> → </span>
                        <span className="ev-moved" data-testid="queue-corrected-to">
                          {r.corrected_indicator_id}
                        </span>
                      </>
                    ) : (
                      r.indicator_id
                    )}
                  </span>
                  <span className={`num ev-conf${low ? ' low' : ''}`}>
                    {low && <span className="ev-low-tag">low</span>}
                    <span data-testid="queue-confidence">
                      {r.confidence === null ? 'not scored' : r.confidence.toFixed(2)}
                    </span>
                  </span>
                  <Decision status={r.review_status} testId="queue-decision" />
                </button>
              </li>
            )
          })}
          {rows.length === 0 && (
            <li className="ev-empty">
              {filter === 'unreviewed'
                ? 'Every Mapping of this Run carries a decision.'
                : filter === 'corrected'
                  ? 'No Mapping of this Run has been corrected.'
                  : 'This Run produced no proven Mappings.'}
            </li>
          )}
        </ol>
      </div>
    </section>
  )
}
