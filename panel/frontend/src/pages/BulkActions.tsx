import { useState, useEffect, useMemo, useRef, FormEvent } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  Layers,
  Server,
  Network,
  Flame,
  CheckCircle2,
  XCircle,
  Plus,
  Trash2,
  Loader2,
  Check,
  X,
  AlertTriangle,
  Power,
  Play,
  Square,
  Terminal,
  Clock,
  ChevronDown,
  FileCode2,
  Search,
  Folder,
  FolderOpen,
} from 'lucide-react'
import { serversApi, bulkApi, BulkJob, BulkJobAction, BulkResult, BulkTerminalResult, Server as ServerType } from '../api/client'
import { Skeleton } from '../components/ui/Skeleton'
import { Checkbox } from '../components/ui/Checkbox'
import { FAQIcon } from '../components/FAQ'

type ActionType = 'haproxy_service' | 'traffic' | 'firewall' | 'terminal'
type ActionMode = 'create' | 'delete' | 'start' | 'stop' | 'restart'

const TIMEOUT_OPTIONS = [
  { value: 30, label: '30s' },
  { value: 60, label: '1m' },
  { value: 120, label: '2m' },
  { value: 300, label: '5m' },
  { value: 600, label: '10m' },
]

const JOB_POLL_INTERVAL_MS = 1500

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

export default function BulkActions() {
  const { t } = useTranslation()
  
  const [servers, setServers] = useState<ServerType[]>([])
  const [selectedServerIds, setSelectedServerIds] = useState<number[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isExecuting, setIsExecuting] = useState(false)
  
  const [activeType, setActiveType] = useState<ActionType>('haproxy_service')
  const [activeMode, setActiveMode] = useState<ActionMode>('start')
  
  const [results, setResults] = useState<BulkResult[]>([])
  
  // Traffic form state
  const [trafficForm, setTrafficForm] = useState({
    port: '',
  })
  
  // Firewall form state
  const [firewallForm, setFirewallForm] = useState({
    port: '',
    protocol: 'any' as 'tcp' | 'udp' | 'any',
    action: 'allow' as 'allow' | 'deny',
    from_ip: '',
    direction: 'in' as 'in' | 'out',
  })
  
  // Firewall delete form
  const [firewallDeleteForm, setFirewallDeleteForm] = useState({
    port: '',
  })
  
  // Terminal form state
  const [terminalForm, setTerminalForm] = useState({
    command: '',
    timeout: 30,
    shell: 'sh' as 'sh' | 'bash',
  })
  const [bulkScriptMode, setBulkScriptMode] = useState(false)
  const [terminalResults, setTerminalResults] = useState<BulkTerminalResult[]>([])
  const [expandedOutputs, setExpandedOutputs] = useState<Set<number>>(new Set())
  
  const [formError, setFormError] = useState('')
  const [jobProgress, setJobProgress] = useState<{ done: number; total: number } | null>(null)
  const pollCancelledRef = useRef(false)

  const [searchQuery, setSearchQuery] = useState('')
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem('bulk_expanded_folders')
      return raw ? new Set(JSON.parse(raw)) : new Set()
    } catch { return new Set() }
  })

  useEffect(() => {
    fetchServers()
    resumeRunningJob()
    return () => { pollCancelledRef.current = true }
  }, [])

  const fetchServers = async () => {
    setIsLoading(true)
    try {
      const res = await serversApi.list()
      // Деактивированные серверы в массовых действиях не участвуют
      setServers((res.data.servers || []).filter(s => s.is_active))
    } catch {
      // ignore
    } finally {
      setIsLoading(false)
    }
  }

  // Офлайн-ноды бэкенд пропускает мгновенно, индикатор предупреждает об этом заранее
  const statusDotClass = (server: ServerType) => {
    if (server.status === 'online') return 'bg-success'
    if (server.status === 'offline') return 'bg-danger'
    return 'bg-warning'
  }

  const groupedServers = useMemo(() => {
    const folders = new Map<string, ServerType[]>()
    const noFolder: ServerType[] = []
    for (const s of servers) {
      if (s.folder) {
        if (!folders.has(s.folder)) folders.set(s.folder, [])
        folders.get(s.folder)!.push(s)
      } else {
        noFolder.push(s)
      }
    }
    return { folders, noFolder }
  }, [servers])

  const sortedFolderNames = useMemo(() => {
    const allNames = [...groupedServers.folders.keys()]
    try {
      const saved: string[] = JSON.parse(localStorage.getItem('dashboard_folder_order') || '[]')
      const ordered = saved.filter(f => allNames.includes(f))
      const rest = allNames.filter(f => !saved.includes(f)).sort()
      return [...ordered, ...rest]
    } catch {
      return allNames.sort()
    }
  }, [groupedServers.folders])

  const filteredGroups = useMemo(() => {
    const q = searchQuery.toLowerCase().trim()
    if (!q) return { folders: groupedServers.folders, noFolder: groupedServers.noFolder }

    const folders = new Map<string, ServerType[]>()
    for (const [name, svrs] of groupedServers.folders) {
      const matched = svrs.filter(s =>
        s.name.toLowerCase().includes(q) || s.url.toLowerCase().includes(q)
      )
      if (matched.length > 0) folders.set(name, matched)
    }
    const noFolder = groupedServers.noFolder.filter(s =>
      s.name.toLowerCase().includes(q) || s.url.toLowerCase().includes(q)
    )
    return { folders, noFolder }
  }, [searchQuery, groupedServers])

  const hasFolders = groupedServers.folders.size > 0

  const toggleServer = (id: number) => {
    setSelectedServerIds(prev =>
      prev.includes(id)
        ? prev.filter(sid => sid !== id)
        : [...prev, id]
    )
  }

  const toggleFolder = (folderServers: ServerType[]) => {
    const folderIds = folderServers.map(s => s.id)
    const allSelected = folderIds.every(id => selectedServerIds.includes(id))
    if (allSelected) {
      setSelectedServerIds(prev => prev.filter(id => !folderIds.includes(id)))
    } else {
      setSelectedServerIds(prev => [...new Set([...prev, ...folderIds])])
    }
  }

  const getFolderCheckState = (folderServers: ServerType[]): 'none' | 'some' | 'all' => {
    const ids = folderServers.map(s => s.id)
    const count = ids.filter(id => selectedServerIds.includes(id)).length
    if (count === 0) return 'none'
    if (count === ids.length) return 'all'
    return 'some'
  }

  const toggleCollapsed = (folder: string) => {
    setExpandedFolders(prev => {
      const next = new Set(prev)
      if (next.has(folder)) next.delete(folder)
      else next.add(folder)
      localStorage.setItem('bulk_expanded_folders', JSON.stringify([...next]))
      return next
    })
  }

  const selectAll = () => {
    const visibleIds = [
      ...Array.from(filteredGroups.folders.values()).flat(),
      ...filteredGroups.noFolder,
    ].map(s => s.id)
    setSelectedServerIds(prev => [...new Set([...prev, ...visibleIds])])
  }

  const deselectAll = () => {
    const visibleIds = new Set([
      ...Array.from(filteredGroups.folders.values()).flat(),
      ...filteredGroups.noFolder,
    ].map(s => s.id))
    setSelectedServerIds(prev => prev.filter(id => !visibleIds.has(id)))
  }
  
  const toggleOutput = (serverId: number) => {
    setExpandedOutputs(prev => {
      const next = new Set(prev)
      if (next.has(serverId)) next.delete(serverId)
      else next.add(serverId)
      return next
    })
  }
  
  // Задача выполняется в фоне на бэкенде: применяем её состояние по мере поллинга
  const applyJobState = (job: BulkJob) => {
    setJobProgress({ done: job.done, total: job.total })
    setResults(job.results)
    if (job.action === 'terminal_execute') {
      setTerminalResults(job.results.map(r => ({
        ...r,
        stdout: r.stdout ?? '',
        stderr: r.stderr ?? '',
        exit_code: r.exit_code ?? -1,
        execution_time_ms: r.execution_time_ms ?? 0,
      })))
    }
  }

  const notifyJobFinished = (job: BulkJob) => {
    const ok = job.results.filter(r => r.success).length
    const fail = job.results.length - ok
    if (fail === 0) {
      toast.success(t('bulk_actions.all_success', { count: ok }))
    } else if (ok === 0) {
      toast.error(t('bulk_actions.all_failed', { count: fail }))
    } else {
      toast.warning(t('bulk_actions.partial_success', { success: ok, failed: fail }))
    }
  }

  // Сетевые сбои поллинга не останавливают отслеживание — задача на бэкенде
  // продолжается, опрашиваем дальше до её завершения
  const trackJob = async (jobId: string) => {
    setIsExecuting(true)
    try {
      while (!pollCancelledRef.current) {
        let job: BulkJob | null = null
        try {
          const res = await bulkApi.getJob(jobId)
          job = res.data
        } catch (err: unknown) {
          const status = (err as { response?: { status?: number } }).response?.status
          if (status === 404) {
            toast.error(t('bulk_actions.job_lost'))
            return
          }
        }
        if (job) {
          applyJobState(job)
          if (job.status === 'completed') {
            notifyJobFinished(job)
            return
          }
        }
        await sleep(JOB_POLL_INTERVAL_MS)
      }
    } finally {
      setIsExecuting(false)
      setJobProgress(null)
    }
  }

  const resumeRunningJob = async () => {
    try {
      const res = await bulkApi.listJobs()
      const running = res.data.jobs.filter(j => j.status === 'running').pop()
      if (!running) return
      toast.info(t('bulk_actions.job_resumed'))
      await trackJob(running.job_id)
    } catch {
      // ignore
    }
  }

  const resolveJobRequest = (): { action: BulkJobAction; params: Record<string, unknown> } => {
    if (activeType === 'terminal') {
      return {
        action: 'terminal_execute',
        params: {
          command: terminalForm.command,
          timeout: terminalForm.timeout,
          shell: terminalForm.shell,
        },
      }
    }
    if (activeType === 'haproxy_service') {
      if (activeMode === 'start') return { action: 'haproxy_start', params: {} }
      if (activeMode === 'stop') return { action: 'haproxy_stop', params: {} }
      return { action: 'haproxy_restart', params: {} }
    }
    if (activeType === 'traffic') {
      return {
        action: activeMode === 'create' ? 'traffic_port_add' : 'traffic_port_remove',
        params: { port: parseInt(trafficForm.port) },
      }
    }
    if (activeMode === 'create') {
      return {
        action: 'firewall_rule_add',
        params: {
          port: parseInt(firewallForm.port),
          protocol: firewallForm.protocol,
          action: firewallForm.action,
          from_ip: firewallForm.from_ip || null,
          direction: firewallForm.direction,
        },
      }
    }
    return {
      action: 'firewall_rule_delete',
      params: { port: parseInt(firewallDeleteForm.port) },
    }
  }

  const handleExecute = async (e: FormEvent) => {
    e.preventDefault()
    setFormError('')
    setResults([])
    setTerminalResults([])
    setExpandedOutputs(new Set())

    if (selectedServerIds.length === 0) {
      setFormError(t('bulk_actions.no_servers_selected'))
      return
    }

    setIsExecuting(true)
    setJobProgress({ done: 0, total: selectedServerIds.length })

    try {
      const request = resolveJobRequest()
      const res = await bulkApi.createJob(request.action, selectedServerIds, request.params)
      await trackJob(res.data.job_id)
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      setFormError(error.response?.data?.detail || t('common.error'))
      toast.error(t('common.action_failed'))
      setIsExecuting(false)
      setJobProgress(null)
    }
  }
  
  const clearResults = () => {
    setResults([])
  }
  
  const successCount = results.filter(r => r.success).length
  const failedCount = results.filter(r => !r.success).length
  
  if (isLoading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
        <div className="flex items-center gap-3 mb-6">
          <Skeleton className="w-10 h-10 rounded-xl" />
          <div>
            <Skeleton className="h-6 w-48 mb-2" />
            <Skeleton className="h-4 w-72" />
          </div>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-1">
            <div className="card">
              <Skeleton className="h-5 w-40 mb-4" />
              <div className="space-y-2">
                {Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-14 w-full rounded-xl" />
                ))}
              </div>
            </div>
          </div>
          <div className="lg:col-span-2">
            <Skeleton className="h-10 w-full mb-4 rounded-xl" />
            <div className="card">
              <Skeleton className="h-40 w-full" />
            </div>
          </div>
        </div>
      </motion.div>
    )
  }
  
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
    >
      {/* Header */}
      <motion.div className="mb-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
        <div className="flex items-center gap-3 mb-2">
          <motion.div
            className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                       flex items-center justify-center border border-accent-500/20"
            whileHover={{ scale: 1.05 }}
          >
            <Layers className="w-5 h-5 text-accent-400" />
          </motion.div>
          <div>
            <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-2">
              {t('bulk_actions.title')}
              <FAQIcon screen="PAGE_BULK_ACTIONS" />
            </h1>
            <p className="text-dark-400 text-sm">{t('bulk_actions.subtitle')}</p>
          </div>
        </div>
      </motion.div>
      
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left column - Server selection */}
        <motion.div className="lg:col-span-1" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
          <div className="card">
            <div className="flex items-center justify-between mb-4">
              <h2 className="font-semibold text-dark-100 flex items-center gap-2">
                <Server className="w-4 h-4 text-accent-500" />
                {t('bulk_actions.select_servers')}
              </h2>
              <span className="text-xs text-dark-400 bg-dark-800 px-2 py-1 rounded-lg">
                {t('bulk_actions.selected_count', { count: selectedServerIds.length })}
              </span>
            </div>

            {servers.length === 0 ? (
              <div className="text-center py-8">
                <Server className="w-12 h-12 text-dark-600 mx-auto mb-3" />
                <p className="text-dark-400">{t('bulk_actions.no_servers')}</p>
              </div>
            ) : (
              <>
                <div className="flex items-center gap-2 mb-3">
                  <div className="flex-1 flex items-center gap-2 bg-dark-800 border border-dark-600 rounded-lg px-3 py-1.5">
                    <Search className="w-4 h-4 text-dark-400 shrink-0" />
                    <input
                      type="text"
                      value={searchQuery}
                      onChange={e => setSearchQuery(e.target.value)}
                      placeholder={t('bulk_actions.search_servers')}
                      className="bg-transparent text-sm text-dark-100 placeholder-dark-500 outline-none w-full"
                    />
                  </div>
                </div>
                <div className="flex gap-2 mb-4">
                  <button onClick={selectAll} className="btn btn-secondary text-xs py-1.5 px-3">
                    {t('bulk_actions.select_all')}
                  </button>
                  <button onClick={deselectAll} className="btn btn-secondary text-xs py-1.5 px-3">
                    {t('bulk_actions.deselect_all')}
                  </button>
                </div>

                <div className="space-y-1 max-h-[400px] overflow-y-auto pr-2">
                  {hasFolders ? (
                    <>
                      {sortedFolderNames
                        .filter(name => filteredGroups.folders.has(name))
                        .map(folderName => {
                          const folderServers = filteredGroups.folders.get(folderName)!
                          const allFolderServers = groupedServers.folders.get(folderName)!
                          const checkState = getFolderCheckState(allFolderServers)
                          const isCollapsed = !expandedFolders.has(folderName)
                          const selectedInFolder = allFolderServers.filter(s => selectedServerIds.includes(s.id)).length

                          return (
                            <div key={folderName} className="mb-1">
                              <div className="flex items-center gap-2 p-2 rounded-lg hover:bg-dark-800/50 transition-colors">
                                <Checkbox
                                  checked={checkState === 'all'}
                                  indeterminate={checkState === 'some'}
                                  onChange={() => toggleFolder(allFolderServers)}
                                />
                                <div
                                  className="flex items-center gap-2 flex-1 min-w-0 cursor-pointer"
                                  onClick={() => toggleCollapsed(folderName)}
                                >
                                  {isCollapsed
                                    ? <Folder className="w-4 h-4 text-accent-400 shrink-0" />
                                    : <FolderOpen className="w-4 h-4 text-accent-400 shrink-0" />
                                  }
                                  <span className="font-medium text-sm text-dark-200 truncate">{folderName}</span>
                                  <span className="text-xs text-dark-500 ml-auto shrink-0">{selectedInFolder}/{allFolderServers.length}</span>
                                  <motion.div
                                    animate={{ rotate: isCollapsed ? -90 : 0 }}
                                    transition={{ duration: 0.15 }}
                                  >
                                    <ChevronDown className="w-3.5 h-3.5 text-dark-500" />
                                  </motion.div>
                                </div>
                              </div>
                              <AnimatePresence initial={false}>
                                {!isCollapsed && (
                                  <motion.div
                                    initial={{ height: 0, opacity: 0 }}
                                    animate={{ height: 'auto', opacity: 1 }}
                                    exit={{ height: 0, opacity: 0 }}
                                    transition={{ duration: 0.15 }}
                                    className="overflow-hidden"
                                  >
                                    <div className="pl-6 space-y-1 pt-1">
                                      {folderServers.map(server => (
                                        <motion.label
                                          key={server.id}
                                          className={`flex items-center gap-3 p-2 rounded-xl cursor-pointer transition-all
                                            ${selectedServerIds.includes(server.id)
                                              ? 'bg-accent-500/10 border border-accent-500/30'
                                              : 'bg-dark-800/50 border border-transparent hover:bg-dark-800'
                                            }`}
                                          whileHover={{ scale: 1.01 }}
                                          whileTap={{ scale: 0.99 }}
                                        >
                                          <Checkbox
                                            checked={selectedServerIds.includes(server.id)}
                                            onChange={() => toggleServer(server.id)}
                                          />
                                          <div className="flex-1 min-w-0">
                                            <p className="font-medium text-sm text-dark-100 truncate">{server.name}</p>
                                            <p className="text-xs text-dark-500 truncate">{server.url}</p>
                                          </div>
                                          <div className={`w-2 h-2 rounded-full shrink-0 ${statusDotClass(server)}`} />
                                        </motion.label>
                                      ))}
                                    </div>
                                  </motion.div>
                                )}
                              </AnimatePresence>
                            </div>
                          )
                        })}

                      {filteredGroups.noFolder.length > 0 && (() => {
                        const checkState = getFolderCheckState(groupedServers.noFolder)
                        const isCollapsed = !expandedFolders.has('__no_folder__')
                        const selectedInGroup = groupedServers.noFolder.filter(s => selectedServerIds.includes(s.id)).length

                        return (
                          <div className="mb-1">
                            <div className="flex items-center gap-2 p-2 rounded-lg hover:bg-dark-800/50 transition-colors">
                              <Checkbox
                                checked={checkState === 'all'}
                                indeterminate={checkState === 'some'}
                                onChange={() => toggleFolder(groupedServers.noFolder)}
                              />
                              <div
                                className="flex items-center gap-2 flex-1 min-w-0 cursor-pointer"
                                onClick={() => toggleCollapsed('__no_folder__')}
                              >
                                <Server className="w-4 h-4 text-dark-400 shrink-0" />
                                <span className="font-medium text-sm text-dark-400 truncate">{t('bulk_actions.no_folder')}</span>
                                <span className="text-xs text-dark-500 ml-auto shrink-0">{selectedInGroup}/{groupedServers.noFolder.length}</span>
                                <motion.div
                                  animate={{ rotate: isCollapsed ? -90 : 0 }}
                                  transition={{ duration: 0.15 }}
                                >
                                  <ChevronDown className="w-3.5 h-3.5 text-dark-500" />
                                </motion.div>
                              </div>
                            </div>
                            <AnimatePresence initial={false}>
                              {!isCollapsed && (
                                <motion.div
                                  initial={{ height: 0, opacity: 0 }}
                                  animate={{ height: 'auto', opacity: 1 }}
                                  exit={{ height: 0, opacity: 0 }}
                                  transition={{ duration: 0.15 }}
                                  className="overflow-hidden"
                                >
                                  <div className="pl-6 space-y-1 pt-1">
                                    {filteredGroups.noFolder.map(server => (
                                      <motion.label
                                        key={server.id}
                                        className={`flex items-center gap-3 p-2 rounded-xl cursor-pointer transition-all
                                          ${selectedServerIds.includes(server.id)
                                            ? 'bg-accent-500/10 border border-accent-500/30'
                                            : 'bg-dark-800/50 border border-transparent hover:bg-dark-800'
                                          }`}
                                        whileHover={{ scale: 1.01 }}
                                        whileTap={{ scale: 0.99 }}
                                      >
                                        <Checkbox
                                          checked={selectedServerIds.includes(server.id)}
                                          onChange={() => toggleServer(server.id)}
                                        />
                                        <div className="flex-1 min-w-0">
                                          <p className="font-medium text-sm text-dark-100 truncate">{server.name}</p>
                                          <p className="text-xs text-dark-500 truncate">{server.url}</p>
                                        </div>
                                        <div className={`w-2 h-2 rounded-full shrink-0 ${statusDotClass(server)}`} />
                                      </motion.label>
                                    ))}
                                  </div>
                                </motion.div>
                              )}
                            </AnimatePresence>
                          </div>
                        )
                      })()}

                      {filteredGroups.folders.size === 0 && filteredGroups.noFolder.length === 0 && (
                        <div className="text-center py-6">
                          <Search className="w-8 h-8 text-dark-600 mx-auto mb-2" />
                          <p className="text-dark-400 text-sm">{t('bulk_actions.no_results')}</p>
                        </div>
                      )}
                    </>
                  ) : (
                    <>
                      {filteredGroups.noFolder.map(server => (
                        <motion.label
                          key={server.id}
                          className={`flex items-center gap-3 p-3 rounded-xl cursor-pointer transition-all
                            ${selectedServerIds.includes(server.id)
                              ? 'bg-accent-500/10 border border-accent-500/30'
                              : 'bg-dark-800/50 border border-transparent hover:bg-dark-800'
                            }`}
                          whileHover={{ scale: 1.01 }}
                          whileTap={{ scale: 0.99 }}
                        >
                          <Checkbox
                            checked={selectedServerIds.includes(server.id)}
                            onChange={() => toggleServer(server.id)}
                          />
                          <div className="flex-1 min-w-0">
                            <p className="font-medium text-dark-100 truncate">{server.name}</p>
                            <p className="text-xs text-dark-500 truncate">{server.url}</p>
                          </div>
                          <div className={`w-2 h-2 rounded-full ${statusDotClass(server)}`} />
                        </motion.label>
                      ))}
                      {filteredGroups.noFolder.length === 0 && (
                        <div className="text-center py-6">
                          <Search className="w-8 h-8 text-dark-600 mx-auto mb-2" />
                          <p className="text-dark-400 text-sm">{t('bulk_actions.no_results')}</p>
                        </div>
                      )}
                    </>
                  )}
                </div>
              </>
            )}
          </div>
        </motion.div>
        
        {/* Right column - Action forms */}
        <motion.div className="lg:col-span-2" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
          {/* Action type tabs */}
          <div className="flex flex-wrap gap-2 mb-4">
            {[
              { type: 'haproxy_service' as const, icon: Power, label: t('bulk_actions.haproxy_service') },
              { type: 'traffic' as const, icon: Network, label: t('bulk_actions.traffic_ports') },
              { type: 'firewall' as const, icon: Flame, label: t('bulk_actions.firewall_rules') },
              { type: 'terminal' as const, icon: Terminal, label: t('bulk_actions.terminal') },
            ].map(({ type, icon: Icon, label }) => (
              <motion.button
                key={type}
                onClick={() => {
                  setActiveType(type)
                  if (type === 'haproxy_service') {
                    setActiveMode('start')
                  } else {
                    setActiveMode('create')
                  }
                  setResults([])
                  setTerminalResults([])
                  setExpandedOutputs(new Set())
                  setFormError('')
                }}
                className={`flex items-center gap-2 px-4 py-2.5 rounded-xl font-medium transition-all
                  ${activeType === type
                    ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                    : 'bg-dark-800/50 text-dark-400 border border-transparent hover:bg-dark-800'
                  }`}
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
              >
                <Icon className="w-4 h-4" />
                <span className="hidden sm:inline">{label}</span>
              </motion.button>
            ))}
          </div>
          
          {/* Action mode tabs */}
          {activeType !== 'terminal' && (
          <div className="flex gap-2 mb-4">
            {activeType === 'haproxy_service' ? (
              <>
                <motion.button
                  onClick={() => {
                    setActiveMode('start')
                    setResults([])
                    setFormError('')
                  }}
                  className={`flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all
                    ${activeMode === 'start'
                      ? 'bg-success/20 text-success border border-success/30'
                      : 'bg-dark-800/50 text-dark-400 border border-transparent hover:bg-dark-800'
                    }`}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  <Play className="w-4 h-4" />
                  {t('bulk_actions.start')}
                </motion.button>
                <motion.button
                  onClick={() => {
                    setActiveMode('stop')
                    setResults([])
                    setFormError('')
                  }}
                  className={`flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all
                    ${activeMode === 'stop'
                      ? 'bg-danger/20 text-danger border border-danger/30'
                      : 'bg-dark-800/50 text-dark-400 border border-transparent hover:bg-dark-800'
                    }`}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  <Square className="w-4 h-4" />
                  {t('bulk_actions.stop')}
                </motion.button>
                <motion.button
                  onClick={() => {
                    setActiveMode('restart')
                    setResults([])
                    setFormError('')
                  }}
                  className={`flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all
                    ${activeMode === 'restart'
                      ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                      : 'bg-dark-800/50 text-dark-400 border border-transparent hover:bg-dark-800'
                    }`}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  <Power className="w-4 h-4" />
                  {t('bulk_actions.restart')}
                </motion.button>
              </>
            ) : (
              <>
                <motion.button
                  onClick={() => {
                    setActiveMode('create')
                    setResults([])
                    setFormError('')
                  }}
                  className={`flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all
                    ${activeMode === 'create'
                      ? 'bg-success/20 text-success border border-success/30'
                      : 'bg-dark-800/50 text-dark-400 border border-transparent hover:bg-dark-800'
                    }`}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  <Plus className="w-4 h-4" />
                  {t('bulk_actions.create')}
                </motion.button>
                <motion.button
                  onClick={() => {
                    setActiveMode('delete')
                    setResults([])
                    setFormError('')
                  }}
                  className={`flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all
                    ${activeMode === 'delete'
                      ? 'bg-danger/20 text-danger border border-danger/30'
                      : 'bg-dark-800/50 text-dark-400 border border-transparent hover:bg-dark-800'
                    }`}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  <Trash2 className="w-4 h-4" />
                  {t('bulk_actions.delete')}
                </motion.button>
              </>
            )}
          </div>
          )}
          
          {/* Form */}
          <div className="card">
            <form onSubmit={handleExecute}>
              {/* HAProxy Service Forms */}
              {activeType === 'haproxy_service' && (
                <div className="space-y-4">
                  <div className="flex items-start gap-3 p-4 bg-dark-800/50 rounded-xl">
                    <Power className={`w-5 h-5 mt-0.5 shrink-0 ${activeMode === 'start' ? 'text-success' : activeMode === 'restart' ? 'text-accent-400' : 'text-danger'}`} />
                    <div>
                      <p className="text-dark-100 font-medium">
                        {activeMode === 'start' ? t('bulk_actions.start_haproxy_title') : activeMode === 'restart' ? t('bulk_actions.restart_haproxy_title') : t('bulk_actions.stop_haproxy_title')}
                      </p>
                      <p className="text-sm text-dark-400 mt-1">
                        {activeMode === 'start' ? t('bulk_actions.start_haproxy_hint') : activeMode === 'restart' ? t('bulk_actions.restart_haproxy_hint') : t('bulk_actions.stop_haproxy_hint')}
                      </p>
                    </div>
                  </div>
                </div>
              )}
              
              {/* Traffic Forms */}
              {activeType === 'traffic' && (
                <div className="space-y-4">
                  {activeMode === 'delete' && (
                    <div className="flex items-start gap-2 p-3 bg-dark-800/50 rounded-lg text-sm text-dark-400">
                      <AlertTriangle className="w-4 h-4 text-warning mt-0.5 shrink-0" />
                      {t('bulk_actions.delete_traffic_hint')}
                    </div>
                  )}
                  <div>
                    <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.port')}</label>
                    <input
                      type="number"
                      value={trafficForm.port}
                      onChange={e => setTrafficForm(prev => ({ ...prev, port: e.target.value }))}
                      className="input w-full"
                      min="1"
                      max="65535"
                      placeholder="443"
                      required
                    />
                  </div>
                </div>
              )}
              
              {/* Firewall Forms */}
              {activeType === 'firewall' && activeMode === 'create' && (
                <div className="space-y-4">
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.port')}</label>
                      <input
                        type="number"
                        value={firewallForm.port}
                        onChange={e => setFirewallForm(prev => ({ ...prev, port: e.target.value }))}
                        className="input w-full"
                        min="1"
                        max="65535"
                        required
                      />
                    </div>
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.protocol')}</label>
                      <select
                        value={firewallForm.protocol}
                        onChange={e => setFirewallForm(prev => ({ ...prev, protocol: e.target.value as 'tcp' | 'udp' | 'any' }))}
                        className="input w-full"
                      >
                        <option value="any">TCP/UDP</option>
                        <option value="tcp">TCP</option>
                        <option value="udp">UDP</option>
                      </select>
                    </div>
                  </div>
                  
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.action')}</label>
                      <select
                        value={firewallForm.action}
                        onChange={e => setFirewallForm(prev => ({ ...prev, action: e.target.value as 'allow' | 'deny' }))}
                        className="input w-full"
                      >
                        <option value="allow">{t('bulk_actions.allow')}</option>
                        <option value="deny">{t('bulk_actions.deny')}</option>
                      </select>
                    </div>
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.direction')}</label>
                      <select
                        value={firewallForm.direction}
                        onChange={e => setFirewallForm(prev => ({ ...prev, direction: e.target.value as 'in' | 'out' }))}
                        className="input w-full"
                      >
                        <option value="in">{t('bulk_actions.incoming')}</option>
                        <option value="out">{t('bulk_actions.outgoing')}</option>
                      </select>
                    </div>
                  </div>
                  
                  <div>
                    <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.from_ip')} ({t('firewall.from_hint')})</label>
                    <input
                      type="text"
                      value={firewallForm.from_ip}
                      onChange={e => setFirewallForm(prev => ({ ...prev, from_ip: e.target.value }))}
                      className="input w-full"
                      placeholder={t('firewall.from_placeholder')}
                    />
                  </div>
                </div>
              )}
              
              {activeType === 'firewall' && activeMode === 'delete' && (
                <div className="space-y-4">
                  <div className="flex items-start gap-2 p-3 bg-dark-800/50 rounded-lg text-sm text-dark-400">
                    <AlertTriangle className="w-4 h-4 text-warning mt-0.5 shrink-0" />
                    {t('bulk_actions.delete_firewall_hint')}
                  </div>
                  <div>
                    <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.port')}</label>
                    <input
                      type="number"
                      value={firewallDeleteForm.port}
                      onChange={e => setFirewallDeleteForm(prev => ({ ...prev, port: e.target.value }))}
                      className="input w-full"
                      min="1"
                      max="65535"
                      required
                    />
                  </div>
                </div>
              )}
              
              {/* Terminal Form */}
              {activeType === 'terminal' && (
                <div className="space-y-4">
                  <div className="flex items-start gap-3 p-4 bg-dark-800/50 rounded-xl">
                    <Terminal className="w-5 h-5 mt-0.5 shrink-0 text-accent-500" />
                    <div className="flex-1">
                      <p className="text-dark-100 font-medium flex items-center gap-2">
                        {t('bulk_actions.terminal')}
                        <FAQIcon screen="BULK_ACTIONS_TERMINAL" size="sm" />
                      </p>
                      <p className="text-sm text-dark-400 mt-1">
                        {bulkScriptMode ? t('bulk_actions.terminal_script_hint') : t('bulk_actions.terminal_hint')}
                      </p>
                    </div>
                  </div>

                  <div>
                    <div className="flex items-center justify-between mb-1.5">
                      <label className="text-sm text-dark-400">
                        {bulkScriptMode ? t('bulk_actions.terminal_script') : t('bulk_actions.terminal_command')}
                      </label>
                      <button
                        type="button"
                        onClick={() => {
                          setBulkScriptMode(prev => {
                            if (!prev) setTerminalForm(f => ({ ...f, shell: 'bash' }))
                            return !prev
                          })
                        }}
                        className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs transition-all ${
                          bulkScriptMode
                            ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                            : 'text-dark-400 hover:text-dark-200 hover:bg-dark-700'
                        }`}
                      >
                        <FileCode2 className="w-3.5 h-3.5" />
                        {t('bulk_actions.terminal_script_mode')}
                      </button>
                    </div>
                    {bulkScriptMode ? (
                      <textarea
                        value={terminalForm.command}
                        onChange={e => setTerminalForm(prev => ({ ...prev, command: e.target.value }))}
                        onKeyDown={e => {
                          if (e.key === 'Tab') {
                            e.preventDefault()
                            const target = e.target as HTMLTextAreaElement
                            const start = target.selectionStart
                            const end = target.selectionEnd
                            const newValue = terminalForm.command.substring(0, start) + '    ' + terminalForm.command.substring(end)
                            setTerminalForm(prev => ({ ...prev, command: newValue }))
                            requestAnimationFrame(() => {
                              target.selectionStart = target.selectionEnd = start + 4
                            })
                          }
                        }}
                        className="w-full h-64 bg-dark-950 border border-dark-700 rounded-xl p-4
                                   font-mono text-sm text-dark-200 resize-y focus:outline-none
                                   focus:border-accent-500/50 focus:ring-1 focus:ring-accent-500/20
                                   scrollbar-thin scrollbar-thumb-dark-700 scrollbar-track-transparent"
                        placeholder={t('bulk_actions.terminal_script_placeholder')}
                        spellCheck={false}
                        required
                      />
                    ) : (
                      <input
                        type="text"
                        value={terminalForm.command}
                        onChange={e => setTerminalForm(prev => ({ ...prev, command: e.target.value }))}
                        className="input w-full font-mono text-sm"
                        placeholder={t('bulk_actions.terminal_command_placeholder')}
                        required
                      />
                    )}
                  </div>

                  <div className="flex flex-wrap items-center gap-4">
                    <div className="flex items-center gap-2">
                      <Clock className="w-4 h-4 text-dark-400" />
                      <label className="text-sm text-dark-400">{t('bulk_actions.terminal_timeout')}</label>
                      <select
                        value={terminalForm.timeout}
                        onChange={e => setTerminalForm(prev => ({ ...prev, timeout: Number(e.target.value) }))}
                        className="input py-1.5 px-2 text-xs w-20"
                      >
                        {TIMEOUT_OPTIONS.map(opt => (
                          <option key={opt.value} value={opt.value}>{opt.label}</option>
                        ))}
                      </select>
                    </div>

                    <div className="flex items-center gap-2">
                      <Terminal className="w-4 h-4 text-dark-400" />
                      <label className="text-sm text-dark-400">{t('bulk_actions.terminal_shell')}</label>
                      <select
                        value={terminalForm.shell}
                        onChange={e => setTerminalForm(prev => ({ ...prev, shell: e.target.value as 'sh' | 'bash' }))}
                        className="input py-1.5 px-2 text-xs w-20"
                      >
                        <option value="sh">sh</option>
                        <option value="bash">bash</option>
                      </select>
                    </div>
                  </div>
                </div>
              )}
              
              {formError && (
                <motion.div
                  className="mt-4 p-3 bg-danger/10 border border-danger/30 rounded-lg text-danger text-sm flex items-center gap-2"
                  initial={{ opacity: 0, y: -10 }}
                  animate={{ opacity: 1, y: 0 }}
                >
                  <XCircle className="w-4 h-4 shrink-0" />
                  {formError}
                </motion.div>
              )}
              
              <div className="mt-6 flex gap-3">
                <motion.button
                  type="submit"
                  disabled={isExecuting || selectedServerIds.length === 0}
                  className={`btn flex items-center gap-2 ${
                    activeType === 'terminal' || activeMode === 'create' || activeMode === 'start' || activeMode === 'restart'
                      ? 'btn-primary'
                      : 'bg-danger hover:bg-danger/80 text-white'
                  }`}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {isExecuting ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      {t('bulk_actions.executing')}
                    </>
                  ) : (
                    <>
                      {activeType === 'terminal' && <Play className="w-4 h-4" />}
                      {activeType !== 'terminal' && activeMode === 'create' && <Plus className="w-4 h-4" />}
                      {activeType !== 'terminal' && activeMode === 'delete' && <Trash2 className="w-4 h-4" />}
                      {activeType !== 'terminal' && activeMode === 'start' && <Play className="w-4 h-4" />}
                      {activeType !== 'terminal' && activeMode === 'stop' && <Square className="w-4 h-4" />}
                      {activeType !== 'terminal' && activeMode === 'restart' && <Power className="w-4 h-4" />}
                      {t('bulk_actions.execute')}
                    </>
                  )}
                </motion.button>
              </div>

              {isExecuting && jobProgress && (
                <motion.div
                  className="mt-4 p-3 bg-dark-800/50 border border-dark-600 rounded-lg"
                  initial={{ opacity: 0, y: -10 }}
                  animate={{ opacity: 1, y: 0 }}
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-sm text-dark-300 flex items-center gap-2">
                      <Loader2 className="w-4 h-4 animate-spin text-accent-400" />
                      {t('bulk_actions.job_progress', { done: jobProgress.done, total: jobProgress.total })}
                    </span>
                  </div>
                  <div className="h-2 bg-dark-800 rounded-full overflow-hidden mb-2">
                    <div
                      className="h-full bg-accent-500 transition-all duration-300"
                      style={{ width: `${jobProgress.total > 0 ? (jobProgress.done / jobProgress.total) * 100 : 0}%` }}
                    />
                  </div>
                  <p className="text-xs text-dark-500">{t('bulk_actions.job_background_hint')}</p>
                </motion.div>
              )}
            </form>
          </div>
          
          {/* Results */}
          <AnimatePresence>
            {results.length > 0 && (
              <motion.div
                className="card mt-4"
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -20 }}
              >
                <div className="flex items-center justify-between mb-4">
                  <h3 className="font-semibold text-dark-100 flex items-center gap-2">
                    {t('bulk_actions.results')}
                    {successCount > 0 && (
                      <span className="text-xs bg-success/20 text-success px-2 py-0.5 rounded-full">
                        {successCount} {t('bulk_actions.success')}
                      </span>
                    )}
                    {failedCount > 0 && (
                      <span className="text-xs bg-danger/20 text-danger px-2 py-0.5 rounded-full">
                        {failedCount} {t('bulk_actions.failed')}
                      </span>
                    )}
                  </h3>
                  <button
                    onClick={clearResults}
                    className="text-xs text-dark-400 hover:text-dark-200 flex items-center gap-1"
                  >
                    <X className="w-3 h-3" />
                    {t('bulk_actions.clear_results')}
                  </button>
                </div>
                
                <div className="space-y-2 max-h-[500px] overflow-y-auto">
                  {results.map((result, index) => {
                    const termResult = terminalResults.find(tr => tr.server_id === result.server_id)
                    const hasOutput = termResult && (termResult.stdout || termResult.stderr)
                    const isExpanded = expandedOutputs.has(result.server_id)
                    
                    return (
                      <motion.div
                        key={`${result.server_id}-${index}`}
                        className={`rounded-lg overflow-hidden ${
                          result.success ? 'bg-success/10' : 'bg-danger/10'
                        }`}
                        initial={{ opacity: 0, x: -10 }}
                        animate={{ opacity: 1, x: 0 }}
                        transition={{ delay: index * 0.05 }}
                      >
                        <div className="flex items-center gap-3 p-3">
                          {result.success ? (
                            <CheckCircle2 className="w-5 h-5 text-success shrink-0" />
                          ) : (
                            <XCircle className="w-5 h-5 text-danger shrink-0" />
                          )}
                          <div className="flex-1 min-w-0">
                            <p className="font-medium text-dark-100">{result.server_name}</p>
                            <div className="flex items-center gap-2">
                              <p className={`text-sm ${result.success ? 'text-success' : 'text-danger'}`}>
                                {result.message}
                              </p>
                              {termResult && termResult.execution_time_ms > 0 && (
                                <span className="text-xs text-dark-500">
                                  {t('bulk_actions.terminal_exec_time', { time: termResult.execution_time_ms })}
                                </span>
                              )}
                            </div>
                          </div>
                          {hasOutput ? (
                            <button
                              onClick={() => toggleOutput(result.server_id)}
                              className="flex items-center gap-1 text-xs text-dark-400 hover:text-dark-200 transition-colors px-2 py-1"
                            >
                              <motion.div
                                animate={{ rotate: isExpanded ? 180 : 0 }}
                                transition={{ duration: 0.2 }}
                              >
                                <ChevronDown className="w-4 h-4" />
                              </motion.div>
                              {isExpanded ? t('bulk_actions.terminal_hide_output') : t('bulk_actions.terminal_show_output')}
                            </button>
                          ) : (
                            result.success ? (
                              <Check className="w-4 h-4 text-success" />
                            ) : (
                              <X className="w-4 h-4 text-danger" />
                            )
                          )}
                        </div>
                        
                        <AnimatePresence>
                          {hasOutput && isExpanded && (
                            <motion.div
                              initial={{ height: 0, opacity: 0 }}
                              animate={{ height: 'auto', opacity: 1 }}
                              exit={{ height: 0, opacity: 0 }}
                              transition={{ duration: 0.2 }}
                              className="overflow-hidden"
                            >
                              <div className="px-3 pb-3">
                                <div className="bg-dark-950 rounded-lg p-3 font-mono text-xs max-h-48 overflow-y-auto border border-dark-800">
                                  {termResult.stdout && (
                                    <div className="whitespace-pre-wrap break-all text-success">
                                      {termResult.stdout}
                                    </div>
                                  )}
                                  {termResult.stderr && (
                                    <div className="whitespace-pre-wrap break-all text-danger mt-1">
                                      {termResult.stderr}
                                    </div>
                                  )}
                                </div>
                              </div>
                            </motion.div>
                          )}
                        </AnimatePresence>
                      </motion.div>
                    )
                  })}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>
      </div>
    </motion.div>
  )
}
