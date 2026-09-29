import { describe, expect, it } from 'vitest'
import { PANE_FAILED, SOURCE_NOTE, isCancelledRender, paneFor } from './sourcePane'

describe('which pane shows the source', () => {
  it('draws a PDF as pages', () => {
    expect(paneFor('pdf')).toBe('pdf')
  })

  it('shows the text of anything that is not a PDF', () => {
    expect(paneFor('html')).toBe('text')
    expect(paneFor('other')).toBe('text')
    expect(paneFor('missing')).toBe('text')
  })

  it('waits until it knows, rather than feeding a web page to the PDF viewer', () => {
    expect(paneFor(undefined)).toBe('loading')
  })

  it('says what a non-PDF source is in a plain sentence', () => {
    expect(SOURCE_NOTE.html).toMatch(/web page/)
    for (const note of [...Object.values(SOURCE_NOTE), PANE_FAILED]) {
      expect(note).not.toMatch(/Exception|Error|undefined/)
      expect(note).not.toMatch(/—/)
    }
  })
})

describe('a page render that was called off', () => {
  it('is not a failure to show', () => {
    const e = Object.assign(new Error('Rendering cancelled, page 3'), {
      name: 'RenderingCancelledException',
    })
    expect(isCancelledRender(e)).toBe(true)
  })

  it('a real failure still is', () => {
    expect(isCancelledRender(new Error('Invalid PDF structure.'))).toBe(false)
    expect(isCancelledRender('nope')).toBe(false)
  })
})
