import { useEffect, useRef, useState } from 'react'
import { isForFocusedControl, isInShell } from './keys'

/** One selection for a list of rows: the row the J and K (or arrow) keys are
 *  on IS the row with keyboard focus once the reviewer is in the list, so Tab
 *  and the keys never point at two different rows. Only the selected row is
 *  in the Tab order; J and K move focus along the list, and Enter opens the
 *  selected row (the browser's own click when a row has focus, this handler
 *  when focus is still on the screen around it). */
export function useRowKeys(count: number, onEnter: (index: number) => void) {
  const [focus, setFocus] = useState(0)
  const listRef = useRef<HTMLOListElement>(null)
  const focusRef = useRef(focus)
  focusRef.current = focus

  useEffect(() => {
    if (count === 0) return
    const rowButtons = () =>
      Array.from(listRef.current?.querySelectorAll<HTMLElement>('.ev-row') ?? [])
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || isInShell(e)) return
      const target = e.target as Node | null
      const onRow = !!(target && listRef.current?.contains(target))
      // A key pressed in some other control (the filter, a button) is that
      // control's; a key pressed on one of the rows is the list's.
      if (!onRow && isForFocusedControl(e)) return
      let next: number | null = null
      if (e.key === 'ArrowDown' || e.key === 'j') next = Math.min(focusRef.current + 1, count - 1)
      else if (e.key === 'ArrowUp' || e.key === 'k') next = Math.max(focusRef.current - 1, 0)
      else if (e.key === 'Enter' && !onRow) {
        onEnter(focusRef.current)
        return
      }
      if (next === null) return
      e.preventDefault()
      setFocus(next)
      rowButtons()[next]?.focus()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [count, onEnter])

  // The selected row stays on screen as the reviewer walks down.
  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>('.ev-row.is-focus')
      ?.scrollIntoView({ block: 'nearest' })
  }, [focus])

  /** Props for row i: in the Tab order only when selected, and focusing it
   *  (by Tab or by a click) makes it the selected row. */
  const rowProps = (i: number) => ({
    tabIndex: i === focus ? 0 : -1,
    onFocus: () => setFocus(i),
  })

  return { focus, setFocus, listRef, rowProps }
}
