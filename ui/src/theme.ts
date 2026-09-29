import { useCallback, useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

// The browser keeps a chosen theme here. Nothing is stored while the app
// simply follows the system setting, so a reader who never touches the switch
// keeps following it.
const KEY = 'regcompass.theme'

function systemTheme(): Theme {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

function storedTheme(): Theme | null {
  try {
    const v = window.localStorage.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : null
  } catch {
    // Storage can be blocked (a private window, cleared site data): the app
    // then follows the system setting and the switch works for this visit.
    return null
  }
}

function store(theme: Theme | null) {
  try {
    if (theme === null) window.localStorage.removeItem(KEY)
    else window.localStorage.setItem(KEY, theme)
  } catch {
    // see storedTheme
  }
}

/** Put a remembered choice on the page before the first paint. The CSS
 *  follows the system setting on its own; this only pins an explicit choice. */
export function applyStoredTheme() {
  const t = storedTheme()
  if (t) document.documentElement.dataset.theme = t
}

/** The theme on screen and a switch to the other one. Choosing the theme the
 *  system already asks for forgets the choice, so the app goes back to
 *  following the system. */
export function useTheme(): { theme: Theme; toggle: () => void } {
  const [chosen, setChosen] = useState<Theme | null>(storedTheme)
  const [system, setSystem] = useState<Theme>(systemTheme)

  useEffect(() => {
    let mq: MediaQueryList
    try {
      mq = window.matchMedia('(prefers-color-scheme: dark)')
    } catch {
      return
    }
    const onChange = () => setSystem(mq.matches ? 'dark' : 'light')
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  useEffect(() => {
    const root = document.documentElement
    if (chosen) root.dataset.theme = chosen
    else delete root.dataset.theme
  }, [chosen])

  const theme = chosen ?? system
  const toggle = useCallback(() => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    const keep = next === system ? null : next
    store(keep)
    setChosen(keep)
  }, [theme, system])

  return { theme, toggle }
}
