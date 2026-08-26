import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Globe, RefreshCw, Loader2, CheckCircle2, XCircle, AlertTriangle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { settingsApi, type TimeSyncStatus } from '../../api/client'
import { useSettingsStore, TIMEZONE_OPTIONS } from '../../stores/settingsStore'
import { SettingsSection } from './SettingsSection'
import { SettingRow } from './SettingRow'
import { Switch } from './Switch'

const STATUS_POLL_MS = 3000
const SERVER_TIMEZONES = TIMEZONE_OPTIONS.filter(o => o.value !== 'auto')

type SyncResult = TimeSyncStatus['last_results'][number]

function describeSyncResult(result: SyncResult): string {
  return [result.timezone || 'OK', result.ntp_service].filter(Boolean).join(' · ')
}

export function TimeSyncSection() {
  const { t } = useTranslation()
  const { serverTimezone, timeSyncEnabled, setServerTimezone, setTimeSyncEnabled } = useSettingsStore()
  const [status, setStatus] = useState<TimeSyncStatus | null>(null)
  const [syncing, setSyncing] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const selectId = useId()

  const stopPolling = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = null
  }, [])

  const fetchStatus = useCallback(async () => {
    try {
      const res = await settingsApi.timeSyncStatus()
      setStatus(res.data)
      return res.data
    } catch {
      return null
    }
  }, [])

  const startPolling = useCallback(() => {
    if (pollRef.current) return
    setSyncing(true)
    pollRef.current = setInterval(async () => {
      const current = await fetchStatus()
      if (current?.sync_in_progress) return
      stopPolling()
      setSyncing(false)
      if (!current) return
      const ok = current.last_results.filter(r => r.success).length
      const total = current.last_results.length
      if (ok === total) {
        toast.success(t('settings.sync_success'))
      } else {
        toast.warning(t('settings.sync_partial', { count: ok, total }))
      }
    }, STATUS_POLL_MS)
  }, [fetchStatus, stopPolling, t])

  useEffect(() => {
    // Синхронизация, запущенная до размонтирования (смена вкладки), подхватывается заново
    fetchStatus().then(current => {
      if (current?.sync_in_progress) startPolling()
    })
    return stopPolling
  }, [fetchStatus, startPolling, stopPolling])

  const handleSync = async () => {
    setSyncing(true)
    try {
      await settingsApi.timeSyncRun()
      toast.success(t('settings.sync_started'))
      startPolling()
    } catch (err: any) {
      setSyncing(false)
      toast.error(err.response?.status === 409 ? t('settings.sync_in_progress') : t('common.error'))
    }
  }

  return (
    <SettingsSection icon={Globe} title={t('settings.time_sync')} description={t('settings.time_sync_desc')} faq="SETTINGS_TIME_SYNC">
      <SettingRow label={t('settings.time_sync_enabled')}>
        <Switch checked={timeSyncEnabled} onChange={setTimeSyncEnabled} />
      </SettingRow>

      {timeSyncEnabled && (
        <>
          <SettingRow label={t('settings.server_timezone')} hint={t('settings.server_timezone_hint')} htmlFor={selectId}>
            <select
              id={selectId}
              className="input py-2 text-sm sm:w-64"
              value={serverTimezone}
              onChange={e => setServerTimezone(e.target.value)}
            >
              {SERVER_TIMEZONES.map(option => (
                <option key={option.value} value={option.value}>{option.label} ({option.offset})</option>
              ))}
            </select>
          </SettingRow>

          <SettingRow label={t('settings.last_sync')} hint={status?.last_sync ? new Date(status.last_sync).toLocaleString() : t('settings.never')}>
            <button onClick={handleSync} disabled={syncing} className="btn btn-secondary text-sm">
              {syncing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
              {syncing ? t('settings.sync_in_progress') : t('settings.sync_now')}
            </button>
          </SettingRow>

          {status && status.last_results.length > 0 && (
            <div className="mt-4 p-3 bg-dark-800/50 rounded-xl border border-dark-700/50 space-y-1">
              {status.last_results.map((r, i) => (
                <div key={i} className="flex items-center justify-between gap-3 text-xs">
                  <span className="text-dark-400 truncate">{r.name}</span>
                  <span className="flex items-center gap-1 shrink-0">
                    {r.success
                      ? <CheckCircle2 className="w-3.5 h-3.5 text-success" />
                      : <XCircle className="w-3.5 h-3.5 text-danger" />}
                    <span className={r.success ? 'text-success' : 'text-danger'}>
                      {r.success ? describeSyncResult(r) : (r.error || 'Error')}
                    </span>
                    {r.success && r.ntp_synchronized === false && (
                      <span className="flex items-center gap-1 text-warning">
                        <AlertTriangle className="w-3.5 h-3.5" />
                        {t('settings.ntp_not_synced')}
                      </span>
                    )}
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </SettingsSection>
  )
}
