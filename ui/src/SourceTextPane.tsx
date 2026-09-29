import { useEffect, useRef } from 'react'
import { markQuote } from './drillDown'
import OpenSource from './OpenSource'
import { SOURCE_NOTE } from './sourcePane'
import type { SourceFormat, SourceLink } from './types'

// The left pane for a source that is not a PDF: the Piece's text in the law
// face, with the Verbatim Quote marked where it sits. The quote is found by its
// own words, the same way the drill-down marks it inside its Piece.
export default function SourceTextPane({
  format,
  text,
  quote,
  link,
}: {
  format: Exclude<SourceFormat, 'pdf'>
  text: string | null
  quote: string | null
  link: SourceLink | null
}) {
  const markRef = useRef<HTMLElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const marked = text ? markQuote(text, quote) : null

  useEffect(() => {
    const mark = markRef.current
    const scroll = scrollRef.current
    if (!scroll) return
    if (!mark) {
      scroll.scrollTop = 0
      return
    }
    const into = mark.getBoundingClientRect().top - scroll.getBoundingClientRect().top
    scroll.scrollTop = Math.max(0, scroll.scrollTop + into - 24)
  }, [text, quote])

  return (
    <section className="ev-pdf" aria-label="Source text">
      <div className="ev-pdf-bar">
        <span className="ev-muted">{SOURCE_NOTE[format]}</span>
        <OpenSource link={link} />
      </div>
      <div className="ev-pdf-scroll ev-text-scroll" ref={scrollRef}>
        {text ? (
          <div className="ev-source-text" data-testid="source-text">
            {marked ? (
              <>
                {marked.before}
                <mark ref={markRef} className="ev-text-hl" data-testid="quote-highlight">
                  {marked.quote}
                </mark>
                {marked.after}
              </>
            ) : (
              text
            )}
          </div>
        ) : (
          <div className="ev-note">The text of this source is not available here.</div>
        )}
      </div>
      {text && !marked && (
        <div className="ev-pdf-bar ev-text-foot">
          <span className="ev-muted">
            No highlight position for this Mapping: find the quote by its words.
          </span>
        </div>
      )}
    </section>
  )
}
