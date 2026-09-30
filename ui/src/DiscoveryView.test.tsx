import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import DiscoveryView from './DiscoveryView'
import { discoveryViewReducer, emptyDiscoveryView, type DiscoveryViewState } from './discoveryViewState'
import type { BaselineSkip, DiscoveryEvent } from './types'

// The Discovery view says what Discovery did, not how it works: the server's
// skip reasons, notes, hosts and limits stay in the events and the logs.

const HOST = 'www.agc.gov.my'
const URL_A = `https://${HOST}/agcportal/uploads/files/Publications/LOM/EN/Act%20709.pdf`
const URL_B = `http://${HOST}/agcportal/uploads/files/Publications/LOM/EN/Act%20563.pdf`

// One baseline law per skip code, each carrying the server's own reason.
const SKIPS: BaselineSkip[] = [
  ['not_allowed_host', `Its address (${HOST}) is not on this Economy's list of official hosts, so it was not fetched.`],
  ['unreachable', `The host could not be reached or did not send the law (${HOST}).`],
  ['no_law_text', 'The page has no law text that can be read.'],
  ['over_cap', 'Not fetched: Discovery stopped at its limit of 12 Documents.'],
  ['no_url', 'The baseline gives no address for this law.'],
  ['robots', "The host's robots.txt asks crawlers not to fetch it, so it was not fetched."],
  ['duplicate', 'The same file is already in the Corpus under another address.'],
  ['not_added', 'Fetched, but it could not be added to the Corpus.'],
  ['redirected_off', 'Its address redirected off the official hosts, so the redirect was not followed.'],
  ['time_limit', 'Not fetched: Discovery reached its time limit of 240 s for baseline laws.'],
  ['attempt_limit', 'Not fetched: Discovery had already tried 30 addresses, its limit.'],
  ['no_title_match', "The Portal's title search has no law under exactly this title, and the baseline gives no official address for it."],
  ['wrong_law', 'The baseline link points to a different law, so what it fetched was not kept.'],
].map(([code, reason], i) => ({
  law: `Law number ${i + 1}`,
  indicators: ['7.1'],
  urls: [`https://${HOST}/law-${i + 1}.pdf`],
  code,
  reason,
}))

const NOTES = [
  'the time limit was reached, so files the Portal crawler fetched were not read into the Corpus',
  'no crawl seed is tagged for Pillar 7, so the Portal crawler did not run',
  'the limit of 12 Document(s) was reached by baseline laws, so the Portal crawler did not run',
  `${HOST} was asked over plain http, which its Portal entry allows for this host: no TLS protected the text in transit`,
  'the Portal crawler failed: ConnectTimeout: timed out reading robots.txt',
  'no Document was fetched for Malaysia: upload the laws by hand with "Add document"',
]

function state(overrides: Partial<DiscoveryViewState> = {}): DiscoveryViewState {
  return {
    ...emptyDiscoveryView,
    portal: {
      run_id: 'disc_1',
      economy: 'MY',
      name: 'Malaysia',
      hosts: [HOST],
      strategy: 'curl_cffi_ladder',
      refresh: false,
    },
    status: 'finished',
    started_at: '2026-09-30T02:00:00+00:00',
    ended_at: '2026-09-30T02:04:00+00:00',
    counts: { found: 2, fetched: 1, added: 1, skipped: 1, scans: 0 },
    documents: [
      {
        url: URL_A,
        title: 'Personal Data Protection Act 2010',
        state: 'added',
        document_id: 'my_pdpa',
        n_pages: 88,
        size_bytes: 1000,
        ocr_applied: false,
        code: null,
        reason: null,
        found_by: 'baseline 7.1',
        indicators: ['7.1'],
      },
      {
        url: URL_B,
        title: null,
        state: 'skipped',
        document_id: null,
        n_pages: null,
        size_bytes: null,
        ocr_applied: false,
        code: 'robots',
        reason: "The Portal's robots.txt asks crawlers not to fetch it, so it was not fetched.",
        found_by: 'portal crawler',
        indicators: [],
      },
    ],
    drawn: { pillar: 7, indicators: ['7.1', '7.2'], max_documents: 12, baseline_skipped: SKIPS, notes: NOTES },
    ...overrides,
  }
}

/** The page's visible words: the markup with its tags taken out. */
function text(html: string): string {
  return html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&gt;/g, '>').replace(/\s+/g, ' ')
}

/** The words of a whole block, up to its closing tag. */
function block(html: string, attr: string): string {
  const m = new RegExp(`${attr}[^>]*>([\\s\\S]*?)</div>`).exec(html)
  return m ? text(m[1]).trim() : ''
}

function between(html: string, attr: string): string {
  const m = new RegExp(`${attr}[^>]*>([\\s\\S]*?)</`).exec(html)
  return m ? text(m[1]).trim() : ''
}

const WORKINGS = [
  /baseline/i, /crawler/i, /robots/i, /\bcap\b/i, /at most/i, /\bseed/i, /time limit/i, /http/i, /ConnectTimeout/, /TLS/,
  /found by/i, /s apart/, /one at a time/, /judge/i, /official source list/i, /seconds?\b/i, /\bUTC\b/, /Elapsed/, /Took/,
  /\b\d+ ?(ms|s|min|h)\b/, /\bpages?\b/i, /page images/i,
]

/** Renders as a screen that asks for reduced motion would: the end state. */
function reducedMotion() {
  vi.stubGlobal('window', { matchMedia: () => ({ matches: true }) })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('DiscoveryView masks how Discovery works', () => {
  it('says what it found, and never the workings, anywhere on the view', () => {
    const html = renderToStaticMarkup(<DiscoveryView state={state()} />)
    const page = text(html)
    for (const word of WORKINGS) expect(page).not.toMatch(word)
    for (const s of SKIPS) expect(page).not.toContain(s.reason)
    expect(page).toContain('Laws not found')
    expect(page.match(/Add this law with Add document\./g)).toHaveLength(SKIPS.length)
    expect(page).toContain('No laws could be fetched for this search. Add them with Add document.')
    // A skipped Document is listed as not added, with no reason.
    expect(page).toContain('Not added')
  })

  it('keeps the host out of the headline and the lead', () => {
    const html = renderToStaticMarkup(<DiscoveryView state={state()} />)
    const title = between(html, 'data-testid="discovery-meta"')
    const lead = between(html, 'data-testid="discovery-lead"')
    expect(title).toBe('Search complete for Malaysia (Pillar 7).')
    expect(lead).toBe('Discovery searches the official legal portals and adds the laws it finds to the Corpus.')
    for (const part of [title, lead, between(html, 'id="dv-title"')]) expect(part).not.toContain(HOST)
  })

  it('while it runs, says it is searching', () => {
    const html = renderToStaticMarkup(
      <DiscoveryView state={state({ status: 'running', ended_at: null, documents: [], drawn: null })} />,
    )
    expect(text(html)).toContain('Accessing the official legal portals for Malaysia . . .')
    expect(html).toContain('data-testid="discovery-sweep"')
    expect(html).toContain('data-testid="discovery-log"')
    expect(html).not.toContain('Elapsed')
    expect(html).not.toContain('What Discovery does')
    for (const word of WORKINGS) expect(text(html)).not.toMatch(word)
  })

  it('with nothing left over, says every law was found', () => {
    const html = renderToStaticMarkup(
      <DiscoveryView
        state={state({ drawn: { pillar: 7, indicators: null, max_documents: 12, baseline_skipped: [], notes: NOTES.slice(0, 5) } })}
      />,
    )
    const page = text(html)
    expect(page).toContain('Search complete for Malaysia (Pillar 7).')
    expect(page).toContain('Every law for this search was found or is already in the Corpus.')
    expect(page).not.toContain('No laws could be fetched')
    for (const word of WORKINGS) expect(page).not.toMatch(word)
  })

  it('a failed Discovery shows the plain sentence, not the exception', () => {
    const html = renderToStaticMarkup(
      <DiscoveryView
        state={state({ status: 'failed', documents: [], drawn: null, failure: `ConnectTimeout: https://${HOST}/robots.txt timed out` })}
      />,
    )
    const page = text(html)
    expect(page).toContain('Discovery stopped.')
    expect(page).toContain('Search stopped for Malaysia.')
    for (const word of WORKINGS) expect(page).not.toMatch(word)
  })
})

// Indonesia, Pillar 7, as Discovery streams it: three laws, the first also
// cited for a second Indicator and so reported again as already held.
const ID_HOST = 'peraturan.bpk.go.id'
const LAW = (n: number) => `https://${ID_HOST}/Details/${n}`
const TITLES = [
  'Law No.27 on Personal Data Protection 2022',
  'Law No.11 on Information and Electronic Transactions 2008',
  'Regulation No.71 on the Provision of Electronic System and Transaction 2019',
]

function indonesia(): DiscoveryEvent[] {
  const drafts = [
    { type: 'discovery_portal', run_id: 'disc_id', economy: 'ID', name: 'Indonesia', hosts: [ID_HOST], strategy: 'x', refresh: false },
    { type: 'discovery_found', url: LAW(1), name: TITLES[0] },
    { type: 'discovery_added', url: LAW(1), document_id: 'id_1', title: TITLES[0], n_pages: 30, ocr_applied: false },
    { type: 'discovery_skipped', url: LAW(1), code: 'in_corpus', reason: 'Already in the Corpus, so it was not asked for again.', title: TITLES[0] },
    { type: 'discovery_found', url: LAW(2), name: TITLES[1] },
    { type: 'discovery_added', url: LAW(2), document_id: 'id_2', title: TITLES[1], n_pages: 20, ocr_applied: false },
    { type: 'discovery_found', url: LAW(3), name: TITLES[2] },
    { type: 'discovery_added', url: LAW(3), document_id: 'id_3', title: TITLES[2], n_pages: 60, ocr_applied: false },
    {
      type: 'discovery_finished',
      counts: {
        run_id: 'disc_id', found: 3, fetched: 3, added: 3, skipped: 1, already_in_corpus: 1,
        pillar: 7, indicators: ['7.1', '7.2', '7.3', '7.4', '7.5'], max_documents: 12,
        found_by: [
          { url: LAW(1), document_id: 'id_1', title: TITLES[0], found_by: 'baseline 7.1, 7.3', status: 'fetched' },
          { url: LAW(2), document_id: 'id_2', title: TITLES[1], found_by: 'official source list 7.2', status: 'fetched' },
          { url: LAW(3), document_id: 'id_3', title: TITLES[2], found_by: 'portal crawler', status: 'fetched' },
          { url: LAW(1), document_id: 'id_1', title: TITLES[0], found_by: 'baseline 7.4', status: 'already in the Corpus' },
        ],
        baseline_skipped: [],
        notes: [],
      },
    },
  ]
  return drafts.map((d, i) => ({ ...d, seq: i, ts: `2026-10-15T03:00:${String(i).padStart(2, '0')}+00:00` }) as DiscoveryEvent)
}

function after(events: DiscoveryEvent[]): DiscoveryViewState {
  return events.reduce((st, event) => discoveryViewReducer(st, { type: 'event', event }), emptyDiscoveryView)
}

function lines(html: string): string[] {
  return [...html.matchAll(/data-testid="discovery-log-line"[^>]*>([\s\S]*?)<\/div>/g)].map((m) => text(m[1]).trim())
}

describe('DiscoveryView while it runs', () => {
  it('writes each law into the log as it is found and added', () => {
    const html = renderToStaticMarkup(<DiscoveryView state={after(indonesia().slice(0, 6))} />)
    expect(lines(html)).toEqual([
      '> Accessing the official legal portals for Indonesia…',
      `found ${TITLES[0]} ${ID_HOST}`,
      'added to the Corpus Added 1',
      `found ${TITLES[1]} ${ID_HOST}`,
      'added to the Corpus Added 2',
    ])
    expect(text(/data-testid="discovery-log-tally"[^>]*>(.*?)<\/span><\/div>/.exec(html)![1]).trim()).toBe('Found 2 Added 2')
    expect(html.match(/class="dv-cursor"/g)).toHaveLength(1)
    expect(between(html, 'data-testid="discovery-added"')).toBe('2')
    expect(between(html, 'data-testid="discovery-not-added"')).toBe('0')
    expect(html).not.toContain('data-testid="discovery-filter"')
  })
})

describe('DiscoveryView once it has finished', () => {
  it('places each law against the Pillar 7 Indicators, with tallies and a summary', () => {
    reducedMotion()
    const html = renderToStaticMarkup(<DiscoveryView state={after(indonesia())} />)
    const page = text(html)
    expect(page).toContain('Filter by Pillar 7')
    expect(between(html, 'data-testid="discovery-meta"')).toBe('Search complete for Indonesia (Pillar 7).')
    const columns = [...html.matchAll(/data-testid="discovery-column"[^>]*>([\s\S]*?)<\/span><\/span>/g)].map((m) => text(m[1]).trim())
    expect(columns).toEqual(['7.1 1 law', '7.2 1 law', '7.3 1 law', '7.4 1 law', '7.5 0 laws'])
    const rows = [...html.matchAll(/data-testid="discovery-filter-row"[\s\S]*?<\/li>/g)].map((m) => m[0])
    expect(rows).toHaveLength(3)
    const chips = (row: string) => [...row.matchAll(/data-testid="discovery-chip"[^>]*>([^<]*)</g)].map((m) => m[1])
    expect(chips(rows[0])).toEqual(['7.1', '7.3', '7.4'])
    expect(chips(rows[1])).toEqual(['7.2'])
    expect(chips(rows[2])).toEqual([])
    expect(text(rows[2])).toContain('Found for Pillar 7')
    expect(rows.every((r) => text(r).includes('Added'))).toBe(true)
    expect(block(html, 'data-testid="discovery-filter-summary"')).toBe(
      '3 laws found for Pillar 7, covering Indicators 7.1, 7.2, 7.3, 7.4.',
    )
    // The second ask for law 1 never shows as not added.
    expect(between(html, 'data-testid="discovery-added"')).toBe('3')
    expect(between(html, 'data-testid="discovery-not-added"')).toBe('0')
    expect(page).not.toContain('Discovery log')
    for (const word of WORKINGS) expect(page).not.toMatch(word)
  })

  it('without reduced motion, starts the reveal from nothing placed', () => {
    const html = renderToStaticMarkup(<DiscoveryView state={after(indonesia())} />)
    expect(html).not.toContain('data-testid="discovery-chip"')
    expect(html.match(/data-lit="false"/g)).toHaveLength(3)
    expect(block(html, 'data-testid="discovery-filter-summary"')).toBe('Placing each law against the Pillar 7 Indicators…')
  })

  it('shows a Discovery with no Pillar as a plain list of Documents', () => {
    const plain = after(indonesia().slice(0, 8).concat({
      type: 'discovery_finished', seq: 8, ts: 'x', counts: { found: 3, fetched: 3, added: 3, skipped: 0 },
    } as DiscoveryEvent))
    const html = renderToStaticMarkup(<DiscoveryView state={plain} />)
    expect(html).not.toContain('Filter by Pillar')
    expect(html.match(/data-testid="discovery-document"/g)).toHaveLength(3)
  })

  it('shows a second Discovery that fetches nothing new as already in the Corpus', () => {
    reducedMotion()
    const again = (drafts: object[]) =>
      after(drafts.map((d, i) => ({ ...d, seq: i, ts: `2026-10-15T04:00:${String(i).padStart(2, '0')}+00:00` }) as DiscoveryEvent))
    const IN_CORPUS = 'Already in the Corpus, so it was not asked for again.'
    const drafts = [
      { type: 'discovery_portal', run_id: 'disc_id2', economy: 'ID', name: 'Indonesia', hosts: [ID_HOST], strategy: 'x', refresh: false },
      ...TITLES.flatMap((title, i) => [
        { type: 'discovery_found', url: LAW(i + 1), name: title },
        { type: 'discovery_skipped', url: LAW(i + 1), code: 'in_corpus', reason: IN_CORPUS, title },
      ]),
    ]
    const finished = { found: 3, fetched: 0, added: 0, skipped: 3, already_in_corpus: 3, baseline_skipped: [], notes: [] }
    for (const [pillar, rowId] of [[7, 'discovery-filter-row'], [null, 'discovery-document']] as const) {
      const html = renderToStaticMarkup(
        <DiscoveryView
          state={again([...drafts, { type: 'discovery_finished', counts: pillar ? { ...finished, pillar, indicators: null } : finished }])}
        />,
      )
      const rows = [...html.matchAll(new RegExp(`data-testid="${rowId}"[\\s\\S]*?</li>`, 'g'))].map((m) => text(m[0]))
      expect(rows).toHaveLength(3)
      for (const r of rows) {
        expect(r).toContain('In the Corpus')
        expect(r).not.toContain('Not added')
      }
      expect(between(html, 'data-testid="discovery-found"')).toBe('3')
      expect(between(html, 'data-testid="discovery-added"')).toBe('0')
      expect(between(html, 'data-testid="discovery-not-added"')).toBe('0')
      for (const word of WORKINGS) expect(text(html)).not.toMatch(word)
    }
  })

  it('keeps every law with no Indicator id as found for the Pillar', () => {
    reducedMotion()
    const html = renderToStaticMarkup(
      <DiscoveryView state={state({ drawn: { pillar: 7, indicators: null, max_documents: 12, baseline_skipped: [], notes: [] }, documents: state().documents.map((d) => ({ ...d, indicators: [] })) })} />,
    )
    const page = text(html)
    expect(page).toContain('Each law found for Pillar 7.')
    expect(page.match(/Found for Pillar 7/g)).toHaveLength(2)
    expect(block(html, 'data-testid="discovery-filter-summary"')).toBe('2 laws found for Pillar 7.')
    for (const word of WORKINGS) expect(page).not.toMatch(word)
  })
})
