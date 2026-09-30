import { useEffect, useId, useState } from 'react'
import type { ReactNode } from 'react'
import type { ConfidencePart, Gloss, IndicatorChoice, RecordDetail, RecordSummary, ReviewStatus } from './types'
import { breakdownRows } from './confidenceBreakdown'
import OpenSource from './OpenSource'
import Decision, { DecisionGlyph } from './Decision'
import { checkCorrection, correctedBy, correctionGroups, overrideLine } from './correction'

// The name a reviewer last corrected under, so they type it once. A browser
// that keeps nothing (a private window) simply asks again.
const NAME_KEY = 'regcompass.reviewerName'
function rememberedName(): string {
  try {
    return window.localStorage.getItem(NAME_KEY) ?? ''
  } catch {
    return ''
  }
}
function rememberName(name: string) {
  try {
    window.localStorage.setItem(NAME_KEY, name)
  } catch {
    // nothing kept; the field is simply empty next time
  }
}

// The Correct picker: the right Indicator from the Run's own Pillars and a
// required reason. Nothing is saved until Save correction; Cancel (or Escape)
// leaves the Mapping exactly as it was.
function CorrectPicker({
  ownId,
  choices,
  onSave,
  onCancel,
}: {
  ownId: string
  choices: IndicatorChoice[]
  onSave: (indicatorId: string, reason: string, reviewer: string) => void
  onCancel: () => void
}) {
  const [indicator, setIndicator] = useState('')
  const [reason, setReason] = useState('')
  const [name, setName] = useState(rememberedName)
  const groups = correctionGroups(choices, ownId)
  const check = checkCorrection(indicator, reason)

  const save = () => {
    if (!check.canSave) return
    rememberName(name.trim())
    onSave(indicator, reason.trim(), name.trim())
  }

  return (
    <div
      className="ev-correct"
      role="group"
      aria-label="Correct the Indicator"
      data-testid="correct-picker"
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          // Leaves the picker, not the audit view.
          e.stopPropagation()
          onCancel()
        }
      }}
    >
      <div className="ev-correct-field">
        <label htmlFor="correct-indicator">Correct to</label>
        <select
          id="correct-indicator"
          className="ev-input"
          value={indicator}
          autoFocus
          data-testid="correct-indicator"
          onChange={(e) => setIndicator(e.target.value)}
        >
          <option value="">Choose an Indicator</option>
          {groups.map((g) => (
            <optgroup key={g.pillar} label={g.label}>
              {g.options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </div>
      <div className="ev-correct-field">
        <label htmlFor="correct-reason">Reason</label>
        <textarea
          id="correct-reason"
          className="ev-input"
          rows={3}
          value={reason}
          placeholder="Why this provision belongs under the Indicator you chose"
          data-testid="correct-reason"
          aria-describedby="correct-reason-count"
          onChange={(e) => setReason(e.target.value)}
        />
        <span
          id="correct-reason-count"
          className={`ev-correct-count${check.over ? ' over' : ''}`}
          aria-live="polite"
          data-testid="correct-reason-count"
        >
          {check.counter}
        </span>
      </div>
      <div className="ev-correct-field">
        <label htmlFor="correct-name">Your name</label>
        <input
          id="correct-name"
          className="ev-input"
          type="text"
          value={name}
          placeholder="Optional: shown with the correction"
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="ev-correct-buttons">
        <button
          type="button"
          className="btn ev-primary"
          disabled={!check.canSave}
          data-testid="correct-save"
          onClick={save}
        >
          Save correction
        </button>
        <button type="button" className="btn" data-testid="correct-cancel" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  )
}

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

// "How this is scored": a click-to-open panel, not a tooltip, so it works by
// keyboard and on touch screens. It lists the four signals the pipeline
// computed the Confidence from, each with its input, weight and share.
function ConfidenceBreakdown({ value, parts }: { value: number; parts: ConfidencePart[] }) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const b = breakdownRows(parts, value)
  return (
    <div className="ev-score">
      <div className="ev-score-line">
        <ConfidenceDots value={value} />
        <button
          type="button"
          className="btn quiet ev-score-toggle"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((o) => !o)}
        >
          How this is scored
        </button>
      </div>
      <div id={panelId} className="ev-score-panel" hidden={!open}>
        <ul className="ev-score-rows">
          {b.rows.map((r) => (
            <li key={r.signal}>
              <span className="ev-score-name">
                {r.label} <span className="ev-muted">{r.weight}</span>
              </span>
              <span className="ev-score-add">+{r.contribution}</span>
              <span className="ev-score-value ev-muted">{r.value}</span>
              <span className="ev-score-why">{r.why}</span>
            </li>
          ))}
          <li className="ev-score-total">
            <span className="ev-score-name">Total</span>
            <span className="ev-score-add">{b.total}</span>
            <span className="ev-score-value ev-muted">{b.sum} before rounding</span>
          </li>
        </ul>
        <p>
          The Engine writes the Rationale. The pipeline computes Confidence from these four
          signals; the Engine never reports its own confidence.
        </p>
        <p>Below 0.60 a Mapping is marked for review.</p>
        <p className="ev-muted">
          Specificity here counts all of this Run's Mappings from the same Piece; the export
          counts only the rows it keeps, so its figure can differ slightly.
        </p>
      </div>
    </div>
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
  correcting = false,
  onCorrect,
  onCorrectCancel,
  onCorrectSave,
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
  // The Correct picker is open for this Mapping.
  correcting?: boolean
  onCorrect?: () => void
  onCorrectCancel?: () => void
  onCorrectSave?: (indicatorId: string, reason: string, reviewer: string) => void
}) {
  const status = summary.review_status
  const rec = detail?.record
  const attempts = rec?.extraction_attempts ?? 0
  const choices = detail?.correction_choices ?? []
  const canCorrect =
    detail !== null && correctionGroups(choices, summary.indicator_id).length > 0
  const correctedId = status === 'corrected' ? summary.corrected_indicator_id : null
  const correctedTitle =
    summary.corrected_indicator_title ?? detail?.corrected_indicator_title ?? null
  const review = detail?.review?.mapping_id === summary.mapping_id ? detail.review : null

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
          {correctedId && (
            <div className="ev-override" data-testid="record-override">
              <p className="ev-override-line">
                <DecisionGlyph status="corrected" />
                <span>
                  {overrideLine(
                    { id: summary.indicator_id, title: summary.indicator_name },
                    { id: correctedId, title: correctedTitle },
                  )}
                </span>
              </p>
              {review && (
                <p className="ev-muted">{correctedBy(review.reviewer, review.reviewed_at)}</p>
              )}
              {summary.review_note && (
                <p className="ev-override-reason">
                  <span className="ev-muted">Reason: </span>
                  {summary.review_note}
                </p>
              )}
            </div>
          )}
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
          <MetaRow label="Rationale" value={detail?.impact_display ?? rec?.impact} />
          <MetaRow
            label="Confidence"
            value={
              summary.confidence === null ? (
                'Not scored'
              ) : summary.confidence_parts ? (
                <ConfidenceBreakdown value={summary.confidence} parts={summary.confidence_parts} />
              ) : (
                <ConfidenceDots value={summary.confidence} />
              )
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
        {correcting && canCorrect && onCorrectSave && onCorrectCancel && (
          <CorrectPicker
            key={summary.mapping_id}
            ownId={summary.indicator_id}
            choices={choices}
            onSave={onCorrectSave}
            onCancel={onCorrectCancel}
          />
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
          <button
            type="button"
            className={`btn ev-act correct${status === 'corrected' ? ' active' : ''}`}
            aria-pressed={status === 'corrected'}
            aria-expanded={correcting}
            disabled={!canCorrect}
            title={
              detail !== null && !canCorrect ? 'No other Indicator of this Run to correct to' : undefined
            }
            data-testid="decide-correct"
            onClick={() => onCorrect?.()}
          >
            {status === 'corrected' && <DecisionGlyph status="corrected" />}
            Correct <kbd>C</kbd>
          </button>
        </div>
      </div>
      )}
    </section>
  )
}
