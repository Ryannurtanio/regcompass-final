import { describe, expect, it } from 'vitest'
import {
  discoveryViewReducer,
  emptyDiscoveryView,
  type DiscoveryViewState,
} from './discoveryViewState'
import type { DiscoveryEvent } from './types'

// A hand-made Discovery: three Documents found; one fetched and added from a
// scan, one fetched and added as text, one the Portal answered 404 for.
type Draft = DiscoveryEvent extends infer E
  ? E extends DiscoveryEvent
    ? Omit<E, 'seq' | 'ts'>
    : never
  : never

function numbered(drafts: Draft[], from = 0): DiscoveryEvent[] {
  return drafts.map(
    (d, i) =>
      ({ ...d, seq: from + i, ts: `2026-09-28T10:00:${String(i).padStart(2, '0')}+00:00` }) as DiscoveryEvent,
  )
}

const A = 'https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf'
const B = 'https://sso.agc.gov.sg/Act/PDPA2012?ViewType=Pdf'
const C = 'https://sso.agc.gov.sg/Act/CA2018?ViewType=Pdf'
const NOT_FOUND = 'The Portal answered HTTP 404, so nothing was saved.'

const PORTAL: Draft = {
  type: 'discovery_portal',
  run_id: 'disc_1',
  economy: 'SG',
  name: 'Singapore',
  hosts: ['sso.agc.gov.sg'],
  strategy: 'curl_cffi_ladder',
  refresh: false,
}

function normalDiscovery(): DiscoveryEvent[] {
  return numbered([
    PORTAL,
    { type: 'discovery_found', url: A, name: 'TA1999.pdf' },
    { type: 'discovery_found', url: B, name: null },
    { type: 'discovery_found', url: C, name: null },
    { type: 'discovery_fetched', url: A, size_bytes: 1200, method: 'httpx' },
    { type: 'discovery_skipped', url: C, code: 'http_error', reason: NOT_FOUND },
    { type: 'discovery_fetched', url: B, size_bytes: 900, method: 'httpx' },
    { type: 'discovery_added', url: A, document_id: 'doc_a', title: 'Telecommunications Act 1999', n_pages: 12, ocr_applied: true },
    { type: 'discovery_added', url: B, document_id: 'doc_b', title: 'Personal Data Protection Act 2012', n_pages: 80, ocr_applied: false },
    {
      type: 'discovery_finished',
      counts: {
        run_id: 'disc_1', found: 3, fetched: 2, added: 2, skipped: 1, stored: 2, already_in_corpus: 0,
        failed: 1, disallowed: 0, duplicates: 0, off_whitelist: 0, spacing_seconds: 6,
      },
    },
  ])
}

function feed(events: DiscoveryEvent[], start: DiscoveryViewState = emptyDiscoveryView) {
  return events.reduce((s, event) => discoveryViewReducer(s, { type: 'event', event }), start)
}

describe('the Discovery view state', () => {
  it('starts empty', () => {
    expect(emptyDiscoveryView.status).toBe('idle')
    expect(emptyDiscoveryView.documents).toEqual([])
  })

  it('names the Portal and starts running', () => {
    const s = feed(normalDiscovery().slice(0, 1))
    expect(s.status).toBe('running')
    expect(s.portal).toMatchObject({ economy: 'SG', name: 'Singapore', hosts: ['sso.agc.gov.sg'] })
    expect(s.phases.find).toBe('working')
    expect(s.started_at).toBe('2026-09-28T10:00:00+00:00')
  })

  it('lists each Document as it arrives, in the order found', () => {
    const s = feed(normalDiscovery().slice(0, 4))
    expect(s.documents.map((d) => d.url)).toEqual([A, B, C])
    expect(s.documents.every((d) => d.state === 'found')).toBe(true)
    expect(s.documents[0].title).toBe('TA1999.pdf')
    expect(s.counts.found).toBe(3)
    expect(s.phases).toMatchObject({ find: 'done', fetch: 'working', read: 'waiting' })
  })

  it('moves a Document from found to fetched to added, with its title and pages', () => {
    const events = normalDiscovery()
    const mid = feed(events.slice(0, 5))
    expect(mid.documents[0]).toMatchObject({ state: 'fetched', size_bytes: 1200 })
    expect(mid.counts.fetched).toBe(1)
    const after = feed(events.slice(0, 8))
    expect(after.documents[0]).toMatchObject({
      state: 'added',
      title: 'Telecommunications Act 1999',
      n_pages: 12,
      ocr_applied: true,
      document_id: 'doc_a',
    })
    expect(after.phases).toMatchObject({ fetch: 'done', read: 'working', add: 'working' })
  })

  it('keeps the plain reason on a skipped Document', () => {
    const s = feed(normalDiscovery().slice(0, 6))
    const c = s.documents.find((d) => d.url === C)!
    expect(c.state).toBe('skipped')
    expect(c.code).toBe('http_error')
    expect(c.reason).toBe(NOT_FOUND)
    expect(s.counts.skipped).toBe(1)
  })

  it('finishes with the Discovery own numbers and every phase settled', () => {
    const s = feed(normalDiscovery())
    expect(s.status).toBe('finished')
    expect(s.counts).toEqual({ found: 3, fetched: 2, added: 2, skipped: 1, scans: 1 })
    expect(s.phases).toEqual({ find: 'done', fetch: 'done', read: 'done', add: 'done' })
    expect(s.spacing_seconds).toBe(6)
    expect(s.ended_at).toBe('2026-09-28T10:00:09+00:00')
  })

  it('marks the phases with nothing to do when everything was already in the Corpus', () => {
    const s = feed(
      numbered([
        PORTAL,
        { type: 'discovery_found', url: A, name: null },
        { type: 'discovery_skipped', url: A, code: 'in_corpus', reason: 'Already in the Corpus, so it was not asked for again.' },
        { type: 'discovery_finished', counts: { found: 1, fetched: 0, added: 0, skipped: 1 } },
      ]),
    )
    expect(s.phases).toEqual({ find: 'done', fetch: 'nothing', read: 'nothing', add: 'nothing' })
    expect(s.documents[0].reason).toMatch(/Corpus/)
  })

  it('reports a failure with its message and keeps what it had found', () => {
    const s = feed(
      numbered([
        PORTAL,
        { type: 'discovery_found', url: A, name: null },
        { type: 'discovery_failed', message: 'RuntimeError: the Portal search went away' },
      ]),
    )
    expect(s.status).toBe('failed')
    expect(s.failure).toMatch(/went away/)
    expect(s.documents).toHaveLength(1)
    expect(s.phases.fetch).toBe('waiting')
  })

  it('shows the Corpus title of a Document skipped as already in the Corpus', () => {
    const s = feed(
      numbered([
        PORTAL,
        { type: 'discovery_found', url: A, name: 'TA1999.pdf' },
        {
          type: 'discovery_skipped',
          url: A,
          code: 'in_corpus',
          reason: 'Already in the Corpus, so it was not asked for again.',
          title: 'Telecommunications Act 1999',
        },
      ]),
    )
    expect(s.documents[0].title).toBe('Telecommunications Act 1999')
  })

  it('keeps the listed name when a skip carries no title', () => {
    const s = feed(normalDiscovery().slice(0, 6))
    expect(s.documents.find((d) => d.url === C)!.title).toBeNull()
    const named = feed(
      numbered([
        PORTAL,
        { type: 'discovery_found', url: A, name: 'TA1999.pdf' },
        { type: 'discovery_skipped', url: A, code: 'robots', reason: 'No.' },
      ]),
    )
    expect(named.documents[0].title).toBe('TA1999.pdf')
  })

  it('adds a Document it was never told was found', () => {
    const s = feed(
      numbered([
        PORTAL,
        { type: 'discovery_added', url: B, document_id: 'doc_b', title: 'PDPA', n_pages: 3, ocr_applied: false },
      ]),
    )
    expect(s.documents).toHaveLength(1)
    expect(s.documents[0]).toMatchObject({ url: B, state: 'added', title: 'PDPA' })
  })

  it('does not depend on the order events arrive in', () => {
    const events = normalDiscovery()
    const shuffled = [events[3], events[0], events[7], events[1], events[9], events[2], events[5], events[4], events[8], events[6]]
    const inOrder = feed(events)
    const outOfOrder = feed(shuffled)
    expect(outOfOrder.documents).toEqual(inOrder.documents)
    expect(outOfOrder.counts).toEqual(inOrder.counts)
    expect(outOfOrder.phases).toEqual(inOrder.phases)
    expect(outOfOrder.status).toBe('finished')
    expect(outOfOrder.events.map((e) => e.seq)).toEqual(events.map((e) => e.seq))
  })

  it('ignores an event it has already seen', () => {
    const events = normalDiscovery()
    const once = feed(events)
    expect(feed([events[4], events[5]], once)).toBe(once)
  })

  it('ignores a Run event', () => {
    const s = feed(normalDiscovery().slice(0, 2))
    const next = discoveryViewReducer(s, {
      type: 'event',
      event: { type: 'step_started', seq: 99, ts: 'x', document_id: 'd', step: 'read' } as unknown as DiscoveryEvent,
    })
    expect(next).toBe(s)
  })

  it('starts again from nothing when a new Discovery begins', () => {
    const first = feed(normalDiscovery())
    const second = feed(numbered([{ ...PORTAL, run_id: 'disc_2', refresh: true } as Draft], 20), first)
    expect(second.portal?.run_id).toBe('disc_2')
    expect(second.documents).toEqual([])
    expect(second.status).toBe('running')
  })

  it('replays a reload, keeping only the last Discovery and none of a Run', () => {
    const old = numbered([PORTAL, { type: 'discovery_found', url: C, name: null }])
    const current = numbered([{ ...PORTAL, run_id: 'disc_9' } as Draft], 10).concat(normalDiscovery().slice(1).map((e) => ({ ...e, seq: e.seq + 10 })))
    const runEvent = { type: 'run_started', seq: 5, ts: 'x' }
    const s = discoveryViewReducer(emptyDiscoveryView, {
      type: 'replay',
      events: [...current.slice().reverse(), runEvent, ...old, current[3]],
    })
    expect(s.portal?.run_id).toBe('disc_9')
    expect(s.documents.map((d) => d.url)).toEqual([A, B, C])
    expect(s.status).toBe('finished')
    expect(s.events).toHaveLength(current.length)
  })

  it('resets', () => {
    expect(discoveryViewReducer(feed(normalDiscovery()), { type: 'reset' })).toBe(emptyDiscoveryView)
  })
})
