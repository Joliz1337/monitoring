import { useEffect, useState, useCallback, useRef } from 'react'
import { Settings as SettingsIcon, RefreshCw, Layout, Languages, Sparkles, Check, Clock, Activity, Shield, AlertTriangle, Loader2, CheckCircle2, XCircle, Terminal, Server, Zap, Cpu, HardDrive, MemoryStick, Database, Download, Upload, Trash2, Archive } from 'lucide-react'
import { useSettingsStore, TIMEZONE_OPTIONS, TRAFFIC_PERIOD_OPTIONS, METRICS_INTERVAL_OPTIONS, HAPROXY_INTERVAL_OPTIONS } from '../stores/settingsStore'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence, LayoutGroup } from 'framer-motion'
import { systemApi, backupApi, PanelCertificateInfo, PanelServerStats, BackupInfo, BackupStatus } from '../api/client'
import { toast } from 'sonner'

interface RenewalResult {
  success: boolean
  message: string
  output?: string | null
  startedAt?: string | null
  completedAt?: string | null
}

type RenewalPhase = 'idle' | 'starting' | 'running' | 'nginx_restarting' | 'done'

export default function Settings() {
  const { 
    refreshInterval, compactView, timezone, trafficPeriod, 
    metricsCollectInterval, haproxyCollectInterval,
    fetchSettings, setRefreshInterval, setCompactView, setTimezone, setTrafficPeriod,
    setMetricsCollectInterval, setHaproxyCollectInterval
  } = useSettingsStore()
  const { t, i18n } = useTranslation()
  
  const [certInfo, setCertInfo] = useState<PanelCertificateInfo | null>(null)
  const [certLoading, setCertLoading] = useState(true)
  const [certRenewing, setCertRenewing] = useState(false)
  const [certRenewResult, setCertRenewResult] = useState<RenewalResult | null>(null)
  const [showOutput, setShowOutput] = useState(false)
  const [renewalPhase, setRenewalPhase] = useState<RenewalPhase>('idle')
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const connectionErrorCountRef = useRef(0)
  const certRenewingRef = useRef(false)
  const maxConnectionErrors = 60 // ~3 minutes with 3s interval during errors
  
  // Server stats state
  const [serverStats, setServerStats] = useState<PanelServerStats | null>(null)
  const [serverStatsLoading, setServerStatsLoading] = useState(true)
  
  const fetchCertInfo = useCallback(async () => {
    try {
      const response = await systemApi.getCertificate()
      setCertInfo(response.data)
    } catch (err) {
      console.error('Failed to fetch certificate info:', err)
    } finally {
      setCertLoading(false)
    }
  }, [])
  
  const fetchServerStats = useCallback(async () => {
    try {
      const response = await systemApi.getServerStats()
      setServerStats(response.data)
    } catch (err) {
      console.error('Failed to fetch server stats:', err)
    } finally {
      setServerStatsLoading(false)
    }
  }, [])
  
  // Backup & Restore state
  const [backups, setBackups] = useState<BackupInfo[]>([])
  const [backupStatus, setBackupStatus] = useState<BackupStatus | null>(null)
  const [backupLoading, setBackupLoading] = useState(true)
  const [confirmRestore, setConfirmRestore] = useState<File | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const backupPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  
  const fetchBackups = useCallback(async () => {
    try {
      const res = await backupApi.list()
      setBackups(res.data.backups)
    } catch (err) {
      console.error('Failed to fetch backups:', err)
    } finally {
      setBackupLoading(false)
    }
  }, [])
  
  const fetchBackupStatus = useCallback(async () => {
    try {
      const res = await backupApi.getStatus()
      setBackupStatus(res.data)
      return res.data
    } catch { return null }
  }, [])
  
  const startBackupPoll = useCallback(() => {
    if (backupPollRef.current) return
    backupPollRef.current = setInterval(async () => {
      const status = await fetchBackupStatus()
      if (status && status.state === 'idle') {
        if (backupPollRef.current) {
          clearInterval(backupPollRef.current)
          backupPollRef.current = null
        }
        fetchBackups()
        if (status.error) {
          toast.error(status.error)
        } else if (status.filename) {
          const wasRestore = status.completed_at && status.filename && !status.filename.startsWith('backup_')
          toast.success(wasRestore ? t('settings.backup_restore_success') : t('settings.backup_success'))
        }
      }
    }, 2000)
  }, [fetchBackupStatus, fetchBackups, t])
  
  const handleCreateBackup = async () => {
    try {
      await backupApi.create()
      await fetchBackupStatus()
      startBackupPoll()
    } catch (err: any) {
      if (err.response?.status === 409) {
        toast.error(t('settings.backup_busy'))
      } else {
        toast.error(t('settings.backup_error'))
      }
    }
  }
  
  const handleDownloadBackup = async (filename: string) => {
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
  
  const handleDeleteBackup = async (filename: string) => {
    try {
      await backupApi.delete(filename)
      setBackups(prev => prev.filter(b => b.filename !== filename))
      setConfirmDelete(null)
    } catch {
      toast.error(t('settings.backup_error'))
    }
  }
  
  const handleRestoreBackup = async (file: File) => {
    setConfirmRestore(null)
    try {
      await backupApi.restore(file)
      await fetchBackupStatus()
      startBackupPoll()
    } catch (err: any) {
      if (err.response?.status === 409) {
        toast.error(t('settings.backup_busy'))
      } else {
        toast.error(err.response?.data?.detail || t('settings.backup_error'))
      }
    }
  }
  
  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      setConfirmRestore(file)
    }
    if (fileInputRef.current) fileInputRef.current.value = ''
  }
  
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    const file = e.dataTransfer.files?.[0]
    if (file && file.name.endsWith('.dump')) {
      setConfirmRestore(file)
    }
  }

  const handleRenewCert = async () => {
    if (certRenewingRef.current) return
    
    certRenewingRef.current = true
    setCertRenewing(true)
    setCertRenewResult(null)
    setShowOutput(false)
    setRenewalPhase('starting')
    connectionErrorCountRef.current = 0
    
    // Clear any existing poll interval
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current)
    }
    
    const finishRenewal = (result: RenewalResult) => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current)
        pollIntervalRef.current = null
      }
      certRenewingRef.current = false
      setRenewalPhase('done')
      setCertRenewing(false)
      setCertRenewResult(result)
      if (!result.success && result.output) {
        setShowOutput(true)
      }
      if (result.success === true) {
        toast.success(t('settings.ssl_renew_success'))
      } else if (result.success === false) {
        toast.error(result.message)
      }
    }
    
    try {
      await systemApi.renewCertificate()
      setRenewalPhase('running')
      
      // Start polling for completion
      const pollStatus = async () => {
        if (!certRenewingRef.current) return
        
        try {
          const status = await systemApi.getCertRenewalStatus()
          
          // Connection restored - reset error counter and update phase
          if (connectionErrorCountRef.current > 0) {
            connectionErrorCountRef.current = 0
            setRenewalPhase('running')
          }
          
          if (!status.data.in_progress) {
            // Process completed
            const result: RenewalResult = {
              success: status.data.last_result === 'success',
              message: '',
              output: status.data.output,
              startedAt: status.data.started_at,
              completedAt: status.data.completed_at
            }
            
            if (status.data.last_result === 'success') {
              result.message = t('settings.ssl_renew_success')
              fetchCertInfo() // Refresh cert info
            } else if (status.data.last_result === 'not_due') {
              result.message = t('settings.ssl_not_due', 'Certificate is not due for renewal (more than 30 days remaining).')
            } else {
              result.message = status.data.last_error || t('settings.ssl_renew_error')
            }
            
            finishRenewal(result)
          }
        } catch {
          // Connection error - likely nginx is restarting
          connectionErrorCountRef.current++
          setRenewalPhase('nginx_restarting')
          
          // After too many errors, give up
          if (connectionErrorCountRef.current >= maxConnectionErrors) {
            finishRenewal({
              success: false,
              message: t('settings.ssl_connection_timeout', 'Connection timed out. Please check server status and refresh the page.')
            })
          }
          // Otherwise, keep polling - nginx will come back
        }
      }
      
      // Poll every 2 seconds (allows nginx ~30-60s to restart before we show status)
      pollIntervalRef.current = setInterval(pollStatus, 2000)
      
      // Also poll immediately
      setTimeout(pollStatus, 500)
      
      // Hard timeout after 5 minutes
      setTimeout(() => {
        if (certRenewingRef.current) {
          finishRenewal({
            success: false,
            message: t('settings.ssl_timeout', 'Renewal timed out. Check server logs.')
          })
        }
      }, 300000)
      
    } catch (err: any) {
      finishRenewal({ 
        success: false, 
        message: err.response?.data?.detail || t('settings.ssl_renew_error') 
      })
    }
  }
  
  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current)
      }
      if (backupPollRef.current) {
        clearInterval(backupPollRef.current)
      }
      certRenewingRef.current = false
    }
  }, [])
  
  useEffect(() => {
    fetchSettings()
    fetchCertInfo()
    fetchServerStats()
    fetchBackups()
    fetchBackupStatus().then(status => {
      if (status && status.state !== 'idle') {
        startBackupPoll()
      }
    })
    
    const statsInterval = setInterval(fetchServerStats, 5000)
    return () => clearInterval(statsInterval)
  }, [fetchSettings, fetchCertInfo, fetchServerStats, fetchBackups, fetchBackupStatus, startBackupPoll])
  
  // Helper function to format bytes
  const formatBytes = (bytes: number): string => {
    if (bytes === 0) return '0 B'
    const k = 1024
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
  }
  
  const changeLanguage = (lng: string) => {
    i18n.changeLanguage(lng)
  }

  const REFRESH_OPTIONS = [
    { value: 5, label: `5 ${t('common.seconds')}` },
    { value: 10, label: `10 ${t('common.seconds')}` },
    { value: 30, label: `30 ${t('common.seconds')}` },
    { value: 60, label: `1 ${t('common.minute')}` },
    { value: 120, label: `2 ${t('common.minutes')}` },
    { value: 300, label: `5 ${t('common.minutes')}` },
  ]

  return (
    <motion.div 
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="max-w-2xl"
    >
      {/* Header */}
      <motion.div 
        className="mb-8"
        initial={{ opacity: 0, x: -20 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ duration: 0.5 }}
      >
        <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-3">
          <SettingsIcon className="w-7 h-7 text-accent-400" />
          {t('settings.title')}
          <Sparkles className="w-4 h-4 text-accent-500" />
        </h1>
        <p className="text-dark-400 mt-1">{t('settings.subtitle')}</p>
      </motion.div>
      
      <div className="space-y-6">
        {/* Server Statistics */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="card group hover:border-dark-700 transition-all">
          <div className="flex items-center gap-3 mb-5">
            <motion.div 
              className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ rotate: 10, scale: 1.05 }}
            >
              <Server className="w-5 h-5 text-accent-500" />
            </motion.div>
            <div>
              <h2 className="font-semibold text-dark-100">{t('settings.server_stats')}</h2>
              <p className="text-sm text-dark-500">{t('settings.server_stats_desc')}</p>
            </div>
          </div>
          
          {serverStatsLoading ? (
            <div className="flex items-center justify-center py-4">
              <Loader2 className="w-5 h-5 text-accent-500 animate-spin" />
            </div>
          ) : serverStats ? (
            <div className="space-y-4">
              {/* CPU */}
              <div className="p-4 bg-dark-800/50 rounded-xl border border-dark-700/50">
                <div className="flex items-center gap-2 mb-3">
                  <Cpu className="w-4 h-4 text-dark-400" />
                  <span className="text-sm text-dark-300">CPU</span>
                  <span className="text-xs text-dark-500 ml-auto">{serverStats.cpu.cores} {t('settings.cores')}</span>
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-dark-400">{t('settings.usage')}</span>
                    <span className={`font-medium ${
                      serverStats.cpu.percent > 80 ? 'text-danger' : 
                      serverStats.cpu.percent > 50 ? 'text-warning' : 'text-success'
                    }`}>{serverStats.cpu.percent.toFixed(1)}%</span>
                  </div>
                  <div className="h-2 bg-dark-700 rounded-full overflow-hidden">
                    <motion.div 
                      className={`h-full rounded-full ${
                        serverStats.cpu.percent > 80 ? 'bg-danger' : 
                        serverStats.cpu.percent > 50 ? 'bg-warning' : 'bg-success'
                      }`}
                      initial={{ width: 0 }}
                      animate={{ width: `${serverStats.cpu.percent}%` }}
                      transition={{ duration: 0.5 }}
                    />
                  </div>
                  <div className="flex items-center justify-between text-xs text-dark-500">
                    <span>{t('settings.load_avg')}: {serverStats.cpu.load_avg_1.toFixed(2)} / {serverStats.cpu.load_avg_5.toFixed(2)} / {serverStats.cpu.load_avg_15.toFixed(2)}</span>
                  </div>
                </div>
              </div>
              
              {/* Memory */}
              <div className="p-4 bg-dark-800/50 rounded-xl border border-dark-700/50">
                <div className="flex items-center gap-2 mb-3">
                  <MemoryStick className="w-4 h-4 text-dark-400" />
                  <span className="text-sm text-dark-300">RAM</span>
                  <span className="text-xs text-dark-500 ml-auto">{formatBytes(serverStats.memory.total)}</span>
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-dark-400">{t('settings.used')}</span>
                    <span className={`font-medium ${
                      serverStats.memory.percent > 90 ? 'text-danger' : 
                      serverStats.memory.percent > 70 ? 'text-warning' : 'text-success'
                    }`}>{formatBytes(serverStats.memory.used)} ({serverStats.memory.percent.toFixed(1)}%)</span>
                  </div>
                  <div className="h-2 bg-dark-700 rounded-full overflow-hidden">
                    <motion.div 
                      className={`h-full rounded-full ${
                        serverStats.memory.percent > 90 ? 'bg-danger' : 
                        serverStats.memory.percent > 70 ? 'bg-warning' : 'bg-success'
                      }`}
                      initial={{ width: 0 }}
                      animate={{ width: `${serverStats.memory.percent}%` }}
                      transition={{ duration: 0.5 }}
                    />
                  </div>
                  <div className="flex items-center justify-between text-xs text-dark-500">
                    <span>{t('settings.available')}: {formatBytes(serverStats.memory.available)}</span>
                    {serverStats.memory.swap_total > 0 && (
                      <span>Swap: {formatBytes(serverStats.memory.swap_used)} / {formatBytes(serverStats.memory.swap_total)}</span>
                    )}
                  </div>
                </div>
              </div>
              
              {/* Disk */}
              <div className="p-4 bg-dark-800/50 rounded-xl border border-dark-700/50">
                <div className="flex items-center gap-2 mb-3">
                  <HardDrive className="w-4 h-4 text-dark-400" />
                  <span className="text-sm text-dark-300">{t('settings.disk')}</span>
                  <span className="text-xs text-dark-500 ml-auto">{formatBytes(serverStats.disk.total)}</span>
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-dark-400">{t('settings.used')}</span>
                    <span className={`font-medium ${
                      serverStats.disk.percent > 90 ? 'text-danger' : 
                      serverStats.disk.percent > 75 ? 'text-warning' : 'text-success'
                    }`}>{formatBytes(serverStats.disk.used)} ({serverStats.disk.percent.toFixed(1)}%)</span>
                  </div>
                  <div className="h-2 bg-dark-700 rounded-full overflow-hidden">
                    <motion.div 
                      className={`h-full rounded-full ${
                        serverStats.disk.percent > 90 ? 'bg-danger' : 
                        serverStats.disk.percent > 75 ? 'bg-warning' : 'bg-success'
                      }`}
                      initial={{ width: 0 }}
                      animate={{ width: `${serverStats.disk.percent}%` }}
                      transition={{ duration: 0.5 }}
                    />
                  </div>
                  <div className="flex items-center justify-between text-xs text-dark-500">
                    <span>{t('settings.free')}: {formatBytes(serverStats.disk.free)}</span>
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="text-sm text-dark-400 text-center py-4">
              {t('settings.stats_unavailable')}
            </div>
          )}
        </motion.div>

        {/* Language */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="card group hover:border-dark-700 transition-all">
          <div className="flex items-center gap-3 mb-5">
            <motion.div 
              className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ rotate: 10, scale: 1.05 }}
            >
              <Languages className="w-5 h-5 text-accent-500" />
            </motion.div>
            <div>
              <h2 className="font-semibold text-dark-100">{t('settings.language')}</h2>
              <p className="text-sm text-dark-500">{t('settings.language_desc')}</p>
            </div>
          </div>
          
          <div className="flex gap-3">
            {[
              { code: 'en', label: 'English', flag: '🇺🇸' },
              { code: 'ru', label: 'Русский', flag: '🇷🇺' }
            ].map((lang) => (
              <motion.button
                key={lang.code}
                onClick={() => changeLanguage(lang.code)}
                className={`relative flex-1 px-4 py-3 rounded-xl text-sm font-medium transition-all ${
                  i18n.language === lang.code
                    ? 'bg-gradient-to-r from-accent-500 to-accent-600 text-dark-950 shadow-lg shadow-accent-500/20'
                    : 'bg-dark-800/60 text-dark-300 hover:bg-dark-700 border border-dark-700/50'
                }`}
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
              >
                <span className="flex items-center justify-center gap-2">
                  <span className="text-base">{lang.flag}</span>
                  {lang.label}
                  <AnimatePresence>
                    {i18n.language === lang.code && (
                      <motion.span
                        initial={{ scale: 0 }}
                        animate={{ scale: 1 }}
                        exit={{ scale: 0 }}
                      >
                        <Check className="w-4 h-4" />
                      </motion.span>
                    )}
                  </AnimatePresence>
                </span>
              </motion.button>
            ))}
          </div>
        </motion.div>

        {/* Timezone */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="card group hover:border-dark-700 transition-all">
          <div className="flex items-center gap-3 mb-5">
            <motion.div 
              className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ rotate: 15, scale: 1.05 }}
            >
              <Clock className="w-5 h-5 text-accent-500" />
            </motion.div>
            <div>
              <h2 className="font-semibold text-dark-100">{t('settings.timezone')}</h2>
              <p className="text-sm text-dark-500">{t('settings.timezone_desc')}</p>
            </div>
          </div>
          
          <LayoutGroup id="timezone-selector">
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
              {TIMEZONE_OPTIONS.map((option) => (
                <motion.button
                  key={option.value}
                  onClick={() => setTimezone(option.value)}
                  className={`relative px-3 py-2.5 rounded-xl text-sm font-medium transition-all ${
                    timezone === option.value
                      ? 'text-white'
                      : 'bg-dark-800/60 text-dark-400 hover:text-dark-200 hover:bg-dark-700 border border-dark-700/50'
                  }`}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {timezone === option.value && (
                    <motion.div
                      className="absolute inset-0 bg-gradient-to-r from-accent-500 to-accent-600 rounded-xl shadow-lg shadow-accent-500/20"
                      layoutId="timezoneIndicator"
                      initial={false}
                      transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                    />
                  )}
                  <span className="relative z-10 flex flex-col items-center gap-0.5">
                    <span className="truncate w-full text-center">{option.label}</span>
                    <span className={`text-xs ${timezone === option.value ? 'text-white/70' : 'text-dark-500'}`}>
                      {option.value === 'auto' ? `(${option.offset})` : option.offset}
                    </span>
                  </span>
                </motion.button>
              ))}
            </div>
          </LayoutGroup>
        </motion.div>

        {/* Auto Refresh */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="card group hover:border-dark-700 transition-all">
          <div className="flex items-center gap-3 mb-5">
            <motion.div 
              className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ rotate: 180 }}
              transition={{ duration: 0.5 }}
            >
              <RefreshCw className="w-5 h-5 text-accent-500" />
            </motion.div>
            <div>
              <h2 className="font-semibold text-dark-100">{t('settings.auto_refresh')}</h2>
              <p className="text-sm text-dark-500">{t('settings.auto_refresh_desc')}</p>
            </div>
          </div>
          
          <LayoutGroup id="refresh-selector">
            <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
              {REFRESH_OPTIONS.map((option) => (
                <motion.button
                  key={option.value}
                  onClick={() => setRefreshInterval(option.value)}
                  className={`relative px-3 py-2.5 rounded-xl text-sm font-medium transition-all ${
                    refreshInterval === option.value
                      ? 'text-white'
                      : 'bg-dark-800/60 text-dark-400 hover:text-dark-200 hover:bg-dark-700 border border-dark-700/50'
                  }`}
                  whileHover={{ scale: 1.03 }}
                  whileTap={{ scale: 0.97 }}
                >
                  {refreshInterval === option.value && (
                    <motion.div
                      className="absolute inset-0 bg-gradient-to-r from-accent-500 to-accent-600 rounded-xl shadow-lg shadow-accent-500/20"
                      layoutId="refreshIndicator"
                      initial={false}
                      transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                    />
                  )}
                  <span className="relative z-10">{option.label}</span>
                </motion.button>
              ))}
            </div>
          </LayoutGroup>
        </motion.div>

        {/* Traffic Period */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="card group hover:border-dark-700 transition-all">
          <div className="flex items-center gap-3 mb-5">
            <motion.div 
              className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ scale: 1.1 }}
            >
              <Activity className="w-5 h-5 text-accent-500" />
            </motion.div>
            <div>
              <h2 className="font-semibold text-dark-100">{t('settings.traffic_period')}</h2>
              <p className="text-sm text-dark-500">{t('settings.traffic_period_desc')}</p>
            </div>
          </div>
          
          <LayoutGroup id="traffic-selector">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {TRAFFIC_PERIOD_OPTIONS.map((option) => (
                <motion.button
                  key={option.value}
                  onClick={() => setTrafficPeriod(option.value)}
                  className={`relative px-3 py-2.5 rounded-xl text-sm font-medium transition-all ${
                    trafficPeriod === option.value
                      ? 'text-white'
                      : 'bg-dark-800/60 text-dark-400 hover:text-dark-200 hover:bg-dark-700 border border-dark-700/50'
                  }`}
                  whileHover={{ scale: 1.03 }}
                  whileTap={{ scale: 0.97 }}
                >
                  {trafficPeriod === option.value && (
                    <motion.div
                      className="absolute inset-0 bg-gradient-to-r from-accent-500 to-accent-600 rounded-xl shadow-lg shadow-accent-500/20"
                      layoutId="trafficIndicator"
                      initial={false}
                      transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                    />
                  )}
                  <span className="relative z-10">
                    {option.value === 1 ? t('settings.traffic_1d') : 
                     option.value === 7 ? t('settings.traffic_7d') :
                     option.value === 30 ? t('settings.traffic_30d') :
                     t('settings.traffic_90d')}
                  </span>
                </motion.button>
              ))}
            </div>
          </LayoutGroup>
        </motion.div>
        
        {/* Layout */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="card group hover:border-dark-700 transition-all">
          <div className="flex items-center gap-3 mb-5">
            <motion.div 
              className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ rotate: 10, scale: 1.05 }}
            >
              <Layout className="w-5 h-5 text-accent-500" />
            </motion.div>
            <div>
              <h2 className="font-semibold text-dark-100">{t('settings.layout')}</h2>
              <p className="text-sm text-dark-500">{t('settings.layout_desc')}</p>
            </div>
          </div>
          
          <div className="flex gap-4">
            <motion.button
              onClick={() => setCompactView(false)}
              className={`flex-1 p-5 rounded-xl border-2 transition-all ${
                !compactView
                  ? 'border-accent-500 bg-accent-500/10 shadow-lg shadow-accent-500/10'
                  : 'border-dark-700/50 hover:border-dark-600 bg-dark-800/30'
              }`}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              <motion.div 
                className="grid grid-cols-2 gap-2 mb-4"
                animate={!compactView ? { scale: [1, 1.02, 1] } : {}}
                transition={{ duration: 2, repeat: Infinity }}
              >
                <div className={`h-10 rounded-lg ${!compactView ? 'bg-accent-500/30' : 'bg-dark-600'}`} />
                <div className={`h-10 rounded-lg ${!compactView ? 'bg-accent-500/30' : 'bg-dark-600'}`} />
              </motion.div>
              <span className={`text-sm font-medium flex items-center justify-center gap-2 ${
                !compactView ? 'text-accent-400' : 'text-dark-400'
              }`}>
                {!compactView && <Check className="w-4 h-4" />}
                {t('settings.grid_view')}
              </span>
            </motion.button>
            
            <motion.button
              onClick={() => setCompactView(true)}
              className={`flex-1 p-5 rounded-xl border-2 transition-all ${
                compactView
                  ? 'border-accent-500 bg-accent-500/10 shadow-lg shadow-accent-500/10'
                  : 'border-dark-700/50 hover:border-dark-600 bg-dark-800/30'
              }`}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              <motion.div 
                className="space-y-2 mb-4"
                animate={compactView ? { scale: [1, 1.02, 1] } : {}}
                transition={{ duration: 2, repeat: Infinity }}
              >
                <div className={`h-3 rounded ${compactView ? 'bg-accent-500/30' : 'bg-dark-600'}`} />
                <div className={`h-3 rounded ${compactView ? 'bg-accent-500/30' : 'bg-dark-600'}`} />
                <div className={`h-3 rounded ${compactView ? 'bg-accent-500/30' : 'bg-dark-600'}`} />
              </motion.div>
              <span className={`text-sm font-medium flex items-center justify-center gap-2 ${
                compactView ? 'text-accent-400' : 'text-dark-400'
              }`}>
                {compactView && <Check className="w-4 h-4" />}
                {t('settings.list_view')}
              </span>
            </motion.button>
          </div>
        </motion.div>
        
        {/* Collector Intervals */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="card group hover:border-dark-700 transition-all">
          <div className="flex items-center gap-3 mb-5">
            <motion.div 
              className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ scale: 1.1 }}
            >
              <Server className="w-5 h-5 text-accent-500" />
            </motion.div>
            <div>
              <h2 className="font-semibold text-dark-100">{t('settings.collector_intervals')}</h2>
              <p className="text-sm text-dark-500">{t('settings.collector_intervals_desc')}</p>
            </div>
          </div>
          
          <div className="space-y-5">
            {/* Metrics Interval */}
            <div>
              <div className="flex items-center gap-2 mb-3">
                <Activity className="w-4 h-4 text-dark-400" />
                <span className="text-sm text-dark-300">{t('settings.metrics_interval')}</span>
              </div>
              <LayoutGroup id="metrics-interval-selector">
                <div className="grid grid-cols-5 gap-2">
                  {METRICS_INTERVAL_OPTIONS.map((option) => (
                    <motion.button
                      key={option.value}
                      onClick={() => setMetricsCollectInterval(option.value)}
                      className={`relative px-3 py-2.5 rounded-xl text-sm font-medium transition-all ${
                        metricsCollectInterval === option.value
                          ? 'text-white'
                          : 'bg-dark-800/60 text-dark-400 hover:text-dark-200 hover:bg-dark-700 border border-dark-700/50'
                      }`}
                      whileHover={{ scale: 1.03 }}
                      whileTap={{ scale: 0.97 }}
                    >
                      {metricsCollectInterval === option.value && (
                        <motion.div
                          className="absolute inset-0 bg-gradient-to-r from-accent-500 to-accent-600 rounded-xl shadow-lg shadow-accent-500/20"
                          layoutId="metricsIntervalIndicator"
                          initial={false}
                          transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                        />
                      )}
                      <span className="relative z-10 flex items-center justify-center gap-1">
                        {option.label}
                        {option.recommended && (
                          <Zap className={`w-3 h-3 ${metricsCollectInterval === option.value ? 'text-white/80' : 'text-accent-500'}`} />
                        )}
                      </span>
                    </motion.button>
                  ))}
                </div>
              </LayoutGroup>
              <p className="text-xs text-dark-500 mt-2 flex items-center gap-1">
                <Zap className="w-3 h-3 text-accent-500" />
                {t('settings.recommended_values')}
              </p>
            </div>
            
            {/* HAProxy Interval */}
            <div>
              <div className="flex items-center gap-2 mb-3">
                <Shield className="w-4 h-4 text-dark-400" />
                <span className="text-sm text-dark-300">{t('settings.haproxy_interval')}</span>
              </div>
              <LayoutGroup id="haproxy-interval-selector">
                <div className="grid grid-cols-4 gap-2">
                  {HAPROXY_INTERVAL_OPTIONS.map((option) => (
                    <motion.button
                      key={option.value}
                      onClick={() => setHaproxyCollectInterval(option.value)}
                      className={`relative px-3 py-2.5 rounded-xl text-sm font-medium transition-all ${
                        haproxyCollectInterval === option.value
                          ? 'text-white'
                          : 'bg-dark-800/60 text-dark-400 hover:text-dark-200 hover:bg-dark-700 border border-dark-700/50'
                      }`}
                      whileHover={{ scale: 1.03 }}
                      whileTap={{ scale: 0.97 }}
                    >
                      {haproxyCollectInterval === option.value && (
                        <motion.div
                          className="absolute inset-0 bg-gradient-to-r from-accent-500 to-accent-600 rounded-xl shadow-lg shadow-accent-500/20"
                          layoutId="haproxyIntervalIndicator"
                          initial={false}
                          transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                        />
                      )}
                      <span className="relative z-10 flex items-center justify-center gap-1">
                        {option.label}
                        {option.recommended && (
                          <Zap className={`w-3 h-3 ${haproxyCollectInterval === option.value ? 'text-white/80' : 'text-accent-500'}`} />
                        )}
                      </span>
                    </motion.button>
                  ))}
                </div>
              </LayoutGroup>
            </div>
          </div>
        </motion.div>
        
        {/* Backup & Restore */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="card group hover:border-dark-700 transition-all">
          <div className="flex items-center gap-3 mb-5">
            <motion.div 
              className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ rotate: 10, scale: 1.05 }}
            >
              <Database className="w-5 h-5 text-accent-500" />
            </motion.div>
            <div className="flex-1">
              <h2 className="font-semibold text-dark-100">{t('settings.backup_title')}</h2>
              <p className="text-sm text-dark-500">{t('settings.backup_desc')}</p>
            </div>
            <motion.button
              onClick={handleCreateBackup}
              disabled={backupStatus?.state !== 'idle' && backupStatus !== null}
              className="btn btn-primary text-sm"
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              {backupStatus?.state === 'creating' ? (
                <><Loader2 className="w-4 h-4 animate-spin" />{t('settings.backup_creating')}</>
              ) : (
                <><Archive className="w-4 h-4" />{t('settings.backup_create')}</>
              )}
            </motion.button>
          </div>
          
          <div className="space-y-4">
            {/* Restore: file upload */}
            <div
              onDrop={handleDrop}
              onDragOver={e => e.preventDefault()}
              onClick={() => !(backupStatus?.state === 'restoring') && fileInputRef.current?.click()}
              className={`relative flex items-center justify-center gap-3 p-4 rounded-xl border-2 border-dashed transition-all cursor-pointer ${
                backupStatus?.state === 'restoring'
                  ? 'border-warning/40 bg-warning/5 cursor-not-allowed'
                  : 'border-dark-700/50 hover:border-accent-500/50 hover:bg-accent-500/5'
              }`}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept=".dump"
                onChange={handleFileSelect}
                className="hidden"
              />
              {backupStatus?.state === 'restoring' ? (
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
            
            {/* Backup status error */}
            <AnimatePresence>
              {backupStatus?.state === 'idle' && backupStatus.error && (
                <motion.div
                  initial={{ opacity: 0, y: -10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -10 }}
                  className="flex items-center gap-3 p-3 rounded-xl bg-danger/10 border border-danger/20"
                >
                  <XCircle className="w-4 h-4 text-danger flex-shrink-0" />
                  <span className="text-sm text-danger break-all">{backupStatus.error}</span>
                </motion.div>
              )}
            </AnimatePresence>
            
            {/* Backup list */}
            {backupLoading ? (
              <div className="flex items-center justify-center py-4">
                <Loader2 className="w-5 h-5 text-accent-500 animate-spin" />
              </div>
            ) : backups.length === 0 ? (
              <div className="text-sm text-dark-500 text-center py-3">{t('settings.backup_empty')}</div>
            ) : (
              <div className="space-y-2">
                <div className="text-xs text-dark-400 uppercase tracking-wider">{t('settings.backup_list_title')}</div>
                {backups.map(b => (
                  <motion.div
                    key={b.filename}
                    className="flex items-center gap-3 p-3 bg-dark-800/50 rounded-xl border border-dark-700/50"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                  >
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
                      <motion.button
                        onClick={() => handleDownloadBackup(b.filename)}
                        className="p-2 rounded-lg text-dark-400 hover:text-accent-400 hover:bg-dark-700/50 transition-colors"
                        whileHover={{ scale: 1.1 }}
                        whileTap={{ scale: 0.9 }}
                        title={t('settings.backup_download')}
                      >
                        <Download className="w-4 h-4" />
                      </motion.button>
                      {confirmDelete === b.filename ? (
                        <div className="flex items-center gap-1">
                          <motion.button
                            onClick={() => handleDeleteBackup(b.filename)}
                            className="p-2 rounded-lg text-danger hover:bg-danger/10 transition-colors"
                            whileTap={{ scale: 0.9 }}
                          >
                            <Check className="w-4 h-4" />
                          </motion.button>
                          <motion.button
                            onClick={() => setConfirmDelete(null)}
                            className="p-2 rounded-lg text-dark-400 hover:bg-dark-700/50 transition-colors"
                            whileTap={{ scale: 0.9 }}
                          >
                            <XCircle className="w-4 h-4" />
                          </motion.button>
                        </div>
                      ) : (
                        <motion.button
                          onClick={() => setConfirmDelete(b.filename)}
                          className="p-2 rounded-lg text-dark-400 hover:text-danger hover:bg-danger/10 transition-colors"
                          whileHover={{ scale: 1.1 }}
                          whileTap={{ scale: 0.9 }}
                          title={t('settings.backup_delete')}
                        >
                          <Trash2 className="w-4 h-4" />
                        </motion.button>
                      )}
                    </div>
                  </motion.div>
                ))}
              </div>
            )}
          </div>
        </motion.div>
        
        {/* Restore confirmation modal */}
        <AnimatePresence>
          {confirmRestore && (
            <motion.div
              className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              onClick={() => setConfirmRestore(null)}
            >
              <motion.div
                className="bg-dark-850 border border-dark-700 rounded-2xl p-6 max-w-md mx-4 shadow-2xl"
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
                <p className="text-xs text-dark-500 mb-5 font-mono bg-dark-800/50 rounded-lg px-3 py-2">
                  {confirmRestore.name} ({formatBytes(confirmRestore.size)})
                </p>
                <div className="flex gap-3">
                  <button
                    onClick={() => setConfirmRestore(null)}
                    className="flex-1 btn btn-secondary"
                  >
                    {t('common.cancel')}
                  </button>
                  <button
                    onClick={() => handleRestoreBackup(confirmRestore)}
                    className="flex-1 btn bg-warning/20 text-warning hover:bg-warning/30 border border-warning/30"
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
        </AnimatePresence>

        {/* SSL Certificate */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="card group hover:border-dark-700 transition-all">
          <div className="flex items-center gap-3 mb-5">
            <motion.div 
              className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ rotate: 10, scale: 1.05 }}
            >
              <Shield className="w-5 h-5 text-accent-500" />
            </motion.div>
            <div>
              <h2 className="font-semibold text-dark-100">{t('settings.ssl_certificate')}</h2>
              <p className="text-sm text-dark-500">{t('settings.ssl_certificate_desc')}</p>
            </div>
          </div>
          
          {certLoading ? (
            <div className="flex items-center justify-center py-4">
              <Loader2 className="w-5 h-5 text-accent-500 animate-spin" />
            </div>
          ) : certInfo?.error ? (
            <div className="flex items-center gap-3 p-4 bg-dark-800/50 rounded-xl border border-dark-700/50">
              <AlertTriangle className="w-5 h-5 text-warning flex-shrink-0" />
              <div>
                <p className="text-sm text-dark-300">
                  {certInfo.error === 'Domain not configured' 
                    ? t('settings.ssl_not_configured')
                    : certInfo.error === 'Certificate not found'
                    ? t('settings.ssl_not_found')
                    : t('settings.ssl_error')}
                </p>
                {certInfo.domain && (
                  <p className="text-xs text-dark-500 mt-1">{certInfo.domain}</p>
                )}
              </div>
            </div>
          ) : certInfo ? (
            <div className="space-y-4">
              {/* Certificate Info */}
              <div className="flex items-center justify-between p-4 bg-dark-800/50 rounded-xl border border-dark-700/50">
                <div className="space-y-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-dark-400">{t('settings.ssl_domain')}:</span>
                    <span className="text-sm text-dark-200 font-mono">{certInfo.domain}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-dark-400">{t('settings.ssl_expires')}:</span>
                    <span className="text-sm text-dark-200">
                      {certInfo.expiry_date && new Date(certInfo.expiry_date).toLocaleDateString()}
                    </span>
                  </div>
                </div>
                
                <div className="flex items-center gap-3">
                  {/* Days left badge */}
                  {certInfo.expired ? (
                    <span className="px-3 py-1.5 text-sm font-medium bg-danger/20 text-danger rounded-lg">
                      {t('settings.ssl_expired')}
                    </span>
                  ) : certInfo.days_left !== undefined && (
                    <span className={`px-3 py-1.5 text-sm font-medium rounded-lg ${
                      certInfo.days_left <= 7 
                        ? 'bg-danger/20 text-danger' 
                        : certInfo.days_left <= 30 
                        ? 'bg-warning/20 text-warning'
                        : 'bg-success/20 text-success'
                    }`}>
                      {t('settings.ssl_days_left', { days: certInfo.days_left })}
                    </span>
                  )}
                  
                  {/* Renew button */}
                  <motion.button
                    onClick={() => handleRenewCert()}
                    disabled={certRenewing}
                    className="btn btn-secondary text-sm"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {certRenewing ? (
                      <>
                        <Loader2 className="w-4 h-4 animate-spin" />
                        {t('settings.ssl_renewing')}
                      </>
                    ) : (
                      <>
                        <RefreshCw className="w-4 h-4" />
                        {t('settings.ssl_renew')}
                      </>
                    )}
                  </motion.button>
                </div>
              </div>
              
              {/* Renewal Status - In Progress */}
              <AnimatePresence mode="wait">
                {certRenewing && renewalPhase === 'starting' && (
                  <motion.div 
                    key="starting"
                    className="flex items-center gap-3 p-4 rounded-xl bg-primary/10 border border-primary/20"
                    initial={{ opacity: 0, y: -10 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -10 }}
                  >
                    <Loader2 className="w-5 h-5 animate-spin text-primary" />
                    <div className="flex-1">
                      <div className="text-sm font-medium text-primary">{t('settings.ssl_starting', 'Starting renewal...')}</div>
                    </div>
                  </motion.div>
                )}
                
                {certRenewing && renewalPhase === 'running' && (
                  <motion.div 
                    key="running"
                    className="flex items-center gap-3 p-4 rounded-xl bg-primary/10 border border-primary/20"
                    initial={{ opacity: 0, y: -10 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -10 }}
                  >
                    <Loader2 className="w-5 h-5 animate-spin text-primary" />
                    <div className="flex-1">
                      <div className="text-sm font-medium text-primary">{t('settings.ssl_renewing_status', 'Renewing certificate...')}</div>
                      <div className="text-xs text-dark-400 mt-1">{t('settings.ssl_renewing_desc', 'This may take a minute. nginx will be restarted.')}</div>
                    </div>
                  </motion.div>
                )}
                
                {certRenewing && renewalPhase === 'nginx_restarting' && (
                  <motion.div 
                    key="nginx"
                    className="flex items-center gap-3 p-4 rounded-xl bg-warning/10 border border-warning/20"
                    initial={{ opacity: 0, y: -10 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -10 }}
                  >
                    <RefreshCw className="w-5 h-5 animate-spin text-warning" />
                    <div className="flex-1">
                      <div className="text-sm font-medium text-warning">{t('settings.ssl_nginx_restarting', 'nginx is restarting...')}</div>
                      <div className="text-xs text-dark-400 mt-1">{t('settings.ssl_nginx_restarting_desc', 'Waiting for server to come back online. This is expected.')}</div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
              
              {/* Renewal Result */}
              <AnimatePresence>
                {certRenewResult && !certRenewing && (
                  <motion.div 
                    className="space-y-3"
                    initial={{ opacity: 0, y: -10 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -10 }}
                  >
                    {/* Result message */}
                    <div className={`flex items-start gap-3 p-4 rounded-xl text-sm ${
                      certRenewResult.success 
                        ? 'bg-success/10 text-success border border-success/20' 
                        : 'bg-danger/10 text-danger border border-danger/20'
                    }`}>
                      {certRenewResult.success ? (
                        <CheckCircle2 className="w-5 h-5 flex-shrink-0 mt-0.5" />
                      ) : (
                        <XCircle className="w-5 h-5 flex-shrink-0 mt-0.5" />
                      )}
                      <div className="flex-1 min-w-0">
                        <div className="font-medium">{certRenewResult.message}</div>
                        {certRenewResult.completedAt && (
                          <div className="text-xs opacity-70 mt-1">
                            {t('settings.ssl_completed_at', 'Completed')}: {new Date(certRenewResult.completedAt).toLocaleString()}
                          </div>
                        )}
                      </div>
                      {/* Toggle output button */}
                      {certRenewResult.output && (
                        <button
                          onClick={() => setShowOutput(!showOutput)}
                          className="flex items-center gap-1 text-xs px-2 py-1 rounded-lg bg-dark-800/50 hover:bg-dark-700/50 transition-colors"
                        >
                          <Terminal className="w-3.5 h-3.5" />
                          {showOutput ? t('common.hide', 'Hide') : t('common.show_log', 'Log')}
                        </button>
                      )}
                    </div>
                    
                    {/* Output log */}
                    <AnimatePresence>
                      {showOutput && certRenewResult.output && (
                        <motion.div
                          initial={{ opacity: 0, height: 0 }}
                          animate={{ opacity: 1, height: 'auto' }}
                          exit={{ opacity: 0, height: 0 }}
                          className="overflow-hidden"
                        >
                          <div className="bg-dark-900 border border-dark-700 rounded-xl p-4">
                            <div className="flex items-center gap-2 text-xs text-dark-400 mb-2">
                              <Terminal className="w-3.5 h-3.5" />
                              {t('settings.ssl_renewal_log', 'Renewal Output')}
                            </div>
                            <pre className="text-xs text-dark-300 whitespace-pre-wrap font-mono max-h-48 overflow-auto">
                              {certRenewResult.output}
                            </pre>
                          </div>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          ) : null}
        </motion.div>

        {/* Info */}
        <motion.div 
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
          className="card bg-dark-800/30 border-dark-700/30"
        >
          <div className="flex items-center gap-3">
            <motion.div
              animate={{ rotate: [0, 10, -10, 0] }}
              transition={{ duration: 4, repeat: Infinity, ease: 'easeInOut' }}
            >
              <SettingsIcon className="w-5 h-5 text-dark-500" />
            </motion.div>
            <div>
              <p className="text-sm text-dark-400">
                {t('settings.storage_notice')}
              </p>
            </div>
          </div>
        </motion.div>
      </div>
    </motion.div>
  )
}
