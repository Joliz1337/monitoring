import { useState, useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { X, Loader2, Upload, CheckCircle2, XCircle, RotateCcw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  nodeImageApi,
  imageDeliveryJobStreamUrl,
  NodeImageDeliveryEvent,
} from '../../api/client'
import { streamNdjsonGet } from '../../utils/ndjsonStream'
import SshCredsFields, {
  SSH_CREDS_DEFAULTS, SshCredsValue, credsFromSettings, hasStoredSshSecret, toDeliveryCreds,
} from './SshCredsFields'
import { Checkbox } from '../ui/Checkbox'

interface Props {
  serverId: number
  serverName: string
  /** Идущая или недавно завершённая доставка по серверу — окно сразу открывается на её логе */
  jobId: string | null
  onStarted: () => void
  onClose: () => void
}

export default function DeliverImageModal({ serverId, serverName, jobId: initialJobId, onStarted, onClose }: Props) {
  const { t } = useTranslation()

  const [creds, setCreds] = useState<SshCredsValue>(SSH_CREDS_DEFAULTS)
  const [hasStoredCreds, setHasStoredCreds] = useState(false)
  const [saveCreds, setSaveCreds] = useState(false)
  const [editCreds, setEditCreds] = useState(false)

  const [jobId, setJobId] = useState<string | null>(initialJobId)
  const [starting, setStarting] = useState(false)
  const [log, setLog] = useState<string[]>([])
  const [result, setResult] = useState<'success' | 'error' | null>(null)

  const logEndRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    nodeImageApi
      .getSettings(serverId)
      .then(({ data }) => {
        setCreds(credsFromSettings(data))
        setHasStoredCreds(hasStoredSshSecret(data))
      })
      .catch(() => {})
  }, [serverId])

  // Стрим переигрывает весь лог задачи, поэтому повторное открытие окна показывает его целиком
  useEffect(() => {
    if (!jobId) return
    const controller = new AbortController()
    setLog([])
    setResult(null)
    streamNdjsonGet<NodeImageDeliveryEvent>(
      imageDeliveryJobStreamUrl(jobId),
      (ev) => {
        if (ev.type === 'log') setLog(prev => [...prev, ev.line])
        else if (ev.type === 'error') setLog(prev => [...prev, '✗ ' + ev.message])
        else if (ev.type === 'done') setResult(ev.status)
      },
      controller.signal,
    ).catch((err: Error) => {
      if (controller.signal.aborted) return
      setLog(prev => [...prev, '✗ ' + (err.message || t('imageDelivery.failed'))])
    })
    return () => controller.abort()
  }, [jobId, t])

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [log])

  const handleForgetCreds = async () => {
    try {
      await nodeImageApi.setSettings(serverId, { ssh_password: '', ssh_private_key: '', ssh_passphrase: '' })
      setHasStoredCreds(false)
      setEditCreds(true)
      toast.success(t('imageDelivery.creds_forgotten'))
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('imageDelivery.failed'))
    }
  }

  const handleDeliver = async () => {
    setStarting(true)
    const useForm = !hasStoredCreds || editCreds
    const body = useForm ? toDeliveryCreds(creds) : {}
    try {
      if (useForm && saveCreds) {
        await nodeImageApi.setSettings(serverId, { image_delivery: 'ssh', ...body })
      }
      const { data } = await nodeImageApi.deliver(serverId, body)
      setJobId(data.job_id)
      onStarted()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || err?.message || t('imageDelivery.failed'))
    } finally {
      setStarting(false)
    }
  }

  const running = jobId !== null && result === null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <motion.div
        className="card w-full max-w-2xl max-h-[90vh] flex flex-col"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
            <Upload className="w-5 h-5 text-accent-500" />
            {t('imageDelivery.title')} — <span className="text-dark-300">{serverName}</span>
          </h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        {jobId ? (
          <div className="flex-1 min-h-[160px] max-h-[360px] overflow-y-auto bg-dark-900/60 border border-dark-700/40 rounded-lg p-3 font-mono text-xs text-dark-300 mb-4">
            {log.map((line, i) => (
              <div key={i} className="whitespace-pre-wrap break-all">{line}</div>
            ))}
            <div ref={logEndRef} />
          </div>
        ) : (
          <>
            <p className="text-sm text-dark-400 mb-4">{t('imageDelivery.desc')}</p>
            {(hasStoredCreds && !editCreds) ? (
              <div className="mb-4 space-y-2">
                <div className="text-sm text-dark-300 bg-dark-800/40 border border-dark-700/40 rounded-lg px-3 py-2">
                  {t('imageDelivery.using_stored', { user: creds.user, host: creds.host, port: creds.port })}
                </div>
                <div className="flex gap-4 text-xs">
                  <button type="button" onClick={() => setEditCreds(true)} className="text-accent-400 hover:underline">
                    {t('imageDelivery.change_creds')}
                  </button>
                  <button type="button" onClick={handleForgetCreds} className="text-danger hover:underline">
                    {t('imageDelivery.forget_creds')}
                  </button>
                </div>
              </div>
            ) : (
              <div className="space-y-3 mb-4">
                <SshCredsFields
                  value={creds}
                  onChange={patch => setCreds(prev => ({ ...prev, ...patch }))}
                  disabled={starting}
                />
                <label className="flex items-center gap-2 text-sm text-dark-300 cursor-pointer">
                  <Checkbox checked={saveCreds} onChange={(e) => setSaveCreds(e.target.checked)} disabled={starting} />
                  {t('imageDelivery.save_creds')}
                </label>
              </div>
            )}
          </>
        )}

        <div className="flex items-center justify-between gap-3 mt-auto">
          <div className="text-sm min-w-0">
            {running && (
              <span className="flex items-center gap-1.5 text-dark-400">
                <Loader2 className="w-4 h-4 animate-spin shrink-0" />{t('imageDelivery.background_hint')}
              </span>
            )}
            {result === 'success' && (
              <span className="flex items-center gap-1.5 text-success"><CheckCircle2 className="w-4 h-4" />{t('imageDelivery.done')}</span>
            )}
            {result === 'error' && (
              <span className="flex items-center gap-1.5 text-danger"><XCircle className="w-4 h-4" />{t('imageDelivery.error')}</span>
            )}
          </div>
          <div className="flex gap-2 shrink-0">
            <button onClick={onClose} className="btn btn-secondary">
              {t('common.close')}
            </button>
            {!jobId && (
              <button onClick={handleDeliver} className="btn btn-primary" disabled={starting}>
                {starting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
                {t('imageDelivery.deliver')}
              </button>
            )}
            {jobId && result !== null && (
              <button onClick={() => setJobId(null)} className="btn btn-primary">
                <RotateCcw className="w-4 h-4" />
                {t('imageDelivery.deliver_again')}
              </button>
            )}
          </div>
        </div>
      </motion.div>
    </div>
  )
}
