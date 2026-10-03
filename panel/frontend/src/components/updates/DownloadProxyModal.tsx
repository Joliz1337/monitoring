import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { CheckCircle2, Globe, Loader2, Trash2, X, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  downloadProxyApi,
  type DownloadProxyChange,
  type DownloadProxyState,
  type DownloadProxyTest,
} from '../../api/client'

export interface DownloadProxyTarget {
  id: number
  name: string
}

interface Props {
  targets: DownloadProxyTarget[]
  onChanged: (serverIds: number[]) => void
  onClose: () => void
}

// Тот же шаблон, что у ноды: адрес уходит в sourced proxy.conf, apt.conf и
// Environment= юнита Docker — без кавычек, пробелов и метасимволов shell
const PROXY_URL_RE = /^https?:\/\/[A-Za-z0-9._~%!*+,=:@[\]-]+\/?$/

// Как в установщике: адрес без схемы — это HTTP-прокси
const normalizeProxyUrl = (value: string) => {
  const trimmed = value.trim()
  return /^https?:\/\//i.test(trimmed) ? trimmed : `http://${trimmed}`
}

const hostOf = (target: string) => target.replace(/^https?:\/\//, '').replace(/\/.*$/, '')

const errorText = (err: any, fallback: string) => err?.response?.data?.detail || err?.message || fallback

export default function DownloadProxyModal({ targets, onChanged, onClose }: Props) {
  const { t } = useTranslation()
  const isSingle = targets.length === 1
  const [state, setState] = useState<DownloadProxyState | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [url, setUrl] = useState('')
  const [checks, setChecks] = useState<DownloadProxyTest[] | null>(null)
  const [testing, setTesting] = useState(false)
  const [applying, setApplying] = useState(false)

  useEffect(() => {
    if (!isSingle) return
    downloadProxyApi.get(targets[0].id)
      .then(({ data }) => setState(data))
      .catch(err => setLoadError(errorText(err, t('updates.proxy_load_failed'))))
  }, [isSingle, targets, t])

  const candidate = url.trim() ? normalizeProxyUrl(url) : ''
  const candidateValid = !candidate || PROXY_URL_RE.test(candidate)

  const runTest = async (testUrl?: string) => {
    setTesting(true)
    setChecks(null)
    try {
      const { data } = await downloadProxyApi.test(targets[0].id, testUrl)
      setChecks(data.checks)
    } catch (err) {
      toast.error(errorText(err, t('updates.proxy_test_failed')))
    } finally {
      setTesting(false)
    }
  }

  const apply = async (action: (id: number) => Promise<{ data: DownloadProxyChange }>, confirmKey: string, doneKey: string) => {
    if (!confirm(t(confirmKey, { count: targets.length }))) return
    setApplying(true)
    const results = await Promise.allSettled(targets.map(target => action(target.id)))
    setApplying(false)

    const changedIds: number[] = []
    results.forEach((res, i) => {
      if (res.status === 'fulfilled') changedIds.push(targets[i].id)
      else toast.error(`${targets[i].name}: ${errorText(res.reason, t('updates.proxy_apply_failed'))}`)
    })
    if (changedIds.length === 0) return
    const restarts = results.some(res => res.status === 'fulfilled' && res.value.data.restart_docker)
    toast.success(t(doneKey, { count: changedIds.length }) + (restarts ? ` ${t('updates.proxy_restart_note')}` : ''))
    onChanged(changedIds)
    onClose()
  }

  const handleSave = () => apply(id => downloadProxyApi.set(id, candidate), 'updates.proxy_confirm_set', 'updates.proxy_saved')
  const handleRemove = () => apply(id => downloadProxyApi.remove(id), 'updates.proxy_confirm_remove', 'updates.proxy_removed')

  const busy = applying || testing

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <motion.div
        className="card w-full max-w-2xl max-h-[90vh] flex flex-col overflow-y-auto"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
            <Globe className="w-5 h-5 text-accent-500" />
            {t('updates.proxy_title')}
            {isSingle
              ? <> — <span className="text-dark-300">{targets[0].name}</span></>
              : <span className="text-dark-400 text-sm font-normal">({t('updates.proxy_servers', { count: targets.length })})</span>}
          </h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        <p className="text-sm text-dark-400 mb-4">{t('updates.proxy_desc')}</p>

        {isSingle && (
          <div className="mb-4">
            {loadError ? (
              <p className="text-sm text-danger">{loadError}</p>
            ) : !state ? (
              <div className="flex items-center gap-2 text-sm text-dark-400"><Loader2 className="w-4 h-4 animate-spin" />{t('updates.proxy_loading')}</div>
            ) : state.entries.length === 0 ? (
              <p className="text-sm text-dark-300">{t('updates.proxy_none')}</p>
            ) : (
              <div className="rounded-lg border border-dark-700/40 divide-y divide-dark-700/40">
                {state.entries.map(entry => (
                  <div key={`${entry.source}:${entry.location}:${entry.url}`} className="px-3 py-2 text-sm">
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-dark-200">{t(`updates.proxy_source_${entry.source}`)}</span>
                      <span className="font-mono text-xs text-warning truncate">{entry.url}</span>
                    </div>
                    <div className="font-mono text-2xs text-dark-500 truncate">{entry.location}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <label className="text-sm text-dark-300 mb-1.5 block">{t('updates.proxy_new')}</label>
        <input
          className="input w-full font-mono"
          value={url}
          onChange={e => { setUrl(e.target.value); setChecks(null) }}
          placeholder="http://user:password@203.0.113.10:3128"
          disabled={busy}
        />
        {!candidateValid && <p className="text-xs text-danger mt-1.5">{t('updates.proxy_invalid')}</p>}

        {checks && (
          <div className="mt-4 space-y-3">
            {checks.length === 0 && <p className="text-sm text-dark-400">{t('updates.proxy_none')}</p>}
            {checks.map(check => (
              <div key={check.url} className="rounded-lg border border-dark-700/40 p-3">
                <div className="font-mono text-xs text-dark-300 mb-2">{check.url}</div>
                {check.results.map(result => (
                  <div key={result.target} className="flex items-center gap-2 text-sm">
                    {result.ok
                      ? <CheckCircle2 className="w-4 h-4 text-success shrink-0" />
                      : <XCircle className="w-4 h-4 text-danger shrink-0" />}
                    <span className="text-dark-200">{hostOf(result.target)}</span>
                    <span className="text-xs text-dark-500 truncate">
                      {result.ok ? t('updates.proxy_check_ok', { ms: result.ms }) : result.error}
                    </span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}

        <p className="text-xs text-warning mt-4">{t('updates.proxy_restart_warning')}</p>

        <div className="flex flex-wrap items-center justify-between gap-2 mt-5">
          <div className="flex gap-2">
            {isSingle && (
              <button
                onClick={() => runTest(candidate || undefined)}
                disabled={busy || !candidateValid || (!candidate && !state?.entries.length)}
                className="btn btn-secondary text-sm"
              >
                {testing && <Loader2 className="w-4 h-4 animate-spin" />}
                {candidate ? t('updates.proxy_test_new') : t('updates.proxy_test_current')}
              </button>
            )}
            {(!isSingle || !!state?.entries.length) && (
              <button onClick={handleRemove} disabled={busy} className="btn btn-secondary text-sm text-danger">
                <Trash2 className="w-4 h-4" />
                {isSingle ? t('updates.proxy_remove') : t('updates.proxy_remove_all', { count: targets.length })}
              </button>
            )}
          </div>
          <div className="flex gap-2">
            <button onClick={onClose} className="btn btn-secondary">{t('common.close')}</button>
            <button onClick={handleSave} disabled={busy || !candidate || !candidateValid} className="btn btn-primary">
              {applying && <Loader2 className="w-4 h-4 animate-spin" />}
              {isSingle ? t('updates.proxy_save') : t('updates.proxy_save_all', { count: targets.length })}
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  )
}
