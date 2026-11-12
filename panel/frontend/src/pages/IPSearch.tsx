/** Страницы «Поиск IP» и «Канал Cloud.ru» */

import { useState, useEffect, useRef, useCallback, useMemo, FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Search, Users, Settings, ScrollText, Plus, Trash2, Edit2, X,
  ChevronRight, Play, Square, Loader2, Save, AlertTriangle,
  Send, Network, Clock, RefreshCw, FolderOpen, Globe, User, Eye, EyeOff,
  Power, Target, Wallet, AlertCircle, Zap, Copy, CheckCircle2, Upload, Cpu, UserCheck, HelpCircle, BarChart3,
  Gauge, CalendarClock, TrendingUp
} from 'lucide-react'
import { useExtStore } from '../stores/ipSearchVkStore'
import { extApi } from '../api/ipSearchVk'
import type {
  ExtAccount, ExtProject, ManualObtainRequest, ExtPreciseCidr, ExtAccountCloudIps, ExtProjectCloudIps,
  ReceivedStats, ReceivedIP,
} from '../api/ipSearchVk'
import { ycApi } from '../api/ipSearchYandex'
import { useYcStore } from '../stores/ipSearchYandexStore'
import type { YcManualObtainRequest } from '../api/ipSearchYandex'
import { useCloudEvoStore, useCloudAdvStore } from '../stores/ipSearchCloudruStore'
import type { CloudStoreHook } from '../stores/ipSearchCloudruStore'
import { cloudEvoApi, cloudAdvApi, cloudBwApi } from '../api/ipSearchCloudru'
import type {
  CloudApi, CloudManualObtainRequest, CloudBwAccount, CloudBwAccountInput, CloudBwCloudEip, CloudBwDynamic,
  CloudBwHistoryEntry, CloudBwIp, CloudBwMode, CloudBwScheduleRule, CloudBwUsage, CloudBwUsageItem, CloudBwUsagePeriod,
} from '../api/ipSearchCloudru'
import { serversApi } from '../api/client'
import { selectelApi } from '../api/ipSearchSelectel'
import { useSelectelStore } from '../stores/ipSearchSelectelStore'
import type { SelectelManualObtainRequest } from '../api/ipSearchSelectel'
import { readStorage, writeStorage } from '../utils/storage'
import { copyToClipboard } from '../utils/clipboard'
import { Checkbox } from '../components/ui/Checkbox'
import { Tooltip } from '../components/ui/Tooltip'
import { SecretInput } from '../components/ui/SecretInput'
import { CopyableIp } from '../components/ui/CopyableIp'
import { Markdown } from '../components/FAQ/markdown'

type TabType = 'accounts' | 'settings' | 'logs' | 'manual' | 'stats' | 'multiqueue'
type CloudTabType = 'accounts' | 'manual' | 'stats' | 'settings' | 'logs'
type CloudEdition = 'evolution' | 'advanced'
type SelectelTabType = 'accounts' | 'manual' | 'stats' | 'multiqueue' | 'settings' | 'logs'

const CLOUD_EVO_DEFAULT_ZONES = ['ru.AZ-1', 'ru.AZ-2', 'ru.AZ-3']
const CLOUD_ADV_REGION = 'ru-moscow-1'
const SELECTEL_DEFAULT_REGIONS = ['ru-1', 'ru-2', 'ru-3', 'ru-7', 'ru-8', 'ru-9']

const containerVariants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1, transition: { staggerChildren: 0.05 } }
}

const itemVariants = {
  hidden: { opacity: 0, y: 10 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.2 } }
}

// ═══════════════════════════════════════════════════════════════════════
// ACCOUNTS TAB
// ═══════════════════════════════════════════════════════════════════════

interface CloudIpsState {
  loading: boolean
  data?: ExtAccountCloudIps
  error?: string
}

function accountCloudIpTotals(data: ExtAccountCloudIps) {
  const onVms = new Set<string>()
  const floating = new Set<string>()
  for (const project of data.projects) {
    project.servers.forEach(server => server.ips.forEach(ip => onVms.add(ip.address)))
    project.floating_ips.forEach(fip => floating.add(fip.address))
  }
  const errors = data.projects.filter(p => p.error).map(p => `${p.project_name}: ${p.error}`)
  return { onVms: onVms.size, floating: floating.size, errors }
}

function CloudIpsChip({ state }: { state?: CloudIpsState }) {
  if (!state) return null

  if (state.loading) {
    return (
      <span className="text-xs px-1.5 py-0.5 rounded bg-dark-700 text-dark-300 flex items-center gap-1">
        <Loader2 className="w-3.5 h-3.5 animate-spin" />IP
      </span>
    )
  }

  if (!state.data) {
    return (
      <Tooltip label={state.error || 'Не удалось получить адреса'}>
        <span className="text-xs px-1.5 py-0.5 rounded bg-danger/20 text-danger flex items-center gap-1">
          <AlertCircle className="w-3.5 h-3.5" />IP: ошибка
        </span>
      </Tooltip>
    )
  }

  const { onVms, floating, errors } = accountCloudIpTotals(state.data)
  const total = onVms + floating
  const details = (
    <div className="space-y-0.5">
      <div>На ВМ: {onVms} · флоатингов без ВМ: {floating}</div>
      {errors.map(error => <div key={error} className="text-danger">{error}</div>)}
    </div>
  )

  return (
    <Tooltip label={details} maxWidth={360}>
      <span className={`text-xs px-1.5 py-0.5 rounded flex items-center gap-1 ${
        total > 0 ? 'bg-sky-500/20 text-sky-400' : 'bg-dark-700 text-dark-400'
      }`}>
        <Network className="w-3.5 h-3.5" />
        {total > 0 ? `${total} IP в облаке` : 'нет IP'}
        {errors.length > 0 && <AlertTriangle className="w-3.5 h-3.5 text-warning" />}
      </span>
    </Tooltip>
  )
}

function ProjectCloudIps({ view }: { view: ExtProjectCloudIps }) {
  if (view.error) {
    return <div className="mt-2 pl-7 text-xs text-danger break-all">{view.error}</div>
  }
  if (view.servers.length === 0 && view.floating_ips.length === 0) {
    return <div className="mt-2 pl-7 text-xs text-dark-500">Нет ВМ и флоатингов</div>
  }

  return (
    <div className="mt-2 pl-7 space-y-1 text-xs">
      {view.servers.map(server => (
        <div key={server.id} className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <Cpu className="w-3.5 h-3.5 text-dark-500 flex-shrink-0" />
          <span className="text-dark-300">{server.name}</span>
          <span className="text-dark-500">{server.status}</span>
          {server.ips.length === 0 ? (
            <span className="text-dark-500">без публичного IP</span>
          ) : server.ips.map(ip => (
            <span key={ip.address} className="font-mono text-sky-400 select-all">
              {ip.address}
              {ip.kind === 'floating' && <span className="font-sans text-dark-500"> (флоатинг)</span>}
            </span>
          ))}
        </div>
      ))}
      {view.floating_ips.map(fip => (
        <div key={fip.address} className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <Globe className="w-3.5 h-3.5 text-dark-500 flex-shrink-0" />
          <span className="font-mono text-sky-400 select-all">{fip.address}</span>
          {fip.bound ? (
            <span className="text-dark-400">флоатинг → {fip.attached_to || fip.fixed_ip || 'порт без ВМ'}</span>
          ) : (
            <span className="text-amber-400">флоатинг свободен</span>
          )}
        </div>
      ))}
    </div>
  )
}

function AccountsTab() {
  const {
    accounts, projects, caughtIps, isLoadingAccounts, isLoadingCaughtIps, isSaving, error,
    fetchAccounts, createAccount, updateAccount, deleteAccount,
    fetchProjects, createProject, updateProject, deleteProject, clearError,
    fetchCaughtIps, clearCaughtIps, deleteCaughtIp,
    workerStatuses, accountErrors, startAccount, stopAccount, fetchWorkerStatuses, clearAccountErrors
  } = useExtStore()

  const [expandedAccounts, setExpandedAccounts] = useState<Set<number>>(new Set())
  const [showAddAccount, setShowAddAccount] = useState(false)
  const [accountForm, setAccountForm] = useState({ email: '', password: '', proxy: '' })
  const [editingAccount, setEditingAccount] = useState<number | null>(null)
  const [addingProjectFor, setAddingProjectFor] = useState<number | null>(null)
  const [editingProject, setEditingProject] = useState<ExtProject | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const [projectForm, setProjectForm] = useState({ project_id: '', project_name: '' })
  const [deleteConfirm, setDeleteConfirm] = useState<{ type: 'account' | 'project'; id: number } | null>(null)
  const [showIpsModal, setShowIpsModal] = useState<{ type: 'account' | 'project' | 'all'; id?: number; name?: string } | null>(null)
  const [cloudIps, setCloudIps] = useState<Record<number, CloudIpsState>>({})

  useEffect(() => { fetchAccounts(); fetchWorkerStatuses() }, [fetchAccounts, fetchWorkerStatuses])
  
  // Poll worker statuses every 3s
  useEffect(() => {
    const interval = setInterval(fetchWorkerStatuses, 3000)
    return () => clearInterval(interval)
  }, [fetchWorkerStatuses])

  const toggleAccount = async (accountId: number) => {
    const newExpanded = new Set(expandedAccounts)
    if (newExpanded.has(accountId)) {
      newExpanded.delete(accountId)
    } else {
      newExpanded.add(accountId)
      if (!projects[accountId]) await fetchProjects(accountId)
    }
    setExpandedAccounts(newExpanded)
  }

  const handleAddAccount = async (e: FormEvent) => {
    e.preventDefault()
    if (!accountForm.email.trim() || !accountForm.password.trim()) return
    const result = await createAccount({
      email: accountForm.email.trim(),
      password: accountForm.password,
      proxy: accountForm.proxy.trim() || undefined
    })
    if (result) { 
      setAccountForm({ email: '', password: '', proxy: '' })
      setShowAddAccount(false) 
    }
  }
  
  const handleEditAccount = (account: { id: number; email: string; password: string; proxy: string | null }) => {
    setEditingAccount(account.id)
    setAccountForm({
      email: account.email,
      password: account.password || '',
      proxy: account.proxy || ''
    })
    setShowPassword(false)
  }
  
  const handleSaveAccount = async (e: FormEvent, accountId: number) => {
    e.preventDefault()
    const result = await updateAccount(accountId, {
      password: accountForm.password,
      proxy: accountForm.proxy.trim() || undefined
    })
    if (result) {
      setEditingAccount(null)
      setAccountForm({ email: '', password: '', proxy: '' })
    }
  }
  
  const cancelAccountEdit = () => {
    setEditingAccount(null)
    setShowAddAccount(false)
    setAccountForm({ email: '', password: '', proxy: '' })
  }

  const handleDeleteAccount = async (id: number) => {
    if (deleteConfirm?.type === 'account' && deleteConfirm.id === id) {
      await deleteAccount(id)
      setDeleteConfirm(null)
    } else {
      setDeleteConfirm({ type: 'account', id })
      setTimeout(() => setDeleteConfirm(null), 3000)
    }
  }

  const handleAddProject = async (accountId: number) => {
    // Auto-expand account if collapsed
    if (!expandedAccounts.has(accountId)) {
      const newExpanded = new Set(expandedAccounts)
      newExpanded.add(accountId)
      setExpandedAccounts(newExpanded)
      if (!projects[accountId]) await fetchProjects(accountId)
    }
    setAddingProjectFor(accountId)
    setEditingProject(null)
    setProjectForm({ project_id: '', project_name: '' })
  }

  const handleEditProject = (project: ExtProject) => {
    setEditingProject(project)
    setAddingProjectFor(null)
    setProjectForm({
      project_id: project.project_id,
      project_name: project.project_name
    })
  }

  const handleSaveProject = async (e: FormEvent) => {
    e.preventDefault()
    if (editingProject) {
      await updateProject(editingProject.id, projectForm)
      setEditingProject(null)
    } else if (addingProjectFor) {
      await createProject(addingProjectFor, projectForm)
      setAddingProjectFor(null)
    }
    setProjectForm({ project_id: '', project_name: '' })
  }

  const handleDeleteProject = async (projectId: number, accountId: number) => {
    if (deleteConfirm?.type === 'project' && deleteConfirm.id === projectId) {
      await deleteProject(projectId, accountId)
      setDeleteConfirm(null)
    } else {
      setDeleteConfirm({ type: 'project', id: projectId })
      setTimeout(() => setDeleteConfirm(null), 3000)
    }
  }

  const cancelForm = () => {
    setAddingProjectFor(null)
    setEditingProject(null)
    setProjectForm({ project_id: '', project_name: '' })
  }

  const handleToggleAccount = async (accountId: number, currentEnabled: boolean) => {
    await updateAccount(accountId, { enabled: !currentEnabled })
  }
  
  const handleStartAccount = async (accountId: number) => {
    await startAccount(accountId)
  }
  
  const handleStopAccount = async (accountId: number) => {
    await stopAccount(accountId)
  }
  
  const getAccountWorkerStatus = (accountId: number) => {
    return workerStatuses[accountId]
  }
  
  const getAccountActiveErrors = (accountId: number) => {
    return accountErrors.filter(e => e.account_id === accountId)
  }
  
  const handleShowIps = async (type: 'account' | 'project' | 'all', id?: number, name?: string) => {
    setShowIpsModal({ type, id, name })
    if (type === 'project' && id !== undefined) {
      await fetchCaughtIps(undefined, id)
    } else if (type === 'account' && id !== undefined) {
      await fetchCaughtIps(id)
    } else {
      await fetchCaughtIps()
    }
  }
  
  const loadCloudIps = useCallback(async (accountId: number) => {
    setCloudIps(prev => ({ ...prev, [accountId]: { ...prev[accountId], loading: true } }))
    try {
      const { data } = await extApi.getAccountCloudIps(accountId)
      setCloudIps(prev => ({ ...prev, [accountId]: { loading: false, data } }))
    } catch (e: any) {
      const error = e.response?.data?.detail || 'Не удалось получить адреса'
      setCloudIps(prev => ({ ...prev, [accountId]: { loading: false, error } }))
    }
  }, [])

  const loadAllCloudIps = () => accounts.forEach(account => loadCloudIps(account.id))
  const isCheckingCloudIps = Object.values(cloudIps).some(state => state.loading)

  const handleClearIps = async () => {
    if (!showIpsModal) return
    const { type, id } = showIpsModal
    if (type === 'project' && id !== undefined) {
      await clearCaughtIps(undefined, id)
    } else if (type === 'account' && id !== undefined) {
      await clearCaughtIps(id)
    } else {
      await clearCaughtIps()
    }
    setShowIpsModal(null)
  }

  if (isLoadingAccounts) {
    return <div className="flex items-center justify-center py-16"><Loader2 className="w-8 h-8 text-accent-400 animate-spin" /></div>
  }

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible">
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 mb-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm flex-1">{error}</span>
            <button onClick={clearError} className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div className="flex items-center justify-between mb-4" variants={itemVariants}>
        <div className="flex items-center gap-4">
          {accounts.length > 0 && (
            <>
              <span className="text-sm text-dark-400">
                Активно: <span className="text-accent-400 font-medium">{accounts.filter(a => a.enabled).length}</span> из {accounts.length}
              </span>
              <div className="flex gap-2">
                <button 
                  onClick={() => accounts.forEach(a => !a.enabled && updateAccount(a.id, { enabled: true }))}
                  className="btn-tool"
                >
                  Включить все
                </button>
                <button
                  onClick={() => accounts.forEach(a => a.enabled && updateAccount(a.id, { enabled: false }))}
                  className="btn-tool"
                >
                  Выключить все
                </button>
                <Tooltip label="Опросить VK: какие публичные адреса есть на ВМ и какие флоатинги заведены в проектах">
                  <button onClick={loadAllCloudIps} disabled={isCheckingCloudIps} className="btn-tool flex items-center gap-1.5">
                    {isCheckingCloudIps
                      ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      : <Network className="w-3.5 h-3.5" />}
                    IP на аккаунтах
                  </button>
                </Tooltip>
              </div>
            </>
          )}
        </div>
        {!showAddAccount ? (
          <motion.button onClick={() => setShowAddAccount(true)} className="btn btn-primary"
            whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
            <Plus className="w-4 h-4" /> Добавить аккаунт
          </motion.button>
        ) : (
          <form onSubmit={handleAddAccount} className="bg-dark-800/50 rounded-xl p-4 w-full max-w-2xl space-y-3" autoComplete="off" data-form-type="other">
            <div className="flex items-center gap-2 mb-3">
              <User className="w-5 h-5 text-accent-400" />
              <h4 className="font-medium text-dark-100">Новый аккаунт</h4>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-dark-400 mb-1">Email</label>
                <input type="text" value={accountForm.email}
                  onChange={(e) => setAccountForm(f => ({ ...f, email: e.target.value }))}
                  className="input text-sm" placeholder="email@example.com" required autoFocus
                  autoComplete="off" data-lpignore="true" data-form-type="other" />
              </div>
              <div>
                <label className="block text-xs text-dark-400 mb-1">Пароль</label>
                <div className="relative">
                  <SecretInput revealed={showPassword} value={accountForm.password}
                    onChange={(e) => setAccountForm(f => ({ ...f, password: e.target.value }))}
                    className="input text-sm pr-10" placeholder="••••••••" required />
                  <button type="button" onClick={() => setShowPassword(!showPassword)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
                    {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
              </div>
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">SOCKS5 Proxy (опционально)</label>
              <input type="text" value={accountForm.proxy}
                onChange={(e) => setAccountForm(f => ({ ...f, proxy: e.target.value }))}
                className="input text-sm" placeholder="host:port:user:pass" autoComplete="off" />
            </div>
            <div className="flex gap-2 pt-2">
              <motion.button type="submit" disabled={isSaving || !accountForm.email.trim() || !accountForm.password.trim()} 
                className="btn btn-primary text-sm" whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                Добавить
              </motion.button>
              <motion.button type="button" onClick={cancelAccountEdit} className="btn btn-secondary text-sm"
                whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>Отмена</motion.button>
            </div>
          </form>
        )}
      </motion.div>

      <div className="space-y-3">
        {accounts.length === 0 ? (
          <motion.div className="card text-center py-12" variants={itemVariants}>
            <Users className="w-12 h-12 text-dark-500 mx-auto mb-3" />
            <p className="text-dark-400">Нет добавленных аккаунтов</p>
            <p className="text-dark-500 text-sm mt-1">Добавьте аккаунт чтобы начать</p>
          </motion.div>
        ) : (
          accounts.map((account) => {
            const ws = getAccountWorkerStatus(account.id)
            const isRunning = ws?.status === 'running'
            const isStopping = ws?.status === 'stopping'
            const hasError = ws?.status === 'error'
            const isProxyWaiting = ws?.status === 'proxy_waiting'
            const accErrors = getAccountActiveErrors(account.id)
            const balanceDepleted = accErrors.some(e => e.error_type === 'ACCESS_DENIED' || e.error_type === 'BALANCE_DEPLETED')
            const statusDot = isRunning ? 'bg-success animate-pulse' : isProxyWaiting ? 'bg-purple-500 animate-pulse' : hasError ? 'bg-danger animate-pulse' : balanceDepleted ? 'bg-warning animate-pulse' : 'bg-dark-500'
            
            return (
            <motion.div key={account.id} className={`card transition-all ${!account.enabled ? 'opacity-60 border-dark-700/30' : ''}`} variants={itemVariants}>
              {/* Balance / Error Warning Banner */}
              <AnimatePresence>
                {accErrors.length > 0 && (
                  <motion.div className="mb-3 space-y-1.5"
                    initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
                    {balanceDepleted && (
                      <div className="flex items-center gap-2 p-2.5 bg-warning/10 border border-warning/20 rounded-lg text-warning text-sm">
                        <Wallet className="w-4 h-4 flex-shrink-0" />
                        <span className="flex-1">Баланс исчерпан — необходимо пополнить</span>
                        <button onClick={() => clearAccountErrors(account.id)} className="p-1 hover:bg-warning/20 rounded">
                          <X className="w-4 h-4" />
                        </button>
                      </div>
                    )}
                    {accErrors.filter(e => e.error_type === 'PROXY_DEAD').length > 0 && (
                      <div className="flex items-center gap-2 p-2.5 bg-purple-500/10 border border-purple-500/20 rounded-lg text-purple-400 text-sm">
                        <AlertCircle className="w-4 h-4 flex-shrink-0" />
                        <span className="flex-1">Прокси не работает</span>
                        <button onClick={() => clearAccountErrors(account.id)} className="p-1 hover:bg-purple-500/20 rounded">
                          <X className="w-4 h-4" />
                        </button>
                      </div>
                    )}
                    {accErrors.filter(e => e.error_type === 'QUOTA_EXCEEDED').length > 0 && (
                      <div className="flex items-center gap-2 p-2.5 bg-orange-500/10 border border-orange-500/20 rounded-lg text-orange-400 text-sm">
                        <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                        <span className="flex-1">Квота проекта исчерпана</span>
                        <button onClick={() => clearAccountErrors(account.id)} className="p-1 hover:bg-orange-500/20 rounded">
                          <X className="w-4 h-4" />
                        </button>
                      </div>
                    )}
                  </motion.div>
                )}
              </AnimatePresence>
              
              <div className="flex items-center justify-between">
                <button onClick={() => toggleAccount(account.id)} className="flex items-center gap-3 flex-1 text-left">
                  <motion.div animate={{ rotate: expandedAccounts.has(account.id) ? 90 : 0 }}>
                    <ChevronRight className="w-5 h-5 text-dark-400" />
                  </motion.div>
                  <div className="relative">
                    <div className={`w-10 h-10 rounded-xl flex items-center justify-center transition-all ${
                      account.enabled 
                        ? 'bg-gradient-to-br from-accent-500/20 to-accent-600/20' 
                        : 'bg-dark-700/50'
                    }`}>
                      <User className={`w-5 h-5 ${account.enabled ? 'text-accent-400' : 'text-dark-500'}`} />
                    </div>
                    {/* Status dot */}
                    <span className={`absolute -top-0.5 -right-0.5 w-3 h-3 rounded-full border-2 border-dark-800 ${statusDot}`} />
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className={`font-medium ${account.enabled ? 'text-dark-100' : 'text-dark-400'}`}>{account.email}</h3>
                      {!account.enabled && <span className="text-xs px-1.5 py-0.5 rounded bg-dark-700 text-dark-400">выкл</span>}
                      {isRunning && <span className="text-xs px-1.5 py-0.5 rounded bg-success/20 text-success flex items-center gap-1"><Zap className="w-3.5 h-3.5" />работает</span>}
                      {isProxyWaiting && <span className="text-xs px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400 flex items-center gap-1"><RefreshCw className="w-3.5 h-3.5 animate-spin" />прокси...</span>}
                      {hasError && <span className="text-xs px-1.5 py-0.5 rounded bg-danger/20 text-danger">ошибка</span>}
                      {account.proxy && <span className="text-xs px-1.5 py-0.5 rounded bg-accent-500/20 text-accent-400 flex items-center gap-1"><Globe className="w-3.5 h-3.5" />proxy</span>}
                      {account.caught_ip_count > 0 && (
                        <button 
                          onClick={(e) => { e.stopPropagation(); handleShowIps('account', account.id, account.email) }}
                          className="text-xs px-1.5 py-0.5 rounded bg-success/20 text-success flex items-center gap-1 hover:bg-success/30 transition-colors"
                        >
                          <Target className="w-3.5 h-3.5" />
                          {account.caught_ip_count} IP
                        </button>
                      )}
                      <CloudIpsChip state={cloudIps[account.id]} />
                    </div>
                    <div className="flex items-center gap-3 text-sm text-dark-500">
                      <span>{account.project_count} проектов</span>
                      {ws && ws.status === 'running' && (
                        <span className="text-dark-400">
                          попыток: {ws.attempts} | поймано: {ws.caught}
                          {ws.released ? <span className="text-amber-400"> | освобождено: {ws.released}</span> : null}
                        </span>
                      )}
                    </div>
                  </div>
                </button>
                <div className="flex items-center gap-2">
                  {/* Per-account Start/Stop */}
                  {account.enabled && (
                    isRunning || isStopping || isProxyWaiting ? (
                      <motion.button
                        onClick={() => handleStopAccount(account.id)}
                        disabled={isStopping}
                        className="btn p-2 bg-danger/10 text-danger hover:bg-danger/20 transition-all"
                        whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}
                        title="Остановить поток"
                      >
                        {isStopping ? <Loader2 className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
                      </motion.button>
                    ) : (
                      <motion.button
                        onClick={() => handleStartAccount(account.id)}
                        className="btn p-2 bg-success/10 text-success hover:bg-success/20 transition-all"
                        whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}
                        title="Запустить поток"
                      >
                        <Play className="w-4 h-4" />
                      </motion.button>
                    )
                  )}
                  <motion.button 
                    onClick={() => handleToggleAccount(account.id, account.enabled)}
                    className={`btn p-2 transition-all ${account.enabled 
                      ? 'bg-accent-500/10 text-accent-400 hover:bg-accent-500/20' 
                      : 'bg-dark-700/50 text-dark-400 hover:bg-dark-700'}`}
                    whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}
                    title={account.enabled ? 'Отключить' : 'Включить'}
                  >
                    <Power className="w-4 h-4" />
                  </motion.button>
                  <motion.button onClick={() => handleEditAccount(account)} className="btn btn-ghost p-2"
                    whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }} title="Редактировать">
                    <Edit2 className="w-4 h-4" />
                  </motion.button>
                  <motion.button onClick={() => handleAddProject(account.id)} className="btn btn-secondary text-sm"
                    whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}>
                    <Plus className="w-4 h-4" /> Проект
                  </motion.button>
                  <motion.button onClick={() => handleDeleteAccount(account.id)}
                    className={`btn p-2 transition-all ${deleteConfirm?.type === 'account' && deleteConfirm.id === account.id
                      ? 'bg-danger text-white' : 'btn-ghost text-danger hover:bg-danger/10'}`}
                    whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                    <Trash2 className="w-4 h-4" />
                  </motion.button>
                </div>
              </div>
              
              {/* Форма редактирования аккаунта */}
              <AnimatePresence>
                {editingAccount === account.id && (
                  <motion.form onSubmit={(e) => handleSaveAccount(e, account.id)} 
                    className="mt-4 pt-4 border-t border-dark-700/50 space-y-3"
                    initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}
                    autoComplete="off" data-form-type="other">
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className="block text-xs text-dark-400 mb-1">Пароль</label>
                        <div className="relative">
                          <SecretInput revealed={showPassword} value={accountForm.password}
                            onChange={(e) => setAccountForm(f => ({ ...f, password: e.target.value }))}
                            className="input text-sm pr-10" placeholder="••••••••" />
                          <button type="button" onClick={() => setShowPassword(!showPassword)}
                            className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
                            {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                          </button>
                        </div>
                      </div>
                      <div>
                        <label className="block text-xs text-dark-400 mb-1">SOCKS5 Proxy</label>
                        <input type="text" value={accountForm.proxy}
                          onChange={(e) => setAccountForm(f => ({ ...f, proxy: e.target.value }))}
                          className="input text-sm" placeholder="host:port:user:pass" autoComplete="off" />
                      </div>
                    </div>
                    <div className="flex gap-2">
                      <motion.button type="submit" disabled={isSaving} className="btn btn-primary text-sm"
                        whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                        {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                        Сохранить
                      </motion.button>
                      <motion.button type="button" onClick={cancelAccountEdit} className="btn btn-secondary text-sm"
                        whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>Отмена</motion.button>
                    </div>
                  </motion.form>
                )}
              </AnimatePresence>

              <AnimatePresence>
                {expandedAccounts.has(account.id) && (
                  <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
                    <div className="mt-4 pt-4 border-t border-dark-700/50 space-y-3">
                      <div className="flex items-center gap-2 text-xs text-dark-500">
                        <Network className="w-3.5 h-3.5" />
                        {cloudIps[account.id]?.data
                          ? <span>Адреса в облаке на {new Date(cloudIps[account.id].data!.checked_at).toLocaleTimeString('ru-RU')}</span>
                          : <span>Адреса в облаке не проверялись</span>}
                        <button
                          onClick={() => loadCloudIps(account.id)}
                          disabled={cloudIps[account.id]?.loading}
                          className="text-accent-400 hover:text-accent-300 disabled:opacity-50 flex items-center gap-1"
                        >
                          <RefreshCw className={`w-3.5 h-3.5 ${cloudIps[account.id]?.loading ? 'animate-spin' : ''}`} />
                          {cloudIps[account.id]?.data ? 'Обновить' : 'Проверить'}
                        </button>
                      </div>
                      {(addingProjectFor === account.id || editingProject?.account_id === account.id) && (
                        <motion.form onSubmit={handleSaveProject} className="bg-dark-800/50 rounded-xl p-4 space-y-3"
                          initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
                          <div className="flex items-center gap-2 mb-3">
                            <FolderOpen className="w-5 h-5 text-accent-400" />
                            <h4 className="font-medium text-dark-100">{editingProject ? 'Редактировать' : 'Новый проект'}</h4>
                          </div>
                          <div className="grid grid-cols-2 gap-3">
                            <div>
                              <label className="block text-xs text-dark-400 mb-1">Project ID</label>
                              <input type="text" value={projectForm.project_id}
                                onChange={(e) => setProjectForm(f => ({ ...f, project_id: e.target.value }))}
                                className="input text-sm" placeholder="a1b2c3d4e5f6..." required />
                            </div>
                            <div>
                              <label className="block text-xs text-dark-400 mb-1">Название проекта</label>
                              <input type="text" value={projectForm.project_name}
                                onChange={(e) => setProjectForm(f => ({ ...f, project_name: e.target.value }))}
                                className="input text-sm" placeholder="mcs0123456789" required />
                            </div>
                          </div>
                          <div className="flex gap-2 pt-2">
                            <motion.button type="submit" disabled={isSaving} className="btn btn-primary text-sm"
                              whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                              {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                              Сохранить
                            </motion.button>
                            <motion.button type="button" onClick={cancelForm} className="btn btn-secondary text-sm"
                              whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>Отмена</motion.button>
                          </div>
                        </motion.form>
                      )}

                      {projects[account.id]?.length === 0 ? (
                        <div className="text-center py-6 text-dark-500 text-sm">Нет проектов</div>
                      ) : (
                        projects[account.id]?.map((project) => {
                          const cloudView = cloudIps[account.id]?.data?.projects.find(p => p.project_db_id === project.id)
                          return (
                          <motion.div key={project.id}
                            className={`p-3 rounded-lg group ${project.disabled ? 'bg-dark-800/20 opacity-60' : 'bg-dark-800/30'}`}
                            whileHover={{ backgroundColor: 'rgba(30, 41, 59, 0.5)' }}>
                            <div className="flex items-center justify-between">
                            <div className="flex items-center gap-3">
                              <FolderOpen className={`w-4 h-4 text-dark-500 ${project.disabled ? 'opacity-50' : ''}`} />
                              <div>
                                <div className="flex items-center gap-2">
                                  <span className="font-medium text-dark-200">{project.project_name}</span>
                                  <span className="text-xs text-dark-500 font-mono">{project.project_id.slice(0, 12)}...</span>
                                  {project.disabled && <span className="text-xs px-1.5 py-0.5 rounded bg-dark-700 text-dark-400">выкл</span>}
                                  {project.caught_ip_count > 0 && (
                                    <span className="text-xs px-1.5 py-0.5 rounded bg-success/20 text-success">
                                      {project.caught_ip_count} IP
                                    </span>
                                  )}
                                  {accountErrors.some(e => e.project_name === project.project_name && e.error_type === 'QUOTA_EXCEEDED' && !e.resolved) && (
                                    <span className="text-xs px-1.5 py-0.5 rounded bg-orange-500/20 text-orange-400">квота</span>
                                  )}
                                  {accountErrors.some(e => e.project_name === project.project_name && (e.error_type === 'ACCESS_DENIED' || e.error_type === 'BALANCE_DEPLETED') && !e.resolved) && (
                                    <span className="text-xs px-1.5 py-0.5 rounded bg-warning/20 text-warning flex items-center gap-1">
                                      <Wallet className="w-3.5 h-3.5" />баланс
                                    </span>
                                  )}
                                </div>
                              </div>
                            </div>
                            <div className="flex items-center gap-1">
                              <motion.button
                                onClick={() => updateProject(project.id, { disabled: !project.disabled })}
                                className={`btn p-1.5 transition-all ${!project.disabled
                                  ? 'text-success hover:bg-success/10'
                                  : 'text-dark-500 hover:bg-dark-700'}`}
                                whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}
                                title={project.disabled ? 'Включить проект' : 'Отключить проект'}
                              >
                                <Power className="w-3.5 h-3.5" />
                              </motion.button>
                              <motion.button onClick={() => handleEditProject(project)} className="btn btn-ghost p-1.5 opacity-60 group-hover:opacity-100 transition-opacity"
                                whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}><Edit2 className="w-3.5 h-3.5" /></motion.button>
                              <motion.button onClick={() => handleDeleteProject(project.id, account.id)}
                                className={`btn p-1.5 transition-all opacity-60 group-hover:opacity-100 ${deleteConfirm?.type === 'project' && deleteConfirm.id === project.id
                                  ? 'bg-danger text-white !opacity-100' : 'btn-ghost text-danger hover:bg-danger/10'}`}
                                whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}><Trash2 className="w-3.5 h-3.5" /></motion.button>
                            </div>
                            </div>
                            {cloudView && <ProjectCloudIps view={cloudView} />}
                          </motion.div>
                          )
                        })
                      )}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>
          )})
        )}
      </div>
      
      {/* Модальное окно пойманных IP */}
      <AnimatePresence>
        {showIpsModal && (
          <motion.div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={() => setShowIpsModal(null)}>
            <motion.div className="bg-dark-800 rounded-2xl p-6 w-full max-w-2xl max-h-[80vh] overflow-hidden flex flex-col"
              initial={{ scale: 0.9, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.9, opacity: 0 }}
              onClick={(e) => e.stopPropagation()}>
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-lg font-medium text-dark-100 flex items-center gap-2">
                  <Target className="w-5 h-5 text-success" />
                  Пойманные IP
                  {showIpsModal.name && <span className="text-dark-400 text-sm">— {showIpsModal.name}</span>}
                </h3>
                <div className="flex items-center gap-2">
                  {caughtIps.length > 0 && (
                    <motion.button onClick={handleClearIps} disabled={isSaving}
                      className="btn btn-ghost text-danger text-sm hover:bg-danger/10"
                      whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                      <Trash2 className="w-4 h-4" /> Очистить все
                    </motion.button>
                  )}
                  <motion.button onClick={() => setShowIpsModal(null)} className="btn btn-ghost p-2"
                    whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                    <X className="w-5 h-5" />
                  </motion.button>
                </div>
              </div>
              
              <div className="flex-1 overflow-y-auto custom-scrollbar">
                {isLoadingCaughtIps ? (
                  <div className="flex items-center justify-center py-8">
                    <Loader2 className="w-6 h-6 text-accent-400 animate-spin" />
                  </div>
                ) : caughtIps.length === 0 ? (
                  <div className="text-center py-8 text-dark-500">
                    <Target className="w-10 h-10 mx-auto mb-2 opacity-50" />
                    <p>Нет пойманных IP</p>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {caughtIps.map((ip) => (
                      <motion.div key={ip.id} className="flex items-center justify-between p-3 bg-dark-700/30 rounded-lg group"
                        initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
                        <div className="flex items-center gap-3">
                          <div className="w-8 h-8 rounded-lg bg-success/20 flex items-center justify-center">
                            <Target className="w-4 h-4 text-success" />
                          </div>
                          <div>
                            <div className="font-mono text-success font-medium">{ip.ip_address}</div>
                            <div className="flex items-center gap-2 text-xs text-dark-500">
                              <span>{ip.subnet_name}</span>
                              {ip.project_name && <span>• {ip.project_name}</span>}
                              <span>• {new Date(ip.caught_at).toLocaleString('ru-RU')}</span>
                            </div>
                          </div>
                        </div>
                        <motion.button onClick={() => deleteCaughtIp(ip.id)}
                          className="btn btn-ghost p-2 text-danger opacity-60 group-hover:opacity-100 transition-opacity"
                          whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                          <Trash2 className="w-4 h-4" />
                        </motion.button>
                      </motion.div>
                    ))}
                  </div>
                )}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// SETTINGS TAB
// ═══════════════════════════════════════════════════════════════════════

type Ipv4Range = [first: number, last: number]

function ipv4CidrRange(cidr: string): Ipv4Range | null {
  const [address, prefixText = '32'] = cidr.trim().split('/')
  const octets = address.split('.').map(Number)
  const prefix = Number(prefixText)
  if (octets.length !== 4 || octets.some(o => !Number.isInteger(o) || o < 0 || o > 255)) return null
  if (!Number.isInteger(prefix) || prefix < 0 || prefix > 32) return null
  // Умножение вместо битовых сдвигов: сдвиги в JS знаковые и ломаются на адресах от 128.0.0.0
  const size = 2 ** (32 - prefix)
  const first = Math.floor(octets.reduce((acc, o) => acc * 256 + o, 0) / size) * size
  return [first, first + size - 1]
}

function rangesOverlap(a: Ipv4Range, b: Ipv4Range): boolean {
  return a[0] <= b[1] && b[0] <= a[1]
}

const SUBNET_MODES = [
  {
    value: 'per_account',
    title: 'Одна подсеть на аккаунт',
    hint: 'Выбранные подсети делятся между аккаунтами: каждый аккаунт всё время ловит в одной своей подсети.',
  },
  {
    value: 'rotate',
    title: 'Все подсети по очереди',
    hint: 'Каждый аккаунт получает все выбранные подсети и перебирает их по кругу: попытка — следующая подсеть. Задержка между попытками прежняя.',
  },
] as const

function SettingsTab() {
  const { settings, isLoadingSettings, isSaving, fetchSettings, updateSettings, error, clearError } = useExtStore()
  const [formData, setFormData] = useState<any>(null)
  const [hasChanges, setHasChanges] = useState(false)
  const [showToken, setShowToken] = useState(false)
  const [showAddSubnet, setShowAddSubnet] = useState(false)
  const [subnetForm, setSubnetForm] = useState({ id: '', cidr: '' })
  const [builtinIds, setBuiltinIds] = useState<Set<number>>(new Set())
  const [subnetNames, setSubnetNames] = useState<Record<number, string>>({})
  const [subnetSaving, setSubnetSaving] = useState(false)
  const [preciseCidrs, setPreciseCidrs] = useState<ExtPreciseCidr[]>([])
  const [preciseInput, setPreciseInput] = useState('')
  const [preciseSaving, setPreciseSaving] = useState(false)
  const [preciseResult, setPreciseResult] = useState<string | null>(null)
  const [subnetMatchResult, setSubnetMatchResult] = useState<{ selected: number; uncovered: string[] } | null>(null)

  useEffect(() => { fetchSettings() }, [fetchSettings])

  useEffect(() => {
    extApi.getSubnets().then(res => {
      if (res.data.builtin_ids) setBuiltinIds(new Set(res.data.builtin_ids))
      if (res.data.subnet_names) setSubnetNames(res.data.subnet_names)
    }).catch(() => {})
  }, [])

  const loadPreciseCidrs = () => {
    extApi.getPreciseCidrs().then(res => setPreciseCidrs(res.data)).catch(() => {})
  }

  useEffect(() => { loadPreciseCidrs() }, [])

  const handleAddPreciseCidrs = async () => {
    const cidrs = [...new Set(preciseInput.split(/[\s,;]+/).filter(Boolean))]
    if (cidrs.length === 0) return
    setPreciseSaving(true)
    setPreciseResult(null)
    const rejected: string[] = []
    let firstError = ''
    for (const cidr of cidrs) {
      try {
        await extApi.addPreciseCidr(cidr)
      } catch (e: any) {
        rejected.push(cidr)
        firstError = firstError || e.response?.data?.detail || 'ошибка добавления'
      }
    }
    // Непринятые строки остаются в поле — их можно поправить и отправить заново
    setPreciseInput(rejected.join('\n'))
    const added = cidrs.length - rejected.length
    setPreciseResult(rejected.length === 0
      ? `Добавлено ${added}`
      : `Добавлено ${added}, не принято ${rejected.length}: ${firstError}`)
    loadPreciseCidrs()
    setPreciseSaving(false)
  }

  const handleRemovePreciseCidr = async (id: number) => {
    try {
      await extApi.removePreciseCidr(id)
      loadPreciseCidrs()
    } catch (e: any) {
      alert(e.response?.data?.detail || 'Ошибка удаления диапазона')
    }
  }

  useEffect(() => {
    if (settings && !formData) {
      setFormData({
        delay_min: settings.delay_min,
        delay_max: settings.delay_max,
        max_ip_per_project: settings.max_ip_per_project,
        stop_on_catch: settings.stop_on_catch,
        telegram_bot_token: settings.telegram_bot_token,
        telegram_chat_id: settings.telegram_chat_id,
        target_subnets: settings.target_subnets,
        proxy_required: settings.proxy_required,
        proxy_check_interval: settings.proxy_check_interval,
        log_level: settings.log_level || 'info',
        start_delay_max: settings.start_delay_max || 60,
        subnet_mode: settings.subnet_mode || 'per_account',
      })
    }
  }, [settings, formData])

  const handleChange = (field: string, value: any) => {
    setFormData((f: any) => f ? { ...f, [field]: value } : f)
    setHasChanges(true)
  }

  const toggleSubnet = (subnetId: number) => {
    if (!formData) return
    const current = formData.target_subnets
    const newSubnets = current.includes(subnetId)
      ? current.filter((s: number) => s !== subnetId)
      : [...current, subnetId]
    handleChange('target_subnets', newSubnets)
  }

  const selectAllSubnets = () => {
    if (!settings) return
    handleChange('target_subnets', Object.keys(settings.available_subnets).map(Number))
  }

  const clearAllSubnets = () => handleChange('target_subnets', [])

  const selectSubnetsByPreciseCidrs = () => {
    if (!settings) return
    const subnetRanges = Object.entries(settings.available_subnets)
      .map(([id, cidr]) => ({ id: Number(id), range: ipv4CidrRange(cidr) }))
    const matchedIds = new Set<number>()
    const uncovered: string[] = []
    for (const { cidr } of preciseCidrs) {
      const target = ipv4CidrRange(cidr)
      const owners = target ? subnetRanges.filter(s => s.range && rangesOverlap(s.range, target)) : []
      if (owners.length === 0) uncovered.push(cidr)
      owners.forEach(s => matchedIds.add(s.id))
    }
    setSubnetMatchResult({ selected: matchedIds.size, uncovered })
    // Совпадений нет — выбор не трогаем: с пустым списком воркер ушёл бы на подсети по умолчанию
    if (matchedIds.size === 0) return
    handleChange('target_subnets', [...matchedIds].sort((a, b) => a - b))
  }

  const handleAddSubnet = async () => {
    const id = parseInt(subnetForm.id)
    if (isNaN(id) || !subnetForm.cidr.trim()) return
    setSubnetSaving(true)
    try {
      await extApi.addSubnet(id, subnetForm.cidr.trim())
      setSubnetForm({ id: '', cidr: '' })
      setShowAddSubnet(false)
      await fetchSettings()
      setFormData(null)
    } catch (e: any) {
      alert(e.response?.data?.detail || 'Ошибка добавления подсети')
    } finally {
      setSubnetSaving(false)
    }
  }

  const handleRemoveSubnet = async (subnetId: number) => {
    if (!confirm(`Удалить подсеть ext-sub${subnetId}?`)) return
    try {
      await extApi.removeSubnet(subnetId)
      await fetchSettings()
      setFormData(null)
    } catch (e: any) {
      alert(e.response?.data?.detail || 'Ошибка удаления подсети')
    }
  }

  const handleSave = async () => {
    if (!formData) return
    const success = await updateSettings(formData)
    if (success) setHasChanges(false)
  }

  if (isLoadingSettings || !formData) {
    return <div className="flex items-center justify-center py-16"><Loader2 className="w-8 h-8 text-accent-400 animate-spin" /></div>
  }

  const subnets = settings?.available_subnets || {}

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-6">
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm flex-1">{error}</span>
            <button onClick={clearError} className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Clock className="w-5 h-5 text-accent-400" /> Задержки
        </h3>
        <div className="grid grid-cols-3 gap-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Минимальная (сек)</label>
            <input type="number" value={formData.delay_min}
              onChange={(e) => handleChange('delay_min', parseInt(e.target.value) || 0)}
              className="input" min={1} />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Максимальная (сек)</label>
            <input type="number" value={formData.delay_max}
              onChange={(e) => handleChange('delay_max', parseInt(e.target.value) || 0)}
              className="input" min={1} />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Задержка старта (сек)</label>
            <input type="number" value={formData.start_delay_max}
              onChange={(e) => handleChange('start_delay_max', parseInt(e.target.value) || 1)}
              className="input" min={1} max={300} title="Рандомная задержка старта каждого аккаунта (от 1 до указанного значения)" />
          </div>
        </div>
        <p className="text-xs text-dark-500 mt-2">Задержка старта — каждый аккаунт стартует с рандомной задержкой от 1 до указанного значения секунд</p>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Send className="w-5 h-5 text-accent-400" /> Telegram уведомления
        </h3>
        <div className="space-y-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Bot Token</label>
            <div className="relative">
              <SecretInput revealed={showToken} value={formData.telegram_bot_token}
                onChange={(e) => handleChange('telegram_bot_token', e.target.value)}
                className="input font-mono pr-10" placeholder="123456789:ABC..." />
              <button type="button" onClick={() => setShowToken(!showToken)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
                {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Chat ID</label>
            <input type="text" value={formData.telegram_chat_id}
              onChange={(e) => handleChange('telegram_chat_id', e.target.value)}
              className="input font-mono" placeholder="123456789" autoComplete="off" />
          </div>
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <ScrollText className="w-5 h-5 text-accent-400" /> Уровень логирования
        </h3>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <motion.button
              onClick={() => handleChange('log_level', 'info')}
              className={`p-4 rounded-xl border text-left transition-all ${
                formData.log_level === 'info'
                  ? 'bg-accent-500/20 border-accent-500/40'
                  : 'bg-dark-800/50 border-dark-700/50 hover:border-dark-600'
              }`}
              whileHover={{ scale: 1.01 }} whileTap={{ scale: 0.99 }}
            >
              <div className="flex items-center gap-2 mb-2">
                <div className={`w-3 h-3 rounded-full ${formData.log_level === 'info' ? 'bg-accent-400' : 'bg-dark-600'}`} />
                <span className={`font-medium ${formData.log_level === 'info' ? 'text-accent-300' : 'text-dark-300'}`}>
                  INFO
                </span>
              </div>
              <p className="text-xs text-dark-400">
                Базовые логи: запуск, попытки, успех, время следующей попытки
              </p>
            </motion.button>
            <motion.button
              onClick={() => handleChange('log_level', 'full')}
              className={`p-4 rounded-xl border text-left transition-all ${
                formData.log_level === 'full'
                  ? 'bg-accent-500/20 border-accent-500/40'
                  : 'bg-dark-800/50 border-dark-700/50 hover:border-dark-600'
              }`}
              whileHover={{ scale: 1.01 }} whileTap={{ scale: 0.99 }}
            >
              <div className="flex items-center gap-2 mb-2">
                <div className={`w-3 h-3 rounded-full ${formData.log_level === 'full' ? 'bg-accent-400' : 'bg-dark-600'}`} />
                <span className={`font-medium ${formData.log_level === 'full' ? 'text-accent-300' : 'text-dark-300'}`}>
                  FULL
                </span>
              </div>
              <p className="text-xs text-dark-400">
                Подробные логи: полный вывод каждого этапа, ответы API, конфиги
              </p>
            </motion.button>
          </div>
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-medium text-dark-100 flex items-center gap-2">
            <Network className="w-5 h-5 text-accent-400" /> Целевые подсети
            <span className="text-sm text-dark-500">({formData.target_subnets.length} из {Object.keys(subnets).length})</span>
          </h3>
          <div className="flex flex-wrap justify-end gap-2">
            <button onClick={() => setShowAddSubnet(v => !v)} className="btn-tool btn-tool-accent">
              <Plus className="w-4 h-4" /> Добавить
            </button>
            <button onClick={selectSubnetsByPreciseCidrs} disabled={preciseCidrs.length === 0}
              title="Отметить только подсети, в которые попадают целевые узкие диапазоны"
              className="btn-tool">
              <Target className="w-4 h-4" /> По целевым
            </button>
            <button onClick={selectAllSubnets} className="btn-tool">Выбрать все</button>
            <button onClick={clearAllSubnets} className="btn-tool">Снять все</button>
          </div>
        </div>
        {subnetMatchResult && (
          <p className={`text-xs mb-3 ${subnetMatchResult.uncovered.length > 0 ? 'text-amber-400' : 'text-dark-500'}`}>
            {subnetMatchResult.selected > 0
              ? `Отмечено подсетей по целевым диапазонам: ${subnetMatchResult.selected}. Не забудьте сохранить.`
              : 'Ни одна подсеть не подошла — выбор оставлен как был.'}
            {subnetMatchResult.uncovered.length > 0 && (
              <> Ни в одну подсеть не попали: <span className="font-mono">{subnetMatchResult.uncovered.join(', ')}</span> — для них добавьте подсеть вручную.</>
            )}
          </p>
        )}
        <AnimatePresence>
          {showAddSubnet && (
            <motion.div className="flex items-center gap-2 mb-3 p-3 bg-dark-800/50 rounded-lg border border-dark-700/50"
              initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
              <div className="flex items-center gap-1">
                <span className="text-sm text-dark-400">ext-sub</span>
                <input type="number" placeholder="ID" value={subnetForm.id}
                  onChange={e => setSubnetForm(f => ({ ...f, id: e.target.value }))}
                  className="w-16 px-2 py-1 bg-dark-900/50 border border-dark-700 rounded text-sm text-dark-100 focus:border-accent-500 outline-none" />
              </div>
              <input type="text" placeholder="CIDR (напр. 217.16.28.0/22)" value={subnetForm.cidr}
                onChange={e => setSubnetForm(f => ({ ...f, cidr: e.target.value }))}
                className="flex-1 px-2 py-1 bg-dark-900/50 border border-dark-700 rounded text-sm text-dark-100 font-mono focus:border-accent-500 outline-none" />
              <button onClick={handleAddSubnet} disabled={subnetSaving || !subnetForm.id || !subnetForm.cidr}
                className="px-3 py-1 bg-green-500/20 hover:bg-green-500/30 text-green-400 rounded text-sm disabled:opacity-40 transition-colors">
                {subnetSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Добавить'}
              </button>
              <button onClick={() => setShowAddSubnet(false)} className="p-1.5 hover:bg-dark-700/50 rounded">
                <X className="w-4 h-4 text-dark-400" />
              </button>
            </motion.div>
          )}
        </AnimatePresence>
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2 max-h-64 overflow-y-auto custom-scrollbar">
          {Object.entries(subnets).sort(([a], [b]) => parseInt(a) - parseInt(b)).map(([id, cidr]) => {
            const subnetId = parseInt(id)
            const isSelected = formData.target_subnets.includes(subnetId)
            const isCustom = !builtinIds.has(subnetId)
            return (
              <div key={id} className="relative group">
                <motion.button onClick={() => toggleSubnet(subnetId)}
                  className={`flex flex-col p-2 rounded-lg text-left text-sm transition-all w-full ${isSelected
                    ? 'bg-accent-500/20 border border-accent-500/40 text-accent-300'
                    : 'bg-dark-800/50 border border-dark-700/50 text-dark-400 hover:border-dark-600'}`}
                  whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                  <span className="font-medium">{subnetNames[parseInt(id)] || `ext-sub${id}`}</span>
                  <span className="text-xs opacity-70 font-mono">{cidr as string}</span>
                </motion.button>
                {isCustom && (
                  <button onClick={(e) => { e.stopPropagation(); handleRemoveSubnet(subnetId) }}
                    className="absolute -top-1 -right-1 p-0.5 bg-danger/80 hover:bg-danger text-white rounded-full opacity-0 group-hover:opacity-100 transition-opacity"
                    title="Удалить подсеть">
                    <X className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
            )
          })}
        </div>
        <p className="text-sm text-dark-400 mt-4 mb-2">Если выбрано несколько подсетей</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {SUBNET_MODES.map(mode => {
            const isActive = formData.subnet_mode === mode.value
            return (
              <motion.button key={mode.value} onClick={() => handleChange('subnet_mode', mode.value)}
                className={`p-3 rounded-xl border text-left transition-all ${isActive
                  ? 'bg-accent-500/20 border-accent-500/40'
                  : 'bg-dark-800/50 border-dark-700/50 hover:border-dark-600'}`}
                whileHover={{ scale: 1.01 }} whileTap={{ scale: 0.99 }}>
                <div className="flex items-center gap-2 mb-1">
                  <div className={`w-3 h-3 rounded-full ${isActive ? 'bg-accent-400' : 'bg-dark-600'}`} />
                  <span className={`font-medium text-sm ${isActive ? 'text-accent-300' : 'text-dark-300'}`}>{mode.title}</span>
                </div>
                <p className="text-xs text-dark-400">{mode.hint}</p>
              </motion.button>
            )
          })}
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <div className="flex items-center justify-between mb-2">
          <h3 className="font-medium text-dark-100 flex items-center gap-2">
            <Target className="w-5 h-5 text-amber-400" /> Целевой узкий диапазон
            <span className="text-sm text-dark-500">({preciseCidrs.length})</span>
          </h3>
        </div>
        <p className="text-xs text-dark-500 mb-3">
          Держим только IP, попавшие в один из этих диапазонов (напр. нужный <span className="font-mono">/24</span> внутри
          выбранной подсети). Остальные адреса воркер сразу освобождает и перебирает дальше. Пусто — оставляем любой IP подсети.
        </p>
        <textarea placeholder={'5.188.141.0/24\n109.120.180.0/24'} value={preciseInput}
          onChange={e => setPreciseInput(e.target.value)}
          className="w-full h-24 px-2 py-1.5 bg-dark-900/50 border border-dark-700 rounded text-sm text-dark-100 font-mono focus:border-amber-500 outline-none" />
        <div className="flex items-center justify-between gap-2 mt-2 mb-3">
          <span className="text-xs text-dark-500">
            {preciseResult || 'Можно вставить несколько строк / через запятую / пробел'}
          </span>
          <button onClick={handleAddPreciseCidrs} disabled={preciseSaving || !preciseInput.trim()}
            className="px-3 py-1.5 bg-amber-500/20 hover:bg-amber-500/30 text-amber-400 rounded text-sm disabled:opacity-40 transition-colors flex items-center gap-1 shrink-0">
            {preciseSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Plus className="w-4 h-4" /> Добавить</>}
          </button>
        </div>
        <div className="flex flex-wrap gap-2">
          {preciseCidrs.length === 0 ? (
            <span className="text-xs text-dark-500 italic">Фильтр выключен — ловим любой IP из выбранных подсетей</span>
          ) : preciseCidrs.map(pc => (
            <span key={pc.id} className="inline-flex items-center gap-1.5 px-2 py-1 bg-amber-500/10 border border-amber-500/30 rounded text-xs text-amber-300 font-mono">
              {pc.cidr}
              <button onClick={() => handleRemovePreciseCidr(pc.id)} className="text-dark-400 hover:text-danger" title="Удалить диапазон">
                <X className="w-4 h-4" />
              </button>
            </span>
          ))}
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Settings className="w-5 h-5 text-accent-400" /> Дополнительно
        </h3>
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-dark-400 mb-1">Max IP на проект</label>
              <input type="number" value={formData.max_ip_per_project}
                onChange={(e) => handleChange('max_ip_per_project', parseInt(e.target.value) || 1)}
                className="input" min={1} />
            </div>
            <div>
              <label className="block text-sm text-dark-400 mb-1">Интервал проверки proxy (сек)</label>
              <input type="number" value={formData.proxy_check_interval}
                onChange={(e) => handleChange('proxy_check_interval', parseInt(e.target.value) || 50)}
                className="input" min={10} />
            </div>
          </div>
          <div className="flex items-center gap-6">
            <label className="flex items-center gap-3 cursor-pointer">
              <Checkbox checked={formData.stop_on_catch}
                onChange={(e) => handleChange('stop_on_catch', e.target.checked)} />
              <span className="text-sm text-dark-300">Остановить при поимке</span>
            </label>
            <label className="flex items-center gap-3 cursor-pointer">
              <Checkbox checked={formData.proxy_required}
                onChange={(e) => handleChange('proxy_required', e.target.checked)} />
              <span className="text-sm text-dark-300">Proxy обязательно</span>
            </label>
          </div>
        </div>
      </motion.div>

      <motion.div className="flex justify-end" variants={itemVariants}>
        <motion.button onClick={handleSave} disabled={!hasChanges || isSaving}
          className={`btn ${hasChanges ? 'btn-primary' : 'btn-secondary opacity-50'}`}
          whileHover={hasChanges ? { scale: 1.02 } : {}} whileTap={hasChanges ? { scale: 0.98 } : {}}>
          {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          Сохранить настройки
        </motion.button>
      </motion.div>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// LOGS TAB
// ═══════════════════════════════════════════════════════════════════════

function LogsTab() {
  const {
    status, logs, isLoadingLogs, isSaving,
    fetchStatus, fetchLogs, clearLogs, startWorker, stopWorker,
    subscribeToLogs, unsubscribeFromLogs,
    accounts, workerStatuses, accountErrors, fetchWorkerStatuses,
    startAccount, stopAccount, clearAccountErrors
  } = useExtStore()

  const [autoScroll, setAutoScroll] = useState(() => {
    const saved = readStorage('ext-logs-autoscroll')
    return saved !== null ? saved === 'true' : true
  })
  const [dismissedErrors, setDismissedErrors] = useState<Set<number>>(new Set())
  const logsEndRef = useRef<HTMLDivElement>(null)
  const logsContainerRef = useRef<HTMLDivElement>(null)
  const isAutoScrollingRef = useRef(false)

  useEffect(() => {
    writeStorage('ext-logs-autoscroll', String(autoScroll))
  }, [autoScroll])

  useEffect(() => {
    fetchStatus()
    fetchLogs()
    fetchWorkerStatuses()
    const statusInterval = setInterval(fetchStatus, 5000)
    const workerInterval = setInterval(fetchWorkerStatuses, 3000)
    return () => { clearInterval(statusInterval); clearInterval(workerInterval); unsubscribeFromLogs() }
  }, [fetchStatus, fetchLogs, fetchWorkerStatuses, unsubscribeFromLogs])

  // Subscribe to SSE only when running, and skip duplicates
  useEffect(() => {
    if (status.running) {
      // Small delay to let fetchLogs finish first, then SSE takes over
      const timeout = setTimeout(() => subscribeToLogs(), 500)
      return () => clearTimeout(timeout)
    }
  }, [status.running, subscribeToLogs])

  useEffect(() => {
    if (autoScroll && logsContainerRef.current) {
      isAutoScrollingRef.current = true
      requestAnimationFrame(() => {
        if (logsContainerRef.current) {
          logsContainerRef.current.scrollTop = logsContainerRef.current.scrollHeight
        }
        setTimeout(() => { isAutoScrollingRef.current = false }, 100)
      })
    }
  }, [logs, autoScroll])

  const handleScroll = () => {
    if (isAutoScrollingRef.current || !logsContainerRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = logsContainerRef.current
    const isNearBottom = scrollHeight - scrollTop - clientHeight < 100
    if (!isNearBottom && autoScroll) setAutoScroll(false)
  }

  const getLogColor = (log: string) => {
    if (log.includes('ACCESS_DENIED') || log.includes('BALANCE_DEPLETED')) return 'text-red-400 font-medium'
    if (log.includes('QUOTA_EXCEEDED')) return 'text-orange-400'
    if (log.includes('IP_CAUGHT') || log.includes('SUCCESS') || log.includes('Caught')) return 'text-green-400 font-bold'
    if (log.includes('PROXY_RECOVERED')) return 'text-green-400 font-medium'
    if (log.includes('PROXY_DEAD') || log.includes('PROXY_FAILED') || log.includes('PROXY_ERROR')) return 'text-purple-400'
    if (log.includes('ERROR') || log.includes('error') || log.includes('Failed') || log.includes('BLOCKED')) return 'text-red-400'
    if (log.includes('WARNING') || log.includes('warning')) return 'text-yellow-400'
    if (log.includes('FINISHED') || log.includes('ALL_PROJECTS_EXHAUSTED')) return 'text-dark-500'
    if (log.includes('SCHEDULED')) return 'text-blue-400/60'
    if (log.includes('START') || log.includes('Starting') || log.includes('[SYSTEM]')) return 'text-blue-400'
    if (log.includes('SUBNET_FOUND')) return 'text-cyan-400'
    if (log.includes('PROXY_OK')) return 'text-purple-300'
    return 'text-dark-300'
  }

  const visibleErrors = (() => {
    const filtered = accountErrors.filter(e => !dismissedErrors.has(e.id))
    const seen = new Map<string, number>()
    return filtered.filter(e => {
      const isBalance = e.error_type === 'ACCESS_DENIED' || e.error_type === 'BALANCE_DEPLETED'
      if (!isBalance) return true
      const key = `balance_${e.account_id}`
      if (seen.has(key)) return false
      seen.set(key, e.id)
      return true
    })
  })()
  const workerList = Object.values(workerStatuses) as Array<{ status: string; attempts: number; caught: number; error_type?: string | null }>

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      {/* Global controls */}
      <motion.div className="flex items-center justify-between" variants={itemVariants}>
        <div className="flex items-center gap-4">
          <div className={`flex items-center gap-2 px-3 py-1.5 rounded-lg ${status.running ? 'bg-success/10 text-success' : 'bg-dark-700/50 text-dark-400'}`}>
            <span className={`w-2 h-2 rounded-full ${status.running ? 'bg-success animate-pulse' : 'bg-dark-500'}`} />
            <span className="text-sm font-medium">
              {status.running ? `Запущен (${status.active_count || workerList.filter(w => w.status === 'running').length} потоков)` :
                status.status === 'starting' ? 'Запускается...' :
                  status.status === 'stopping' ? 'Останавливается...' : 'Остановлен'}
            </span>
          </div>
          {status.running ? (
            <motion.button onClick={stopWorker} disabled={isSaving}
              className="btn bg-danger hover:bg-danger/80 text-white" whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
              {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
              Остановить все
            </motion.button>
          ) : (
            <motion.button onClick={startWorker} disabled={isSaving}
              className="btn btn-primary" whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
              {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              Запустить все
            </motion.button>
          )}
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-2 text-sm text-dark-400 cursor-pointer">
            <Checkbox checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} />
            Автоскролл
          </label>
          <motion.button onClick={() => fetchLogs()} disabled={isLoadingLogs} className="btn btn-ghost p-2"
            whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
            <RefreshCw className={`w-4 h-4 ${isLoadingLogs ? 'animate-spin' : ''}`} />
          </motion.button>
          <motion.button onClick={clearLogs} className="btn btn-ghost p-2 text-dark-400 hover:text-dark-200"
            whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
            <Trash2 className="w-4 h-4" />
          </motion.button>
        </div>
      </motion.div>

      {/* Account Status Overview Panel */}
      {accounts.length > 0 && (
        <motion.div variants={itemVariants}>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {accounts.filter(a => a.enabled).map((account) => {
              const ws = workerStatuses[account.id]
              const isRunning = ws?.status === 'running'
              const hasError = ws?.status === 'error'
              const isStopping = ws?.status === 'stopping'
              const isProxyWait = ws?.status === 'proxy_waiting'
              
              return (
                <div key={account.id} className={`flex items-center justify-between p-2.5 rounded-lg text-sm transition-all ${
                  isRunning ? 'bg-success/5 border border-success/20' :
                  isProxyWait ? 'bg-purple-500/5 border border-purple-500/20' :
                  hasError ? 'bg-danger/5 border border-danger/20' :
                  'bg-dark-800/30 border border-dark-700/30'
                }`}>
                  <div className="flex items-center gap-2 min-w-0 flex-1">
                    <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
                      isRunning ? 'bg-success animate-pulse' : isProxyWait ? 'bg-purple-500 animate-pulse' : hasError ? 'bg-danger' : 'bg-dark-500'
                    }`} />
                    <span className="text-dark-300 truncate">{account.email.split('@')[0]}</span>
                    {isRunning && ws && (
                      <span className="text-dark-500 text-xs flex-shrink-0">{ws.attempts}a/{ws.caught}c</span>
                    )}
                    {isProxyWait && (
                      <span className="text-xs text-purple-400 flex-shrink-0">прокси...</span>
                    )}
                    {hasError && ws?.error_type && (
                      <span className="text-xs text-danger flex-shrink-0">
                        {ws.error_type === 'ACCESS_DENIED' || ws.error_type === 'BALANCE_DEPLETED' ? 'баланс' :
                         ws.error_type === 'PROXY_DEAD' ? 'прокси' :
                         ws.error_type === 'QUOTA_EXCEEDED' ? 'квота' : 'ошибка'}
                      </span>
                    )}
                  </div>
                  <div className="flex-shrink-0 ml-2">
                    {isRunning || isStopping || isProxyWait ? (
                      <motion.button onClick={() => stopAccount(account.id)} disabled={isStopping}
                        className="p-1 text-danger hover:bg-danger/10 rounded"
                        whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                        {isStopping ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Square className="w-3.5 h-3.5" />}
                      </motion.button>
                    ) : (
                      <motion.button onClick={() => startAccount(account.id)}
                        className="p-1 text-success hover:bg-success/10 rounded"
                        whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                        <Play className="w-3.5 h-3.5" />
                      </motion.button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </motion.div>
      )}

      {/* Error Notification Banners */}
      <AnimatePresence>
        {visibleErrors.length > 0 && (
          <motion.div className="space-y-2" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            {visibleErrors.map((err) => {
              const accEmail = accounts.find(a => a.id === err.account_id)?.email || `ACC ${err.account_id}`
              const isBalance = err.error_type === 'ACCESS_DENIED' || err.error_type === 'BALANCE_DEPLETED'
              const isQuota = err.error_type === 'QUOTA_EXCEEDED'
              const isProxy = err.error_type === 'PROXY_DEAD' || err.error_type === 'PROXY_FAILED'
              
              return (
                <motion.div key={err.id}
                  className={`flex items-center gap-3 p-3 rounded-xl text-sm ${
                    isBalance ? 'bg-danger/10 border border-danger/20 text-danger' :
                    isProxy ? 'bg-purple-500/10 border border-purple-500/20 text-purple-400' :
                    isQuota ? 'bg-orange-500/10 border border-orange-500/20 text-orange-400' :
                    'bg-yellow-500/10 border border-yellow-500/20 text-yellow-400'
                  }`}
                  initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, height: 0 }}>
                  {isBalance ? <Wallet className="w-4 h-4 flex-shrink-0" /> :
                   isProxy ? <Globe className="w-4 h-4 flex-shrink-0" /> :
                   <AlertTriangle className="w-4 h-4 flex-shrink-0" />}
                  <span className="flex-1">
                    <strong>{accEmail}</strong>: {
                      isBalance ? 'баланс исчерпан, пополните аккаунт' :
                      isProxy ? 'прокси не работает' :
                      isQuota ? `квота проекта ${err.project_name || ''} исчерпана` :
                      err.error_message || err.error_type
                    }
                  </span>
                  <button onClick={() => {
                    setDismissedErrors(prev => new Set([...prev, err.id]))
                    clearAccountErrors(err.account_id)
                  }} className="p-1.5 hover:opacity-70 rounded">
                    <X className="w-4 h-4" />
                  </button>
                </motion.div>
              )
            })}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Log Console */}
      <motion.div className="card p-0 overflow-hidden" variants={itemVariants}>
        <div ref={logsContainerRef} onScroll={handleScroll}
          className="h-[500px] overflow-y-auto custom-scrollbar bg-dark-900/50 font-mono text-xs">
          {logs.length === 0 ? (
            <div className="flex items-center justify-center h-full text-dark-500">
              <div className="text-center">
                <ScrollText className="w-10 h-10 mx-auto mb-2 opacity-50" />
                <p>Нет логов</p>
                <p className="text-xs mt-1">Запустите аккаунты для просмотра логов</p>
              </div>
            </div>
          ) : (
            <div className="p-3 space-y-0.5">
              {logs.map((log, index) => (
                <div key={index} className={`${getLogColor(log)} leading-5`}>{log}</div>
              ))}
              <div ref={logsEndRef} />
            </div>
          )}
        </div>
      </motion.div>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// MANUAL IP TAB
// ═══════════════════════════════════════════════════════════════════════

function ManualTab() {
  const {
    accounts, projects, settings,
    fetchAccounts, fetchProjects, fetchSettings,
    manualObtainIp, manualObtainHistory, isObtaining, clearManualHistory,
    error, clearError
  } = useExtStore()

  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(null)
  const [selectedProjectId, setSelectedProjectId] = useState<number | null>(null)
  const [selectedSubnetKey, setSelectedSubnetKey] = useState<number | null>(null)
  const [useCustomSubnet, setUseCustomSubnet] = useState(false)
  const [customSubnetName, setCustomSubnetName] = useState('')
  const [copied, setCopied] = useState<number | null>(null)
  const [subnetNames, setSubnetNames] = useState<Record<number, string>>({})

  useEffect(() => { fetchAccounts(); fetchSettings() }, [fetchAccounts, fetchSettings])

  useEffect(() => {
    extApi.getSubnets().then(res => {
      if (res.data.subnet_names) setSubnetNames(res.data.subnet_names)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (selectedAccountId) {
      if (!projects[selectedAccountId]) fetchProjects(selectedAccountId)
      setSelectedProjectId(null)
    }
  }, [selectedAccountId, fetchProjects, projects])

  const accountProjects = selectedAccountId ? (projects[selectedAccountId] || []) : []
  const subnets = settings?.available_subnets || {}

  const canObtain = selectedAccountId && selectedProjectId &&
    (useCustomSubnet ? customSubnetName.trim() : selectedSubnetKey !== null) && !isObtaining

  const handleObtain = async () => {
    if (!canObtain || !selectedAccountId || !selectedProjectId) return

    const account = accounts.find(a => a.id === selectedAccountId)
    const project = accountProjects.find(p => p.id === selectedProjectId)
    if (!account || !project) return

    const subnetLabel = useCustomSubnet
      ? customSubnetName.trim()
      : `${subnetNames[selectedSubnetKey!] || `ext-sub${selectedSubnetKey}`} — ${subnets[selectedSubnetKey!]}`

    const data: ManualObtainRequest = {
      account_id: selectedAccountId,
      project_db_id: selectedProjectId,
      subnet_key: selectedSubnetKey || 0,
      custom_subnet_name: useCustomSubnet ? customSubnetName.trim() : undefined,
    }

    await manualObtainIp(data, account.email, project.project_name, subnetLabel)
  }

  const handleCopyIp = (ip: string, entryId: number) => {
    navigator.clipboard.writeText(ip)
    setCopied(entryId)
    setTimeout(() => setCopied(null), 2000)
  }

  const lastResult = manualObtainHistory[0]

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm flex-1">{error}</span>
            <button onClick={clearError} className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Форма */}
      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Target className="w-5 h-5 text-accent-400" /> Получение IP вручную
        </h3>

        <div className="space-y-4">
          {/* Аккаунт */}
          <div>
            <label className="block text-sm text-dark-400 mb-1">Аккаунт</label>
            <select
              value={selectedAccountId || ''}
              onChange={e => setSelectedAccountId(e.target.value ? Number(e.target.value) : null)}
              className="input"
            >
              <option value="">Выберите аккаунт...</option>
              {accounts.map(acc => (
                <option key={acc.id} value={acc.id}>
                  {acc.email}{acc.proxy ? ' (proxy)' : ''}
                </option>
              ))}
            </select>
          </div>

          {/* Проект */}
          <div>
            <label className="block text-sm text-dark-400 mb-1">Проект</label>
            <select
              value={selectedProjectId || ''}
              onChange={e => setSelectedProjectId(e.target.value ? Number(e.target.value) : null)}
              className="input"
              disabled={!selectedAccountId}
            >
              <option value="">
                {!selectedAccountId ? 'Сначала выберите аккаунт' :
                 accountProjects.length === 0 ? 'Нет проектов' : 'Выберите проект...'}
              </option>
              {accountProjects.map(proj => (
                <option key={proj.id} value={proj.id}>
                  {proj.project_name} ({proj.project_id})
                </option>
              ))}
            </select>
          </div>

          {/* Подсеть */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="block text-sm text-dark-400">Подсеть</label>
              <div className="flex gap-2">
                <button
                  onClick={() => { setUseCustomSubnet(false); setCustomSubnetName('') }}
                  className={`text-xs px-2 py-0.5 rounded ${!useCustomSubnet ? 'bg-accent-500/20 text-accent-400' : 'text-dark-400 hover:text-dark-300'}`}
                >
                  Из списка
                </button>
                <button
                  onClick={() => { setUseCustomSubnet(true); setSelectedSubnetKey(null) }}
                  className={`text-xs px-2 py-0.5 rounded ${useCustomSubnet ? 'bg-accent-500/20 text-accent-400' : 'text-dark-400 hover:text-dark-300'}`}
                >
                  Своя
                </button>
              </div>
            </div>
            {useCustomSubnet ? (
              <input
                type="text"
                value={customSubnetName}
                onChange={e => setCustomSubnetName(e.target.value)}
                className="input text-sm font-mono"
                placeholder="ext-sub99 или имя подсети"
              />
            ) : (
              <select
                value={selectedSubnetKey ?? ''}
                onChange={e => setSelectedSubnetKey(e.target.value ? Number(e.target.value) : null)}
                className="input"
              >
                <option value="">Выберите подсеть...</option>
                {Object.entries(subnets).sort(([a], [b]) => parseInt(a) - parseInt(b)).map(([id, cidr]) => (
                  <option key={id} value={id}>{subnetNames[parseInt(id)] || `ext-sub${id}`} — {cidr as string}</option>
                ))}
              </select>
            )}
          </div>

          {/* Кнопка */}
          <motion.button
            onClick={handleObtain}
            disabled={!canObtain}
            className={`btn w-full justify-center ${canObtain ? 'btn-primary' : 'btn-secondary opacity-50'}`}
            whileHover={canObtain ? { scale: 1.01 } : {}}
            whileTap={canObtain ? { scale: 0.99 } : {}}
          >
            {isObtaining ? (
              <>
                <Loader2 className="w-5 h-5 animate-spin" />
                Получение IP...
              </>
            ) : (
              <>
                <Zap className="w-5 h-5" />
                Получить IP
              </>
            )}
          </motion.button>
        </div>
      </motion.div>

      {/* Последний результат */}
      <AnimatePresence>
        {lastResult && !isObtaining && (
          <motion.div
            className={`card border ${lastResult.success ? 'border-success/30 bg-success/5' : 'border-danger/30 bg-danger/5'}`}
            initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }}
          >
            <div className="flex items-center gap-3">
              {lastResult.success ? (
                <>
                  <div className="w-10 h-10 rounded-xl bg-success/20 flex items-center justify-center flex-shrink-0">
                    <CheckCircle2 className="w-5 h-5 text-success" />
                  </div>
                  <div className="flex-1">
                    <div className="font-mono text-lg text-success font-bold">{lastResult.ipAddress}</div>
                    <div className="text-xs text-dark-400">{lastResult.subnetName} • {lastResult.accountEmail}</div>
                  </div>
                  <motion.button
                    onClick={() => handleCopyIp(lastResult.ipAddress!, lastResult.id)}
                    className="btn btn-ghost p-2 text-dark-400 hover:text-success"
                    whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}
                  >
                    {copied === lastResult.id ? <CheckCircle2 className="w-5 h-5 text-success" /> : <Copy className="w-5 h-5" />}
                  </motion.button>
                </>
              ) : (
                <>
                  <div className="w-10 h-10 rounded-xl bg-danger/20 flex items-center justify-center flex-shrink-0">
                    <AlertCircle className="w-5 h-5 text-danger" />
                  </div>
                  <div className="flex-1">
                    <div className="text-sm text-danger font-medium">Не удалось получить IP</div>
                    <div className="text-xs text-dark-400 mt-0.5">{lastResult.error}</div>
                    <div className="text-xs text-dark-500 mt-0.5">{lastResult.subnetName} • {lastResult.accountEmail}</div>
                  </div>
                </>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* История */}
      {manualObtainHistory.length > 0 && (
        <motion.div className="card" variants={itemVariants}>
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-medium text-dark-100 flex items-center gap-2 text-sm">
              <Clock className="w-4 h-4 text-accent-400" />
              История попыток
              <span className="text-dark-500">({manualObtainHistory.length})</span>
            </h3>
            <motion.button onClick={clearManualHistory}
              className="btn btn-ghost text-xs text-dark-400 hover:text-danger p-1.5"
              whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}>
              <Trash2 className="w-3.5 h-3.5" /> Очистить
            </motion.button>
          </div>

          <div className="space-y-1.5 max-h-64 overflow-y-auto custom-scrollbar">
            {manualObtainHistory.map((entry) => (
              <div key={entry.id}
                className={`flex items-center gap-3 p-2.5 rounded-lg text-sm ${
                  entry.success ? 'bg-success/5 border border-success/10' : 'bg-danger/5 border border-danger/10'
                }`}>
                <span className={`w-2 h-2 rounded-full flex-shrink-0 ${entry.success ? 'bg-success' : 'bg-danger'}`} />
                <span className="text-dark-500 text-xs w-16 flex-shrink-0">{entry.timestamp.split(', ')[1] || entry.timestamp}</span>
                <span className="text-dark-300 truncate flex-1">{entry.accountEmail.split('@')[0]}</span>
                <span className="text-dark-500 text-xs truncate max-w-32">{entry.subnetName.split(' — ')[0]}</span>
                {entry.success ? (
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <span className="font-mono text-success text-xs">{entry.ipAddress}</span>
                    <button onClick={() => handleCopyIp(entry.ipAddress!, entry.id)}
                      className="p-1 text-dark-500 hover:text-success rounded">
                      {copied === entry.id ? <CheckCircle2 className="w-4 h-4 text-success" /> : <Copy className="w-4 h-4" />}
                    </button>
                  </div>
                ) : (
                  <span className="text-danger text-xs truncate max-w-48 flex-shrink-0" title={entry.error}>{entry.error}</span>
                )}
              </div>
            ))}
          </div>
        </motion.div>
      )}
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// MULTIQUEUE TAB
// ═══════════════════════════════════════════════════════════════════════

interface RcFileData {
  auth_url: string
  project_id: string
  auth_type: 'password' | 'v3token'
  username: string
  user_domain_name: string
}

function parseRcFile(content: string): RcFileData | null {
  const get = (key: string): string => {
    const m = content.match(new RegExp(`export\\s+${key}=["']?([^"'\\n]+)["']?`))
    return m ? m[1].trim() : ''
  }
  const auth_url = get('OS_AUTH_URL')
  const project_id = get('OS_PROJECT_ID')
  if (!auth_url || !project_id) return null

  const raw_auth_type = get('OS_AUTH_TYPE')
  const is_token = raw_auth_type === 'v3token' || content.includes('OS_TOKEN')
  const auth_type = is_token ? 'v3token' as const : 'password' as const
  const username = get('OS_USERNAME')
  const user_domain_name = get('OS_USER_DOMAIN_NAME') || 'users'

  if (auth_type === 'password' && !username) return null
  return { auth_url, project_id, auth_type, username, user_domain_name }
}

function findAccountByEmail(accounts: ExtAccount[], email: string): ExtAccount | null {
  const wanted = email.trim().toLowerCase()
  if (!wanted) return null
  return accounts.find(a => a.email.trim().toLowerCase() === wanted) ?? null
}

function MultiqueueCredentialsStatus({ rcData, account, isLoading, secret, proxy, hasImageId }: {
  rcData: RcFileData
  account: ExtAccount | null
  isLoading: boolean
  secret: string
  proxy: string
  hasImageId: boolean
}) {
  if (!rcData.username) return null

  if (!account) {
    return (
      <div className="mt-3 rounded-xl border border-dark-700/50 bg-dark-800/30 px-4 py-3 text-xs text-dark-400 flex items-center gap-2">
        {isLoading ? (
          <><Loader2 className="w-4 h-4 animate-spin flex-shrink-0" /> Ищу аккаунт в базе…</>
        ) : (
          <><User className="w-4 h-4 flex-shrink-0" /> Аккаунта {rcData.username} нет в базе — введите {rcData.auth_type === 'v3token' ? 'API токен' : 'пароль'} и при необходимости прокси вручную.</>
        )}
      </div>
    )
  }

  const accountProxy = account.proxy ?? ''
  const currentProxy = proxy.trim()
  let proxyStatus = 'У аккаунта нет прокси — при необходимости укажите его ниже'
  if (accountProxy && currentProxy === accountProxy) proxyStatus = 'Прокси аккаунта подставлен'
  else if (accountProxy && !currentProxy) proxyStatus = 'Прокси аккаунта убран — запросы пойдут напрямую'
  else if (currentProxy) proxyStatus = 'Прокси указан вручную'

  const isPasswordMode = rcData.auth_type === 'password'
  const isPasswordFromAccount = !!account.password && secret === account.password
  const passwordStatus = isPasswordFromAccount ? 'Пароль подставлен из базы' : 'Пароль изменён вручную'

  return (
    <div className="mt-3 rounded-xl border border-green-500/30 bg-green-500/5 px-4 py-3">
      <p className="text-sm text-green-300 font-medium flex items-center gap-2">
        <UserCheck className="w-4 h-4 flex-shrink-0" /> Аккаунт найден в базе
      </p>
      <p className="text-xs text-dark-400 mt-1">
        {isPasswordMode ? `${passwordStatus}. ` : ''}{proxyStatus}.
        {isPasswordMode && isPasswordFromAccount && !hasImageId && ' Осталось ввести ID образа.'}
      </p>
    </div>
  )
}

function MultiqueueTab() {
  const { accounts, isLoadingAccounts, fetchAccounts } = useExtStore()
  const [rcData, setRcData] = useState<RcFileData | null>(null)
  const [rcFileName, setRcFileName] = useState<string>('')
  const [secret, setSecret] = useState('')
  const [imageId, setImageId] = useState('')
  const [proxy, setProxy] = useState('')
  const [showSecret, setShowSecret] = useState(false)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<{ success: boolean; error?: string; image_name?: string } | null>(null)
  const [parseError, setParseError] = useState<string>('')
  const fileInputRef = useRef<HTMLInputElement>(null)
  const autofilledRcRef = useRef<RcFileData | null>(null)

  useEffect(() => { fetchAccounts() }, [fetchAccounts])

  const matchedAccount = rcData ? findAccountByEmail(accounts, rcData.username) : null

  // Креды подставляются один раз на загруженный файл — список аккаунтов может прийти позже файла,
  // а повторная подстановка затёрла бы правки пароля и прокси, сделанные руками
  useEffect(() => {
    if (!rcData || !matchedAccount || autofilledRcRef.current === rcData) return
    autofilledRcRef.current = rcData
    if (rcData.auth_type === 'password') setSecret(matchedAccount.password)
    setProxy(matchedAccount.proxy ?? '')
  }, [rcData, matchedAccount])

  const processFile = (file: File) => {
    setRcFileName(file.name)
    setParseError('')
    setResult(null)
    const reader = new FileReader()
    reader.onload = (ev) => {
      const parsed = parseRcFile(ev.target?.result as string)
      if (parsed) {
        setRcData(parsed)
      } else {
        setRcData(null)
        setParseError('Не удалось распарсить RC-файл. Нужен OS_AUTH_URL и OS_PROJECT_ID.')
      }
    }
    reader.readAsText(file)
  }

  const handleReset = () => {
    setRcData(null)
    setRcFileName('')
    setSecret('')
    setImageId('')
    setProxy('')
    setResult(null)
    setParseError('')
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const handleSubmit = async () => {
    if (!rcData || !secret || !imageId.trim()) return
    setLoading(true)
    setResult(null)
    try {
      const resp = await extApi.enableMultiqueue({
        auth_url: rcData.auth_url,
        project_id: rcData.project_id,
        image_id: imageId.trim(),
        auth_type: rcData.auth_type,
        ...(proxy.trim() ? { proxy: proxy.trim() } : {}),
        ...(rcData.auth_type === 'v3token'
          ? { api_token: secret }
          : { username: rcData.username, user_domain_name: rcData.user_domain_name, password: secret }
        ),
      })
      setResult(resp.data)
    } catch (err: any) {
      setResult({ success: false, error: err.response?.data?.detail || err.response?.data?.error || 'Ошибка запроса' })
    } finally {
      setLoading(false)
    }
  }

  const isTokenMode = rcData?.auth_type === 'v3token'
  const canSubmit = rcData && secret && imageId.trim() && !loading

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-1">
          <Cpu className="w-5 h-5 text-accent-400" /> Включение Multiqueue
        </h3>
        <p className="text-xs text-dark-500 mb-4">
          Устанавливает <code className="text-dark-300">hw_vif_multiqueue_enabled=true</code> на образе.
          Данные не сохраняются — одноразовое выполнение.
        </p>

        {/* Загрузка RC-файла */}
        <div className="mb-4">
          <label className="block text-sm text-dark-400 mb-2">RC-файл (openrc.sh)</label>
          {!rcData ? (
            <div
              onDrop={e => { e.preventDefault(); const f = e.dataTransfer.files?.[0]; if (f) processFile(f) }}
              onDragOver={e => e.preventDefault()}
              onClick={() => fileInputRef.current?.click()}
              className="border-2 border-dashed border-dark-700 hover:border-accent-500/50 rounded-xl p-8 text-center cursor-pointer transition-colors"
            >
              <Upload className="w-8 h-8 text-dark-500 mx-auto mb-2" />
              <p className="text-sm text-dark-400">Перетащите RC-файл сюда или нажмите для выбора</p>
              <p className="text-xs text-dark-500 mt-1">Портал → Настройки проекта → Доступ по API → Скачать openrc</p>
              <input ref={fileInputRef} type="file" accept=".sh,.bash,.txt"
                onChange={e => { const f = e.target.files?.[0]; if (f) processFile(f) }} className="hidden" />
            </div>
          ) : (
            <div className="bg-dark-800/50 border border-dark-700/50 rounded-xl p-4">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="w-4 h-4 text-green-400" />
                  <span className="text-sm text-dark-200">{rcFileName}</span>
                  <span className={`text-xs px-1.5 py-0.5 rounded ${isTokenMode ? 'bg-cyan-500/20 text-cyan-400' : 'bg-amber-500/20 text-amber-400'}`}>
                    {isTokenMode ? 'Token' : 'Password'}
                  </span>
                </div>
                <button onClick={handleReset} className="btn-tool">
                  <X className="w-4 h-4" /> Сбросить
                </button>
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div>
                  <span className="text-dark-500">Auth URL:</span>
                  <span className="text-dark-300 ml-1 font-mono break-all">{rcData.auth_url}</span>
                </div>
                <div>
                  <span className="text-dark-500">Project ID:</span>
                  <span className="text-dark-300 ml-1 font-mono">{rcData.project_id}</span>
                </div>
                {!isTokenMode && rcData.username && (
                  <>
                    <div>
                      <span className="text-dark-500">Username:</span>
                      <span className="text-dark-300 ml-1">{rcData.username}</span>
                    </div>
                    <div>
                      <span className="text-dark-500">Domain:</span>
                      <span className="text-dark-300 ml-1">{rcData.user_domain_name}</span>
                    </div>
                  </>
                )}
              </div>
            </div>
          )}
          {parseError && (
            <p className="text-xs text-danger mt-2 flex items-center gap-1">
              <AlertTriangle className="w-3.5 h-3.5" /> {parseError}
            </p>
          )}
          {rcData && (
            <MultiqueueCredentialsStatus
              rcData={rcData}
              account={matchedAccount}
              isLoading={isLoadingAccounts}
              secret={secret}
              proxy={proxy}
              hasImageId={!!imageId.trim()}
            />
          )}
        </div>

        {/* Секрет (пароль или API токен) */}
        <div className="mb-4">
          <label className="block text-sm text-dark-400 mb-1">
            {isTokenMode ? 'API токен' : 'Пароль'}
          </label>
          <div className="relative">
            <SecretInput
              revealed={showSecret}
              value={secret}
              onChange={e => setSecret(e.target.value)}
              className="input pr-10"
              placeholder={isTokenMode ? 'API токен из портала' : 'Пароль от аккаунта'}
              disabled={!rcData}
            />
            <button
              onClick={() => setShowSecret(v => !v)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-dark-500 hover:text-dark-300"
              type="button"
            >
              {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
        </div>

        {/* ID образа */}
        <div className="mb-4">
          <label className="block text-sm text-dark-400 mb-1">ID образа</label>
          <input
            type="text"
            value={imageId}
            onChange={e => setImageId(e.target.value)}
            className="input font-mono text-sm"
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            disabled={!rcData}
          />
        </div>

        {/* SOCKS5-прокси до API облака */}
        <div className="mb-4">
          <label className="block text-sm text-dark-400 mb-1">SOCKS5 Proxy (опционально)</label>
          <input
            type="text"
            value={proxy}
            onChange={e => setProxy(e.target.value)}
            className="input font-mono text-sm"
            placeholder="host:port:user:pass"
            autoComplete="off"
            disabled={!rcData}
          />
          <p className="text-xs text-dark-500 mt-1">
            Запросы к Keystone и Glance пойдут через прокси, как у аккаунтов. Если аккаунт есть в базе, его прокси подставится сам — его можно поменять или стереть.
          </p>
        </div>

        {/* Кнопка */}
        <motion.button
          onClick={handleSubmit}
          disabled={!canSubmit}
          className="w-full py-2.5 rounded-xl font-medium text-sm flex items-center justify-center gap-2 transition-all bg-accent-500/20 hover:bg-accent-500/30 text-accent-400 disabled:opacity-40 disabled:cursor-not-allowed"
          whileHover={canSubmit ? { scale: 1.01 } : {}}
          whileTap={canSubmit ? { scale: 0.99 } : {}}
        >
          {loading ? (
            <><Loader2 className="w-4 h-4 animate-spin" /> Выполнение...</>
          ) : (
            <><Zap className="w-4 h-4" /> Включить Multiqueue</>
          )}
        </motion.button>
      </motion.div>

      {/* Результат */}
      <AnimatePresence>
        {result && (
          <motion.div
            className={`card border ${result.success ? 'border-green-500/30 bg-green-500/5' : 'border-danger/30 bg-danger/5'}`}
            initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}
          >
            <div className="flex items-center gap-2">
              {result.success ? (
                <CheckCircle2 className="w-5 h-5 text-green-400 flex-shrink-0" />
              ) : (
                <AlertTriangle className="w-5 h-5 text-danger flex-shrink-0" />
              )}
              <div>
                {result.success ? (
                  <div>
                    <p className="text-sm text-green-300 font-medium">Multiqueue включён</p>
                    {result.image_name && (
                      <p className="text-xs text-dark-400 mt-0.5">Образ: {result.image_name}</p>
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-danger">{result.error}</p>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// YC ACCOUNTS TAB
// ═══════════════════════════════════════════════════════════════════════

function YcAccountsTab() {
  const {
    accounts, isLoadingAccounts, isSaving, error,
    fetchAccounts, createAccount, updateAccount, deleteAccount, clearError,
    workerStatuses, accountErrors, startAccount, stopAccount, fetchWorkerStatuses, clearAccountErrors,
    caughtIps, isLoadingCaughtIps, fetchCaughtIps, clearCaughtIps, deleteCaughtIp
  } = useYcStore()

  const [showAddAccount, setShowAddAccount] = useState(false)
  const [accountForm, setAccountForm] = useState({ label: '', oauth_token: '', folder_id: '', proxy: '' })
  const [editingAccount, setEditingAccount] = useState<number | null>(null)
  const [showToken, setShowToken] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null)
  const [showIpsModal, setShowIpsModal] = useState(false)

  useEffect(() => { fetchAccounts(); fetchWorkerStatuses() }, [fetchAccounts, fetchWorkerStatuses])

  useEffect(() => {
    const interval = setInterval(fetchWorkerStatuses, 3000)
    return () => clearInterval(interval)
  }, [fetchWorkerStatuses])

  const handleAddAccount = async (e: FormEvent) => {
    e.preventDefault()
    if (!accountForm.oauth_token.trim() || !accountForm.folder_id.trim()) return
    const result = await createAccount({
      oauth_token: accountForm.oauth_token.trim(),
      folder_id: accountForm.folder_id.trim(),
      proxy: accountForm.proxy.trim() || undefined,
      label: accountForm.label.trim() || undefined,
    })
    if (result) {
      setAccountForm({ label: '', oauth_token: '', folder_id: '', proxy: '' })
      setShowAddAccount(false)
    }
  }

  const handleEditAccount = (account: { id: number; label: string; proxy: string | null }) => {
    setEditingAccount(account.id)
    setAccountForm({ label: account.label, oauth_token: '', folder_id: '', proxy: account.proxy || '' })
    setShowToken(false)
  }

  const handleSaveAccount = async (e: FormEvent, accountId: number) => {
    e.preventDefault()
    const data: { label?: string; oauth_token?: string; proxy?: string } = {
      label: accountForm.label.trim() || undefined,
      proxy: accountForm.proxy.trim() || undefined,
    }
    if (accountForm.oauth_token.trim()) data.oauth_token = accountForm.oauth_token.trim()
    const result = await updateAccount(accountId, data)
    if (result) {
      setEditingAccount(null)
      setAccountForm({ label: '', oauth_token: '', folder_id: '', proxy: '' })
    }
  }

  const cancelEdit = () => {
    setEditingAccount(null)
    setShowAddAccount(false)
    setAccountForm({ label: '', oauth_token: '', folder_id: '', proxy: '' })
  }

  const handleDeleteAccount = async (id: number) => {
    if (deleteConfirm === id) {
      await deleteAccount(id)
      setDeleteConfirm(null)
    } else {
      setDeleteConfirm(id)
      setTimeout(() => setDeleteConfirm(null), 3000)
    }
  }

  const handleShowIps = async () => {
    setShowIpsModal(true)
    await fetchCaughtIps()
  }

  if (isLoadingAccounts) {
    return <div className="flex items-center justify-center py-16"><Loader2 className="w-8 h-8 text-orange-400 animate-spin" /></div>
  }

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible">
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 mb-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm flex-1">{error}</span>
            <button onClick={clearError} className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div className="flex items-center justify-between mb-4" variants={itemVariants}>
        <div className="flex items-center gap-4">
          {accounts.length > 0 && (
            <>
              <span className="text-sm text-dark-400">
                Активно: <span className="text-orange-400 font-medium">{accounts.filter(a => a.enabled).length}</span> из {accounts.length}
              </span>
              {accounts.some(a => a.caught_ip_count > 0) && (
                <button onClick={handleShowIps}
                  className="text-xs px-2 py-1 rounded bg-success/20 text-success hover:bg-success/30 flex items-center gap-1">
                  <Target className="w-3.5 h-3.5" />
                  {accounts.reduce((sum, a) => sum + a.caught_ip_count, 0)} IP
                </button>
              )}
            </>
          )}
        </div>
        {!showAddAccount ? (
          <motion.button onClick={() => setShowAddAccount(true)} className="btn btn-primary"
            whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
            <Plus className="w-4 h-4" /> Добавить аккаунт
          </motion.button>
        ) : (
          <form onSubmit={handleAddAccount} className="bg-dark-800/50 rounded-xl p-4 w-full max-w-2xl space-y-3" autoComplete="off" data-form-type="other">
            <div className="flex items-center gap-2 mb-3">
              <User className="w-5 h-5 text-orange-400" />
              <h4 className="font-medium text-dark-100">Новый аккаунт Yandex Cloud</h4>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-dark-400 mb-1">Метка (опционально)</label>
                <input type="text" value={accountForm.label}
                  onChange={(e) => setAccountForm(f => ({ ...f, label: e.target.value }))}
                  className="input text-sm" placeholder="my-yc-account" autoComplete="off" />
              </div>
              <div>
                <label className="block text-xs text-dark-400 mb-1">Folder ID</label>
                <input type="text" value={accountForm.folder_id}
                  onChange={(e) => setAccountForm(f => ({ ...f, folder_id: e.target.value }))}
                  className="input text-sm font-mono" placeholder="b1g..." required autoComplete="off" />
              </div>
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">OAuth Token</label>
              <div className="relative">
                <SecretInput revealed={showToken} value={accountForm.oauth_token}
                  onChange={(e) => setAccountForm(f => ({ ...f, oauth_token: e.target.value }))}
                  className="input text-sm pr-10 font-mono" placeholder="y0_..." required />
                <button type="button" onClick={() => setShowToken(!showToken)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
                  {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">SOCKS5 Proxy (опционально)</label>
              <input type="text" value={accountForm.proxy}
                onChange={(e) => setAccountForm(f => ({ ...f, proxy: e.target.value }))}
                className="input text-sm" placeholder="host:port:user:pass" autoComplete="off" />
            </div>
            <div className="flex gap-2 pt-2">
              <motion.button type="submit" disabled={isSaving || !accountForm.oauth_token.trim() || !accountForm.folder_id.trim()}
                className="btn btn-primary text-sm" whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                Добавить
              </motion.button>
              <motion.button type="button" onClick={cancelEdit} className="btn btn-secondary text-sm"
                whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>Отмена</motion.button>
            </div>
          </form>
        )}
      </motion.div>

      <div className="space-y-3">
        {accounts.length === 0 ? (
          <motion.div className="card text-center py-12" variants={itemVariants}>
            <Users className="w-12 h-12 text-dark-500 mx-auto mb-3" />
            <p className="text-dark-400">Нет добавленных аккаунтов</p>
            <p className="text-dark-500 text-sm mt-1">Добавьте аккаунт Yandex Cloud чтобы начать</p>
          </motion.div>
        ) : (
          accounts.map((account) => {
            const ws = workerStatuses[account.id]
            const isRunning = ws?.status === 'running'
            const isStopping = ws?.status === 'stopping'
            const hasError = ws?.status === 'error'
            const isProxyWaiting = ws?.status === 'proxy_waiting'
            const accErrors = accountErrors.filter(e => e.account_id === account.id)
            const statusDot = isRunning ? 'bg-success animate-pulse' : isProxyWaiting ? 'bg-purple-500 animate-pulse' : hasError ? 'bg-danger animate-pulse' : 'bg-dark-500'

            return (
              <motion.div key={account.id} className={`card transition-all ${!account.enabled ? 'opacity-60 border-dark-700/30' : ''}`} variants={itemVariants}>
                <AnimatePresence>
                  {accErrors.length > 0 && (
                    <motion.div className="mb-3 space-y-1.5"
                      initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
                      {accErrors.filter(e => e.error_type === 'PROXY_DEAD').length > 0 && (
                        <div className="flex items-center gap-2 p-2.5 bg-purple-500/10 border border-purple-500/20 rounded-lg text-purple-400 text-sm">
                          <AlertCircle className="w-4 h-4 flex-shrink-0" />
                          <span className="flex-1">Прокси не работает</span>
                          <button onClick={() => clearAccountErrors(account.id)} className="p-1 hover:bg-purple-500/20 rounded">
                            <X className="w-4 h-4" />
                          </button>
                        </div>
                      )}
                      {accErrors.filter(e => !['PROXY_DEAD'].includes(e.error_type)).length > 0 && (
                        <div className="flex items-center gap-2 p-2.5 bg-danger/10 border border-danger/20 rounded-lg text-danger text-sm">
                          <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                          <span className="flex-1">{accErrors.find(e => e.error_type !== 'PROXY_DEAD')?.error_message || 'Ошибка'}</span>
                          <button onClick={() => clearAccountErrors(account.id)} className="p-1 hover:bg-danger/20 rounded">
                            <X className="w-4 h-4" />
                          </button>
                        </div>
                      )}
                    </motion.div>
                  )}
                </AnimatePresence>

                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3 flex-1">
                    <div className="relative">
                      <div className={`w-10 h-10 rounded-xl flex items-center justify-center transition-all ${
                        account.enabled
                          ? 'bg-gradient-to-br from-orange-500/20 to-orange-600/20'
                          : 'bg-dark-700/50'
                      }`}>
                        <User className={`w-5 h-5 ${account.enabled ? 'text-orange-400' : 'text-dark-500'}`} />
                      </div>
                      <span className={`absolute -top-0.5 -right-0.5 w-3 h-3 rounded-full border-2 border-dark-800 ${statusDot}`} />
                    </div>
                    <div>
                      <div className="flex items-center gap-2">
                        <h3 className={`font-medium ${account.enabled ? 'text-dark-100' : 'text-dark-400'}`}>
                          {account.label || `Account ${account.id}`}
                        </h3>
                        <span className="text-xs text-dark-500 font-mono">{account.folder_id}</span>
                        {!account.enabled && <span className="text-xs px-1.5 py-0.5 rounded bg-dark-700 text-dark-400">выкл</span>}
                        {isRunning && <span className="text-xs px-1.5 py-0.5 rounded bg-success/20 text-success flex items-center gap-1"><Zap className="w-3.5 h-3.5" />работает</span>}
                        {isProxyWaiting && <span className="text-xs px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400 flex items-center gap-1"><RefreshCw className="w-3.5 h-3.5 animate-spin" />прокси...</span>}
                        {hasError && <span className="text-xs px-1.5 py-0.5 rounded bg-danger/20 text-danger">ошибка</span>}
                        {account.proxy && <span className="text-xs px-1.5 py-0.5 rounded bg-orange-500/20 text-orange-400 flex items-center gap-1"><Globe className="w-3.5 h-3.5" />proxy</span>}
                        {account.caught_ip_count > 0 && (
                          <span className="text-xs px-1.5 py-0.5 rounded bg-success/20 text-success flex items-center gap-1">
                            <Target className="w-3.5 h-3.5" />{account.caught_ip_count} IP
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-3 text-sm text-dark-500">
                        <span className="font-mono text-xs">{account.oauth_token_masked}</span>
                        {ws && ws.status === 'running' && (
                          <span className="text-dark-400">
                            попыток: {ws.attempts} | поймано: {ws.caught}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {account.enabled && (
                      isRunning || isStopping || isProxyWaiting ? (
                        <motion.button onClick={() => stopAccount(account.id)} disabled={isStopping}
                          className="btn p-2 bg-danger/10 text-danger hover:bg-danger/20 transition-all"
                          whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }} title="Остановить">
                          {isStopping ? <Loader2 className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
                        </motion.button>
                      ) : (
                        <motion.button onClick={() => startAccount(account.id)}
                          className="btn p-2 bg-success/10 text-success hover:bg-success/20 transition-all"
                          whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }} title="Запустить">
                          <Play className="w-4 h-4" />
                        </motion.button>
                      )
                    )}
                    <motion.button
                      onClick={() => updateAccount(account.id, { enabled: !account.enabled })}
                      className={`btn p-2 transition-all ${account.enabled
                        ? 'bg-orange-500/10 text-orange-400 hover:bg-orange-500/20'
                        : 'bg-dark-700/50 text-dark-400 hover:bg-dark-700'}`}
                      whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}
                      title={account.enabled ? 'Отключить' : 'Включить'}>
                      <Power className="w-4 h-4" />
                    </motion.button>
                    <motion.button onClick={() => handleEditAccount(account)} className="btn btn-ghost p-2"
                      whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }} title="Редактировать">
                      <Edit2 className="w-4 h-4" />
                    </motion.button>
                    <motion.button onClick={() => handleDeleteAccount(account.id)}
                      className={`btn p-2 transition-all ${deleteConfirm === account.id
                        ? 'bg-danger text-white' : 'btn-ghost text-danger hover:bg-danger/10'}`}
                      whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                      <Trash2 className="w-4 h-4" />
                    </motion.button>
                  </div>
                </div>

                <AnimatePresence>
                  {editingAccount === account.id && (
                    <motion.form onSubmit={(e) => handleSaveAccount(e, account.id)}
                      className="mt-4 pt-4 border-t border-dark-700/50 space-y-3"
                      initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}
                      autoComplete="off" data-form-type="other">
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className="block text-xs text-dark-400 mb-1">Метка</label>
                          <input type="text" value={accountForm.label}
                            onChange={(e) => setAccountForm(f => ({ ...f, label: e.target.value }))}
                            className="input text-sm" placeholder="my-yc-account" autoComplete="off" />
                        </div>
                        <div>
                          <label className="block text-xs text-dark-400 mb-1">SOCKS5 Proxy</label>
                          <input type="text" value={accountForm.proxy}
                            onChange={(e) => setAccountForm(f => ({ ...f, proxy: e.target.value }))}
                            className="input text-sm" placeholder="host:port:user:pass" autoComplete="off" />
                        </div>
                      </div>
                      <div>
                        <label className="block text-xs text-dark-400 mb-1">Новый OAuth Token (оставьте пустым чтобы не менять)</label>
                        <div className="relative">
                          <SecretInput revealed={showToken} value={accountForm.oauth_token}
                            onChange={(e) => setAccountForm(f => ({ ...f, oauth_token: e.target.value }))}
                            className="input text-sm pr-10 font-mono" placeholder="y0_..." />
                          <button type="button" onClick={() => setShowToken(!showToken)}
                            className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
                            {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                          </button>
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <motion.button type="submit" disabled={isSaving} className="btn btn-primary text-sm"
                          whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                          {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                          Сохранить
                        </motion.button>
                        <motion.button type="button" onClick={cancelEdit} className="btn btn-secondary text-sm"
                          whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>Отмена</motion.button>
                      </div>
                    </motion.form>
                  )}
                </AnimatePresence>
              </motion.div>
            )
          })
        )}
      </div>

      {/* Модальное окно пойманных IP */}
      <AnimatePresence>
        {showIpsModal && (
          <motion.div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={() => setShowIpsModal(false)}>
            <motion.div className="bg-dark-800 rounded-2xl p-6 w-full max-w-2xl max-h-[80vh] overflow-hidden flex flex-col"
              initial={{ scale: 0.9, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.9, opacity: 0 }}
              onClick={(e) => e.stopPropagation()}>
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-lg font-medium text-dark-100 flex items-center gap-2">
                  <Target className="w-5 h-5 text-success" />
                  Пойманные IP (Yandex)
                </h3>
                <div className="flex items-center gap-2">
                  {caughtIps.length > 0 && (
                    <motion.button onClick={() => { clearCaughtIps(); setShowIpsModal(false) }} disabled={isSaving}
                      className="btn btn-ghost text-danger text-sm hover:bg-danger/10"
                      whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                      <Trash2 className="w-4 h-4" /> Очистить все
                    </motion.button>
                  )}
                  <motion.button onClick={() => setShowIpsModal(false)} className="btn btn-ghost p-2"
                    whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                    <X className="w-5 h-5" />
                  </motion.button>
                </div>
              </div>
              <div className="flex-1 overflow-y-auto custom-scrollbar">
                {isLoadingCaughtIps ? (
                  <div className="flex items-center justify-center py-8">
                    <Loader2 className="w-6 h-6 text-orange-400 animate-spin" />
                  </div>
                ) : caughtIps.length === 0 ? (
                  <div className="text-center py-8 text-dark-500">
                    <Target className="w-10 h-10 mx-auto mb-2 opacity-50" />
                    <p>Нет пойманных IP</p>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {caughtIps.map((ip) => (
                      <motion.div key={ip.id} className="flex items-center justify-between p-3 bg-dark-700/30 rounded-lg group"
                        initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
                        <div className="flex items-center gap-3">
                          <div className="w-8 h-8 rounded-lg bg-success/20 flex items-center justify-center">
                            <Target className="w-4 h-4 text-success" />
                          </div>
                          <div>
                            <div className="font-mono text-success font-medium">{ip.ip_address}</div>
                            <div className="flex items-center gap-2 text-xs text-dark-500">
                              <span>{ip.zone}</span>
                              <span>• {ip.account_label}</span>
                              <span>• {new Date(ip.caught_at).toLocaleString('ru-RU')}</span>
                            </div>
                          </div>
                        </div>
                        <motion.button onClick={() => deleteCaughtIp(ip.id)}
                          className="btn btn-ghost p-2 text-danger opacity-60 group-hover:opacity-100 transition-opacity"
                          whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                          <Trash2 className="w-4 h-4" />
                        </motion.button>
                      </motion.div>
                    ))}
                  </div>
                )}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// YC MANUAL TAB
// ═══════════════════════════════════════════════════════════════════════

const YC_DEFAULT_ZONES = ['ru-central1-a', 'ru-central1-b', 'ru-central1-c', 'ru-central1-d']

function YcManualTab() {
  const {
    accounts, settings,
    fetchAccounts, fetchSettings,
    manualObtainIp, manualObtainHistory, isObtaining, clearManualHistory,
    error, clearError
  } = useYcStore()

  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(null)
  const [selectedZone, setSelectedZone] = useState<string>('')
  const [useCustomZone, setUseCustomZone] = useState(false)
  const [customZone, setCustomZone] = useState('')
  const [copied, setCopied] = useState<number | null>(null)

  useEffect(() => { fetchAccounts(); fetchSettings() }, [fetchAccounts, fetchSettings])

  const zone = useCustomZone ? customZone.trim() : selectedZone
  const canObtain = selectedAccountId && zone && !isObtaining

  const handleObtain = async () => {
    if (!canObtain || !selectedAccountId) return
    const account = accounts.find(a => a.id === selectedAccountId)
    if (!account) return

    const data: YcManualObtainRequest = {
      account_id: selectedAccountId,
      zone,
    }
    await manualObtainIp(data, account.label || `Account ${account.id}`, zone)
  }

  const handleCopyIp = (ip: string, entryId: number) => {
    navigator.clipboard.writeText(ip)
    setCopied(entryId)
    setTimeout(() => setCopied(null), 2000)
  }

  const lastResult = manualObtainHistory[0]
  const availableZones = settings?.available_zones || YC_DEFAULT_ZONES

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm flex-1">{error}</span>
            <button onClick={clearError} className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Target className="w-5 h-5 text-orange-400" /> Получение IP вручную (Yandex)
        </h3>
        <div className="space-y-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Аккаунт</label>
            <select value={selectedAccountId || ''}
              onChange={e => setSelectedAccountId(e.target.value ? Number(e.target.value) : null)}
              className="input">
              <option value="">Выберите аккаунт...</option>
              {accounts.map(acc => (
                <option key={acc.id} value={acc.id}>
                  {acc.label || `Account ${acc.id}`} ({acc.folder_id}){acc.proxy ? ' (proxy)' : ''}
                </option>
              ))}
            </select>
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="block text-sm text-dark-400">Зона</label>
              <div className="flex gap-2">
                <button onClick={() => { setUseCustomZone(false); setCustomZone('') }}
                  className={`text-xs px-2 py-0.5 rounded ${!useCustomZone ? 'bg-orange-500/20 text-orange-400' : 'text-dark-400 hover:text-dark-300'}`}>
                  Из списка
                </button>
                <button onClick={() => { setUseCustomZone(true); setSelectedZone('') }}
                  className={`text-xs px-2 py-0.5 rounded ${useCustomZone ? 'bg-orange-500/20 text-orange-400' : 'text-dark-400 hover:text-dark-300'}`}>
                  Своя
                </button>
              </div>
            </div>
            {useCustomZone ? (
              <input type="text" value={customZone}
                onChange={e => setCustomZone(e.target.value)}
                className="input text-sm font-mono" placeholder="ru-central1-e" />
            ) : (
              <select value={selectedZone}
                onChange={e => setSelectedZone(e.target.value)}
                className="input">
                <option value="">Выберите зону...</option>
                {availableZones.map(z => (
                  <option key={z} value={z}>{z}</option>
                ))}
              </select>
            )}
          </div>

          <motion.button onClick={handleObtain} disabled={!canObtain}
            className={`btn w-full justify-center ${canObtain ? 'btn-primary' : 'btn-secondary opacity-50'}`}
            whileHover={canObtain ? { scale: 1.01 } : {}}
            whileTap={canObtain ? { scale: 0.99 } : {}}>
            {isObtaining ? (
              <><Loader2 className="w-5 h-5 animate-spin" /> Получение IP...</>
            ) : (
              <><Zap className="w-5 h-5" /> Получить IP</>
            )}
          </motion.button>
        </div>
      </motion.div>

      <AnimatePresence>
        {lastResult && !isObtaining && (
          <motion.div className={`card border ${lastResult.success ? 'border-success/30 bg-success/5' : 'border-danger/30 bg-danger/5'}`}
            initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }}>
            <div className="flex items-center gap-3">
              {lastResult.success ? (
                <>
                  <div className="w-10 h-10 rounded-xl bg-success/20 flex items-center justify-center flex-shrink-0">
                    <CheckCircle2 className="w-5 h-5 text-success" />
                  </div>
                  <div className="flex-1">
                    <div className="font-mono text-lg text-success font-bold">{lastResult.ipAddress}</div>
                    <div className="text-xs text-dark-400">{lastResult.zone} • {lastResult.accountLabel}</div>
                  </div>
                  <motion.button onClick={() => handleCopyIp(lastResult.ipAddress!, lastResult.id)}
                    className="btn btn-ghost p-2 text-dark-400 hover:text-success"
                    whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                    {copied === lastResult.id ? <CheckCircle2 className="w-5 h-5 text-success" /> : <Copy className="w-5 h-5" />}
                  </motion.button>
                </>
              ) : (
                <>
                  <div className="w-10 h-10 rounded-xl bg-danger/20 flex items-center justify-center flex-shrink-0">
                    <AlertCircle className="w-5 h-5 text-danger" />
                  </div>
                  <div className="flex-1">
                    <div className="text-sm text-danger font-medium">Не удалось получить IP</div>
                    <div className="text-xs text-dark-400 mt-0.5">{lastResult.error}</div>
                    <div className="text-xs text-dark-500 mt-0.5">{lastResult.zone} • {lastResult.accountLabel}</div>
                  </div>
                </>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {manualObtainHistory.length > 0 && (
        <motion.div className="card" variants={itemVariants}>
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-medium text-dark-100 flex items-center gap-2 text-sm">
              <Clock className="w-4 h-4 text-orange-400" />
              История попыток
              <span className="text-dark-500">({manualObtainHistory.length})</span>
            </h3>
            <motion.button onClick={clearManualHistory}
              className="btn btn-ghost text-xs text-dark-400 hover:text-danger p-1.5"
              whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}>
              <Trash2 className="w-3.5 h-3.5" /> Очистить
            </motion.button>
          </div>
          <div className="space-y-1.5 max-h-64 overflow-y-auto custom-scrollbar">
            {manualObtainHistory.map((entry) => (
              <div key={entry.id}
                className={`flex items-center gap-3 p-2.5 rounded-lg text-sm ${
                  entry.success ? 'bg-success/5 border border-success/10' : 'bg-danger/5 border border-danger/10'
                }`}>
                <span className={`w-2 h-2 rounded-full flex-shrink-0 ${entry.success ? 'bg-success' : 'bg-danger'}`} />
                <span className="text-dark-500 text-xs w-16 flex-shrink-0">{entry.timestamp.split(', ')[1] || entry.timestamp}</span>
                <span className="text-dark-300 truncate flex-1">{entry.accountLabel}</span>
                <span className="text-dark-500 text-xs truncate max-w-32">{entry.zone}</span>
                {entry.success ? (
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <span className="font-mono text-success text-xs">{entry.ipAddress}</span>
                    <button onClick={() => handleCopyIp(entry.ipAddress!, entry.id)}
                      className="p-1 text-dark-500 hover:text-success rounded">
                      {copied === entry.id ? <CheckCircle2 className="w-4 h-4 text-success" /> : <Copy className="w-4 h-4" />}
                    </button>
                  </div>
                ) : (
                  <span className="text-danger text-xs truncate max-w-48 flex-shrink-0" title={entry.error}>{entry.error}</span>
                )}
              </div>
            ))}
          </div>
        </motion.div>
      )}
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// YC SETTINGS TAB
// ═══════════════════════════════════════════════════════════════════════

function YcSettingsTab() {
  const { settings, isLoadingSettings, isSaving, fetchSettings, updateSettings, error, clearError } = useYcStore()
  const [formData, setFormData] = useState<any>(null)
  const [hasChanges, setHasChanges] = useState(false)
  const [showToken, setShowToken] = useState(false)
  const [showAddSubnet, setShowAddSubnet] = useState(false)
  const [newSubnetCidr, setNewSubnetCidr] = useState('')
  const [subnetSaving, setSubnetSaving] = useState(false)
  const [showAddZone, setShowAddZone] = useState(false)
  const [newZone, setNewZone] = useState('')

  useEffect(() => { fetchSettings() }, [fetchSettings])

  useEffect(() => {
    if (settings && !formData) {
      setFormData({
        delay_min: settings.delay_min,
        delay_max: settings.delay_max,
        start_delay_max: settings.start_delay_max,
        stop_on_catch: settings.stop_on_catch,
        telegram_bot_token: settings.telegram_bot_token,
        telegram_chat_id: settings.telegram_chat_id,
        target_subnets: settings.target_subnets,
        zones: settings.zones,
        proxy_required: settings.proxy_required,
        proxy_check_interval: settings.proxy_check_interval,
        log_level: settings.log_level || 'info',
      })
    }
  }, [settings, formData])

  const handleChange = (field: string, value: any) => {
    setFormData((f: any) => f ? { ...f, [field]: value } : f)
    setHasChanges(true)
  }

  const toggleSubnet = (idx: number) => {
    if (!formData) return
    const current = formData.target_subnets as number[]
    const newSubnets = current.includes(idx)
      ? current.filter((s: number) => s !== idx)
      : [...current, idx]
    handleChange('target_subnets', newSubnets)
  }

  const selectAllSubnets = () => {
    if (!settings) return
    handleChange('target_subnets', settings.available_subnets.map((_: string, i: number) => i))
  }

  const clearAllSubnets = () => handleChange('target_subnets', [])

  const toggleZone = (zone: string) => {
    if (!formData) return
    const current = formData.zones as string[]
    const newZones = current.includes(zone)
      ? current.filter((z: string) => z !== zone)
      : [...current, zone]
    handleChange('zones', newZones)
  }

  const handleAddSubnet = async () => {
    if (!newSubnetCidr.trim()) return
    setSubnetSaving(true)
    try {
      await ycApi.addSubnet(newSubnetCidr.trim())
      setNewSubnetCidr('')
      setShowAddSubnet(false)
      await fetchSettings()
      setFormData(null)
    } catch (e: any) {
      alert(e.response?.data?.detail || 'Ошибка добавления подсети')
    } finally {
      setSubnetSaving(false)
    }
  }

  const handleRemoveSubnet = async (cidr: string) => {
    if (!confirm(`Удалить подсеть ${cidr}?`)) return
    try {
      await ycApi.removeSubnet(cidr)
      await fetchSettings()
      setFormData(null)
    } catch (e: any) {
      alert(e.response?.data?.detail || 'Ошибка удаления подсети')
    }
  }

  const handleAddZone = () => {
    if (!newZone.trim() || !formData) return
    const zones = formData.zones as string[]
    if (!zones.includes(newZone.trim())) {
      handleChange('zones', [...zones, newZone.trim()])
    }
    setNewZone('')
    setShowAddZone(false)
  }

  const handleSave = async () => {
    if (!formData) return
    const success = await updateSettings(formData)
    if (success) setHasChanges(false)
  }

  if (isLoadingSettings || !formData) {
    return <div className="flex items-center justify-center py-16"><Loader2 className="w-8 h-8 text-orange-400 animate-spin" /></div>
  }

  const availableSubnets = settings?.available_subnets || []
  const availableZones = settings?.available_zones || YC_DEFAULT_ZONES

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-6">
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm flex-1">{error}</span>
            <button onClick={clearError} className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Clock className="w-5 h-5 text-orange-400" /> Задержки
        </h3>
        <div className="grid grid-cols-3 gap-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Минимальная (сек)</label>
            <input type="number" value={formData.delay_min}
              onChange={(e) => handleChange('delay_min', parseInt(e.target.value) || 0)}
              className="input" min={1} />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Максимальная (сек)</label>
            <input type="number" value={formData.delay_max}
              onChange={(e) => handleChange('delay_max', parseInt(e.target.value) || 0)}
              className="input" min={1} />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Задержка старта (сек)</label>
            <input type="number" value={formData.start_delay_max}
              onChange={(e) => handleChange('start_delay_max', parseInt(e.target.value) || 1)}
              className="input" min={1} max={300} title="Рандомная задержка старта каждого аккаунта" />
          </div>
        </div>
        <p className="text-xs text-dark-500 mt-2">Задержка старта — каждый аккаунт стартует с рандомной задержкой от 1 до указанного значения секунд</p>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Send className="w-5 h-5 text-orange-400" /> Telegram уведомления
        </h3>
        <div className="space-y-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Bot Token</label>
            <div className="relative">
              <SecretInput revealed={showToken} value={formData.telegram_bot_token}
                onChange={(e) => handleChange('telegram_bot_token', e.target.value)}
                className="input font-mono pr-10" placeholder="123456789:ABC..." />
              <button type="button" onClick={() => setShowToken(!showToken)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
                {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Chat ID</label>
            <input type="text" value={formData.telegram_chat_id}
              onChange={(e) => handleChange('telegram_chat_id', e.target.value)}
              className="input font-mono" placeholder="123456789" autoComplete="off" />
          </div>
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <ScrollText className="w-5 h-5 text-orange-400" /> Уровень логирования
        </h3>
        <div className="grid grid-cols-2 gap-3">
          <motion.button onClick={() => handleChange('log_level', 'info')}
            className={`p-4 rounded-xl border text-left transition-all ${
              formData.log_level === 'info'
                ? 'bg-orange-500/20 border-orange-500/40'
                : 'bg-dark-800/50 border-dark-700/50 hover:border-dark-600'
            }`}
            whileHover={{ scale: 1.01 }} whileTap={{ scale: 0.99 }}>
            <div className="flex items-center gap-2 mb-2">
              <div className={`w-3 h-3 rounded-full ${formData.log_level === 'info' ? 'bg-orange-400' : 'bg-dark-600'}`} />
              <span className={`font-medium ${formData.log_level === 'info' ? 'text-orange-300' : 'text-dark-300'}`}>INFO</span>
            </div>
            <p className="text-xs text-dark-400">Базовые логи: запуск, попытки, успех</p>
          </motion.button>
          <motion.button onClick={() => handleChange('log_level', 'full')}
            className={`p-4 rounded-xl border text-left transition-all ${
              formData.log_level === 'full'
                ? 'bg-orange-500/20 border-orange-500/40'
                : 'bg-dark-800/50 border-dark-700/50 hover:border-dark-600'
            }`}
            whileHover={{ scale: 1.01 }} whileTap={{ scale: 0.99 }}>
            <div className="flex items-center gap-2 mb-2">
              <div className={`w-3 h-3 rounded-full ${formData.log_level === 'full' ? 'bg-orange-400' : 'bg-dark-600'}`} />
              <span className={`font-medium ${formData.log_level === 'full' ? 'text-orange-300' : 'text-dark-300'}`}>FULL</span>
            </div>
            <p className="text-xs text-dark-400">Подробные логи: полный вывод, ответы API</p>
          </motion.button>
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-medium text-dark-100 flex items-center gap-2">
            <Network className="w-5 h-5 text-orange-400" /> Целевые подсети (CIDR)
            <span className="text-sm text-dark-500">({(formData.target_subnets as number[]).length} из {availableSubnets.length})</span>
          </h3>
          <div className="flex gap-2">
            <button onClick={() => setShowAddSubnet(v => !v)} className="btn-tool btn-tool-accent">
              <Plus className="w-4 h-4" /> Добавить
            </button>
            <button onClick={selectAllSubnets} className="btn-tool">Выбрать все</button>
            <button onClick={clearAllSubnets} className="btn-tool">Снять все</button>
          </div>
        </div>
        <AnimatePresence>
          {showAddSubnet && (
            <motion.div className="flex items-center gap-2 mb-3 p-3 bg-dark-800/50 rounded-lg border border-dark-700/50"
              initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
              <input type="text" placeholder="CIDR (напр. 84.201.128.0/18)" value={newSubnetCidr}
                onChange={e => setNewSubnetCidr(e.target.value)}
                className="flex-1 px-2 py-1 bg-dark-900/50 border border-dark-700 rounded text-sm text-dark-100 font-mono focus:border-orange-500 outline-none" />
              <button onClick={handleAddSubnet} disabled={subnetSaving || !newSubnetCidr.trim()}
                className="px-3 py-1 bg-green-500/20 hover:bg-green-500/30 text-green-400 rounded text-sm disabled:opacity-40 transition-colors">
                {subnetSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Добавить'}
              </button>
              <button onClick={() => setShowAddSubnet(false)} className="p-1.5 hover:bg-dark-700/50 rounded">
                <X className="w-4 h-4 text-dark-400" />
              </button>
            </motion.div>
          )}
        </AnimatePresence>
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2 max-h-64 overflow-y-auto custom-scrollbar">
          {availableSubnets.map((cidr, idx) => {
            const isSelected = (formData.target_subnets as number[]).includes(idx)
            return (
              <div key={idx} className="relative group">
                <motion.button onClick={() => toggleSubnet(idx)}
                  className={`flex flex-col p-2 rounded-lg text-left text-sm transition-all w-full ${isSelected
                    ? 'bg-orange-500/20 border border-orange-500/40 text-orange-300'
                    : 'bg-dark-800/50 border border-dark-700/50 text-dark-400 hover:border-dark-600'}`}
                  whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                  <span className="font-mono text-xs">{cidr}</span>
                </motion.button>
                <button onClick={(e) => { e.stopPropagation(); handleRemoveSubnet(cidr) }}
                  className="absolute -top-1 -right-1 p-0.5 bg-danger/80 hover:bg-danger text-white rounded-full opacity-0 group-hover:opacity-100 transition-opacity"
                  title="Удалить подсеть">
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
            )
          })}
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-medium text-dark-100 flex items-center gap-2">
            <Globe className="w-5 h-5 text-orange-400" /> Зоны доступности
          </h3>
          <button onClick={() => setShowAddZone(v => !v)} className="btn-tool btn-tool-accent">
            <Plus className="w-4 h-4" /> Добавить
          </button>
        </div>
        <AnimatePresence>
          {showAddZone && (
            <motion.div className="flex items-center gap-2 mb-3 p-3 bg-dark-800/50 rounded-lg border border-dark-700/50"
              initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
              <input type="text" placeholder="ru-central1-e" value={newZone}
                onChange={e => setNewZone(e.target.value)}
                className="flex-1 px-2 py-1 bg-dark-900/50 border border-dark-700 rounded text-sm text-dark-100 font-mono focus:border-orange-500 outline-none" />
              <button onClick={handleAddZone} disabled={!newZone.trim()}
                className="px-3 py-1 bg-green-500/20 hover:bg-green-500/30 text-green-400 rounded text-sm disabled:opacity-40 transition-colors">
                Добавить
              </button>
              <button onClick={() => setShowAddZone(false)} className="p-1.5 hover:bg-dark-700/50 rounded">
                <X className="w-4 h-4 text-dark-400" />
              </button>
            </motion.div>
          )}
        </AnimatePresence>
        <div className="flex flex-wrap gap-2">
          {availableZones.map((zone) => {
            const isSelected = (formData.zones as string[]).includes(zone)
            return (
              <motion.button key={zone} onClick={() => toggleZone(zone)}
                className={`px-3 py-1.5 rounded-lg text-sm font-mono transition-all ${isSelected
                  ? 'bg-orange-500/20 border border-orange-500/40 text-orange-300'
                  : 'bg-dark-800/50 border border-dark-700/50 text-dark-400 hover:border-dark-600'}`}
                whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                {zone}
              </motion.button>
            )
          })}
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Settings className="w-5 h-5 text-orange-400" /> Дополнительно
        </h3>
        <div className="space-y-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Интервал проверки proxy (сек)</label>
            <input type="number" value={formData.proxy_check_interval}
              onChange={(e) => handleChange('proxy_check_interval', parseInt(e.target.value) || 50)}
              className="input w-48" min={10} />
          </div>
          <div className="flex items-center gap-6">
            <label className="flex items-center gap-3 cursor-pointer">
              <Checkbox checked={formData.stop_on_catch}
                onChange={(e) => handleChange('stop_on_catch', e.target.checked)} />
              <span className="text-sm text-dark-300">Остановить при поимке</span>
            </label>
            <label className="flex items-center gap-3 cursor-pointer">
              <Checkbox checked={formData.proxy_required}
                onChange={(e) => handleChange('proxy_required', e.target.checked)} />
              <span className="text-sm text-dark-300">Proxy обязательно</span>
            </label>
          </div>
        </div>
      </motion.div>

      <motion.div className="flex justify-end" variants={itemVariants}>
        <motion.button onClick={handleSave} disabled={!hasChanges || isSaving}
          className={`btn ${hasChanges ? 'btn-primary' : 'btn-secondary opacity-50'}`}
          whileHover={hasChanges ? { scale: 1.02 } : {}} whileTap={hasChanges ? { scale: 0.98 } : {}}>
          {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          Сохранить настройки
        </motion.button>
      </motion.div>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// YC LOGS TAB
// ═══════════════════════════════════════════════════════════════════════

function YcLogsTab() {
  const {
    status, logs, isLoadingLogs, isSaving,
    fetchStatus, fetchLogs, clearLogs, startWorker, stopWorker,
    subscribeToLogs, unsubscribeFromLogs,
    accounts, workerStatuses, accountErrors, fetchWorkerStatuses,
    startAccount, stopAccount, clearAccountErrors
  } = useYcStore()

  const [autoScroll, setAutoScroll] = useState(() => {
    const saved = readStorage('yc-logs-autoscroll')
    return saved !== null ? saved === 'true' : true
  })
  const [dismissedErrors, setDismissedErrors] = useState<Set<number>>(new Set())
  const logsEndRef = useRef<HTMLDivElement>(null)
  const logsContainerRef = useRef<HTMLDivElement>(null)
  const isAutoScrollingRef = useRef(false)

  useEffect(() => {
    writeStorage('yc-logs-autoscroll', String(autoScroll))
  }, [autoScroll])

  useEffect(() => {
    fetchStatus()
    fetchLogs()
    fetchWorkerStatuses()
    const statusInterval = setInterval(fetchStatus, 5000)
    const workerInterval = setInterval(fetchWorkerStatuses, 3000)
    return () => { clearInterval(statusInterval); clearInterval(workerInterval); unsubscribeFromLogs() }
  }, [fetchStatus, fetchLogs, fetchWorkerStatuses, unsubscribeFromLogs])

  useEffect(() => {
    if (status.running) {
      const timeout = setTimeout(() => subscribeToLogs(), 500)
      return () => clearTimeout(timeout)
    }
  }, [status.running, subscribeToLogs])

  useEffect(() => {
    if (autoScroll && logsContainerRef.current) {
      isAutoScrollingRef.current = true
      requestAnimationFrame(() => {
        if (logsContainerRef.current) {
          logsContainerRef.current.scrollTop = logsContainerRef.current.scrollHeight
        }
        setTimeout(() => { isAutoScrollingRef.current = false }, 100)
      })
    }
  }, [logs, autoScroll])

  const handleScroll = () => {
    if (isAutoScrollingRef.current || !logsContainerRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = logsContainerRef.current
    const isNearBottom = scrollHeight - scrollTop - clientHeight < 100
    if (!isNearBottom && autoScroll) setAutoScroll(false)
  }

  const getLogColor = (log: string) => {
    if (log.includes('IP_CAUGHT') || log.includes('SUCCESS') || log.includes('Caught')) return 'text-green-400 font-bold'
    if (log.includes('PROXY_RECOVERED')) return 'text-green-400 font-medium'
    if (log.includes('PROXY_DEAD') || log.includes('PROXY_FAILED') || log.includes('PROXY_ERROR')) return 'text-purple-400'
    if (log.includes('ERROR') || log.includes('error') || log.includes('Failed') || log.includes('BLOCKED')) return 'text-red-400'
    if (log.includes('WARNING') || log.includes('warning')) return 'text-yellow-400'
    if (log.includes('FINISHED')) return 'text-dark-500'
    if (log.includes('SCHEDULED')) return 'text-blue-400/60'
    if (log.includes('START') || log.includes('Starting') || log.includes('[SYSTEM]')) return 'text-blue-400'
    if (log.includes('SUBNET_FOUND')) return 'text-cyan-400'
    if (log.includes('PROXY_OK')) return 'text-purple-300'
    return 'text-dark-300'
  }

  const visibleErrors = (() => {
    const filtered = accountErrors.filter(e => !dismissedErrors.has(e.id))
    const seen = new Map<string, number>()
    return filtered.filter(e => {
      const key = `${e.error_type}_${e.account_id}`
      if (seen.has(key)) return false
      seen.set(key, e.id)
      return true
    })
  })()
  const workerList = Object.values(workerStatuses) as Array<{ status: string; attempts: number; caught: number; error_type?: string | null }>

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      <motion.div className="flex items-center justify-between" variants={itemVariants}>
        <div className="flex items-center gap-4">
          <div className={`flex items-center gap-2 px-3 py-1.5 rounded-lg ${status.running ? 'bg-success/10 text-success' : 'bg-dark-700/50 text-dark-400'}`}>
            <span className={`w-2 h-2 rounded-full ${status.running ? 'bg-success animate-pulse' : 'bg-dark-500'}`} />
            <span className="text-sm font-medium">
              {status.running ? `Запущен (${status.active_count || workerList.filter(w => w.status === 'running').length} потоков)` :
                status.status === 'starting' ? 'Запускается...' :
                  status.status === 'stopping' ? 'Останавливается...' : 'Остановлен'}
            </span>
          </div>
          {status.running ? (
            <motion.button onClick={stopWorker} disabled={isSaving}
              className="btn bg-danger hover:bg-danger/80 text-white" whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
              {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
              Остановить все
            </motion.button>
          ) : (
            <motion.button onClick={startWorker} disabled={isSaving}
              className="btn btn-primary" whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
              {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              Запустить все
            </motion.button>
          )}
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-2 text-sm text-dark-400 cursor-pointer">
            <Checkbox checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} />
            Автоскролл
          </label>
          <motion.button onClick={() => fetchLogs()} disabled={isLoadingLogs} className="btn btn-ghost p-2"
            whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
            <RefreshCw className={`w-4 h-4 ${isLoadingLogs ? 'animate-spin' : ''}`} />
          </motion.button>
          <motion.button onClick={clearLogs} className="btn btn-ghost p-2 text-dark-400 hover:text-dark-200"
            whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
            <Trash2 className="w-4 h-4" />
          </motion.button>
        </div>
      </motion.div>

      {accounts.length > 0 && (
        <motion.div variants={itemVariants}>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {accounts.filter(a => a.enabled).map((account) => {
              const ws = workerStatuses[account.id]
              const isRunning = ws?.status === 'running'
              const hasError = ws?.status === 'error'
              const isStopping = ws?.status === 'stopping'
              const isProxyWait = ws?.status === 'proxy_waiting'

              return (
                <div key={account.id} className={`flex items-center justify-between p-2.5 rounded-lg text-sm transition-all ${
                  isRunning ? 'bg-success/5 border border-success/20' :
                  isProxyWait ? 'bg-purple-500/5 border border-purple-500/20' :
                  hasError ? 'bg-danger/5 border border-danger/20' :
                  'bg-dark-800/30 border border-dark-700/30'
                }`}>
                  <div className="flex items-center gap-2 min-w-0 flex-1">
                    <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
                      isRunning ? 'bg-success animate-pulse' : isProxyWait ? 'bg-purple-500 animate-pulse' : hasError ? 'bg-danger' : 'bg-dark-500'
                    }`} />
                    <span className="text-dark-300 truncate">{account.label || `#${account.id}`}</span>
                    {isRunning && ws && (
                      <span className="text-dark-500 text-xs flex-shrink-0">{ws.attempts}a/{ws.caught}c</span>
                    )}
                    {isProxyWait && <span className="text-xs text-purple-400 flex-shrink-0">прокси...</span>}
                    {hasError && ws?.error_type && (
                      <span className="text-xs text-danger flex-shrink-0">
                        {ws.error_type === 'PROXY_DEAD' ? 'прокси' : 'ошибка'}
                      </span>
                    )}
                  </div>
                  <div className="flex-shrink-0 ml-2">
                    {isRunning || isStopping || isProxyWait ? (
                      <motion.button onClick={() => stopAccount(account.id)} disabled={isStopping}
                        className="p-1 text-danger hover:bg-danger/10 rounded"
                        whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                        {isStopping ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Square className="w-3.5 h-3.5" />}
                      </motion.button>
                    ) : (
                      <motion.button onClick={() => startAccount(account.id)}
                        className="p-1 text-success hover:bg-success/10 rounded"
                        whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}>
                        <Play className="w-3.5 h-3.5" />
                      </motion.button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </motion.div>
      )}

      <AnimatePresence>
        {visibleErrors.length > 0 && (
          <motion.div className="space-y-2" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            {visibleErrors.map((err) => {
              const accLabel = accounts.find(a => a.id === err.account_id)?.label || `ACC ${err.account_id}`
              const isProxy = err.error_type === 'PROXY_DEAD' || err.error_type === 'PROXY_FAILED'

              return (
                <motion.div key={err.id}
                  className={`flex items-center gap-3 p-3 rounded-xl text-sm ${
                    isProxy ? 'bg-purple-500/10 border border-purple-500/20 text-purple-400' :
                    'bg-danger/10 border border-danger/20 text-danger'
                  }`}
                  initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, height: 0 }}>
                  {isProxy ? <Globe className="w-4 h-4 flex-shrink-0" /> : <AlertTriangle className="w-4 h-4 flex-shrink-0" />}
                  <span className="flex-1">
                    <strong>{accLabel}</strong>: {isProxy ? 'прокси не работает' : err.error_message || err.error_type}
                  </span>
                  <button onClick={() => {
                    setDismissedErrors(prev => new Set([...prev, err.id]))
                    clearAccountErrors(err.account_id)
                  }} className="p-1.5 hover:opacity-70 rounded">
                    <X className="w-4 h-4" />
                  </button>
                </motion.div>
              )
            })}
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div className="card p-0 overflow-hidden" variants={itemVariants}>
        <div ref={logsContainerRef} onScroll={handleScroll}
          className="h-[500px] overflow-y-auto custom-scrollbar bg-dark-900/50 font-mono text-xs">
          {logs.length === 0 ? (
            <div className="flex items-center justify-center h-full text-dark-500">
              <div className="text-center">
                <ScrollText className="w-10 h-10 mx-auto mb-2 opacity-50" />
                <p>Нет логов</p>
                <p className="text-xs mt-1">Запустите аккаунты для просмотра логов</p>
              </div>
            </div>
          ) : (
            <div className="p-3 space-y-0.5">
              {logs.map((log, index) => (
                <div key={index} className={`${getLogColor(log)} leading-5`}>{log}</div>
              ))}
              <div ref={logsEndRef} />
            </div>
          )}
        </div>
      </motion.div>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// YC PAGE (container with tabs)
// ═══════════════════════════════════════════════════════════════════════

type YcTabType = 'accounts' | 'manual' | 'stats' | 'settings' | 'logs'

function YcPage() {
  const [activeTab, setActiveTab] = useState<YcTabType>('accounts')

  const tabs: { id: YcTabType; label: string; icon: React.ReactNode }[] = [
    { id: 'accounts', label: 'Аккаунты', icon: <Users className="w-4 h-4" /> },
    { id: 'manual', label: 'Ручной IP', icon: <Target className="w-4 h-4" /> },
    { id: 'stats', label: 'Статистика', icon: <BarChart3 className="w-4 h-4" /> },
    { id: 'settings', label: 'Настройки', icon: <Settings className="w-4 h-4" /> },
    { id: 'logs', label: 'Логи', icon: <ScrollText className="w-4 h-4" /> },
  ]

  return (
    <>
      <motion.div className="flex gap-1 mb-6 bg-dark-800/50 p-1 rounded-xl w-fit" variants={itemVariants}>
        {tabs.map((tab) => (
          <motion.button key={tab.id} onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${activeTab === tab.id
              ? 'bg-orange-500/20 text-orange-400'
              : 'text-dark-400 hover:text-dark-200 hover:bg-dark-700/50'}`}
            whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
            {tab.icon} {tab.label}
          </motion.button>
        ))}
      </motion.div>

      <AnimatePresence mode="wait">
        <motion.div key={activeTab} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }} transition={{ duration: 0.15 }}>
          {activeTab === 'accounts' && <YcAccountsTab />}
          {activeTab === 'manual' && <YcManualTab />}
          {activeTab === 'stats' && <YcStatsTab />}
          {activeTab === 'settings' && <YcSettingsTab />}
          {activeTab === 'logs' && <YcLogsTab />}
        </motion.div>
      </AnimatePresence>
    </>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// CLOUD.RU — HELP (Evolution / Advanced)
// Справка живёт здесь, а не в публичном data/faq: вне зашифрованного модуля
// её текст раскрыл бы, как устроен парсер.
// ═══════════════════════════════════════════════════════════════════════

type CloudHelpArticle = 'overview' | 'settings' | 'stats'

// Вкладки Cloud.ru общие для обеих редакций, различия собраны здесь
interface CloudEditionConfig {
  title: string
  labelPrefix: string
  useStore: CloudStoreHook
  api: CloudApi
  // Выбор показывается, только когда зон больше одной: у Advanced регион один
  zones: string[]
  formatZone: (zone: string) => string
  // Запрос конкретного IP: поле в «Ручном» и режим «точечный» у воркера (только Advanced)
  supportsSpecificIp: boolean
  // Временный ключ: третье поле STS-токена в форме аккаунта (только Advanced)
  usesStsToken: boolean
  keyIdPlaceholder: string
  keySecretPlaceholder: string
  CredentialsHelp: () => JSX.Element
  help: Record<CloudHelpArticle, string>
}

const CLOUD_EVO_HELP: Record<CloudHelpArticle, string> = {
  overview: `# Cloud.ru Evolution

Парсер ловит floating IP из нужных подсетей: создаёт адреса в проекте Cloud.ru, оставляет те, что попали в whitelist, остальные удаляет и пробует снова. Каждый аккаунт работает отдельным процессом со своими раундами и счётчиками.

## Как проходит раунд

1. **Набор пачки.** Воркер создаёт адреса по одному, пока облако не ответит ошибкой квоты. Зоны чередуются по кругу, каждый раунд начинается с первой выбранной. После каждого создания — пауза Burst.
2. **Ожидание адресов.** Воркер по очереди ждёт, пока каждому созданному адресу выдадут IP и он выйдет из занятого статуса. Ждёт не дольше «Таймаута ожидания», спрашивая статус раз в «Шаг опроса».
3. **Сортировка.** Адрес из whitelist остаётся в проекте, попадает в «Пойманные IP» и в Telegram. Остальные удаляются, перед каждым удалением — пауза Delete.
4. **Пауза Round** — и следующий раунд.

Все адреса, полученные за раунд, пойманные и удалённые, попадают во вкладку «Статистика».

## Полезно знать

- Адреса, которые были в проекте до запуска, и адреса, привязанные к ВМ, воркер не удаляет никогда.
- Ошибкой квоты считается ответ, в котором есть quota, limit, exceeded, too many или 429. Пять других ошибок создания подряд закрывают пачку раньше.
- Адрес, которому за таймаут так и не выдали IP, удаляется как промах.
- Задержки, зоны и whitelist воркер получает при запуске. После изменения перезапустите аккаунт: Stop, затем Start.
- Упавший воркер панель перезапускает сама: проверяет раз в 30 секунд, перезапускает до 10 раз подряд, счётчик обнуляется после 5 минут нормальной работы. Не перезапускает при неверных ключах (AUTH_FAILED), обязательном прокси, которого нет у аккаунта (NO_PROXY), и внутренней ошибке воркера (FATAL_CRASH).
- «Ручной IP» берёт один адрес вне раундов: ждёт его до 90 секунд, спрашивая статус раз в 2 секунды, и ничего не удаляет. Задержки из настроек к нему не применяются, в статистику он не попадает.`,

  settings: `# Задержки и ожидание

Все значения — в секундах. Задержки действуют только на воркеры аккаунтов, «Ручной IP» их не использует. Новые значения воркер получит после перезапуска аккаунта.

## Где что применяется

| Параметр | Где применяется | По умолчанию |
|---|---|---|
| **Burst** | Пауза после каждого созданного адреса и после ошибки создания. Ошибка квоты закрывает пачку сразу, без паузы. Меньше Burst — пачка набирается быстрее, но выше шанс упереться в ограничение частоты запросов | 0,5 |
| **Round** | Пауза между раундами, после удаления промахов. Если в раунде не создалось ни одного адреса, например квота уже занята пойманными, перед повтором — тоже Round | 8 |
| **Delete** | Пауза перед удалением каждого промаха. Если облако не приняло удаление, та же пауза перед повтором, всего до 5 попыток | 3 |
| **Jitter** | Случайная добавка от −Jitter до +Jitter к каждой паузе Burst, Round и Delete, чтобы запросы не шли ровным метрономом. Пауза не бывает меньше нуля | 2 |
| **Таймаут ожидания** | Сколько ждать каждый новый адрес, пока ему выдадут IP. Тот же предел действует перед удалением: воркер ждёт, пока адрес выйдет из занятого статуса | 60 |
| **Шаг опроса статуса** | Как часто спрашивать статус адреса в обоих ожиданиях. Jitter на него не действует | 2 |

Длительность раунда примерно такая: квота × Burst + ожидание адресов + промахи × Delete + Round, плюс время самих запросов к облаку.

## Остальные настройки

- **Зоны доступности** — где создавать адреса. Внутри пачки они чередуются по кругу.
- **Whitelist CIDR** — подсети, которые считаются попаданием. Пустой список — попаданий нет, воркер удаляет всё.
- **Telegram** — по сообщению на каждый пойманный адрес.
- **Proxy обязательно** — аккаунт без прокси не запустится и остановится с ошибкой NO_PROXY.`,

  stats: `# Статистика

Всё, что облако выдавало воркерам: каждый адрес, пойманный или удалённый, с числом повторов. Статистика копится, пока работают аккаунты, и обновляется на странице раз в 10 секунд.

## Что показывает

- **Получено** — сколько раз облако выдало адрес. Один и тот же IP, выданный трижды, даёт 3.
- **Уникальных IP** — сколько среди них разных адресов.
- **Поймано** — сколько разных адресов попало в whitelist и осталось в проекте.
- **Зоны** — те же числа по каждой зоне и её доля во всех выдачах. Клик по зоне оставляет в списке только её подсети.
- **Подсети /24** — куда попадали выданные адреса, по убыванию числа выдач и с разбивкой по зонам. Пометка whitelist значит, что подсеть пересекается с вашим whitelist. Клик по строке раскрывает сами адреса.

## Полезно знать

- Адреса, которым за таймаут не выдали IP, и адреса из «Ручного IP» в статистику не попадают.
- «Сбросить» очищает статистику выбранного аккаунта, а если выбраны все аккаунты, то всю. Пойманные IP при этом остаются. Удалённый аккаунт уносит свою статистику с собой.
- Пометка «поймано» у адреса ставится в момент выдачи и после правки whitelist не пересчитывается. Пометка whitelist у подсети, наоборот, всегда считается по текущему списку.`,
}

const CLOUD_ADV_HELP: Record<CloudHelpArticle, string> = {
  overview: `# Cloud.ru Advanced

Парсер ловит публичные IP (EIP) из нужных подсетей в проекте Cloud.ru Advanced. Каждый аккаунт работает отдельным процессом со своими раундами и счётчиками. Регион у Advanced один — ru-moscow-1, поэтому зоны выбирать не нужно.

## Два режима (в «Настройках»)

- **Случайный.** Воркер создаёт адреса, какие выдаёт облако, оставляет попавшие в whitelist, остальные удаляет и пробует снова. Подходит, когда нужен любой адрес из широкой подсети.
- **Точечный.** Воркер обходит адреса whitelist-подсетей по одному и просит у облака каждый конкретный адрес напрямую. Занятый другим аккаунтом адрес облако не отдаст — воркер его пропускает и идёт дальше. Так не тратится квота на лишние адреса и не приходится ничего удалять. Подходит, когда нужен конкретный адрес или узкий список.

На вкладке «Ручной IP» то же самое вручную: поле «Конкретный IP» пустое — любой свободный адрес, заполнено — именно этот.

## Как проходит раунд (случайный режим)

1. **Набор пачки.** Воркер создаёт адреса по одному, пока облако не ответит ошибкой квоты. После каждого создания — пауза Burst.
2. **Ожидание адресов.** IP приходит сразу, но несколько секунд адрес ещё выделяется. Воркер ждёт, пока каждый адрес выйдет из этого статуса, не дольше «Таймаута ожидания», спрашивая статус раз в «Шаг опроса».
3. **Сортировка.** Адрес из whitelist остаётся в проекте, попадает в «Пойманные IP» и в Telegram. Остальные удаляются, перед каждым удалением — пауза Delete.
4. **Пауза Round** — и следующий раунд.

В точечном режиме пачки нет: воркер запрашивает адреса whitelist по одному, успешно взятый сразу оставляет, между запросами — пауза Burst. Все полученные адреса в обоих режимах попадают во вкладку «Статистика».

## Полезно знать

- Квота на EIP по умолчанию — 10 адресов, поднимает её только поддержка Cloud.ru. За раунд проверяется не больше квоты за вычетом уже занятых адресов.
- Освобождённый адрес Cloud.ru в течение суток выдаёт тому же аккаунту в первую очередь, поэтому удалённые промахи могут возвращаться. Насколько часто, видно в «Статистике» по разнице «Получено» и «Уникальных IP».
- Адрес создаётся с выделенной полосой 1 Мбит/с и оплатой по трафику: пока он не привязан к серверу, платится только почасовая аренда.
- Адреса, которые были в проекте до запуска, и адреса, привязанные к серверу, воркер не удаляет никогда.
- Ошибкой квоты считается ответ, в котором есть quota, limit, exceeded, too many или 429. Пять других ошибок создания подряд закрывают пачку раньше.
- Ключ проверяется первым же запросом. Неверный AK/SK или нет прав на EIP — аккаунт останавливается с AUTH_FAILED и сам не перезапускается.
- В подпись запроса входит время: облако отклоняет запросы, если часы сервера панели ушли больше чем на 15 минут.
- Задержки и whitelist воркер получает при запуске. После изменения перезапустите аккаунт: Stop, затем Start.
- Упавший воркер панель перезапускает сама: проверяет раз в 30 секунд, перезапускает до 10 раз подряд, счётчик обнуляется после 5 минут нормальной работы. Не перезапускает при неверном ключе (AUTH_FAILED), обязательном прокси, которого нет у аккаунта (NO_PROXY), и внутренней ошибке воркера (FATAL_CRASH).
- «Ручной IP» берёт один адрес вне раундов: ждёт выделения до 90 секунд, спрашивая статус раз в 2 секунды, и ничего не удаляет. Задержки из настроек к нему не применяются, в статистику он не попадает.`,

  settings: `# Задержки и ожидание

Все значения — в секундах. Задержки действуют только на воркеры аккаунтов, «Ручной IP» их не использует. Новые значения воркер получит после перезапуска аккаунта.

## Где что применяется

| Параметр | Где применяется | По умолчанию |
|---|---|---|
| **Burst** | Пауза после каждого созданного адреса и после ошибки создания. Ошибка квоты закрывает пачку сразу, без паузы | 0,5 |
| **Round** | Пауза между раундами, после удаления промахов. Если в раунде не создалось ни одного адреса, например квота уже занята пойманными, перед повтором — тоже Round | 8 |
| **Delete** | Пауза перед удалением каждого промаха. Если облако не приняло удаление, та же пауза перед повтором, всего до 5 попыток | 3 |
| **Jitter** | Случайная добавка от −Jitter до +Jitter к каждой паузе Burst, Round и Delete, чтобы запросы не шли ровным метрономом. Пауза не бывает меньше нуля | 2 |
| **Таймаут ожидания** | Сколько ждать, пока новый адрес выйдет из выделения. Тот же предел действует перед удалением: воркер ждёт, пока адрес выйдет из занятого статуса | 60 |
| **Шаг опроса статуса** | Как часто спрашивать статус адреса в обоих ожиданиях. Jitter на него не действует | 2 |

Длительность раунда примерно такая: квота × Burst + ожидание адресов + промахи × Delete + Round, плюс время самих запросов к облаку.

## Остальные настройки

- **Режим перебора** — «Случайный» (брать что выдаст облако и отбирать по whitelist) или «Точечный» (запрашивать адреса whitelist напрямую, по одному). В точечном режиме задержка Delete почти не используется: удалять нечего, кроме редкого случая, когда облако проигнорировало запрошенный адрес.
- **Whitelist** — подсети (CIDR) и/или отдельные IP, которые считаются попаданием. Можно вставить пачку строк разом — одним запросом. В точечном режиме это ещё и список адресов для перебора: укажите конкретные IP, чтобы пробовать именно их, а не проходить всю подсеть. Пустой список — воркеру нечего делать. «Очистить всё» убирает весь список одной кнопкой.
- **Telegram** — по сообщению на каждый пойманный адрес.
- **Proxy обязательно** — аккаунт без прокси не запустится и остановится с ошибкой NO_PROXY.`,

  stats: `# Статистика

Всё, что облако выдавало воркерам: каждый адрес, пойманный или удалённый, с числом повторов. Статистика копится, пока работают аккаунты, и обновляется на странице раз в 10 секунд.

## Что показывает

- **Получено** — сколько раз облако выдало адрес. Один и тот же IP, выданный трижды, даёт 3.
- **Уникальных IP** — сколько среди них разных адресов. Cloud.ru в течение суток возвращает освобождённые адреса тому же аккаунту, поэтому повторы здесь — обычное дело.
- **Поймано** — сколько разных адресов попало в whitelist и осталось в проекте.
- **Регион** — у Advanced он один, ru-moscow-1.
- **Подсети /24** — куда попадали выданные адреса, по убыванию числа выдач. Пометка whitelist значит, что подсеть пересекается с вашим whitelist. Клик по строке раскрывает сами адреса.

## Полезно знать

- Адреса из «Ручного IP» в статистику не попадают.
- «Сбросить» очищает статистику выбранного аккаунта, а если выбраны все аккаунты, то всю. Пойманные IP при этом остаются. Удалённый аккаунт уносит свою статистику с собой.
- Пометка «поймано» у адреса ставится в момент выдачи и после правки whitelist не пересчитывается. Пометка whitelist у подсети, наоборот, всегда считается по текущему списку.`,
}

function CloudHelpButton({ onClick, size = 'md' }: { onClick: () => void; size?: 'sm' | 'md' }) {
  const sizeClasses = size === 'sm' ? 'p-1 [&_svg]:w-3.5 [&_svg]:h-3.5' : 'p-1.5 [&_svg]:w-4 [&_svg]:h-4'
  return (
    <Tooltip label="Справка по разделу" position="top">
      <motion.button type="button" onClick={onClick} aria-label="Справка по разделу"
        className={`${sizeClasses} rounded-lg text-amber-300/70 hover:text-amber-200 hover:bg-amber-500/10
                    border border-transparent hover:border-amber-500/20 transition-colors`}
        whileHover={{ scale: 1.08 }} whileTap={{ scale: 0.92 }}>
        <HelpCircle />
      </motion.button>
    </Tooltip>
  )
}

function HelpDrawer({ open, subtitle, onClose, children }: {
  open: boolean
  subtitle: string
  onClose: () => void
  children: React.ReactNode
}) {
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open, onClose])

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-[65]" onClick={onClose}
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }} />
          <motion.div
            className="fixed right-0 top-0 bottom-0 z-[65] w-full max-w-2xl bg-dark-900/95 backdrop-blur-xl
                       border-l border-dark-800/60 flex flex-col shadow-2xl"
            initial={{ x: '100%' }} animate={{ x: 0 }} exit={{ x: '100%' }}
            transition={{ type: 'spring', damping: 26, stiffness: 280 }}>
            <div className="flex items-center justify-between px-6 py-4 border-b border-dark-800/60">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-amber-500/25 to-orange-500/20
                                flex items-center justify-center border border-amber-500/30 shadow-lg shadow-amber-500/10">
                  <HelpCircle className="w-5 h-5 text-amber-300" />
                </div>
                <div>
                  <h2 className="text-lg font-semibold text-dark-50">Справка</h2>
                  <p className="text-xs text-dark-500">{subtitle}</p>
                </div>
              </div>
              <motion.button onClick={onClose} aria-label="Закрыть"
                className="p-2 rounded-lg hover:bg-dark-800 text-dark-400 hover:text-dark-100 transition-colors"
                whileHover={{ scale: 1.08 }} whileTap={{ scale: 0.92 }}>
                <X className="w-5 h-5" />
              </motion.button>
            </div>
            <div className="flex-1 overflow-y-auto px-6 py-6">
              {children}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// CLOUD.RU — ACCOUNTS TAB
// ═══════════════════════════════════════════════════════════════════════

function CloudEvoCredentialsHelp() {
  return (
    <details className="group rounded-lg bg-dark-800/40 border border-dark-700/50 text-sm">
      <summary className="flex items-center gap-2 px-3 py-2 cursor-pointer select-none text-violet-300 hover:text-violet-200 list-none">
        <ChevronRight className="w-4 h-4 transition-transform group-open:rotate-90" />
        Где взять данные?
      </summary>
      <div className="px-3 pb-3 space-y-2 text-dark-300">
        <p>
          <span className="text-dark-100 font-medium">keyId и secret</span> — ключ доступа сервисного аккаунта.
          В консоли Evolution: «Пользователи» → вкладка «Сервисные аккаунты» → «Создать», роль «Администратор проекта».
          Откройте созданный аккаунт → вкладка «Ключи доступа» → «Создать». Key ID вставьте в keyId, Key Secret — в secret.
        </p>
        <p>
          <span className="text-dark-100 font-medium">project_id</span> — идентификатор проекта. Откройте нужный проект
          в console.cloud.ru и скопируйте UUID из адресной строки: <span className="font-mono text-dark-200">console.cloud.ru/projects/&lt;project_id&gt;/…</span>
        </p>
        <p className="text-dark-400">
          Key Secret сохраните сразу после создания ключа. У ключа есть срок жизни: когда он истечёт,
          создайте новый и обновите secret в аккаунте.
        </p>
      </div>
    </details>
  )
}

function CloudAdvCredentialsHelp() {
  return (
    <details className="group rounded-lg bg-dark-800/40 border border-dark-700/50 text-sm">
      <summary className="flex items-center gap-2 px-3 py-2 cursor-pointer select-none text-violet-300 hover:text-violet-200 list-none">
        <ChevronRight className="w-4 h-4 transition-transform group-open:rotate-90" />
        Где взять данные?
      </summary>
      <div className="px-3 pb-3 space-y-2 text-dark-300">
        <p>
          <span className="text-dark-100 font-medium">AK, SK и STS Token</span> — в консоли: имя пользователя в правом
          верхнем углу → «My Credentials» → «Temporary Access Key». Выберите в «Validity» максимальный срок (переключите
          «minutes» на дни/часы) и нажмите «Generate». Скопируйте <span className="font-mono text-dark-200">AK</span>,
          <span className="font-mono text-dark-200"> SK</span> и <span className="font-mono text-dark-200">STS Token</span> в
          соответствующие поля. Постоянный ключ (без STS Token) тоже подойдёт, если он доступен в «API Credentials».
        </p>
        <p>
          <span className="text-dark-100 font-medium">project_id</span> — там же, вкладка «API Credentials»: в таблице
          проектов скопируйте Project ID проекта <span className="font-mono text-dark-200">{CLOUD_ADV_REGION}</span>.
        </p>
        <p className="text-dark-400">
          Временный ключ живёт ограниченный срок: когда он истечёт, воркер остановится с ошибкой авторизации — сгенерируйте
          новый и обновите AK/SK/STS Token в аккаунте. Ставьте максимально доступный срок.
        </p>
      </div>
    </details>
  )
}

function CloudAccountsTab({ edition }: { edition: CloudEditionConfig }) {
  const {
    accounts, caughtIps, isLoadingAccounts, isLoadingCaughtIps, isSaving, error,
    fetchAccounts, createAccount, updateAccount, deleteAccount, clearError,
    fetchCaughtIps, clearCaughtIps, deleteCaughtIp, clearAccountCaughtIps,
    workerStatuses, accountErrors, startAccount, stopAccount, fetchWorkerStatuses, clearAccountErrors,
  } = edition.useStore()
  const { CredentialsHelp } = edition

  const [showAddAccount, setShowAddAccount] = useState(false)
  const [accountForm, setAccountForm] = useState({ key_id: '', key_secret: '', project_id: '', label: '', proxy: '', key_token: '' })
  const [editingAccount, setEditingAccount] = useState<number | null>(null)
  const [showSecret, setShowSecret] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null)
  const [showIpsModal, setShowIpsModal] = useState<{ type: 'account' | 'all'; id?: number; name?: string } | null>(null)

  useEffect(() => { fetchAccounts(); fetchWorkerStatuses() }, [fetchAccounts, fetchWorkerStatuses])

  useEffect(() => {
    const interval = setInterval(fetchWorkerStatuses, 3000)
    return () => clearInterval(interval)
  }, [fetchWorkerStatuses])

  const handleAddAccount = async (e: FormEvent) => {
    e.preventDefault()
    if (!accountForm.key_id.trim() || !accountForm.key_secret.trim() || !accountForm.project_id.trim()) return
    const result = await createAccount({
      key_id: accountForm.key_id.trim(),
      key_secret: accountForm.key_secret,
      project_id: accountForm.project_id.trim(),
      label: accountForm.label.trim() || undefined,
      proxy: accountForm.proxy.trim() || undefined,
      ...(edition.usesStsToken ? { key_token: accountForm.key_token.trim() } : {}),
    })
    if (result) {
      setAccountForm({ key_id: '', key_secret: '', project_id: '', label: '', proxy: '', key_token: '' })
      setShowAddAccount(false)
    }
  }

  const handleStartEdit = (acc: typeof accounts[number]) => {
    setEditingAccount(acc.id)
    setAccountForm({
      key_id: acc.key_id,
      key_secret: '',
      project_id: acc.project_id,
      label: acc.label,
      proxy: acc.proxy || '',
      key_token: '',
    })
    setShowSecret(false)
  }

  const handleSaveEdit = async (e: FormEvent, id: number) => {
    e.preventDefault()
    const patch: Record<string, string | undefined> = {
      key_id: accountForm.key_id.trim(),
      project_id: accountForm.project_id.trim(),
      label: accountForm.label.trim() || undefined,
      proxy: accountForm.proxy.trim() || undefined,
    }
    if (accountForm.key_secret) patch.key_secret = accountForm.key_secret
    if (edition.usesStsToken && accountForm.key_token.trim()) patch.key_token = accountForm.key_token.trim()
    const result = await updateAccount(id, patch)
    if (result) {
      setEditingAccount(null)
      setAccountForm({ key_id: '', key_secret: '', project_id: '', label: '', proxy: '', key_token: '' })
    }
  }

  const openIpsModal = (type: 'account' | 'all', id?: number, name?: string) => {
    setShowIpsModal({ type, id, name })
    fetchCaughtIps()
  }

  const visibleIps = showIpsModal?.type === 'all'
    ? caughtIps
    : caughtIps.filter(ip => ip.account_id === showIpsModal?.id)

  const visibleIpsByZone = Object.entries(
    visibleIps.reduce<Record<string, number>>((acc, ip) => {
      acc[ip.zone] = (acc[ip.zone] || 0) + 1
      return acc
    }, {})
  ).sort(([a], [b]) => a.localeCompare(b))

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm flex-1">{error}</span>
            <button onClick={clearError} className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div className="flex items-center justify-between" variants={itemVariants}>
        <div className="flex items-center gap-3">
          <button onClick={() => openIpsModal('all', undefined, 'Все аккаунты')}
            className="px-3 py-1.5 rounded-lg bg-dark-800/50 hover:bg-dark-700/50 text-sm text-dark-200 flex items-center gap-2 border border-dark-700/50">
            <Eye className="w-4 h-4" /> Все пойманные IP
          </button>
        </div>
        <motion.button onClick={() => setShowAddAccount(v => !v)}
          className="btn btn-primary flex items-center gap-2"
          whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
          <Plus className="w-4 h-4" /> Добавить аккаунт
        </motion.button>
      </motion.div>

      <AnimatePresence>
        {showAddAccount && (
          <motion.form onSubmit={handleAddAccount} className="card space-y-3"
            initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
            <h4 className="font-medium text-dark-100">Новый аккаунт {edition.title}</h4>
            <CredentialsHelp />
            <div className="grid grid-cols-2 gap-3">
              <input type="text" placeholder="Метка (необязательно)" value={accountForm.label}
                onChange={e => setAccountForm(f => ({ ...f, label: e.target.value }))}
                className="input" autoComplete="off" />
              <input type="text" placeholder="project_id" value={accountForm.project_id}
                onChange={e => setAccountForm(f => ({ ...f, project_id: e.target.value }))}
                className="input font-mono" autoComplete="off" required />
            </div>
            <input type="text" placeholder={edition.keyIdPlaceholder} value={accountForm.key_id}
              onChange={e => setAccountForm(f => ({ ...f, key_id: e.target.value }))}
              className="input font-mono" autoComplete="off" required />
            <div className="relative">
              <SecretInput revealed={showSecret} placeholder={edition.keySecretPlaceholder} value={accountForm.key_secret}
                onChange={e => setAccountForm(f => ({ ...f, key_secret: e.target.value }))}
                className="input font-mono pr-10" required />
              <button type="button" onClick={() => setShowSecret(s => !s)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
                {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
            {edition.usesStsToken && (
              <SecretInput revealed={showSecret} placeholder="STS Token (временный ключ)" value={accountForm.key_token}
                onChange={e => setAccountForm(f => ({ ...f, key_token: e.target.value }))}
                className="input font-mono" required />
            )}
            <input type="text" placeholder="proxy (socks5://user:pass@host:port, необязательно)" value={accountForm.proxy}
              onChange={e => setAccountForm(f => ({ ...f, proxy: e.target.value }))}
              className="input font-mono" autoComplete="off" />
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setShowAddAccount(false)} className="btn btn-secondary">Отмена</button>
              <button type="submit" disabled={isSaving} className="btn btn-primary flex items-center gap-2">
                {isSaving && <Loader2 className="w-4 h-4 animate-spin" />} Создать
              </button>
            </div>
          </motion.form>
        )}
      </AnimatePresence>

      {isLoadingAccounts ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 text-violet-400 animate-spin" />
        </div>
      ) : accounts.length === 0 ? (
        <div className="card text-center py-12">
          <Wallet className="w-12 h-12 text-dark-500 mx-auto mb-3" />
          <p className="text-dark-300 text-sm">Аккаунтов пока нет</p>
          <p className="text-dark-500 text-sm mt-1">Добавьте аккаунт Cloud.ru чтобы начать</p>
        </div>
      ) : (
        <div className="space-y-2">
          {accounts.map(acc => {
            const status = workerStatuses[acc.id]
            const isRunning = status && (status.status === 'running' || status.status === 'proxy_waiting')
            const hasError = accountErrors.some(e => e.account_id === acc.id && !e.resolved)
            const isEditing = editingAccount === acc.id
            return (
              <motion.div key={acc.id} className="card"
                initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
                {isEditing ? (
                  <form onSubmit={(e) => handleSaveEdit(e, acc.id)} className="space-y-3">
                    <div className="grid grid-cols-2 gap-3">
                      <input type="text" placeholder="Метка" value={accountForm.label}
                        onChange={e => setAccountForm(f => ({ ...f, label: e.target.value }))}
                        className="input" autoComplete="off" />
                      <input type="text" placeholder="project_id" value={accountForm.project_id}
                        onChange={e => setAccountForm(f => ({ ...f, project_id: e.target.value }))}
                        className="input font-mono" autoComplete="off" required />
                    </div>
                    <input type="text" placeholder={edition.keyIdPlaceholder} value={accountForm.key_id}
                      onChange={e => setAccountForm(f => ({ ...f, key_id: e.target.value }))}
                      className="input font-mono" autoComplete="off" required />
                    <div className="relative">
                      <SecretInput revealed={showSecret}
                        placeholder={`${edition.keySecretPlaceholder} (пусто — оставить прежний)`} value={accountForm.key_secret}
                        onChange={e => setAccountForm(f => ({ ...f, key_secret: e.target.value }))}
                        className="input font-mono pr-10" />
                      <button type="button" onClick={() => setShowSecret(s => !s)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
                        {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    {edition.usesStsToken && (
                      <SecretInput revealed={showSecret}
                        placeholder="STS Token (пусто — оставить прежний)" value={accountForm.key_token}
                        onChange={e => setAccountForm(f => ({ ...f, key_token: e.target.value }))}
                        className="input font-mono" />
                    )}
                    <input type="text" placeholder="proxy" value={accountForm.proxy}
                      onChange={e => setAccountForm(f => ({ ...f, proxy: e.target.value }))}
                      className="input font-mono" autoComplete="off" />
                    <div className="flex justify-end gap-2">
                      <button type="button" onClick={() => { setEditingAccount(null); setAccountForm({ key_id: '', key_secret: '', project_id: '', label: '', proxy: '', key_token: '' }) }}
                        className="btn btn-secondary">Отмена</button>
                      <button type="submit" disabled={isSaving} className="btn btn-primary flex items-center gap-2">
                        {isSaving && <Loader2 className="w-4 h-4 animate-spin" />} Сохранить
                      </button>
                    </div>
                  </form>
                ) : (
                  <div className="flex items-center justify-between gap-3 flex-wrap">
                    <div className="flex items-center gap-3 min-w-0">
                      <div className={`w-2.5 h-2.5 rounded-full ${
                        isRunning ? (hasError ? 'bg-amber-400' : 'bg-violet-400 animate-pulse')
                                  : (acc.enabled ? 'bg-dark-500' : 'bg-dark-700')
                      }`} />
                      <div className="min-w-0">
                        <div className="font-medium text-dark-100 truncate">
                          {acc.label || `${edition.labelPrefix}-${acc.id}`}
                          <span className="ml-2 text-xs text-dark-500 font-mono">{acc.project_id}</span>
                        </div>
                        <div className="text-xs text-dark-500 font-mono truncate">
                          key={acc.key_id.slice(0, 12)}…
                          {acc.proxy && <span className="ml-2 text-violet-400/70">proxy</span>}
                        </div>
                        {hasError && (
                          <div className="text-xs text-amber-400 mt-1 flex items-center gap-1">
                            <AlertCircle className="w-3.5 h-3.5" />
                            {accountErrors.find(e => e.account_id === acc.id && !e.resolved)?.error_type}
                            <button onClick={() => clearAccountErrors(acc.id)} className="ml-1 hover:text-amber-300">
                              <X className="w-4 h-4" />
                            </button>
                          </div>
                        )}
                        {status && (
                          <div className="text-xs text-dark-500 mt-0.5">
                            Раунд {status.current_round || 0} · попыток {status.attempts} · поймано {status.caught}
                          </div>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      <button onClick={() => openIpsModal('account', acc.id, acc.label || `${edition.labelPrefix}-${acc.id}`)}
                        className="px-2.5 py-1 rounded-lg bg-dark-800/50 hover:bg-dark-700/50 text-xs text-dark-200 flex items-center gap-1">
                        <Eye className="w-3.5 h-3.5" /> {acc.caught_ip_count}
                      </button>
                      {isRunning ? (
                        <button onClick={() => stopAccount(acc.id)} disabled={isSaving}
                          className="px-3 py-1.5 rounded-lg bg-dark-800 text-dark-200 hover:bg-dark-700 flex items-center gap-1 text-sm">
                          <Square className="w-3.5 h-3.5" /> Stop
                        </button>
                      ) : (
                        <button onClick={() => startAccount(acc.id)} disabled={isSaving || !acc.enabled}
                          className="px-3 py-1.5 rounded-lg bg-violet-500/20 text-violet-300 hover:bg-violet-500/30 disabled:opacity-40 flex items-center gap-1 text-sm">
                          <Play className="w-3.5 h-3.5" /> Start
                        </button>
                      )}
                      <button onClick={() => updateAccount(acc.id, { enabled: !acc.enabled })}
                        className={`px-2.5 py-1.5 rounded-lg text-sm ${acc.enabled ? 'bg-dark-800 text-dark-300' : 'bg-dark-900 text-dark-500'}`}>
                        <Power className="w-4 h-4" />
                      </button>
                      <button onClick={() => handleStartEdit(acc)}
                        className="px-2.5 py-1.5 rounded-lg bg-dark-800 text-dark-300 hover:bg-dark-700">
                        <Edit2 className="w-4 h-4" />
                      </button>
                      <button onClick={() => setDeleteConfirm(acc.id)}
                        className="px-2.5 py-1.5 rounded-lg bg-danger/10 text-danger hover:bg-danger/20">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                )}
              </motion.div>
            )
          })}
        </div>
      )}

      <AnimatePresence>
        {deleteConfirm !== null && (
          <motion.div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={() => setDeleteConfirm(null)}>
            <motion.div className="card max-w-sm w-full"
              initial={{ scale: 0.95 }} animate={{ scale: 1 }} exit={{ scale: 0.95 }}
              onClick={e => e.stopPropagation()}>
              <h4 className="font-medium text-dark-100 mb-2">Удалить аккаунт?</h4>
              <p className="text-sm text-dark-400 mb-4">Удалятся также все пойманные IP.</p>
              <div className="flex justify-end gap-2">
                <button onClick={() => setDeleteConfirm(null)} className="btn btn-secondary">Отмена</button>
                <button onClick={async () => { await deleteAccount(deleteConfirm); setDeleteConfirm(null) }}
                  disabled={isSaving} className="btn bg-danger/20 text-danger hover:bg-danger/30">
                  Удалить
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {showIpsModal && (
          <motion.div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={() => setShowIpsModal(null)}>
            <motion.div className="card max-w-2xl w-full max-h-[85vh] overflow-hidden flex flex-col"
              initial={{ scale: 0.95 }} animate={{ scale: 1 }} exit={{ scale: 0.95 }}
              onClick={e => e.stopPropagation()}>
              <div className="flex items-center justify-between mb-3">
                <h4 className="font-medium text-dark-100">
                  Пойманные IP — {showIpsModal.name || 'Все'}
                </h4>
                <div className="flex items-center gap-2">
                  {showIpsModal.type === 'all' && visibleIps.length > 0 && (
                    <button onClick={async () => { await clearCaughtIps(); setShowIpsModal(null) }}
                      className="btn-tool btn-tool-danger">
                      Очистить все
                    </button>
                  )}
                  {showIpsModal.type === 'account' && showIpsModal.id && visibleIps.length > 0 && (
                    <button onClick={async () => { await clearAccountCaughtIps(showIpsModal.id!); setShowIpsModal(null) }}
                      className="btn-tool btn-tool-danger">
                      Очистить
                    </button>
                  )}
                  <button onClick={() => setShowIpsModal(null)} className="p-1.5 hover:bg-dark-700/50 rounded">
                    <X className="w-4 h-4 text-dark-400" />
                  </button>
                </div>
              </div>
              {!isLoadingCaughtIps && visibleIps.length > 0 && (
                <div className="flex flex-wrap items-center gap-2 mb-3 text-xs">
                  <span className="text-dark-400">Всего {visibleIps.length}:</span>
                  {visibleIpsByZone.map(([zone, count]) => (
                    <span key={zone} className="px-2 py-1 rounded-lg bg-violet-500/10 border border-violet-500/30 font-mono text-violet-300">
                      {zone || '—'} · {count}
                    </span>
                  ))}
                </div>
              )}
              {isLoadingCaughtIps ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="w-6 h-6 text-violet-400 animate-spin" />
                </div>
              ) : visibleIps.length === 0 ? (
                <p className="text-center text-dark-500 text-sm py-8">Пойманных IP пока нет</p>
              ) : (
                <div className="overflow-auto space-y-1">
                  {visibleIps.map(ip => (
                    <div key={ip.id} className="flex items-center justify-between gap-2 p-2 rounded bg-dark-800/40">
                      <div className="font-mono text-sm text-violet-300">{ip.ip_address}</div>
                      <div className="text-xs text-dark-500 flex-1 truncate">
                        {ip.zone} · {ip.account_label || `id=${ip.account_id}`}
                      </div>
                      <button onClick={() => { navigator.clipboard.writeText(ip.ip_address) }}
                        className="p-1.5 text-dark-400 hover:text-dark-200" title="Скопировать">
                        <Copy className="w-4 h-4" />
                      </button>
                      <button onClick={() => deleteCaughtIp(ip.id)}
                        className="p-1.5 text-dark-500 hover:text-danger" title="Удалить">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// CLOUD.RU — MANUAL TAB
// ═══════════════════════════════════════════════════════════════════════

function CloudManualTab({ edition }: { edition: CloudEditionConfig }) {
  const { accounts, fetchAccounts, manualObtainIp, manualObtainHistory, isObtaining, clearManualHistory } = edition.useStore()
  const [accountId, setAccountId] = useState<number | null>(null)
  const [zone, setZone] = useState<string>(edition.zones[0])
  const [specificIp, setSpecificIp] = useState('')
  const hasZoneChoice = edition.zones.length > 1

  useEffect(() => { fetchAccounts() }, [fetchAccounts])

  useEffect(() => {
    if (accountId === null && accounts.length > 0) setAccountId(accounts[0].id)
  }, [accounts, accountId])

  const handleObtain = async () => {
    if (accountId === null) return
    const acc = accounts.find(a => a.id === accountId)
    const label = acc?.label || `${edition.labelPrefix}-${accountId}`
    const wantedIp = edition.supportsSpecificIp ? specificIp.trim() : ''
    const req: CloudManualObtainRequest = { account_id: accountId }
    if (hasZoneChoice) req.zone = zone
    if (wantedIp) req.ip_address = wantedIp
    // В историю как «метку» — запрошенный адрес (точечный) или регион/зону (случайный)
    await manualObtainIp(req, label, wantedIp || zone)
  }

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      <motion.div className="card space-y-4" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2">
          <Target className="w-5 h-5 text-violet-400" /> Получить один IP
        </h3>

        <div>
          <label className="block text-sm text-dark-400 mb-1">Аккаунт</label>
          <select value={accountId ?? ''} onChange={e => setAccountId(Number(e.target.value))}
            className="input">
            {accounts.length === 0 && <option value="">Нет аккаунтов</option>}
            {accounts.map(a => (
              <option key={a.id} value={a.id}>
                {a.label || `${edition.labelPrefix}-${a.id}`} · {a.project_id}
              </option>
            ))}
          </select>
        </div>

        {hasZoneChoice && <div>
          <label className="block text-sm text-dark-400 mb-2">Зона</label>
          <div className="grid grid-cols-3 gap-3">
            {edition.zones.map(z => (
              <motion.button key={z} onClick={() => setZone(z)}
                className={`px-6 py-4 rounded-xl border-2 text-lg font-mono font-bold transition-all ${
                  zone === z
                    ? 'bg-violet-500/20 border-violet-500/60 text-violet-300 shadow-lg shadow-violet-500/10'
                    : 'bg-dark-800/50 border-dark-700/50 text-dark-400 hover:border-dark-600'
                }`}
                whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                {z}
              </motion.button>
            ))}
          </div>
        </div>}

        {edition.supportsSpecificIp && <div>
          <label className="block text-sm text-dark-400 mb-1">Конкретный IP (необязательно)</label>
          <input type="text" value={specificIp} onChange={e => setSpecificIp(e.target.value)}
            placeholder="пусто — любой свободный адрес" className="input font-mono" autoComplete="off" />
          <p className="text-xs text-dark-500 mt-1">
            Облако выдаст этот адрес, если он свободен. Занятый другим аккаунтом взять нельзя.
          </p>
        </div>}

        <div className="flex justify-end">
          <motion.button onClick={handleObtain} disabled={isObtaining || accountId === null}
            className="btn btn-primary flex items-center gap-2"
            whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
            {isObtaining ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
            Получить IP
          </motion.button>
        </div>
      </motion.div>

      {manualObtainHistory.length > 0 && (
        <motion.div className="card" variants={itemVariants}>
          <div className="flex items-center justify-between mb-3">
            <h4 className="font-medium text-dark-100">История</h4>
            <button onClick={clearManualHistory} className="btn-tool">
              Очистить
            </button>
          </div>
          <div className="space-y-2">
            {manualObtainHistory.map(entry => (
              <div key={entry.id} className="flex items-center gap-3 p-2 rounded bg-dark-800/40 text-sm">
                <div className="flex-shrink-0">
                  {entry.success
                    ? <CheckCircle2 className="w-4 h-4 text-violet-400" />
                    : <AlertCircle className="w-4 h-4 text-danger" />}
                </div>
                <div className="text-xs text-dark-500 w-32 flex-shrink-0">{entry.timestamp}</div>
                <div className="text-xs text-dark-400 flex-shrink-0">{entry.accountLabel} · {entry.zone}</div>
                <div className="flex-1 font-mono text-violet-300 truncate">
                  {entry.success ? entry.ipAddress : entry.error}
                </div>
                {entry.success && entry.ipAddress && (
                  <button onClick={() => navigator.clipboard.writeText(entry.ipAddress!)}
                    className="p-1.5 text-dark-400 hover:text-dark-200">
                    <Copy className="w-4 h-4" />
                  </button>
                )}
              </div>
            ))}
          </div>
        </motion.div>
      )}
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// CLOUD.RU — SETTINGS TAB
// ═══════════════════════════════════════════════════════════════════════

function CloudSettingsTab({ edition, onHelp }: { edition: CloudEditionConfig; onHelp: () => void }) {
  const {
    settings, isLoadingSettings, isSaving, fetchSettings, updateSettings, error, clearError,
    whitelist, fetchWhitelist, addWhitelistCidrs, removeWhitelistCidr, clearWhitelist,
  } = edition.useStore()
  const hasZoneChoice = edition.zones.length > 1
  const [formData, setFormData] = useState<any>(null)
  const [hasChanges, setHasChanges] = useState(false)
  const [showToken, setShowToken] = useState(false)
  const [bulkCidrs, setBulkCidrs] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)
  const [bulkResult, setBulkResult] = useState<string | null>(null)
  const [confirmClearWl, setConfirmClearWl] = useState(false)

  useEffect(() => { fetchSettings(); fetchWhitelist() }, [fetchSettings, fetchWhitelist])

  useEffect(() => {
    if (settings && !formData) {
      setFormData({
        delay_burst: settings.delay_burst,
        delay_round: settings.delay_round,
        delay_delete: settings.delay_delete,
        jitter: settings.jitter,
        provision_timeout: settings.provision_timeout,
        provision_poll: settings.provision_poll,
        ...(hasZoneChoice ? { zones: settings.zones } : {}),
        ...(edition.supportsSpecificIp ? { mode: settings.mode || 'random' } : {}),
        telegram_bot_token: settings.telegram_bot_token,
        telegram_chat_id: settings.telegram_chat_id,
        proxy_required: settings.proxy_required,
        log_level: settings.log_level || 'info',
      })
    }
  }, [settings, formData, hasZoneChoice, edition.supportsSpecificIp])

  const handleChange = (field: string, value: any) => {
    setFormData((f: any) => f ? { ...f, [field]: value } : f)
    setHasChanges(true)
  }

  const toggleZone = (zone: string) => {
    if (!formData) return
    const current = formData.zones as string[]
    const next = current.includes(zone) ? current.filter((z: string) => z !== zone) : [...current, zone]
    if (next.length === 0) return
    handleChange('zones', next)
  }

  const handleSave = async () => {
    if (!formData) return
    const success = await updateSettings(formData)
    if (success) setHasChanges(false)
  }

  const handleBulkAdd = async () => {
    const lines = bulkCidrs.split(/[\s,]+/).map(s => s.trim()).filter(Boolean)
    if (lines.length === 0) return
    setBulkBusy(true)
    setBulkResult(null)
    // Вся пачка — одним запросом: не спамим бэкенд и не дёргаем список на каждой строке
    const res = await addWhitelistCidrs(lines)
    setBulkBusy(false)
    if (!res) { setBulkResult('Не удалось добавить'); return }
    const parts = [`Добавлено ${res.added}`]
    if (res.skipped) parts.push(`уже были ${res.skipped}`)
    if (res.invalid.length) parts.push(`некорректных ${res.invalid.length}`)
    setBulkResult(parts.join(', '))
    if (res.invalid.length === 0) setBulkCidrs('')
  }

  const handleClearWhitelist = async () => {
    await clearWhitelist()
    setConfirmClearWl(false)
    setBulkResult(null)
  }

  if (isLoadingSettings || !formData) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-8 h-8 text-violet-400 animate-spin" />
      </div>
    )
  }

  const availableZones = settings?.available_zones || edition.zones

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-6">
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm flex-1">{error}</span>
            <button onClick={clearError} className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Clock className="w-5 h-5 text-violet-400" /> Задержки (сек)
          <CloudHelpButton onClick={onHelp} size="sm" />
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Burst (между POST)</label>
            <input type="number" step="0.1" min={0} value={formData.delay_burst}
              onChange={e => handleChange('delay_burst', parseFloat(e.target.value) || 0)}
              className="input font-mono" />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Round (между раундами)</label>
            <input type="number" step="0.1" min={0} value={formData.delay_round}
              onChange={e => handleChange('delay_round', parseFloat(e.target.value) || 0)}
              className="input font-mono" />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Delete (перед DELETE)</label>
            <input type="number" step="0.1" min={0} value={formData.delay_delete}
              onChange={e => handleChange('delay_delete', parseFloat(e.target.value) || 0)}
              className="input font-mono" />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Jitter (±рандом)</label>
            <input type="number" step="0.1" min={0} value={formData.jitter}
              onChange={e => handleChange('jitter', parseFloat(e.target.value) || 0)}
              className="input font-mono" />
          </div>
        </div>
        <p className="text-xs text-dark-500 mt-2">
          Каждая задержка получает рандомное отклонение от −jitter до +jitter (но не меньше нуля).
        </p>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <RefreshCw className="w-5 h-5 text-violet-400" /> Provisioning
          <CloudHelpButton onClick={onHelp} size="sm" />
        </h3>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Таймаут ожидания (сек)</label>
            <input type="number" min={5} max={600} value={formData.provision_timeout}
              onChange={e => handleChange('provision_timeout', parseInt(e.target.value) || 60)}
              className="input font-mono" />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Шаг опроса статуса (сек)</label>
            <input type="number" step="0.1" min={0.5} value={formData.provision_poll}
              onChange={e => handleChange('provision_poll', parseFloat(e.target.value) || 2)}
              className="input font-mono" />
          </div>
        </div>
      </motion.div>

      {edition.supportsSpecificIp && (
        <motion.div className="card" variants={itemVariants}>
          <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
            <Target className="w-5 h-5 text-violet-400" /> Режим перебора
            <CloudHelpButton onClick={onHelp} size="sm" />
          </h3>
          <div className="grid grid-cols-2 gap-3">
            {([
              ['random', 'Случайный', 'Брать что выдаст облако и оставлять попавшие в whitelist'],
              ['targeted', 'Точечный', 'Запрашивать адреса из whitelist напрямую, по одному'],
            ] as const).map(([value, title, desc]) => (
              <motion.button key={value} onClick={() => handleChange('mode', value)}
                className={`p-4 rounded-xl border text-left transition-all ${
                  formData.mode === value
                    ? 'bg-violet-500/20 border-violet-500/40'
                    : 'bg-dark-800/50 border-dark-700/50 hover:border-dark-600'
                }`}
                whileHover={{ scale: 1.01 }} whileTap={{ scale: 0.99 }}>
                <div className="flex items-center gap-2 mb-1">
                  <div className={`w-3 h-3 rounded-full ${formData.mode === value ? 'bg-violet-400' : 'bg-dark-600'}`} />
                  <span className={`font-medium ${formData.mode === value ? 'text-violet-300' : 'text-dark-300'}`}>
                    {title}
                  </span>
                </div>
                <p className="text-xs text-dark-400">{desc}</p>
              </motion.button>
            ))}
          </div>
          <p className="text-xs text-dark-500 mt-2">
            Точечный режим обходит whitelist-подсети адрес за адресом и просит у облака каждый напрямую — так не тратится квота на лишние адреса. Занятые другими пропускаются.
          </p>
        </motion.div>
      )}

      {hasZoneChoice && (
        <motion.div className="card" variants={itemVariants}>
          <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
            <Globe className="w-5 h-5 text-violet-400" /> Зоны доступности
          </h3>
          <div className="grid grid-cols-3 gap-3">
            {availableZones.map(zone => {
              const isSelected = (formData.zones as string[]).includes(zone)
              return (
                <motion.button key={zone} onClick={() => toggleZone(zone)}
                  className={`px-6 py-4 rounded-xl border-2 text-lg font-mono font-bold transition-all ${
                    isSelected
                      ? 'bg-violet-500/20 border-violet-500/60 text-violet-300 shadow-lg shadow-violet-500/10'
                      : 'bg-dark-800/50 border-dark-700/50 text-dark-400 hover:border-dark-600'
                  }`}
                  whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                  {zone}
                </motion.button>
              )
            })}
          </div>
          <p className="text-xs text-dark-500 mt-2">
            Воркер ходит по выбранным зонам по очереди (round-robin).
          </p>
        </motion.div>
      )}

      <motion.div className="card" variants={itemVariants}>
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-medium text-dark-100 flex items-center gap-2">
            <Network className="w-5 h-5 text-violet-400" /> Whitelist
          </h3>
          <div className="flex items-center gap-3">
            <span className="text-xs text-dark-500">{whitelist.length} записей</span>
            {whitelist.length > 0 && (
              confirmClearWl ? (
                <span className="flex items-center gap-2 text-xs">
                  <span className="text-dark-400">Очистить всё?</span>
                  <button onClick={handleClearWhitelist} disabled={isSaving}
                    className="text-danger hover:text-danger/80 font-medium">Да</button>
                  <button onClick={() => setConfirmClearWl(false)} className="text-dark-400 hover:text-dark-200">Нет</button>
                </span>
              ) : (
                <button onClick={() => setConfirmClearWl(true)}
                  className="text-xs text-danger hover:text-danger/80 flex items-center gap-1">
                  <Trash2 className="w-3.5 h-3.5" /> Очистить всё
                </button>
              )
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-2 mb-3">
          {whitelist.length === 0 ? (
            <span className="text-xs text-dark-500 italic">Пусто — добавьте подсети или отдельные IP для отлова попаданий</span>
          ) : whitelist.map(w => (
            <span key={w.id} className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-violet-500/10 border border-violet-500/30 text-sm font-mono text-violet-300">
              {w.cidr}
              <button onClick={() => removeWhitelistCidr(w.id)} className="text-dark-400 hover:text-danger">
                <X className="w-4 h-4" />
              </button>
            </span>
          ))}
        </div>
        <textarea value={bulkCidrs} onChange={e => setBulkCidrs(e.target.value)}
          placeholder="45.9.24.66&#10;109.120.188.0/22&#10;89.208.228.0/22"
          className="input font-mono h-24" />
        <div className="flex items-center justify-between mt-2">
          <span className="text-xs text-dark-500">
            {bulkResult || 'Подсети (CIDR) и/или отдельные IP — по строке, через запятую или пробел'}
          </span>
          <button onClick={handleBulkAdd} disabled={bulkBusy || !bulkCidrs.trim()}
            className="btn btn-primary flex items-center gap-2 disabled:opacity-40">
            {bulkBusy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
            Добавить
          </button>
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Send className="w-5 h-5 text-violet-400" /> Telegram уведомления
        </h3>
        <div className="space-y-3">
          <div className="relative">
            <SecretInput revealed={showToken} value={formData.telegram_bot_token}
              onChange={e => handleChange('telegram_bot_token', e.target.value)}
              className="input font-mono pr-10" placeholder="Bot Token (123456789:ABC…)" />
            <button type="button" onClick={() => setShowToken(s => !s)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
              {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
          <input type="text" value={formData.telegram_chat_id}
            onChange={e => handleChange('telegram_chat_id', e.target.value)}
            className="input font-mono" placeholder="Chat ID" autoComplete="off" />
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <ScrollText className="w-5 h-5 text-violet-400" /> Уровень логирования
        </h3>
        <div className="grid grid-cols-2 gap-3">
          {(['info', 'full'] as const).map(level => (
            <motion.button key={level} onClick={() => handleChange('log_level', level)}
              className={`p-4 rounded-xl border text-left transition-all ${
                formData.log_level === level
                  ? 'bg-violet-500/20 border-violet-500/40'
                  : 'bg-dark-800/50 border-dark-700/50 hover:border-dark-600'
              }`}
              whileHover={{ scale: 1.01 }} whileTap={{ scale: 0.99 }}>
              <div className="flex items-center gap-2 mb-1">
                <div className={`w-3 h-3 rounded-full ${formData.log_level === level ? 'bg-violet-400' : 'bg-dark-600'}`} />
                <span className={`font-medium ${formData.log_level === level ? 'text-violet-300' : 'text-dark-300'}`}>
                  {level.toUpperCase()}
                </span>
              </div>
              <p className="text-xs text-dark-400">
                {level === 'info' ? 'Только ключевые события раундов' : 'Подробный лог каждой попытки'}
              </p>
            </motion.button>
          ))}
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Settings className="w-5 h-5 text-violet-400" /> Дополнительно
        </h3>
        <label className="flex items-center gap-3 cursor-pointer">
          <Checkbox checked={formData.proxy_required}
            onChange={e => handleChange('proxy_required', e.target.checked)} />
          <span className="text-sm text-dark-300">Proxy обязательно (не запускать аккаунт без proxy)</span>
        </label>
      </motion.div>

      <motion.div className="flex justify-end" variants={itemVariants}>
        <motion.button onClick={handleSave} disabled={!hasChanges || isSaving}
          className={`btn ${hasChanges ? 'btn-primary' : 'btn-secondary opacity-50'} flex items-center gap-2`}
          whileHover={hasChanges ? { scale: 1.02 } : {}} whileTap={hasChanges ? { scale: 0.98 } : {}}>
          {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          Сохранить
        </motion.button>
      </motion.div>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// CLOUD.RU — LOGS TAB
// ═══════════════════════════════════════════════════════════════════════

function CloudLogsTab({ edition }: { edition: CloudEditionConfig }) {
  const { logs, fetchLogs, clearLogs, subscribeToLogs, unsubscribeFromLogs } = edition.useStore()
  const scrollRef = useRef<HTMLDivElement>(null)
  const [autoScroll, setAutoScroll] = useState(true)

  useEffect(() => {
    fetchLogs(500)
    subscribeToLogs()
    return () => unsubscribeFromLogs()
  }, [fetchLogs, subscribeToLogs, unsubscribeFromLogs])

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [logs, autoScroll])

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs text-dark-500">{logs.length} строк</div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 cursor-pointer text-sm text-dark-300">
            <Checkbox checked={autoScroll} onChange={e => setAutoScroll(e.target.checked)} />
            автоскролл
          </label>
          <button onClick={async () => { await clearLogs() }}
            className="text-sm text-danger hover:text-danger/80">Очистить</button>
        </div>
      </div>
      <div ref={scrollRef}
        className="card font-mono text-xs text-dark-300 h-[60vh] overflow-y-auto whitespace-pre-wrap">
        {logs.length === 0 ? (
          <div className="text-dark-500 italic">Логов нет. Запустите воркер аккаунта.</div>
        ) : logs.map((line, i) => <div key={i}>{line}</div>)}
      </div>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// STATS TAB — общая для всех провайдеров
// ═══════════════════════════════════════════════════════════════════════

const RECEIVED_STATS_REFRESH_MS = 10_000

const formatCount = (n: number) => n.toLocaleString('ru-RU')
const formatSeen = (ts: string | null) => (ts ? new Date(ts).toLocaleString('ru-RU') : '—')

interface ReceivedStatsApi {
  getReceivedStats: (accountId?: number) => Promise<{ data: ReceivedStats }>
  getReceivedSubnetIps: (subnet: string, accountId?: number) => Promise<{ data: ReceivedIP[] }>
  clearReceivedIps: (accountId?: number) => Promise<unknown>
}

interface StatsAccount {
  id: number
  label: string
}

type StatsAccent = 'accent' | 'orange' | 'violet' | 'cyan'

// Классы целиком, а не через шаблон: Tailwind собирает только те, что видит в исходнике
const STATS_ACCENT_CLASSES: Record<StatsAccent, {
  text: string
  icon: string
  activeCard: string
  bar: string
  badge: string
  activeChip: string
  filter: string
}> = {
  accent: {
    text: 'text-accent-300',
    icon: 'text-accent-400',
    activeCard: 'border-accent-500/60 bg-accent-500/10',
    bar: 'bg-accent-500/70',
    badge: 'bg-accent-500/15 border-accent-500/30 text-accent-300',
    activeChip: 'bg-accent-500/20 text-accent-300',
    filter: 'bg-accent-500/10 border-accent-500/30 text-accent-300',
  },
  orange: {
    text: 'text-orange-300',
    icon: 'text-orange-400',
    activeCard: 'border-orange-500/60 bg-orange-500/10',
    bar: 'bg-orange-500/70',
    badge: 'bg-orange-500/15 border-orange-500/30 text-orange-300',
    activeChip: 'bg-orange-500/20 text-orange-300',
    filter: 'bg-orange-500/10 border-orange-500/30 text-orange-300',
  },
  violet: {
    text: 'text-violet-300',
    icon: 'text-violet-400',
    activeCard: 'border-violet-500/60 bg-violet-500/10',
    bar: 'bg-violet-500/70',
    badge: 'bg-violet-500/15 border-violet-500/30 text-violet-300',
    activeChip: 'bg-violet-500/20 text-violet-300',
    filter: 'bg-violet-500/10 border-violet-500/30 text-violet-300',
  },
  cyan: {
    text: 'text-cyan-300',
    icon: 'text-cyan-400',
    activeCard: 'border-cyan-500/60 bg-cyan-500/10',
    bar: 'bg-cyan-500/70',
    badge: 'bg-cyan-500/15 border-cyan-500/30 text-cyan-300',
    activeChip: 'bg-cyan-500/20 text-cyan-300',
    filter: 'bg-cyan-500/10 border-cyan-500/30 text-cyan-300',
  },
}

function ReceivedSubnetIps({ api, subnet, accountId, accent, formatZone }: {
  api: ReceivedStatsApi
  subnet: string
  accountId?: number
  accent: StatsAccent
  formatZone: (zone: string) => string
}) {
  const colors = STATS_ACCENT_CLASSES[accent]
  const [ips, setIps] = useState<ReceivedIP[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api.getReceivedSubnetIps(subnet, accountId)
      .then(res => { if (!cancelled) setIps(res.data) })
      .catch((e: any) => { if (!cancelled) setLoadError(e.response?.data?.detail || 'Не удалось загрузить адреса') })
    return () => { cancelled = true }
  }, [api, subnet, accountId])

  if (loadError) return <p className="text-xs text-danger px-3 py-2">{loadError}</p>
  if (!ips) {
    return (
      <div className="flex justify-center py-3">
        <Loader2 className={`w-4 h-4 ${colors.icon} animate-spin`} />
      </div>
    )
  }

  return (
    <div className="space-y-1 px-3 pb-3">
      {ips.map(ip => (
        <div key={`${ip.zone}-${ip.ip_address}`}
          className="flex items-center gap-3 px-2 py-1.5 rounded bg-dark-900/60 text-xs">
          <span className={`font-mono text-sm w-32 flex-shrink-0 ${ip.caught ? colors.text : 'text-dark-200'}`}>
            {ip.ip_address}
          </span>
          <span className="font-mono text-dark-400 w-20 flex-shrink-0 truncate">{formatZone(ip.zone)}</span>
          <span className="text-dark-300 w-14 flex-shrink-0">×{formatCount(ip.received)}</span>
          <span className="flex-1 min-w-0 truncate text-dark-500">последний раз {formatSeen(ip.last_seen)}</span>
          {ip.caught && (
            <span className={`flex items-center gap-1 ${colors.text} flex-shrink-0`}>
              <CheckCircle2 className="w-3.5 h-3.5" /> поймано
            </span>
          )}
          <button onClick={() => navigator.clipboard.writeText(ip.ip_address)}
            className="p-1.5 text-dark-400 hover:text-dark-200 flex-shrink-0" title="Скопировать">
            <Copy className="w-4 h-4" />
          </button>
        </div>
      ))}
    </div>
  )
}

function ReceivedStatsTab({ api, accounts, accent, zonesTitle, formatZone = zone => zone, onHelp }: {
  api: ReceivedStatsApi
  accounts: StatsAccount[]
  accent: StatsAccent
  zonesTitle: string
  formatZone?: (zone: string) => string
  onHelp?: () => void
}) {
  const colors = STATS_ACCENT_CLASSES[accent]
  const [accountId, setAccountId] = useState<number | undefined>(undefined)
  const [stats, setStats] = useState<ReceivedStats | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [zoneFilter, setZoneFilter] = useState<string | null>(null)
  const [expandedSubnet, setExpandedSubnet] = useState<string | null>(null)
  const [confirmReset, setConfirmReset] = useState(false)
  const [isResetting, setIsResetting] = useState(false)

  const loadStats = useCallback(async () => {
    try {
      const res = await api.getReceivedStats(accountId)
      setStats(res.data)
      setLoadError(null)
    } catch (e: any) {
      setLoadError(e.response?.data?.detail || 'Не удалось загрузить статистику')
    }
  }, [api, accountId])

  useEffect(() => {
    setStats(null)
    setExpandedSubnet(null)
    setZoneFilter(null)
    loadStats()
    const interval = setInterval(loadStats, RECEIVED_STATS_REFRESH_MS)
    return () => clearInterval(interval)
  }, [loadStats])

  const handleReset = async () => {
    setIsResetting(true)
    try {
      await api.clearReceivedIps(accountId)
      setExpandedSubnet(null)
      await loadStats()
    } catch (e: any) {
      setLoadError(e.response?.data?.detail || 'Не удалось сбросить статистику')
    } finally {
      setIsResetting(false)
      setConfirmReset(false)
    }
  }

  const visibleSubnets = useMemo(() => {
    if (!stats) return []
    if (!zoneFilter) return stats.subnets
    return stats.subnets.filter(s => s.zones[zoneFilter] !== undefined)
  }, [stats, zoneFilter])

  const resetScope = accountId === undefined
    ? 'всех аккаунтов'
    : `аккаунта ${accounts.find(a => a.id === accountId)?.label || accountId}`

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      <motion.div className="flex items-center justify-between gap-3 flex-wrap" variants={itemVariants}>
        <div className="flex items-center gap-2">
          <select value={accountId ?? ''} className="input w-auto"
            onChange={e => setAccountId(e.target.value ? Number(e.target.value) : undefined)}>
            <option value="">Все аккаунты</option>
            {accounts.map(a => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select>
          <button onClick={loadStats} title="Обновить"
            className="p-2 rounded-lg bg-dark-800/50 hover:bg-dark-700/50 text-dark-300 border border-dark-700/50">
            <RefreshCw className="w-4 h-4" />
          </button>
          {onHelp && <CloudHelpButton onClick={onHelp} />}
        </div>
        <button onClick={() => setConfirmReset(true)} disabled={!stats || stats.totals.received === 0}
          className="px-3 py-1.5 rounded-lg bg-danger/10 text-danger hover:bg-danger/20 disabled:opacity-40 text-sm flex items-center gap-2">
          <Trash2 className="w-4 h-4" /> Сбросить
        </button>
      </motion.div>

      {loadError && (
        <div className="flex items-center gap-3 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger">
          <AlertTriangle className="w-5 h-5 flex-shrink-0" />
          <span className="text-sm">{loadError}</span>
        </div>
      )}

      {!stats ? (
        !loadError && (
          <div className="flex items-center justify-center py-16">
            <Loader2 className={`w-8 h-8 ${colors.icon} animate-spin`} />
          </div>
        )
      ) : stats.totals.received === 0 ? (
        <div className="card text-center py-12">
          <BarChart3 className="w-12 h-12 text-dark-500 mx-auto mb-3" />
          <p className="text-dark-300 text-sm">Статистики пока нет</p>
          <p className="text-dark-500 text-sm mt-1">
            Она появится, когда воркеры начнут получать адреса: сюда попадает каждый адрес, который выдало облако.
          </p>
        </div>
      ) : (
        <>
          <motion.div className="grid grid-cols-2 md:grid-cols-4 gap-3" variants={itemVariants}>
            {[
              { label: 'Получено', value: stats.totals.received },
              { label: 'Уникальных IP', value: stats.totals.unique_ips },
              { label: 'Поймано', value: stats.totals.caught, highlight: true },
              { label: 'Подсетей /24', value: stats.totals.subnets },
            ].map(item => (
              <div key={item.label} className="card py-3">
                <div className="text-xs text-dark-500">{item.label}</div>
                <div className={`text-2xl font-bold font-mono mt-1 ${item.highlight ? colors.text : 'text-dark-100'}`}>
                  {formatCount(item.value)}
                </div>
              </div>
            ))}
          </motion.div>
          <p className="text-xs text-dark-500">
            Первая выдача {formatSeen(stats.totals.first_seen)} · последняя {formatSeen(stats.totals.last_seen)}
          </p>

          <motion.div className="grid grid-cols-1 md:grid-cols-3 gap-3" variants={itemVariants}>
            {stats.zones.map(z => {
              const share = (z.received / stats.totals.received) * 100
              const isActive = zoneFilter === z.zone
              return (
                <button key={z.zone} onClick={() => setZoneFilter(isActive ? null : z.zone)}
                  className={`card text-left transition-all border-2 ${
                    isActive ? colors.activeCard : 'border-transparent hover:border-dark-600'
                  }`}>
                  <div className="flex items-center justify-between gap-2">
                    <span className={`font-mono font-bold truncate ${colors.text}`}>{z.zone}</span>
                    <span className="text-xs text-dark-500 flex-shrink-0">{share.toFixed(1)}%</span>
                  </div>
                  <div className="text-2xl font-bold font-mono text-dark-100 mt-2">{formatCount(z.received)}</div>
                  <div className="h-1.5 rounded-full bg-dark-800 mt-2 overflow-hidden">
                    <div className={`h-full rounded-full ${colors.bar}`} style={{ width: `${share}%` }} />
                  </div>
                  <div className="text-xs text-dark-400 mt-2">
                    уникальных {formatCount(z.unique_ips)} · поймано{' '}
                    <span className={z.caught > 0 ? colors.text : ''}>{formatCount(z.caught)}</span>
                  </div>
                  <div className="text-xs text-dark-500 mt-0.5">последняя выдача {formatSeen(z.last_seen)}</div>
                </button>
              )
            })}
          </motion.div>

          <motion.div className="card p-0 overflow-hidden" variants={itemVariants}>
            <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-dark-800/60">
              <h3 className="font-medium text-dark-100 flex items-center gap-2">
                <Network className={`w-5 h-5 ${colors.icon}`} /> Подсети /24
                <span className="text-xs text-dark-500 font-normal">{visibleSubnets.length}</span>
              </h3>
              {zoneFilter && (
                <button onClick={() => setZoneFilter(null)}
                  className={`px-2.5 py-1 rounded-lg border text-xs font-mono flex items-center gap-1.5 ${colors.filter}`}>
                  {zoneFilter} <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
            <div className="hidden md:grid grid-cols-[minmax(0,1.6fr)_minmax(0,1.6fr)_repeat(3,minmax(0,0.7fr))_minmax(0,1.2fr)] gap-3 px-4 py-2 text-xs text-dark-500 border-b border-dark-800/40">
              <span>Подсеть</span><span>{zonesTitle}</span><span>Получено</span><span>Уникальных</span><span>Поймано</span><span>Последняя</span>
            </div>
            <div className="divide-y divide-dark-800/40">
              {visibleSubnets.map(s => {
                const isExpanded = expandedSubnet === s.subnet
                return (
                  <div key={s.subnet}>
                    <button onClick={() => setExpandedSubnet(isExpanded ? null : s.subnet)}
                      className="w-full grid grid-cols-2 md:grid-cols-[minmax(0,1.6fr)_minmax(0,1.6fr)_repeat(3,minmax(0,0.7fr))_minmax(0,1.2fr)] gap-x-3 gap-y-1 px-4 py-2.5 text-left text-sm hover:bg-dark-800/30 items-center">
                      <span className="flex items-center gap-2 min-w-0">
                        <ChevronRight className={`w-4 h-4 text-dark-500 flex-shrink-0 transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
                        <span className="font-mono text-dark-100 truncate">{s.subnet}</span>
                        {s.in_whitelist && (
                          <span className={`px-1.5 py-0.5 rounded border text-2xs flex-shrink-0 ${colors.badge}`}>
                            whitelist
                          </span>
                        )}
                      </span>
                      <span className="flex flex-wrap gap-1">
                        {Object.entries(s.zones).sort(([a], [b]) => a.localeCompare(b)).map(([zone, count]) => (
                          <span key={zone} className={`px-1.5 py-0.5 rounded text-2xs font-mono ${
                            zone === zoneFilter ? colors.activeChip : 'bg-dark-800 text-dark-400'
                          }`}>
                            {formatZone(zone)} · {formatCount(count)}
                          </span>
                        ))}
                      </span>
                      <span className="font-mono text-dark-100"><span className="md:hidden text-dark-500 font-sans text-xs">получено </span>{formatCount(s.received)}</span>
                      <span className="font-mono text-dark-300"><span className="md:hidden text-dark-500 font-sans text-xs">уникальных </span>{formatCount(s.unique_ips)}</span>
                      <span className={`font-mono ${s.caught > 0 ? colors.text : 'text-dark-500'}`}><span className="md:hidden text-dark-500 font-sans text-xs">поймано </span>{formatCount(s.caught)}</span>
                      <span className="text-xs text-dark-500">{formatSeen(s.last_seen)}</span>
                    </button>
                    {isExpanded && (
                      <ReceivedSubnetIps api={api} subnet={s.subnet} accountId={accountId}
                        accent={accent} formatZone={formatZone} />
                    )}
                  </div>
                )
              })}
            </div>
          </motion.div>
        </>
      )}

      <AnimatePresence>
        {confirmReset && (
          <motion.div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={() => setConfirmReset(false)}>
            <motion.div className="card max-w-sm w-full"
              initial={{ scale: 0.95 }} animate={{ scale: 1 }} exit={{ scale: 0.95 }}
              onClick={e => e.stopPropagation()}>
              <h4 className="font-medium text-dark-100 mb-2">Сбросить статистику?</h4>
              <p className="text-sm text-dark-400 mb-4">
                Удалится статистика {resetScope}. Пойманные IP останутся.
              </p>
              <div className="flex justify-end gap-2">
                <button onClick={() => setConfirmReset(false)} className="btn btn-secondary">Отмена</button>
                <button onClick={handleReset} disabled={isResetting}
                  className="btn bg-danger/20 text-danger hover:bg-danger/30 flex items-center gap-2">
                  {isResetting && <Loader2 className="w-4 h-4 animate-spin" />} Сбросить
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

function VkStatsTab() {
  const { accounts, fetchAccounts } = useExtStore()
  useEffect(() => { fetchAccounts() }, [fetchAccounts])
  const statsAccounts = useMemo(() => accounts.map(a => ({ id: a.id, label: a.email })), [accounts])
  return <ReceivedStatsTab api={extApi} accounts={statsAccounts} accent="accent" zonesTitle="Подсети VK" />
}

function YcStatsTab() {
  const { accounts, fetchAccounts } = useYcStore()
  useEffect(() => { fetchAccounts() }, [fetchAccounts])
  const statsAccounts = useMemo(
    () => accounts.map(a => ({ id: a.id, label: `${a.label || `yc-${a.id}`} · ${a.folder_id}` })),
    [accounts],
  )
  return <ReceivedStatsTab api={ycApi} accounts={statsAccounts} accent="orange" zonesTitle="Зоны" />
}

function CloudStatsTab({ edition, onHelp }: { edition: CloudEditionConfig; onHelp: () => void }) {
  const { accounts, fetchAccounts } = edition.useStore()
  useEffect(() => { fetchAccounts() }, [fetchAccounts])
  const statsAccounts = useMemo(
    () => accounts.map(a => ({ id: a.id, label: `${a.label || `${edition.labelPrefix}-${a.id}`} · ${a.project_id}` })),
    [accounts, edition.labelPrefix],
  )
  return (
    <ReceivedStatsTab api={edition.api} accounts={statsAccounts} accent="violet"
      zonesTitle={edition.zones.length > 1 ? 'Зоны' : 'Регион'}
      formatZone={edition.formatZone} onHelp={onHelp} />
  )
}

function SelectelStatsTab() {
  const { accounts, fetchAccounts } = useSelectelStore()
  useEffect(() => { fetchAccounts() }, [fetchAccounts])
  const statsAccounts = useMemo(
    () => accounts.map(a => ({ id: a.id, label: `${a.label || `selectel-${a.id}`} · ${a.project_id}` })),
    [accounts],
  )
  return <ReceivedStatsTab api={selectelApi} accounts={statsAccounts} accent="cyan" zonesTitle="Регионы" />
}

// ═══════════════════════════════════════════════════════════════════════
// CLOUD.RU — PAGE (Evolution / Advanced switcher)
// ═══════════════════════════════════════════════════════════════════════

const CLOUD_EDITIONS: Record<CloudEdition, CloudEditionConfig> = {
  evolution: {
    title: 'Cloud.ru Evolution',
    labelPrefix: 'cloud-evo',
    useStore: useCloudEvoStore,
    api: cloudEvoApi,
    zones: CLOUD_EVO_DEFAULT_ZONES,
    formatZone: zone => zone.replace('ru.', ''),
    supportsSpecificIp: false,
    usesStsToken: false,
    keyIdPlaceholder: 'keyId',
    keySecretPlaceholder: 'secret',
    CredentialsHelp: CloudEvoCredentialsHelp,
    help: CLOUD_EVO_HELP,
  },
  advanced: {
    title: 'Cloud.ru Advanced',
    labelPrefix: 'cloud-adv',
    useStore: useCloudAdvStore,
    api: cloudAdvApi,
    zones: [CLOUD_ADV_REGION],
    formatZone: zone => zone,
    supportsSpecificIp: true,
    usesStsToken: true,
    keyIdPlaceholder: 'AK (Access Key ID)',
    keySecretPlaceholder: 'SK (Secret Access Key)',
    CredentialsHelp: CloudAdvCredentialsHelp,
    help: CLOUD_ADV_HELP,
  },
}

const CLOUD_EDITION_LABELS: { id: CloudEdition; label: string }[] = [
  { id: 'evolution', label: 'Evolution' },
  { id: 'advanced', label: 'Advanced' },
]

function CloudPage() {
  const [editionId, setEditionId] = useState<CloudEdition>('evolution')
  const [activeTab, setActiveTab] = useState<CloudTabType>('accounts')
  const [helpArticle, setHelpArticle] = useState<CloudHelpArticle | null>(null)
  const closeHelp = useCallback(() => setHelpArticle(null), [])
  const edition = CLOUD_EDITIONS[editionId]

  const tabs: { id: CloudTabType; label: string; icon: React.ReactNode }[] = [
    { id: 'accounts', label: 'Аккаунты', icon: <Users className="w-4 h-4" /> },
    { id: 'manual', label: 'Ручной IP', icon: <Target className="w-4 h-4" /> },
    { id: 'stats', label: 'Статистика', icon: <BarChart3 className="w-4 h-4" /> },
    { id: 'settings', label: 'Настройки', icon: <Settings className="w-4 h-4" /> },
    { id: 'logs', label: 'Логи', icon: <ScrollText className="w-4 h-4" /> },
  ]

  return (
    <>
      <motion.div className="flex gap-2 mb-4" variants={itemVariants}>
        {CLOUD_EDITION_LABELS.map(({ id, label }) => (
          <button key={id} onClick={() => setEditionId(id)}
            className={`px-6 py-3 rounded-xl text-sm font-bold transition-all border-2 ${
              editionId === id
                ? 'bg-violet-500/20 text-violet-300 border-violet-500/40 shadow-lg shadow-violet-500/10'
                : 'bg-dark-800/50 text-dark-400 border-dark-700/50 hover:border-dark-600'
            }`}>
            {label}
          </button>
        ))}
      </motion.div>

      <motion.div className="flex items-center gap-2 mb-6" variants={itemVariants}>
        <div className="flex gap-1 bg-dark-800/50 p-1 rounded-xl w-fit">
          {tabs.map(tab => (
            <motion.button key={tab.id} onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                activeTab === tab.id
                  ? 'bg-violet-500/20 text-violet-400'
                  : 'text-dark-400 hover:text-dark-200 hover:bg-dark-700/50'
              }`}
              whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
              {tab.icon} {tab.label}
            </motion.button>
          ))}
        </div>
        <CloudHelpButton onClick={() => setHelpArticle('overview')} />
      </motion.div>

      {/* key с редакцией: при переключении вкладка монтируется заново, а не тащит форму другой редакции */}
      <AnimatePresence mode="wait">
        <motion.div key={`${editionId}-${activeTab}`} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }} transition={{ duration: 0.15 }}>
          {activeTab === 'accounts' && <CloudAccountsTab edition={edition} />}
          {activeTab === 'manual' && <CloudManualTab edition={edition} />}
          {activeTab === 'stats' && <CloudStatsTab edition={edition} onHelp={() => setHelpArticle('stats')} />}
          {activeTab === 'settings' && <CloudSettingsTab edition={edition} onHelp={() => setHelpArticle('settings')} />}
          {activeTab === 'logs' && <CloudLogsTab edition={edition} />}
        </motion.div>
      </AnimatePresence>

      <HelpDrawer open={helpArticle !== null} subtitle={edition.title} onClose={closeHelp}>
        {helpArticle && <Markdown source={edition.help[helpArticle]} />}
      </HelpDrawer>
    </>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// SELECTEL — ACCOUNTS TAB
// ═══════════════════════════════════════════════════════════════════════

function SelectelAccountsTab() {
  const {
    accounts, caughtIps, isLoadingAccounts, isLoadingCaughtIps, isSaving, error,
    fetchAccounts, createAccount, updateAccount, deleteAccount, clearError,
    fetchCaughtIps, clearCaughtIps, deleteCaughtIp, clearAccountCaughtIps,
    workerStatuses, accountErrors, startAccount, stopAccount, fetchWorkerStatuses, clearAccountErrors,
  } = useSelectelStore()

  const [showAddAccount, setShowAddAccount] = useState(false)
  const [accountForm, setAccountForm] = useState({ username: '', password: '', domain: '', project_id: '', label: '', proxy: '' })
  const [editingAccount, setEditingAccount] = useState<number | null>(null)
  const [showSecret, setShowSecret] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null)
  const [showIpsModal, setShowIpsModal] = useState<{ type: 'account' | 'all'; id?: number; name?: string } | null>(null)

  useEffect(() => { fetchAccounts(); fetchWorkerStatuses() }, [fetchAccounts, fetchWorkerStatuses])

  useEffect(() => {
    const interval = setInterval(fetchWorkerStatuses, 3000)
    return () => clearInterval(interval)
  }, [fetchWorkerStatuses])

  const emptyForm = { username: '', password: '', domain: '', project_id: '', label: '', proxy: '' }

  const handleAddAccount = async (e: FormEvent) => {
    e.preventDefault()
    if (!accountForm.username.trim() || !accountForm.password.trim() || !accountForm.domain.trim() || !accountForm.project_id.trim()) return
    const result = await createAccount({
      username: accountForm.username.trim(),
      password: accountForm.password,
      domain: accountForm.domain.trim(),
      project_id: accountForm.project_id.trim(),
      label: accountForm.label.trim() || undefined,
      proxy: accountForm.proxy.trim() || undefined,
    })
    if (result) {
      setAccountForm(emptyForm)
      setShowAddAccount(false)
    }
  }

  const handleStartEdit = (acc: typeof accounts[number]) => {
    setEditingAccount(acc.id)
    setAccountForm({
      username: acc.username,
      password: '',
      domain: acc.domain,
      project_id: acc.project_id,
      label: acc.label,
      proxy: acc.proxy || '',
    })
    setShowSecret(false)
  }

  const handleSaveEdit = async (e: FormEvent, id: number) => {
    e.preventDefault()
    const patch: Record<string, string | undefined> = {
      username: accountForm.username.trim(),
      domain: accountForm.domain.trim(),
      project_id: accountForm.project_id.trim(),
      label: accountForm.label.trim() || undefined,
      proxy: accountForm.proxy.trim() || undefined,
    }
    if (accountForm.password) patch.password = accountForm.password
    const result = await updateAccount(id, patch)
    if (result) {
      setEditingAccount(null)
      setAccountForm(emptyForm)
    }
  }

  const openIpsModal = (type: 'account' | 'all', id?: number, name?: string) => {
    setShowIpsModal({ type, id, name })
    fetchCaughtIps()
  }

  const visibleIps = showIpsModal?.type === 'all'
    ? caughtIps
    : caughtIps.filter(ip => ip.account_id === showIpsModal?.id)

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm flex-1">{error}</span>
            <button onClick={clearError} className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div className="flex items-center justify-between" variants={itemVariants}>
        <div className="flex items-center gap-3">
          <button onClick={() => openIpsModal('all', undefined, 'Все аккаунты')}
            className="px-3 py-1.5 rounded-lg bg-dark-800/50 hover:bg-dark-700/50 text-sm text-dark-200 flex items-center gap-2 border border-dark-700/50">
            <Eye className="w-4 h-4" /> Все пойманные IP
          </button>
        </div>
        <motion.button onClick={() => setShowAddAccount(v => !v)}
          className="btn btn-primary flex items-center gap-2"
          whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
          <Plus className="w-4 h-4" /> Добавить аккаунт
        </motion.button>
      </motion.div>

      <AnimatePresence>
        {showAddAccount && (
          <motion.form onSubmit={handleAddAccount} className="card space-y-3"
            initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
            <h4 className="font-medium text-dark-100">Новый аккаунт Selectel</h4>
            <div className="grid grid-cols-2 gap-3">
              <input type="text" placeholder="Метка (необязательно)" value={accountForm.label}
                onChange={e => setAccountForm(f => ({ ...f, label: e.target.value }))}
                className="input" autoComplete="off" />
              <input type="text" placeholder="Account ID (домен)" value={accountForm.domain}
                onChange={e => setAccountForm(f => ({ ...f, domain: e.target.value }))}
                className="input font-mono" autoComplete="off" required />
            </div>
            <input type="text" placeholder="project_id" value={accountForm.project_id}
              onChange={e => setAccountForm(f => ({ ...f, project_id: e.target.value }))}
              className="input font-mono" autoComplete="off" required />
            <input type="text" placeholder="Сервисный пользователь (username)" value={accountForm.username}
              onChange={e => setAccountForm(f => ({ ...f, username: e.target.value }))}
              className="input font-mono" autoComplete="off" required />
            <div className="relative">
              <SecretInput revealed={showSecret} placeholder="Пароль" value={accountForm.password}
                onChange={e => setAccountForm(f => ({ ...f, password: e.target.value }))}
                className="input font-mono pr-10" required />
              <button type="button" onClick={() => setShowSecret(s => !s)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
                {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
            <input type="text" placeholder="proxy (socks5://user:pass@host:port, необязательно)" value={accountForm.proxy}
              onChange={e => setAccountForm(f => ({ ...f, proxy: e.target.value }))}
              className="input font-mono" autoComplete="off" />
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setShowAddAccount(false)} className="btn btn-secondary">Отмена</button>
              <button type="submit" disabled={isSaving} className="btn btn-primary flex items-center gap-2">
                {isSaving && <Loader2 className="w-4 h-4 animate-spin" />} Создать
              </button>
            </div>
          </motion.form>
        )}
      </AnimatePresence>

      {isLoadingAccounts ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 text-cyan-400 animate-spin" />
        </div>
      ) : accounts.length === 0 ? (
        <div className="card text-center py-12">
          <Wallet className="w-12 h-12 text-dark-500 mx-auto mb-3" />
          <p className="text-dark-300 text-sm">Аккаунтов пока нет</p>
          <p className="text-dark-500 text-sm mt-1">Добавьте аккаунт Selectel чтобы начать</p>
        </div>
      ) : (
        <div className="space-y-2">
          {accounts.map(acc => {
            const status = workerStatuses[acc.id]
            const isRunning = status && (status.status === 'running' || status.status === 'proxy_waiting')
            const hasError = accountErrors.some(e => e.account_id === acc.id && !e.resolved)
            const isEditing = editingAccount === acc.id
            return (
              <motion.div key={acc.id} className="card"
                initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
                {isEditing ? (
                  <form onSubmit={(e) => handleSaveEdit(e, acc.id)} className="space-y-3">
                    <div className="grid grid-cols-2 gap-3">
                      <input type="text" placeholder="Метка" value={accountForm.label}
                        onChange={e => setAccountForm(f => ({ ...f, label: e.target.value }))}
                        className="input" autoComplete="off" />
                      <input type="text" placeholder="Account ID (домен)" value={accountForm.domain}
                        onChange={e => setAccountForm(f => ({ ...f, domain: e.target.value }))}
                        className="input font-mono" autoComplete="off" required />
                    </div>
                    <input type="text" placeholder="project_id" value={accountForm.project_id}
                      onChange={e => setAccountForm(f => ({ ...f, project_id: e.target.value }))}
                      className="input font-mono" autoComplete="off" required />
                    <input type="text" placeholder="username" value={accountForm.username}
                      onChange={e => setAccountForm(f => ({ ...f, username: e.target.value }))}
                      className="input font-mono" autoComplete="off" required />
                    <div className="relative">
                      <SecretInput revealed={showSecret}
                        placeholder="Пароль (пусто — оставить прежний)" value={accountForm.password}
                        onChange={e => setAccountForm(f => ({ ...f, password: e.target.value }))}
                        className="input font-mono pr-10" />
                      <button type="button" onClick={() => setShowSecret(s => !s)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
                        {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <input type="text" placeholder="proxy" value={accountForm.proxy}
                      onChange={e => setAccountForm(f => ({ ...f, proxy: e.target.value }))}
                      className="input font-mono" autoComplete="off" />
                    <div className="flex justify-end gap-2">
                      <button type="button" onClick={() => { setEditingAccount(null); setAccountForm(emptyForm) }}
                        className="btn btn-secondary">Отмена</button>
                      <button type="submit" disabled={isSaving} className="btn btn-primary flex items-center gap-2">
                        {isSaving && <Loader2 className="w-4 h-4 animate-spin" />} Сохранить
                      </button>
                    </div>
                  </form>
                ) : (
                  <div className="flex items-center justify-between gap-3 flex-wrap">
                    <div className="flex items-center gap-3 min-w-0">
                      <div className={`w-2.5 h-2.5 rounded-full ${
                        isRunning ? (hasError ? 'bg-amber-400' : 'bg-cyan-400 animate-pulse')
                                  : (acc.enabled ? 'bg-dark-500' : 'bg-dark-700')
                      }`} />
                      <div className="min-w-0">
                        <div className="font-medium text-dark-100 truncate">
                          {acc.label || `selectel-${acc.id}`}
                          <span className="ml-2 text-xs text-dark-500 font-mono">{acc.project_id}</span>
                        </div>
                        <div className="text-xs text-dark-500 font-mono truncate">
                          {acc.username} · {acc.domain}
                          {acc.proxy && <span className="ml-2 text-cyan-400/70">proxy</span>}
                        </div>
                        {hasError && (
                          <div className="text-xs text-amber-400 mt-1 flex items-center gap-1">
                            <AlertCircle className="w-3.5 h-3.5" />
                            {accountErrors.find(e => e.account_id === acc.id && !e.resolved)?.error_type}
                            <button onClick={() => clearAccountErrors(acc.id)} className="ml-1 hover:text-amber-300">
                              <X className="w-4 h-4" />
                            </button>
                          </div>
                        )}
                        {status && (
                          <div className="text-xs text-dark-500 mt-0.5">
                            Раунд {status.current_round || 0} · попыток {status.attempts} · поймано {status.caught}
                          </div>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      <button onClick={() => openIpsModal('account', acc.id, acc.label || `selectel-${acc.id}`)}
                        className="px-2.5 py-1 rounded-lg bg-dark-800/50 hover:bg-dark-700/50 text-xs text-dark-200 flex items-center gap-1">
                        <Eye className="w-3.5 h-3.5" /> {acc.caught_ip_count}
                      </button>
                      {isRunning ? (
                        <button onClick={() => stopAccount(acc.id)} disabled={isSaving}
                          className="px-3 py-1.5 rounded-lg bg-dark-800 text-dark-200 hover:bg-dark-700 flex items-center gap-1 text-sm">
                          <Square className="w-3.5 h-3.5" /> Stop
                        </button>
                      ) : (
                        <button onClick={() => startAccount(acc.id)} disabled={isSaving || !acc.enabled}
                          className="px-3 py-1.5 rounded-lg bg-cyan-500/20 text-cyan-300 hover:bg-cyan-500/30 disabled:opacity-40 flex items-center gap-1 text-sm">
                          <Play className="w-3.5 h-3.5" /> Start
                        </button>
                      )}
                      <button onClick={() => updateAccount(acc.id, { enabled: !acc.enabled })}
                        className={`px-2.5 py-1.5 rounded-lg text-sm ${acc.enabled ? 'bg-dark-800 text-dark-300' : 'bg-dark-900 text-dark-500'}`}>
                        <Power className="w-4 h-4" />
                      </button>
                      <button onClick={() => handleStartEdit(acc)}
                        className="px-2.5 py-1.5 rounded-lg bg-dark-800 text-dark-300 hover:bg-dark-700">
                        <Edit2 className="w-4 h-4" />
                      </button>
                      <button onClick={() => setDeleteConfirm(acc.id)}
                        className="px-2.5 py-1.5 rounded-lg bg-danger/10 text-danger hover:bg-danger/20">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                )}
              </motion.div>
            )
          })}
        </div>
      )}

      <AnimatePresence>
        {deleteConfirm !== null && (
          <motion.div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={() => setDeleteConfirm(null)}>
            <motion.div className="card max-w-sm w-full"
              initial={{ scale: 0.95 }} animate={{ scale: 1 }} exit={{ scale: 0.95 }}
              onClick={e => e.stopPropagation()}>
              <h4 className="font-medium text-dark-100 mb-2">Удалить аккаунт?</h4>
              <p className="text-sm text-dark-400 mb-4">Удалятся также все пойманные IP.</p>
              <div className="flex justify-end gap-2">
                <button onClick={() => setDeleteConfirm(null)} className="btn btn-secondary">Отмена</button>
                <button onClick={async () => { await deleteAccount(deleteConfirm); setDeleteConfirm(null) }}
                  disabled={isSaving} className="btn bg-danger/20 text-danger hover:bg-danger/30">
                  Удалить
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {showIpsModal && (
          <motion.div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={() => setShowIpsModal(null)}>
            <motion.div className="card max-w-2xl w-full max-h-[85vh] overflow-hidden flex flex-col"
              initial={{ scale: 0.95 }} animate={{ scale: 1 }} exit={{ scale: 0.95 }}
              onClick={e => e.stopPropagation()}>
              <div className="flex items-center justify-between mb-3">
                <h4 className="font-medium text-dark-100">
                  Пойманные IP — {showIpsModal.name || 'Все'}
                </h4>
                <div className="flex items-center gap-2">
                  {showIpsModal.type === 'all' && visibleIps.length > 0 && (
                    <button onClick={async () => { await clearCaughtIps(); setShowIpsModal(null) }}
                      className="btn-tool btn-tool-danger">
                      Очистить все
                    </button>
                  )}
                  {showIpsModal.type === 'account' && showIpsModal.id && visibleIps.length > 0 && (
                    <button onClick={async () => { await clearAccountCaughtIps(showIpsModal.id!); setShowIpsModal(null) }}
                      className="btn-tool btn-tool-danger">
                      Очистить
                    </button>
                  )}
                  <button onClick={() => setShowIpsModal(null)} className="p-1.5 hover:bg-dark-700/50 rounded">
                    <X className="w-4 h-4 text-dark-400" />
                  </button>
                </div>
              </div>
              {isLoadingCaughtIps ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="w-6 h-6 text-cyan-400 animate-spin" />
                </div>
              ) : visibleIps.length === 0 ? (
                <p className="text-center text-dark-500 text-sm py-8">Пойманных IP пока нет</p>
              ) : (
                <div className="overflow-auto space-y-1">
                  {visibleIps.map(ip => (
                    <div key={ip.id} className="flex items-center justify-between gap-2 p-2 rounded bg-dark-800/40">
                      <div className="font-mono text-sm text-cyan-300">{ip.ip_address}</div>
                      <div className="text-xs text-dark-500 flex-1 truncate">
                        {ip.region} · {ip.account_label || `id=${ip.account_id}`}
                      </div>
                      <button onClick={() => { navigator.clipboard.writeText(ip.ip_address) }}
                        className="p-1.5 text-dark-400 hover:text-dark-200" title="Скопировать">
                        <Copy className="w-4 h-4" />
                      </button>
                      <button onClick={() => deleteCaughtIp(ip.id)}
                        className="p-1.5 text-dark-500 hover:text-danger" title="Удалить">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// SELECTEL — MANUAL TAB
// ═══════════════════════════════════════════════════════════════════════

function SelectelManualTab() {
  const { accounts, fetchAccounts, manualObtainIp, manualObtainHistory, isObtaining, clearManualHistory } = useSelectelStore()
  const [accountId, setAccountId] = useState<number | null>(null)
  const [region, setRegion] = useState<string>(SELECTEL_DEFAULT_REGIONS[2])

  useEffect(() => { fetchAccounts() }, [fetchAccounts])

  useEffect(() => {
    if (accountId === null && accounts.length > 0) setAccountId(accounts[0].id)
  }, [accounts, accountId])

  const handleObtain = async () => {
    if (accountId === null) return
    const acc = accounts.find(a => a.id === accountId)
    const label = acc?.label || `selectel-${accountId}`
    const req: SelectelManualObtainRequest = { account_id: accountId, region }
    await manualObtainIp(req, label, region)
  }

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      <motion.div className="card space-y-4" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2">
          <Target className="w-5 h-5 text-cyan-400" /> Получить один IP
        </h3>

        <div>
          <label className="block text-sm text-dark-400 mb-1">Аккаунт</label>
          <select value={accountId ?? ''} onChange={e => setAccountId(Number(e.target.value))}
            className="input">
            {accounts.length === 0 && <option value="">Нет аккаунтов</option>}
            {accounts.map(a => (
              <option key={a.id} value={a.id}>
                {a.label || `selectel-${a.id}`} · {a.project_id}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm text-dark-400 mb-2">Регион</label>
          <div className="grid grid-cols-3 gap-3">
            {SELECTEL_DEFAULT_REGIONS.map(r => (
              <motion.button key={r} onClick={() => setRegion(r)}
                className={`px-6 py-4 rounded-xl border-2 text-lg font-mono font-bold transition-all ${
                  region === r
                    ? 'bg-cyan-500/20 border-cyan-500/60 text-cyan-300 shadow-lg shadow-cyan-500/10'
                    : 'bg-dark-800/50 border-dark-700/50 text-dark-400 hover:border-dark-600'
                }`}
                whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                {r}
              </motion.button>
            ))}
          </div>
        </div>

        <div className="flex justify-end">
          <motion.button onClick={handleObtain} disabled={isObtaining || accountId === null}
            className="btn btn-primary flex items-center gap-2"
            whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
            {isObtaining ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
            Получить IP
          </motion.button>
        </div>
      </motion.div>

      {manualObtainHistory.length > 0 && (
        <motion.div className="card" variants={itemVariants}>
          <div className="flex items-center justify-between mb-3">
            <h4 className="font-medium text-dark-100">История</h4>
            <button onClick={clearManualHistory} className="btn-tool">
              Очистить
            </button>
          </div>
          <div className="space-y-2">
            {manualObtainHistory.map(entry => (
              <div key={entry.id} className="flex items-center gap-3 p-2 rounded bg-dark-800/40 text-sm">
                <div className="flex-shrink-0">
                  {entry.success
                    ? <CheckCircle2 className="w-4 h-4 text-cyan-400" />
                    : <AlertCircle className="w-4 h-4 text-danger" />}
                </div>
                <div className="text-xs text-dark-500 w-32 flex-shrink-0">{entry.timestamp}</div>
                <div className="text-xs text-dark-400 flex-shrink-0">{entry.accountLabel} · {entry.region}</div>
                <div className="flex-1 font-mono text-cyan-300 truncate">
                  {entry.success ? entry.ipAddress : entry.error}
                </div>
                {entry.success && entry.ipAddress && (
                  <button onClick={() => navigator.clipboard.writeText(entry.ipAddress!)}
                    className="p-1.5 text-dark-400 hover:text-dark-200">
                    <Copy className="w-4 h-4" />
                  </button>
                )}
              </div>
            ))}
          </div>
        </motion.div>
      )}
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// SELECTEL — MULTIQUEUE TAB
// ═══════════════════════════════════════════════════════════════════════

interface SelectelRcFileData {
  auth_url: string
  project_id: string
  region: string
  auth_type: 'password' | 'v3token'
  username: string
  user_domain_name: string
}

function parseSelectelRcFile(content: string): SelectelRcFileData | null {
  const get = (key: string): string => {
    const m = content.match(new RegExp(`(?:export\\s+)?${key}=["']?([^"'\\n]+)["']?`))
    return m ? m[1].trim() : ''
  }
  const auth_url = get('OS_AUTH_URL')
  const project_id = get('OS_PROJECT_ID')
  if (!auth_url || !project_id) return null

  const region = get('OS_REGION_NAME')
  const is_token = get('OS_AUTH_TYPE') === 'v3token' || content.includes('OS_TOKEN')
  const auth_type = is_token ? 'v3token' as const : 'password' as const
  const username = get('OS_USERNAME')
  const user_domain_name = get('OS_USER_DOMAIN_NAME')

  if (auth_type === 'password' && !username) return null
  return { auth_url, project_id, region, auth_type, username, user_domain_name }
}

function SelectelMultiqueueTab() {
  const [rcData, setRcData] = useState<SelectelRcFileData | null>(null)
  const [rcFileName, setRcFileName] = useState<string>('')
  const [secret, setSecret] = useState('')
  const [imageId, setImageId] = useState('')
  const [proxy, setProxy] = useState('')
  const [showSecret, setShowSecret] = useState(false)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<{ success: boolean; error?: string; image_name?: string } | null>(null)
  const [parseError, setParseError] = useState<string>('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const processFile = (file: File) => {
    setRcFileName(file.name)
    setParseError('')
    setResult(null)
    const reader = new FileReader()
    reader.onload = (ev) => {
      const parsed = parseSelectelRcFile(ev.target?.result as string)
      if (parsed) {
        setRcData(parsed)
      } else {
        setRcData(null)
        setParseError('Не удалось распарсить RC-файл. Нужен OS_AUTH_URL и OS_PROJECT_ID.')
      }
    }
    reader.readAsText(file)
  }

  const handleReset = () => {
    setRcData(null)
    setRcFileName('')
    setSecret('')
    setImageId('')
    setProxy('')
    setResult(null)
    setParseError('')
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const handleSubmit = async () => {
    if (!rcData || !secret || !imageId.trim()) return
    setLoading(true)
    setResult(null)
    try {
      const resp = await selectelApi.enableMultiqueue({
        auth_url: rcData.auth_url,
        project_id: rcData.project_id,
        image_id: imageId.trim(),
        auth_type: rcData.auth_type,
        ...(rcData.region ? { region: rcData.region } : {}),
        ...(proxy.trim() ? { proxy: proxy.trim() } : {}),
        ...(rcData.auth_type === 'v3token'
          ? { api_token: secret }
          : { username: rcData.username, user_domain_name: rcData.user_domain_name, password: secret }
        ),
      })
      setResult(resp.data)
    } catch (err: any) {
      setResult({ success: false, error: err.response?.data?.detail || err.response?.data?.error || 'Ошибка запроса' })
    } finally {
      setLoading(false)
    }
  }

  const isTokenMode = rcData?.auth_type === 'v3token'
  const canSubmit = rcData && secret && imageId.trim() && !loading

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-4">
      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-1">
          <Cpu className="w-5 h-5 text-cyan-400" /> Включение Multiqueue
        </h3>
        <p className="text-xs text-dark-500 mb-4">
          Устанавливает <code className="text-dark-300">hw_vif_multiqueue_enabled=true</code> на образе Selectel.
          Данные не сохраняются — одноразовое выполнение.
        </p>

        <div className="mb-4">
          <label className="block text-sm text-dark-400 mb-2">RC-файл (rc.sh)</label>
          {!rcData ? (
            <div
              onDrop={e => { e.preventDefault(); const f = e.dataTransfer.files?.[0]; if (f) processFile(f) }}
              onDragOver={e => e.preventDefault()}
              onClick={() => fileInputRef.current?.click()}
              className="border-2 border-dashed border-dark-700 hover:border-cyan-500/50 rounded-xl p-8 text-center cursor-pointer transition-colors"
            >
              <Upload className="w-8 h-8 text-dark-500 mx-auto mb-2" />
              <p className="text-sm text-dark-400">Перетащите RC-файл сюда или нажмите для выбора</p>
              <p className="text-xs text-dark-500 mt-1">my.selectel.ru → IAM → Сервисные пользователи → выбрать пользователя → Доступ → скачать RC-файл</p>
              <input ref={fileInputRef} type="file" accept=".sh,.bash,.txt"
                onChange={e => { const f = e.target.files?.[0]; if (f) processFile(f) }} className="hidden" />
            </div>
          ) : (
            <div className="bg-dark-800/50 border border-dark-700/50 rounded-xl p-4">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="w-4 h-4 text-green-400" />
                  <span className="text-sm text-dark-200">{rcFileName}</span>
                  <span className={`text-xs px-1.5 py-0.5 rounded ${isTokenMode ? 'bg-cyan-500/20 text-cyan-400' : 'bg-amber-500/20 text-amber-400'}`}>
                    {isTokenMode ? 'Token' : 'Password'}
                  </span>
                </div>
                <button onClick={handleReset} className="btn-tool">
                  <X className="w-4 h-4" /> Сбросить
                </button>
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div>
                  <span className="text-dark-500">Auth URL:</span>
                  <span className="text-dark-300 ml-1 font-mono break-all">{rcData.auth_url}</span>
                </div>
                <div>
                  <span className="text-dark-500">Project ID:</span>
                  <span className="text-dark-300 ml-1 font-mono">{rcData.project_id}</span>
                </div>
                {rcData.region && (
                  <div>
                    <span className="text-dark-500">Region:</span>
                    <span className="text-dark-300 ml-1 font-mono">{rcData.region}</span>
                  </div>
                )}
                {!isTokenMode && rcData.username && (
                  <>
                    <div>
                      <span className="text-dark-500">Username:</span>
                      <span className="text-dark-300 ml-1">{rcData.username}</span>
                    </div>
                    <div>
                      <span className="text-dark-500">Domain:</span>
                      <span className="text-dark-300 ml-1">{rcData.user_domain_name}</span>
                    </div>
                  </>
                )}
              </div>
              {!rcData.region && (
                <p className="text-xs text-amber-400/80 mt-2 flex items-center gap-1">
                  <AlertTriangle className="w-3.5 h-3.5" /> В RC нет OS_REGION_NAME — Glance-эндпоинт возьмём из каталога.
                </p>
              )}
            </div>
          )}
          {parseError && (
            <p className="text-xs text-danger mt-2 flex items-center gap-1">
              <AlertTriangle className="w-3.5 h-3.5" /> {parseError}
            </p>
          )}
        </div>

        <div className="mb-4">
          <label className="block text-sm text-dark-400 mb-1">
            {isTokenMode ? 'API токен' : 'Пароль'}
          </label>
          <div className="relative">
            <SecretInput
              revealed={showSecret}
              value={secret}
              onChange={e => setSecret(e.target.value)}
              className="input pr-10"
              placeholder={isTokenMode ? 'API токен' : 'Пароль сервисного пользователя'}
              disabled={!rcData}
            />
            <button
              onClick={() => setShowSecret(v => !v)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-dark-500 hover:text-dark-300"
              type="button"
            >
              {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
        </div>

        <div className="mb-4">
          <label className="block text-sm text-dark-400 mb-1">ID образа</label>
          <input
            type="text"
            value={imageId}
            onChange={e => setImageId(e.target.value)}
            className="input font-mono text-sm"
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            disabled={!rcData}
          />
        </div>

        <div className="mb-4">
          <label className="block text-sm text-dark-400 mb-1">SOCKS5 Proxy (опционально)</label>
          <input
            type="text"
            value={proxy}
            onChange={e => setProxy(e.target.value)}
            className="input font-mono text-sm"
            placeholder="host:port:user:pass"
            autoComplete="off"
            disabled={!rcData}
          />
          <p className="text-xs text-dark-500 mt-1">Запросы к Keystone и Glance пойдут через прокси, как у аккаунтов.</p>
        </div>

        <motion.button
          onClick={handleSubmit}
          disabled={!canSubmit}
          className="w-full py-2.5 rounded-xl font-medium text-sm flex items-center justify-center gap-2 transition-all bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-400 disabled:opacity-40 disabled:cursor-not-allowed"
          whileHover={canSubmit ? { scale: 1.01 } : {}}
          whileTap={canSubmit ? { scale: 0.99 } : {}}
        >
          {loading ? (
            <><Loader2 className="w-4 h-4 animate-spin" /> Выполнение...</>
          ) : (
            <><Zap className="w-4 h-4" /> Включить Multiqueue</>
          )}
        </motion.button>
      </motion.div>

      <AnimatePresence>
        {result && (
          <motion.div
            className={`card border ${result.success ? 'border-green-500/30 bg-green-500/5' : 'border-danger/30 bg-danger/5'}`}
            initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}
          >
            <div className="flex items-center gap-2">
              {result.success ? (
                <CheckCircle2 className="w-5 h-5 text-green-400 flex-shrink-0" />
              ) : (
                <AlertTriangle className="w-5 h-5 text-danger flex-shrink-0" />
              )}
              <div>
                {result.success ? (
                  <div>
                    <p className="text-sm text-green-300 font-medium">Multiqueue включён</p>
                    {result.image_name && (
                      <p className="text-xs text-dark-400 mt-0.5">Образ: {result.image_name}</p>
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-danger">{result.error}</p>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// SELECTEL — SETTINGS TAB
// ═══════════════════════════════════════════════════════════════════════

function SelectelSettingsTab() {
  const {
    settings, isLoadingSettings, isSaving, fetchSettings, updateSettings, error, clearError,
    whitelist, fetchWhitelist, addWhitelistCidrs, removeWhitelistCidr, clearWhitelist,
  } = useSelectelStore()
  const [formData, setFormData] = useState<any>(null)
  const [hasChanges, setHasChanges] = useState(false)
  const [showToken, setShowToken] = useState(false)
  const [bulkCidrs, setBulkCidrs] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)
  const [bulkResult, setBulkResult] = useState<string | null>(null)
  const [confirmClearWl, setConfirmClearWl] = useState(false)

  useEffect(() => { fetchSettings(); fetchWhitelist() }, [fetchSettings, fetchWhitelist])

  useEffect(() => {
    if (settings && !formData) {
      setFormData({
        delay_burst: settings.delay_burst,
        delay_round: settings.delay_round,
        delay_delete: settings.delay_delete,
        jitter: settings.jitter,
        provision_timeout: settings.provision_timeout,
        provision_poll: settings.provision_poll,
        regions: settings.regions,
        telegram_bot_token: settings.telegram_bot_token,
        telegram_chat_id: settings.telegram_chat_id,
        proxy_required: settings.proxy_required,
        log_level: settings.log_level || 'info',
      })
    }
  }, [settings, formData])

  const handleChange = (field: string, value: any) => {
    setFormData((f: any) => f ? { ...f, [field]: value } : f)
    setHasChanges(true)
  }

  const toggleRegion = (region: string) => {
    if (!formData) return
    const current = formData.regions as string[]
    const next = current.includes(region) ? current.filter((r: string) => r !== region) : [...current, region]
    if (next.length === 0) return
    handleChange('regions', next)
  }

  const handleSave = async () => {
    if (!formData) return
    const success = await updateSettings(formData)
    if (success) setHasChanges(false)
  }

  const handleBulkAdd = async () => {
    const lines = bulkCidrs.split(/[\s,]+/).map(s => s.trim()).filter(Boolean)
    if (lines.length === 0) return
    setBulkBusy(true)
    setBulkResult(null)
    // Вся пачка — одним запросом: не спамим бэкенд и не дёргаем список на каждой строке
    const res = await addWhitelistCidrs(lines)
    setBulkBusy(false)
    if (!res) { setBulkResult('Не удалось добавить'); return }
    const parts = [`Добавлено ${res.added}`]
    if (res.skipped) parts.push(`уже были ${res.skipped}`)
    if (res.invalid.length) parts.push(`некорректных ${res.invalid.length}`)
    setBulkResult(parts.join(', '))
    if (res.invalid.length === 0) setBulkCidrs('')
  }

  const handleClearWhitelist = async () => {
    await clearWhitelist()
    setConfirmClearWl(false)
    setBulkResult(null)
  }

  if (isLoadingSettings || !formData) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-8 h-8 text-cyan-400 animate-spin" />
      </div>
    )
  }

  const availableRegions = settings?.available_regions || SELECTEL_DEFAULT_REGIONS

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-6">
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm flex-1">{error}</span>
            <button onClick={clearError} className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Clock className="w-5 h-5 text-cyan-400" /> Задержки (сек)
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Burst (между POST)</label>
            <input type="number" step="0.1" min={0} value={formData.delay_burst}
              onChange={e => handleChange('delay_burst', parseFloat(e.target.value) || 0)}
              className="input font-mono" />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Round (между раундами)</label>
            <input type="number" step="0.1" min={0} value={formData.delay_round}
              onChange={e => handleChange('delay_round', parseFloat(e.target.value) || 0)}
              className="input font-mono" />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Delete (перед DELETE)</label>
            <input type="number" step="0.1" min={0} value={formData.delay_delete}
              onChange={e => handleChange('delay_delete', parseFloat(e.target.value) || 0)}
              className="input font-mono" />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Jitter (±рандом)</label>
            <input type="number" step="0.1" min={0} value={formData.jitter}
              onChange={e => handleChange('jitter', parseFloat(e.target.value) || 0)}
              className="input font-mono" />
          </div>
        </div>
        <p className="text-xs text-dark-500 mt-2">
          Каждая задержка получает рандомное отклонение от −jitter до +jitter (но не меньше нуля).
        </p>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <RefreshCw className="w-5 h-5 text-cyan-400" /> Provisioning
        </h3>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Таймаут удаления (сек)</label>
            <input type="number" min={5} max={600} value={formData.provision_timeout}
              onChange={e => handleChange('provision_timeout', parseInt(e.target.value) || 60)}
              className="input font-mono" />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Шаг опроса статуса (сек)</label>
            <input type="number" step="0.1" min={0.5} value={formData.provision_poll}
              onChange={e => handleChange('provision_poll', parseFloat(e.target.value) || 2)}
              className="input font-mono" />
          </div>
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Globe className="w-5 h-5 text-cyan-400" /> Регионы (пулы)
        </h3>
        <div className="grid grid-cols-3 gap-3">
          {availableRegions.map(region => {
            const isSelected = (formData.regions as string[]).includes(region)
            return (
              <motion.button key={region} onClick={() => toggleRegion(region)}
                className={`px-6 py-4 rounded-xl border-2 text-lg font-mono font-bold transition-all ${
                  isSelected
                    ? 'bg-cyan-500/20 border-cyan-500/60 text-cyan-300 shadow-lg shadow-cyan-500/10'
                    : 'bg-dark-800/50 border-dark-700/50 text-dark-400 hover:border-dark-600'
                }`}
                whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                {region}
              </motion.button>
            )
          })}
        </div>
        <p className="text-xs text-dark-500 mt-2">
          Воркер ходит по выбранным пулам по очереди (round-robin).
        </p>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-medium text-dark-100 flex items-center gap-2">
            <Network className="w-5 h-5 text-cyan-400" /> Whitelist
          </h3>
          <div className="flex items-center gap-3">
            <span className="text-xs text-dark-500">{whitelist.length} записей</span>
            {whitelist.length > 0 && (
              confirmClearWl ? (
                <span className="flex items-center gap-2 text-xs">
                  <span className="text-dark-400">Очистить всё?</span>
                  <button onClick={handleClearWhitelist} disabled={isSaving}
                    className="text-danger hover:text-danger/80 font-medium">Да</button>
                  <button onClick={() => setConfirmClearWl(false)} className="text-dark-400 hover:text-dark-200">Нет</button>
                </span>
              ) : (
                <button onClick={() => setConfirmClearWl(true)}
                  className="text-xs text-danger hover:text-danger/80 flex items-center gap-1">
                  <Trash2 className="w-3.5 h-3.5" /> Очистить всё
                </button>
              )
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-2 mb-3">
          {whitelist.length === 0 ? (
            <span className="text-xs text-dark-500 italic">Пусто — добавьте подсети или отдельные IP для отлова попаданий</span>
          ) : whitelist.map(w => (
            <span key={w.id} className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-cyan-500/10 border border-cyan-500/30 text-sm font-mono text-cyan-300">
              {w.cidr}
              <button onClick={() => removeWhitelistCidr(w.id)} className="text-dark-400 hover:text-danger">
                <X className="w-4 h-4" />
              </button>
            </span>
          ))}
        </div>
        <textarea value={bulkCidrs} onChange={e => setBulkCidrs(e.target.value)}
          placeholder="45.9.24.66&#10;188.68.0.0/16&#10;5.101.0.0/16"
          className="input font-mono h-24" />
        <div className="flex items-center justify-between mt-2">
          <span className="text-xs text-dark-500">
            {bulkResult || 'Подсети (CIDR) и/или отдельные IP — по строке, через запятую или пробел'}
          </span>
          <button onClick={handleBulkAdd} disabled={bulkBusy || !bulkCidrs.trim()}
            className="btn btn-primary flex items-center gap-2 disabled:opacity-40">
            {bulkBusy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
            Добавить
          </button>
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Send className="w-5 h-5 text-cyan-400" /> Telegram уведомления
        </h3>
        <div className="space-y-3">
          <div className="relative">
            <SecretInput revealed={showToken} value={formData.telegram_bot_token}
              onChange={e => handleChange('telegram_bot_token', e.target.value)}
              className="input font-mono pr-10" placeholder="Bot Token (123456789:ABC…)" />
            <button type="button" onClick={() => setShowToken(s => !s)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
              {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
          <input type="text" value={formData.telegram_chat_id}
            onChange={e => handleChange('telegram_chat_id', e.target.value)}
            className="input font-mono" placeholder="Chat ID" autoComplete="off" />
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <ScrollText className="w-5 h-5 text-cyan-400" /> Уровень логирования
        </h3>
        <div className="grid grid-cols-2 gap-3">
          {(['info', 'full'] as const).map(level => (
            <motion.button key={level} onClick={() => handleChange('log_level', level)}
              className={`p-4 rounded-xl border text-left transition-all ${
                formData.log_level === level
                  ? 'bg-cyan-500/20 border-cyan-500/40'
                  : 'bg-dark-800/50 border-dark-700/50 hover:border-dark-600'
              }`}
              whileHover={{ scale: 1.01 }} whileTap={{ scale: 0.99 }}>
              <div className="flex items-center gap-2 mb-1">
                <div className={`w-3 h-3 rounded-full ${formData.log_level === level ? 'bg-cyan-400' : 'bg-dark-600'}`} />
                <span className={`font-medium ${formData.log_level === level ? 'text-cyan-300' : 'text-dark-300'}`}>
                  {level.toUpperCase()}
                </span>
              </div>
              <p className="text-xs text-dark-400">
                {level === 'info' ? 'Только ключевые события раундов' : 'Подробный лог каждой попытки'}
              </p>
            </motion.button>
          ))}
        </div>
      </motion.div>

      <motion.div className="card" variants={itemVariants}>
        <h3 className="font-medium text-dark-100 flex items-center gap-2 mb-4">
          <Settings className="w-5 h-5 text-cyan-400" /> Дополнительно
        </h3>
        <label className="flex items-center gap-3 cursor-pointer">
          <Checkbox checked={formData.proxy_required}
            onChange={e => handleChange('proxy_required', e.target.checked)} />
          <span className="text-sm text-dark-300">Proxy обязательно (не запускать аккаунт без proxy)</span>
        </label>
      </motion.div>

      <motion.div className="flex justify-end" variants={itemVariants}>
        <motion.button onClick={handleSave} disabled={!hasChanges || isSaving}
          className={`btn ${hasChanges ? 'btn-primary' : 'btn-secondary opacity-50'} flex items-center gap-2`}
          whileHover={hasChanges ? { scale: 1.02 } : {}} whileTap={hasChanges ? { scale: 0.98 } : {}}>
          {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          Сохранить
        </motion.button>
      </motion.div>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// SELECTEL — LOGS TAB
// ═══════════════════════════════════════════════════════════════════════

function SelectelLogsTab() {
  const { logs, fetchLogs, clearLogs, subscribeToLogs, unsubscribeFromLogs } = useSelectelStore()
  const scrollRef = useRef<HTMLDivElement>(null)
  const [autoScroll, setAutoScroll] = useState(true)

  useEffect(() => {
    fetchLogs(500)
    subscribeToLogs()
    return () => unsubscribeFromLogs()
  }, [fetchLogs, subscribeToLogs, unsubscribeFromLogs])

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [logs, autoScroll])

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs text-dark-500">{logs.length} строк</div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 cursor-pointer text-sm text-dark-300">
            <Checkbox checked={autoScroll} onChange={e => setAutoScroll(e.target.checked)} />
            автоскролл
          </label>
          <button onClick={async () => { await clearLogs() }}
            className="text-sm text-danger hover:text-danger/80">Очистить</button>
        </div>
      </div>
      <div ref={scrollRef}
        className="card font-mono text-xs text-dark-300 h-[60vh] overflow-y-auto whitespace-pre-wrap">
        {logs.length === 0 ? (
          <div className="text-dark-500 italic">Логов нет. Запустите воркер аккаунта.</div>
        ) : logs.map((line, i) => <div key={i}>{line}</div>)}
      </div>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// SELECTEL — PAGE
// ═══════════════════════════════════════════════════════════════════════

function SelectelPage() {
  const [activeTab, setActiveTab] = useState<SelectelTabType>('accounts')

  const tabs: { id: SelectelTabType; label: string; icon: React.ReactNode }[] = [
    { id: 'accounts', label: 'Аккаунты', icon: <Users className="w-4 h-4" /> },
    { id: 'manual', label: 'Ручной IP', icon: <Target className="w-4 h-4" /> },
    { id: 'stats', label: 'Статистика', icon: <BarChart3 className="w-4 h-4" /> },
    { id: 'multiqueue', label: 'Multiqueue', icon: <Cpu className="w-4 h-4" /> },
    { id: 'settings', label: 'Настройки', icon: <Settings className="w-4 h-4" /> },
    { id: 'logs', label: 'Логи', icon: <ScrollText className="w-4 h-4" /> },
  ]

  return (
    <>
      <motion.div className="flex gap-1 mb-6 bg-dark-800/50 p-1 rounded-xl w-fit" variants={itemVariants}>
        {tabs.map(tab => (
          <motion.button key={tab.id} onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
              activeTab === tab.id
                ? 'bg-cyan-500/20 text-cyan-400'
                : 'text-dark-400 hover:text-dark-200 hover:bg-dark-700/50'
            }`}
            whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
            {tab.icon} {tab.label}
          </motion.button>
        ))}
      </motion.div>

      <AnimatePresence mode="wait">
        <motion.div key={activeTab} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }} transition={{ duration: 0.15 }}>
          {activeTab === 'accounts' && <SelectelAccountsTab />}
          {activeTab === 'manual' && <SelectelManualTab />}
          {activeTab === 'stats' && <SelectelStatsTab />}
          {activeTab === 'multiqueue' && <SelectelMultiqueueTab />}
          {activeTab === 'settings' && <SelectelSettingsTab />}
          {activeTab === 'logs' && <SelectelLogsTab />}
        </motion.div>
      </AnimatePresence>
    </>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// CLOUD.RU — ШИРИНА КАНАЛА (ADVANCED)
// ═══════════════════════════════════════════════════════════════════════

const BW_HOURS = Array.from({ length: 24 }, (_, hour) => hour)
// Потолок полосы EIP в консоли Cloud.ru Advanced
const BW_MAX_MBPS = 2000
// Проверено на ноде за прокси: при полосе 1000 трафик держал ~1 Гбит/с в обе стороны сразу
const BW_DUPLEX_HINT = 'Полоса действует на каждое направление отдельно: при 1000 Мбит/с нода принимает до 1000 и одновременно отдаёт до 1000'
const BW_SHARED_DUPLEX_HINT = 'Общую полосу делят все её адреса: лимит — на их суммарную скорость, на каждое направление отдельно'
const BW_SIZE_HINT = `До 300 Мбит/с — любое значение, от 300 до 1000 — кратно 50, больше 1000 — кратно 500, максимум ${BW_MAX_MBPS}`
// Минимум общей полосы по справочнику Cloud.ru Advanced
const BW_SHARED_MIN_MBPS = 5
const BW_SHARED_SIZE_HINT = `Общая полоса — не меньше ${BW_SHARED_MIN_MBPS} Мбит/с. ${BW_SIZE_HINT}`
const BW_SHARE_TYPE_SHARED = 'WHOLE'
const BW_MIN_SCHEDULE_RULES = 2
const BW_DEFAULT_SCHEDULE: CloudBwScheduleRule[] = [
  { start_hour: 0, end_hour: 8, mbps: 500 },
  { start_hour: 8, end_hour: 24, mbps: 1000 },
]
// Планировщик меняет полосу в начале часа — страница подтягивает состояние раз в минуту
const BW_REFRESH_MS = 60_000
// У .btn нет вида выключенной кнопки — без этого «Сохранить» без изменений выглядит активной
const BW_DISABLED = 'disabled:opacity-40 disabled:cursor-not-allowed disabled:pointer-events-none'
const BW_RULE_COLORS = ['bg-violet-500/60', 'bg-cyan-500/60', 'bg-amber-500/60', 'bg-emerald-500/60', 'bg-pink-500/60', 'bg-sky-500/60']

const BW_MODES: { id: CloudBwMode; label: string; icon: React.ReactNode }[] = [
  { id: 'off', label: 'Выключено', icon: <Power className="w-4 h-4" /> },
  { id: 'schedule', label: 'Расписание', icon: <CalendarClock className="w-4 h-4" /> },
  { id: 'dynamic', label: 'Динамика', icon: <TrendingUp className="w-4 h-4" /> },
]

interface BwServerOption {
  id: number
  name: string
}

interface BwDraft {
  server_id: number | null
  server_ids: number[]
  mode: CloudBwMode
  schedule: CloudBwScheduleRule[]
  dynamic: CloudBwDynamic
}

const formatClock = (hour: number) => `${String(hour).padStart(2, '0')}:00`
const formatBwTime = (ts: string | null) => (ts ? new Date(ts).toLocaleString('ru-RU') : '—')

function bwErrorText(e: any, fallback: string): string {
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  // 422 от pydantic — список ошибок по полям
  if (Array.isArray(detail)) return detail.map((item: { msg?: string }) => item.msg).filter(Boolean).join('; ') || fallback
  return fallback
}

/** Подсветка поля: шаг размера тот же, что проверяет сервер перед отправкой в Cloud.ru */
function isValidBandwidth(mbps: number, shared: boolean): boolean {
  const minMbps = shared ? BW_SHARED_MIN_MBPS : 1
  if (!Number.isInteger(mbps) || mbps < minMbps || mbps > BW_MAX_MBPS) return false
  const step = mbps <= 300 ? 1 : mbps <= 1000 ? 50 : 500
  return mbps % step === 0
}

function ruleHours(rule: CloudBwScheduleRule): number[] {
  if (rule.start_hour === rule.end_hour) return []
  if (rule.end_hour > rule.start_hour) return BW_HOURS.slice(rule.start_hour, rule.end_hour)
  return [...BW_HOURS.slice(rule.start_hour), ...BW_HOURS.slice(0, rule.end_hour)]
}

const draftFromIp = (ip: CloudBwIp): BwDraft => ({
  server_id: ip.server_id,
  server_ids: ip.server_ids,
  mode: ip.mode,
  schedule: ip.schedule.length ? ip.schedule : BW_DEFAULT_SCHEDULE,
  dynamic: ip.dynamic,
})

const CLOUD_BW_POLICY_JSON = JSON.stringify({
  Version: '1.1',
  Statement: [{
    Effect: 'Allow',
    Action: ['vpc:publicIps:list', 'vpc:publicIps:get', 'vpc:bandwidths:get', 'vpc:bandwidths:update'],
  }],
}, null, 2)

const CLOUD_BW_GUIDE_START = `# Как подключить аккаунт Cloud.ru Advanced

Панели нужен **постоянный** ключ отдельного пользователя, которому разрешено только читать публичные IP и менять ширину их канала.

- Ключ главного администратора аккаунта не используйте — он открывает весь аккаунт.
- Временный ключ (с STS Token) живёт не больше суток, после этого панель перестанет менять полосу.

Всё делается в консоли Advanced, в разделе **Identity and Access Management**. Пункты ниже — в меню слева.

## 1. Политика с правами

1. **Permissions** → **Policies/Roles** → **Create Custom Policy**.
2. **Policy Name** — \`panel-bandwidth\`.
3. **Policy View** — переключите на **JSON**, сотрите всё в поле и вставьте текст ниже.
4. **OK**. Поле **Scope** заполнится само: VPC — сервис уровня проекта.`

const CLOUD_BW_GUIDE_END = `Это только чтение адресов и смена ширины их канала: удалить IP, создать новый или тронуть серверы с такими правами нельзя.

Без JSON, кликами: **Select service** → **Virtual Private Cloud (VPC)** (последняя строка списка) → **Select action** → отметьте те же четыре действия → **OK**. Пункты «(Optional)» не трогайте.

## 2. Группа

1. **User Groups** → **Create User Group** → имя \`panel-bandwidth\` → **OK**.
2. В строке группы — **Authorize** → поиском найдите политику \`panel-bandwidth\`, отметьте → **Next**.
3. Область — **Region-specific projects**, отметьте **ru-moscow-1** → **OK**.

## 3. Пользователь и ключ

1. **Users** → **Create User** (зелёная кнопка справа). Имя — например \`panel-bandwidth\`, почту и телефон можно не заполнять.
2. **Access Type** — отметьте только **Programmatic access**, способ входа — **Access key**. **Management console access** не отмечайте.
3. **Next** → на шаге с группами отметьте \`panel-bandwidth\`. Группу **admin** не отмечайте — это полный доступ ко всему аккаунту.
4. На шаге **Finish** скачайте \`credentials.csv\`: в нём **AK** (Access Key Id) и **SK** (Secret Access Key). SK второй раз не покажут.

Группы ещё нет? На шаге с группами ничего не отмечайте и создайте пользователя, затем сделайте раздел 2 и добавьте пользователя в группу: в строке группы — **Manage User**. Ключ можно выпустить и позже: в строке пользователя — **Security Settings** → **Access Keys** → **Create Access Key**.

## 4. Project ID

Имя аккаунта в правом верхнем углу консоли → **My Credentials** → вкладка **API Credentials** → **Project ID** проекта **ru-moscow-1**.

## 5. В панели

**Добавить аккаунт** → Project ID, AK и SK → **Проверить ключ и добавить**. Панель сразу запросит у Cloud.ru список адресов проекта:

- «Нет прав» — политика не привязана к группе, в области группы не отмечен ru-moscow-1 или пользователь не в группе. Права иногда начинают действовать через несколько минут — подождите и попробуйте снова.
- «Ключ не принят» — AK или SK скопированы с ошибкой.

Если политика из JSON не сохраняется, в разделе 2 можно выбрать вместо неё системную с полным доступом к VPC (поиск по \`VPC\`). Прав у неё больше, чем нужно, но она точно подойдёт.`

function BwPolicyJson() {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(timer)
  }, [copied])

  return (
    <div className="relative">
      <pre className="overflow-x-auto rounded-xl bg-dark-950/80 border border-dark-800/60 px-4 py-3 text-xs font-mono text-dark-100">
        {CLOUD_BW_POLICY_JSON}
      </pre>
      <button type="button" onClick={() => copyToClipboard(CLOUD_BW_POLICY_JSON).then(() => setCopied(true))}
        className="absolute top-2 right-2 flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium
                   bg-dark-800 border border-dark-700 text-dark-200 hover:text-dark-50 hover:border-dark-600 transition-colors">
        {copied ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
        {copied ? 'Скопировано' : 'Скопировать'}
      </button>
    </div>
  )
}

function CloudBwGuide() {
  return (
    <div className="space-y-4">
      <Markdown source={CLOUD_BW_GUIDE_START} />
      <BwPolicyJson />
      <Markdown source={CLOUD_BW_GUIDE_END} />
    </div>
  )
}

function BwGuideLink({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" onClick={onClick}
      className="flex items-center gap-2 text-sm text-violet-300 hover:text-violet-200 transition-colors">
      <HelpCircle className="w-4 h-4" /> Как получить ключ — пошаговая инструкция
    </button>
  )
}

function BwAccountForm({ initial, isEdit, saving, error, onSubmit, onCancel, onShowGuide }: {
  initial: CloudBwAccountInput
  isEdit: boolean
  saving: boolean
  error: string | null
  onSubmit: (data: CloudBwAccountInput) => void
  onCancel: () => void
  onShowGuide: () => void
}) {
  const [form, setForm] = useState({ label: '', project_id: '', key_id: '', key_secret: '', ...initial })
  const [showSecret, setShowSecret] = useState(false)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    onSubmit({
      label: form.label.trim(),
      project_id: form.project_id.trim(),
      key_id: form.key_id.trim(),
      ...(form.key_secret.trim() ? { key_secret: form.key_secret.trim() } : {}),
    })
  }

  return (
    <form onSubmit={submit} className="space-y-3">
      {!isEdit && <BwGuideLink onClick={onShowGuide} />}
      <div className="grid grid-cols-2 gap-3">
        <input type="text" placeholder="Метка (необязательно)" value={form.label}
          onChange={e => setForm(f => ({ ...f, label: e.target.value }))} className="input" autoComplete="off" />
        <input type="text" placeholder="Project ID" value={form.project_id}
          onChange={e => setForm(f => ({ ...f, project_id: e.target.value }))} className="input font-mono" autoComplete="off" required />
      </div>
      <input type="text" placeholder="AK (Access Key ID)" value={form.key_id}
        onChange={e => setForm(f => ({ ...f, key_id: e.target.value }))} className="input font-mono" autoComplete="off" required />
      <div className="relative">
        <SecretInput revealed={showSecret} value={form.key_secret} required={!isEdit}
          placeholder={isEdit ? 'SK (пусто — оставить прежний)' : 'SK (Secret Access Key)'}
          onChange={e => setForm(f => ({ ...f, key_secret: e.target.value }))} className="input font-mono pr-10" />
        <button type="button" onClick={() => setShowSecret(s => !s)}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200">
          {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
        </button>
      </div>
      {error && <p className="text-sm text-danger flex items-center gap-2"><AlertTriangle className="w-4 h-4 flex-shrink-0" /> {error}</p>}
      <div className="flex justify-end gap-2">
        <button type="button" onClick={onCancel} className="btn btn-secondary">Отмена</button>
        <button type="submit" disabled={saving} className={`btn btn-primary flex items-center gap-2 ${BW_DISABLED}`}>
          {saving && <Loader2 className="w-4 h-4 animate-spin" />} {isEdit ? 'Сохранить' : 'Проверить ключ и добавить'}
        </button>
      </div>
    </form>
  )
}

function BwAccountsSection({ accounts, onChanged, onShowGuide }: {
  accounts: CloudBwAccount[]
  onChanged: () => void
  onShowGuide: () => void
}) {
  // null — форма закрыта, 'new' — новый аккаунт, число — правка аккаунта
  const [editing, setEditing] = useState<'new' | number | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null)

  const openForm = (target: 'new' | number | null) => {
    setEditing(target)
    setError(null)
  }

  const submit = async (data: CloudBwAccountInput) => {
    setSaving(true)
    setError(null)
    try {
      if (editing === 'new') await cloudBwApi.createAccount(data)
      else if (editing !== null) await cloudBwApi.updateAccount(editing, data)
      setEditing(null)
      onChanged()
    } catch (e) {
      setError(bwErrorText(e, 'Не удалось сохранить аккаунт'))
    } finally {
      setSaving(false)
    }
  }

  const remove = async (id: number) => {
    try {
      await cloudBwApi.deleteAccount(id)
      setConfirmDelete(null)
      onChanged()
    } catch (e) {
      setError(bwErrorText(e, 'Не удалось удалить аккаунт'))
    }
  }

  return (
    <motion.div className="card space-y-3" variants={itemVariants}>
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
          <Wallet className="w-5 h-5 text-violet-400" /> Аккаунты
        </h2>
        {editing === null && (
          <button onClick={() => openForm('new')} className="btn btn-secondary flex items-center gap-2 text-sm">
            <Plus className="w-4 h-4" /> Добавить аккаунт
          </button>
        )}
      </div>

      {editing === 'new' && (
        <BwAccountForm initial={{}} isEdit={false} saving={saving} error={error}
          onSubmit={submit} onCancel={() => openForm(null)} onShowGuide={onShowGuide} />
      )}

      {accounts.length === 0 && editing !== 'new' && (
        <div className="space-y-2">
          <p className="text-sm text-dark-500">Добавьте аккаунт Cloud.ru Advanced — его ключом панель меняет полосу адресов.</p>
          <BwGuideLink onClick={onShowGuide} />
        </div>
      )}

      {accounts.map(account => (
        <div key={account.id} className="rounded-xl bg-dark-800/40 border border-dark-700/50 p-3">
          {editing === account.id ? (
            <BwAccountForm initial={{ label: account.label, project_id: account.project_id, key_id: account.key_id }}
              isEdit saving={saving} error={error} onSubmit={submit} onCancel={() => openForm(null)}
              onShowGuide={onShowGuide} />
          ) : (
            <div className="flex items-center justify-between gap-3 flex-wrap">
              <div className="min-w-0">
                <div className="font-medium text-dark-100 truncate">
                  {account.label || `Аккаунт #${account.id}`}
                  <span className="ml-2 text-xs text-dark-500 font-mono">{account.project_id}</span>
                </div>
                <div className="text-xs text-dark-500 font-mono truncate">
                  AK {account.key_id.slice(0, 12)}… · под автоматикой: {account.ip_count}
                </div>
              </div>
              {confirmDelete === account.id ? (
                <div className="flex items-center gap-2 text-sm">
                  <span className="text-dark-300">Удалить вместе с адресами? Полоса в облаке останется как есть.</span>
                  <button onClick={() => remove(account.id)} className="btn btn-danger text-sm">Удалить</button>
                  <button onClick={() => setConfirmDelete(null)} className="btn btn-secondary text-sm">Отмена</button>
                </div>
              ) : (
                <div className="flex items-center gap-1">
                  <Tooltip label="Изменить" position="top">
                    <button onClick={() => openForm(account.id)} disabled={editing !== null}
                      className="p-2 rounded-lg text-dark-400 hover:text-dark-100 hover:bg-dark-700/50 disabled:opacity-30">
                      <Edit2 className="w-4 h-4" />
                    </button>
                  </Tooltip>
                  <Tooltip label="Удалить" position="top">
                    <button onClick={() => setConfirmDelete(account.id)}
                      className="p-2 rounded-lg text-dark-400 hover:text-danger hover:bg-danger/10">
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </Tooltip>
                </div>
              )}
            </div>
          )}
        </div>
      ))}

      {error && editing === null && (
        <p className="text-sm text-danger flex items-center gap-2"><AlertTriangle className="w-4 h-4 flex-shrink-0" /> {error}</p>
      )}
    </motion.div>
  )
}

/** Ноды общей полосы: выбранные — плашками, новая добавляется из списка остальных */
function BwNodePicker({ servers, value, disabled = false, onChange }: {
  servers: BwServerOption[]
  value: number[]
  disabled?: boolean
  onChange: (value: number[]) => void
}) {
  const names = new Map(servers.map(server => [server.id, server.name]))
  const available = servers.filter(server => !value.includes(server.id))

  return (
    <div className="flex items-center gap-2 flex-wrap">
      {value.map(id => (
        <span key={id} className="flex items-center gap-1 pl-2.5 pr-1 py-1 rounded-lg text-sm
                                  bg-violet-500/15 border border-violet-500/25 text-violet-200">
          {names.get(id) ?? `#${id}`}
          {!disabled && (
            <button type="button" onClick={() => onChange(value.filter(item => item !== id))} aria-label="Убрать ноду"
              className="p-0.5 rounded text-violet-300 hover:text-violet-100 hover:bg-violet-500/20">
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </span>
      ))}
      {!disabled && available.length > 0 && (
        <select value="" onChange={e => e.target.value && onChange([...value, Number(e.target.value)])}
          className="input w-auto min-w-[12rem] flex-1">
          <option value="">{value.length ? 'Добавить ноду' : 'Выберите ноды'}</option>
          {available.map(server => <option key={server.id} value={server.id}>{server.name}</option>)}
        </select>
      )}
      {disabled && value.length === 0 && <span className="text-sm text-dark-500">Ноды не привязаны</span>}
    </div>
  )
}

function BwAddedMark({ text }: { text: string }) {
  return <span className="text-sm text-emerald-400 flex items-center gap-1"><CheckCircle2 className="w-4 h-4" /> {text}</span>
}

function BwAddButton({ adding, disabled, label, onClick }: {
  adding: boolean
  disabled: boolean
  label: string
  onClick: () => void
}) {
  return (
    <button onClick={onClick} disabled={disabled} className={`btn btn-primary flex items-center gap-2 text-sm ${BW_DISABLED}`}>
      {adding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} {label}
    </button>
  )
}

function BwEipRow({ eip, servers, nodeId, adding, disabled, onNodeChange, onAdd }: {
  eip: CloudBwCloudEip
  servers: BwServerOption[]
  nodeId: number | null
  adding: boolean
  disabled: boolean
  onNodeChange: (nodeId: number | null) => void
  onAdd: () => void
}) {
  return (
    <div className="flex items-center gap-3 flex-wrap rounded-xl bg-dark-800/40 border border-dark-700/50 p-3">
      <div className="min-w-[10rem]">
        <div className="font-mono text-dark-100">{eip.ip_address}</div>
        <div className="text-xs text-dark-500">
          {eip.bandwidth_mbps ?? '—'} Мбит/с
          {!eip.attached && <span> · не привязан к серверу</span>}
        </div>
      </div>
      <select value={nodeId ?? ''} disabled={eip.added}
        onChange={e => onNodeChange(e.target.value ? Number(e.target.value) : null)}
        className="input flex-1 min-w-[12rem]">
        <option value="">Нода не привязана</option>
        {servers.map(server => <option key={server.id} value={server.id}>{server.name}</option>)}
      </select>
      {eip.added ? <BwAddedMark text="Добавлен" /> : <BwAddButton adding={adding} disabled={disabled} label="Добавить" onClick={onAdd} />}
    </div>
  )
}

interface BwSharedGroup {
  bandwidth_id: string
  name: string | null
  mbps: number | null
  eips: CloudBwCloudEip[]
  added: boolean
  suggested_server_ids: number[]
}

/** Адреса общей полосы приходят по одному — в списке они одна строка на полосу */
function groupSharedEips(eips: CloudBwCloudEip[]): BwSharedGroup[] {
  const groups = new Map<string, BwSharedGroup>()
  for (const eip of eips) {
    if (eip.share_type !== BW_SHARE_TYPE_SHARED || !eip.bandwidth_id) continue
    const group = groups.get(eip.bandwidth_id) ?? {
      bandwidth_id: eip.bandwidth_id, name: eip.bandwidth_name, mbps: eip.bandwidth_mbps,
      eips: [], added: eip.added, suggested_server_ids: [],
    }
    group.eips.push(eip)
    if (eip.suggested_server_id !== null && !group.suggested_server_ids.includes(eip.suggested_server_id)) {
      group.suggested_server_ids.push(eip.suggested_server_id)
    }
    groups.set(eip.bandwidth_id, group)
  }
  return [...groups.values()]
}

function BwSharedRow({ group, servers, nodeIds, adding, disabled, onNodesChange, onAdd }: {
  group: BwSharedGroup
  servers: BwServerOption[]
  nodeIds: number[]
  adding: boolean
  disabled: boolean
  onNodesChange: (nodeIds: number[]) => void
  onAdd: () => void
}) {
  return (
    <div className="flex items-center gap-3 flex-wrap rounded-xl bg-dark-800/40 border border-dark-700/50 p-3">
      <div className="min-w-[10rem] max-w-full">
        <div className="text-dark-100 flex items-center gap-1.5">
          <Network className="w-4 h-4 text-violet-300 flex-shrink-0" /> {group.name || 'Общая полоса'}
        </div>
        <div className="text-xs text-dark-500">Общая полоса · {group.mbps ?? '—'} Мбит/с · адресов: {group.eips.length}</div>
        <div className="text-xs text-dark-400 font-mono break-all">{group.eips.map(eip => eip.ip_address).join(', ')}</div>
      </div>
      <div className="flex-1 min-w-[12rem]">
        <BwNodePicker servers={servers} value={nodeIds} disabled={group.added} onChange={onNodesChange} />
      </div>
      {group.added
        ? <BwAddedMark text="Добавлена" />
        : <BwAddButton adding={adding} disabled={disabled} label="Добавить полосу" onClick={onAdd} />}
    </div>
  )
}

function BwEipGroup({ title, hint, highlighted = false, empty, onClose, children }: {
  title: string
  hint: string
  highlighted?: boolean
  empty?: string
  onClose?: () => void
  children: React.ReactNode[]
}) {
  return (
    <div className={`rounded-xl border p-3 space-y-2 ${
      highlighted ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-dark-700/50 bg-dark-900/30'
    }`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h4 className={`text-sm font-semibold flex items-center gap-2 ${highlighted ? 'text-emerald-300' : 'text-dark-200'}`}>
            {highlighted ? <CheckCircle2 className="w-4 h-4" /> : <Globe className="w-4 h-4" />} {title}
          </h4>
          <p className="text-xs text-dark-500 mt-0.5">{hint}</p>
        </div>
        {onClose && (
          <button onClick={onClose} aria-label="Скрыть блок"
            className="p-1.5 rounded-lg text-dark-400 hover:text-dark-100 hover:bg-dark-700/50">
            <X className="w-4 h-4" />
          </button>
        )}
      </div>
      {children.length ? children : <p className="text-sm text-dark-500">{empty}</p>}
    </div>
  )
}

// Скрытый крестиком блок «Остальные адреса» остаётся скрытым и после перезагрузки страницы
const BW_HIDE_OTHERS_KEY = 'cloud-bw-hide-other-eips'

// Внутри группы сначала то, что ещё не добавлено: ради этого окно и открывают
const notAddedFirst = <T extends { added: boolean }>(items: T[]) => [...items].sort((a, b) => Number(a.added) - Number(b.added))

interface BwAddItem {
  added: boolean
  inPanel: boolean
  element: React.ReactNode
}

function BwAddIpPanel({ accounts, servers, onAdded, onClose }: {
  accounts: CloudBwAccount[]
  servers: BwServerOption[]
  onAdded: () => void
  onClose: () => void
}) {
  const [accountId, setAccountId] = useState<number | null>(accounts[0]?.id ?? null)
  const [eips, setEips] = useState<CloudBwCloudEip[] | null>(null)
  const [nodeChoice, setNodeChoice] = useState<Record<string, number | null>>({})
  const [sharedNodeChoice, setSharedNodeChoice] = useState<Record<string, number[]>>({})
  const [loadError, setLoadError] = useState<string | null>(null)
  const [addingId, setAddingId] = useState<string | null>(null)
  const [addError, setAddError] = useState<string | null>(null)
  const [hideOthers, setHideOthers] = useState(() => readStorage(BW_HIDE_OTHERS_KEY) === '1')

  const toggleOthers = (hidden: boolean) => {
    setHideOthers(hidden)
    writeStorage(BW_HIDE_OTHERS_KEY, hidden ? '1' : '0')
  }

  useEffect(() => {
    if (accountId === null) return
    let cancelled = false
    setEips(null)
    setLoadError(null)
    cloudBwApi.getCloudEips(accountId)
      .then(({ data }) => {
        if (cancelled) return
        setEips(data)
        setNodeChoice(Object.fromEntries(data.map(eip => [eip.eip_id, eip.suggested_server_id])))
        setSharedNodeChoice(Object.fromEntries(groupSharedEips(data).map(group => [group.bandwidth_id, group.suggested_server_ids])))
      })
      .catch(e => { if (!cancelled) setLoadError(bwErrorText(e, 'Не удалось получить адреса из Cloud.ru')) })
    return () => { cancelled = true }
  }, [accountId])

  const submitAdd = async (key: string, label: string, request: () => Promise<unknown>,
                           isAdded: (eip: CloudBwCloudEip) => boolean) => {
    setAddingId(key)
    setAddError(null)
    try {
      await request()
      setEips(list => list?.map(item => (isAdded(item) ? { ...item, added: true } : item)) ?? null)
      onAdded()
    } catch (e) {
      setAddError(`${label}: ${bwErrorText(e, 'не удалось добавить')}`)
    } finally {
      setAddingId(null)
    }
  }

  const add = (eip: CloudBwCloudEip) => {
    if (accountId === null) return
    submitAdd(eip.eip_id, eip.ip_address,
      () => cloudBwApi.addIp({ account_id: accountId, eip_id: eip.eip_id, server_id: nodeChoice[eip.eip_id] ?? null }),
      item => item.eip_id === eip.eip_id)
  }

  const addShared = (group: BwSharedGroup) => {
    if (accountId === null) return
    submitAdd(group.bandwidth_id, group.name || 'Общая полоса',
      () => cloudBwApi.addShared({
        account_id: accountId, bandwidth_id: group.bandwidth_id, server_ids: sharedNodeChoice[group.bandwidth_id] ?? [],
      }),
      item => item.share_type === BW_SHARE_TYPE_SHARED && item.bandwidth_id === group.bandwidth_id)
  }

  const items: BwAddItem[] = [
    ...groupSharedEips(eips ?? []).map(group => ({
      added: group.added,
      inPanel: group.suggested_server_ids.length > 0,
      element: (
        <BwSharedRow key={group.bandwidth_id} group={group} servers={servers}
          nodeIds={sharedNodeChoice[group.bandwidth_id] ?? []}
          adding={addingId === group.bandwidth_id} disabled={addingId !== null}
          onNodesChange={nodeIds => setSharedNodeChoice(choice => ({ ...choice, [group.bandwidth_id]: nodeIds }))}
          onAdd={() => addShared(group)} />
      ),
    })),
    ...(eips ?? []).filter(eip => eip.share_type !== BW_SHARE_TYPE_SHARED).map(eip => ({
      added: eip.added,
      inPanel: eip.suggested_server_id !== null,
      element: (
        <BwEipRow key={eip.eip_id} eip={eip} servers={servers} nodeId={nodeChoice[eip.eip_id] ?? null}
          adding={addingId === eip.eip_id} disabled={addingId !== null}
          onNodeChange={nodeId => setNodeChoice(choice => ({ ...choice, [eip.eip_id]: nodeId }))}
          onAdd={() => add(eip)} />
      ),
    })),
  ]
  const inPanel = notAddedFirst(items.filter(item => item.inPanel)).map(item => item.element)
  const others = notAddedFirst(items.filter(item => !item.inPanel)).map(item => item.element)

  return (
    <motion.div className="card space-y-3"
      initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
      <div className="flex items-center justify-between gap-3">
        <h3 className="font-medium text-dark-100">Адреса в проекте Cloud.ru</h3>
        <button onClick={onClose} aria-label="Закрыть" className="p-1.5 rounded-lg text-dark-400 hover:text-dark-100 hover:bg-dark-700/50">
          <X className="w-4 h-4" />
        </button>
      </div>
      {accounts.length > 1 && (
        <select value={accountId ?? ''} onChange={e => setAccountId(Number(e.target.value))} className="input">
          {accounts.map(account => (
            <option key={account.id} value={account.id}>{account.label || `Аккаунт #${account.id}`} · {account.project_id}</option>
          ))}
        </select>
      )}
      <p className="text-xs text-dark-500">
        Добавляйте только свои адреса — автоматика трогает лишь те, что добавлены. Общая полоса добавляется
        целиком, одной строкой со всеми её адресами.
      </p>
      {loadError && <p className="text-sm text-danger flex items-center gap-2"><AlertTriangle className="w-4 h-4 flex-shrink-0" /> {loadError}</p>}
      {addError && <p className="text-sm text-danger flex items-center gap-2"><AlertTriangle className="w-4 h-4 flex-shrink-0" /> {addError}</p>}
      {eips === null && !loadError ? (
        <div className="flex justify-center py-6"><Loader2 className="w-6 h-6 text-violet-400 animate-spin" /></div>
      ) : eips && eips.length === 0 ? (
        <p className="text-sm text-dark-500">В проекте нет публичных IP</p>
      ) : eips && (
        <>
          <BwEipGroup highlighted title={`Есть в панели · ${inPanel.length}`}
            hint="Адрес совпал с сервером панели — нода уже подставлена. У общей полосы — все ноды её адресов"
            empty="Ни один адрес проекта не совпал с серверами панели">
            {inPanel}
          </BwEipGroup>
          {others.length > 0 && (hideOthers ? (
            <button onClick={() => toggleOthers(false)}
              className="flex items-center gap-2 text-sm text-dark-400 hover:text-dark-200 transition-colors">
              <Eye className="w-4 h-4" /> Показать остальные адреса проекта · {others.length}
            </button>
          ) : (
            <BwEipGroup title={`Остальные адреса проекта · ${others.length}`}
              hint="Сервера с таким адресом в панели нет. Для расписания нода не нужна, для динамики выберите её вручную"
              onClose={() => toggleOthers(true)}>
              {others}
            </BwEipGroup>
          ))}
        </>
      )}
    </motion.div>
  )
}

function bwSizeHints(shared: boolean): string {
  return shared ? `${BW_SHARED_DUPLEX_HINT}. ${BW_SHARED_SIZE_HINT}` : `${BW_DUPLEX_HINT}. ${BW_SIZE_HINT}`
}

function BwScheduleEditor({ rules, shared, onChange }: {
  rules: CloudBwScheduleRule[]
  shared: boolean
  onChange: (rules: CloudBwScheduleRule[]) => void
}) {
  const owners = BW_HOURS.map(hour => rules.flatMap((rule, index) => (ruleHours(rule).includes(hour) ? [index] : [])))
  const gaps = owners.filter(owner => owner.length === 0).length
  const overlaps = owners.filter(owner => owner.length > 1).length

  const updateRule = (index: number, patch: Partial<CloudBwScheduleRule>) =>
    onChange(rules.map((rule, i) => (i === index ? { ...rule, ...patch } : rule)))

  // Новый интервал закрывает первую дыру, а если её нет — встаёт в начало суток для правки
  const addRule = () => {
    const start = Math.max(0, owners.findIndex(owner => owner.length === 0))
    let end = start + 1
    while (end < 24 && owners[end].length === 0) end++
    onChange([...rules, { start_hour: start, end_hour: end, mbps: rules[rules.length - 1]?.mbps ?? 500 }])
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-dark-400">
        Время московское (UTC+3). Интервалы должны покрыть все 24 часа без пересечений; час окончания в интервал
        не входит. Интервал может идти через полночь, например с 22:00 до 06:00.
      </p>
      <div>
        <div className="flex gap-px rounded-lg overflow-hidden">
          {owners.map((owner, hour) => (
            <div key={hour} title={`${formatClock(hour)}–${formatClock(hour + 1)}`}
              className={`h-3 flex-1 ${owner.length === 1 ? BW_RULE_COLORS[owner[0] % BW_RULE_COLORS.length] : 'bg-danger/70'}`} />
          ))}
        </div>
        <div className="flex justify-between text-[10px] text-dark-500 mt-1 font-mono">
          {[0, 6, 12, 18, 24].map(hour => <span key={hour}>{formatClock(hour)}</span>)}
        </div>
        {(gaps > 0 || overlaps > 0) && (
          <p className="text-xs text-danger mt-1">
            {gaps > 0 && `Не покрыто часов: ${gaps}. `}{overlaps > 0 && `Пересекается часов: ${overlaps}.`}
          </p>
        )}
      </div>
      <div className="space-y-2">
        {rules.map((rule, index) => (
          <div key={index} className="flex items-center gap-2 flex-wrap">
            <span className={`w-2.5 h-2.5 rounded-full ${BW_RULE_COLORS[index % BW_RULE_COLORS.length]}`} />
            <span className="text-sm text-dark-400">с</span>
            <select value={rule.start_hour} onChange={e => updateRule(index, { start_hour: Number(e.target.value) })} className="input w-28">
              {BW_HOURS.map(hour => <option key={hour} value={hour}>{formatClock(hour)}</option>)}
            </select>
            <span className="text-sm text-dark-400">до</span>
            <select value={rule.end_hour} onChange={e => updateRule(index, { end_hour: Number(e.target.value) })} className="input w-28">
              {BW_HOURS.map(hour => <option key={hour} value={hour + 1}>{formatClock(hour + 1)}</option>)}
            </select>
            <input type="number" min={shared ? BW_SHARED_MIN_MBPS : 1} max={BW_MAX_MBPS} value={rule.mbps || ''}
              onChange={e => updateRule(index, { mbps: Number(e.target.value) })}
              className={`input w-28 font-mono ${isValidBandwidth(rule.mbps, shared) ? '' : 'border-danger/60'}`} />
            <span className="text-sm text-dark-400">Мбит/с</span>
            <button type="button" onClick={() => onChange(rules.filter((_, i) => i !== index))}
              disabled={rules.length <= BW_MIN_SCHEDULE_RULES} aria-label="Удалить интервал"
              className="p-1.5 rounded-lg text-dark-400 hover:text-danger hover:bg-danger/10 disabled:opacity-30 disabled:pointer-events-none">
              <Trash2 className="w-4 h-4" />
            </button>
          </div>
        ))}
      </div>
      <button type="button" onClick={addRule} className="btn btn-secondary flex items-center gap-2 text-sm">
        <Plus className="w-4 h-4" /> Добавить интервал
      </button>
      <p className="text-xs text-dark-500">{bwSizeHints(shared)}</p>
    </div>
  )
}

function BwDynamicEditor({ value, hasNode, shared, onChange }: {
  value: CloudBwDynamic
  hasNode: boolean
  shared: boolean
  onChange: (value: CloudBwDynamic) => void
}) {
  const set = (patch: Partial<CloudBwDynamic>) => onChange({ ...value, ...patch })
  const hourChoices = BW_HOURS.map(hour => hour + 1)
  const labelClass = 'block text-xs text-dark-400 mb-1'
  const minMbps = shared ? BW_SHARED_MIN_MBPS : 1
  const speedSource = shared
    ? 'суммарную скорость выбранных нод на пике за выбранное окно — входы и выходы нод складываются поминутно, берётся большая из двух сумм'
    : 'пиковую скорость ноды за выбранное окно — большее из входа и выхода'

  return (
    <div className="space-y-3">
      {!hasNode && (
        <p className="text-sm text-amber-400 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 flex-shrink-0" />
          {shared ? 'Привяжите полосу к нодам — она считается по их суммарной скорости' : 'Привяжите адрес к ноде — полоса считается по её скорости'}
        </p>
      )}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <label>
          <span className={labelClass}>Пик за последние</span>
          <select value={value.window_hours} onChange={e => set({ window_hours: Number(e.target.value) })} className="input">
            {hourChoices.map(hours => <option key={hours} value={hours}>{hours} ч</option>)}
          </select>
        </label>
        <label>
          <span className={labelClass}>Запас, %</span>
          <input type="number" min={0} max={300} value={value.reserve_percent}
            onChange={e => set({ reserve_percent: Number(e.target.value) })} className="input font-mono" />
        </label>
        <label>
          <span className={labelClass}>Снижать раз в</span>
          <select value={value.interval_hours} onChange={e => set({ interval_hours: Number(e.target.value) })} className="input">
            {hourChoices.map(hours => <option key={hours} value={hours}>{hours} ч</option>)}
          </select>
        </label>
        <label>
          <span className={labelClass}>Минимум, Мбит/с</span>
          <input type="number" min={minMbps} max={BW_MAX_MBPS} value={value.min_mbps || ''} onChange={e => set({ min_mbps: Number(e.target.value) })}
            className={`input font-mono ${isValidBandwidth(value.min_mbps, shared) ? '' : 'border-danger/60'}`} />
        </label>
        <label>
          <span className={labelClass}>Максимум, Мбит/с</span>
          <input type="number" min={minMbps} max={BW_MAX_MBPS} value={value.max_mbps || ''} onChange={e => set({ max_mbps: Number(e.target.value) })}
            className={`input font-mono ${isValidBandwidth(value.max_mbps, shared) ? '' : 'border-danger/60'}`} />
        </label>
      </div>
      <p className="text-xs text-dark-400">
        В начале каждого часа панель берёт {speedSource}, добавляет запас и округляет вверх до размера, который
        примет Cloud.ru: выше 1000 Мбит/с это только 1500 и 2000, поэтому, например, 1552 станет 2000. Если
        получилось больше текущей полосы, поднимает сразу. Если меньше — только раз в выбранное число часов и только
        когда метрики {shared ? 'каждой ноды' : 'ноды'} есть хотя бы за 80% окна.
      </p>
      <p className="text-xs text-dark-500">
        {bwSizeHints(shared)}. Окно не длиннее суток: подробные метрики панель хранит 24 часа.
      </p>
    </div>
  )
}

function BwHistory({ ipId }: { ipId: number }) {
  const [entries, setEntries] = useState<CloudBwHistoryEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    cloudBwApi.getHistory(ipId)
      .then(({ data }) => setEntries(data))
      .catch(e => setError(bwErrorText(e, 'Не удалось загрузить историю')))
  }, [ipId])

  if (error) return <p className="text-sm text-danger">{error}</p>
  if (!entries) return <div className="flex justify-center py-3"><Loader2 className="w-5 h-5 text-violet-400 animate-spin" /></div>
  if (!entries.length) return <p className="text-sm text-dark-500">Полоса ещё не менялась</p>

  return (
    <div className="space-y-1.5 max-h-64 overflow-y-auto pr-1">
      {entries.map(entry => (
        <div key={entry.id} className="flex items-start gap-3 text-sm">
          <span className="text-dark-500 font-mono text-xs whitespace-nowrap pt-0.5">{formatBwTime(entry.created_at)}</span>
          {entry.error ? (
            <span className="text-danger">{entry.error}</span>
          ) : (
            <span className="text-dark-200">
              <span className="font-mono">{entry.old_mbps} → {entry.new_mbps} Мбит/с</span>
              {entry.note && <span className="text-dark-400"> · {entry.note}</span>}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

function BwIpCard({ ip, servers, onChanged, onDeleted }: {
  ip: CloudBwIp
  servers: BwServerOption[]
  onChanged: (ip: CloudBwIp) => void
  onDeleted: () => void
}) {
  const [draft, setDraft] = useState<BwDraft>(() => draftFromIp(ip))
  const [busy, setBusy] = useState<'save' | 'apply' | 'delete' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const dirty = JSON.stringify(draft) !== JSON.stringify(draftFromIp(ip))

  const run = async (action: 'save' | 'apply' | 'delete', request: () => Promise<void>, fallback: string) => {
    setBusy(action)
    setError(null)
    try {
      await request()
    } catch (e) {
      setError(bwErrorText(e, fallback))
    } finally {
      setBusy(null)
    }
  }

  const save = () => run('save', async () => {
    const { data } = await cloudBwApi.updateIp(ip.id, {
      server_id: draft.server_id,
      server_ids: draft.server_ids,
      mode: draft.mode,
      schedule: draft.schedule,
      dyn_window_hours: draft.dynamic.window_hours,
      dyn_reserve_percent: draft.dynamic.reserve_percent,
      dyn_interval_hours: draft.dynamic.interval_hours,
      dyn_min_mbps: draft.dynamic.min_mbps,
      dyn_max_mbps: draft.dynamic.max_mbps,
    })
    onChanged(data)
    setDraft(draftFromIp(data))
  }, 'Не удалось сохранить правило')

  const apply = () => run('apply', async () => {
    onChanged((await cloudBwApi.applyIp(ip.id)).data)
  }, 'Не удалось проверить полосу')

  const remove = () => run('delete', async () => {
    await cloudBwApi.deleteIp(ip.id)
    onDeleted()
  }, 'Не удалось удалить адрес')

  return (
    <motion.div className="card space-y-4" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-10 h-10 rounded-xl bg-violet-500/15 border border-violet-500/25 flex items-center justify-center flex-shrink-0">
            {ip.shared ? <Network className="w-5 h-5 text-violet-300" /> : <Gauge className="w-5 h-5 text-violet-300" />}
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              {ip.shared ? (
                <span className="text-dark-50 font-medium">{ip.bandwidth_name || 'Общая полоса'}</span>
              ) : (
                <CopyableIp value={ip.ip_address} className="font-mono text-dark-50 font-medium" />
              )}
              {ip.shared && (
                <span className="px-1.5 py-0.5 rounded-md text-[11px] bg-violet-500/15 border border-violet-500/25 text-violet-300">
                  общая полоса
                </span>
              )}
              <span className="text-xs text-dark-500">{ip.account_label || `Аккаунт #${ip.account_id}`}</span>
            </div>
            {ip.shared ? (
              <div className="text-xs text-dark-400 font-mono break-all">
                {ip.members.length ? ip.members.map(member => member.ip_address).join(', ') : 'Адресов в полосе нет'}
              </div>
            ) : (
              <div className="text-xs text-dark-500">
                {ip.server_name ? `Нода ${ip.server_name}` : 'Нода не привязана'}
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="px-3 py-1.5 rounded-lg bg-dark-800/60 border border-dark-700/50 text-sm">
            <span className="font-mono text-dark-50">{ip.current_mbps ?? '—'}</span> <span className="text-dark-400">Мбит/с</span>
          </div>
          {confirmDelete ? (
            <>
              <button onClick={remove} disabled={busy !== null} className={`btn btn-danger text-sm ${BW_DISABLED}`}>Удалить</button>
              <button onClick={() => setConfirmDelete(false)} className="btn btn-secondary text-sm">Отмена</button>
            </>
          ) : (
            <Tooltip label="Убрать из автоматики (полоса в облаке останется как есть)" position="top">
              <button onClick={() => setConfirmDelete(true)} aria-label="Удалить адрес"
                className="p-2 rounded-lg text-dark-400 hover:text-danger hover:bg-danger/10">
                <Trash2 className="w-4 h-4" />
              </button>
            </Tooltip>
          )}
        </div>
      </div>

      <div className="space-y-1">
        {ip.last_error ? (
          <div className="flex items-start gap-2 p-3 rounded-lg bg-danger/10 border border-danger/20 text-sm text-danger">
            <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" /> {ip.last_error}
          </div>
        ) : ip.last_note && (
          <p className="text-sm text-dark-300">{ip.last_note}</p>
        )}
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-dark-500">
          <span>Проверка: {formatBwTime(ip.last_check_at)}</span>
          <span>Смена полосы: {formatBwTime(ip.last_change_at)}</span>
          {ip.next_decrease_at && <span>Плановый пересчёт: {formatBwTime(ip.next_decrease_at)}</span>}
        </div>
      </div>

      <div className="flex items-end gap-3 flex-wrap">
        {ip.shared ? (
          <div className="flex-1 min-w-[14rem]">
            <span className="block text-xs text-dark-400 mb-1">Ноды</span>
            <BwNodePicker servers={servers} value={draft.server_ids}
              onChange={serverIds => setDraft(d => ({ ...d, server_ids: serverIds }))} />
          </div>
        ) : (
          <label className="flex-1 min-w-[14rem]">
            <span className="block text-xs text-dark-400 mb-1">Нода</span>
            <select value={draft.server_id ?? ''} className="input"
              onChange={e => setDraft(d => ({ ...d, server_id: e.target.value ? Number(e.target.value) : null }))}>
              <option value="">Не привязана</option>
              {servers.map(server => <option key={server.id} value={server.id}>{server.name}</option>)}
            </select>
          </label>
        )}
        <div className="flex gap-1 bg-dark-800/50 p-1 rounded-xl">
          {BW_MODES.map(mode => (
            <button key={mode.id} type="button" onClick={() => setDraft(d => ({ ...d, mode: mode.id }))}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                draft.mode === mode.id ? 'bg-violet-500/20 text-violet-300' : 'text-dark-400 hover:text-dark-200 hover:bg-dark-700/50'
              }`}>
              {mode.icon} {mode.label}
            </button>
          ))}
        </div>
      </div>

      {draft.mode === 'schedule' && (
        <BwScheduleEditor rules={draft.schedule} shared={ip.shared} onChange={schedule => setDraft(d => ({ ...d, schedule }))} />
      )}
      {draft.mode === 'dynamic' && (
        <BwDynamicEditor value={draft.dynamic} shared={ip.shared}
          hasNode={ip.shared ? draft.server_ids.length > 0 : draft.server_id !== null}
          onChange={dynamic => setDraft(d => ({ ...d, dynamic }))} />
      )}
      {draft.mode === 'off' && (
        <p className="text-sm text-dark-500">{ip.shared ? 'Панель не трогает эту общую полосу.' : 'Панель не трогает полосу этого адреса.'}</p>
      )}

      {error && (
        <p className="text-sm text-danger flex items-center gap-2"><AlertTriangle className="w-4 h-4 flex-shrink-0" /> {error}</p>
      )}

      <div className="flex items-center justify-between gap-2 flex-wrap">
        <button onClick={() => setShowHistory(v => !v)} className="btn btn-ghost flex items-center gap-2 text-sm">
          <ScrollText className="w-4 h-4" /> {showHistory ? 'Скрыть историю' : 'История'}
        </button>
        <div className="flex items-center gap-2 flex-wrap">
          {dirty && <span className="text-xs text-amber-400">Есть несохранённые изменения</span>}
          <Tooltip label="Сверить полосу с правилом прямо сейчас" position="top">
            <button onClick={apply} disabled={busy !== null || dirty || ip.mode === 'off'}
              className={`btn btn-secondary flex items-center gap-2 text-sm ${BW_DISABLED}`}>
              {busy === 'apply' ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />} Проверить
            </button>
          </Tooltip>
          <button onClick={save} disabled={busy !== null || !dirty} className={`btn btn-primary flex items-center gap-2 text-sm ${BW_DISABLED}`}>
            {busy === 'save' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} Сохранить и применить
          </button>
        </div>
      </div>

      {showHistory && <BwHistory key={ip.last_check_at ?? 'never'} ipId={ip.id} />}
    </motion.div>
  )
}

const BW_USAGE_PERIODS: { id: CloudBwUsagePeriod; label: string }[] = [
  { id: 'day', label: 'Сутки' },
  { id: 'week', label: '7 дней' },
  { id: '30d', label: '30 дней' },
  { id: 'month', label: 'Этот месяц' },
  { id: 'prev_month', label: 'Прошлый месяц' },
]
const BW_USAGE_PERIOD_KEY = 'cloud-bw-usage-period'
const BW_USAGE_DEFAULT_PERIOD: CloudBwUsagePeriod = 'month'
const BW_USAGE_CELL = 'py-2 px-3 text-right font-mono whitespace-nowrap'

function savedUsagePeriod(): CloudBwUsagePeriod {
  const saved = readStorage(BW_USAGE_PERIOD_KEY)
  return BW_USAGE_PERIODS.find(item => item.id === saved)?.id ?? BW_USAGE_DEFAULT_PERIOD
}

const formatNumber = (value: number, digits = 0) => value.toLocaleString('ru-RU', { maximumFractionDigits: digits })
const formatRub = (value: number) =>
  `${value.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ₽`
// Границы месяцев сервер считает по Москве — по ней и показываем, иначе месяц «начнётся» не в полночь
const formatBwDate = (ts: string) => new Date(ts).toLocaleString('ru-RU', {
  day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow',
})

function BwUsageStat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded-xl bg-dark-800/40 border border-dark-700/50 p-3">
      <div className="text-xs text-dark-400">{label}</div>
      <div className="text-lg font-semibold text-dark-50 font-mono mt-0.5">{value}</div>
      <div className="text-xs text-dark-500 mt-0.5">{hint}</div>
    </div>
  )
}

function bwUsageRange(item: CloudBwUsageItem): string {
  if (item.min_mbps === item.max_mbps) return String(item.min_mbps)
  return `${item.min_mbps}–${item.max_mbps}`
}

function BwUsageRow({ item, periodStart }: { item: CloudBwUsageItem; periodStart: string }) {
  const covered = item.avg_mbps !== null
  // Посчитано не с начала периода — запись добавили в раздел посреди него
  const partial = covered && item.covered_from !== null && new Date(item.covered_from) > new Date(periodStart)
  const subtitle = !covered ? 'В этот период записи в разделе не было'
    : partial ? `Считается с ${formatBwDate(item.covered_from!)}`
    : item.account_label
  const value = (text: string) => (covered ? text : '—')

  return (
    <tr className="border-b border-dark-800/60 last:border-0">
      <td className="py-2 pr-3">
        <div className={item.shared ? 'text-dark-100' : 'text-dark-100 font-mono'}>
          {item.shared ? item.bandwidth_name || 'Общая полоса' : item.ip_address}
        </div>
        <div className="text-xs text-dark-500">
          {subtitle}{item.shared && ` · адресов: ${item.eip_count}`}
        </div>
      </td>
      <td className={`${BW_USAGE_CELL} text-dark-50`}>{value(formatNumber(item.avg_mbps ?? 0, 1))}</td>
      <td className={`${BW_USAGE_CELL} text-dark-300`}>{value(bwUsageRange(item))}</td>
      <td className={`${BW_USAGE_CELL} text-dark-300`}>{value(formatNumber(item.mbps_hours))}</td>
      <td className={`${BW_USAGE_CELL} text-dark-300`}>{value(formatRub(item.bandwidth_rub))}</td>
      <td className={`${BW_USAGE_CELL} text-dark-300`}>{value(formatRub(item.eip_rub))}</td>
      <td className={`${BW_USAGE_CELL} text-dark-50`}>{value(formatRub(item.total_rub))}</td>
    </tr>
  )
}

function BwUsageReport({ usage }: { usage: CloudBwUsage }) {
  const { total, tariff } = usage
  const periodEnd = usage.period === 'prev_month' ? formatBwDate(usage.end) : 'сейчас'

  return (
    <div className="space-y-4">
      <p className="text-xs text-dark-500">
        {formatBwDate(usage.start)} — {periodEnd} (МСК) · {formatNumber(usage.hours)} ч. Суммы с НДС {tariff.vat_percent}%
      </p>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <BwUsageStat label="Средняя полоса" value={`${formatNumber(total.avg_mbps, 1)} Мбит/с`}
          hint="сколько в среднем стояло на всех адресах вместе" />
        <BwUsageStat label="Мбит/с·ч" value={formatNumber(total.mbps_hours)}
          hint="ширина × часы — за них Cloud.ru берёт деньги" />
        <BwUsageStat label="К оплате за период" value={formatRub(total.total_rub)}
          hint={`без НДС ${formatRub(total.total_rub_without_vat)}`} />
        <BwUsageStat label="≈ за 30 дней" value={formatRub(total.forecast_30d_rub)}
          hint="если средняя полоса останется такой же" />
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-dark-400 border-b border-dark-700/50">
              <th className="py-2 pr-3 font-medium text-left">Адрес или общая полоса</th>
              <th className="py-2 px-3 font-medium text-right">Средняя, Мбит/с</th>
              <th className="py-2 px-3 font-medium text-right">Мин–макс</th>
              <th className="py-2 px-3 font-medium text-right">Мбит/с·ч</th>
              <th className="py-2 px-3 font-medium text-right">Полоса</th>
              <th className="py-2 px-3 font-medium text-right">Адреса</th>
              <th className="py-2 px-3 font-medium text-right">Итого</th>
            </tr>
          </thead>
          <tbody>
            {usage.items.map(item => <BwUsageRow key={item.ip_id} item={item} periodStart={usage.start} />)}
          </tbody>
          {usage.items.length > 1 && (
            <tfoot>
              <tr className="border-t border-dark-700/50 text-dark-100">
                <td className="py-2 pr-3 font-medium">Всего</td>
                <td className={BW_USAGE_CELL}>{formatNumber(total.avg_mbps, 1)}</td>
                <td />
                <td className={BW_USAGE_CELL}>{formatNumber(total.mbps_hours)}</td>
                <td className={BW_USAGE_CELL}>{formatRub(total.bandwidth_rub)}</td>
                <td className={BW_USAGE_CELL}>{formatRub(total.eip_rub)}</td>
                <td className={`${BW_USAGE_CELL} font-semibold text-dark-50`}>{formatRub(total.total_rub)}</td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>

      <p className="text-xs text-dark-500">
        Тариф Cloud.ru Advanced без НДС: полоса — {formatNumber(tariff.bandwidth_per_mbps_hour, 3)} ₽ за Мбит/с в час
        (первые {tariff.dedicated_low_tier_mbps} Мбит/с выделенной и {tariff.shared_low_tier_mbps} общей —
        по {formatNumber(tariff.low_tier_per_mbps_hour, 3)} ₽), адрес — {formatNumber(tariff.eip_per_hour, 2)} ₽ в час.
        Средняя считается по истории смен полосы. Смену размера в консоли Cloud.ru панель замечает при следующей
        проверке, а у записей с выключенной автоматикой — не замечает.
      </p>
    </div>
  )
}

/** refreshKey меняется вместе с размером полос — тогда расход пересчитывается */
function BwUsageSection({ refreshKey }: { refreshKey: string }) {
  const [period, setPeriod] = useState<CloudBwUsagePeriod>(savedUsagePeriod)
  const [usage, setUsage] = useState<CloudBwUsage | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    cloudBwApi.getUsage(period)
      .then(({ data }) => {
        if (cancelled) return
        setUsage(data)
        setError(null)
      })
      .catch(e => {
        if (!cancelled) setError(bwErrorText(e, 'Не удалось посчитать расход'))
      })
    return () => { cancelled = true }
  }, [period, refreshKey])

  const choosePeriod = (next: CloudBwUsagePeriod) => {
    if (next === period) return
    setPeriod(next)
    setUsage(null)
    writeStorage(BW_USAGE_PERIOD_KEY, next)
  }

  return (
    <motion.div className="card space-y-4" variants={itemVariants}>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
          <BarChart3 className="w-5 h-5 text-violet-400" /> Средняя полоса и оплата
        </h2>
        <div className="flex gap-1 bg-dark-800/50 p-1 rounded-xl flex-wrap">
          {BW_USAGE_PERIODS.map(item => (
            <button key={item.id} type="button" onClick={() => choosePeriod(item.id)}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                period === item.id ? 'bg-violet-500/20 text-violet-300' : 'text-dark-400 hover:text-dark-200 hover:bg-dark-700/50'
              }`}>
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <p className="text-sm text-danger flex items-center gap-2"><AlertTriangle className="w-4 h-4 flex-shrink-0" /> {error}</p>
      ) : usage ? (
        <BwUsageReport usage={usage} />
      ) : (
        <div className="flex justify-center py-6"><Loader2 className="w-6 h-6 text-violet-400 animate-spin" /></div>
      )}
    </motion.div>
  )
}

export function CloudBandwidthPage() {
  const { t } = useTranslation()
  const [accounts, setAccounts] = useState<CloudBwAccount[]>([])
  const [ips, setIps] = useState<CloudBwIp[]>([])
  const [servers, setServers] = useState<BwServerOption[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showAddIp, setShowAddIp] = useState(false)
  const [showGuide, setShowGuide] = useState(false)
  const closeGuide = useCallback(() => setShowGuide(false), [])

  const reload = useCallback(async () => {
    try {
      const [accountsResponse, ipsResponse] = await Promise.all([cloudBwApi.getAccounts(), cloudBwApi.getIps()])
      setAccounts(accountsResponse.data)
      setIps(ipsResponse.data)
      setError(null)
    } catch (e) {
      setError(bwErrorText(e, 'Не удалось загрузить данные раздела'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    reload()
    serversApi.list()
      .then(({ data }) => setServers(data.servers.map(server => ({ id: server.id, name: server.name }))))
      .catch(e => setError(bwErrorText(e, 'Не удалось загрузить список нод')))
  }, [reload])

  useEffect(() => {
    const timer = setInterval(() => {
      cloudBwApi.getIps().then(({ data }) => setIps(data)).catch(() => undefined)
    }, BW_REFRESH_MS)
    return () => clearInterval(timer)
  }, [])

  const replaceIp = (updated: CloudBwIp) => setIps(list => list.map(ip => (ip.id === updated.id ? updated : ip)))
  const usageKey = ips.map(ip => `${ip.id}:${ip.current_mbps}`).join(',')

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible" className="space-y-6">
      <motion.div variants={itemVariants}>
        <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-3">
          <Gauge className="w-7 h-7 text-violet-400" /> {t('cloud_bandwidth.title')}
          <CloudHelpButton onClick={() => setShowGuide(true)} />
        </h1>
        <p className="text-dark-400 mt-1">
          Ширина канала публичных IP и общих полос Cloud.ru Advanced — по расписанию или по пиковой нагрузке нод.
          Полоса меняется в начале каждого часа.
        </p>
      </motion.div>

      {error && (
        <div className="flex items-center gap-3 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger">
          <AlertTriangle className="w-5 h-5 flex-shrink-0" />
          <span className="text-sm flex-1">{error}</span>
          <button onClick={() => setError(null)} aria-label="Закрыть" className="p-1.5 hover:bg-danger/20 rounded-lg"><X className="w-4 h-4" /></button>
        </div>
      )}

      <BwAccountsSection accounts={accounts} onChanged={reload} onShowGuide={() => setShowGuide(true)} />

      {!loading && ips.length > 0 && <BwUsageSection refreshKey={usageKey} />}

      <motion.div className="space-y-3" variants={itemVariants}>
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-dark-100">Адреса и общие полосы</h2>
          <button onClick={() => setShowAddIp(v => !v)} disabled={accounts.length === 0}
            className={`btn btn-primary flex items-center gap-2 ${BW_DISABLED}`}>
            <Plus className="w-4 h-4" /> Добавить
          </button>
        </div>

        <AnimatePresence>
          {showAddIp && accounts.length > 0 && (
            <BwAddIpPanel accounts={accounts} servers={servers} onAdded={reload} onClose={() => setShowAddIp(false)} />
          )}
        </AnimatePresence>

        {loading ? (
          <div className="flex justify-center py-16"><Loader2 className="w-8 h-8 text-violet-400 animate-spin" /></div>
        ) : ips.length === 0 ? (
          <div className="card text-center py-12">
            <Gauge className="w-12 h-12 text-dark-500 mx-auto mb-3" />
            <p className="text-dark-300 text-sm">Под автоматикой пока ничего нет</p>
            <p className="text-dark-500 text-sm mt-1">
              {accounts.length ? 'Нажмите «Добавить» и выберите свои IP или общие полосы из проекта' : 'Сначала добавьте аккаунт'}
            </p>
          </div>
        ) : (
          ips.map(ip => (
            <BwIpCard key={ip.id} ip={ip} servers={servers} onChanged={replaceIp} onDeleted={reload} />
          ))
        )}
      </motion.div>

      <HelpDrawer open={showGuide} subtitle="Канал Cloud.ru" onClose={closeGuide}>
        <CloudBwGuide />
      </HelpDrawer>
    </motion.div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// MAIN PAGE
// ═══════════════════════════════════════════════════════════════════════

export default function IpSearchPage() {
  const { t } = useTranslation()
  const [activeTab, setActiveTab] = useState<TabType>('accounts')
  const [provider, setProvider] = useState<'vk' | 'yandex' | 'cloud' | 'selectel'>('vk')

  const tabs: { id: TabType; label: string; icon: React.ReactNode }[] = [
    { id: 'accounts', label: 'Аккаунты', icon: <Users className="w-4 h-4" /> },
    { id: 'manual', label: 'Ручной IP', icon: <Target className="w-4 h-4" /> },
    { id: 'stats', label: 'Статистика', icon: <BarChart3 className="w-4 h-4" /> },
    { id: 'multiqueue', label: 'Multiqueue', icon: <Cpu className="w-4 h-4" /> },
    { id: 'settings', label: 'Настройки', icon: <Settings className="w-4 h-4" /> },
    { id: 'logs', label: 'Логи', icon: <ScrollText className="w-4 h-4" /> },
  ]

  return (
    <motion.div variants={containerVariants} initial="hidden" animate="visible">
      <motion.div className="mb-6" variants={itemVariants}>
        <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-3">
          <Search className="w-7 h-7 text-accent-400" /> {t('ip_search.title')}
        </h1>
        <p className="text-dark-400 mt-1">{t('ip_search.subtitle')}</p>
      </motion.div>

      <motion.div className="flex gap-1 bg-dark-800/50 p-1 rounded-xl w-fit mb-4" variants={itemVariants}>
        <button onClick={() => setProvider('vk')}
          className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${provider === 'vk' ? 'bg-accent-500/20 text-accent-400' : 'text-dark-400 hover:text-dark-200'}`}>
          VK Cloud
        </button>
        <button onClick={() => setProvider('yandex')}
          className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${provider === 'yandex' ? 'bg-orange-500/20 text-orange-400' : 'text-dark-400 hover:text-dark-200'}`}>
          Yandex Cloud
        </button>
        <button onClick={() => setProvider('cloud')}
          className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${provider === 'cloud' ? 'bg-violet-500/20 text-violet-400' : 'text-dark-400 hover:text-dark-200'}`}>
          Cloud.ru
        </button>
        <button onClick={() => setProvider('selectel')}
          className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${provider === 'selectel' ? 'bg-cyan-500/20 text-cyan-400' : 'text-dark-400 hover:text-dark-200'}`}>
          Selectel
        </button>
      </motion.div>

      {provider === 'vk' && (
        <>
          <motion.div className="flex gap-1 mb-6 bg-dark-800/50 p-1 rounded-xl w-fit" variants={itemVariants}>
            {tabs.map((tab) => (
              <motion.button key={tab.id} onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${activeTab === tab.id
                  ? 'bg-accent-500/20 text-accent-400'
                  : 'text-dark-400 hover:text-dark-200 hover:bg-dark-700/50'}`}
                whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}>
                {tab.icon} {tab.label}
              </motion.button>
            ))}
          </motion.div>

          <AnimatePresence mode="wait">
            <motion.div key={activeTab} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }} transition={{ duration: 0.15 }}>
              {activeTab === 'accounts' && <AccountsTab />}
              {activeTab === 'manual' && <ManualTab />}
              {activeTab === 'stats' && <VkStatsTab />}
              {activeTab === 'multiqueue' && <MultiqueueTab />}
              {activeTab === 'settings' && <SettingsTab />}
              {activeTab === 'logs' && <LogsTab />}
            </motion.div>
          </AnimatePresence>
        </>
      )}

      {provider === 'yandex' && <YcPage />}
      {provider === 'cloud' && <CloudPage />}
      {provider === 'selectel' && <SelectelPage />}
    </motion.div>
  )
}
