import { useCallback, useRef, useState } from 'react'
import { streamNdjson, StreamUnauthorizedError } from '../utils/ndjsonStream'

export interface BulkStreamRow<TRes> {
  server_id: number
  server_name: string
  state: 'running' | 'success' | 'error'
  result?: TRes
}

export interface BulkStreamState<TRes> {
  active: boolean
  finished: boolean
  total: number
  ok: number
  failed: number
  rows: BulkStreamRow<TRes>[]
  error: string | null
}

type BulkStreamEvent<TRes> =
  | { type: 'start'; total: number; servers: { server_id: number; server_name: string }[] }
  | ({ type: 'result'; server_id: number; server_name: string; success: boolean } & TRes)
  | { type: 'done'; total: number; ok: number; failed: number }

function idleState<TRes>(): BulkStreamState<TRes> {
  return { active: false, finished: false, total: 0, ok: 0, failed: 0, rows: [], error: null }
}

/**
 * Запускает стриминговую bulk-операцию (NDJSON: start → result → done) и собирает
 * прогресс по серверам. Строки заполняются на событии start и «загораются»
 * success/error по мере прихода каждого result — пользователь видит, какой сервер
 * уже обработан.
 */
export function useBulkStream<TRes extends { success: boolean }>() {
  const [progress, setProgress] = useState<BulkStreamState<TRes>>(idleState<TRes>())
  const abortRef = useRef<AbortController | null>(null)

  const run = useCallback(async (url: string, body: unknown): Promise<BulkStreamState<TRes>> => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    let state: BulkStreamState<TRes> = { ...idleState<TRes>(), active: true }
    setProgress(state)

    const handle = (ev: BulkStreamEvent<TRes>) => {
      if (ev.type === 'start') {
        state = {
          ...state,
          total: ev.total,
          rows: ev.servers.map(s => ({
            server_id: s.server_id,
            server_name: s.server_name,
            state: 'running' as const,
          })),
        }
      } else if (ev.type === 'result') {
        const { type: _type, ...result } = ev
        state = {
          ...state,
          rows: state.rows.map(r =>
            r.server_id === ev.server_id
              ? { ...r, state: ev.success ? 'success' : 'error', result: result as unknown as TRes }
              : r,
          ),
        }
      } else {
        state = { ...state, finished: true, ok: ev.ok, failed: ev.failed }
      }
      setProgress(state)
    }

    try {
      await streamNdjson<BulkStreamEvent<TRes>>(url, body, handle, controller.signal)
    } catch (e) {
      if (controller.signal.aborted || e instanceof StreamUnauthorizedError) {
        state = { ...state, active: false }
        setProgress(state)
        return state
      }
      // Поток оборвался — серверы без результата помечаем ошибкой, а не вечным «running»
      state = {
        ...state,
        error: e instanceof Error ? e.message : String(e),
        rows: state.rows.map(r => (r.state === 'running' ? { ...r, state: 'error' as const } : r)),
      }
    }

    state = { ...state, active: false }
    setProgress(state)
    return state
  }, [])

  const cancel = useCallback(() => abortRef.current?.abort(), [])
  const reset = useCallback(() => {
    abortRef.current?.abort()
    setProgress(idleState<TRes>())
  }, [])

  return { progress, run, cancel, reset }
}
