import { describe, expect, it } from 'vitest'
import { documentSize, pageSpan, pageSuffix, textSize } from './sourceText'

describe('web pages have no pages', () => {
  it('says web page, not 1 page', () => {
    expect(documentSize('html', 1)).toBe('web page')
    expect(documentSize('pdf', 44)).toBe('44 pages')
    expect(documentSize(null, 1)).toBe('1 page')
    expect(documentSize('pdf', null)).toBeNull()
  })

  it('drops the page from a place on a web page', () => {
    expect(pageSuffix('html', 1)).toBe('')
    expect(pageSuffix('pdf', 3)).toBe(', page 3')
    expect(pageSpan('html', 1, 1)).toBeNull()
    expect(pageSpan('pdf', 2, 4)).toBe('pages 2 to 4')
    expect(pageSpan(undefined, 2, 2)).toBe('page 2')
  })
})

describe('textSize', () => {
  it('counts English words', () => {
    expect(textSize('No organisation shall process personal data.')).toBe('6 words')
  })

  it('counts Chinese by character, not by space', () => {
    expect(textSize('第一条 为了保护个人信息权益')).toBe('13 characters')
  })
})
