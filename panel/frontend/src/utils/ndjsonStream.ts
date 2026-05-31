export class StreamUnauthorizedError extends Error {
  constructor() {
    super('Unauthorized')
    this.name = 'StreamUnauthorizedError'
  }
}

function redirectToLogin() {
  const currentPath = window.location.pathname
  const uid = currentPath.split('/')[1]
  if (uid && !currentPath.includes('/login')) {
    window.location.href = `/${uid}/login`
  }
}

async function readNdjsonResponse<T>(res: Response, onMessage: (msg: T) => void): Promise<void> {
  if (res.status === 401) {
    redirectToLogin()
    throw new StreamUnauthorizedError()
  }

  if (!res.ok || !res.body) {
    let detail = `HTTP ${res.status}`
    try {
      const data = await res.json()
      if (data?.detail) detail = data.detail
    } catch {
      // ответ не JSON — оставляем код статуса
    }
    throw new Error(detail)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let newline: number
    while ((newline = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, newline).trim()
      buffer = buffer.slice(newline + 1)
      if (line) onMessage(JSON.parse(line) as T)
    }
  }

  const tail = buffer.trim()
  if (tail) onMessage(JSON.parse(tail) as T)
}

/**
 * Читает NDJSON-поток ответа на POST: построчно парсит JSON и отдаёт каждый
 * объект в onMessage. Используется для bulk-операций SSH Security.
 */
export async function streamNdjson<T>(
  url: string,
  body: unknown,
  onMessage: (msg: T) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  await readNdjsonResponse(res, onMessage)
}

/**
 * Читает NDJSON-поток ответа на GET. Используется для подписки на лог фоновой
 * задачи установки ноды — поток переподключаемый и не привязан к телу запроса.
 */
export async function streamNdjsonGet<T>(
  url: string,
  onMessage: (msg: T) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: 'GET',
    credentials: 'include',
    signal,
  })
  await readNdjsonResponse(res, onMessage)
}
