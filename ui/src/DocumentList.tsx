import { useCallback } from 'react'
import type { DocumentSummary } from './types'
import { useRowKeys } from './rowKeys'
import { DecisionGlyph } from './Decision'

/** How far one Document's review has got, as a bar split by decision and the
 *  same numbers in words, so the bar is never the only way to read it. */
function ReviewBar({ d }: { d: DocumentSummary }) {
  const corrected = d.n_corrected ?? 0
  const reviewed = d.n_accepted + d.n_rejected + d.n_flagged + corrected
  const pct = (n: number) => (d.n_records ? `${(n / d.n_records) * 100}%` : '0%')
  return (
    <span className="ev-review">
      <span className="ev-bar" aria-hidden="true">
        <i className="accepted" style={{ width: pct(d.n_accepted) }} />
        <i className="rejected" style={{ width: pct(d.n_rejected) }} />
        <i className="flagged" style={{ width: pct(d.n_flagged) }} />
        <i className="corrected" style={{ width: pct(corrected) }} />
      </span>
      <span className="ev-review-words">
        <span className="ev-muted">
          {reviewed} of {d.n_records} reviewed
        </span>
        {d.n_accepted > 0 && (
          <span className="ev-count accepted">
            <DecisionGlyph status="accepted" />
            {d.n_accepted}
            <span className="ev-sr"> accepted</span>
          </span>
        )}
        {d.n_rejected > 0 && (
          <span className="ev-count rejected">
            <DecisionGlyph status="rejected" />
            {d.n_rejected}
            <span className="ev-sr"> rejected</span>
          </span>
        )}
        {d.n_flagged > 0 && (
          <span className="ev-count flagged">
            <DecisionGlyph status="flagged" />
            {d.n_flagged}
            <span className="ev-sr"> flagged</span>
          </span>
        )}
        {corrected > 0 && (
          <span className="ev-count corrected">
            <DecisionGlyph status="corrected" />
            {corrected}
            <span className="ev-sr"> corrected</span>
          </span>
        )}
      </span>
    </span>
  )
}

export default function DocumentList({
  docs,
  onOpen,
}: {
  docs: DocumentSummary[] | null
  onOpen: (documentId: string) => void
}) {
  const open = useCallback(
    (i: number) => {
      if (docs?.[i]) onOpen(docs[i].document_id)
    },
    [docs, onOpen],
  )
  const { focus, listRef, rowProps } = useRowKeys(docs?.length ?? 0, open)

  if (!docs) return <div className="ev-note">Loading the Documents…</div>

  const totals = docs.reduce(
    (t, d) => ({
      records: t.records + d.n_records,
      reviewed: t.reviewed + d.n_accepted + d.n_rejected + d.n_flagged + (d.n_corrected ?? 0),
    }),
    { records: 0, reviewed: 0 },
  )

  // One Run reads one Economy, so its code on every row would be noise; it is
  // named only when the list does mix Economies.
  const mixed = new Set(docs.map((d) => d.economy)).size > 1

  return (
    <section className="ev-list" aria-labelledby="ev-docs-title">
      <h2 id="ev-docs-title" className="ev-sr">
        Documents
      </h2>
      <p className="ev-summary">
        {docs.length} {docs.length === 1 ? 'Document' : 'Documents'}, {totals.records} proven{' '}
        {totals.records === 1 ? 'Mapping' : 'Mappings'}, {totals.reviewed} reviewed.{' '}
        <span className="ev-muted">Open one to check its Mappings against the page.</span>
      </p>
      <div className="ev-card">
        <div className="ev-head ev-doc-grid" aria-hidden="true">
          <span>Document</span>
          <span className="num">Pages</span>
          <span className="num">Mappings</span>
          <span>Review</span>
          <span />
        </div>
        <ol className="ev-rows" ref={listRef}>
          {docs.map((d, i) => (
            <li key={d.document_id}>
              <button
                type="button"
                className={`ev-row ev-doc-grid${i === focus ? ' is-focus' : ''}`}
                data-testid={`doc-row-${d.document_id}`}
                {...rowProps(i)}
                onClick={() => onOpen(d.document_id)}
              >
                <span className="ev-doc-title">
                  <span className="ev-law">{d.title}</span>
                  <span className="ev-tags">
                    {mixed && <span className="ev-tag">{d.economy}</span>}
                    {d.ocr_applied && (
                      <span className="ev-tag" title="A scanned Document, read with OCR">
                        read by OCR
                      </span>
                    )}
                    {d.source_kind === 'manual' && (
                      <span
                        className="ev-tag"
                        title="Added by hand, not found by Discovery. The Evidence Export discloses this on every row from this Document."
                      >
                        added by hand
                      </span>
                    )}
                    {d.source_kind === 'fixture' && (
                      <span
                        className="ev-tag warn"
                        title="Seeded by `regcompass seed` from the legislation bundled with this install, not collected from the Portal on the day of the Run. A demonstration Corpus, never a collection."
                      >
                        fixture, demo only
                      </span>
                    )}
                  </span>
                </span>
                <span className="num">
                  {d.n_pages.toLocaleString('en-US')}
                  <span className="ev-unit"> pages</span>
                </span>
                <span className="num ev-strong">
                  {d.n_records}
                  <span className="ev-unit"> {d.n_records === 1 ? 'Mapping' : 'Mappings'}</span>
                </span>
                <ReviewBar d={d} />
                <svg className="ev-chev" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
                  <path d="M6 3 L11 8 L6 13" fill="none" stroke="currentColor" strokeWidth="1.6" />
                </svg>
              </button>
            </li>
          ))}
          {docs.length === 0 && (
            <li className="ev-empty">This Run has no Documents to show.</li>
          )}
        </ol>
      </div>
    </section>
  )
}
