import { useState } from 'react'
import { motion } from 'framer-motion'
import { X, Loader2, Upload, KeyRound, AlertTriangle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { nodeImageApi, ImageDeliveryBulkResult } from '../../api/client'
import SshCredsFields, { SSH_CREDS_DEFAULTS, SshCredsValue, hasSshSecret, toDeliveryCreds } from './SshCredsFields'
import { Checkbox } from '../ui/Checkbox'

export interface BulkDeliveryServer {
  id: number
  name: string
  hasSshCreds: boolean
}

interface Props {
  servers: BulkDeliveryServer[]
  onStarted: () => void
  onClose: () => void
}

const MISSING_NAMES_SHOWN = 8

export default function BulkDeliverImageModal({ servers, onStarted, onClose }: Props) {
  const { t } = useTranslation()

  const [creds, setCreds] = useState<SshCredsValue>(SSH_CREDS_DEFAULTS)
  const [saveCreds, setSaveCreds] = useState(false)
  const [starting, setStarting] = useState(false)

  const withoutCreds = servers.filter(s => !s.hasSshCreds)
  const formFilled = hasSshSecret(creds)
  const willStart = servers.length - (formFilled ? 0 : withoutCreds.length)

  const reportResult = ({ started, skipped }: ImageDeliveryBulkResult) => {
    if (started.length > 0) toast.success(t('imageDelivery.bulk_started', { count: started.length }))
    if (skipped.length === 0) return
    const names = skipped
      .map(s => `${s.name ?? `#${s.server_id}`} — ${t(`imageDelivery.skip_${s.reason}`)}`)
      .join('\n')
    toast.warning(t('imageDelivery.bulk_skipped', { count: skipped.length }), {
      description: <div className="whitespace-pre-line">{names}</div>,
    })
  }

  const handleStart = async () => {
    setStarting(true)
    try {
      const body = formFilled ? toDeliveryCreds({ ...creds, host: '' }) : {}
      const { data } = await nodeImageApi.deliverBulk(servers.map(s => s.id), body, formFilled && saveCreds)
      reportResult(data)
      onStarted()
      onClose()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('imageDelivery.failed'))
    } finally {
      setStarting(false)
    }
  }

  const shownMissing = withoutCreds.slice(0, MISSING_NAMES_SHOWN).map(s => s.name).join(', ')
  const hiddenMissing = withoutCreds.length - MISSING_NAMES_SHOWN

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <motion.div
        className="card w-full max-w-2xl max-h-[90vh] flex flex-col overflow-y-auto"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
            <Upload className="w-5 h-5 text-accent-500" />
            {t('imageDelivery.bulk_title', { count: servers.length })}
          </h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        <p className="text-sm text-dark-400 mb-4">{t('imageDelivery.bulk_desc')}</p>

        <div className="flex items-center gap-2 text-sm text-dark-300 bg-dark-800/40 border border-dark-700/40 rounded-lg px-3 py-2 mb-4">
          <KeyRound className="w-4 h-4 text-success shrink-0" />
          {t('imageDelivery.bulk_with_creds', { count: servers.length - withoutCreds.length })}
        </div>

        {withoutCreds.length > 0 && (
          <div className="space-y-3 mb-4">
            <div className="flex items-start gap-2 text-sm text-warning bg-warning/10 border border-warning/20 rounded-lg px-3 py-2">
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
              <div>
                <div>{t('imageDelivery.bulk_without_creds', { count: withoutCreds.length })}</div>
                <div className="text-xs text-dark-400 mt-1 break-words">
                  {shownMissing}
                  {hiddenMissing > 0 && ` ${t('imageDelivery.bulk_and_more', { count: hiddenMissing })}`}
                </div>
              </div>
            </div>
            <p className="text-xs text-dark-400">{t('imageDelivery.bulk_form_hint')}</p>
            <SshCredsFields
              value={creds}
              onChange={patch => setCreds(prev => ({ ...prev, ...patch }))}
              disabled={starting}
              showHost={false}
            />
            <label className="flex items-center gap-2 text-sm text-dark-300 cursor-pointer">
              <Checkbox checked={saveCreds} onChange={(e) => setSaveCreds(e.target.checked)} disabled={starting || !formFilled} />
              {t('imageDelivery.save_creds')}
            </label>
          </div>
        )}

        <div className="flex items-center justify-end gap-2 mt-auto">
          <button onClick={onClose} className="btn btn-secondary">
            {t('common.close')}
          </button>
          <button onClick={handleStart} className="btn btn-primary" disabled={starting || willStart === 0}>
            {starting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
            {t('imageDelivery.bulk_start', { count: willStart })}
          </button>
        </div>
      </motion.div>
    </div>
  )
}
