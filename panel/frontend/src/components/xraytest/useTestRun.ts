import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  xrayTestApi,
  xrayTestStreamUrl,
  type XrayTestCell,
  type XrayTestEvent,
  type XrayTestRunRequest,
} from '../../api/client'
import { streamNdjsonGet, StreamUnauthorizedError } from '../../utils/ndjsonStream'

const JOB_KEY = 'xray_test_job_v1'
// Прогон живёт на панели и не обрывается вместе с соединением, поэтому потерю
// потока лечим переподключением: при нём бэкенд отдаёт все результаты заново.
const STREAM_RETRIES = 60
const STREAM_RETRY_DELAY = 3000

interface StoredJob {
  jobId: string
  total: number
}

function readStoredJob(): StoredJob | null {
  try {
    const raw = localStorage.getItem(JOB_KEY)
    return raw ? (JSON.parse(raw) as StoredJob) : null
  } catch {
    return null
  }
}

function writeStoredJob(job: StoredJob | null) {
  try {
    if (job) localStorage.setItem(JOB_KEY, JSON.stringify(job))
    else localStorage.removeItem(JOB_KEY)
  } catch {
    // приватный режим браузера — переживём без восстановления после F5
  }
}

export interface RunSummary {
  ok: number
  degraded: number
  fail: number
}

/**
 * Ведёт один прогон: запуск, живой поток результатов и восстановление после
 * перезагрузки страницы. Задача живёт на бэкенде, поэтому закрытая вкладка её
 * не обрывает — при возврате поток переподключается к тому же job_id.
 */
export function useTestRun() {
  const [jobId, setJobId] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [total, setTotal] = useState(0)
  const [cells, setCells] = useState<XrayTestCell[]>([])
  const [log, setLog] = useState<string[]>([])
  const [summary, setSummary] = useState<RunSummary | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const restoredRef = useRef(false)
  const finishedRef = useRef(false)
  const attachRef = useRef<((id: string, expected: number, attempt?: number) => void) | null>(null)
  const retryRef = useRef<number | null>(null)

  const attach = useCallback((id: string, expected: number, attempt = 0) => {
    abortRef.current?.abort()
    if (retryRef.current) window.clearTimeout(retryRef.current)
    const controller = new AbortController()
    abortRef.current = controller

    setJobId(id)
    setRunning(true)
    setTotal(expected)
    setCells([])
    // Журнал бэкенд переигрывает вместе с результатами — иначе строки задвоятся
    setLog([])
    setSummary(null)
    if (attempt === 0) finishedRef.current = false

    const retry = () => {
      if (finishedRef.current || controller.signal.aborted) return
      if (attempt >= STREAM_RETRIES) {
        setRunning(false)
        return
      }
      retryRef.current = window.setTimeout(
        () => attachRef.current?.(id, expected, attempt + 1),
        STREAM_RETRY_DELAY,
      )
    }

    streamNdjsonGet<XrayTestEvent>(
      xrayTestStreamUrl(id),
      event => {
        if (event.type === 'start') {
          setTotal(event.total)
        } else if (event.type === 'log') {
          setLog(prev => [...prev.slice(-400), event.line])
        } else if (event.type === 'cell') {
          const { type, done, ...cell } = event
          setCells(prev => [...prev, cell as XrayTestCell])
        } else if (event.type === 'done') {
          finishedRef.current = true
          setSummary({ ok: event.ok, degraded: event.degraded, fail: event.fail })
          setRunning(false)
          writeStoredJob(null)
          if (event.status === 'error' && event.error) toast.error(event.error)
        }
      },
      controller.signal,
    ).then(() => {
      // Поток кончился, а «готово» не пришло — значит оборвался по дороге
      retry()
    }).catch(error => {
      if (controller.signal.aborted || error instanceof StreamUnauthorizedError) return
      retry()
    })
  }, [])

  useEffect(() => {
    attachRef.current = attach
  }, [attach])

  const start = useCallback(async (request: XrayTestRunRequest) => {
    try {
      const { data } = await xrayTestApi.run(request)
      writeStoredJob({ jobId: data.job_id, total: data.total })
      setLog([])
      attach(data.job_id, data.total)
      return true
    } catch (error) {
      toast.error(extractError(error))
      return false
    }
  }, [attach])

  const cancel = useCallback(async () => {
    if (!jobId) return
    try {
      await xrayTestApi.cancel(jobId)
      setRunning(false)
      writeStoredJob(null)
    } catch {
      toast.error('Не удалось отменить проверку')
    }
  }, [jobId])

  const reset = useCallback(() => {
    abortRef.current?.abort()
    writeStoredJob(null)
    setJobId(null)
    setRunning(false)
    setCells([])
    setLog([])
    setSummary(null)
    setTotal(0)
  }, [])

  useEffect(() => {
    if (restoredRef.current) return
    restoredRef.current = true

    const stored = readStoredJob()
    if (!stored) return

    xrayTestApi.jobs()
      .then(({ data }) => {
        const job = data.jobs.find(item => item.job_id === stored.jobId)
        if (job) attach(stored.jobId, job.total)
        else writeStoredJob(null)
      })
      .catch(() => writeStoredJob(null))
  }, [attach])

  useEffect(() => () => abortRef.current?.abort(), [])

  return { jobId, running, total, cells, log, summary, start, cancel, reset }
}

export function extractError(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String((detail as { message: unknown }).message)
  }
  return (error as Error)?.message || 'Неизвестная ошибка'
}
