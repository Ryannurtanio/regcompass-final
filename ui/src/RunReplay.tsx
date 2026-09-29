import { useEffect, useReducer, useRef, useState } from 'react'
import RunView from './RunView'
import { emptyRunView, runViewReducer } from './runViewState'
import {
  DEFAULT_REPLAY_SPEED,
  REPLAY_SPEEDS,
  ReplayController,
  type ReplayPhase,
} from './replayControl'

// A recorded Run, watched again on the same Run view a live Run uses, fed to
// the same reducer. The controls only pace the stream: pause, resume, speed.

const PHASE_TEXT: Record<ReplayPhase, string> = {
  playing: 'Playing',
  paused: 'Paused',
  finished: 'Shown to the end',
  broken: 'The replay stream stopped. Resume carries on from where it stopped.',
  stopped: 'Stopped',
}

export default function RunReplay({
  runId,
  onClose,
  onOpenEvidence,
}: {
  runId: string
  onClose: () => void
  onOpenEvidence?: (runId: string) => void
}) {
  const [state, dispatch] = useReducer(runViewReducer, emptyRunView)
  const [phase, setPhase] = useState<ReplayPhase | null>(null)
  const [speed, setSpeed] = useState<number>(DEFAULT_REPLAY_SPEED)
  const controller = useRef<ReplayController | null>(null)
  // The speed the next start uses, without restarting the replay on change.
  const speedRef = useRef(speed)
  speedRef.current = speed

  const begin = () => {
    controller.current?.stop()
    dispatch({ type: 'reset' })
    const c = new ReplayController({
      runId,
      speed: speedRef.current,
      onEvent: (event) => dispatch({ type: 'event', event }),
      onPhase: setPhase,
    })
    controller.current = c
    c.start()
  }

  useEffect(() => {
    begin()
    return () => controller.current?.stop()
  }, [runId]) // eslint-disable-line react-hooks/exhaustive-deps

  const changeSpeed = (next: number) => {
    setSpeed(next)
    controller.current?.setSpeed(next)
  }

  // The end of the replay says how the Run itself ended.
  const phaseText =
    phase === 'finished' && state.status === 'failed'
      ? 'Shown to the end: this Run stopped before it finished'
      : phase === 'finished' && state.status === 'finished'
        ? 'Shown to the end: the Run finished'
        : phase
          ? PHASE_TEXT[phase]
          : 'Starting'

  return (
    <div className="panel run-screen">
      <section className="replay-bar" aria-label="Replay controls" data-testid="replay-bar">
        <div className="replay-head">
          <button className="btn back-btn" onClick={onClose}>
            ← Run history
          </button>
          <span className="hint" title={runId}>
            Run Record {runId}
          </span>
        </div>
        <div className="replay-controls">
          {phase === 'playing' ? (
            <button className="primary" onClick={() => controller.current?.pause()}>
              Pause
            </button>
          ) : phase === 'paused' || phase === 'broken' ? (
            <button className="primary" onClick={() => controller.current?.resume()}>
              Resume
            </button>
          ) : (
            <button className="primary" onClick={begin}>
              Watch from the start
            </button>
          )}
          <label className="replay-speed">
            Speed{' '}
            <select value={speed} onChange={(e) => changeSpeed(Number(e.target.value))}>
              {REPLAY_SPEEDS.map((s) => (
                <option key={s} value={s}>
                  {s} times
                </option>
              ))}
            </select>
          </label>
          <span className="hint" role="status" data-testid="replay-phase">
            {phaseText}
          </span>
        </div>
        <span className="hint">
          Paced at {speed} times the Run's own speed, with long waits cut short.
        </span>
      </section>
      <RunView state={state} onOpenEvidence={onOpenEvidence} />
    </div>
  )
}
