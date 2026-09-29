import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Every face is bundled: a Run and the screens around it never fetch a font.
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/newsreader/400.css'
import '@fontsource/newsreader/500.css'
import '@fontsource/noto-sans-lao/400.css'
import '@fontsource/noto-sans-lao/500.css'
import './styles.css'
import App from './App'
import { applyStoredTheme } from './theme'

applyStoredTheme()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
