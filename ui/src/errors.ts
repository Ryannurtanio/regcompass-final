// Every failure the screens show, in plain words. A reader sees one sentence
// saying what happened and what to do next; the raw text (an exception, a
// status code, a server path) stays behind "Show the technical message", the
// same disclosure the Run view uses when a Run stops.

/** A request the server answered with an error status. `detail` is the
 *  server's own `detail` field: a sentence, a structured object, or nothing. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`)
  }
}

/** One failure, ready for the screen. */
export interface PlainError {
  message: string
  // The raw text, for the disclosure; null when the sentence is all there is.
  technical: string | null
}

/** What was being done, so a failure can say what to try next. */
export type ErrorContext = 'generic' | 'fetch' | 'discovery'

const SERVER_FAILED =
  'The server could not finish this. Try again; if it keeps happening, the technical message says why.'
const NOT_FOUND =
  'That was not found on the server. It may have been cleared; reload the page and try again.'
const UNREACHABLE = 'The server could not be reached. Check the connection, then try again.'
const GENERIC = 'Something went wrong and this could not be finished. Try again.'

const FETCH_BLOCKED =
  'The site did not let us fetch this page. Download the file and use Upload instead.'
const FETCH_TIMEOUT =
  'The site did not answer in time. Try again in a minute, or download the file and use Upload instead.'
const FETCH_FORBIDDEN =
  "The site's rules do not allow fetching this page automatically. Download the file and use Upload instead."
const FETCH_UNREADABLE =
  'The file was fetched, but its text could not be read. Upload a copy that has selectable text.'
const FETCH_OTHER =
  'The Document could not be added. Try again, or download the file and use Upload instead.'

const PORTAL_BLOCKED =
  'The Portal did not let us fetch its pages. Try again later, or add the Documents one by one with Add document.'
const PORTAL_TIMEOUT =
  'The Portal did not answer in time. Try again in a few minutes.'
const PORTAL_FORBIDDEN =
  "The Portal's rules do not allow collecting its pages automatically. Add the Documents one by one with Add document."
const PORTAL_UNREADABLE =
  'The Portal answered, but its list of laws could not be read the way Discovery expects. Try again later, or add the Documents one by one with Add document.'
const PORTAL_OTHER =
  'Something went wrong while reading the Portal.'

type Kind = 'blocked' | 'timeout' | 'forbidden' | 'unreadable' | 'other'

const SENTENCES: Record<ErrorContext, Record<Kind, string>> = {
  generic: {
    blocked: FETCH_BLOCKED,
    timeout: FETCH_TIMEOUT,
    forbidden: FETCH_FORBIDDEN,
    unreadable: FETCH_UNREADABLE,
    other: GENERIC,
  },
  fetch: {
    blocked: FETCH_BLOCKED,
    timeout: FETCH_TIMEOUT,
    forbidden: FETCH_FORBIDDEN,
    unreadable: FETCH_UNREADABLE,
    other: FETCH_OTHER,
  },
  discovery: {
    blocked: PORTAL_BLOCKED,
    timeout: PORTAL_TIMEOUT,
    forbidden: PORTAL_FORBIDDEN,
    unreadable: PORTAL_UNREADABLE,
    other: PORTAL_OTHER,
  },
}

/** Which kind of failure an exception class names. */
function kindOf(className: string): Kind {
  if (/Timeout|TimedOut/i.test(className)) return 'timeout'
  if (/Robots/.test(className)) return 'forbidden'
  if (/Ladder|FetchFailed|HTTPStatus|Connect|SSL|Remote|Network|Protocol/.test(className)) return 'blocked'
  if (/Extraction|Grid|Discovery|Parse|Decode/.test(className)) return 'unreadable'
  return 'other'
}

// "LadderExhaustedError: ...", "httpx.ReadTimeout: ...", "KeyError: 'x'".
const CLASS_PREFIX = /^([A-Za-z_][\w.]*?(Error|Exception|Timeout|Warning|Interrupt|Exit))\s*:\s*/
// An absolute path on the server, never a URL: "https://a/b" is left alone.
const SERVER_PATH = /(^|[\s('"])\/(?!\/)[\w.-]+\/[\w./-]+/
const BARE_STATUS = /\bHTTP \d{3}\b/

/** A failure's raw text, as a sentence. An exception class name is read for
 *  what kind of failure it was and never shown; a server sentence that is
 *  already plain passes through untouched. */
export function plainFailure(raw: string, context: ErrorContext = 'generic'): PlainError {
  const text = raw.replace(/^Error:\s*/, '').trim()
  const cls = CLASS_PREFIX.exec(text)
  if (cls) {
    const name = cls[1].split('.').pop() ?? cls[1]
    return { message: SENTENCES[context][kindOf(name)], technical: text }
  }
  if (!text || SERVER_PATH.test(text) || BARE_STATUS.test(text) || /Traceback/.test(text)) {
    return { message: context === 'generic' ? GENERIC : SENTENCES[context].other, technical: text || null }
  }
  return { message: text, technical: null }
}

/** Any caught value, as a sentence for the screen. */
export function plainError(e: unknown, context: ErrorContext = 'generic'): PlainError {
  if (e instanceof ApiError) {
    const d = e.detail
    if (typeof d === 'string' && d.trim()) return plainFailure(d, context)
    if (d && typeof d === 'object' && typeof (d as { message?: unknown }).message === 'string') {
      return plainFailure((d as { message: string }).message, context)
    }
    const technical = `HTTP ${e.status}`
    if (e.status === 404) return { message: NOT_FOUND, technical }
    if (e.status >= 500) return { message: SERVER_FAILED, technical }
    return { message: GENERIC, technical }
  }
  // fetch() rejects with a TypeError when the server cannot be reached at all.
  if (e instanceof TypeError && /fetch|network|load failed/i.test(e.message)) {
    return { message: UNREACHABLE, technical: e.message }
  }
  if (e instanceof Error) return plainFailure(e.message, context)
  return plainFailure(String(e ?? ''), context)
}

/** An Evidence Export that was refused. */
export interface ExportRefusal extends PlainError {
  // Each thing to fix, with every Document named by its title.
  reasons: string[]
}

/** Why the Export did not write the workbook, with Documents named by title
 *  rather than by the id the server's checks use. */
export function exportRefusal(e: unknown, titles: Record<string, string>): ExportRefusal {
  const d = e instanceof ApiError ? e.detail : null
  const failures =
    d && typeof d === 'object' && Array.isArray((d as { gate_failures?: unknown }).gate_failures)
      ? ((d as { gate_failures: unknown[] }).gate_failures).map(String)
      : null
  if (failures) {
    const reasons = failures.map((f) => withTitles(f, titles))
    return {
      message:
        failures.length === 1
          ? 'The workbook was not written. One thing must be fixed first:'
          : `The workbook was not written. ${failures.length} things must be fixed first:`,
      technical: null,
      reasons,
    }
  }
  return { ...plainError(e), reasons: [] }
}

// The characters a Document id is made of. An id only counts as named where
// the text around it is not more of an id, so doc_my_ACT_593 never matches
// inside doc_my_ACT_593_(2).
const ID_CHAR = '[A-Za-z0-9_()-]'

/** The text with each Document id, as a whole id, replaced by its title.
 *  Longer ids go first, and one pass does them all, so a title that happens
 *  to contain another id is never rewritten again. */
export function withTitles(text: string, titles: Record<string, string>): string {
  const ids = Object.keys(titles)
    .filter((id) => id && titles[id])
    .sort((a, b) => b.length - a.length)
  if (ids.length === 0) return text
  const escaped = ids.map((id) => id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const pattern = new RegExp(`(?<!${ID_CHAR})(?:${escaped.join('|')})(?!${ID_CHAR})`, 'g')
  return text.replace(pattern, (id) => titles[id] ?? id)
}
