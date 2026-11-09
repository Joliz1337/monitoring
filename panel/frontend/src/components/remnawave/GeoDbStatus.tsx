import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Database, Loader2, RefreshCw } from 'lucide-react'
import { remnawaveApi, type RemnawaveGeoStatus } from '../../api/client'

const POLL_WHILE_UPDATING_MS = 3000

export default function GeoDbStatus() {
  const { t } = useTranslation()
  const [status, setStatus] = useState<RemnawaveGeoStatus | null>(null)

  const fetchStatus = useCallback(async () => {
    try {
      const res = await remnawaveApi.getGeoStatus()
      setStatus(res.data)
    } catch { /* статус базы — справочный, без него настройки работают */ }
  }, [])

  useEffect(() => { fetchStatus() }, [fetchStatus])

  // Пока база качается, статус опрашивается, чтобы показать результат без перезагрузки
  useEffect(() => {
    if (!status?.updating) return
    const timer = window.setTimeout(fetchStatus, POLL_WHILE_UPDATING_MS)
    return () => window.clearTimeout(timer)
  }, [status, fetchStatus])

  const startUpdate = async () => {
    try {
      await remnawaveApi.updateGeoDb()
      setStatus(s => (s ? { ...s, updating: true } : s))
      window.setTimeout(fetchStatus, POLL_WHILE_UPDATING_MS)
    } catch { toast.error('Error') }
  }

  if (!status) return null

  const stateText = status.updating
    ? t('remnawave.geo.settings_geo_updating')
    : status.ready
      ? t('remnawave.geo.settings_geo_ready', { month: status.month ?? '—' })
      : t('remnawave.geo.settings_geo_missing')

  return (
    <div className="rounded-xl border border-dark-800/60 bg-dark-900/40 p-3 space-y-2">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm text-dark-200 whitespace-nowrap">
          <Database className="w-4 h-4 text-dark-400" />
          {t('remnawave.geo.settings_geo_title')}
        </div>
        <button
          type="button"
          onClick={startUpdate}
          disabled={status.updating}
          className="btn btn-secondary text-xs !px-2.5 !py-1 whitespace-nowrap shrink-0"
        >
          {status.updating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          {t('remnawave.geo.settings_geo_update')}
        </button>
      </div>
      <div className={`text-xs ${status.ready ? 'text-dark-300' : 'text-warning'}`}>{stateText}</div>
      {status.error && (
        <div className="text-xs text-danger break-words">{t('remnawave.geo.settings_geo_error', { error: status.error })}</div>
      )}
      <p className="text-xs text-dark-500">{t('remnawave.geo.settings_geo_hint')}</p>
    </div>
  )
}
