import { describe, expect, it } from 'vitest'
import { DEFAULT_QUEUE_SORT, orderQueue, parseQueueSort, queueSortSearch } from './queueOrder'
import type { QueueRecord } from './types'

type Part = Pick<
  QueueRecord,
  'mapping_id' | 'document_title' | 'section' | 'subsection' | 'page_number' | 'confidence' | 'indicator_id'
>

function row(p: Partial<Part> & { mapping_id: string }): QueueRecord {
  return {
    document_id: 'doc',
    document_title: 'Act A',
    indicator_id: '6.1',
    indicator_name: '',
    section: 'Section 1',
    subsection: null,
    page_number: 1,
    quote_preview: '',
    confidence: 0.5,
    controlling_evidence: false,
    review_status: null,
    review_note: null,
    source_url: null,
    location_reference: '',
    source_link: null,
    format: 'pdf',
    corrected_indicator_id: null,
    ...p,
  } as QueueRecord
}

const ids = (rows: QueueRecord[]) => rows.map((r) => r.mapping_id)

// Two laws, a row with no score, and a tie on Confidence.
const rows = [
  row({ mapping_id: 'm1', document_title: 'Act B', section: 'Section 10', page_number: 4, confidence: 0.4 }),
  row({ mapping_id: 'm2', document_title: 'Act A', section: 'Section 2', page_number: 1, confidence: null }),
  row({ mapping_id: 'm3', document_title: 'Act A', section: 'Section 9', page_number: 3, confidence: 0.9 }),
  row({ mapping_id: 'm4', document_title: 'Act B', section: 'Section 2', page_number: 1, confidence: 0.4 }),
  row({ mapping_id: 'm5', document_title: 'Act A', section: 'Section 10', page_number: 3, confidence: 0.7 }),
]

describe('orderQueue', () => {
  it('puts the lowest Confidence first, not scored last, ties in document order', () => {
    expect(ids(orderQueue(rows, 'low'))).toEqual(['m4', 'm1', 'm5', 'm3', 'm2'])
  })

  it('puts the highest Confidence first, not scored still last', () => {
    expect(ids(orderQueue(rows, 'high'))).toEqual(['m3', 'm5', 'm4', 'm1', 'm2'])
  })

  it('reads in document order: law, then page, then provision in number order', () => {
    expect(ids(orderQueue(rows, 'document'))).toEqual(['m2', 'm3', 'm5', 'm4', 'm1'])
  })

  it('orders provisions of a web page (no pages) by their numbers', () => {
    const web = [
      row({ mapping_id: 'w1', section: 'Article 12', page_number: null }),
      row({ mapping_id: 'w2', section: 'Article 3', subsection: '(2)', page_number: null }),
      row({ mapping_id: 'w3', section: 'Article 3', subsection: '(1)', page_number: null }),
    ]
    expect(ids(orderQueue(web, 'document'))).toEqual(['w3', 'w2', 'w1'])
  })

  it('leaves the input list as it was', () => {
    const before = ids(rows)
    orderQueue(rows, 'high')
    expect(ids(rows)).toEqual(before)
  })
})

describe('the sort in the address', () => {
  it('reads each order and falls back to the default for anything else', () => {
    expect(parseQueueSort('?sort=high')).toBe('high')
    expect(parseQueueSort('?economy=MY&sort=document')).toBe('document')
    expect(parseQueueSort('?sort=low')).toBe('low')
    expect(parseQueueSort('?sort=nonsense')).toBe(DEFAULT_QUEUE_SORT)
    expect(parseQueueSort('')).toBe(DEFAULT_QUEUE_SORT)
    expect(DEFAULT_QUEUE_SORT).toBe('low')
  })

  it('writes the order in, keeping every other parameter', () => {
    expect(queueSortSearch('?economy=MY&engine=a', 'high')).toBe('?economy=MY&engine=a&sort=high')
    expect(parseQueueSort(queueSortSearch('?run=r1', 'document'))).toBe('document')
  })

  it('leaves the default and a closed screen out of the address', () => {
    expect(queueSortSearch('?economy=MY&sort=high', 'low')).toBe('?economy=MY')
    expect(queueSortSearch('?sort=document', null)).toBe('')
  })
})
