import { useState, useEffect, FormEvent } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  Layers,
  Server,
  Shield,
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
  Code,
  Shuffle,
  Clock,
  Save,
  ChevronDown,
} from 'lucide-react'
import { serversApi, bulkApi, BulkResult, BulkTerminalResult, Server as ServerType } from '../api/client'
import { Skeleton } from '../components/ui/Skeleton'
import { Checkbox } from '../components/ui/Checkbox'

type ActionType = 'haproxy_service' | 'haproxy' | 'traffic' | 'firewall' | 'terminal' | 'haproxy_config'
type ActionMode = 'create' | 'delete' | 'start' | 'stop'

const RULES_START_MARKER = '# === RULES START ==='
const RULES_END_MARKER = '# === RULES END ==='

const DEFAULT_HAPROXY_TEMPLATE = `global
    stats socket /var/run/haproxy.sock mode 660 level admin expose-fd listeners
    no log
    tune.bufsize 32768
    tune.maxpollevents 1024
    tune.recv_enough 16384

defaults
    mode tcp
    timeout connect 5s
    timeout client 30m
    timeout server 30m
    timeout tunnel 2h
    timeout client-fin 5s
    timeout server-fin 5s
    option dontlognull
    option redispatch
    option tcp-smart-accept
    option tcp-smart-connect
    option splice-auto
    option clitcpka
    option srvtcpka

${RULES_START_MARKER}
${RULES_END_MARKER}
`

const TIMEOUT_OPTIONS = [
  { value: 30, label: '30s' },
  { value: 60, label: '1m' },
  { value: 120, label: '2m' },
  { value: 300, label: '5m' },
  { value: 600, label: '10m' },
]

export default function BulkActions() {
  const { t } = useTranslation()
  
  const [servers, setServers] = useState<ServerType[]>([])
  const [selectedServerIds, setSelectedServerIds] = useState<number[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isExecuting, setIsExecuting] = useState(false)
  
  const [activeType, setActiveType] = useState<ActionType>('haproxy_service')
  const [activeMode, setActiveMode] = useState<ActionMode>('start')
  
  const [results, setResults] = useState<BulkResult[]>([])
  
  // HAProxy form state
  const [haproxyForm, setHaproxyForm] = useState({
    name: '',
    rule_type: 'tcp' as 'tcp' | 'https',
    listen_port: '',
    target_ip: '',
    target_port: '',
    cert_domain: '',
    target_ssl: false,
    send_proxy: false,
  })
  
  // HAProxy delete form
  const [haproxyDeleteForm, setHaproxyDeleteForm] = useState({
    listen_port: '',
    target_ip: '',
    target_port: '',
  })
  
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
  const [terminalResults, setTerminalResults] = useState<BulkTerminalResult[]>([])
  const [expandedOutputs, setExpandedOutputs] = useState<Set<number>>(new Set())
  
  // HAProxy config form state
  const [configContent, setConfigContent] = useState(DEFAULT_HAPROXY_TEMPLATE)
  const [configReloadAfter, setConfigReloadAfter] = useState(true)
  
  const [formError, setFormError] = useState('')
  
  useEffect(() => {
    fetchServers()
  }, [])
  
  const fetchServers = async () => {
    setIsLoading(true)
    try {
      const res = await serversApi.list()
      setServers(res.data.servers || [])
    } catch {
      // ignore
    } finally {
      setIsLoading(false)
    }
  }
  
  const toggleServer = (id: number) => {
    setSelectedServerIds(prev => 
      prev.includes(id) 
        ? prev.filter(sid => sid !== id)
        : [...prev, id]
    )
  }
  
  const selectAll = () => {
    setSelectedServerIds(servers.map(s => s.id))
  }
  
  const deselectAll = () => {
    setSelectedServerIds([])
  }
  
  const toggleOutput = (serverId: number) => {
    setExpandedOutputs(prev => {
      const next = new Set(prev)
      if (next.has(serverId)) next.delete(serverId)
      else next.add(serverId)
      return next
    })
  }
  
  const handleApplyDefaultTemplate = () => {
    const startIdx = configContent.indexOf(RULES_START_MARKER)
    const endIdx = configContent.indexOf(RULES_END_MARKER)
    
    let rulesContent = ''
    if (startIdx !== -1 && endIdx !== -1 && endIdx > startIdx) {
      rulesContent = configContent.slice(startIdx + RULES_START_MARKER.length, endIdx).trim()
    }
    
    let newConfig = DEFAULT_HAPROXY_TEMPLATE
    if (rulesContent) {
      newConfig = newConfig.replace(
        RULES_END_MARKER,
        rulesContent + '\n' + RULES_END_MARKER
      )
    }
    
    setConfigContent(newConfig)
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
    
    try {
      let response: BulkResult[] = []
      
      if (activeType === 'terminal') {
        const res = await bulkApi.executeCommand(
          selectedServerIds,
          terminalForm.command,
          terminalForm.timeout,
          terminalForm.shell
        )
        setTerminalResults(res.data)
        response = res.data
      } else if (activeType === 'haproxy_config') {
        const res = await bulkApi.applyHAProxyConfig(
          selectedServerIds,
          configContent,
          configReloadAfter
        )
        response = res.data
      } else if (activeType === 'haproxy_service') {
        if (activeMode === 'start') {
          const res = await bulkApi.startHAProxy(selectedServerIds)
          response = res.data
        } else if (activeMode === 'stop') {
          const res = await bulkApi.stopHAProxy(selectedServerIds)
          response = res.data
        }
      } else if (activeType === 'haproxy') {
        if (activeMode === 'create') {
          const res = await bulkApi.createHAProxyRule(selectedServerIds, {
            name: haproxyForm.name,
            rule_type: haproxyForm.rule_type,
            listen_port: parseInt(haproxyForm.listen_port),
            target_ip: haproxyForm.target_ip,
            target_port: parseInt(haproxyForm.target_port),
            cert_domain: haproxyForm.cert_domain || undefined,
            target_ssl: haproxyForm.target_ssl,
            send_proxy: haproxyForm.send_proxy,
          })
          response = res.data
        } else {
          const res = await bulkApi.deleteHAProxyRule(
            selectedServerIds,
            parseInt(haproxyDeleteForm.listen_port),
            haproxyDeleteForm.target_ip,
            parseInt(haproxyDeleteForm.target_port)
          )
          response = res.data
        }
      } else if (activeType === 'traffic') {
        const port = parseInt(trafficForm.port)
        if (activeMode === 'create') {
          const res = await bulkApi.addTrackedPort(selectedServerIds, port)
          response = res.data
        } else {
          const res = await bulkApi.removeTrackedPort(selectedServerIds, port)
          response = res.data
        }
      } else if (activeType === 'firewall') {
        if (activeMode === 'create') {
          const res = await bulkApi.addFirewallRule(selectedServerIds, {
            port: parseInt(firewallForm.port),
            protocol: firewallForm.protocol,
            action: firewallForm.action,
            from_ip: firewallForm.from_ip || null,
            direction: firewallForm.direction,
          })
          response = res.data
        } else {
          const res = await bulkApi.deleteFirewallRule(
            selectedServerIds,
            parseInt(firewallDeleteForm.port)
          )
          response = res.data
        }
      }
      
      setResults(response)
      const ok = response.filter(r => r.success).length
      const fail = response.filter(r => !r.success).length
      if (fail === 0) {
        toast.success(t('bulk_actions.all_success', { count: ok }))
      } else if (ok === 0) {
        toast.error(t('bulk_actions.all_failed', { count: fail }))
      } else {
        toast.warning(t('bulk_actions.partial_success', { success: ok, failed: fail }))
      }
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      setFormError(error.response?.data?.detail || t('common.error'))
      toast.error(t('common.action_failed'))
    } finally {
      setIsExecuting(false)
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
            <h1 className="text-2xl font-bold text-dark-50">{t('bulk_actions.title')}</h1>
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
                <div className="flex gap-2 mb-4">
                  <button
                    onClick={selectAll}
                    className="btn btn-secondary text-xs py-1.5 px-3"
                  >
                    {t('bulk_actions.select_all')}
                  </button>
                  <button
                    onClick={deselectAll}
                    className="btn btn-secondary text-xs py-1.5 px-3"
                  >
                    {t('bulk_actions.deselect_all')}
                  </button>
                </div>
                
                <div className="space-y-2 max-h-[400px] overflow-y-auto pr-2">
                  {servers.map(server => (
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
                      <div className={`w-2 h-2 rounded-full ${server.is_active ? 'bg-success' : 'bg-dark-600'}`} />
                    </motion.label>
                  ))}
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
              { type: 'haproxy' as const, icon: Shield, label: t('bulk_actions.haproxy_rules') },
              { type: 'haproxy_config' as const, icon: Code, label: t('bulk_actions.haproxy_config') },
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
          {activeType !== 'terminal' && activeType !== 'haproxy_config' && (
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
                    <Power className={`w-5 h-5 mt-0.5 shrink-0 ${activeMode === 'start' ? 'text-success' : 'text-danger'}`} />
                    <div>
                      <p className="text-dark-100 font-medium">
                        {activeMode === 'start' ? t('bulk_actions.start_haproxy_title') : t('bulk_actions.stop_haproxy_title')}
                      </p>
                      <p className="text-sm text-dark-400 mt-1">
                        {activeMode === 'start' ? t('bulk_actions.start_haproxy_hint') : t('bulk_actions.stop_haproxy_hint')}
                      </p>
                    </div>
                  </div>
                </div>
              )}
              
              {/* HAProxy Rules Forms */}
              {activeType === 'haproxy' && activeMode === 'create' && (
                <div className="space-y-4">
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.rule_name')}</label>
                      <input
                        type="text"
                        value={haproxyForm.name}
                        onChange={e => setHaproxyForm(prev => ({ ...prev, name: e.target.value }))}
                        className="input w-full"
                        required
                      />
                    </div>
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.rule_type')}</label>
                      <select
                        value={haproxyForm.rule_type}
                        onChange={e => setHaproxyForm(prev => ({ ...prev, rule_type: e.target.value as 'tcp' | 'https' }))}
                        className="input w-full"
                      >
                        <option value="tcp">TCP</option>
                        <option value="https">HTTPS</option>
                      </select>
                    </div>
                  </div>
                  
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.listen_port')}</label>
                      <input
                        type="number"
                        value={haproxyForm.listen_port}
                        onChange={e => setHaproxyForm(prev => ({ ...prev, listen_port: e.target.value }))}
                        className="input w-full"
                        min="1"
                        max="65535"
                        required
                      />
                    </div>
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.target_ip')}</label>
                      <input
                        type="text"
                        value={haproxyForm.target_ip}
                        onChange={e => setHaproxyForm(prev => ({ ...prev, target_ip: e.target.value }))}
                        className="input w-full"
                        placeholder="127.0.0.1"
                        required
                      />
                    </div>
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.target_port')}</label>
                      <input
                        type="number"
                        value={haproxyForm.target_port}
                        onChange={e => setHaproxyForm(prev => ({ ...prev, target_port: e.target.value }))}
                        className="input w-full"
                        min="1"
                        max="65535"
                        required
                      />
                    </div>
                  </div>
                  
                  {haproxyForm.rule_type === 'https' && (
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.cert_domain')}</label>
                      <input
                        type="text"
                        value={haproxyForm.cert_domain}
                        onChange={e => setHaproxyForm(prev => ({ ...prev, cert_domain: e.target.value }))}
                        className="input w-full"
                        placeholder="example.com"
                      />
                    </div>
                  )}
                  
                  <label className="flex items-center gap-2 cursor-pointer">
                    <Checkbox
                      checked={haproxyForm.target_ssl}
                      onChange={e => setHaproxyForm(prev => ({ ...prev, target_ssl: (e.target as HTMLInputElement).checked }))}
                    />
                    <span className="text-sm text-dark-300">{t('bulk_actions.target_ssl')}</span>
                  </label>
                  
                  <label className="flex items-center gap-2 cursor-pointer">
                    <Checkbox
                      checked={haproxyForm.send_proxy}
                      onChange={e => setHaproxyForm(prev => ({ ...prev, send_proxy: (e.target as HTMLInputElement).checked }))}
                    />
                    <span className="text-sm text-dark-300">{t('bulk_actions.send_proxy')}</span>
                  </label>
                </div>
              )}
              
              {activeType === 'haproxy' && activeMode === 'delete' && (
                <div className="space-y-4">
                  <div className="flex items-start gap-2 p-3 bg-dark-800/50 rounded-lg text-sm text-dark-400">
                    <AlertTriangle className="w-4 h-4 text-warning mt-0.5 shrink-0" />
                    {t('bulk_actions.delete_haproxy_hint')}
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.listen_port')}</label>
                      <input
                        type="number"
                        value={haproxyDeleteForm.listen_port}
                        onChange={e => setHaproxyDeleteForm(prev => ({ ...prev, listen_port: e.target.value }))}
                        className="input w-full"
                        min="1"
                        max="65535"
                        required
                      />
                    </div>
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.target_ip')}</label>
                      <input
                        type="text"
                        value={haproxyDeleteForm.target_ip}
                        onChange={e => setHaproxyDeleteForm(prev => ({ ...prev, target_ip: e.target.value }))}
                        className="input w-full"
                        placeholder="127.0.0.1"
                        required
                      />
                    </div>
                    <div>
                      <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.target_port')}</label>
                      <input
                        type="number"
                        value={haproxyDeleteForm.target_port}
                        onChange={e => setHaproxyDeleteForm(prev => ({ ...prev, target_port: e.target.value }))}
                        className="input w-full"
                        min="1"
                        max="65535"
                        required
                      />
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
                      className="input w-full max-w-xs"
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
                      className="input w-full max-w-xs"
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
                      className="input w-full max-w-xs"
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
                    <div>
                      <p className="text-dark-100 font-medium">{t('bulk_actions.terminal')}</p>
                      <p className="text-sm text-dark-400 mt-1">{t('bulk_actions.terminal_hint')}</p>
                    </div>
                  </div>
                  
                  <div>
                    <label className="text-sm text-dark-400 block mb-1.5">{t('bulk_actions.terminal_command')}</label>
                    <input
                      type="text"
                      value={terminalForm.command}
                      onChange={e => setTerminalForm(prev => ({ ...prev, command: e.target.value }))}
                      className="input w-full font-mono text-sm"
                      placeholder={t('bulk_actions.terminal_command_placeholder')}
                      required
                    />
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
              
              {/* HAProxy Config Form */}
              {activeType === 'haproxy_config' && (
                <div className="space-y-4">
                  <div className="flex items-start gap-3 p-4 bg-dark-800/50 rounded-xl">
                    <AlertTriangle className="w-5 h-5 mt-0.5 shrink-0 text-warning" />
                    <div>
                      <p className="text-dark-100 font-medium">{t('bulk_actions.haproxy_config')}</p>
                      <p className="text-sm text-dark-400 mt-1">{t('bulk_actions.haproxy_config_hint')}</p>
                    </div>
                  </div>
                  
                  <div className="flex items-center gap-3">
                    <motion.button
                      type="button"
                      onClick={handleApplyDefaultTemplate}
                      className="btn btn-secondary text-xs"
                      whileHover={{ scale: 1.02 }}
                      whileTap={{ scale: 0.98 }}
                    >
                      <Shuffle className="w-3.5 h-3.5" />
                      {t('bulk_actions.apply_default_template')}
                    </motion.button>
                    
                    <label className="flex items-center gap-2 cursor-pointer">
                      <Checkbox
                        checked={configReloadAfter}
                        onChange={e => setConfigReloadAfter((e.target as HTMLInputElement).checked)}
                      />
                      <span className="text-sm text-dark-300">{t('bulk_actions.haproxy_config_reload')}</span>
                    </label>
                  </div>
                  
                  <textarea
                    value={configContent}
                    onChange={e => setConfigContent(e.target.value)}
                    className="w-full h-[40vh] bg-dark-950 border border-dark-700 rounded-xl p-4 
                               font-mono text-sm text-dark-200 resize-none focus:outline-none 
                               focus:border-accent-500/50 focus:ring-1 focus:ring-accent-500/20
                               scrollbar-thin scrollbar-thumb-dark-700 scrollbar-track-transparent"
                    placeholder={t('bulk_actions.haproxy_config_placeholder')}
                    spellCheck={false}
                    required
                  />
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
                    activeType === 'terminal' || activeType === 'haproxy_config' || activeMode === 'create' || activeMode === 'start' 
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
                      {activeType === 'haproxy_config' && <Save className="w-4 h-4" />}
                      {activeType !== 'terminal' && activeType !== 'haproxy_config' && activeMode === 'create' && <Plus className="w-4 h-4" />}
                      {activeType !== 'terminal' && activeType !== 'haproxy_config' && activeMode === 'delete' && <Trash2 className="w-4 h-4" />}
                      {activeType !== 'terminal' && activeType !== 'haproxy_config' && activeMode === 'start' && <Play className="w-4 h-4" />}
                      {activeType !== 'terminal' && activeType !== 'haproxy_config' && activeMode === 'stop' && <Square className="w-4 h-4" />}
                      {t('bulk_actions.execute')}
                    </>
                  )}
                </motion.button>
              </div>
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
