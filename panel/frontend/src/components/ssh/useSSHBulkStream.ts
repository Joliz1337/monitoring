import { useCallback, useRef, useState } from 'react'
import { SSHBulkEvent, SSHStepResult } from '../../api/client'
import { streamNdjson, StreamUnauthorizedError } from '../../utils/ndjsonStream'

export interface BulkProgressRow {
  server_id: number
  server_name: string
  state: 'running' | 'success' | 'error'
  steps: SSHStepResult[]
}

export interface BulkProgressState {
  active: boolean
  finished: boolean
  total: number
  ok: number
  failed: number
  rows: BulkProgressRow[]
  error: string | null
}

const IDLE: BulkProgressState = {
  active: false, finished: false, total: 0, ok: 0, failed: 0, rows: [], error: null,
}

/**
 * Запускает стриминговую bulk-операцию SSH Security и собирает прогресс по серверам.
 * Строки заполняются на событии start и «загораются» success/error по мере прихода
 * каждого result — пользователь видит, какой сервер уже обработан.
 */
export function useSSHBulkStream() {
  const [progress, setProgress] = useState<BulkProgressState>(IDLE)
  const abortRef = useRef<AbortController | null>(null)

  const run = useCallback(async (url: string, body: unknown): Promise<BulkProgressState> => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    let state: BulkProgressState = { ...IDLE, active: true }
    setProgress(state)

    const handle = (ev: SSHBulkEvent) => {
      if (ev.type === 'start') {
        state = {
          ...state,
          total: ev.total,
          rows: ev.servers.map(s => ({
            server_id: s.server_id,
            server_name: s.server_name,
            state: 'running' as const,
            steps: [],
          })),
        }
      } else if (ev.type === 'result') {
        state = {
          ...state,
          rows: state.rows.map(r =>
            r.server_id === ev.server_id
              ? { ...r, state: ev.success ? 'success' : 'error', steps: ev.steps }
              : r,
          ),
        }
      } else {
        state = { ...state, finished: true, ok: ev.ok, failed: ev.failed }
      }
      setProgress(state)
    }

    try {
      await streamNdjson<SSHBulkEvent>(url, body, handle, controller.signal)
    } catch (e) {
      if (controller.signal.aborted || e instanceof StreamUnauthorizedError) {
        state = { ...state, active: false }
        setProgress(state)
        return state
      }
      state = { ...state, error: e instanceof Error ? e.message : String(e) }
    }

    state = { ...state, active: false }
    setProgress(state)
    return state
  }, [])

  const cancel = useCallback(() => abortRef.current?.abort(), [])
  const reset = useCallback(() => {
    abortRef.current?.abort()
    setProgress(IDLE)
  }, [])

  return { progress, run, cancel, reset }
}
