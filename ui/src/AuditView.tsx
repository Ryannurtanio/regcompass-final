import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  fetchRecordDetail,
  fetchRecords,
  fetchReviewQueue,
  postReview,
  reviewGloss,
} from './api'
import type {
  QueueRecord,
  RecordDetail,
  RecordSummary,
  ReviewStatus,
  SourceFormat,
} from './types'
import PdfPane from './PdfPane'
import SourceTextPane from './SourceTextPane'
import { paneFor } from './sourcePane'
import RecordPane from './RecordPane'
import ErrorNote from './ErrorNote'
import { plainError, type PlainError } from './errors'
import { isForFocusedControl, isInShell } from './keys'

// What the reviewer is told when a save or a load fails. Plain words, shown
// beside the decision buttons, and the panes stay where they are: one failed
// save is not a reason to lose the page and the quote.
const NOT_SAVED = 'That decision was not saved. Try again.'
const GLOSS_NOT_SAVED = 'The gloss approval was not saved. Try again.'
const DETAIL_FAILED = 'This Mapping could not be loaded. Step to another one and back to retry.'
const LIST_FAILED = 'The Mappings could not be loaded. Go back and open them again.'

/** One row of the list the audit view is stepping through. Opened from the
 *  Documents table it is one Document's row and carries no Document of its
 *  own; opened from the Review queue it names the Document it came from,
 *  because that list spans them all. */
type Row = RecordSummary & Partial<Pick<QueueRecord, 'document_id' | 'document_title'>>

export default function AuditView({
  documentId,
  runId,
  queue = false,
  unreviewedOnly = false,
  startMappingId = null,
  onBack,
  onReviewSaved,
  onDocumentTitle,
  onCurrent,
  readOnly = false,
  onOpenEvidence,
}: {
  documentId: string
  // The Run these Mappings belong to; null means the newest completed Run.
  runId: string | null
  // Step through the Review queue (every Document of the Run, lowest
  // Confidence first) instead of this one Document's records. Accept, reject,
  // flag and the previous and next keys then all follow queue order, because
  // they read the same list.
  queue?: boolean
  unreviewedOnly?: boolean
  // The row the reviewer opened, so the view lands on it rather than on the
  // top of the list.
  startMappingId?: string | null
  onBack: () => void
  onReviewSaved: () => void
  // The Document the current row belongs to, for the header. In queue order it
  // changes under the reviewer's feet, and a header naming the Document they
  // started on would be naming the wrong law.
  onDocumentTitle?: (title: string | null) => void
  // The Mapping on screen, each time it changes, for a caller that shows
  // more about it beside the panes.
  onCurrent?: (mappingId: string) => void
  // Look, do not decide: shown inside a Run, where a stray key must never
  // write a Review Decision. No A, R, F, no note, no gloss approval, and no
  // page keys (the page scrolls as usual); Escape still leaves.
  readOnly?: boolean
  onOpenEvidence?: () => void
}) {
  const [records, setRecords] = useState<Row[] | null>(null)
  const [idx, setIdx] = useState(0)
  const [detail, setDetail] = useState<RecordDetail | null>(null)
  const [page, setPage] = useState(1)
  const [error, setError] = useState<PlainError | null>(null)
  // A failure the reviewer can carry on past, shown inline by the decision.
  const [notice, setNotice] = useState<string | null>(null)
  // The note travels with the decision, so it lives here rather than in the
  // pane: A, R and F send whatever the reviewer has typed for THIS Mapping.
  const [note, setNote] = useState('')
  // What each Document's stored file is, learned from its first record. Kept
  // per Document so the PDF pane stays mounted (and its file loaded) while the
  // reviewer steps through one PDF's Mappings, and a web page is never handed
  // to the PDF viewer while its record is still loading.
  const [formats, setFormats] = useState<Record<string, SourceFormat>>({})

  useEffect(() => {
    let live = true
    const loading: Promise<Row[]> = queue
      ? fetchReviewQueue(runId, unreviewedOnly).then((q) => q.records)
      : fetchRecords(documentId, runId)
    loading
      .then((rs) => {
        if (!live) return
        setRecords(rs)
        const at = startMappingId
          ? rs.findIndex((r) => r.mapping_id === startMappingId)
          : -1
        setIdx(at === -1 ? 0 : at)
      })
      .catch((e) => {
        console.error(e)
        if (live) setError({ message: LIST_FAILED, technical: plainError(e).technical ?? plainError(e).message })
      })
    return () => {
      live = false
    }
  }, [documentId, runId, queue, unreviewedOnly, startMappingId])

  const current = records?.[idx] ?? null
  // In queue order the PDF pane follows the row, not the Document the reviewer
  // opened the queue from.
  const activeDocumentId = current?.document_id ?? documentId

  useEffect(() => {
    onDocumentTitle?.(current?.document_title ?? null)
  }, [current?.document_title, onDocumentTitle])

  useEffect(() => {
    if (current) onCurrent?.(current.mapping_id)
  }, [current?.mapping_id, onCurrent]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!current) return
    setDetail(null)
    setNotice(null)
    setNote(current.review_note ?? '')
    fetchRecordDetail(current.mapping_id, runId)
      .then((d) => {
        setDetail(d)
        setFormats((f) =>
          f[d.record.document_id] === d.source_format
            ? f
            : { ...f, [d.record.document_id]: d.source_format },
        )
        const first = d.highlights[0]
        setPage(first ? first.page : d.record.page_number ?? 1)
      })
      .catch((e) => {
        console.error(e)
        setNotice(DETAIL_FAILED)
      })
  }, [current?.mapping_id, runId]) // eslint-disable-line react-hooks/exhaustive-deps

  const review = useCallback(
    (status: ReviewStatus) => {
      if (readOnly || !current || !records) return
      setNotice(null)
      postReview(runId, current.mapping_id, status, note)
        .then((rev) => {
          setRecords(
            records.map((r) =>
              r.mapping_id === current.mapping_id
                ? { ...r, review_status: status, review_note: rev.comment }
                : r,
            ),
          )
          setDetail((d) => (d ? { ...d, review: rev } : d))
          onReviewSaved()
          // keyboard-first flow: advance to the next unreviewed record
          const after = records.findIndex(
            (r, i) => i > idx && r.review_status === null && r.mapping_id !== current.mapping_id,
          )
          if (after !== -1) setIdx(after)
        })
        .catch((e) => {
          console.error(e)
          setNotice(NOT_SAVED)
        })
    },
    [readOnly, current, records, idx, note, runId, onReviewSaved],
  )

  const glossReview = useCallback(
    (english: string, reviewedBy: string) => {
      if (readOnly || !current) return
      setNotice(null)
      reviewGloss(runId, current.mapping_id, english, reviewedBy)
        .then((g) => setDetail((d) => (d ? { ...d, gloss: g } : d)))
        .catch((e) => {
          console.error(e)
          setNotice(GLOSS_NOT_SAVED)
        })
    },
    [readOnly, current, runId],
  )

  useEffect(() => {
    if (readOnly) return
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      // A key the top bar or the phone menu already used (Esc on a nav tip or
      // on the open menu) is not also "leave the audit view".
      if (e.defaultPrevented || isInShell(e)) return
      // A reviewer typing a note is writing prose, not pressing shortcuts:
      // without this, the "a" in "ambiguous" would accept the Mapping.
      const target = e.target as HTMLElement | null
      if (
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT' ||
          target.isContentEditable)
      ) {
        if (e.key === 'Escape') target.blur()
        return
      }
      // Enter and Space belong to the button or link that has focus.
      if ((e.key === 'Enter' || e.key === ' ') && isForFocusedControl(e)) return
      switch (e.key) {
        case 'Escape':
          onBack()
          break
        case 'j':
        case 'ArrowDown':
          e.preventDefault()
          setIdx((i) => Math.min(i + 1, (records?.length ?? 1) - 1))
          break
        case 'k':
        case 'ArrowUp':
          e.preventDefault()
          setIdx((i) => Math.max(i - 1, 0))
          break
        case 'a':
          review('accepted')
          break
        case 'r':
          review('rejected')
          break
        case 'f':
          review('flagged')
          break
        case 'ArrowLeft':
          setPage((p) => Math.max(1, p - 1))
          break
        case 'ArrowRight':
          setPage((p) => p + 1)
          break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [readOnly, onBack, review, records])

  const pageHighlights = useMemo(
    () => (detail ? detail.highlights.filter((h) => h.page === page) : []),
    [detail, page],
  )
  const highlightPages = useMemo(
    () => (detail ? [...new Set(detail.highlights.map((h) => h.page))].sort((a, b) => a - b) : []),
    [detail],
  )

  if (error) return <ErrorNote className="ev-error" error={error} />
  if (!records) return <div className="ev-note">Loading the Mappings…</div>
  if (records.length === 0)
    return <div className="ev-note">This Document has no proven Mappings in this Run.</div>

  const format = formats[activeDocumentId]
  const pane = paneFor(format)
  // The text pane shows only the record it was sent with; between records it
  // waits rather than showing the last one's Piece under the new header.
  const textDetail =
    detail && detail.record.mapping_id === current?.mapping_id ? detail : null

  return (
    <div className="ev-audit">
      {pane === 'pdf' ? (
        <PdfPane
          documentId={activeDocumentId}
          runId={runId}
          page={page}
          onPage={setPage}
          highlights={pageHighlights}
          highlightPages={highlightPages}
          highlightAvailable={detail?.highlight_available ?? true}
        />
      ) : pane === 'text' && format !== 'pdf' && format !== undefined && textDetail ? (
        <SourceTextPane
          format={format}
          text={textDetail.source_text}
          quote={textDetail.record.verbatim_quote}
          link={records[idx].source_link}
        />
      ) : (
        <section className="ev-pdf" aria-label="Source">
          <div className="ev-note">Loading the source…</div>
        </section>
      )}
      <RecordPane
        summary={records[idx]}
        detail={detail}
        index={idx}
        total={records.length}
        note={note}
        onNote={setNote}
        onReview={review}
        onGlossReview={glossReview}
        onPrev={() => setIdx((i) => Math.max(i - 1, 0))}
        onNext={() => setIdx((i) => Math.min(i + 1, records.length - 1))}
        readOnly={readOnly}
        onOpenEvidence={onOpenEvidence}
        notice={notice}
      />
    </div>
  )
}
