import { describe, expect, it } from 'vitest'
import { ApiError, exportRefusal, plainError, plainFailure, withTitles } from './errors'

// What a reader must never see in an error line: a server path, a status code
// on its own, an exception class name, or the "Error:" JavaScript puts in front.
function assertPlain(message: string) {
  expect(message).not.toMatch(/\/api\//)
  expect(message).not.toMatch(/HTTP \d{3}/)
  expect(message).not.toMatch(/^Error:/)
  expect(message).not.toMatch(/\b[A-Z]\w+(Error|Exception)\b/)
  expect(message).not.toMatch(/(^|\s)\/[\w.-]+\/[\w./-]+/)
}

describe('plainError', () => {
  it('shows the server detail when it is already a sentence', () => {
    const p = plainError(new ApiError(404, 'Only one Engine has run Australia Pillar 7. Run the other Engine first.'))
    expect(p.message).toBe('Only one Engine has run Australia Pillar 7. Run the other Engine first.')
    expect(p.technical).toBeNull()
  })

  it('a bare status with no detail becomes a plain sentence, the code behind the toggle', () => {
    const p = plainError(new ApiError(500, null))
    assertPlain(p.message)
    expect(p.message).toMatch(/server/i)
    expect(p.technical).toMatch(/500/)
  })

  it('a missing record is named as such', () => {
    const p = plainError(new ApiError(404, null))
    assertPlain(p.message)
    expect(p.message).toMatch(/not found|no longer/i)
  })

  it('never shows a server file path in the sentence', () => {
    const p = plainError(new ApiError(404, 'no database at /data/regcompass.db: start a Run first'))
    assertPlain(p.message)
    expect(p.technical).toContain('/data/regcompass.db')
  })

  it('an unreachable server says so', () => {
    const p = plainError(new TypeError('Failed to fetch'))
    assertPlain(p.message)
    expect(p.message).toMatch(/could not be reached/i)
    expect(p.technical).toBe('Failed to fetch')
  })

  it('an unknown failure is a generic sentence with the text behind the toggle', () => {
    const p = plainError(new Error('/api/records?sort=confidence: HTTP 500'))
    assertPlain(p.message)
    expect(p.technical).toBe('/api/records?sort=confidence: HTTP 500')
  })

  it('drops the "Error:" prefix a string conversion adds', () => {
    const p = plainError('Error: Something odd happened')
    expect(p.message).toBe('Something odd happened')
  })

  it('accepts a structured detail with a message', () => {
    const p = plainError(new ApiError(409, { message: 'A Run is already going.' }))
    expect(p.message).toBe('A Run is already going.')
  })
})

describe('plainFailure: exception class names from the server', () => {
  it('the Indonesia page that refused every way of fetching it', () => {
    const raw =
      'LadderExhaustedError: every rung failed for https://peraturan.go.id/x: Patchright headful (documented rung 3)'
    const p = plainFailure(raw, 'fetch')
    assertPlain(p.message)
    expect(p.message).toBe(
      'The site did not let us fetch this page. Download the file and use Upload instead.',
    )
    expect(p.technical).toBe(raw)
  })

  it('an add-by-URL 502 detail maps the same way through plainError', () => {
    const p = plainError(new ApiError(502, 'FetchFailedError: 403 Forbidden'), 'fetch')
    expect(p.message).toMatch(/did not let us fetch/)
    expect(p.technical).toBe('FetchFailedError: 403 Forbidden')
  })

  it('a timeout says the site did not answer', () => {
    const p = plainFailure('ReadTimeout: timed out', 'fetch')
    assertPlain(p.message)
    expect(p.message).toMatch(/did not answer/)
  })

  it('a file whose text cannot be read', () => {
    const p = plainFailure('ExtractionError: no text layer', 'fetch')
    assertPlain(p.message)
    expect(p.message).toMatch(/text could not be read/)
  })

  it('a Discovery failure speaks of the Portal', () => {
    const p = plainFailure('LaoGridError: the grid had no rows', 'discovery')
    assertPlain(p.message)
    expect(p.message).toMatch(/Portal/)
    expect(p.technical).toBe('LaoGridError: the grid had no rows')
  })

  it('an unknown class still gets a sentence, never its name', () => {
    const p = plainFailure('KeyError: document_id', 'discovery')
    assertPlain(p.message)
    expect(p.technical).toBe('KeyError: document_id')
  })

  it('a plain sentence from the server passes through', () => {
    const p = plainFailure('The Portal forbids automated collection for Thailand.', 'fetch')
    expect(p.message).toBe('The Portal forbids automated collection for Thailand.')
    expect(p.technical).toBeNull()
  })
})

describe('exportRefusal', () => {
  const titles = { 'doc_sg_1a2b': 'Telecommunications Act 1999' }

  it('names Documents by title, in plain words, with no gate jargon', () => {
    const r = exportRefusal(
      new ApiError(422, {
        gate_failures: [
          'doc_sg_1a2b: no Source URL recorded, so no shippable row can be built for it.',
        ],
      }),
      titles,
    )
    expect(r.message).not.toMatch(/gate/i)
    expect(r.message).toMatch(/not written/)
    expect(r.reasons).toEqual([
      'Telecommunications Act 1999: no Source URL recorded, so no shippable row can be built for it.',
    ])
  })

  it('a refusal with a sentence detail keeps the sentence', () => {
    const r = exportRefusal(new ApiError(409, 'That is a Discovery, not a Run.'), titles)
    expect(r.message).toBe('That is a Discovery, not a Run.')
    expect(r.reasons).toEqual([])
  })

  it('any other failure is plain too', () => {
    const r = exportRefusal(new ApiError(500, null), titles)
    assertPlain(r.message)
    expect(r.technical).toMatch(/500/)
  })
})

describe('withTitles', () => {
  const titles = {
    doc_my_ACT_593: 'Communications Act 1998',
    'doc_my_ACT_593_(2)': 'Communications (Amendment) Act 2004',
  }

  it('replaces whole ids only, so a prefix id never garbles a longer one', () => {
    expect(withTitles('doc_my_ACT_593_(2): no Source URL recorded', titles)).toBe(
      'Communications (Amendment) Act 2004: no Source URL recorded',
    )
    expect(withTitles('rows of doc_my_ACT_593 and doc_my_ACT_593_(2).', titles)).toBe(
      'rows of Communications Act 1998 and Communications (Amendment) Act 2004.',
    )
  })

  it('leaves an id that is only part of a longer, unknown id alone', () => {
    expect(withTitles('doc_my_ACT_593_extra failed', { doc_my_ACT_593: 'X' })).toBe(
      'doc_my_ACT_593_extra failed',
    )
  })
})
