import { describe, expect, it } from 'vitest'
import { checkCorrection, correctedBy, correctionGroups, overrideLine, REASON_MAX } from './correction'

const choices = [
  { id: '6.1', title: 'Ban and local processing requirements', pillar: 6 },
  { id: '6.4', title: 'Conditional flow regimes', pillar: 6 },
  { id: '7.1', title: 'Lack of comprehensive legal framework for data protection', pillar: 7 },
]

describe('the picker offers the Run Indicators grouped by Pillar', () => {
  it('groups by Pillar and reads "6.1 title"', () => {
    const groups = correctionGroups(choices, '7.3')
    expect(groups.map((g) => g.label)).toEqual(['Pillar 6', 'Pillar 7'])
    expect(groups[0].options).toEqual([
      { value: '6.1', label: '6.1 Ban and local processing requirements' },
      { value: '6.4', label: '6.4 Conditional flow regimes' },
    ])
  })

  it('never offers the Mapping own Indicator', () => {
    const groups = correctionGroups(choices, '6.4')
    expect(groups.flatMap((g) => g.options.map((o) => o.value))).toEqual(['6.1', '7.1'])
  })

  it('drops a Pillar left with nothing to offer', () => {
    const groups = correctionGroups(choices.slice(2), '7.1')
    expect(groups).toEqual([])
  })
})

describe('Save correction waits for an Indicator and a reason', () => {
  it('needs both', () => {
    expect(checkCorrection('', 'a reason').canSave).toBe(false)
    expect(checkCorrection('6.1', '').canSave).toBe(false)
    expect(checkCorrection('6.1', '   ').canSave).toBe(false)
    expect(checkCorrection('6.1', 'a reason').canSave).toBe(true)
  })

  it('counts to 300 and refuses more', () => {
    expect(checkCorrection('6.1', 'abc').counter).toBe('3 / 300')
    const full = checkCorrection('6.1', 'x'.repeat(REASON_MAX))
    expect(full).toEqual({ canSave: true, counter: '300 / 300', over: false })
    const over = checkCorrection('6.1', 'x'.repeat(REASON_MAX + 1))
    expect(over.canSave).toBe(false)
    expect(over.over).toBe(true)
  })

  it('counts what the server keeps, without the outer spaces', () => {
    expect(checkCorrection('6.1', '  ab  ').counter).toBe('2 / 300')
  })
})

describe('the override line', () => {
  it('names both Indicators with their titles', () => {
    expect(
      overrideLine(
        { id: '6.4', title: 'Conditional flow regimes' },
        { id: '6.1', title: 'Ban and local processing requirements' },
      ),
    ).toBe(
      'Engine proposed 6.4 (Conditional flow regimes), reviewer corrected to 6.1 (Ban and local processing requirements)',
    )
  })

  it('still reads without a title', () => {
    expect(overrideLine({ id: '6.4', title: '' }, { id: '6.1', title: null })).toBe(
      'Engine proposed 6.4, reviewer corrected to 6.1',
    )
  })

  it('says who and when, in UTC', () => {
    expect(correctedBy('Ryan', '2026-09-29T14:05:09+00:00')).toBe('By Ryan, 29 Sep 2026 14:05 UTC')
    expect(correctedBy(null, '2026-09-29T14:05:09+00:00')).toBe(
      'By an unnamed reviewer, 29 Sep 2026 14:05 UTC',
    )
    expect(correctedBy('  ', null)).toBe('By an unnamed reviewer')
  })
})
