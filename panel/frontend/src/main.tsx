import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { reloadOnStaleChunk } from './utils/staleChunkReload'
import './index.css'
import './i18n'

window.addEventListener('vite:preloadError', (event) => {
  if (reloadOnStaleChunk()) event.preventDefault()
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
