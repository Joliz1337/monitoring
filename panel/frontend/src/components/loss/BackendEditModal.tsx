import { useEffect, useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Loader2, Replace, X } from 'lucide-react'
import {
  lossApi,
  type BackendEditAction,
  type BackendEditBody,
  type BackendEditJob,
  type BackendEditProfile,
  type LossSuggestion,
} from '../../api/client'
import LossProbeBadge from '../ui/LossProbeBadge'
import { IPV4_RE, PREVIEW_DEBOUNCE_MS, inputCls, outcomeText } from './editShared'

interface Props {
  ip: string
  port: number
  onClose: () => void
  onJobStarted: (job: BackendEditJob) => void
}

function parsePort(value: string): number | null {
  const port = Number(value)
  return Number.isInteger(port) && port >= 1 && port <= 65535 ? port : null
}

export default function BackendEditModal({ ip, port, onClose, onJobStarted }: Props) {
  const { t } = useTranslation()
  const [action, setAction] = useState<BackendEditAction>('replace')
  const [newIp, setNewIp] = useState(ip)
  const [newPort, setNewPort] = useState(String(port))
  const [allPortsChecked, setAllPortsChecked] = useState(true)
  const [profiles, setProfiles] = useState<BackendEditProfile[] | null>(null)
  const [suggestions, setSuggestions] = useState<LossSuggestion[]>([])
  const [loadingPreview, setLoadingPreview] = useState(false)
  const [applying, setApplying] = useState(false)

  const parsedPort = parsePort(newPort)
  const ipValid = IPV4_RE.test(newIp.trim())
  const changesIp = action === 'replace' && newIp.trim() !== ip
  const changesPort = action === 'replace' && parsedPort !== null && parsedPort !== port
  // Смена порта касается одного порта: у остальных портов этого IP своя судьба
  const allPorts = allPortsChecked && !changesPort
  const noop = action === 'replace' && !changesIp && !changesPort
  const inputsValid = action === 'delete' || (ipValid && parsedPort !== null)

  const body = useMemo<BackendEditBody>(() => ({
    ip,
    port,
    all_ports: allPorts,
    action,
    new_ip: action === 'replace' ? newIp.trim() : null,
    new_port: action === 'replace' ? parsedPort : null,
  }), [ip, port, allPorts, action, newIp, parsedPort])

  useEffect(() => {
    if (!inputsValid) {
      setProfiles(null)
      return
    }
    const timer = setTimeout(async () => {
      setLoadingPreview(true)
      try {
        const { data } = await lossApi.backendsPreview([body])
        setProfiles(data.items[0] ?? [])
        setSuggestions(data.suggestions[ip] ?? [])
      } catch {
        setProfiles(null)
      } finally {
        setLoadingPreview(false)
      }
    }, PREVIEW_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [body, inputsValid, ip])

  const affected = (profiles ?? []).flatMap(p => p.rules).filter(r => r.outcome !== 'skipped').length

  const apply = async () => {
    setApplying(true)
    try {
      const { data } = await lossApi.backendsApply([body])
      onJobStarted(data.job)
      toast.success(t('loss.job_started'))
      onClose()
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      toast.error(typeof detail === 'string' ? `${t('loss.edit_failed')}: ${detail}` : t('loss.edit_failed'))
    } finally {
      setApplying(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <motion.div
        className="card w-full max-w-2xl max-h-[90vh] flex flex-col"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
            <Replace className="w-5 h-5 text-accent-500" />
            {t('loss.edit_title', { target: `${ip}:${port}` })}
          </h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="overflow-y-auto space-y-4 pr-1">
          <p className="text-xs text-dark-400">{t('loss.edit_hint')}</p>

          <div className="flex gap-2">
            {(['replace', 'delete'] as const).map(value => (
              <button
                key={value}
                onClick={() => setAction(value)}
                className={`px-3 py-1.5 rounded-lg text-sm border transition-colors ${
                  action === value
                    ? 'bg-accent-500/15 text-accent-300 border-accent-500/40'
                    : 'text-dark-400 border-dark-700 hover:text-dark-200'
                }`}
              >
                {t(`loss.action_${value}`)}
              </button>
            ))}
          </div>

          {action === 'replace' && (
            <div className="grid grid-cols-1 sm:grid-cols-[1fr_140px] gap-3">
              <div>
                <label className="block text-xs text-dark-400 mb-1">{t('loss.field_new_ip')}</label>
                <input value={newIp} onChange={e => setNewIp(e.target.value)} className={inputCls} />
              </div>
              <div>
                <label className="block text-xs text-dark-400 mb-1">{t('loss.field_new_port')}</label>
                <input value={newPort} onChange={e => setNewPort(e.target.value)} className={inputCls} inputMode="numeric" />
              </div>
            </div>
          )}

          {action === 'replace' && suggestions.length > 0 && (
            <div>
              <p className="text-xs text-dark-400 mb-1.5">{t('loss.suggestions_title')}</p>
              <div className="flex flex-wrap gap-2">
                {suggestions.map(s => (
                  <button
                    key={s.ip}
                    onClick={() => setNewIp(s.ip)}
                    className={`flex items-center gap-2 px-2.5 py-1 rounded-lg border text-xs font-mono transition-colors ${
                      newIp.trim() === s.ip ? 'border-accent-500/50 bg-accent-500/10' : 'border-dark-700 hover:border-dark-500'
                    }`}
                  >
                    <span className="text-dark-200">{s.ip}</span>
                    {s.worst_loss != null
                      ? <LossProbeBadge probe={{ loss_pct: s.worst_loss, rtt_ms: null, samples: 0 }} />
                      : <span className="text-dark-500 font-sans">{t('loss.suggestion_unknown')}</span>}
                  </button>
                ))}
              </div>
            </div>
          )}

          <label className={`flex items-center gap-2 text-xs ${changesPort ? 'text-dark-600' : 'text-dark-300 cursor-pointer'}`}>
            <input
              type="checkbox"
              checked={allPorts}
              disabled={changesPort}
              onChange={e => setAllPortsChecked(e.target.checked)}
              className="accent-accent-500"
            />
            {changesPort ? t('loss.all_ports_port_change') : t('loss.all_ports', { ip })}
          </label>

          <div className="rounded-lg border border-dark-800/60">
            <div className="flex items-center gap-2 px-3 py-2 text-xs text-dark-400 border-b border-dark-800/60">
              {t('loss.preview_title')}
              {loadingPreview && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            </div>
            {profiles === null ? (
              <p className="px-3 py-4 text-xs text-dark-500">{inputsValid ? t('loss.preview_loading') : t('loss.preview_invalid')}</p>
            ) : profiles.length === 0 ? (
              <p className="px-3 py-4 text-xs text-dark-500">{t('loss.preview_empty')}</p>
            ) : (
              <div className="divide-y divide-dark-800/60">
                {profiles.map(profile => (
                  <div key={`${profile.kind}-${profile.profile_id}`} className="px-3 py-2">
                    <div className="text-sm text-dark-200">
                      <span className="text-xs text-dark-500 mr-1.5">{t(`loss.kind_${profile.kind}`)}</span>
                      {profile.profile_name}
                      <span className="text-xs text-dark-500 ml-2">{t('loss.servers_count', { count: profile.servers })}</span>
                    </div>
                    <ul className="mt-1 space-y-0.5">
                      {profile.rules.map(rule => (
                        <li key={rule.rule} className="text-xs flex gap-2">
                          <span className="font-mono text-dark-300">{rule.rule}</span>
                          <span className={rule.outcome === 'skipped' ? 'text-warning' : 'text-dark-400'}>
                            {outcomeText(rule, action, noop, t)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </div>

          <p className="text-xs text-dark-500">{t('loss.edit_not_touched')}</p>
        </div>

        <div className="flex justify-end gap-2 pt-4">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm text-dark-300 hover:bg-dark-800">
            {t('common.cancel')}
          </button>
          <button
            onClick={apply}
            disabled={applying || noop || !inputsValid || affected === 0}
            className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-40"
          >
            {applying && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('loss.edit_apply', { count: affected })}
          </button>
        </div>
      </motion.div>
    </div>
  )
}
