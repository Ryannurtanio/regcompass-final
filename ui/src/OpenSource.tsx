import type { SourceLink } from './types'

// Follow one Mapping row to where its quote actually lives. An official Portal
// address opens as it is (a PDF link carries #page=N, so the browser lands on
// the cited page); a Document with no Portal address recorded opens the copy
// this app is serving, and the label says so rather than implying an official
// source. An anchor, not a button: the browser's own new tab, copy-link and
// open-in-window come free, and the server already computed the href.
export default function OpenSource({ link }: { link: SourceLink | null }) {
  if (!link) return null
  return (
    <a
      className="source-link"
      href={link.href}
      target="_blank"
      rel="noopener noreferrer"
    >
      {link.kind === 'local' ? 'Open source (local copy)' : 'Open source'}
      {link.page !== null ? `, page ${link.page}` : ''}
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <path
          d="M5 2.5 H2.5 V9.5 H9.5 V7 M7 2.5 H9.5 V5 M9.5 2.5 L5.5 6.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
        />
      </svg>
      <span className="ev-sr"> (opens in a new tab)</span>
    </a>
  )
}
