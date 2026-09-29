/** Whether a key press belongs to the control that has focus rather than to a
 *  screen's own shortcuts: a form field someone is typing in, or a button or
 *  link that Enter and Space already operate. Without this, Enter on the theme
 *  switch would also open the first Document of the list behind it. */
export function isForFocusedControl(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null
  if (!t || !(t instanceof HTMLElement)) return false
  if (t.isContentEditable) return true
  return ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'A'].includes(t.tagName)
}

/** Whether a key press happened in the app's frame (top bar, phone menu)
 *  rather than on the screen itself. A screen's shortcuts leave those alone. */
export function isInShell(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null
  return !!(t && t instanceof HTMLElement && t.closest('.topbar, .phone-menu'))
}
