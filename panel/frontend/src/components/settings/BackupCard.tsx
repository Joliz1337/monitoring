import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { Database, Archive, Upload, Download, Trash2, Loader2, Check, XCircle, AlertTriangle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { backupApi, type BackupInfo, type BackupStatus } from '../../api/client'
import { formatBytes } from '../../utils/format'
import { Tooltip } from '../ui/Tooltip'
import { SettingsSection } from './SettingsSection'

const STATUS_POLL_MS = 2000
const VOLUME_SUFFIX = /\.(\d{3})$/

interface VolumeSet {
  count: number
  totalSize: number
  missing: number[]
}

// Набор томов из Telegram: `…enc.001`, `.002`, … Полнота — по непрерывности номеров;
// пропавший последний том по именам не виден, его поймает бэкенд при расшифровке
function detectVolumeSet(files: File[]): VolumeSet | null {
  const numbers = files.map(f => Number(f.name.match(VOLUME_SUFFIX)?.[1]))
  if (!files.length || numbers.some(Number.isNaN)) return null
  const present = new Set(numbers)
  const missing: number[] = []
  for (let n = 1; n <= Math.max(...numbers); n++) {
    if (!present.has(n)) missing.push(n)
  }
  return { count: files.length, totalSize: files.reduce((sum, f) => sum + f.size, 0), missing }
}

const volumeLabel = (n: number) => `.${String(n).padStart(3, '0')}`

function RestoreWarning({ text }: { text: string }) {
  return (
    <p className="text-xs text-warning mb-3 flex items-center gap-1.5">
      <AlertTriangle className="w-3 h-3 flex-shrink-0" />
      {text}
    </p>
  )
}

export function BackupCard() {
  const { t } = useTranslation()
  const [backups, setBackups] = useState<BackupInfo[]>([])
  const [status, setStatus] = useState<BackupStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [confirmRestore, setConfirmRestore] = useState<File[] | null>(null)
  const [restorePassword, setRestorePassword] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchBackups = useCallback(async () => {
    try {
      const res = await backupApi.list()
      setBackups(res.data.backups)
    } catch (err) {
      console.error('Failed to fetch backups:', err)
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchStatus = useCallback(async () => {
    try {
      const res = await backupApi.getStatus()
      setStatus(res.data)
      return res.data
    } catch {
      return null
    }
  }, [])

  const startPoll = useCallback(() => {
    if (pollRef.current) return
    pollRef.current = setInterval(async () => {
      const current = await fetchStatus()
      if (!current || current.state !== 'idle') return
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
      fetchBackups()
      if (current.error) {
        toast.error(current.error)
      } else if (current.filename) {
        const wasRestore = current.completed_at && !current.filename.startsWith('backup_')
        toast.success(wasRestore ? t('settings.backup_restore_success') : t('settings.backup_success'))
      }
    }, STATUS_POLL_MS)
  }, [fetchStatus, fetchBackups, t])

  useEffect(() => {
    fetchBackups()
    fetchStatus().then(current => {
      if (current && current.state !== 'idle') startPoll()
    })
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [fetchBackups, fetchStatus, startPoll])

  const handleCreate = async () => {
    try {
      await backupApi.create()
      await fetchStatus()
      startPoll()
    } catch (err: any) {
      toast.error(err.response?.status === 409 ? t('settings.backup_busy') : t('settings.backup_error'))
    }
  }

  const handleDownload = async (filename: string) => {
    try {
      const res = await backupApi.download(filename)
      const url = window.URL.createObjectURL(new Blob([res.data]))
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      a.click()
      window.URL.revokeObjectURL(url)
    } catch {
      toast.error(t('settings.backup_error'))
    }
  }

  const handleDelete = async (filename: string) => {
    try {
      await backupApi.delete(filename)
      setBackups(prev => prev.filter(b => b.filename !== filename))
      setConfirmDelete(null)
    } catch {
      toast.error(t('settings.backup_error'))
    }
  }

  const handleRestore = async (files: File[]) => {
    const password = restorePassword
    setConfirmRestore(null)
    setRestorePassword('')
    try {
      await backupApi.restore(files, password)
      await fetchStatus()
      startPoll()
    } catch (err: any) {
      if (err.response?.status === 409) {
        toast.error(t('settings.backup_busy'))
      } else {
        toast.error(err.response?.data?.detail || t('settings.backup_error'))
      }
    }
  }

  const handleFileSelect = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || [])
    if (files.length) setConfirmRestore(files)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const handleDrop = (e: DragEvent) => {
    e.preventDefault()
    const files = Array.from(e.dataTransfer.files || [])
    if (files.length) setConfirmRestore(files)
  }

  const closeRestore = () => {
    setConfirmRestore(null)
    setRestorePassword('')
  }

  const busy = status !== null && status.state !== 'idle'
  const restoring = status?.state === 'restoring'

  const volumeSet = confirmRestore ? detectVolumeSet(confirmRestore) : null
  const multiplePlainFiles = confirmRestore !== null && !volumeSet && confirmRestore.length > 1
  const restoreBlocked = multiplePlainFiles || (volumeSet !== null && (volumeSet.missing.length > 0 || !restorePassword))

  const createButton = (
    <button onClick={handleCreate} disabled={busy} className="btn btn-primary text-sm">
      {status?.state === 'creating'
        ? <><Loader2 className="w-4 h-4 animate-spin" />{t('settings.backup_creating')}</>
        : <><Archive className="w-4 h-4" />{t('settings.backup_create')}</>}
    </button>
  )

  return (
    <SettingsSection
      icon={Database}
      title={t('settings.backup_title')}
      description={t('settings.backup_desc')}
      faq="SETTINGS_BACKUP"
      right={createButton}
    >
      <div className="space-y-4">
        <div
          onDrop={handleDrop}
          onDragOver={e => e.preventDefault()}
          onClick={() => !restoring && fileInputRef.current?.click()}
          className={`relative flex items-center justify-center gap-3 p-4 rounded-xl border-2 border-dashed transition-all cursor-pointer ${
            restoring
              ? 'border-warning/40 bg-warning/5 cursor-not-allowed'
              : 'border-dark-700/50 hover:border-accent-500/50 hover:bg-accent-500/5'
          }`}
        >
          <input ref={fileInputRef} type="file" multiple onChange={handleFileSelect} className="hidden" />
          {restoring ? (
            <>
              <Loader2 className="w-5 h-5 text-warning animate-spin" />
              <span className="text-sm text-warning">{t('settings.backup_restoring')}</span>
            </>
          ) : (
            <>
              <Upload className="w-5 h-5 text-dark-400" />
              <div className="text-center">
                <span className="text-sm text-dark-300">{t('settings.backup_upload')}</span>
                <p className="text-xs text-dark-500 mt-0.5">{t('settings.backup_upload_hint')}</p>
              </div>
            </>
          )}
        </div>

        <AnimatePresence>
          {status?.state === 'idle' && status.error && (
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              className="flex items-center gap-3 p-3 rounded-xl bg-danger/10 border border-danger/20"
            >
              <XCircle className="w-4 h-4 text-danger flex-shrink-0" />
              <span className="text-sm text-danger break-all">{status.error}</span>
            </motion.div>
          )}
        </AnimatePresence>

        {loading ? (
          <div className="flex items-center justify-center py-4">
            <Loader2 className="w-5 h-5 text-accent-500 animate-spin" />
          </div>
        ) : backups.length === 0 ? (
          <div className="text-sm text-dark-500 text-center py-3">{t('settings.backup_empty')}</div>
        ) : (
          <div className="space-y-2">
            <div className="text-xs text-dark-400 uppercase tracking-wider">{t('settings.backup_list_title')}</div>
            {backups.map(b => (
              <div key={b.filename} className="flex items-center gap-3 p-3 bg-dark-800/50 rounded-xl border border-dark-700/50">
                <Archive className="w-4 h-4 text-dark-400 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-dark-200 truncate">{b.filename}</div>
                  <div className="flex items-center gap-3 text-xs text-dark-500 mt-0.5">
                    <span>{formatBytes(b.size)}</span>
                    <span>{new Date(b.created_at).toLocaleString()}</span>
                    {b.version && <span className="text-dark-600">v{b.version}</span>}
                  </div>
                </div>
                <div className="flex items-center gap-1.5">
                  <Tooltip label={t('settings.backup_download')}>
                    <button
                      onClick={() => handleDownload(b.filename)}
                      className="p-2 rounded-lg text-dark-400 hover:text-accent-400 hover:bg-dark-700/50 transition-colors"
                    >
                      <Download className="w-4 h-4" />
                    </button>
                  </Tooltip>
                  {confirmDelete === b.filename ? (
                    <div className="flex items-center gap-1">
                      <Tooltip label={t('common.delete')}>
                        <button onClick={() => handleDelete(b.filename)} className="p-2 rounded-lg text-danger hover:bg-danger/10 transition-colors">
                          <Check className="w-4 h-4" />
                        </button>
                      </Tooltip>
                      <Tooltip label={t('common.cancel')}>
                        <button onClick={() => setConfirmDelete(null)} className="p-2 rounded-lg text-dark-400 hover:bg-dark-700/50 transition-colors">
                          <XCircle className="w-4 h-4" />
                        </button>
                      </Tooltip>
                    </div>
                  ) : (
                    <Tooltip label={t('settings.backup_delete')}>
                      <button
                        onClick={() => setConfirmDelete(b.filename)}
                        className="p-2 rounded-lg text-dark-400 hover:text-danger hover:bg-danger/10 transition-colors"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </Tooltip>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Портал: корень вкладки анимируется transform'ом и стал бы containing block для fixed-оверлея */}
      {createPortal(
        <AnimatePresence>
          {confirmRestore && (
            <motion.div
              className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              onClick={closeRestore}
            >
              <motion.div
                className="bg-dark-900 border border-dark-700 rounded-2xl p-6 max-w-md mx-4 shadow-2xl"
                initial={{ scale: 0.9, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                exit={{ scale: 0.9, opacity: 0 }}
                onClick={e => e.stopPropagation()}
              >
                <div className="flex items-center gap-3 mb-4">
                  <div className="w-10 h-10 rounded-xl bg-warning/20 flex items-center justify-center">
                    <AlertTriangle className="w-5 h-5 text-warning" />
                  </div>
                  <h3 className="text-lg font-semibold text-dark-100">{t('settings.backup_restore')}</h3>
                </div>
                <p className="text-sm text-dark-300 mb-2">{t('settings.backup_confirm_restore')}</p>
                <p className="text-xs text-dark-500 mb-3 font-mono bg-dark-800/50 rounded-lg px-3 py-2">
                  {volumeSet
                    ? t('settings.backup_restore_parts', { count: volumeSet.count, size: formatBytes(volumeSet.totalSize) })
                    : confirmRestore.map(f => `${f.name} (${formatBytes(f.size)})`).join(', ')}
                </p>
                {volumeSet && volumeSet.missing.length > 0 && (
                  <RestoreWarning text={t('settings.backup_restore_missing', { list: volumeSet.missing.map(volumeLabel).join(', ') })} />
                )}
                {multiplePlainFiles && <RestoreWarning text={t('settings.backup_restore_multi_plain')} />}
                {volumeSet && (
                  <div className="mb-4">
                    <input
                      type="password"
                      autoFocus
                      value={restorePassword}
                      onChange={e => setRestorePassword(e.target.value)}
                      placeholder={t('settings.backup_restore_password')}
                      className="input w-full"
                    />
                    <p className="text-xs text-dark-500 mt-1.5">{t('settings.backup_restore_password_hint')}</p>
                  </div>
                )}
                <div className="flex gap-3">
                  <button onClick={closeRestore} className="flex-1 btn btn-secondary">
                    {t('common.cancel')}
                  </button>
                  <button
                    onClick={() => handleRestore(confirmRestore)}
                    disabled={restoreBlocked}
                    className="flex-1 btn bg-warning/20 text-warning hover:bg-warning/30 border border-warning/30 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {t('settings.backup_restore')}
                  </button>
                </div>
                <p className="text-xs text-dark-500 mt-3 flex items-center gap-1.5">
                  <AlertTriangle className="w-3 h-3" />
                  {t('settings.backup_restart_hint')}
                </p>
              </motion.div>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </SettingsSection>
  )
}
