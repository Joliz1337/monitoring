import { useEffect, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  KeyRound,
  Plus,
  Copy,
  Check,
  Trash2,
  Loader2,
  AlertTriangle,
  ChevronDown,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { installKeysApi, type NodeInstallKey } from '../../api/client'

const VALIDITY_OPTIONS = [30, 90, 365, 1095]
const EXPIRY_WARNING_DAYS = 14
const DELETE_CONFIRM_MS = 3000

async function copyText(value: string) {
  try {
    await navigator.clipboard.writeText(value)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = value
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
}

function daysLeft(expiresAt: string): number {
  return Math.ceil((new Date(expiresAt).getTime() - Date.now()) / 86400000)
}

export default function InstallKeysPanel() {
  const { t, i18n } = useTranslation()
  const [open, setOpen] = useState(false)
  const [keys, setKeys] = useState<NodeInstallKey[]>([])
  const [loaded, setLoaded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [name, setName] = useState('')
  const [validityDays, setValidityDays] = useState(90)
  const [creating, setCreating] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null)

  useEffect(() => {
    if (!open || loaded) return
    setLoading(true)
    installKeysApi
      .list()
      .then((res) => {
        setKeys(res.data.keys)
        setLoaded(true)
      })
      .catch(() => toast.error(t('servers.install_keys_load_failed')))
      .finally(() => setLoading(false))
  }, [open, loaded, t])

  const handleCreate = async () => {
    const trimmed = name.trim()
    if (!trimmed) return
    setCreating(true)
    try {
      const res = await installKeysApi.create(trimmed, validityDays)
      setKeys((prev) => [res.data, ...prev])
      setName('')
      await copyText(res.data.install_command)
      setCopied(`cmd-${res.data.id}`)
      setTimeout(() => setCopied(null), 2000)
      toast.success(t('servers.install_keys_created'))
    } catch {
      toast.error(t('servers.install_keys_create_failed'))
    } finally {
      setCreating(false)
    }
  }

  const handleCopy = async (id: string, value: string) => {
    await copyText(value)
    setCopied(id)
    setTimeout(() => setCopied(null), 2000)
  }

  const handleDelete = async (keyId: number) => {
    if (confirmDelete !== keyId) {
      setConfirmDelete(keyId)
      setTimeout(() => setConfirmDelete((cur) => (cur === keyId ? null : cur)), DELETE_CONFIRM_MS)
      return
    }
    setConfirmDelete(null)
    try {
      await installKeysApi.delete(keyId)
      setKeys((prev) => prev.filter((k) => k.id !== keyId))
    } catch {
      toast.error(t('servers.install_keys_delete_failed'))
    }
  }

  return (
    <div className="mb-6 p-4 rounded-xl bg-dark-800/40 border border-dark-700">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 text-left"
      >
        <KeyRound className="w-5 h-5 text-accent-500" />
        <span className="text-sm font-semibold text-dark-100">{t('servers.install_keys_title')}</span>
        <ChevronDown
          className={`w-4 h-4 text-dark-400 ml-auto transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            <p className="text-xs text-dark-400 mt-3">{t('servers.install_keys_subtitle')}</p>

            <div className="flex items-start gap-2 mt-3 p-3 rounded-lg bg-warning/10 border border-warning/20">
              <AlertTriangle className="w-4 h-4 text-warning flex-shrink-0 mt-0.5" />
              <p className="text-xs text-dark-300">{t('servers.install_keys_warning')}</p>
            </div>

            <div className="flex flex-col sm:flex-row gap-3 mt-4">
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t('servers.install_keys_name_placeholder')}
                className="input flex-1"
                maxLength={200}
              />
              <select
                value={validityDays}
                onChange={(e) => setValidityDays(Number(e.target.value))}
                className="input sm:w-44"
              >
                {VALIDITY_OPTIONS.map((days) => (
                  <option key={days} value={days}>
                    {t('servers.install_keys_days', { count: days })}
                  </option>
                ))}
              </select>
              <motion.button
                type="button"
                onClick={handleCreate}
                disabled={creating || !name.trim()}
                className="btn btn-primary text-sm disabled:opacity-50"
                whileTap={{ scale: 0.98 }}
              >
                {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                {t('servers.install_keys_create')}
              </motion.button>
            </div>

            <div className="mt-4 space-y-2">
              {loading && (
                <div className="flex items-center gap-2 text-dark-400 text-sm">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {t('common.loading')}
                </div>
              )}

              {!loading && keys.length === 0 && (
                <p className="text-sm text-dark-500">{t('servers.install_keys_empty')}</p>
              )}

              {keys.map((key) => {
                const left = daysLeft(key.expires_at)
                const expired = left <= 0
                const expiringSoon = !expired && left <= EXPIRY_WARNING_DAYS
                return (
                  <div
                    key={key.id}
                    className="p-3 rounded-lg bg-dark-900/50 border border-dark-700"
                  >
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm text-dark-100 font-medium">{key.name}</span>
                      <span
                        className={`text-xs px-2 py-0.5 rounded-full ${
                          expired
                            ? 'bg-danger/15 text-danger'
                            : expiringSoon
                              ? 'bg-warning/15 text-warning'
                              : 'bg-dark-700 text-dark-300'
                        }`}
                      >
                        {expired
                          ? t('servers.install_keys_expired')
                          : t('servers.install_keys_expires', {
                              date: new Date(key.expires_at).toLocaleDateString(i18n.language),
                            })}
                      </span>
                      <button
                        type="button"
                        onClick={() => handleDelete(key.id)}
                        className={`ml-auto p-1.5 rounded-lg transition-colors ${
                          confirmDelete === key.id
                            ? 'bg-danger/20 text-danger'
                            : 'text-dark-400 hover:text-danger hover:bg-dark-700'
                        }`}
                        title={t('servers.install_keys_delete')}
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>

                    <p className="text-xs text-dark-500 font-mono mt-1 break-all">
                      {key.fingerprint.slice(0, 23)}…
                    </p>

                    <div className="flex gap-2 mt-2">
                      <button
                        type="button"
                        onClick={() => handleCopy(`cmd-${key.id}`, key.install_command)}
                        className={`btn text-xs ${
                          copied === `cmd-${key.id}`
                            ? 'bg-success/20 text-success border-success/30'
                            : 'btn-secondary'
                        }`}
                      >
                        {copied === `cmd-${key.id}` ? (
                          <><Check className="w-3.5 h-3.5" />{t('servers.copied')}</>
                        ) : (
                          <><Copy className="w-3.5 h-3.5" />{t('servers.install_keys_copy_command')}</>
                        )}
                      </button>
                      <button
                        type="button"
                        onClick={() => handleCopy(`tok-${key.id}`, key.token)}
                        className={`btn text-xs ${
                          copied === `tok-${key.id}`
                            ? 'bg-success/20 text-success border-success/30'
                            : 'btn-secondary'
                        }`}
                      >
                        {copied === `tok-${key.id}` ? (
                          <><Check className="w-3.5 h-3.5" />{t('servers.copied')}</>
                        ) : (
                          <><Copy className="w-3.5 h-3.5" />{t('servers.install_keys_copy_token')}</>
                        )}
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
