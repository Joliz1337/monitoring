import { useState, useEffect } from 'react'
import { Send, Loader2, Save, CheckCircle2, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { backupApi, BackupAutoSettings, BackupAutoSettingsIn } from '../../api/client'

export default function AutoBackupCard() {
  const { t } = useTranslation()

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState(false)
  const [testing, setTesting] = useState(false)

  const [enabled, setEnabled] = useState(false)
  const [scheduleKind, setScheduleKind] = useState<'daily' | 'every_hours'>('daily')
  const [atTime, setAtTime] = useState('04:00')
  const [everyHours, setEveryHours] = useState(24)
  const [chatId, setChatId] = useState('')
  const [volumeMb, setVolumeMb] = useState(45)
  const [botToken, setBotToken] = useState('')
  const [password, setPassword] = useState('')
  const [hasToken, setHasToken] = useState(false)
  const [hasPassword, setHasPassword] = useState(false)
  const [lastRunAt, setLastRunAt] = useState<string | null>(null)
  const [lastStatus, setLastStatus] = useState<string | null>(null)
  const [lastError, setLastError] = useState<string | null>(null)

  const apply = (s: BackupAutoSettings) => {
    setEnabled(s.enabled)
    setScheduleKind(s.schedule_kind)
    setAtTime(s.at_time || '04:00')
    setEveryHours(s.every_hours || 24)
    setChatId(s.chat_id || '')
    setVolumeMb(s.volume_size_mb || 45)
    setHasToken(s.has_bot_token)
    setHasPassword(s.has_archive_password)
    setLastRunAt(s.last_run_at)
    setLastStatus(s.last_status)
    setLastError(s.last_error)
  }

  useEffect(() => {
    backupApi
      .getAutoSettings()
      .then(({ data }) => apply(data))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const buildPayload = (): BackupAutoSettingsIn => {
    const p: BackupAutoSettingsIn = {
      enabled,
      schedule_kind: scheduleKind,
      at_time: atTime,
      every_hours: everyHours,
      chat_id: chatId,
      volume_size_mb: volumeMb,
    }
    if (botToken) p.bot_token = botToken
    if (password) p.archive_password = password
    return p
  }

  const save = async () => {
    setSaving(true)
    try {
      await backupApi.updateAutoSettings(buildPayload())
      setBotToken('')
      setPassword('')
      const { data } = await backupApi.getAutoSettings()
      apply(data)
      toast.success(t('backup.auto.saved'))
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('backup.auto.save_failed'))
    } finally {
      setSaving(false)
    }
  }

  const testTelegram = async () => {
    setTesting(true)
    try {
      // сначала сохраняем текущие креды, затем шлём тест
      await backupApi.updateAutoSettings(buildPayload())
      setBotToken('')
      setPassword('')
      await backupApi.testTelegram()
      toast.success(t('backup.auto.test_ok'))
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('backup.auto.test_failed'))
    } finally {
      setTesting(false)
    }
  }

  const runNow = async () => {
    setRunning(true)
    try {
      await backupApi.telegramNow()
      toast.success(t('backup.auto.run_started'))
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('backup.auto.run_failed'))
    } finally {
      setRunning(false)
    }
  }

  if (loading) return null

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-base font-semibold text-dark-100">{t('backup.auto.title')}</h3>
          <p className="text-sm text-dark-400 mt-0.5">{t('backup.auto.desc')}</p>
        </div>
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span className="text-sm text-dark-300">{t('backup.auto.enabled')}</span>
        </label>
      </div>

      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <select className="input" value={scheduleKind} onChange={(e) => setScheduleKind(e.target.value as any)}>
            <option value="daily">{t('backup.auto.daily')}</option>
            <option value="every_hours">{t('backup.auto.every_hours')}</option>
          </select>
          {scheduleKind === 'daily' ? (
            <input className="input w-28" value={atTime} onChange={(e) => setAtTime(e.target.value)} placeholder="04:00" />
          ) : (
            <input className="input w-28" type="number" min={1} value={everyHours} onChange={(e) => setEveryHours(Number(e.target.value) || 24)} />
          )}
          <span className="text-xs text-dark-500">{t('backup.auto.time_utc')}</span>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <input className="input" placeholder={hasToken ? t('backup.auto.token_set') : t('backup.auto.bot_token')} value={botToken} onChange={(e) => setBotToken(e.target.value)} />
          <input className="input" placeholder={t('backup.auto.chat_id')} value={chatId} onChange={(e) => setChatId(e.target.value)} />
          <input className="input" type="password" placeholder={hasPassword ? t('backup.auto.password_set') : t('backup.auto.password')} value={password} onChange={(e) => setPassword(e.target.value)} />
          <input className="input" type="number" min={1} max={49} value={volumeMb} onChange={(e) => setVolumeMb(Number(e.target.value) || 45)} title={t('backup.auto.volume_mb')} />
        </div>

        {lastRunAt && (
          <div className="text-xs flex items-center gap-1.5">
            {lastStatus === 'ok' ? (
              <span className="text-success flex items-center gap-1"><CheckCircle2 className="w-3.5 h-3.5" />{t('backup.auto.last_ok')}</span>
            ) : (
              <span className="text-danger flex items-center gap-1"><XCircle className="w-3.5 h-3.5" />{lastError || t('backup.auto.last_error')}</span>
            )}
            <span className="text-dark-500">· {new Date(lastRunAt).toLocaleString()}</span>
          </div>
        )}

        <p className="text-xs text-dark-500">{t('backup.auto.note')}</p>

        <div className="flex flex-wrap gap-2 pt-1">
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            {t('common.save')}
          </button>
          <button className="btn btn-secondary" onClick={testTelegram} disabled={testing}>
            {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            {t('backup.auto.test')}
          </button>
          <button className="btn btn-secondary" onClick={runNow} disabled={running}>
            {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            {t('backup.auto.run_now')}
          </button>
        </div>
      </div>
    </div>
  )
}
