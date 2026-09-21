import { useEffect, useState } from 'react'
import { Loader2, Save, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import type { ExitProxySettings } from '../../api/client'
import { useExitProxyStore } from '../../stores/exitProxyStore'
import { getFlag } from '../../utils/format'
import { Toggle } from '../ui/Toggle'

interface Form {
  enabled: boolean
  check_interval_min: number
  port: number
  blocked_countries: string[]
  notify_enabled: boolean
}

const COUNTRY_RE = /^[A-Z]{2}$/

function toForm(settings: ExitProxySettings): Form {
  return {
    enabled: settings.enabled,
    check_interval_min: settings.check_interval_min,
    port: settings.port,
    blocked_countries: settings.blocked_countries,
    notify_enabled: settings.notify_enabled,
  }
}

export default function SettingsTab() {
  const { t } = useTranslation()
  const settings = useExitProxyStore(s => s.settings)
  const saveSettings = useExitProxyStore(s => s.saveSettings)
  const saving = useExitProxyStore(s => s.isBusy('settings-save'))
  const [form, setForm] = useState<Form | null>(settings ? toForm(settings) : null)
  const [country, setCountry] = useState('')

  useEffect(() => {
    if (settings) setForm(toForm(settings))
  }, [settings])

  if (!form || !settings) return null

  const portChanged = form.port !== settings.port

  const addCountry = () => {
    const code = country.trim().toUpperCase()
    if (!COUNTRY_RE.test(code)) {
      toast.error(t('exit_proxy.country_invalid'))
      return
    }
    if (!form.blocked_countries.includes(code)) {
      setForm({ ...form, blocked_countries: [...form.blocked_countries, code] })
    }
    setCountry('')
  }

  const save = () => {
    if (portChanged && !window.confirm(t('exit_proxy.confirm_port_change'))) return
    saveSettings(form)
  }

  return (
    <div className="card p-5 space-y-5 max-w-3xl">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-dark-200">{t('exit_proxy.enable')}</p>
          <p className="text-xs text-dark-500">{t('exit_proxy.enable_hint')}</p>
        </div>
        <Toggle on={form.enabled} onClick={() => setForm({ ...form, enabled: !form.enabled })} />
      </div>

      <div className="grid sm:grid-cols-2 gap-4">
        <div>
          <label className="text-sm text-dark-400">{t('exit_proxy.interval')}</label>
          <input
            type="number"
            min={1}
            max={1440}
            value={form.check_interval_min}
            onChange={e => setForm({ ...form, check_interval_min: Number(e.target.value) })}
            className="input w-full mt-1"
          />
          <p className="text-xs text-dark-500 mt-1">{t('exit_proxy.interval_hint')}</p>
        </div>
        <div>
          <label className="text-sm text-dark-400">{t('exit_proxy.port')}</label>
          <input
            type="number"
            min={1024}
            max={65535}
            value={form.port}
            onChange={e => setForm({ ...form, port: Number(e.target.value) })}
            className="input w-full mt-1 font-mono"
          />
          <p className="text-xs text-dark-500 mt-1">{t('exit_proxy.port_hint')}</p>
          {portChanged && (
            <p className="text-xs text-amber-400 mt-1">{t('exit_proxy.ports_changed_warning')}</p>
          )}
        </div>
      </div>

      <div>
        <label className="text-sm text-dark-400">{t('exit_proxy.blocked_countries')}</label>
        <p className="text-xs text-dark-500 mb-2">{t('exit_proxy.blocked_countries_hint')}</p>
        <div className="flex flex-wrap items-center gap-2">
          {form.blocked_countries.map(code => (
            <span key={code} className="inline-flex items-center gap-1.5 bg-dark-800 rounded px-2 py-1 text-xs font-mono text-dark-200">
              {getFlag(code)} {code}
              <button onClick={() => setForm({ ...form, blocked_countries: form.blocked_countries.filter(c => c !== code) })} className="text-dark-500 hover:text-red-400">
                <Trash2 className="w-3 h-3" />
              </button>
            </span>
          ))}
          <input
            value={country}
            onChange={e => setCountry(e.target.value.toUpperCase())}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addCountry() } }}
            onBlur={() => country && addCountry()}
            placeholder={t('exit_proxy.country_placeholder')}
            maxLength={2}
            className="input w-20 font-mono uppercase"
          />
        </div>
      </div>

      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-dark-200">{t('exit_proxy.notify')}</p>
          <p className="text-xs text-dark-500">{t('exit_proxy.notify_hint')}</p>
        </div>
        <Toggle on={form.notify_enabled} onClick={() => setForm({ ...form, notify_enabled: !form.notify_enabled })} />
      </div>

      <p className="text-xs text-dark-500">{t('exit_proxy.min_node_version', { version: settings.min_node_version })}</p>

      <button onClick={save} disabled={saving} className="btn btn-primary w-full justify-center">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
        {t('exit_proxy.save')}
      </button>
    </div>
  )
}
