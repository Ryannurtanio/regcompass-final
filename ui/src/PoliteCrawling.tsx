import { useEffect, useState } from 'react'
import { fetchPoliteness } from './api'
import type { Politeness } from './types'
import ErrorNote from './ErrorNote'
import { plainError, type PlainError } from './errors'

/** "6 s", "2.5 s": the least time left between two requests to one site. */
function seconds(n: number): string {
  return `${Number.isInteger(n) ? n : n.toFixed(1)} s`
}

function connections(n: number): string {
  return n === 1 ? '1 connection at a time' : `${n} connections at a time`
}

/** Settings card "Polite crawling": the crawler's limits, read from the same
 *  Portal configuration it runs on. Nothing here can be changed. */
export default function PoliteCrawling() {
  const [data, setData] = useState<Politeness | null>(null)
  const [error, setError] = useState<PlainError | null>(null)

  useEffect(() => {
    fetchPoliteness()
      .then(setData)
      .catch((e) => setError(plainError(e)))
  }, [])

  return (
    <section className="settings-card polite-card" aria-labelledby="polite-title">
      <div className="settings-card-head">
        <h2 id="polite-title">Polite crawling</h2>
        <p className="lead">
          How RegCompass treats each official site it reads from. These limits
          are built in and always on. They are shown here to check, not to
          change.
        </p>
      </div>

      {error && <ErrorNote error={error} />}

      <div className="polite-switch">
        <label className="locked-switch" htmlFor="robots-switch">
          <input
            id="robots-switch"
            type="checkbox"
            role="switch"
            checked
            disabled
            readOnly
            aria-disabled="true"
            aria-describedby="robots-switch-note"
          />
          <span className="locked-switch-track" aria-hidden="true">
            <span className="locked-switch-thumb" />
          </span>
          <span className="locked-switch-label">robots.txt respected</span>
          <svg className="lock-cue" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <rect x="2.5" y="6" width="9" height="6.5" rx="1.2" fill="none" stroke="currentColor" strokeWidth="1.4" />
            <path d="M4.5 6 V4.3 a2.5 2.5 0 0 1 5 0 V6" fill="none" stroke="currentColor" strokeWidth="1.4" />
          </svg>
        </label>
        <span id="robots-switch-note" className="hint">
          Always on. It cannot be turned off.
        </span>
      </div>

      {data && (
        <table className="polite-table">
          <thead>
            <tr>
              <th>Economy</th>
              <th>Sites</th>
              <th>Wait between requests</th>
              <th>Connections</th>
              <th>If robots.txt answers with a server error</th>
            </tr>
          </thead>
          <tbody>
            {data.portals.map((p) => (
              <tr key={p.economy}>
                <td data-label="Economy">
                  <span>
                    {p.name} <span className="polite-code">({p.economy})</span>
                  </span>
                </td>
                <td data-label="Sites" className="mono">
                  <span className="polite-hosts">
                    {p.hosts.map((h) => (
                      <span key={h}>{h}</span>
                    ))}
                  </span>
                </td>
                <td data-label="Wait">
                  <span>at least {seconds(p.min_interval_seconds)}</span>
                </td>
                <td data-label="Connections">
                  <span>{connections(p.connections_per_host)}</span>
                </td>
                <td data-label="robots.txt error" className="polite-policy">
                  <span>{p.robots_unavailable_policy}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {data && (
        <ul className="polite-rules">
          <li>{data.crawl_delay_rule}</li>
          <li>{data.robots_unreachable_rule}</li>
        </ul>
      )}
    </section>
  )
}
