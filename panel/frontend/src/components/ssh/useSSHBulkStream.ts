import { useCallback, useMemo } from 'react'
import { SSHStepResult } from '../../api/client'
import { BulkStreamState, useBulkStream } from '../../hooks/useBulkStream'

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

type SSHBulkResult = { success: boolean; steps?: SSHStepResult[] }

function toProgressState(state: BulkStreamState<SSHBulkResult>): BulkProgressState {
  return {
    ...state,
    rows: state.rows.map(r => ({
      server_id: r.server_id,
      server_name: r.server_name,
      state: r.state,
      steps: r.result?.steps ?? [],
    })),
  }
}

/**
 * Адаптер SSH Security над общим useBulkStream: тот хранит результат ноды целиком,
 * здесь он раскрывается в шаги применения для BulkProgressPanel.
 */
export function useSSHBulkStream() {
  const stream = useBulkStream<SSHBulkResult>()

  const progress = useMemo(() => toProgressState(stream.progress), [stream.progress])

  const { run: streamRun } = stream
  const run = useCallback(
    async (url: string, body: unknown): Promise<BulkProgressState> =>
      toProgressState(await streamRun(url, body)),
    [streamRun],
  )

  return { progress, run, cancel: stream.cancel, reset: stream.reset }
}
