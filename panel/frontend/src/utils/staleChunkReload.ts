const RELOAD_GUARD_KEY = 'chunk_reload_at'
const RELOAD_GUARD_MS = 15_000

export function isChunkLoadError(error: Error): boolean {
  return (
    error.name === 'ChunkLoadError' ||
    error.message.includes('Loading chunk') ||
    error.message.includes('Failed to fetch dynamically imported module') ||
    error.message.includes('Importing a module script failed')
  )
}

/**
 * Устаревший чанк после обновления панели: вкладка, открытая до обновления,
 * держит index.html со старыми хэшами, и ленивая страница не грузится, пока
 * пользователь не нажмёт F5. Перезагружаем сами; повтор не раньше чем через
 * RELOAD_GUARD_MS — иначе при действительно пропавшем файле страница крутилась бы в цикле.
 */
export function reloadOnStaleChunk(): boolean {
  let lastReloadAt = 0
  try {
    lastReloadAt = Number(sessionStorage.getItem(RELOAD_GUARD_KEY) || 0)
  } catch { /* приватный режим браузера */ }
  if (Date.now() - lastReloadAt < RELOAD_GUARD_MS) return false
  try {
    sessionStorage.setItem(RELOAD_GUARD_KEY, String(Date.now()))
  } catch { /* приватный режим браузера */ }
  window.location.reload()
  return true
}
