import { useEffect, useRef, useState } from 'react'
import {
  getDocument,
  GlobalWorkerOptions,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { pdfUrl } from './api'
import { PANE_FAILED, isCancelledRender } from './sourcePane'
import ErrorNote from './ErrorNote'
import { plainError, type PlainError } from './errors'
import type { HighlightRect } from './types'

// The worker is emitted as a local hashed asset by Vite (?url import): no
// CDN, works offline, and the real-worker path never needs the eval fallback.
GlobalWorkerOptions.workerSrc = workerUrl

// Where the viewer finds its WebAssembly image decoders. A scanned act is a
// page of compressed image data, and without this the viewer has no decoder
// for it: the page renders blank white with the quote highlight floating on
// nothing. It must be a DIRECTORY url ending in a slash, because the viewer
// appends each module's own file name to it. The build copies those modules
// into this folder under their own names, so the fetch is local and the whole
// audit view keeps working with no internet at all.
const wasmUrl = new URL('wasm/', document.baseURI).href

export default function PdfPane({
  documentId,
  runId,
  page,
  onPage,
  highlights,
  highlightPages,
  highlightAvailable,
}: {
  documentId: string
  runId: string | null
  page: number
  onPage: (p: number) => void
  highlights: HighlightRect[]
  highlightPages: number[]
  highlightAvailable: boolean
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const docRef = useRef<{ id: string; doc: PDFDocumentProxy } | null>(null)
  const highlightsRef = useRef<HighlightRect[]>(highlights)
  highlightsRef.current = highlights
  const [numPages, setNumPages] = useState(0)
  // scale maps PDF points (the backend's WordBox space, top-left origin) to
  // rendered CSS pixels; the overlay divs use the same factor.
  const [scale, setScale] = useState(1)
  // A plain sentence, never the renderer's own exception text.
  // The page that would not draw: a plain line, the cause behind the toggle.
  const [failed, setFailed] = useState<PlainError | null>(null)

  useEffect(() => {
    let cancelled = false
    // The draw in flight on the one canvas. A newer page or Mapping cancels it
    // on the way out: two draws on the same canvas is an error that sticks.
    let task: RenderTask | null = null
    const render = async () => {
      try {
        const key = `${documentId}@${runId ?? ''}`
        if (!docRef.current || docRef.current.id !== key) {
          const doc = await getDocument({ url: pdfUrl(documentId, runId), wasmUrl }).promise
          if (cancelled) return
          docRef.current = { id: key, doc }
          setNumPages(doc.numPages)
        }
        const doc = docRef.current.doc
        const clamped = Math.min(Math.max(1, page), doc.numPages)
        if (clamped !== page) {
          onPage(clamped)
          return
        }
        const pdfPage = await doc.getPage(clamped)
        if (cancelled) return
        const base = pdfPage.getViewport({ scale: 1 })
        const paneWidth = (scrollRef.current?.clientWidth ?? 800) - 32
        const s = Math.min(paneWidth / base.width, 2)
        const viewport = pdfPage.getViewport({ scale: s })
        const canvas = canvasRef.current
        if (!canvas) return
        canvas.width = viewport.width
        canvas.height = viewport.height
        const ctx = canvas.getContext('2d')!
        task = pdfPage.render({ canvas, canvasContext: ctx, viewport })
        await task.promise
        if (!cancelled) {
          setScale(s)
          setFailed(null)
          // bring the quote into view: the page is usually taller than the pane
          const first = highlightsRef.current[0]
          if (first && scrollRef.current) {
            scrollRef.current.scrollTop = Math.max(0, first.y0 * s - 120)
          }
        }
      } catch (e) {
        if (cancelled || isCancelledRender(e)) return
        console.error(e)
        setFailed({ message: PANE_FAILED, technical: plainError(e).technical ?? String((e as Error)?.message ?? e) })
      }
    }
    render()
    return () => {
      cancelled = true
      task?.cancel()
    }
  }, [documentId, runId, page, onPage])

  const onQuotePage = highlightPages.includes(page)

  return (
    <section className="ev-pdf" aria-label="PDF page">
      <div className="ev-pdf-bar">
        <div className="ev-pager">
          <button
            type="button"
            className="btn ev-icon"
            aria-label="Previous page"
            onClick={() => onPage(Math.max(1, page - 1))}
            disabled={page <= 1}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <path d="M9 2.5 L4.5 7 L9 11.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
            </svg>
          </button>
          <span className="ev-page-no" aria-live="polite">
            Page {page}
            {numPages ? ` of ${numPages}` : ''}
          </span>
          <button
            type="button"
            className="btn ev-icon"
            aria-label="Next page"
            onClick={() => onPage(page + 1)}
            disabled={numPages > 0 && page >= numPages}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <path d="M5 2.5 L9.5 7 L5 11.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
            </svg>
          </button>
        </div>
        {highlightPages.length > 0 && (
          <span className="ev-quote-pages">
            <span className="ev-muted">
              Quote highlighted on page{highlightPages.length > 1 ? 's' : ''}
            </span>
            {highlightPages.map((p) => (
              <button
                key={p}
                type="button"
                className={`ev-page-chip${p === page ? ' on' : ''}`}
                aria-current={p === page ? 'page' : undefined}
                onClick={() => onPage(p)}
              >
                {p}
              </button>
            ))}
          </span>
        )}
        {highlightPages.length > 0 && !onQuotePage && (
          <span className="ev-muted ev-offpage">The quote is not on this page.</span>
        )}
        {!highlightAvailable && (
          <span className="ev-muted">
            No highlight position for this Mapping: find the quote by its words.
          </span>
        )}
      </div>
      <div className="ev-pdf-scroll" ref={scrollRef}>
        {/* The canvas stays mounted through a failure, so the next page or
            Mapping can draw on it and clear the note. */}
        {failed && <ErrorNote className="ev-note" error={failed} />}
        <div className="ev-pdf-page" data-testid="pdf-page" hidden={failed !== null}>
          <canvas ref={canvasRef} />
          {highlights.map((h, i) => (
            <div
              key={i}
              className="ev-hl"
              data-testid="quote-highlight"
              style={{
                left: h.x0 * scale,
                top: h.y0 * scale,
                width: (h.x1 - h.x0) * scale,
                height: (h.y1 - h.y0) * scale,
              }}
            />
          ))}
        </div>
      </div>
    </section>
  )
}
