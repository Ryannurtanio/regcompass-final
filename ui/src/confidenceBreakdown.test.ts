import { describe, expect, it } from 'vitest'
import { breakdownRows } from './confidenceBreakdown'
import type { ConfidencePart } from './types'

// confidence_parts(0.62, 180, 2, 3) as the server returns it: 0.66.
const parts: ConfidencePart[] = [
  { signal: 'similarity', label: 'Meaning match', raw: 0.62, weight: 0.45, score: 0.675, contribution: 0.30375 },
  { signal: 'quote_length', label: 'Quote length', raw: 180, weight: 0.25, score: 0.75, contribution: 0.1875 },
  { signal: 'specificity', label: 'Specificity', raw: 2, weight: 0.15, score: 0.85, contribution: 0.1275 },
  { signal: 'attempts', label: 'Proof attempts', raw: 3, weight: 0.15, score: 0.3, contribution: 0.045 },
]

describe('how a Confidence is scored', () => {
  it('lists the four signals in order with their weights', () => {
    const b = breakdownRows(parts, 0.66)
    expect(b.rows.map((r) => r.label)).toEqual(['Meaning match', 'Quote length', 'Specificity', 'Proof attempts'])
    expect(b.rows.map((r) => r.weight)).toEqual(['45%', '25%', '15%', '15%'])
  })

  it('says each raw value in plain words', () => {
    const b = breakdownRows(parts, 0.66)
    expect(b.rows.map((r) => r.value)).toEqual([
      'cosine 0.62',
      '180 characters',
      'this Piece maps to 2 Indicators',
      'passed on attempt 3',
    ])
  })

  it('explains under each row how the signal is scored and why it counts', () => {
    const why = breakdownRows(parts, 0.66).rows.map((r) => r.why)
    expect(why[0]).toContain('0.35 or less scores nothing, 0.75 or more scores full')
    expect(why[0]).toContain('Weighs most')
    expect(why[1]).toContain('Full marks at 240 characters')
    expect(why[2]).toContain('takes off 15%, down to 40%')
    expect(why[3]).toContain('full marks on the first, 60% on the second, 30% on the third')
    for (const w of why) {
      expect(w.length).toBeGreaterThan(0)
      expect(w).not.toMatch(/\u2014/)
    }
  })

  it('reads a clean first-attempt single-Indicator row naturally', () => {
    const clean = parts.map((p) =>
      p.signal === 'specificity' || p.signal === 'attempts' ? { ...p, raw: 1, score: 1, contribution: 0.15 } : p,
    )
    const b = breakdownRows(clean, 0.79)
    expect(b.rows[2].value).toBe('this Piece maps to 1 Indicator')
    expect(b.rows[3].value).toBe('passed on the first attempt')
  })

  it('adds the contributions up to the shown Confidence', () => {
    const b = breakdownRows(parts, 0.66)
    expect(b.rows.map((r) => r.contribution)).toEqual(['0.3038', '0.1875', '0.1275', '0.0450'])
    expect(b.sum).toBe('0.6638')
    expect(b.total).toBe('0.66')
  })

  it('shows rows that add up to the shown sum, over many inputs', () => {
    // The server's formula (export.confidence_parts), mirrored for test inputs.
    const partsFor = (cosine: number, len: number, n: number, attempts: number): ConfidencePart[] => {
      const sim = Math.max(0, Math.min(1, (cosine - 0.35) / 0.4))
      const qlen = Math.min(1, len / 240)
      const multi = n <= 1 ? 1 : Math.max(0.4, 1 - 0.15 * (n - 1))
      const retry = attempts === 1 ? 1 : attempts === 2 ? 0.6 : 0.3
      return [
        { signal: 'similarity', label: '', raw: cosine, weight: 0.45, score: sim, contribution: 0.45 * sim },
        { signal: 'quote_length', label: '', raw: len, weight: 0.25, score: qlen, contribution: 0.25 * qlen },
        { signal: 'specificity', label: '', raw: n, weight: 0.15, score: multi, contribution: 0.15 * multi },
        { signal: 'attempts', label: '', raw: attempts, weight: 0.15, score: retry, contribution: 0.15 * retry },
      ]
    }
    let seed = 7
    const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648)
    for (let k = 0; k < 5000; k++) {
      const p = partsFor(rand(), Math.floor(rand() * 600), 1 + Math.floor(rand() * 8), 1 + Math.floor(rand() * 4))
      const exact = p.reduce((a, x) => a + x.contribution, 0)
      const b = breakdownRows(p, Math.round(exact * 100) / 100)
      const shownRows = b.rows.reduce((a, r) => a + Number(r.contribution), 0)
      expect(Math.abs(shownRows - Number(b.sum))).toBeLessThan(0.0001)
      expect(Math.abs(Number(b.sum) - exact)).toBeLessThanOrEqual(0.00005 + 1e-12)
      b.rows.forEach((r, i) => expect(Math.abs(Number(r.contribution) - p[i].contribution)).toBeLessThan(0.0001 + 1e-12))
    }
  })
})
