// Состояние интерфейса (свёрнутые папки, порядок, переключатели) не должно ронять
// страницу: setItem бросает QuotaExceededError при переполненном хранилище, а любое
// обращение к localStorage — SecurityError, если браузер запретил сайту хранить данные.
// Брошенное из обработчика, апдейтера setState или эффекта, это уводит страницу
// в ErrorBoundary. При сбое состояние просто живёт до перезагрузки

export function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch { /* квота исчерпана или хранилище запрещено */ }
}
