import { RUN_STATUS_WORD } from './runSetup'
import type { RunRecord } from './types'

/** How long the Run took, which is what a reader actually wants from the two
 *  timestamps. The Comparison cards already show this; the list did not, and
 *  an end time is only useful for subtracting anyway. An add that answered
 *  from what was already stored takes hundredths of a second, which "0.0 s"
 *  read as a broken clock; it says "under 0.1 s" instead. */
export function durationText(started: string | null, ended: string | null): string {
  if (!started || !ended) return '-'
  const a = new Date(started).getTime()
  const b = new Date(ended).getTime()
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return '-'
  const s = (b - a) / 1000
  if (s < 0.1) return 'under 0.1 s'
  if (s < 60) return `${s.toFixed(1)} s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m} min ${Math.round(s % 60)} s` : `${Math.floor(m / 60)} h ${m % 60} min`
}

/** The status in words. An add the server turned away (these bytes are
 *  already here, a file it cannot read, a host off the whitelist) did its job
 *  by refusing, so it reads "Refused", not "Stopped". */
export function statusWord(r: RunRecord): string {
  if (r.status === 'failed' && r.details?.manual && r.details?.outcome === 'refused') return 'Refused'
  return RUN_STATUS_WORD[r.status] ?? r.status
}
