import { useEffect, useState, useCallback, useMemo } from 'react'
import { KeyRound, Shield, Lock, Loader2, Trash2, Plus, AlertTriangle, CheckCircle2, XCircle, ChevronDown, ChevronUp, Info, Copy, RefreshCw, Save, Eye, EyeOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import { sshSecurityApi, serversApi, Server as ServerType, SSHConfig, Fail2banConfig, SSHKey, SSHStatus, Fail2banBannedIP, BulkSSHResult, SSHPresets } from '../api/client'
import { Skeleton } from '../components/ui/Skeleton'
import { Tooltip } from '../components/ui/Tooltip'
import { FAQIcon } from '../components/FAQ'

type TabType = 'ssh' | 'fail2ban' | 'keys'

type DurationUnit = 'seconds' | 'minutes' | 'hours' | 'days'

const UNIT_MULTIPLIERS: Record<DurationUnit, number> = {
  seconds: 1,
  minutes: 60,
  hours: 3600,
  days: 86400,
}

function getBestUnit(secs: number): DurationUnit {
  if (secs > 0 && secs % 86400 === 0) return 'days'
  if (secs > 0 && secs % 3600 === 0) return 'hours'
  if (secs > 0 && secs % 60 === 0) return 'minutes'
  return 'seconds'
}

function DurationInput({ value, onChange, t }: { value: number; onChange: (v: number) => void; t: (key: string) => string }) {
  const [unit, setUnit] = useState<DurationUnit>(() => getBestUnit(value))
  const displayValue = Math.round(value / UNIT_MULTIPLIERS[unit])

  const handleValueChange = (raw: string) => {
    const num = parseInt(raw) || 0
    onChange(num * UNIT_MULTIPLIERS[unit])
  }

  const handleUnitChange = (newUnit: DurationUnit) => {
    setUnit(newUnit)
    onChange(displayValue * UNIT_MULTIPLIERS[newUnit])
  }

  return (
    <div className="flex items-center gap-2">
      <input
        type="number"
        value={displayValue}
        onChange={e => handleValueChange(e.target.value)}
        min={1}
        className="w-24 bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-dark-100 text-sm
                   focus:outline-none focus:border-accent-500 [appearance:textfield]
                   [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
      />
      <select
        value={unit}
        onChange={e => handleUnitChange(e.target.value as DurationUnit)}
        className="bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-dark-100 text-sm
                   focus:outline-none focus:border-accent-500"
      >
        <option value="seconds">{t('ssh_security.seconds')}</option>
        <option value="minutes">{t('ssh_security.minutes')}</option>
        <option value="hours">{t('ssh_security.hours')}</option>
        <option value="days">{t('ssh_security.days')}</option>
      </select>
    </div>
  )
}

function ToggleSwitch({ value, onChange, disabled }: { value: boolean; onChange: () => void; disabled?: boolean }) {
  return (
    <button
      onClick={onChange}
      disabled={disabled}
      className={`relative w-11 h-6 rounded-full transition-colors ${
        value ? 'bg-accent-500' : 'bg-dark-700'
      } ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
    >
      <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
        value ? 'translate-x-5' : ''
      }`} />
    </button>
  )
}

function NumberInput({ value, onChange, min, max }: { value: number; onChange: (v: number) => void; min: number; max: number }) {
  return (
    <input
      type="number"
      value={value}
      onChange={e => onChange(Math.min(max, Math.max(min, parseInt(e.target.value) || min)))}
      min={min}
      max={max}
      className="w-24 bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-dark-100 text-sm
                 focus:outline-none focus:border-accent-500 [appearance:textfield]
                 [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
    />
  )
}

interface SettingRowProps {
  label: string
  description: string
  children: React.ReactNode
}

function SettingRow({ label, description, children }: SettingRowProps) {
  return (
    <div className="flex items-center justify-between gap-6 py-3 border-b border-dark-800 last:border-0">
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-dark-100">{label}</div>
        <div className="text-xs text-dark-400 mt-0.5">{description}</div>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

function generatePassword(length: number = 24): string {
  const upper = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
  const lower = 'abcdefghijklmnopqrstuvwxyz'
  const digits = '0123456789'
  const special = '!@#$%^&*_+-='
  const all = upper + lower + digits + special
  const required = [
    upper[Math.floor(Math.random() * upper.length)],
    lower[Math.floor(Math.random() * lower.length)],
    digits[Math.floor(Math.random() * digits.length)],
    special[Math.floor(Math.random() * special.length)],
  ]
  const rest = Array.from({ length: length - required.length }, () =>
    all[Math.floor(Math.random() * all.length)]
  )
  return [...required, ...rest].sort(() => Math.random() - 0.5).join('')
}

type PasswordStrength = 'weak' | 'medium' | 'strong'

function checkPasswordStrength(password: string): PasswordStrength {
  if (password.length < 8) return 'weak'
  const hasUpper = /[A-Z]/.test(password)
  const hasLower = /[a-z]/.test(password)
  const hasDigit = /[0-9]/.test(password)
  const hasSpecial = /[^A-Za-z0-9]/.test(password)
  const score = [hasUpper, hasLower, hasDigit, hasSpecial].filter(Boolean).length
  if (password.length >= 16 && score >= 3) return 'strong'
  if (password.length >= 12 && score >= 3) return 'strong'
  if (score >= 3) return 'medium'
  return 'weak'
}

const STRENGTH_COLORS: Record<PasswordStrength, string> = {
  weak: 'text-red-400',
  medium: 'text-yellow-400',
  strong: 'text-emerald-400',
}

function formatPresetValue(key: string, value: unknown, t: (k: string) => string): string {
  if (typeof value === 'boolean') return value ? t('ssh_security.root_yes') : t('ssh_security.root_no')
  if (key === 'permit_root_login') {
    if (value === 'prohibit-password') return t('ssh_security.root_prohibit_password')
    if (value === 'no') return t('ssh_security.root_no')
    return t('ssh_security.root_yes')
  }
  if (key === 'ban_time' || key === 'find_time') {
    const secs = value as number
    if (secs >= 86400 && secs % 86400 === 0) return `${secs / 86400} ${t('ssh_security.days')}`
    if (secs >= 3600 && secs % 3600 === 0) return `${secs / 3600} ${t('ssh_security.hours')}`
    if (secs >= 60 && secs % 60 === 0) return `${secs / 60} ${t('ssh_security.minutes')}`
    return `${secs} ${t('ssh_security.seconds')}`
  }
  if (key === 'client_alive_interval' || key === 'login_grace_time') {
    return `${value} ${t('ssh_security.seconds')}`
  }
  if (Array.isArray(value)) {
    return value.length > 0 ? value.join(', ') : '—'
  }
  return String(value)
}

function presetKeyLabel(key: string, t: (k: string) => string): string {
  const map: Record<string, string> = {
    port: t('ssh_security.port'),
    permit_root_login: t('ssh_security.permit_root'),
    password_authentication: t('ssh_security.password_auth'),
    pubkey_authentication: t('ssh_security.pubkey_auth'),
    max_auth_tries: t('ssh_security.max_auth_tries'),
    login_grace_time: t('ssh_security.login_grace_time'),
    client_alive_interval: t('ssh_security.client_alive_interval'),
    client_alive_count_max: t('ssh_security.client_alive_count_max'),
    max_sessions: t('ssh_security.max_sessions'),
    x11_forwarding: t('ssh_security.x11_forwarding'),
    enabled: t('ssh_security.f2b_enabled'),
    max_retry: t('ssh_security.f2b_max_retry'),
    ban_time: t('ssh_security.f2b_ban_time'),
    find_time: t('ssh_security.f2b_find_time'),
  }
  return map[key] || key
}

interface PresetCardProps {
  type: string
  icon: React.ReactNode
  iconBg: string
  title: string
  desc: string
  preset: { ssh: Record<string, unknown>; fail2ban: Record<string, unknown> }
  applyingPreset: string | null
  bulkApplying: boolean
  onApply: () => void
  onBulk: () => void
  onDelete?: () => void
  t: (key: string) => string
}

function PresetCard({ type, icon, iconBg, title, desc, preset, applyingPreset, bulkApplying, onApply, onBulk, onDelete, t }: PresetCardProps) {
  const [expanded, setExpanded] = useState(false)

  const sshEntries = Object.entries(preset.ssh || {})
  const f2bEntries = Object.entries(preset.fail2ban || {})

  return (
    <motion.div
      className="card flex flex-col"
      whileHover={{ scale: 1.005 }}
      transition={{ type: 'spring', stiffness: 400 }}
    >
      <div className="flex items-center gap-3 mb-2">
        <div className={`p-2.5 ${iconBg} rounded-xl shrink-0`}>
          {icon}
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-dark-100">{title}</h3>
          <p className="text-xs text-dark-400">{desc}</p>
        </div>
        {onDelete && (
          <Tooltip label={t('common.delete')}>
            <button onClick={onDelete} className="p-1.5 text-dark-500 hover:text-red-400 transition-colors shrink-0">
              <Trash2 className="w-4 h-4" />
            </button>
          </Tooltip>
        )}
      </div>

      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-dark-500 hover:text-dark-300 transition-colors mb-2 self-start"
      >
        {expanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
        {t('ssh_security.preset_details')}
      </button>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="bg-dark-800/50 rounded-lg p-3 mb-3 text-xs space-y-2">
              <div className="text-dark-500 font-medium uppercase tracking-wider text-[10px] mb-1">SSH</div>
              {sshEntries.map(([key, val]) => (
                <div key={key} className="flex justify-between items-center">
                  <span className="text-dark-400">{presetKeyLabel(key, t)}</span>
                  <span className="text-dark-200 font-mono">{formatPresetValue(key, val, t)}</span>
                </div>
              ))}
              <div className="text-dark-500 font-medium uppercase tracking-wider text-[10px] mt-3 mb-1">Fail2ban</div>
              {f2bEntries.map(([key, val]) => (
                <div key={key} className="flex justify-between items-center">
                  <span className="text-dark-400">{presetKeyLabel(key, t)}</span>
                  <span className="text-dark-200 font-mono">{formatPresetValue(key, val, t)}</span>
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="flex gap-2 mt-auto pt-1">
        <motion.button
          onClick={onApply}
          disabled={applyingPreset !== null}
          className="btn btn-primary text-xs px-3 py-1.5"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          {applyingPreset === type ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
          {t('ssh_security.apply_preset')}
        </motion.button>
        <motion.button
          onClick={onBulk}
          disabled={bulkApplying}
          className="btn btn-secondary text-xs px-3 py-1.5"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          {t('ssh_security.apply_to_all')}
        </motion.button>
      </div>
    </motion.div>
  )
}

export default function SSHSecurity() {
  const { t } = useTranslation()

  const [servers, setServers] = useState<ServerType[]>([])
  const [selectedServerId, setSelectedServerId] = useState<number | null>(null)

  const [activeTab, setActiveTab] = useState<TabType>('ssh')

  const [loading, setLoading] = useState(true)
  const [applying, setApplying] = useState(false)
  const [applyingPreset, setApplyingPreset] = useState<string | null>(null)

  const [sshConfig, setSshConfig] = useState<SSHConfig | null>(null)
  const [editedConfig, setEditedConfig] = useState<Partial<SSHConfig>>({})

  const [fail2ban, setFail2ban] = useState<Fail2banConfig | null>(null)
  const [editedFail2ban, setEditedFail2ban] = useState<Partial<Fail2banConfig>>({})
  const [bannedIps, setBannedIps] = useState<Fail2banBannedIP[]>([])
  const [unbanningIp, setUnbanningIp] = useState<string | null>(null)
  const [unbanningAll, setUnbanningAll] = useState(false)

  const [sshKeys, setSshKeys] = useState<SSHKey[]>([])
  const [newKeyText, setNewKeyText] = useState('')
  const [addingKey, setAddingKey] = useState(false)
  const [removingKey, setRemovingKey] = useState<string | null>(null)

  const [status, setStatus] = useState<SSHStatus | null>(null)

  const [bulkResults, setBulkResults] = useState<BulkSSHResult[] | null>(null)
  const [bulkApplying, setBulkApplying] = useState(false)
  const [showBulkConfirm, setShowBulkConfirm] = useState<string | null>(null)

  const [presets, setPresets] = useState<SSHPresets | null>(null)

  // Password
  const [passwordValue, setPasswordValue] = useState('')
  const [passwordUser, setPasswordUser] = useState('root')
  const [changingPassword, setChangingPassword] = useState(false)
  const [changingPasswordAll, setChangingPasswordAll] = useState(false)
  const [showPassword, setShowPassword] = useState(false)

  // Custom presets
  const [customPresetName, setCustomPresetName] = useState('')
  const [savingPreset, setSavingPreset] = useState(false)

  const [nodeUnsupported, setNodeUnsupported] = useState(false)
  const mergedConfig = useMemo<SSHConfig | null>(() => {
    if (!sshConfig) return null
    return { ...sshConfig, ...editedConfig } as SSHConfig
  }, [sshConfig, editedConfig])

  const mergedFail2ban = useMemo<Fail2banConfig | null>(() => {
    if (!fail2ban) return null
    return { ...fail2ban, ...editedFail2ban } as Fail2banConfig
  }, [fail2ban, editedFail2ban])

  const hasSSHChanges = Object.keys(editedConfig).length > 0
  const hasFail2banChanges = Object.keys(editedFail2ban).length > 0
  const hasChanges = hasSSHChanges || hasFail2banChanges

  const fetchServers = useCallback(async () => {
    try {
      const response = await serversApi.list()
      setServers(response.data.servers)
      if (response.data.servers.length > 0 && !selectedServerId) {
        setSelectedServerId(response.data.servers[0].id)
      }
    } catch {
      toast.error(t('common.error'))
    }
  }, [selectedServerId, t])

  const fetchServerData = useCallback(async (serverId: number) => {
    setNodeUnsupported(false)
    try {
      const [configRes, fail2banRes, keysRes, statusRes] = await Promise.all([
        sshSecurityApi.getConfig(serverId),
        sshSecurityApi.getFail2ban(serverId).catch(() => null),
        sshSecurityApi.getKeys(serverId).catch(() => null),
        sshSecurityApi.getStatus(serverId).catch(() => null),
      ])

      setSshConfig(configRes.data.config)
      setEditedConfig({})

      if (fail2banRes) {
        setFail2ban(fail2banRes.data)
        setEditedFail2ban({})
      } else {
        setFail2ban(null)
        setEditedFail2ban({})
      }

      if (keysRes) {
        setSshKeys(keysRes.data.keys)
      } else {
        setSshKeys([])
      }

      if (statusRes) {
        setStatus(statusRes.data)
      } else {
        setStatus(null)
      }

      // Load banned IPs if fail2ban is available
      if (fail2banRes?.data?.installed && fail2banRes.data.enabled) {
        try {
          const bannedRes = await sshSecurityApi.getBanned(serverId)
          setBannedIps(bannedRes.data.ips)
        } catch {
          setBannedIps([])
        }
      } else {
        setBannedIps([])
      }
    } catch (err: any) {
      const statusCode = err.response?.status
      if (statusCode === 501 || statusCode === 503) {
        setNodeUnsupported(true)
        setSshConfig(null)
        setFail2ban(null)
        setSshKeys([])
        setStatus(null)
      } else {
        toast.error(t('ssh_security.config_failed'))
      }
    }
  }, [t])

  const fetchBannedIps = useCallback(async (serverId: number) => {
    try {
      const res = await sshSecurityApi.getBanned(serverId)
      setBannedIps(res.data.ips)
    } catch {
      setBannedIps([])
    }
  }, [])

  const fetchPresets = useCallback(async () => {
    try {
      const res = await sshSecurityApi.getPresets()
      setPresets(res.data)
    } catch {
      // presets are optional
    }
  }, [])

  useEffect(() => {
    const init = async () => {
      setLoading(true)
      await Promise.all([fetchServers(), fetchPresets()])
      setLoading(false)
    }
    init()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!selectedServerId) return
    fetchServerData(selectedServerId)
  }, [selectedServerId, fetchServerData])

  const updateSSHField = <K extends keyof SSHConfig>(key: K, value: SSHConfig[K]) => {
    if (sshConfig && sshConfig[key] === value) {
      setEditedConfig(prev => {
        const next = { ...prev }
        delete next[key]
        return next
      })
    } else {
      setEditedConfig(prev => ({ ...prev, [key]: value }))
    }
  }

  const updateFail2banField = <K extends keyof Fail2banConfig>(key: K, value: Fail2banConfig[K]) => {
    if (fail2ban && fail2ban[key] === value) {
      setEditedFail2ban(prev => {
        const next = { ...prev }
        delete next[key]
        return next
      })
    } else {
      setEditedFail2ban(prev => ({ ...prev, [key]: value }))
    }
  }

  const handleApply = async () => {
    if (!selectedServerId || !hasChanges) return
    setApplying(true)
    try {
      if (hasSSHChanges) {
        const res = await sshSecurityApi.updateConfig(selectedServerId, editedConfig)
        if (res.data.warnings?.length) {
          res.data.warnings.forEach(w => toast.warning(w))
        }
      }
      if (hasFail2banChanges) {
        await sshSecurityApi.updateFail2ban(selectedServerId, editedFail2ban)
      }
      toast.success(t('ssh_security.config_applied'))
      await fetchServerData(selectedServerId)
      if (hasFail2banChanges) {
        await fetchBannedIps(selectedServerId)
      }
    } catch (err: any) {
      const msg = err.response?.data?.detail || t('ssh_security.config_failed')
      toast.error(msg)
    } finally {
      setApplying(false)
    }
  }

  const handleReset = () => {
    setEditedConfig({})
    setEditedFail2ban({})
  }

  const handleBulkApply = async () => {
    if (!hasChanges) return
    setShowBulkConfirm(null)
    setBulkApplying(true)
    setBulkResults(null)
    try {
      const serverIds = servers.map(s => s.id)
      const results: BulkSSHResult[] = []
      if (hasSSHChanges) {
        const res = await sshSecurityApi.bulkConfig(serverIds, editedConfig)
        results.push(...res.data.results)
      }
      if (hasFail2banChanges) {
        const res = await sshSecurityApi.bulkFail2ban(serverIds, editedFail2ban)
        res.data.results.forEach(r => {
          const existing = results.find(e => e.server_id === r.server_id)
          if (existing && !r.success) {
            existing.success = false
            existing.error = [existing.error, r.error].filter(Boolean).join('; ')
          } else if (!existing) {
            results.push(r)
          }
        })
      }
      setBulkResults(results)
      showBulkToast(results)
      if (selectedServerId) {
        await fetchServerData(selectedServerId)
        await fetchBannedIps(selectedServerId)
      }
    } catch (err: any) {
      toast.error(err.response?.data?.detail || t('ssh_security.config_failed'))
    } finally {
      setBulkApplying(false)
    }
  }

  const handleApplyPreset = async (type: string) => {
    if (!presets || !selectedServerId) return
    let preset: { ssh: Record<string, unknown>; fail2ban: Record<string, unknown> }
    if (type === 'recommended' || type === 'maximum') {
      preset = presets[type]
    } else {
      const custom = presets.custom?.find(p => p.name === type)
      if (!custom) return
      preset = custom
    }
    setApplyingPreset(type)
    try {
      if (preset.ssh && Object.keys(preset.ssh).length > 0) {
        await sshSecurityApi.updateConfig(selectedServerId, preset.ssh)
      }
      if (preset.fail2ban && Object.keys(preset.fail2ban).length > 0) {
        await sshSecurityApi.updateFail2ban(selectedServerId, preset.fail2ban)
      }
      toast.success(t('ssh_security.preset_applied'))
      await fetchServerData(selectedServerId)
      await fetchBannedIps(selectedServerId)
    } catch (err: any) {
      toast.error(err.response?.data?.detail || t('ssh_security.config_failed'))
    } finally {
      setApplyingPreset(null)
    }
  }

  const handleBulkPreset = async (type: string) => {
    if (!presets) return
    setShowBulkConfirm(null)
    setBulkApplying(true)
    setBulkResults(null)
    let preset: { ssh: Record<string, unknown>; fail2ban: Record<string, unknown> }
    if (type === 'recommended' || type === 'maximum') {
      preset = presets[type]
    } else {
      const custom = presets.custom?.find(p => p.name === type)
      if (!custom) { setBulkApplying(false); return }
      preset = custom
    }
    try {
      const serverIds = servers.map(s => s.id)
      const results: BulkSSHResult[] = []
      if (preset.ssh && Object.keys(preset.ssh).length > 0) {
        const res = await sshSecurityApi.bulkConfig(serverIds, preset.ssh)
        results.push(...res.data.results)
      }
      if (preset.fail2ban && Object.keys(preset.fail2ban).length > 0) {
        const res = await sshSecurityApi.bulkFail2ban(serverIds, preset.fail2ban)
        res.data.results.forEach(r => {
          const existing = results.find(e => e.server_id === r.server_id)
          if (existing && !r.success) {
            existing.success = false
            existing.error = [existing.error, r.error].filter(Boolean).join('; ')
          } else if (!existing) {
            results.push(r)
          }
        })
      }
      setBulkResults(results)
      showBulkToast(results)
      if (selectedServerId) {
        await fetchServerData(selectedServerId)
        await fetchBannedIps(selectedServerId)
      }
    } catch (err: any) {
      toast.error(err.response?.data?.detail || t('ssh_security.config_failed'))
    } finally {
      setBulkApplying(false)
    }
  }

  const handleUnban = async (ip: string) => {
    if (!selectedServerId) return
    setUnbanningIp(ip)
    try {
      await sshSecurityApi.unbanIp(selectedServerId, ip)
      toast.success(t('ssh_security.ip_unbanned'))
      await fetchBannedIps(selectedServerId)
    } catch {
      toast.error(t('common.error'))
    } finally {
      setUnbanningIp(null)
    }
  }

  const handleUnbanAll = async () => {
    if (!selectedServerId) return
    setUnbanningAll(true)
    try {
      await sshSecurityApi.unbanAll(selectedServerId)
      toast.success(t('ssh_security.all_unbanned'))
      await fetchBannedIps(selectedServerId)
    } catch {
      toast.error(t('common.error'))
    } finally {
      setUnbanningAll(false)
    }
  }

  const handleAddKey = async () => {
    if (!selectedServerId || !newKeyText.trim()) return
    setAddingKey(true)
    try {
      await sshSecurityApi.addKey(selectedServerId, newKeyText.trim())
      toast.success(t('ssh_security.key_added'))
      setNewKeyText('')
      const res = await sshSecurityApi.getKeys(selectedServerId)
      setSshKeys(res.data.keys)
    } catch (err: any) {
      toast.error(err.response?.data?.detail || t('common.error'))
    } finally {
      setAddingKey(false)
    }
  }

  const handleRemoveKey = async (fingerprint: string) => {
    if (!selectedServerId) return
    setRemovingKey(fingerprint)
    try {
      await sshSecurityApi.removeKey(selectedServerId, fingerprint)
      toast.success(t('ssh_security.key_removed'))
      const res = await sshSecurityApi.getKeys(selectedServerId)
      setSshKeys(res.data.keys)
    } catch (err: any) {
      toast.error(err.response?.data?.detail || t('common.error'))
    } finally {
      setRemovingKey(null)
    }
  }

  const handleChangePassword = async () => {
    if (!selectedServerId || !passwordValue) return
    setChangingPassword(true)
    try {
      await sshSecurityApi.changePassword(selectedServerId, passwordValue, passwordUser)
      toast.success(t('ssh_security.password_changed'))
      setPasswordValue('')
    } catch (err: any) {
      toast.error(err.response?.data?.detail || t('ssh_security.password_failed'))
    } finally {
      setChangingPassword(false)
    }
  }

  const handleChangePasswordAll = async () => {
    if (!passwordValue) return
    setChangingPasswordAll(true)
    setBulkResults(null)
    try {
      const serverIds = servers.map(s => s.id)
      const res = await sshSecurityApi.bulkPassword(serverIds, passwordValue, passwordUser)
      setBulkResults(res.data.results)
      showBulkToast(res.data.results)
      setPasswordValue('')
    } catch (err: any) {
      toast.error(err.response?.data?.detail || t('ssh_security.password_failed'))
    } finally {
      setChangingPasswordAll(false)
    }
  }

  const handleSaveCustomPreset = async () => {
    if (!customPresetName.trim() || !mergedConfig) return
    setSavingPreset(true)
    try {
      const sshData: Record<string, unknown> = { ...mergedConfig }
      const f2bData: Record<string, unknown> = mergedFail2ban ? { ...mergedFail2ban } : {}
      delete (f2bData as any).installed
      const res = await sshSecurityApi.saveCustomPreset(customPresetName.trim(), sshData, f2bData)
      if (presets) {
        setPresets({ ...presets, custom: res.data.presets })
      }
      toast.success(t('ssh_security.custom_preset_saved'))
      setCustomPresetName('')
    } catch (err: any) {
      toast.error(err.response?.data?.detail || t('common.error'))
    } finally {
      setSavingPreset(false)
    }
  }

  const handleDeleteCustomPreset = async (name: string) => {
    try {
      const res = await sshSecurityApi.deleteCustomPreset(name)
      if (presets) {
        setPresets({ ...presets, custom: res.data.presets })
      }
      toast.success(t('ssh_security.custom_preset_deleted'))
    } catch {
      toast.error(t('common.error'))
    }
  }

  const showBulkToast = (results: BulkSSHResult[]) => {
    const total = results.length
    const ok = results.filter(r => r.success).length
    const failed = total - ok
    if (failed === 0) {
      toast.success(t('ssh_security.bulk_all_ok', { ok, total }))
    } else if (ok === 0) {
      toast.error(t('ssh_security.bulk_all_failed', { total }))
    } else {
      toast.warning(t('ssh_security.bulk_partial', { ok, total, failed }))
    }
  }

  const passwordStrength = passwordValue ? checkPasswordStrength(passwordValue) : null

  const formatBanRemaining = (seconds: number): string => {
    if (seconds <= 0) return '< 1s'
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    const s = seconds % 60
    const parts: string[] = []
    if (h > 0) parts.push(`${h}h`)
    if (m > 0) parts.push(`${m}m`)
    if (s > 0 && h === 0) parts.push(`${s}s`)
    return parts.join(' ')
  }

  if (loading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
        <div className="flex items-center gap-3">
          <Skeleton className="w-10 h-10 rounded-xl" />
          <div>
            <Skeleton className="h-6 w-48 mb-2" />
            <Skeleton className="h-4 w-80" />
          </div>
        </div>
        <Skeleton className="h-12 w-72 rounded-xl" />
        <div className="flex gap-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-32 rounded-xl" />
          ))}
        </div>
        <div className="card">
          <Skeleton className="h-5 w-36 mb-4" />
          <div className="space-y-4">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="flex justify-between items-center">
                <div>
                  <Skeleton className="h-4 w-40 mb-1" />
                  <Skeleton className="h-3 w-64" />
                </div>
                <Skeleton className="h-8 w-24 rounded-lg" />
              </div>
            ))}
          </div>
        </div>
      </motion.div>
    )
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="space-y-6"
    >
      {/* Header */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="flex items-center justify-between flex-wrap gap-4"
      >
        <div className="flex items-center gap-3">
          <KeyRound className="w-7 h-7 text-accent-400" />
          <div>
            <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-2">
              {t('ssh_security.title')}
              <FAQIcon screen="PAGE_SSH_SECURITY" />
            </h1>
            <p className="text-dark-400 text-sm">{t('ssh_security.subtitle')}</p>
          </div>
        </div>

        {status && (
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`px-2.5 py-1 rounded-full text-xs font-medium ${
              status.sshd_running
                ? 'bg-emerald-500/15 text-emerald-400'
                : 'bg-red-500/15 text-red-400'
            }`}>
              {status.sshd_running ? t('ssh_security.status_online') : t('ssh_security.status_offline')}
            </span>
            <span className="px-2.5 py-1 rounded-full text-xs font-medium bg-dark-700 text-dark-300">
              {t('ssh_security.status_port')}: {status.sshd_port}
            </span>
            <span className="px-2.5 py-1 rounded-full text-xs font-medium bg-dark-700 text-dark-300">
              {t('ssh_security.status_auth')}: {status.auth_method}
            </span>
            <span className={`px-2.5 py-1 rounded-full text-xs font-medium ${
              status.fail2ban_running
                ? 'bg-emerald-500/15 text-emerald-400'
                : 'bg-dark-700 text-dark-400'
            }`}>
              {status.fail2ban_running ? t('ssh_security.status_f2b_active') : t('ssh_security.status_f2b_inactive')}
              {status.fail2ban_running && status.fail2ban_banned_count > 0 && (
                <> ({status.fail2ban_banned_count} {t('ssh_security.status_banned')})</>
              )}
            </span>
          </div>
        )}
      </motion.div>

      {/* Server Selector — button grid */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.05 }}
      >
        {servers.length === 0 ? (
          <p className="text-sm text-dark-400">{t('ssh_security.no_servers')}</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {servers.map(server => (
              <motion.button
                key={server.id}
                onClick={() => setSelectedServerId(server.id)}
                className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-all border ${
                  server.id === selectedServerId
                    ? 'bg-accent-500/15 text-accent-400 border-accent-500/30'
                    : 'bg-dark-800/60 text-dark-300 border-dark-700/50 hover:bg-dark-800 hover:text-dark-100 hover:border-dark-600'
                }`}
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.97 }}
              >
                <div className={`w-2 h-2 rounded-full shrink-0 ${server.is_active ? 'bg-emerald-400' : 'bg-dark-500'}`} />
                <span className="truncate max-w-[140px]">{server.name}</span>
              </motion.button>
            ))}
          </div>
        )}
      </motion.div>

      {/* Node unsupported message */}
      {nodeUnsupported && selectedServerId && (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex items-start gap-4 p-5 bg-orange-500/10 border border-orange-500/20 rounded-xl"
        >
          <AlertTriangle className="w-6 h-6 text-orange-400 mt-0.5 shrink-0" />
          <div>
            <h3 className="text-sm font-semibold text-orange-300 mb-1">{t('ssh_security.node_unsupported')}</h3>
            <p className="text-xs text-dark-400">{t('ssh_security.node_unsupported_desc')}</p>
          </div>
        </motion.div>
      )}

      {/* Presets */}
      {presets && selectedServerId && !nodeUnsupported && (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.1 }}
          className="grid grid-cols-1 md:grid-cols-2 gap-4"
        >
          <PresetCard
            type="recommended"
            icon={<Shield className="w-5 h-5 text-accent-400" />}
            iconBg="bg-accent-500/15"
            title={t('ssh_security.preset_recommended')}
            desc={t('ssh_security.preset_recommended_desc')}
            preset={presets.recommended}
            applyingPreset={applyingPreset}
            bulkApplying={bulkApplying}
            onApply={() => handleApplyPreset('recommended')}
            onBulk={() => setShowBulkConfirm('preset-recommended')}
            t={t}
          />
          <PresetCard
            type="maximum"
            icon={<Lock className="w-5 h-5 text-orange-400" />}
            iconBg="bg-orange-500/15"
            title={t('ssh_security.preset_maximum')}
            desc={t('ssh_security.preset_maximum_desc')}
            preset={presets.maximum}
            applyingPreset={applyingPreset}
            bulkApplying={bulkApplying}
            onApply={() => handleApplyPreset('maximum')}
            onBulk={() => setShowBulkConfirm('preset-maximum')}
            t={t}
          />
          {presets.custom?.map(cp => (
            <PresetCard
              key={cp.name}
              type={cp.name}
              icon={<Save className="w-5 h-5 text-purple-400" />}
              iconBg="bg-purple-500/15"
              title={cp.name}
              desc={t('ssh_security.custom_presets')}
              preset={cp}
              applyingPreset={applyingPreset}
              bulkApplying={bulkApplying}
              onApply={() => handleApplyPreset(cp.name)}
              onBulk={() => setShowBulkConfirm(`preset-${cp.name}`)}
              onDelete={() => handleDeleteCustomPreset(cp.name)}
              t={t}
            />
          ))}
        </motion.div>
      )}

      {/* Save custom preset */}
      {selectedServerId && !nodeUnsupported && mergedConfig && (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.12 }}
          className="flex items-center gap-3"
        >
          <input
            type="text"
            value={customPresetName}
            onChange={e => setCustomPresetName(e.target.value)}
            placeholder={t('ssh_security.custom_preset_name')}
            className="bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-dark-100 text-sm
                       focus:outline-none focus:border-accent-500 w-48"
          />
          <motion.button
            onClick={handleSaveCustomPreset}
            disabled={savingPreset || !customPresetName.trim()}
            className="btn btn-secondary text-xs px-3 py-2 flex items-center gap-1.5"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            {savingPreset ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            {t('ssh_security.custom_preset_save_current')}
          </motion.button>
        </motion.div>
      )}

      {/* Tabs */}
      {selectedServerId && !nodeUnsupported && (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.15 }}
          className="flex gap-2 border-b border-dark-700 pb-2"
        >
          {(['ssh', 'fail2ban', 'keys'] as const).map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                activeTab === tab
                  ? 'bg-accent-500/20 text-accent-400'
                  : 'text-dark-400 hover:text-dark-200 hover:bg-dark-800'
              }`}
            >
              {t(`ssh_security.tab_${tab}`)}
            </button>
          ))}
        </motion.div>
      )}

      {/* Tab Content */}
      {selectedServerId && !nodeUnsupported && (
        <AnimatePresence mode="wait">
          {/* SSH Settings Tab */}
          {activeTab === 'ssh' && mergedConfig && (
            <motion.div
              key="ssh"
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: 20 }}
              className="space-y-4"
            >
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {/* Access section */}
                <div className="card">
                  <h3 className="text-lg font-semibold text-dark-100 mb-1 flex items-center gap-2">
                    {t('ssh_security.section_access')}
                    <FAQIcon screen="SSH_SECURITY_SSHD" size="sm" />
                  </h3>
                  <div className="divide-y divide-dark-800">
                    <SettingRow label={t('ssh_security.port')} description={t('ssh_security.port_desc')}>
                      <NumberInput
                        value={mergedConfig.port}
                        onChange={v => updateSSHField('port', v)}
                        min={1}
                        max={65535}
                      />
                    </SettingRow>

                    <SettingRow label={t('ssh_security.permit_root')} description={t('ssh_security.permit_root_desc')}>
                      <select
                        value={mergedConfig.permit_root_login}
                        onChange={e => updateSSHField('permit_root_login', e.target.value as SSHConfig['permit_root_login'])}
                        className="bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-dark-100 text-sm
                                   focus:outline-none focus:border-accent-500"
                      >
                        <option value="yes">{t('ssh_security.root_yes')}</option>
                        <option value="no">{t('ssh_security.root_no')}</option>
                        <option value="prohibit-password">{t('ssh_security.root_prohibit_password')}</option>
                      </select>
                    </SettingRow>

                    <SettingRow label={t('ssh_security.password_auth')} description={t('ssh_security.password_auth_desc')}>
                      <ToggleSwitch
                        value={mergedConfig.password_authentication}
                        onChange={() => updateSSHField('password_authentication', !mergedConfig.password_authentication)}
                      />
                    </SettingRow>

                    <SettingRow label={t('ssh_security.pubkey_auth')} description={t('ssh_security.pubkey_auth_desc')}>
                      <ToggleSwitch
                        value={mergedConfig.pubkey_authentication}
                        onChange={() => updateSSHField('pubkey_authentication', !mergedConfig.pubkey_authentication)}
                      />
                    </SettingRow>

                    <SettingRow label={t('ssh_security.allow_users')} description={t('ssh_security.allow_users_desc')}>
                      <input
                        type="text"
                        value={(mergedConfig.allow_users ?? []).join(' ')}
                        onChange={e => {
                          const val = e.target.value.trim()
                          updateSSHField('allow_users', val ? val.split(/\s+/) : [])
                        }}
                        placeholder={t('ssh_security.allow_users_placeholder')}
                        className="w-40 bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-dark-100 text-sm
                                   focus:outline-none focus:border-accent-500"
                      />
                    </SettingRow>

                    <SettingRow label={t('ssh_security.x11_forwarding')} description={t('ssh_security.x11_forwarding_desc')}>
                      <ToggleSwitch
                        value={mergedConfig.x11_forwarding}
                        onChange={() => updateSSHField('x11_forwarding', !mergedConfig.x11_forwarding)}
                      />
                    </SettingRow>
                  </div>
                </div>

                {/* Limits section */}
                <div className="card">
                  <h3 className="text-lg font-semibold text-dark-100 mb-1">{t('ssh_security.section_limits')}</h3>
                  <div className="divide-y divide-dark-800">
                    <SettingRow label={t('ssh_security.max_auth_tries')} description={t('ssh_security.max_auth_tries_desc')}>
                      <NumberInput
                        value={mergedConfig.max_auth_tries}
                        onChange={v => updateSSHField('max_auth_tries', v)}
                        min={1}
                        max={10}
                      />
                    </SettingRow>

                    <SettingRow label={t('ssh_security.login_grace_time')} description={t('ssh_security.login_grace_time_desc')}>
                      <NumberInput
                        value={mergedConfig.login_grace_time}
                        onChange={v => updateSSHField('login_grace_time', v)}
                        min={10}
                        max={600}
                      />
                    </SettingRow>

                    <SettingRow label={t('ssh_security.max_sessions')} description={t('ssh_security.max_sessions_desc')}>
                      <NumberInput
                        value={mergedConfig.max_sessions}
                        onChange={v => updateSSHField('max_sessions', v)}
                        min={1}
                        max={20}
                      />
                    </SettingRow>

                    <SettingRow label={t('ssh_security.client_alive_interval')} description={t('ssh_security.client_alive_interval_desc')}>
                      <NumberInput
                        value={mergedConfig.client_alive_interval}
                        onChange={v => updateSSHField('client_alive_interval', v)}
                        min={0}
                        max={3600}
                      />
                    </SettingRow>

                    <SettingRow label={t('ssh_security.client_alive_count_max')} description={t('ssh_security.client_alive_count_max_desc')}>
                      <NumberInput
                        value={mergedConfig.client_alive_count_max}
                        onChange={v => updateSSHField('client_alive_count_max', v)}
                        min={1}
                        max={10}
                      />
                    </SettingRow>
                  </div>
                </div>
              </div>

              {/* Password section */}
              <div className="card">
                <h3 className="text-lg font-semibold text-dark-100 mb-3">{t('ssh_security.password_title')}</h3>
                <div className="grid grid-cols-1 sm:grid-cols-[120px_1fr] gap-x-4 gap-y-3 items-start">
                  <label className="text-sm text-dark-400 pt-2">{t('ssh_security.password_user')}</label>
                  <input
                    type="text"
                    value={passwordUser}
                    onChange={e => setPasswordUser(e.target.value)}
                    className="w-40 bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-dark-100 text-sm
                               focus:outline-none focus:border-accent-500"
                  />

                  <label className="text-sm text-dark-400 pt-2">{t('ssh_security.password_new')}</label>
                  <div className="space-y-2">
                    <div className="flex items-center gap-2">
                      <div className="relative flex-1">
                        <input
                          type={showPassword ? 'text' : 'password'}
                          value={passwordValue}
                          onChange={e => setPasswordValue(e.target.value)}
                          className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 pr-9 text-dark-100 text-sm font-mono
                                     focus:outline-none focus:border-accent-500"
                        />
                        <button
                          type="button"
                          onClick={() => setShowPassword(!showPassword)}
                          className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-500 hover:text-dark-300"
                        >
                          {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                        </button>
                      </div>
                      <Tooltip label={t('ssh_security.password_generate')}>
                        <motion.button
                          onClick={() => {
                            const pwd = generatePassword(24)
                            setPasswordValue(pwd)
                            setShowPassword(true)
                          }}
                          className="btn btn-secondary text-xs px-2.5 py-2 shrink-0"
                          whileHover={{ scale: 1.02 }}
                          whileTap={{ scale: 0.98 }}
                        >
                          <RefreshCw className="w-4 h-4" />
                        </motion.button>
                      </Tooltip>
                      {passwordValue && (
                        <motion.button
                          onClick={() => {
                            navigator.clipboard.writeText(passwordValue)
                            toast.success(t('ssh_security.password_copied'))
                          }}
                          className="btn btn-secondary text-xs px-2.5 py-2 shrink-0"
                          whileHover={{ scale: 1.02 }}
                          whileTap={{ scale: 0.98 }}
                        >
                          <Copy className="w-4 h-4" />
                        </motion.button>
                      )}
                    </div>
                    {passwordValue && (
                      <div className="flex items-center gap-2">
                        {passwordValue.length < 8 ? (
                          <span className="text-xs text-red-400">{t('ssh_security.password_too_short')}</span>
                        ) : (
                          <>
                            <div className="flex gap-1">
                              {[1, 2, 3].map(i => (
                                <div key={i} className={`h-1.5 w-8 rounded-full ${
                                  i <= (passwordStrength === 'strong' ? 3 : passwordStrength === 'medium' ? 2 : 1)
                                    ? passwordStrength === 'strong' ? 'bg-emerald-400' : passwordStrength === 'medium' ? 'bg-yellow-400' : 'bg-red-400'
                                    : 'bg-dark-700'
                                }`} />
                              ))}
                            </div>
                            <span className={`text-xs ${STRENGTH_COLORS[passwordStrength!]}`}>
                              {passwordStrength === 'strong' ? t('ssh_security.password_strong')
                                : passwordStrength === 'medium' ? t('ssh_security.password_medium')
                                : t('ssh_security.password_weak')}
                            </span>
                          </>
                        )}
                      </div>
                    )}
                    <div className="flex gap-2 pt-1">
                      <motion.button
                        onClick={handleChangePassword}
                        disabled={changingPassword || !passwordValue || passwordValue.length < 8}
                        className="btn btn-primary text-xs px-3 py-1.5"
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.98 }}
                      >
                        {changingPassword ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                        {t('ssh_security.password_apply')}
                      </motion.button>
                      <motion.button
                        onClick={handleChangePasswordAll}
                        disabled={changingPasswordAll || !passwordValue || passwordValue.length < 8}
                        className="btn btn-secondary text-xs px-3 py-1.5"
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.98 }}
                      >
                        {changingPasswordAll ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                        {t('ssh_security.password_apply_all')}
                      </motion.button>
                    </div>
                  </div>
                </div>
              </div>
            </motion.div>
          )}

          {/* Fail2ban Tab */}
          {activeTab === 'fail2ban' && (
            <motion.div
              key="fail2ban"
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: 20 }}
            >
              {fail2ban && !fail2ban.installed && (
                <div className="flex items-start gap-3 p-3 bg-blue-500/10 border border-blue-500/20 rounded-lg mb-4">
                  <Info className="w-4 h-4 text-blue-400 mt-0.5 shrink-0" />
                  <p className="text-sm text-blue-300">{t('ssh_security.f2b_not_installed')}</p>
                </div>
              )}

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {/* Settings */}
                <div className="card">
                  <h3 className="text-lg font-semibold text-dark-100 mb-4 flex items-center gap-2">
                    {t('ssh_security.f2b_title')}
                    <FAQIcon screen="SSH_SECURITY_FAIL2BAN" size="sm" />
                  </h3>
                  {mergedFail2ban && (
                    <>
                      <div className="flex items-center justify-between pb-4 border-b border-dark-800">
                        <div className="text-sm font-medium text-dark-100">
                          {mergedFail2ban.enabled ? t('ssh_security.f2b_enabled') : t('ssh_security.f2b_disabled')}
                        </div>
                        <ToggleSwitch
                          value={mergedFail2ban.enabled}
                          onChange={() => updateFail2banField('enabled', !mergedFail2ban.enabled)}
                        />
                      </div>
                      <AnimatePresence>
                        {mergedFail2ban.enabled && (
                          <motion.div
                            initial={{ opacity: 0, height: 0 }}
                            animate={{ opacity: 1, height: 'auto' }}
                            exit={{ opacity: 0, height: 0 }}
                            className="overflow-hidden"
                          >
                            <div className="divide-y divide-dark-800 pt-2">
                              <SettingRow label={t('ssh_security.f2b_max_retry')} description={t('ssh_security.f2b_max_retry_desc')}>
                                <NumberInput
                                  value={mergedFail2ban.max_retry}
                                  onChange={v => updateFail2banField('max_retry', v)}
                                  min={1}
                                  max={20}
                                />
                              </SettingRow>
                              <SettingRow label={t('ssh_security.f2b_ban_time')} description={t('ssh_security.f2b_ban_time_desc')}>
                                <DurationInput
                                  value={mergedFail2ban.ban_time}
                                  onChange={v => updateFail2banField('ban_time', v)}
                                  t={t}
                                />
                              </SettingRow>
                              <SettingRow label={t('ssh_security.f2b_find_time')} description={t('ssh_security.f2b_find_time_desc')}>
                                <DurationInput
                                  value={mergedFail2ban.find_time}
                                  onChange={v => updateFail2banField('find_time', v)}
                                  t={t}
                                />
                              </SettingRow>
                            </div>
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </>
                  )}
                </div>

                {/* Banned IPs */}
                {mergedFail2ban?.enabled && (
                  <div className="card">
                    <div className="flex items-center justify-between mb-4">
                      <h3 className="text-lg font-semibold text-dark-100">{t('ssh_security.f2b_banned_ips')}</h3>
                      {bannedIps.length > 0 && (
                        <motion.button
                          onClick={handleUnbanAll}
                          disabled={unbanningAll}
                          className="btn btn-secondary text-xs px-3 py-1.5"
                          whileHover={{ scale: 1.02 }}
                          whileTap={{ scale: 0.98 }}
                        >
                          {unbanningAll ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                          {t('ssh_security.f2b_unban_all')}
                        </motion.button>
                      )}
                    </div>
                    {bannedIps.length === 0 ? (
                      <p className="text-dark-400 text-center py-6 text-sm">{t('ssh_security.f2b_no_banned')}</p>
                    ) : (
                      <div className="space-y-2 max-h-72 overflow-y-auto">
                        {bannedIps.map(banned => (
                          <div
                            key={banned.ip}
                            className="flex items-center justify-between p-3 bg-dark-800/50 rounded-lg border border-dark-700/50"
                          >
                            <div className="flex items-center gap-3">
                              <code className="text-sm text-dark-200 font-mono">{banned.ip}</code>
                              <span className="text-xs text-dark-500">
                                {formatBanRemaining(banned.ban_time_remaining)}
                              </span>
                            </div>
                            <motion.button
                              onClick={() => handleUnban(banned.ip)}
                              disabled={unbanningIp === banned.ip}
                              className="text-xs text-dark-400 hover:text-accent-400 transition-colors px-2 py-1"
                              whileHover={{ scale: 1.05 }}
                              whileTap={{ scale: 0.95 }}
                            >
                              {unbanningIp === banned.ip ? (
                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                              ) : (
                                t('ssh_security.f2b_unban')
                              )}
                            </motion.button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </motion.div>
          )}

          {/* SSH Keys Tab */}
          {activeTab === 'keys' && (
            <motion.div
              key="keys"
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: 20 }}
              className="space-y-4"
            >
              {/* Warning */}
              {sshKeys.length === 0 && mergedConfig && !mergedConfig.password_authentication && (
                <div className="flex items-start gap-3 p-4 bg-orange-500/10 border border-orange-500/20 rounded-lg">
                  <AlertTriangle className="w-5 h-5 text-orange-400 mt-0.5 shrink-0" />
                  <p className="text-sm text-orange-300">{t('ssh_security.keys_warning_no_keys')}</p>
                </div>
              )}

              <div className="grid grid-cols-1 lg:grid-cols-[1fr_auto] gap-4">
                {/* Keys Table */}
                <div className="card">
                  <h3 className="text-lg font-semibold text-dark-100 mb-4 flex items-center gap-2">
                    {t('ssh_security.keys_title')}
                    <FAQIcon screen="SSH_SECURITY_KEYS" size="sm" />
                  </h3>
                  {sshKeys.length === 0 ? (
                    <p className="text-dark-400 text-center py-6 text-sm">{t('ssh_security.keys_no_keys')}</p>
                  ) : (
                    <div className="space-y-2 max-h-80 overflow-y-auto">
                      <div className="grid grid-cols-[1fr_80px_1fr_40px] gap-3 px-3 py-2 text-xs text-dark-500 font-medium">
                        <span>{t('ssh_security.keys_fingerprint')}</span>
                        <span>{t('ssh_security.keys_type')}</span>
                        <span>{t('ssh_security.keys_comment')}</span>
                        <span />
                      </div>
                      {sshKeys.map(key => (
                        <div
                          key={key.fingerprint}
                          className="grid grid-cols-[1fr_80px_1fr_40px] gap-3 items-center p-3 bg-dark-800/50 rounded-lg border border-dark-700/50"
                        >
                          <code className="text-xs text-dark-200 font-mono truncate" title={key.fingerprint}>
                            {key.fingerprint}
                          </code>
                          <span className="text-xs text-dark-400">{key.type}</span>
                          <span className="text-xs text-dark-400 truncate" title={key.comment}>
                            {key.comment || '—'}
                          </span>
                          <Tooltip label={t('common.delete')}>
                            <button
                              onClick={() => handleRemoveKey(key.fingerprint)}
                              disabled={removingKey === key.fingerprint}
                              className="p-1.5 text-dark-400 hover:text-danger transition-colors justify-self-end"
                            >
                              {removingKey === key.fingerprint ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                              ) : (
                                <Trash2 className="w-4 h-4" />
                              )}
                            </button>
                          </Tooltip>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Add Key Form */}
                <div className="card lg:w-80">
                  <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('ssh_security.keys_add')}</h3>
                  <textarea
                    value={newKeyText}
                    onChange={e => setNewKeyText(e.target.value)}
                    placeholder={t('ssh_security.keys_add_placeholder')}
                    rows={4}
                    className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-dark-100 text-sm
                               font-mono resize-none focus:outline-none focus:border-accent-500 mb-3"
                  />
                  <motion.button
                    onClick={handleAddKey}
                    disabled={addingKey || !newKeyText.trim()}
                    className="btn btn-primary w-full"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {addingKey ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                    {t('ssh_security.keys_add')}
                  </motion.button>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      )}

      {/* Bulk Results */}
      <AnimatePresence>
        {bulkResults && (() => {
          const totalCount = bulkResults.length
          const okCount = bulkResults.filter(r => r.success).length
          const failedCount = totalCount - okCount
          const allOk = failedCount === 0
          const allFailed = okCount === 0

          return (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 10 }}
              className="card"
            >
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-3">
                  <h3 className="text-sm font-semibold text-dark-100">{t('ssh_security.bulk_summary')}</h3>
                  <span className={`px-2.5 py-0.5 rounded-full text-xs font-medium ${
                    allOk ? 'bg-emerald-500/15 text-emerald-400'
                    : allFailed ? 'bg-red-500/15 text-red-400'
                    : 'bg-orange-500/15 text-orange-400'
                  }`}>
                    {allOk ? <>{t('ssh_security.bulk_summary_ok', { ok: okCount, total: totalCount })}</>
                    : allFailed ? <>{failedCount} / {totalCount}</>
                    : <>{okCount} / {totalCount}</>}
                  </span>
                </div>
                <button onClick={() => setBulkResults(null)} className="text-dark-500 hover:text-dark-300 text-xs">
                  ✕
                </button>
              </div>

              {/* Summary bar */}
              {totalCount > 1 && (
                <div className="flex items-center gap-2 mb-3">
                  <div className="flex-1 h-1.5 rounded-full bg-dark-800 overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${allOk ? 'bg-emerald-400' : allFailed ? 'bg-red-400' : 'bg-orange-400'}`}
                      style={{ width: `${(okCount / totalCount) * 100}%` }}
                    />
                  </div>
                  <span className="text-xs text-dark-500 shrink-0 tabular-nums">
                    {okCount} / {totalCount}
                  </span>
                </div>
              )}

              <div className="space-y-1.5">
                {bulkResults.map(r => (
                  <div key={r.server_id} className="flex items-center gap-2 text-sm">
                    {r.success ? (
                      <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
                    ) : (
                      <XCircle className="w-4 h-4 text-red-400 shrink-0" />
                    )}
                    <span className="text-dark-200 font-medium">{r.server_name}</span>
                    {r.success ? (
                      <span className="text-dark-500 text-xs">{r.message || t('ssh_security.bulk_success')}</span>
                    ) : (
                      <span className="text-red-400 text-xs">{r.error || t('ssh_security.bulk_failed')}</span>
                    )}
                    {r.warnings && r.warnings.length > 0 && (
                      <span className="text-orange-400 text-xs">({r.warnings.join(', ')})</span>
                    )}
                  </div>
                ))}
              </div>
            </motion.div>
          )
        })()}
      </AnimatePresence>

      {/* Bulk Confirm Modal */}
      <AnimatePresence>
        {showBulkConfirm && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
            onClick={() => setShowBulkConfirm(null)}
          >
            <motion.div
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              className="bg-dark-900 border border-dark-700 rounded-xl p-6 max-w-md w-full mx-4 shadow-2xl"
              onClick={e => e.stopPropagation()}
            >
              <div className="flex items-center gap-3 mb-4">
                <AlertTriangle className="w-6 h-6 text-orange-400" />
                <h3 className="text-lg font-semibold text-dark-100">{t('ssh_security.apply_to_all')}</h3>
              </div>
              <p className="text-sm text-dark-300 mb-2">
                {servers.length} {t('common.servers').toLowerCase()}
              </p>
              <div className="text-xs text-dark-500 mb-6 space-y-1">
                {servers.map(s => (
                  <div key={s.id} className="flex items-center gap-2">
                    <div className={`w-1.5 h-1.5 rounded-full ${s.is_active ? 'bg-emerald-400' : 'bg-dark-500'}`} />
                    {s.name}
                  </div>
                ))}
              </div>
              <div className="flex gap-3 justify-end">
                <button
                  onClick={() => setShowBulkConfirm(null)}
                  className="btn btn-secondary text-sm"
                >
                  {t('common.cancel')}
                </button>
                <motion.button
                  onClick={() => {
                    if (showBulkConfirm === 'config') handleBulkApply()
                    else if (showBulkConfirm?.startsWith('preset-')) handleBulkPreset(showBulkConfirm.replace('preset-', ''))
                  }}
                  disabled={bulkApplying}
                  className="btn btn-primary text-sm"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {bulkApplying ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                  {t('ssh_security.apply')}
                </motion.button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Sticky Bottom Bar */}
      <AnimatePresence>
        {hasChanges && selectedServerId && (
          <motion.div
            initial={{ opacity: 0, y: 40 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 40 }}
            className="fixed bottom-0 left-0 right-0 z-40 bg-dark-900/95 backdrop-blur border-t border-dark-700 px-6 py-3"
          >
            <div className="max-w-5xl mx-auto flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="w-2 h-2 rounded-full bg-orange-400 animate-pulse" />
                <span className="text-sm text-dark-200">{t('ssh_security.unsaved_changes')}</span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={handleReset}
                  className="btn btn-secondary text-sm"
                >
                  {t('ssh_security.reset')}
                </button>
                <motion.button
                  onClick={handleApply}
                  disabled={applying}
                  className="btn btn-primary text-sm"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {applying ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                  {t('ssh_security.apply')}
                </motion.button>
                <motion.button
                  onClick={() => setShowBulkConfirm('config')}
                  disabled={bulkApplying}
                  className="btn btn-secondary text-sm"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {bulkApplying ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                  {t('ssh_security.apply_to_all')}
                </motion.button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
