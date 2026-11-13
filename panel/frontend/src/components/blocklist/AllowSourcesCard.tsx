import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Globe, Loader2, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { blocklistApi, type AllowlistSource } from '../../api/client'
import { Toggle } from '../ui/Toggle'

type Busy = 'add' | 'refresh' | null

/** Списки IP/CIDR по URL (Cloudflare, Yandex Cloud…) — часть белого списка входящих */
export default function AllowSourcesCard() {
  const { t } = useTranslation()
  const [sources, setSources] = useState<AllowlistSource[]>([])
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState<Busy>(null)

  const load = useCallback(async () => {
    try {
      const { data } = await blocklistApi.getAllowSources()
      setSources(data.sources)
    } catch (err) {
      console.error('Failed to fetch allowlist sources:', err)
    }
  }, [])

  useEffect(() => { load() }, [load])

  // Сервер скачивает список сразу: ответ уже говорит, разобрался ли он
  const announce = (source: AllowlistSource) => {
    if (source.error_message) {
      toast.error(t('blocklist.allow_src_failed', { error: source.error_message }))
    } else {
      toast.success(t('blocklist.allow_src_loaded', { count: source.ip_count }))
    }
  }

  const add = async () => {
    const address = url.trim()
    if (!address || busy) return
    setBusy('add')
    try {
      const { data } = await blocklistApi.addAllowSource({ name: name.trim() || address, url: address })
      setName('')
      setUrl('')
      announce(data.source)
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      toast.error(typeof detail === 'string' ? detail : t('blocklist.allow_src_action_failed'))
    } finally {
      setBusy(null)
      await load()
    }
  }

  const toggle = async (source: AllowlistSource) => {
    try {
      const { data } = await blocklistApi.updateAllowSource(source.id, { enabled: !source.enabled })
      if (data.source.enabled) announce(data.source)
    } catch {
      toast.error(t('blocklist.allow_src_action_failed'))
    }
    await load()
  }

  const remove = async (source: AllowlistSource) => {
    try {
      await blocklistApi.deleteAllowSource(source.id)
    } catch {
      toast.error(t('blocklist.allow_src_action_failed'))
    }
    await load()
  }

  const refresh = async () => {
    if (busy) return
    setBusy('refresh')
    try {
      const { data } = await blocklistApi.refreshAllowSources()
      setSources(data.sources)
      toast.success(t('blocklist.allow_src_refreshed'))
    } catch {
      toast.error(t('blocklist.allow_src_action_failed'))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="card">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div>
          <h3 className="text-lg font-semibold text-dark-100 mb-2 flex items-center gap-2">
            <Globe className="w-5 h-5 text-emerald-400" />
            {t('blocklist.allow_src_title')}
          </h3>
          <p className="text-sm text-dark-400">{t('blocklist.allow_src_desc')}</p>
        </div>
        <button onClick={refresh} disabled={busy === 'refresh' || sources.length === 0} className="btn btn-secondary shrink-0">
          {busy === 'refresh' ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          {t('blocklist.allow_src_refresh')}
        </button>
      </div>

      <div className="flex flex-col sm:flex-row gap-2 mb-4">
        <input
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder={t('blocklist.allow_src_name_placeholder')}
          className="input sm:w-44"
        />
        <input
          value={url}
          onChange={e => setUrl(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') add() }}
          placeholder="https://www.cloudflare.com/ips-v4/"
          className="input flex-1 font-mono text-sm"
        />
        <button onClick={add} disabled={busy === 'add' || !url.trim()} className="btn btn-primary shrink-0">
          {busy === 'add' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
          {t('blocklist.add')}
        </button>
      </div>

      {sources.length === 0 ? (
        <p className="text-sm text-dark-500">{t('blocklist.allow_src_empty')}</p>
      ) : (
        <div className="border border-dark-800 rounded-lg divide-y divide-dark-800">
          {sources.map(source => (
            <div key={source.id} className="p-3 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-sm text-dark-100 truncate">{source.name}</span>
                  <span className="text-2xs px-1.5 py-0.5 rounded bg-dark-800 text-dark-400">{source.ip_count} IP/CIDR</span>
                </div>
                <div className="text-xs text-dark-500 truncate font-mono">{source.url}</div>
                {source.error_message && (
                  <div className="text-xs text-red-400">
                    {source.error_message}
                    {source.ip_count > 0 && ` — ${t('blocklist.allow_src_last_good')}`}
                  </div>
                )}
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <Toggle on={source.enabled} onClick={() => toggle(source)} />
                <button onClick={() => remove(source)} className="text-dark-500 hover:text-red-400">
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
