import type { SourceFormat } from './types'

/** Which left pane the audit view shows. Only a PDF can be drawn as pages;
 *  anything else is shown as its text. Until the record says what the file
 *  is, neither: handing a web page to the PDF viewer only fails. */
export function paneFor(format: SourceFormat | undefined): 'pdf' | 'text' | 'loading' {
  if (format === undefined) return 'loading'
  return format === 'pdf' ? 'pdf' : 'text'
}

/** The line above a source shown as text, saying why there is no page. */
export const SOURCE_NOTE: Record<Exclude<SourceFormat, 'pdf'>, string> = {
  html: 'This source is a web page, so its text is shown here.',
  other: 'This source is not a PDF, so its text is shown here.',
  missing: 'The stored file is not available here, so its text is shown instead.',
}

/** What the PDF pane says when a page cannot be drawn. */
export const PANE_FAILED =
  'This page could not be shown here. Open the source to read it.'

/** A render the pane itself called off, because the reviewer moved to another
 *  page or Mapping before it finished. Expected, and never shown as an error. */
export function isCancelledRender(e: unknown): boolean {
  return e instanceof Error && e.name === 'RenderingCancelledException'
}
