import type { ConfidencePart } from './types'

export interface BreakdownRow {
  signal: ConfidencePart['signal']
  label: string
  /** The signal's input in plain words, e.g. "180 characters". */
  value: string
  /** The signal's weight as a percentage, e.g. "45%". */
  weight: string
  /** What the signal added to the Confidence, e.g. "0.3038". */
  contribution: string
  /** How the signal is scored and why it counts, in plain words. */
  why: string
}

export interface Breakdown {
  rows: BreakdownRow[]
  /** The contributions added up, before rounding. */
  sum: string
  /** The Confidence the parts round to, as the server computed it. */
  total: string
}

const LABELS: Record<ConfidencePart['signal'], string> = {
  similarity: 'Meaning match',
  quote_length: 'Quote length',
  specificity: 'Specificity',
  attempts: 'Proof attempts',
}

// How each signal is scored and why it counts. Kept true to the server's
// formula (export.confidence_parts): the cosine is rescaled over 0.35 to 0.75,
// quote length saturates at 240 characters, each extra Indicator costs 0.15
// down to a floor of 0.4, and attempts score 1, 0.6, then 0.3.
const WHY: Record<ConfidencePart['signal'], string> = {
  similarity:
    "Closeness in meaning to the Indicator's description: 0.35 or less scores nothing, 0.75 or more scores full. Weighs most, as the most direct sign the law is about this Indicator.",
  quote_length:
    'Full marks at 240 characters. A fuller quote carries more context and is harder to match by accident.',
  specificity:
    'Each extra Indicator mapped from the same Piece takes off 15%, down to 40%. A passage that matches many Indicators is likely general wording.',
  attempts:
    'Tries until the quote was found word for word in the source: full marks on the first, 60% on the second, 30% on the third. Needing retries means a less certain match.',
}

function rawInWords(part: ConfidencePart): string {
  switch (part.signal) {
    case 'similarity':
      return `cosine ${part.raw.toFixed(2)}`
    case 'quote_length':
      return part.raw === 1 ? '1 character' : `${part.raw} characters`
    case 'specificity':
      return part.raw <= 1 ? 'this Piece maps to 1 Indicator' : `this Piece maps to ${part.raw} Indicators`
    case 'attempts':
      return part.raw <= 1 ? 'passed on the first attempt' : `passed on attempt ${part.raw}`
  }
}

/** The four signals behind a Confidence, as the "How this is scored" panel
 *  lists them. Pure: the numbers are the server's, only the words are added. */
export function breakdownRows(parts: ConfidencePart[], confidence: number): Breakdown {
  const sum = parts.reduce((acc, p) => acc + p.contribution, 0)
  const shown = shownContributions(parts.map((p) => p.contribution), sum)
  return {
    rows: parts.map((p, i) => ({
      signal: p.signal,
      label: LABELS[p.signal] ?? p.label,
      value: rawInWords(p),
      weight: `${Math.round(p.weight * 100)}%`,
      contribution: (shown[i] / SCALE).toFixed(DECIMALS),
      why: WHY[p.signal] ?? '',
    })),
    sum: (Math.round(sum * SCALE) / SCALE).toFixed(DECIMALS),
    total: confidence.toFixed(2),
  }
}

const DECIMALS = 4
const SCALE = 10 ** DECIMALS

// Each contribution to 4 decimals, rounded so the shown rows add up exactly to
// the shown sum: rounding each row on its own can leave the column 0.0001 or
// 0.0002 off, which a reader adding it up would rightly question. Every row
// starts rounded down; the units still missing go to the rows that lost the
// most (largest remainder), so no row moves by more than one unit.
function shownContributions(contributions: number[], sum: number): number[] {
  const target = Math.round(sum * SCALE)
  const scaled = contributions.map((c) => c * SCALE)
  const out = scaled.map((x) => Math.floor(x + 1e-9))
  let missing = target - out.reduce((a, b) => a + b, 0)
  const order = scaled
    .map((x, i) => ({ i, rest: x - out[i] }))
    .sort((a, b) => b.rest - a.rest || a.i - b.i)
  for (let k = 0; missing > 0 && k < order.length; k++, missing--) out[order[k].i] += 1
  return out
}
