import { useState, useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { X, Loader2, Upload, CheckCircle2, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  nodeImageApi,
  imageDeliveryJobStreamUrl,
  ImageDeliveryCreds,
  NodeImageDeliveryEvent,
} from '../../api/client'
import { streamNdjsonGet } from '../../utils/ndjsonStream'

interface Props {
  serverId: number
  serverName: string
  onClose: () => void
}

export default function DeliverImageModal({ serverId, serverName, onClose }: Props) {
  const { t } = useTranslation()

  const [host, setHost] = useState('')
  const [port, setPort] = useState('22')
  const [user, setUser] = useState('root')
  const [authMethod, setAuthMethod] = useState<'password' | 'key'>('password')
  const [password, setPassword] = useState('')
  const [privateKey, setPrivateKey] = useState('')
  const [passphrase, setPassphrase] = useState('')
  const [hasStoredCreds, setHasStoredCreds] = useState(false)
  const [saveCreds, setSaveCreds] = useState(false)

  const [delivering, setDelivering] = useState(false)
  const [log, setLog] = useState<string[]>([])
  const [result, setResult] = useState<'success' | 'error' | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const logEndRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    nodeImageApi
      .getSettings(serverId)
      .then(({ data }) => {
        setHost(data.ssh_host || '')
        setPort(String(data.ssh_port || 22))
        setUser(data.ssh_user || 'root')
        setHasStoredCreds(data.has_ssh_password || data.has_ssh_private_key)
        if (data.has_ssh_private_key) setAuthMethod('key')
      })
      .catch(() => {})
  }, [serverId])

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [log])

  useEffect(() => () => abortRef.current?.abort(), [])

  const handleDeliver = async () => {
    setDelivering(true)
    setResult(null)
    setLog([])

    const creds: ImageDeliveryCreds = {}
    if (!hasStoredCreds) {
      creds.ssh_host = host.trim()
      creds.ssh_port = Number(port) || 22
      creds.ssh_user = user.trim() || 'root'
      if (authMethod === 'password') {
        creds.ssh_password = password
      } else {
        creds.ssh_private_key = privateKey
        if (passphrase) creds.ssh_passphrase = passphrase
      }
    }

    try {
      if (!hasStoredCreds && saveCreds) {
        await nodeImageApi.setSettings(serverId, { image_delivery: 'ssh', ...creds })
      }
      const { data } = await nodeImageApi.deliver(serverId, creds)
      const controller = new AbortController()
      abortRef.current = controller
      await streamNdjsonGet<NodeImageDeliveryEvent>(
        imageDeliveryJobStreamUrl(data.job_id),
        (ev) => {
          if (ev.type === 'log') {
            setLog((prev) => [...prev, ev.line])
          } else if (ev.type === 'error') {
            setLog((prev) => [...prev, '✗ ' + ev.message])
            setResult('error')
          } else if (ev.type === 'done') {
            setResult((prev) => (prev === 'error' ? 'error' : 'success'))
          }
        },
        controller.signal,
      )
    } catch (err: any) {
      const msg = err?.response?.data?.detail || err?.message || t('imageDelivery.failed')
      setLog((prev) => [...prev, '✗ ' + msg])
      setResult('error')
      toast.error(msg)
    } finally {
      setDelivering(false)
    }
  }

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

        <p className="text-sm text-dark-400 mb-4">{t('imageDelivery.desc')}</p>

        {hasStoredCreds ? (
          <div className="text-sm text-dark-300 bg-dark-800/40 border border-dark-700/40 rounded-lg px-3 py-2 mb-4">
            {t('imageDelivery.using_stored', { user, host, port })}
          </div>
        ) : (
          <div className="space-y-3 mb-4">
            <div className="grid grid-cols-3 gap-3">
              <div className="col-span-2">
                <label className="block text-xs text-dark-400 mb-1">{t('imageDelivery.host')}</label>
                <input className="input w-full" value={host} onChange={(e) => setHost(e.target.value)} disabled={delivering} />
              </div>
              <div>
                <label className="block text-xs text-dark-400 mb-1">{t('imageDelivery.port')}</label>
                <input className="input w-full" value={port} onChange={(e) => setPort(e.target.value.replace(/\D/g, ''))} disabled={delivering} />
              </div>
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('imageDelivery.user')}</label>
              <input className="input w-full" value={user} onChange={(e) => setUser(e.target.value)} disabled={delivering} />
            </div>
            <div className="flex gap-4 text-sm">
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input type="radio" checked={authMethod === 'password'} onChange={() => setAuthMethod('password')} disabled={delivering} />
                {t('imageDelivery.auth_password')}
              </label>
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input type="radio" checked={authMethod === 'key'} onChange={() => setAuthMethod('key')} disabled={delivering} />
                {t('imageDelivery.auth_key')}
              </label>
            </div>
            {authMethod === 'password' ? (
              <input type="password" className="input w-full" placeholder={t('imageDelivery.password')} value={password} onChange={(e) => setPassword(e.target.value)} disabled={delivering} />
            ) : (
              <>
                <textarea className="input w-full font-mono text-xs" rows={4} placeholder={t('imageDelivery.private_key')} value={privateKey} onChange={(e) => setPrivateKey(e.target.value)} disabled={delivering} />
                <input type="password" className="input w-full" placeholder={t('imageDelivery.passphrase')} value={passphrase} onChange={(e) => setPassphrase(e.target.value)} disabled={delivering} />
              </>
            )}
            <label className="flex items-center gap-2 text-sm text-dark-300 cursor-pointer">
              <input type="checkbox" checked={saveCreds} onChange={(e) => setSaveCreds(e.target.checked)} disabled={delivering} />
              {t('imageDelivery.save_creds')}
            </label>
          </div>
        )}

        {log.length > 0 && (
          <div className="flex-1 min-h-[120px] max-h-[280px] overflow-y-auto bg-dark-900/60 border border-dark-700/40 rounded-lg p-3 font-mono text-xs text-dark-300 mb-4">
            {log.map((line, i) => (
              <div key={i} className="whitespace-pre-wrap break-all">{line}</div>
            ))}
            <div ref={logEndRef} />
          </div>
        )}

        <div className="flex items-center justify-between mt-auto">
          <div className="text-sm">
            {result === 'success' && (
              <span className="flex items-center gap-1.5 text-success"><CheckCircle2 className="w-4 h-4" />{t('imageDelivery.done')}</span>
            )}
            {result === 'error' && (
              <span className="flex items-center gap-1.5 text-danger"><XCircle className="w-4 h-4" />{t('imageDelivery.error')}</span>
            )}
          </div>
          <div className="flex gap-2">
            <button onClick={onClose} className="btn btn-secondary" disabled={delivering}>
              {t('common.close')}
            </button>
            <button onClick={handleDeliver} className="btn btn-primary" disabled={delivering}>
              {delivering ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
              {t('imageDelivery.deliver')}
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  )
}
