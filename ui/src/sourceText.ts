// How the drill-down describes a Document or a passage of it. A web page has
// no pages (its text is stored as page 1 of one), and Chinese text has no
// spaces between words, so both need their own words.

export type SourceKind = 'pdf' | 'html' | null | undefined

const count = (n: number, one: string) => `${n.toLocaleString('en-US')} ${n === 1 ? one : `${one}s`}`

// Han, kana and Hangul: one character carries about a word.
const DENSE = /[぀-ヿ㐀-鿿가-힯]/g

/** The size of a Document, for its one-line summary: its pages, or "web page". */
export function documentSize(format: SourceKind, nPages: number | null | undefined): string | null {
  if (format === 'html') return 'web page'
  return nPages ? count(nPages, 'page') : null
}

/** ", page 4" after a place in a PDF; nothing for a web page. */
export function pageSuffix(format: SourceKind, page: number | null | undefined): string {
  return format !== 'html' && page ? `, page ${page}` : ''
}

/** Where a passage sits in its PDF: "page 4" or "pages 4 to 6"; nothing for a
 *  web page. */
export function pageSpan(
  format: SourceKind,
  page: number | null | undefined,
  pageEnd: number | null | undefined,
): string | null {
  if (format === 'html' || !page) return null
  return pageEnd && pageEnd !== page ? `pages ${page} to ${pageEnd}` : `page ${page}`
}

/** How long a passage is: its words, or for text written mostly in Chinese,
 *  Japanese or Korean, its characters, because spaces do not separate words
 *  there and counting them would call a whole article "3 words". */
export function textSize(text: string): string {
  const dense = (text.match(DENSE) ?? []).length
  const words = text.replace(DENSE, ' ').split(/\s+/).filter(Boolean).length
  if (dense > words) return count(dense + words, 'character')
  return count(words, 'word')
}
