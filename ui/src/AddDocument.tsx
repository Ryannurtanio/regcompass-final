import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import {
  deleteDocument,
  fetchCorpus,
  fetchDocumentRemoval,
  patchDocument,
  pdfUrl,
  postDocumentUpload,
  postDocumentUrl,
} from './api'
import type { AddedDocument, CorpusDocument, DocumentRemoval, ServerStatus } from './types'
import ErrorNote from './ErrorNote'
import { plainError, type PlainError } from './errors'

function when(ts: string | null): string {
  if (!ts) return '-'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

function shortWhen(ts: string | null): string {
  if (!ts) return '-'
  const d = new Date(ts)
  return Number.isNaN(d.getTime())
    ? ts
    : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

// The name a reviewer last worked under, shared with the Correct control, so
// they type it once. A browser that keeps nothing simply asks again.
const NAME_KEY = 'regcompass.reviewerName'
function rememberedName(): string {
  try {
    return window.localStorage.getItem(NAME_KEY) ?? ''
  } catch {
    return ''
  }
}
function rememberName(name: string) {
  try {
    if (name) window.localStorage.setItem(NAME_KEY, name)
  } catch {
    // nothing kept; the field is simply empty next time
  }
}

/** Who corrected this row and when, in the quiet meta line. */
function editedLine(d: CorpusDocument): string | null {
  if (!d.edited_at) return null
  const who = d.edited_by ? ` by ${d.edited_by}` : ''
  return `edited${who}, ${shortWhen(d.edited_at)}`
}

/** A row's title, linked to where the Document is published when that is
 *  recorded, and the ways to check and correct it: "our copy" opens the file
 *  the Corpus stored, and Edit changes the Source URL and the title in place.
 *  Adding the Document again by URL would fetch the file a second time and
 *  leave a SECOND Document in the Corpus, so the fix edits the one already
 *  there. A row with no address says so, because the Evidence Export refuses
 *  it, and its control carries the name that refusal gives it. */
function DocumentTitle({
  doc,
  onSaved,
}: {
  doc: CorpusDocument
  onSaved: () => void
}) {
  const id = doc.document_id
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState(doc.title)
  const [url, setUrl] = useState(doc.source_url ?? '')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<PlainError | null>(null)
  const titleInput = useRef<HTMLInputElement>(null)

  // A long title opens showing its beginning, with the caret there, rather
  // than scrolled to its end.
  useEffect(() => {
    const el = titleInput.current
    if (!open || !el) return
    el.focus()
    el.setSelectionRange(0, 0)
    el.scrollLeft = 0
  }, [open])

  const start = () => {
    setTitle(doc.title)
    setUrl(doc.source_url ?? '')
    setName(rememberedName())
    setError(null)
    setOpen(true)
  }

  const titleChanged = title.trim() !== '' && title.trim() !== doc.title
  const urlChanged = url.trim() !== '' && url.trim() !== (doc.source_url ?? '')
  const canSave = !busy && (titleChanged || urlChanged)

  const save = () => {
    if (!canSave) return
    setBusy(true)
    setError(null)
    patchDocument(id, {
      ...(titleChanged ? { title } : {}),
      ...(urlChanged ? { sourceUrl: url } : {}),
      reviewer: name,
    })
      .then(() => {
        rememberName(name.trim())
        setOpen(false)
        onSaved()
      })
      .catch((e) => setError(plainError(e)))
      .finally(() => setBusy(false))
  }

  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Enter') save()
    if (e.key === 'Escape') setOpen(false)
  }

  const edited = editedLine(doc)

  return (
    <>
      {doc.source_url ? (
        <a
          className="law"
          href={doc.source_url}
          target="_blank"
          rel="noopener noreferrer"
          title={`Open the source: ${doc.source_url}`}
          data-testid={`source-link-${id}`}
        >
          {doc.title}
        </a>
      ) : (
        <span className="law">{doc.title}</span>
      )}
      <span className="doc-meta">
        {doc.source_url === null && (
          <span
            className="corpus-flag"
            title="No Source URL recorded: this Document's rows open the local copy, and the Evidence Export refuses them until an official address is given."
          >
            no Source URL
          </span>
        )}
        {doc.has_copy && (
          <a
            href={pdfUrl(id)}
            target="_blank"
            rel="noopener noreferrer"
            title="Open the file the Corpus stored for this Document"
            data-testid={`our-copy-${id}`}
          >
            our copy
          </a>
        )}
        {!open && (
          <button
            type="button"
            className="link-btn"
            data-testid={
              doc.source_url === null ? `set-source-url-${id}` : `edit-document-${id}`
            }
            onClick={start}
          >
            {doc.source_url === null ? 'Set Source URL' : 'Edit'}
          </button>
        )}
        {edited && <span data-testid={`edited-${id}`}>{edited}</span>}
      </span>
      {open && (
        <span className="edit-document" role="group" aria-label={`Edit ${doc.title}`}>
          <label>
            <span>Title</span>
            <input
              type="text"
              value={title}
              disabled={busy}
              ref={titleInput}
              data-testid={`title-input-${id}`}
              onChange={(e) => setTitle(e.target.value)}
              onKeyDown={onKey}
            />
          </label>
          <label>
            <span>Source URL</span>
            <input
              type="url"
              value={url}
              disabled={busy}
              placeholder="https://official.portal/act.pdf"
              data-testid={`source-url-input-${id}`}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={onKey}
            />
          </label>
          <label>
            <span>Your name (optional)</span>
            <input
              type="text"
              value={name}
              disabled={busy}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={onKey}
            />
          </label>
          <span className="edit-document-actions">
            <button
              type="button"
              className="primary"
              disabled={!canSave}
              data-testid={`save-document-${id}`}
              onClick={save}
            >
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button
              type="button"
              className="link-btn"
              disabled={busy}
              onClick={() => setOpen(false)}
            >
              Cancel
            </button>
          </span>
          <span className="hint">
            Changes the title and link only. The Document, its text and its
            Mappings stay as they are; the next Evidence Export uses the new
            values.
          </span>
          {error && <span className="corpus-flag">{error.message}</span>}
        </span>
      )}
    </>
  )
}

/** The way out of one bad add that does not clear the whole Economy and its
 *  Runs. It asks first: the server says what removing the Document takes
 *  (Mappings in past Runs quote it, or none do), and only the reviewer's yes
 *  removes it. */
function RemoveDocument({
  documentId,
  title,
  onRemoved,
}: {
  documentId: string
  title: string
  onRemoved: (removal: DocumentRemoval) => void
}) {
  const [preview, setPreview] = useState<DocumentRemoval | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<PlainError | null>(null)

  const ask = () => {
    setBusy(true)
    setError(null)
    fetchDocumentRemoval(documentId)
      .then(setPreview)
      .catch((e) => setError(plainError(e)))
      .finally(() => setBusy(false))
  }
  const confirm = () => {
    setBusy(true)
    setError(null)
    deleteDocument(documentId)
      .then((done) => {
        setPreview(null)
        onRemoved(done)
      })
      .catch((e) => setError(plainError(e)))
      .finally(() => setBusy(false))
  }

  if (!preview) {
    return (
      <>
        <button
          type="button"
          className="link-btn"
          disabled={busy}
          data-testid={`remove-document-${documentId}`}
          title={`Remove ${title} from this Corpus`}
          onClick={ask}
        >
          {busy ? 'Checking…' : 'Remove'}
        </button>
        {error && <span className="corpus-flag"> {error.message}</span>}
      </>
    )
  }
  return (
    <span className="remove-document" role="group" aria-label={`Remove ${title}`}>
      <span className="hint">{preview.summary}</span>{' '}
      <button
        type="button"
        disabled={busy}
        data-testid={`confirm-remove-${documentId}`}
        onClick={confirm}
      >
        {busy ? 'Removing…' : 'Remove it'}
      </button>{' '}
      <button type="button" className="link-btn" disabled={busy} onClick={() => setPreview(null)}>
        Keep it
      </button>
      {error && <span className="corpus-flag"> {error.message}</span>}
    </span>
  )
}

/** "Add document": the other way a Document enters an Economy's Corpus.
 *
 *  Discovery fills a Corpus from a Portal. This fills it one Document at a
 *  time, from a file the reviewer picked or from an official URL they typed,
 *  and the Document then takes exactly the path a discovered one takes.
 *
 *  On an Economy whose Portal forbids automated collection the URL lane is
 *  closed, with the reason on screen rather than a control that fails: adding
 *  by URL is still a request we would be making. Upload stays open, and the
 *  whole control opens by default, because for those Economies it is the only
 *  door in.
 */
export default function AddDocument({
  status,
  economy,
  reloadKey,
  onAdded,
  onCorpusCount,
  onCorpus,
  onCorpusFailed,
  openSignal,
}: {
  status: ServerStatus | null
  economy: string
  // Changes when something outside this control may have changed the Corpus,
  // a Discovery having just finished being the case that matters.
  reloadKey?: number
  onAdded?: (added: AddedDocument) => void
  // How many Documents this Economy's Corpus holds, reported upward so the
  // Run panel can say "nothing here yet" before a Run is ever refused.
  onCorpusCount?: (economy: string, n: number) => void
  // The Corpus itself, for the Run panel's short list: one read serves both,
  // so the panel never asks for the same Corpus a second time.
  onCorpus?: (economy: string, documents: CorpusDocument[]) => void
  // The Corpus could not be read, so no count or list is known for it.
  onCorpusFailed?: (economy: string) => void
  // Bumped when another control asks for this one ("Add document" beside the
  // Start Run button): it opens and comes into view.
  openSignal?: number
}) {
  const manualOnly = (status?.manual_only ?? []).includes(economy)
  // Not the same thing: no Discovery plan is wired for this Economy yet, so
  // both add lanes stay open and it may gain a strategy later.
  const noDiscovery = (status?.no_discovery ?? []).includes(economy)
  const languages = useMemo(
    () => status?.economy_languages?.[economy] ?? [],
    [status, economy],
  )

  const [open, setOpen] = useState(false)
  const [lane, setLane] = useState<'upload' | 'url'>('upload')
  const [sourceUrl, setSourceUrl] = useState('')
  // The statute's own name. Optional, and worth typing: it becomes the
  // Document's title, and the title is what the Evidence Export writes into
  // the organizer's Law Name column.
  const [title, setTitle] = useState('')
  const [language, setLanguage] = useState('')
  const [allowAnyHost, setAllowAnyHost] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<PlainError | null>(null)
  const [added, setAdded] = useState<AddedDocument | null>(null)
  const [removed, setRemoved] = useState<string | null>(null)
  // This Economy's Corpus, listed here because this is where a reviewer has
  // just put something into it. /api/documents answers what a Run mapped, so
  // before the first Run it is empty and an upload appears to have vanished.
  const [corpus, setCorpus] = useState<CorpusDocument[] | null>(null)
  // A Corpus read that failed, said as such: "Reading the Corpus" forever
  // would read as a slow server, and an empty list as an empty Corpus.
  const [corpusError, setCorpusError] = useState<PlainError | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  // Held in a ref, NOT read as a dependency. The parent passes a fresh arrow
  // on every render, so depending on it would rebuild the fetch on every
  // render, and the fetch sets state, which renders again: a slow request
  // loop for the life of the page (measured at about one a second).
  const reportCount = useRef(onCorpusCount)
  reportCount.current = onCorpusCount
  const reportCorpus = useRef(onCorpus)
  reportCorpus.current = onCorpus
  const reportFailed = useRef(onCorpusFailed)
  reportFailed.current = onCorpusFailed

  const reloadCorpus = useCallback(() => {
    if (!economy) return
    setCorpusError(null)
    fetchCorpus(economy)
      .then((c) => {
        setCorpus(c.documents)
        reportCount.current?.(c.economy, c.n)
        reportCorpus.current?.(c.economy, c.documents)
      })
      .catch((e) => {
        setCorpus(null)
        setCorpusError(plainError(e))
        reportFailed.current?.(economy)
      })
  }, [economy])

  // Re-read when the Economy changes, and when the parent says something else
  // has touched the Corpus (a Discovery that just finished).
  useEffect(reloadCorpus, [reloadCorpus, reloadKey])

  // The Language follows the Economy: its first configured Language is the
  // default, and the reviewer overrides it per Document when they know better.
  useEffect(() => {
    setLanguage(languages[0] ?? '')
    setAdded(null)
    setError(null)
  }, [economy, languages.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  // A manual-only Economy has no URL lane at all, so never leave the control
  // sitting on a tab it cannot use.
  useEffect(() => {
    if (manualOnly) setLane('upload')
  }, [manualOnly])

  useEffect(() => {
    if (manualOnly || noDiscovery) setOpen(true)
  }, [manualOnly, noDiscovery, economy])

  const rootRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!openSignal) return
    setOpen(true)
    rootRef.current?.scrollIntoView({ block: 'start' })
    rootRef.current?.querySelector<HTMLElement>('.add-toggle')?.focus({ preventScroll: true })
  }, [openSignal])

  const submit = () => {
    setError(null)
    setAdded(null)
    setRemoved(null)
    setBusy(true)
    const done = (a: AddedDocument) => {
      setAdded(a)
      setSourceUrl('')
      setTitle('')
      if (fileRef.current) fileRef.current.value = ''
      reloadCorpus()
      onAdded?.(a)
    }
    // A fetch that failed says so in plain words; the site's own refusal
    // stays behind the technical toggle.
    const failed = (e: unknown) => setError(plainError(e, lane === 'url' ? 'fetch' : 'generic'))
    const settled = () => setBusy(false)
    if (lane === 'upload') {
      const file = fileRef.current?.files?.[0]
      if (!file) {
        setError({
          message: 'Choose a file to upload: the law as a PDF, or its saved web page.',
          technical: null,
        })
        setBusy(false)
        return
      }
      postDocumentUpload({ economy, sourceUrl, language, title, file })
        .then(done)
        .catch(failed)
        .finally(settled)
    } else {
      postDocumentUrl({ economy, sourceUrl, language, title, allowAnyHost })
        .then(done)
        .catch(failed)
        .finally(settled)
    }
  }

  // An upload needs no Source URL. A file saved by hand during the live hour
  // has to be able to enter the Corpus now; where it is published is a fact
  // that can be recorded later, and the Export refuses the row until it is.
  // The URL lane is different: without the URL there is nothing to fetch.
  const ready = Boolean(
    economy && language && (lane === 'upload' || sourceUrl.trim()),
  )

  const economyName = status?.economy_names?.[economy] ?? economy

  return (
    <section
      className={manualOnly ? 'add-document prominent' : 'add-document'}
      id="add-document"
      ref={rootRef}
      aria-labelledby="add-document-title"
    >
      <div className="add-document-head">
        <div>
          <h2 id="add-document-title">
            Corpus of {economyName}
            {corpus === null ? '' : `, ${corpus.length} Document${corpus.length === 1 ? '' : 's'}`}
          </h2>
          <p className="hint">
            What a Run on {economyName} reads. Add a Document by uploading its
            PDF or giving its official URL to fetch once.
          </p>
        </div>
        <button
          type="button"
          className="btn add-toggle"
          aria-expanded={open}
          aria-controls="add-document-body"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? 'Close' : 'Add document'}
        </button>
      </div>

      {manualOnly && (
        <div className="notice">
          {economyName} is manual-only: its Portal's own site rules do not
          permit automated collection, so Discovery never runs here and every
          Document is uploaded by hand. Adding one by URL would still be a
          request we made, so that lane is closed too.
        </div>
      )}

      {!manualOnly && noDiscovery && (
        <div className="notice">
          {economyName} has no Discovery strategy configured yet, so nothing is
          fetched for it automatically. Both ways below stay open: add a
          Document by its official Source URL, where the Portal host is
          whitelisted, or upload the file.
        </div>
      )}

      {open && (
        <div className="add-document-body" id="add-document-body">
          <div className="add-form">
            <div className="lane-tabs" role="group" aria-label="How to add it">
              <button
                type="button"
                className={lane === 'upload' ? 'lane-tab on' : 'lane-tab'}
                aria-pressed={lane === 'upload'}
                disabled={busy}
                onClick={() => setLane('upload')}
              >
                Upload a file
              </button>
              <button
                type="button"
                className={lane === 'url' ? 'lane-tab on' : 'lane-tab'}
                aria-pressed={lane === 'url'}
                disabled={busy || manualOnly}
                title={
                  manualOnly
                    ? 'This Portal forbids automated collection, and an add by URL is still a request. Upload the file instead.'
                    : undefined
                }
                onClick={() => setLane('url')}
              >
                Fetch a URL
              </button>
            </div>

            <div className="add-fields">
              <div className="field">
                <label htmlFor="add-title">Law name</label>
                <input
                  id="add-title"
                  type="text"
                  value={title}
                  disabled={busy}
                  placeholder="Electronic Transactions Act 2010"
                  onChange={(e) => setTitle(e.target.value)}
                />
                <span className="hint">
                  Optional, and worth typing: it becomes the Document's title,
                  which the Evidence Export writes into the Law Name column.
                  Left blank, the name comes from the file.
                </span>
              </div>

              <div className="field">
                <label htmlFor="add-source-url">
                  Source URL{lane === 'upload' ? ' (optional)' : ''}
                </label>
                <input
                  id="add-source-url"
                  type="url"
                  value={sourceUrl}
                  disabled={busy}
                  placeholder="https://official.portal/act.pdf"
                  onChange={(e) => setSourceUrl(e.target.value)}
                />
                <span className="hint">
                  {lane === 'upload'
                    ? 'Where this file is published. Leave it blank to add the file now: its rows then open the local copy, and the Evidence Export refuses them until you record the official address here.'
                    : 'The official URL to fetch, once, politely.'}
                </span>
              </div>

              {lane === 'upload' && (
                <div className="field">
                  <label htmlFor="add-file">File</label>
                  <input
                    id="add-file"
                    type="file"
                    accept=".pdf,.html,.htm,.txt,application/pdf,text/html,text/plain"
                    ref={fileRef}
                    disabled={busy}
                  />
                  <span className="hint">
                    A PDF, a saved web page or plain text. Word files and images
                    are refused: save the law as a PDF first.
                  </span>
                </div>
              )}

              <div className="field">
                <label htmlFor="add-language">Language</label>
                <select
                  id="add-language"
                  value={language}
                  disabled={busy}
                  onChange={(e) => setLanguage(e.target.value)}
                >
                  {languages.map((l) => (
                    <option key={l} value={l}>
                      {l}
                    </option>
                  ))}
                </select>
                <span className="hint">
                  Decides how scanned pages are read and which Gate lane the
                  Document takes. Defaults to this Economy's first Language;
                  correct it per Document.
                </span>
              </div>

              {lane === 'url' && (
                <div className="field check-field">
                  <label htmlFor="add-any-host">
                    <input
                      id="add-any-host"
                      type="checkbox"
                      checked={allowAnyHost}
                      disabled={busy}
                      onChange={(e) => setAllowAnyHost(e.target.checked)}
                    />
                    <span>Official source outside the configured Portal</span>
                  </label>
                  <span className="hint">
                    Tick this to vouch for a host the Portal whitelist does not
                    carry. Every row from this Document then says so.
                  </span>
                </div>
              )}
            </div>

            <div className="field actions-row">
              <button className="primary" disabled={busy || !ready} onClick={submit}>
                {busy ? 'Adding…' : 'Add to Corpus'}
              </button>
            </div>

            {error && <ErrorNote error={error} testId="add-error" />}
            {removed && <div className="notice">{removed}</div>}
            {added && (
              <div className="notice">
                Added {added.title} ({added.document_id}): {added.n_pages.toLocaleString('en-US')} pages
                {added.ocr_applied ? ', OCR applied' : ''}. This Economy's Corpus
                now holds {added.corpus_documents} Document
                {added.corpus_documents === 1 ? '' : 's'}. Highlight boxes are
                written on the next Run.
              </div>
            )}
            {added?.warning && (
              <div className="error" data-testid="add-warning">
                {added.warning}
              </div>
            )}
          </div>

          <div className="corpus-list">
            {corpusError ? (
              <ErrorNote
                testId="corpus-error"
                error={{ ...corpusError, message: `The Corpus could not be read. ${corpusError.message}` }}
                onRetry={reloadCorpus}
              />
            ) : corpus === null ? (
              <span className="hint">Reading the Corpus…</span>
            ) : corpus.length === 0 ? (
              <span className="hint" data-testid="corpus-empty">
                No Documents yet. Add one above, or run Discovery.
              </span>
            ) : (
              <div className="corpus-scroll">
                <table className="corpus" data-testid="corpus-table">
                  <thead>
                    <tr>
                      <th>Document</th>
                      <th>How</th>
                      <th>Language</th>
                      <th className="num">Pages</th>
                      <th className="num">Added</th>
                      <th>
                        <span className="sr-only">Remove</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {corpus.map((d) => (
                      <tr key={d.document_id}>
                        {/* A row carrying the inline control must not be
                            clipped: a truncated cell hides the control and
                            swallows the click meant for it. */}
                        <td className="roomy doc-title" title={d.document_id}>
                          <DocumentTitle doc={d} onSaved={reloadCorpus} />
                        </td>
                        <td>{d.source_kind}</td>
                        <td>{d.language ?? '-'}</td>
                        <td className="num">{d.n_pages?.toLocaleString('en-US') ?? '-'}</td>
                        <td className="num" title={when(d.added_at)}>
                          {shortWhen(d.added_at)}
                        </td>
                        <td className="row-actions">
                          <RemoveDocument
                            documentId={d.document_id}
                            title={d.title}
                            onRemoved={(r) => {
                              setAdded(null)
                              setRemoved(r.summary)
                              reloadCorpus()
                            }}
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <span className="hint">
              A Document appears here the moment it is added; the Evidence
              screen lists it once a Run has mapped it.
            </span>
          </div>
        </div>
      )}
    </section>
  )
}
