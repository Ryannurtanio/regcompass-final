import { describe, expect, it } from 'vitest'
import { afterEach, vi } from 'vitest'
import {
  discoveryLog,
  discoveryViewReducer,
  emptyDiscoveryView,
  pillarPanel,
  revealStepMs,
  startReveal,
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

describe('a Discovery by Pillar', () => {
  const skippedLaw = {
    law: 'Firm Copy Act',
    indicators: ['4.2'],
    urls: ['https://www.examplelawfirm.com/a.pdf'],
    code: 'not_allowed_host',
    reason: 'Its address (www.examplelawfirm.com) is not on this Economy\'s list of official hosts, so it was not fetched.',
  }

  function drawnDiscovery(): DiscoveryEvent[] {
    return numbered([
      PORTAL,
      { type: 'discovery_found', url: A, name: 'Telecommunications Act 1999' },
      { type: 'discovery_fetched', url: A, size_bytes: 1200, method: 'httpx' },
      { type: 'discovery_added', url: A, document_id: 'doc_a', title: 'Telecommunications Act 1999', n_pages: 12, ocr_applied: false },
      {
        type: 'discovery_finished',
        counts: {
          run_id: 'disc_1', found: 1, fetched: 1, added: 1, skipped: 0,
          pillar: 4, indicators: ['4.2', '4.3'], max_documents: 12,
          found_by: [{ url: A, document_id: 'doc_a', title: 'Telecommunications Act 1999', found_by: 'baseline 4.2', status: 'fetched' }],
          baseline_skipped: [skippedLaw],
          notes: ['no crawl seed is tagged for Pillar 4, so the Portal crawler did not run'],
        },
      },
    ])
  }

  it('keeps the draw, why each Document came in, and the laws left out', () => {
    const s = feed(drawnDiscovery())
    expect(s.drawn).toEqual({
      pillar: 4,
      indicators: ['4.2', '4.3'],
      max_documents: 12,
      baseline_skipped: [skippedLaw],
      notes: ['no crawl seed is tagged for Pillar 4, so the Portal crawler did not run'],
    })
    expect(s.documents[0].found_by).toBe('baseline 4.2')
  })

  it('is null for a Discovery with no Pillar', () => {
    expect(feed(normalDiscovery()).drawn).toBeNull()
    expect(feed(normalDiscovery()).documents.every((d) => d.found_by === null)).toBe(true)
  })

  it('survives a reload', () => {
    const s = discoveryViewReducer(emptyDiscoveryView, { type: 'replay', events: drawnDiscovery() })
    expect(s.drawn?.baseline_skipped).toEqual([skippedLaw])
  })
})

describe('a law found for several Indicators', () => {
  const IN_CORPUS = 'Already in the Corpus, so it was not asked for again.'

  // Law A is cited for 7.1 and 7.3: added for 7.1, then asked for again for
  // 7.3 and reported as already in the Corpus. Law B came from the Portal.
  function twiceCited(): DiscoveryEvent[] {
    return numbered([
      PORTAL,
      { type: 'discovery_found', url: A, name: 'Personal Data Protection Act' },
      { type: 'discovery_added', url: A, document_id: 'doc_a', title: 'Personal Data Protection Act', n_pages: 9, ocr_applied: false },
      { type: 'discovery_skipped', url: A, code: 'in_corpus', reason: IN_CORPUS, title: 'Personal Data Protection Act' },
      { type: 'discovery_found', url: B, name: 'Cybersecurity Act' },
      { type: 'discovery_added', url: B, document_id: 'doc_b', title: 'Cybersecurity Act', n_pages: 4, ocr_applied: false },
      {
        type: 'discovery_finished',
        counts: {
          run_id: 'disc_1', found: 2, fetched: 2, added: 2, skipped: 1, already_in_corpus: 1,
          pillar: 7, indicators: ['7.1', '7.2', '7.3'], max_documents: 12,
          found_by: [
            { url: A, document_id: 'doc_a', title: 'Personal Data Protection Act', found_by: 'baseline 7.3', status: 'fetched' },
            { url: B, document_id: 'doc_b', title: 'Cybersecurity Act', found_by: 'portal crawler', status: 'fetched' },
            { url: A, document_id: 'doc_a', title: 'Personal Data Protection Act', found_by: 'official source list 7.1, 6.2', status: 'already in the Corpus' },
          ],
          baseline_skipped: [],
          notes: [],
        },
      },
    ])
  }

  it('merges every found_by entry for the same address', () => {
    const s = feed(twiceCited())
    const a = s.documents.find((d) => d.url === A)!
    expect(a.indicators).toEqual(['6.2', '7.1', '7.3'])
    expect(s.documents.find((d) => d.url === B)!.indicators).toEqual([])
  })

  it('never demotes a Document this Discovery added to not added', () => {
    const running = feed(twiceCited().slice(0, 4))
    expect(running.documents[0].state).toBe('added')
    expect(running.counts).toMatchObject({ added: 1, skipped: 0 })
    const s = feed(twiceCited())
    expect(s.documents.map((d) => d.state)).toEqual(['added', 'added'])
    // The Discovery counted the second ask as skipped; the screen agrees with its list.
    expect(s.counts).toMatchObject({ found: 2, added: 2, skipped: 0 })
  })

  it('still marks a Document an earlier Discovery added as not added', () => {
    const s = feed(
      numbered([
        PORTAL,
        { type: 'discovery_skipped', url: A, code: 'in_corpus', reason: IN_CORPUS, title: 'Held Act' },
      ]),
    )
    expect(s.documents[0]).toMatchObject({ state: 'skipped', title: 'Held Act' })
    // Found, and already in the Corpus: not counted as not added.
    expect(s.counts).toMatchObject({ found: 1, added: 0, skipped: 0 })
    expect(pillarPanel({ ...s, drawn: { pillar: 7, indicators: null, max_documents: null, baseline_skipped: [], notes: [] } })!.rows[0])
      .toMatchObject({ added: false, inCorpus: true })
  })

  it('never takes a Document this Discovery added back to not added, whatever the later skip', () => {
    const s = feed(
      numbered([
        PORTAL,
        { type: 'discovery_found', url: A, name: null },
        { type: 'discovery_added', url: A, document_id: 'doc_a', title: 'Telecommunications Act 1999', n_pages: 12, ocr_applied: false },
        { type: 'discovery_skipped', url: A, code: 'duplicate', reason: 'Same file.' },
        { type: 'discovery_skipped', url: A, code: 'fetch_error', reason: 'No.' },
      ]),
    )
    expect(s.documents[0]).toMatchObject({ state: 'added', document_id: 'doc_a', code: null })
    expect(s.counts).toMatchObject({ found: 1, added: 1, skipped: 0 })
    expect(discoveryLog(s).map((l) => l.tag)).toEqual(['>', 'found', 'added'])
  })

  it('writes the log from the events, one line per law', () => {
    const s = feed(twiceCited())
    expect(discoveryLog(s).map((l) => [l.tag, l.text, l.right])).toEqual([
      ['>', 'Accessing the official legal portals for Singapore…', ''],
      ['found', 'Personal Data Protection Act', 'sso.agc.gov.sg'],
      ['added', 'to the Corpus', 'Added 1'],
      ['found', 'Cybersecurity Act', 'sso.agc.gov.sg'],
      ['added', 'to the Corpus', 'Added 2'],
      ['done', '2 laws added to the Corpus for Singapore, Pillar 7.', ''],
    ])
  })

  it('logs a law already in the Corpus and one not added', () => {
    const s = feed(
      numbered([
        PORTAL,
        { type: 'discovery_skipped', url: A, code: 'in_corpus', reason: IN_CORPUS, title: 'Held Act' },
        { type: 'discovery_found', url: C, name: null },
        { type: 'discovery_skipped', url: C, code: 'fetch_error', reason: 'No.' },
        { type: 'discovery_failed', message: 'RuntimeError: gone' },
      ]),
    )
    expect(discoveryLog(s).map((l) => [l.tag, l.text])).toEqual([
      ['>', 'Accessing the official legal portals for Singapore…'],
      ['in Corpus', 'Held Act, already in the Corpus'],
      ['found', 'CA2018'],
      ['not added', 'CA2018, not added'],
      ['stop', 'Discovery stopped.'],
    ])
  })

  it('lays the Documents out against the Indicators asked for', () => {
    const panel = pillarPanel(feed(twiceCited()))!
    expect(panel.pillar).toBe(7)
    expect(panel.columns).toEqual(['7.1', '7.2', '7.3'])
    expect(panel.rows.map((r) => [r.title, r.ids, r.added])).toEqual([
      ['Personal Data Protection Act', ['7.1', '7.3'], true],
      ['Cybersecurity Act', [], true],
    ])
  })

  it('uses the Indicators found when every one was asked for', () => {
    const events = twiceCited()
    const last = events[events.length - 1] as Extract<DiscoveryEvent, { type: 'discovery_finished' }>
    const every = [...events.slice(0, -1), { ...last, counts: { ...last.counts, indicators: null } }]
    expect(pillarPanel(feed(every))!.columns).toEqual(['7.1', '7.3'])
  })

  it('has no panel for a Discovery with no Pillar', () => {
    expect(pillarPanel(feed(normalDiscovery()))).toBeNull()
  })
})

describe('the reveal', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows one Document at a time and the whole of it inside about six seconds', () => {
    vi.useFakeTimers()
    const seen: number[] = []
    startReveal(12, 12, (n) => seen.push(n))
    expect(seen).toEqual([0])
    vi.advanceTimersByTime(700)
    expect(seen).toEqual([0, 1])
    vi.advanceTimersByTime(6000)
    expect(seen).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
    expect(700 + 11 * revealStepMs(12)).toBeLessThanOrEqual(6000)
    expect(revealStepMs(5)).toBe(1000)
  })

  it('shows the rest at once past its limit', () => {
    vi.useFakeTimers()
    const seen: number[] = []
    startReveal(20, 12, (n) => seen.push(n))
    vi.advanceTimersByTime(10000)
    expect(seen[seen.length - 1]).toBe(20)
    expect(seen).not.toContain(13)
  })

  it('stops when told to, leaving no timer behind', () => {
    vi.useFakeTimers()
    const seen: number[] = []
    const stop = startReveal(5, 12, (n) => seen.push(n))
    vi.advanceTimersByTime(1700)
    stop()
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(10000)
    expect(seen).toEqual([0, 1, 2])
  })
})
