import type { PlainError } from './errors'

/** A failure on screen: one plain sentence, an optional way to try again,
 *  and the raw text behind "Show the technical message", as the Run view
 *  shows a stopped Run. `className` keeps each screen's own placement. */
export default function ErrorNote({
  error,
  onRetry,
  className = 'error',
  testId,
  children,
}: {
  error: PlainError
  onRetry?: () => void
  className?: string
  testId?: string
  children?: React.ReactNode
}) {
  return (
    <div className={`${className} error-note`} role="alert" data-testid={testId}>
      <p>
        {error.message}
        {onRetry && (
          <>
            {' '}
            <button type="button" className="link-btn" onClick={onRetry}>
              Try again
            </button>
          </>
        )}
      </p>
      {children}
      {error.technical && (
        <details>
          <summary>Show the technical message</summary>
          <code>{error.technical}</code>
        </details>
      )}
    </div>
  )
}
