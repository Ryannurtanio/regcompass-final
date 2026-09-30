// The orders the Review queue can be read in. The server hands the whole Run
// over in one list, so the order is chosen here, without another read, and
// kept in the page address (`?sort=`) so a reload opens the same order.

import type { QueueRecord } from './types'

/** Lowest Confidence first, highest Confidence first, or the order the laws
 *  themselves read in. */
export type QueueSort = 'low' | 'high' | 'document'

export const QUEUE_SORTS: readonly { value: QueueSort; label: string }[] = [
  { value: 'low', label: 'Lowest Confidence first' },
  { value: 'high', label: 'Highest Confidence first' },
  { value: 'document', label: 'Document order' },
]

/** The queue proper: the rows to hand-check come first. */
export const DEFAULT_QUEUE_SORT: QueueSort = 'low'

// "Section 9" before "Section 10": numbers inside a label compare as numbers.
const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' })

/** Law by law, then page by page, then provision by provision. A row with no
 *  page (a web page) keeps to its provision numbers alone. */
function documentOrder(a: QueueRecord, b: QueueRecord): number {
  return (
    collator.compare(a.document_title, b.document_title) ||
    (a.page_number ?? Infinity) - (b.page_number ?? Infinity) ||
    collator.compare(a.section, b.section) ||
    collator.compare(a.subsection ?? '', b.subsection ?? '') ||
    collator.compare(a.indicator_id, b.indicator_id) ||
    (a.mapping_id < b.mapping_id ? -1 : a.mapping_id > b.mapping_id ? 1 : 0)
  )
}

/** The rows in the chosen order, as a new list. In both Confidence orders a
 *  row that was not scored goes last, and equal scores read in document
 *  order, so the list is the same on every read. */
export function orderQueue(rows: readonly QueueRecord[], sort: QueueSort): QueueRecord[] {
  const out = [...rows]
  if (sort === 'document') return out.sort(documentOrder)
  const sign = sort === 'high' ? -1 : 1
  return out.sort((a, b) => {
    if (a.confidence === null || b.confidence === null) {
      if (a.confidence === b.confidence) return documentOrder(a, b)
      return a.confidence === null ? 1 : -1
    }
    return sign * (a.confidence - b.confidence) || documentOrder(a, b)
  })
}

/** The order a page address asks for; the default when it names none (or one
 *  this screen does not offer). */
export function parseQueueSort(search: string): QueueSort {
  const v = new URLSearchParams(search).get('sort')
  return QUEUE_SORTS.some((s) => s.value === v) ? (v as QueueSort) : DEFAULT_QUEUE_SORT
}

/** The query string with this order written in, leaving every other
 *  parameter as it was. The default is left out, and null (Evidence closed)
 *  takes the order out too. Empty when nothing is left. */
export function queueSortSearch(search: string, sort: QueueSort | null): string {
  const params = new URLSearchParams(search)
  if (sort && sort !== DEFAULT_QUEUE_SORT) params.set('sort', sort)
  else params.delete('sort')
  const out = params.toString()
  return out ? `?${out}` : ''
}
