import { useCallback, useEffect, useRef, useState } from 'react'
import { isAxiosError, type AxiosResponse } from 'axios'
import { toast } from 'sonner'
import type { ProfileRollout } from '../api/client'

const POLL_INTERVAL_MS = 2000

export type RolloutProgress = Pick<ProfileRollout<unknown>, 'done' | 'total'>

// Запуск уже запрошен, бэкенд ещё не ответил
const STARTING: RolloutProgress = { done: 0, total: null }

interface ProfileRolloutOptions<R> {
  fetchState: (profileId: number) => Promise<AxiosResponse<{ job: ProfileRollout<R> | null }>>
  /** Раскатка дошла до конца — страница показывает свой итог по серверам */
  onCompleted: (results: R[]) => void
  /** Слежение закончилось любым исходом — пора перечитать профиль и список */
  onFinished: () => void
  failedText: string
  lostText: string
}

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const progressOf = (job: ProfileRollout<unknown>): RolloutProgress => ({ done: job.done, total: job.total })

function errorDetail(err: unknown): string | null {
  const detail = isAxiosError(err) ? err.response?.data?.detail : null
  return typeof detail === 'string' ? detail : null
}

/**
 * «Синхронизировать всё» в профиле: раскатка идёт на бэкенде фоновой задачей и
 * не упирается в таймаут запроса. Хук запускает её, опрашивает ход до завершения
 * и подхватывает раскатку, которая уже шла, когда профиль открыли.
 *
 * rollout — ход идущей раскатки, null — раскатки нет.
 */
export function useProfileRollout<R>(profileId: number, options: ProfileRolloutOptions<R>) {
  const [rollout, setRollout] = useState<RolloutProgress | null>(null)
  const optionsRef = useRef(options)
  optionsRef.current = options
  // Поколение слежения: смена профиля, размонтирование и новый запуск его сдвигают —
  // прежний цикл опроса видит это и выходит, не трогая состояние
  const generationRef = useRef(0)

  const track = useCallback(async (jobId: string, generation: number) => {
    for (;;) {
      await sleep(POLL_INTERVAL_MS)
      if (generation !== generationRef.current) return

      let job: ProfileRollout<R> | null
      try {
        job = (await optionsRef.current.fetchState(profileId)).data.job
      } catch {
        // Сбой опроса — не сбой раскатки: она идёт на бэкенде, спрашиваем дальше
        continue
      }
      if (generation !== generationRef.current) return

      if (job?.job_id === jobId && job.status === 'running') {
        setRollout(progressOf(job))
        continue
      }

      setRollout(null)
      const { onCompleted, onFinished, failedText, lostText } = optionsRef.current
      // Раскатки живут в памяти бэкенда: после его перезапуска нашей там уже нет
      if (job?.job_id !== jobId) toast.warning(lostText)
      else if (job.status === 'completed') onCompleted(job.results ?? [])
      else toast.error(job.error || failedText)
      onFinished()
      return
    }
  }, [profileId])

  // Подхватывает раскатку, запущенную не отсюда: до перезагрузки страницы,
  // из другой вкладки или запросом, ответ на который до нас не дошёл
  const attach = useCallback(async (generation: number) => {
    let job: ProfileRollout<R> | null = null
    try {
      job = (await optionsRef.current.fetchState(profileId)).data.job
    } catch {
      // Состояние узнать не удалось — кнопку запуска не блокируем
    }
    if (generation !== generationRef.current) return

    if (job?.status !== 'running') {
      setRollout(null)
      return
    }
    setRollout(progressOf(job))
    track(job.job_id, generation)
  }, [profileId, track])

  useEffect(() => {
    const generation = ++generationRef.current
    // Профиль сменился: ход прежней раскатки к нему не относится
    setRollout(null)
    attach(generation)
    return () => { generationRef.current++ }
  }, [attach])

  // request — сам POST запуска. Возвращается, как только бэкенд принял или отверг
  // запуск; слежение за ходом продолжается в фоне
  const start = useCallback(async (request: () => Promise<AxiosResponse<{ job: ProfileRollout<R> }>>) => {
    const generation = ++generationRef.current
    setRollout(STARTING)
    try {
      const { job } = (await request()).data
      if (generation !== generationRef.current) return
      setRollout(progressOf(job))
      track(job.job_id, generation)
    } catch (err) {
      if (generation !== generationRef.current) return
      toast.error(errorDetail(err) ?? optionsRef.current.failedText)
      // Отказ «раскатка уже идёт» (409) или ответ, потерянный по дороге:
      // задача на бэкенде может идти — тогда следим за ней
      await attach(generation)
    }
  }, [attach, track])

  return { rollout, start }
}
