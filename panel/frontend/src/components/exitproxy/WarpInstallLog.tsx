import { useEffect, useRef, useState } from 'react'
import { CheckCircle2, Loader2, X, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { type RemnawaveInstallEvent, warpInstallStreamUrl } from '../../api/client'
import { streamNdjsonGet, StreamUnauthorizedError } from '../../utils/ndjsonStream'

type Phase = 'running' | 'success' | 'error'

interface Props {
  jobId: string
  onFinished: (ok: boolean) => void
  onClose: () => void
}

// Лог установки WARP через агента — NDJSON-стрим, как у установки ноды Remnawave
export default function WarpInstallLog({ jobId, onFinished, onClose }: Props) {
  const { t } = useTranslation()
  const [lines, setLines] = useState<string[]>([])
  const [phase, setPhase] = useState<Phase>('running')
  const logRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    const controller = new AbortController()
    let finished = false
    let ok = false
    ;(async () => {
      try {
        await streamNdjsonGet<RemnawaveInstallEvent>(warpInstallStreamUrl(jobId), (ev) => {
          if (ev.type === 'log') setLines(prev => [...prev, ev.line])
          else if (ev.type === 'error') setLines(prev => [...prev, `[ERROR] ${ev.message}`])
          else if (ev.type === 'done') {
            finished = true
            ok = ev.status === 'success'
          }
        }, controller.signal)
      } catch (e) {
        if (!(e instanceof StreamUnauthorizedError) && !controller.signal.aborted) {
          setLines(prev => [...prev, `[ERROR] ${e instanceof Error ? e.message : String(e)}`])
        }
      }
      if (controller.signal.aborted) return
      // Тихий обрыв без done: установка продолжается на бэке, повторное открытие переподключит лог
      if (finished) {
        setPhase(ok ? 'success' : 'error')
        onFinished(ok)
      }
    })()
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [lines])

  return (
    <div className="rounded-lg border border-dark-700/50 bg-dark-900/60 p-3 space-y-2">
      <div className="flex items-center gap-2 text-xs">
        {phase === 'running' && <Loader2 className="w-3.5 h-3.5 animate-spin text-accent-400" />}
        {phase === 'success' && <CheckCircle2 className="w-3.5 h-3.5 text-green-400" />}
        {phase === 'error' && <XCircle className="w-3.5 h-3.5 text-red-400" />}
        <span className="text-dark-200 font-medium">
          {phase === 'running' ? t('exit_proxy.warp_installing') : phase === 'success' ? t('exit_proxy.warp_install_done') : t('exit_proxy.warp_install_failed')}
        </span>
        <button type="button" onClick={onClose} className="ml-auto text-dark-500 hover:text-dark-300">
          <X className="w-4 h-4" />
        </button>
      </div>
      <pre ref={logRef} className="text-[11px] font-mono text-dark-300 bg-dark-950/60 rounded-md p-2 max-h-56 overflow-auto whitespace-pre-wrap break-all">
        {lines.join('\n')}
      </pre>
    </div>
  )
}
