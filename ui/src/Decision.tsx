import type { ReviewStatus } from './types'

/** A Review Decision as a shape AND a word, so it never rests on colour alone:
 *  accepted is a filled disc with a tick, rejected a ring with a cross,
 *  flagged an amber diamond with "!", and not reviewed a hollow grey ring. */
const WORDS: Record<ReviewStatus | 'unreviewed', string> = {
  accepted: 'Accepted',
  rejected: 'Rejected',
  flagged: 'Flagged',
  unreviewed: 'Not reviewed',
}

export function DecisionGlyph({ status }: { status: ReviewStatus | null }) {
  const s = status ?? 'unreviewed'
  return (
    <svg className={`ev-glyph ${s}`} width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
      {s === 'accepted' && (
        <>
          <circle cx="7" cy="7" r="6.25" fill="currentColor" />
          <path
            d="M4 7.2 L6.1 9.2 L10 4.9"
            fill="none"
            stroke="var(--surface)"
            strokeWidth="1.7"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </>
      )}
      {s === 'rejected' && (
        <>
          <circle cx="7" cy="7" r="5.75" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="M4.9 4.9 L9.1 9.1 M9.1 4.9 L4.9 9.1" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </>
      )}
      {s === 'flagged' && (
        <>
          <path d="M7 0.8 L13.2 7 L7 13.2 L0.8 7 Z" fill="currentColor" />
          <path d="M7 3.9 V7.8" stroke="var(--surface)" strokeWidth="1.6" strokeLinecap="round" />
          <circle cx="7" cy="10" r="0.95" fill="var(--surface)" />
        </>
      )}
      {s === 'unreviewed' && (
        <circle cx="7" cy="7" r="5.75" fill="none" stroke="currentColor" strokeWidth="1.5" />
      )}
    </svg>
  )
}

export default function Decision({
  status,
  testId,
}: {
  status: ReviewStatus | null
  testId?: string
}) {
  const s = status ?? 'unreviewed'
  return (
    <span className={`ev-decision ${s}`} data-testid={testId}>
      <DecisionGlyph status={status} />
      {WORDS[s]}
    </span>
  )
}
