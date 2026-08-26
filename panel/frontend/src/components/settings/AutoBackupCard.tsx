import { useEffect, useId, useState } from 'react'
import { Send, Loader2, Save, CheckCircle2, XCircle, CalendarClock } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { backupApi, BackupAutoSettings, BackupAutoSettingsIn } from '../../api/client'
import { SettingsSection } from './SettingsSection'
import { SettingRow } from './SettingRow'
import { Switch } from './Switch'

export default function AutoBackupCard() {
  const { t } = useTranslation()
  const scheduleSelectId = useId()

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
    <SettingsSection icon={CalendarClock} title={t('backup.auto.title')} description={t('backup.auto.desc')}>
      <SettingRow label={t('backup.auto.enabled')} hint={enabled ? t('backup.auto.state_on') : t('backup.auto.state_off')}>
        <Switch checked={enabled} onChange={setEnabled} />
      </SettingRow>

      <SettingRow label={t('backup.auto.schedule')} htmlFor={scheduleSelectId}>
        <div className="flex flex-wrap items-center gap-2">
          <select
            id={scheduleSelectId}
            className="input py-2 text-sm w-auto"
            value={scheduleKind}
            onChange={e => setScheduleKind(e.target.value as 'daily' | 'every_hours')}
          >
            <option value="daily">{t('backup.auto.daily')}</option>
            <option value="every_hours">{t('backup.auto.every_hours')}</option>
          </select>
          {scheduleKind === 'daily' ? (
            <>
              <input className="input py-2 text-sm w-24" value={atTime} onChange={e => setAtTime(e.target.value)} placeholder="04:00" />
              <span className="text-xs text-dark-500">{t('backup.auto.time_utc')}</span>
            </>
          ) : (
            <>
              <input className="input py-2 text-sm w-20" type="number" min={1} value={everyHours} onChange={e => setEveryHours(Number(e.target.value) || 24)} />
              <span className="text-xs text-dark-500">{t('backup.auto.hours')}</span>
            </>
          )}
        </div>
      </SettingRow>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-4 border-t border-dark-800/50">
        <div>
          <label className="block text-xs text-dark-400 mb-1">{t('backup.auto.bot_token')}</label>
          <input className="input py-2 text-sm" placeholder={hasToken ? t('backup.auto.token_set') : ''} value={botToken} onChange={e => setBotToken(e.target.value)} />
        </div>
        <div>
          <label className="block text-xs text-dark-400 mb-1">{t('backup.auto.chat_id')}</label>
          <input className="input py-2 text-sm" placeholder="-100..." value={chatId} onChange={e => setChatId(e.target.value)} />
        </div>
        <div>
          <label className="block text-xs text-dark-400 mb-1">{t('backup.auto.password')}</label>
          <input className="input py-2 text-sm" type="password" placeholder={hasPassword ? t('backup.auto.password_set') : ''} value={password} onChange={e => setPassword(e.target.value)} />
        </div>
        <div>
          <label className="block text-xs text-dark-400 mb-1">{t('backup.auto.volume_mb')}</label>
          <input className="input py-2 text-sm" type="number" min={1} max={49} value={volumeMb} onChange={e => setVolumeMb(Number(e.target.value) || 45)} />
        </div>
      </div>

      {lastRunAt && (
        <div className="text-xs flex items-center gap-1.5 mt-4">
          {lastStatus === 'ok' ? (
            <span className="text-success flex items-center gap-1"><CheckCircle2 className="w-3.5 h-3.5" />{t('backup.auto.last_ok')}</span>
          ) : (
            <span className="text-danger flex items-center gap-1"><XCircle className="w-3.5 h-3.5" />{lastError || t('backup.auto.last_error')}</span>
          )}
          <span className="text-dark-500">· {new Date(lastRunAt).toLocaleString()}</span>
        </div>
      )}

      <p className="text-xs text-dark-500 mt-4">{t('backup.auto.note')}</p>

      <div className="flex flex-wrap gap-2 mt-4">
        <button className="btn btn-primary text-sm" onClick={save} disabled={saving}>
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          {t('common.save')}
        </button>
        <button className="btn btn-secondary text-sm" onClick={testTelegram} disabled={testing}>
          {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
          {t('backup.auto.test')}
        </button>
        <button className="btn btn-secondary text-sm" onClick={runNow} disabled={running}>
          {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
          {t('backup.auto.run_now')}
        </button>
      </div>
    </SettingsSection>
  )
}
