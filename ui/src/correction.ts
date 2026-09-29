// Correct: a reviewer's override of the Indicator an Engine chose. The pure
// part of the picker and of the line that states the override, kept out of the
// component so it can be tested without a browser.
import type { IndicatorChoice } from './types'

/** The most a correction's reason may say (the server refuses more). */
export const REASON_MAX = 300

export interface ChoiceOption {
  value: string
  label: string
}

export interface ChoiceGroup {
  pillar: number
  label: string
  options: ChoiceOption[]
}

/** The picker's options, one group per Pillar in the order the server sent
 *  them, each option reading "6.1 title". The Mapping's own Indicator is left
 *  out even if it was sent: a correction always changes something. */
export function correctionGroups(choices: IndicatorChoice[], ownId: string): ChoiceGroup[] {
  const groups: ChoiceGroup[] = []
  for (const c of choices) {
    if (c.id === ownId) continue
    let group = groups.find((g) => g.pillar === c.pillar)
    if (!group) {
      group = { pillar: c.pillar, label: `Pillar ${c.pillar}`, options: [] }
      groups.push(group)
    }
    group.options.push({ value: c.id, label: `${c.id} ${c.title}` })
  }
  return groups
}

export interface CorrectionCheck {
  /** Save correction is on only when this is true. */
  canSave: boolean
  /** The live counter under the reason, "n / 300". */
  counter: string
  over: boolean
}

/** Whether the picker can save: an Indicator is chosen and the reason, with
 *  its outer spaces trimmed as the server trims them, is 1 to 300 characters. */
export function checkCorrection(indicatorId: string, reason: string): CorrectionCheck {
  const n = reason.trim().length
  const over = n > REASON_MAX
  return {
    canSave: indicatorId !== '' && n >= 1 && !over,
    counter: `${n} / ${REASON_MAX}`,
    over,
  }
}

const titled = (id: string, title: string | null | undefined) => (title ? `${id} (${title})` : id)

/** The override in one line: what the Engine proposed and what the reviewer
 *  corrected it to, each with its title. */
export function overrideLine(
  original: { id: string; title: string },
  corrected: { id: string; title: string | null },
): string {
  return `Engine proposed ${titled(original.id, original.title)}, reviewer corrected to ${titled(
    corrected.id,
    corrected.title,
  )}`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** Who corrected it and when, in UTC so every reader sees the same time:
 *  "By Ryan, 29 Sep 2026 14:05 UTC". A decision with no name says so. */
export function correctedBy(reviewer: string | null, reviewedAt: string | null): string {
  const who = reviewer && reviewer.trim() ? reviewer.trim() : 'an unnamed reviewer'
  const t = reviewedAt ? new Date(reviewedAt) : null
  if (!t || Number.isNaN(t.getTime())) return `By ${who}`
  const pad = (n: number) => String(n).padStart(2, '0')
  const when = `${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]} ${t.getUTCFullYear()} ${pad(
    t.getUTCHours(),
  )}:${pad(t.getUTCMinutes())} UTC`
  return `By ${who}, ${when}`
}
