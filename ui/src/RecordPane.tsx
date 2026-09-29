import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import type { Gloss, RecordDetail, RecordSummary, ReviewStatus } from './types'
import OpenSource from './OpenSource'
import Decision, { DecisionGlyph } from './Decision'

// The composite as a shape AND as the number it is. The dots read at a glance
// down a column of records; the number is the thing a judge asks for, and a
// value that only exists inside a hover tooltip is a value nobody can check.
function ConfidenceDots({ value }: { value: number }) {
  const filled = Math.round(value * 5)
  return (
    <span className="ev-confidence">
      <span className="ev-dots" aria-hidden="true">
        {Array.from({ length: 5 }, (_, i) => (
          <i key={i} className={i < filled ? 'on' : ''} />
        ))}
      </span>
      <span className="confidence-value" data-testid="confidence-value">
        {value.toFixed(2)}
      </span>
    </span>
  )
}

// Display-only reflow: the extractor's hard line breaks read as broken prose,
// so single newlines become spaces; blank lines stay paragraph breaks. The
// record's verbatim bytes are untouched (the byte-exact text is what the PDF
// highlight and the M7 check run on).
function QuoteBlock({ text }: { text: string }) {
  const paragraphs = text.split(/\n\s*\n/).map((p) => p.replace(/\s*\n\s*/g, ' ').trim())
  return (
    <blockquote className="ev-quote" data-testid="verbatim-quote">
      {paragraphs.map((p, i) => (
        <p key={i}>{p}</p>
      ))}
    </blockquote>
  )
}

// The Gloss sits BELOW the original quote, never in place of it: the verbatim
// bytes are the evidence, an English rendering is a reading aid. It shows its
// non-authoritative label until a named person approves the text, and the name
// is required because that name is the whole authority for dropping the label.
function GlossBlock({
  gloss,
  onReview,
}: {
  gloss: Gloss
  onReview: (english: string, reviewedBy: string) => void
}) {
  const [text, setText] = useState(gloss.english ?? '')
  const [name, setName] = useState(gloss.reviewed_by ?? '')

  useEffect(() => {
    setText(gloss.english ?? '')
    setName(gloss.reviewed_by ?? '')
  }, [gloss.mapping_id, gloss.english, gloss.reviewed_by])

  if (gloss.english === null) {
    return (
      <div className="ev-gloss">
        <div className="ev-gloss-label">English gloss</div>
        <p className="ev-muted">
          No English gloss could be drafted for this quote ({gloss.uncertainty_flag}).
        </p>
      </div>
    )
  }

  return (
    <div className="ev-gloss">
      <div className="ev-gloss-label">
        {gloss.reviewed ? `English gloss reviewed by ${gloss.reviewed_by}` : gloss.label}
      </div>
      <textarea
        className="ev-input ev-gloss-text"
        value={text}
        rows={4}
        onChange={(e) => setText(e.target.value)}
        aria-label="English gloss"
      />
      <div className="ev-gloss-row">
        <input
          className="ev-input"
          type="text"
          value={name}
          placeholder="Your name, to approve this gloss"
          onChange={(e) => setName(e.target.value)}
          aria-label="reviewer name"
        />
        <button
          className="btn"
          disabled={!text.trim() || !name.trim()}
          onClick={() => onReview(text.trim(), name.trim())}
        >
          Mark reviewed
        </button>
      </div>
    </div>
  )
}

function MetaRow({ label, value }: { label: string; value: ReactNode }) {
  if (value === null || value === undefined || value === '') return null
  return (
    <div className="ev-meta-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}

export default function RecordPane({
  summary,
  detail,
  index,
  total,
  note,
  onNote,
  onReview,
  onGlossReview,
  onPrev,
  onNext,
  readOnly = false,
  onOpenEvidence,
  notice = null,
}: {
  summary: RecordSummary
  detail: RecordDetail | null
  index: number
  total: number
  note: string
  onNote: (value: string) => void
  onReview: (s: ReviewStatus) => void
  onGlossReview: (english: string, reviewedBy: string) => void
  onPrev: () => void
  onNext: () => void
  // Shown to read, not to decide: no decision buttons, no note, no gloss
  // approval, and a way to where the Mapping is reviewed instead.
  readOnly?: boolean
  onOpenEvidence?: () => void
  // A save or load that failed, in plain words, shown by the decision.
  notice?: string | null
}) {
  const status = summary.review_status
  const rec = detail?.record
  const attempts = rec?.extraction_attempts ?? 0

  return (
    <section className="ev-record" aria-label="Mapping" data-testid="record-pane">
      <div className="ev-record-scroll">
        <div className="ev-record-top">
          <div className="ev-stepper">
            <button
              type="button"
              className="btn quiet ev-icon"
              onClick={onPrev}
              disabled={index === 0}
              aria-label="Previous Mapping"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
                <path d="M9 2.5 L4.5 7 L9 11.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
              </svg>
            </button>
            <span className="ev-stepper-no" data-testid="mapping-position">
              Mapping {index + 1} of {total}
            </span>
            <button
              type="button"
              className="btn quiet ev-icon"
              onClick={onNext}
              disabled={index === total - 1}
              aria-label="Next Mapping"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
                <path d="M5 2.5 L9.5 7 L5 11.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
              </svg>
            </button>
          </div>
          <Decision status={status} testId="record-decision" />
        </div>

        <div className="ev-indicator">
          <div className="ev-muted">Indicator {summary.indicator_id}</div>
          <h2 className="ev-indicator-name">{summary.indicator_name}</h2>
          <div className="ev-provision">
            <span>
              {summary.section}
              {summary.subsection ? ` ${summary.subsection}` : ''}
              {summary.format !== 'html' && summary.page_number
                ? `, PDF page ${summary.page_number}`
                : ''}
            </span>
            <OpenSource link={summary.source_link} />
          </div>
        </div>

        <QuoteBlock text={rec ? rec.verbatim_quote : summary.quote_preview} />

        {detail?.gloss ? (
          readOnly ? (
            detail.gloss.english !== null ? (
              <div className="ev-gloss">
                <div className="ev-gloss-label">
                  {detail.gloss.reviewed ? `English gloss reviewed by ${detail.gloss.reviewed_by}` : detail.gloss.label}
                </div>
                <p>{detail.gloss.english}</p>
              </div>
            ) : null
          ) : (
            <GlossBlock gloss={detail.gloss} onReview={onGlossReview} />
          )
        ) : null}

        <dl className="ev-meta">
          <MetaRow label="Rationale" value={rec?.impact} />
          <MetaRow
            label="Confidence"
            value={
              summary.confidence !== null ? <ConfidenceDots value={summary.confidence} /> : 'Not scored'
            }
          />
          <MetaRow
            label="Proof"
            value={
              rec
                ? `Word-for-word check against the source passed${
                    attempts <= 1 ? ' on the first attempt' : ` after ${attempts} attempts`
                  }`
                : null
            }
          />
        </dl>

        <details className="ev-more">
          <summary>More about this Mapping</summary>
          <dl className="ev-meta">
            <MetaRow label="Discovery" value={rec?.discovery_tag} />
            <MetaRow label="Controlling" value={summary.controlling_evidence ? 'Yes' : 'No'} />
            <MetaRow
              label="Relationship"
              value={rec?.relationship_to_group?.replaceAll('_', ' ')}
            />
            <MetaRow label="Measure type" value={rec?.measure_type?.replaceAll('_', ' ')} />
            <MetaRow label="Mapping ID" value={<code>{summary.mapping_id}</code>} />
          </dl>
        </details>
      </div>

      {readOnly ? (
        <div className="ev-decide ev-readonly" data-testid="record-read-only">
          {notice && <p className="ev-notice" role="alert">{notice}</p>}
          <p className="ev-muted">Reviewing happens in Evidence, where accepted Mappings go into the Evidence Export.</p>
          {onOpenEvidence && (
            <button type="button" className="btn" onClick={onOpenEvidence} data-testid="open-in-evidence">
              Open in Evidence to review
            </button>
          )}
        </div>
      ) : (
      <div className="ev-decide">
        {notice && (
          <p className="ev-notice" role="alert" data-testid="decide-notice">
            {notice}
          </p>
        )}
        <div className="ev-note-field">
          <label htmlFor="review-note">Note</label>
          <input
            id="review-note"
            className="ev-input"
            type="text"
            value={note}
            placeholder="Optional: why you accept, reject or flag this Mapping"
            onChange={(e) => onNote(e.target.value)}
          />
        </div>
        <div className="ev-actions">
          <button
            type="button"
            className={`btn ev-act accept${status === 'accepted' ? ' active' : ''}${
              status && status !== 'accepted' ? ' settled' : ''
            }`}
            aria-pressed={status === 'accepted'}
            data-testid="decide-accept"
            onClick={() => onReview('accepted')}
          >
            {status === 'accepted' && <DecisionGlyph status="accepted" />}
            Accept <kbd>A</kbd>
          </button>
          <button
            type="button"
            className={`btn ev-act reject${status === 'rejected' ? ' active' : ''}`}
            aria-pressed={status === 'rejected'}
            data-testid="decide-reject"
            onClick={() => onReview('rejected')}
          >
            {status === 'rejected' && <DecisionGlyph status="rejected" />}
            Reject <kbd>R</kbd>
          </button>
          <button
            type="button"
            className={`btn ev-act flag${status === 'flagged' ? ' active' : ''}`}
            aria-pressed={status === 'flagged'}
            data-testid="decide-flag"
            onClick={() => onReview('flagged')}
          >
            {status === 'flagged' && <DecisionGlyph status="flagged" />}
            Flag <kbd>F</kbd>
          </button>
        </div>
      </div>
      )}
    </section>
  )
}
