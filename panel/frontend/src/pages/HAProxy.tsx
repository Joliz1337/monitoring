import { useState, useEffect, FormEvent, useMemo, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  ArrowLeft,
  Shield,
  Plus,
  Trash2,
  Edit2,
  RefreshCw,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Lock,
  Globe,
  Loader2,
  X,
  Activity,
  Zap,
  Play,
  Square,
  Upload,
  FileKey,
  Shuffle,
  Flame,
  ChevronDown,
  ChevronUp,
  Copy,
  FileText,
  Code,
  Save,
} from 'lucide-react'
import { proxyApi, HAProxyRule, HAProxyStatus, Certificate, FirewallRule } from '../api/client'
import { useServersStore } from '../stores/serversStore'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { useCachedData, createServerCacheKey } from '../hooks/useCachedData'
import CachedDataBanner from '../components/ui/CachedDataBanner'

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

export default function HAProxy() {
  const { uid, serverId } = useParams()
  const navigate = useNavigate()
  const { servers, fetchServers } = useServersStore()
  const { t } = useTranslation()
  
  const [status, setStatus] = useState<HAProxyStatus | null>(null)
  const [rules, setRules] = useState<HAProxyRule[]>([])
  const [certs, setCerts] = useState<string[]>([])
  const [certDetails, setCertDetails] = useState<Record<string, Certificate>>({})
  
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  
  // Cache for offline data
  interface HAProxyCacheData {
    status: HAProxyStatus | null
    rules: HAProxyRule[]
    certs: string[]
    certDetails: Record<string, Certificate>
    firewallRules: FirewallRule[]
    firewallActive: boolean
  }
  const cacheKey = serverId ? createServerCacheKey(serverId, 'haproxy') : ''
  const { isCached, cachedAt, saveToCache, loadFromCache, setIsCached, setCachedAt } = useCachedData<HAProxyCacheData>(cacheKey)
  
  const [showRuleForm, setShowRuleForm] = useState(false)
  const [editingRule, setEditingRule] = useState<HAProxyRule | null>(null)
  const [ruleForm, setRuleForm] = useState({
    name: '',
    rule_type: 'tcp' as 'tcp' | 'https',
    listen_port: '',
    target_ip: '',
    target_port: '',
    cert_domain: '',
    target_ssl: true,
    send_proxy: false,
  })
  const [formError, setFormError] = useState('')
  
  // Certificate form states
  const [showCertForm, setShowCertForm] = useState<'generate' | 'upload' | null>(null)
  const [certForm, setCertForm] = useState({
    domain: '',
    email: '',
    cert_content: '',
    key_content: '',
  })
  const [certFormError, setCertFormError] = useState('')
  const [certErrorLog, setCertErrorLog] = useState<string | null>(null)
  const [certErrorExpanded, setCertErrorExpanded] = useState(false)
  
  // Certificate renewal states
  const [renewLog, setRenewLog] = useState<{ domain: string; success: boolean; message: string; log?: string } | null>(null)
  const [renewLogExpanded, setRenewLogExpanded] = useState(false)
  
  // Certificate details expanded state
  const [expandedCert, setExpandedCert] = useState<string | null>(null)
  
  // Config editor states
  const [showConfigModal, setShowConfigModal] = useState(false)
  const [configContent, setConfigContent] = useState('')
  const [configPath, setConfigPath] = useState('')
  const [configLoading, setConfigLoading] = useState(false)
  const [configSaving, setConfigSaving] = useState(false)
  const [configError, setConfigError] = useState<string | null>(null)
  const [configSuccess, setConfigSuccess] = useState<string | null>(null)
  const [configModalMouseDownOnOverlay, setConfigModalMouseDownOnOverlay] = useState(false)
  
  // Firewall states
  const [firewallRules, setFirewallRules] = useState<FirewallRule[]>([])
  const [firewallActive, setFirewallActive] = useState(false)
  const [showFirewallForm, setShowFirewallForm] = useState(false)
  const [firewallForm, setFirewallForm] = useState({
    port: '',
    protocol: 'any' as 'tcp' | 'udp' | 'any',
    action: 'allow' as 'allow' | 'deny',
    from_ip: '',
    direction: 'in' as 'in' | 'out',
  })
  const [firewallFormError, setFirewallFormError] = useState('')
  const [firewallErrorLog, setFirewallErrorLog] = useState<string | null>(null)
  const [firewallErrorExpanded, setFirewallErrorExpanded] = useState(false)
  
  // Collapsed sections states
  const [collapsedSections, setCollapsedSections] = useState<{
    proxyRules: boolean
    sslCerts: boolean
    firewall: boolean
  }>({ proxyRules: false, sslCerts: false, firewall: false })
  
  const toggleSection = (section: 'proxyRules' | 'sslCerts' | 'firewall') => {
    setCollapsedSections(prev => ({ ...prev, [section]: !prev[section] }))
  }
  
  const server = servers.find(s => s.id === Number(serverId))
  
  // Memoized filtered firewall rules (IPv4 only)
  const filteredFirewallRules = useMemo(() => 
    firewallRules.filter(r => !r.ipv6),
    [firewallRules]
  )
  
  const applyCachedResponse = useCallback((data: { status?: HAProxyStatus; rules?: { rules: HAProxyRule[] }; certs?: { certificates: Certificate[] }; firewall?: { rules: FirewallRule[]; active: boolean } }) => {
    setStatus(data.status || null)
    setRules(data.rules?.rules || [])
    setCerts((data.certs?.certificates || []).map(c => c.domain))
    setFirewallRules(data.firewall?.rules || [])
    setFirewallActive(data.firewall?.active || false)

    const details: Record<string, Certificate> = {}
    for (const cert of data.certs?.certificates || []) {
      details[cert.domain] = cert
    }
    setCertDetails(details)
    setError(null)
    setIsCached(false)
    setCachedAt(null)

    saveToCache({
      status: data.status || null,
      rules: data.rules?.rules || [],
      certs: (data.certs?.certificates || []).map(c => c.domain),
      certDetails: details,
      firewallRules: data.firewall?.rules || [],
      firewallActive: data.firewall?.active || false,
    })
  }, [saveToCache, setIsCached, setCachedAt])

  // Initial data load — single request from panel DB cache
  const fetchData = useCallback(async () => {
    if (!serverId) return
    setIsLoading(true)
    
    try {
      const { data } = await proxyApi.getHAProxyCached(Number(serverId))
      applyCachedResponse(data)
    } catch {
      const cached = loadFromCache()
      if (cached) {
        setStatus(cached.status)
        setRules(cached.rules)
        setCerts(cached.certs)
        setCertDetails(cached.certDetails)
        setFirewallRules(cached.firewallRules)
        setFirewallActive(cached.firewallActive)
        setError(null)
      } else {
        setError(t('haproxy.failed_fetch'))
      }
    } finally {
      setIsLoading(false)
    }
  }, [serverId, applyCachedResponse, loadFromCache, t])

  // Auto-refresh — single request from panel DB cache (lightweight, no loader)
  const refreshFromCache = useCallback(async () => {
    if (!serverId) return
    try {
      const { data } = await proxyApi.getHAProxyCached(Number(serverId))
      applyCachedResponse(data)
    } catch {
      // Silent fail for background refresh
    }
  }, [serverId, applyCachedResponse])
  
  // Background refresh (no full-screen loader)
  const refreshData = useCallback(async () => {
    if (!serverId || isRefreshing) return
    setIsRefreshing(true)
    
    try {
      const [statusRes, rulesRes, certsRes, fwRes, allCertsRes] = await Promise.all([
        proxyApi.getHAProxyStatus(Number(serverId)),
        proxyApi.getHAProxyRules(Number(serverId)),
        proxyApi.getHAProxyCerts(Number(serverId)),
        proxyApi.getFirewallRules(Number(serverId)).catch(() => ({ data: { rules: [], active: false } })),
        proxyApi.getAllCerts(Number(serverId)).catch(() => ({ data: { certificates: [] } })),
      ])
      
      const statusData = statusRes.data
      const rulesData = rulesRes.data.rules || []
      const certsData = certsRes.data.certificates || []
      const fwRulesData = fwRes.data.rules || []
      const fwActiveData = fwRes.data.active || false
      
      const details: Record<string, Certificate> = {}
      for (const cert of allCertsRes.data.certificates || []) {
        details[cert.domain] = cert
      }
      
      setStatus(statusData)
      setRules(rulesData)
      setCerts(certsData)
      setFirewallRules(fwRulesData)
      setFirewallActive(fwActiveData)
      setCertDetails(details)
      setError(null)
      setIsCached(false)
      setCachedAt(null)
      
      // Save to cache
      saveToCache({
        status: statusData,
        rules: rulesData,
        certs: certsData,
        certDetails: details,
        firewallRules: fwRulesData,
        firewallActive: fwActiveData,
      })
    } catch {
      // On refresh error, try loading from cache if we don't have data yet
      if (!status && !rules.length) {
        const cached = loadFromCache()
        if (cached) {
          setStatus(cached.status)
          setRules(cached.rules)
          setCerts(cached.certs)
          setCertDetails(cached.certDetails)
          setFirewallRules(cached.firewallRules)
          setFirewallActive(cached.firewallActive)
        }
      }
    } finally {
      setIsRefreshing(false)
    }
  }, [serverId, isRefreshing, status, rules.length, saveToCache, loadFromCache, setIsCached, setCachedAt])
  
  // Quick status-only refresh
  const refreshStatus = async () => {
    if (!serverId) return
    try {
      const statusRes = await proxyApi.getHAProxyStatus(Number(serverId))
      setStatus(statusRes.data)
    } catch {
      // Silent fail
    }
  }
  
  useEffect(() => {
    fetchServers()
    fetchData()
  }, [serverId, fetchServers, fetchData])
  
  useAutoRefresh(refreshFromCache, { immediate: false })
  
  const handleReload = async () => {
    setActionLoading('reload')
    try {
      await proxyApi.reloadHAProxy(Number(serverId))
      await refreshData()
      toast.success(t('haproxy.haproxy_reloaded'))
    } catch {
      toast.error(t('haproxy.failed_save_rule'))
    }
    setActionLoading(null)
  }
  
  const handleDeleteRule = async (name: string) => {
    if (!confirm(t('haproxy.confirm_delete_rule', { name }))) return
    setActionLoading(`delete-${name}`)
    try {
      await proxyApi.deleteHAProxyRule(Number(serverId), name)
      await refreshData()
    } catch {}
    setActionLoading(null)
  }
  
  const handleEditRule = (rule: HAProxyRule) => {
    setEditingRule(rule)
    setRuleForm({
      name: rule.name,
      rule_type: rule.rule_type,
      listen_port: rule.listen_port.toString(),
      target_ip: rule.target_ip,
      target_port: rule.target_port.toString(),
      cert_domain: rule.cert_domain || '',
      target_ssl: rule.target_ssl || false,
      send_proxy: rule.send_proxy || false,
    })
    setShowRuleForm(true)
  }
  
  const handleSubmitRule = async (e: FormEvent) => {
    e.preventDefault()
    setFormError('')
    setActionLoading('submit-rule')
    
    const data = {
      name: ruleForm.name,
      rule_type: ruleForm.rule_type,
      listen_port: parseInt(ruleForm.listen_port),
      target_ip: ruleForm.target_ip,
      target_port: parseInt(ruleForm.target_port),
      cert_domain: ruleForm.cert_domain || undefined,
      target_ssl: ruleForm.target_ssl,
      send_proxy: ruleForm.send_proxy,
    }
    
    try {
      if (editingRule) {
        await proxyApi.updateHAProxyRule(Number(serverId), editingRule.name, data)
        toast.success(t('haproxy.rule_updated'))
      } else {
        await proxyApi.createHAProxyRule(Number(serverId), data)
        toast.success(t('haproxy.rule_created'))
      }
      await refreshData()
      setShowRuleForm(false)
      setEditingRule(null)
      setRuleForm({ name: '', rule_type: 'tcp', listen_port: '', target_ip: '', target_port: '', cert_domain: '', target_ssl: true, send_proxy: false })
    } catch (err: unknown) {
      toast.error(t('haproxy.failed_save_rule'))
      const error = err as { response?: { data?: { detail?: string | Array<{ msg?: string; loc?: string[] }> } } }
      const detail = error.response?.data?.detail
      // Handle FastAPI validation errors (422) which return detail as array
      if (Array.isArray(detail)) {
        const messages = detail.map(d => d.msg || JSON.stringify(d)).join('; ')
        setFormError(messages || t('haproxy.failed_save_rule'))
      } else {
        setFormError(detail || t('haproxy.failed_save_rule'))
      }
    }
    setActionLoading(null)
  }
  
  const handleRenewCerts = async () => {
    if (certs.length === 0) return
    
    setActionLoading('renew')
    setRenewLog(null)
    setRenewLogExpanded(false)
    
    const results: { domain: string; success: boolean; message: string }[] = []
    let fullLog = ''
    
    for (const domain of certs) {
      try {
        const res = await proxyApi.renewSingleCert(Number(serverId), domain)
        results.push({
          domain,
          success: res.data.success,
          message: res.data.message,
        })
        fullLog += `[${domain}] ${res.data.success ? '✓' : '✗'} ${res.data.message}\n`
        if (res.data.output_log) {
          fullLog += `${res.data.output_log}\n`
        }
        fullLog += '\n'
      } catch (err: unknown) {
        const error = err as { response?: { data?: { detail?: string | Array<{ msg?: string }>; message?: string } } }
        const detail = error.response?.data?.detail
        let errorMessage: string
        if (Array.isArray(detail)) {
          errorMessage = detail.map(d => d.msg || JSON.stringify(d)).join('; ') || t('haproxy.renewal_failed')
        } else {
          errorMessage = detail || error.response?.data?.message || t('haproxy.renewal_failed')
        }
        results.push({
          domain,
          success: false,
          message: errorMessage,
        })
        fullLog += `[${domain}] ✗ ${errorMessage}\n\n`
      }
    }
    
    const successCount = results.filter(r => r.success).length
    const allSuccess = successCount === results.length
    const renewedDomains = results.filter(r => r.success).map(r => r.domain)
    
    setRenewLog({
      domain: renewedDomains.length > 0 ? renewedDomains.join(', ') : t('haproxy.renew_all'),
      success: allSuccess,
      message: `${successCount}/${results.length} ${t('haproxy.certs_renewed')}`,
      log: fullLog.trim(),
    })
    setRenewLogExpanded(true)
    
    if (successCount > 0) {
      await refreshData()
    }
    
    setActionLoading(null)
  }
  
  const handleRenewSingleCert = async (domain: string) => {
    setActionLoading(`renew-${domain}`)
    setRenewLog(null)
    setRenewLogExpanded(false)
    
    try {
      const res = await proxyApi.renewSingleCert(Number(serverId), domain)
      setRenewLog({
        domain,
        success: res.data.success,
        message: res.data.message,
        log: res.data.output_log,
      })
      setRenewLogExpanded(true)
      if (res.data.success) {
        await refreshData()
      }
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string | Array<{ msg?: string }>; message?: string; output_log?: string } } }
      const detail = error.response?.data?.detail
      let errorMessage: string
      if (Array.isArray(detail)) {
        errorMessage = detail.map(d => d.msg || JSON.stringify(d)).join('; ') || t('haproxy.renewal_failed')
      } else {
        errorMessage = detail || error.response?.data?.message || t('haproxy.renewal_failed')
      }
      setRenewLog({
        domain,
        success: false,
        message: errorMessage,
        log: error.response?.data?.output_log,
      })
      setRenewLogExpanded(true)
    }
    setActionLoading(null)
  }
  
  const handleStartHAProxy = async () => {
    setActionLoading('start')
    try {
      await proxyApi.startHAProxy(Number(serverId))
      // Quick status check, then full refresh
      await refreshStatus()
      await refreshData()
      toast.success(t('haproxy.haproxy_started'))
    } catch {
      toast.error(t('haproxy.failed_save_rule'))
    }
    setActionLoading(null)
  }
  
  const handleStopHAProxy = async () => {
    if (!confirm(t('haproxy.confirm_stop'))) return
    setActionLoading('stop')
    try {
      await proxyApi.stopHAProxy(Number(serverId))
      // Quick status check, then full refresh
      await refreshStatus()
      await refreshData()
      toast.success(t('haproxy.haproxy_stopped'))
    } catch {
      toast.error(t('haproxy.failed_save_rule'))
    }
    setActionLoading(null)
  }
  
  const handleGenerateCert = async (e: FormEvent) => {
    e.preventDefault()
    setCertFormError('')
    setCertErrorLog(null)
    setCertErrorExpanded(false)
    setActionLoading('generate-cert')
    
    try {
      const res = await proxyApi.generateCert(Number(serverId), {
        domain: certForm.domain,
        email: certForm.email || undefined,
        method: 'standalone',
      })
      
      // Check if response indicates failure with error_log
      const data = res.data as { success?: boolean; message?: string; error_log?: string }
      if (data.success === false) {
        setCertFormError(data.message || t('haproxy.failed_generate_cert'))
        if (data.error_log) {
          setCertErrorLog(data.error_log)
        }
        toast.error(t('haproxy.failed_generate_cert'))
      } else {
        await refreshData()
        setShowCertForm(null)
        setCertForm({ domain: '', email: '', cert_content: '', key_content: '' })
        toast.success(t('haproxy.cert_generated'))
      }
    } catch (err: unknown) {
      toast.error(t('haproxy.failed_generate_cert'))
      const error = err as { response?: { data?: { detail?: string | Array<{ msg?: string }>; message?: string; error_log?: string } } }
      const detail = error.response?.data?.detail
      // Handle FastAPI validation errors (422) which return detail as array
      if (Array.isArray(detail)) {
        setCertFormError(detail.map(d => d.msg || JSON.stringify(d)).join('; ') || t('haproxy.failed_generate_cert'))
      } else {
        setCertFormError(detail || error.response?.data?.message || t('haproxy.failed_generate_cert'))
      }
      if (error.response?.data?.error_log) {
        setCertErrorLog(error.response.data.error_log)
      }
    }
    setActionLoading(null)
  }
  
  const handleUploadCert = async (e: FormEvent) => {
    e.preventDefault()
    setCertFormError('')
    setActionLoading('upload-cert')
    
    try {
      await proxyApi.uploadCert(Number(serverId), {
        domain: certForm.domain,
        cert_content: certForm.cert_content,
        key_content: certForm.key_content,
      })
      await refreshData()
      setShowCertForm(null)
      setCertForm({ domain: '', email: '', cert_content: '', key_content: '' })
      toast.success(t('haproxy.cert_uploaded'))
    } catch (err: unknown) {
      toast.error(t('haproxy.failed_upload_cert'))
      const error = err as { response?: { data?: { detail?: string | Array<{ msg?: string }> } } }
      const detail = error.response?.data?.detail
      if (Array.isArray(detail)) {
        setCertFormError(detail.map(d => d.msg || JSON.stringify(d)).join('; ') || t('haproxy.failed_upload_cert'))
      } else {
        setCertFormError(detail || t('haproxy.failed_upload_cert'))
      }
    }
    setActionLoading(null)
  }
  
  const handleDeleteCert = async (domain: string) => {
    if (!confirm(t('haproxy.confirm_delete_cert', { domain }))) return
    setActionLoading(`delete-cert-${domain}`)
    try {
      await proxyApi.deleteCert(Number(serverId), domain)
      await refreshData()
      toast.success(t('haproxy.cert_deleted'))
    } catch {
      toast.error(t('haproxy.failed_save_rule'))
    }
    setActionLoading(null)
  }
  
  // Firewall handlers
  const handleAddFirewallRule = async (e: FormEvent) => {
    e.preventDefault()
    setFirewallFormError('')
    setFirewallErrorLog(null)
    setFirewallErrorExpanded(false)
    setActionLoading('add-fw-rule')
    
    try {
      const res = await proxyApi.addFirewallRule(Number(serverId), {
        port: parseInt(firewallForm.port),
        protocol: firewallForm.protocol,
        action: firewallForm.action,
        from_ip: firewallForm.from_ip || null,
        direction: firewallForm.direction,
      })
      if (!res.data.success) {
        setFirewallFormError(res.data.message || t('firewall.failed_add_rule'))
        if (res.data.error_log) {
          setFirewallErrorLog(res.data.error_log)
        }
        toast.error(t('firewall.failed_add_rule'))
      } else {
        await refreshData()
        setShowFirewallForm(false)
        setFirewallForm({ port: '', protocol: 'any', action: 'allow', from_ip: '', direction: 'in' })
        toast.success(t('haproxy.firewall_rule_added'))
      }
    } catch (err: unknown) {
      toast.error(t('firewall.failed_add_rule'))
      const error = err as { response?: { data?: { detail?: string | Array<{ msg?: string }>; message?: string; error_log?: string } } }
      const detail = error.response?.data?.detail
      if (Array.isArray(detail)) {
        setFirewallFormError(detail.map(d => d.msg || JSON.stringify(d)).join('; ') || t('firewall.failed_add_rule'))
      } else {
        setFirewallFormError(detail || error.response?.data?.message || t('firewall.failed_add_rule'))
      }
      if (error.response?.data?.error_log) {
        setFirewallErrorLog(error.response.data.error_log)
      }
    }
    setActionLoading(null)
  }
  
  const handleDeleteFirewallRule = async (ruleNumber: number, port: number, protocol: string) => {
    if (!confirm(t('firewall.confirm_delete', { number: ruleNumber, port, protocol }))) return
    setActionLoading(`delete-fw-${ruleNumber}`)
    try {
      await proxyApi.deleteFirewallRuleByNumber(Number(serverId), ruleNumber)
      await refreshData()
      toast.success(t('haproxy.firewall_rule_deleted'))
    } catch {
      toast.error(t('firewall.failed_add_rule'))
    }
    setActionLoading(null)
  }
  
  const handleEnableFirewall = async () => {
    setActionLoading('enable-fw')
    setFirewallFormError('')
    setFirewallErrorLog(null)
    try {
      const res = await proxyApi.enableFirewall(Number(serverId))
      if (!res.data.success) {
        setFirewallFormError(res.data.message || t('firewall.failed_enable'))
        if (res.data.error_log) {
          setFirewallErrorLog(res.data.error_log)
        }
        toast.error(t('firewall.failed_enable'))
      } else {
        toast.success(t('haproxy.firewall_enabled'))
      }
      await refreshData()
    } catch (err: unknown) {
      toast.error(t('firewall.failed_enable'))
      const error = err as { response?: { data?: { detail?: string | Array<{ msg?: string }>; message?: string; error_log?: string } } }
      const detail = error.response?.data?.detail
      if (Array.isArray(detail)) {
        setFirewallFormError(detail.map(d => d.msg || JSON.stringify(d)).join('; ') || t('firewall.failed_enable'))
      } else {
        setFirewallFormError(detail || t('firewall.failed_enable'))
      }
      if (error.response?.data?.error_log) {
        setFirewallErrorLog(error.response.data.error_log)
      }
    }
    setActionLoading(null)
  }
  
  const handleDisableFirewall = async () => {
    if (!confirm(t('firewall.confirm_disable'))) return
    setActionLoading('disable-fw')
    setFirewallFormError('')
    setFirewallErrorLog(null)
    try {
      const res = await proxyApi.disableFirewall(Number(serverId))
      if (!res.data.success) {
        setFirewallFormError(res.data.message || t('firewall.failed_disable'))
        if (res.data.error_log) {
          setFirewallErrorLog(res.data.error_log)
        }
        toast.error(t('firewall.failed_disable'))
      } else {
        toast.success(t('haproxy.firewall_disabled'))
      }
      await refreshData()
    } catch (err: unknown) {
      toast.error(t('firewall.failed_disable'))
      const error = err as { response?: { data?: { detail?: string | Array<{ msg?: string }>; message?: string; error_log?: string } } }
      const detail = error.response?.data?.detail
      if (Array.isArray(detail)) {
        setFirewallFormError(detail.map(d => d.msg || JSON.stringify(d)).join('; ') || t('firewall.failed_disable'))
      } else {
        setFirewallFormError(detail || t('firewall.failed_disable'))
      }
      if (error.response?.data?.error_log) {
        setFirewallErrorLog(error.response.data.error_log)
      }
    }
    setActionLoading(null)
  }
  
  // Config editor handlers
  const handleOpenConfig = async () => {
    setShowConfigModal(true)
    setConfigLoading(true)
    setConfigError(null)
    setConfigSuccess(null)
    
    try {
      const res = await proxyApi.getHAProxyConfig(Number(serverId))
      setConfigContent(res.data.content)
      setConfigPath(res.data.path)
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      setConfigError(error.response?.data?.detail || 'Failed to load config')
    } finally {
      setConfigLoading(false)
    }
  }
  
  const handleSaveConfig = async () => {
    setConfigSaving(true)
    setConfigError(null)
    setConfigSuccess(null)
    
    try {
      const res = await proxyApi.applyHAProxyConfig(Number(serverId), configContent, true)
      if (res.data.success) {
        setConfigSuccess(t('haproxy.config_saved'))
        await refreshData()
        toast.success(t('haproxy.config_saved'))
      } else {
        setConfigError(res.data.message)
        toast.error(t('haproxy.config_save_error'))
      }
    } catch (err: unknown) {
      toast.error(t('haproxy.config_save_error'))
      const error = err as { response?: { data?: { detail?: string } } }
      setConfigError(error.response?.data?.detail || t('haproxy.config_save_error'))
    } finally {
      setConfigSaving(false)
    }
  }
  
  const handleApplyDefaultTemplate = () => {
    // Extract rules content between markers from current config
    const startIdx = configContent.indexOf(RULES_START_MARKER)
    const endIdx = configContent.indexOf(RULES_END_MARKER)
    
    let rulesContent = ''
    if (startIdx !== -1 && endIdx !== -1 && endIdx > startIdx) {
      rulesContent = configContent.slice(startIdx + RULES_START_MARKER.length, endIdx).trim()
    }
    
    // Generate new config with default template and preserved rules
    let newConfig = DEFAULT_HAPROXY_TEMPLATE
    if (rulesContent) {
      newConfig = newConfig.replace(
        RULES_END_MARKER,
        rulesContent + '\n' + RULES_END_MARKER
      )
    }
    
    setConfigContent(newConfig)
  }
  
  if (isLoading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
        <div className="flex items-center gap-4 mb-6">
          <div className="p-2.5"><ArrowLeft className="w-5 h-5 text-dark-600" /></div>
          <div className="flex-1 space-y-2">
            <div className="h-6 w-48 bg-dark-700/50 rounded-lg animate-pulse" />
            <div className="h-4 w-32 bg-dark-700/30 rounded-lg animate-pulse" />
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="card p-5 space-y-3">
              <div className="h-4 w-24 bg-dark-700/50 rounded animate-pulse" />
              <div className="h-8 w-32 bg-dark-700/30 rounded animate-pulse" />
            </div>
          ))}
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
      <motion.div 
        className="flex items-center gap-4 mb-6"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <motion.button
          onClick={() => navigate(`/${uid}/server/${serverId}`)}
          className="p-2.5 hover:bg-dark-800 rounded-xl text-dark-400 hover:text-dark-200 transition-all"
          whileHover={{ scale: 1.05, x: -2 }}
          whileTap={{ scale: 0.95 }}
        >
          <ArrowLeft className="w-5 h-5" />
        </motion.button>
        <div className="flex-1">
          <motion.h1 
            className="text-2xl font-bold text-dark-50 flex items-center gap-3"
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
          >
            <Shield className="w-7 h-7 text-accent-400" />
            {t('haproxy.title')}
            {isRefreshing && (
              <motion.div
                initial={{ opacity: 0, scale: 0.8 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0 }}
              >
                <Loader2 className="w-5 h-5 text-accent-400 animate-spin" />
              </motion.div>
            )}
          </motion.h1>
          <motion.p 
            className="text-dark-400 mt-1"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.1 }}
          >
            {server?.name}
          </motion.p>
        </div>
        <motion.button
          onClick={refreshData}
          disabled={isRefreshing}
          className="p-2.5 hover:bg-dark-800 rounded-xl text-dark-400 hover:text-dark-200 transition-all disabled:opacity-50"
          whileHover={{ scale: 1.05 }}
          whileTap={{ scale: 0.95 }}
          title={t('common.refresh_data')}
        >
          <RefreshCw className={`w-5 h-5 ${isRefreshing ? 'animate-spin' : ''}`} />
        </motion.button>
      </motion.div>
      
      <AnimatePresence mode="wait">
        {error ? (
          <motion.div 
            className="card text-center py-16"
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0 }}
            key="error"
          >
            <motion.div
              animate={{ y: [0, -5, 0] }}
              transition={{ duration: 2, repeat: Infinity }}
            >
              <AlertTriangle className="w-16 h-16 text-warning/70 mx-auto mb-4" />
            </motion.div>
            <p className="text-dark-400">{error}</p>
          </motion.div>
        ) : (
          <motion.div key="content">
            {/* Cached data indicator */}
            <AnimatePresence>
              {isCached && (
                <CachedDataBanner cachedAt={cachedAt} />
              )}
            </AnimatePresence>
            
            {/* Status Cards */}
            <motion.div 
              className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4 }}
            >
              <motion.div 
                className={`card group hover:border-dark-700 transition-all relative overflow-hidden ${
                  actionLoading === 'start' || actionLoading === 'stop' ? 'border-accent-500/30' : ''
                }`}
                whileHover={{ scale: 1.02 }}
              >
                {/* Loading overlay for status changes */}
                <AnimatePresence>
                  {(actionLoading === 'start' || actionLoading === 'stop') && (
                    <motion.div
                      className="absolute inset-0 bg-dark-900/50 backdrop-blur-sm flex items-center justify-center z-10"
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }}
                    >
                      <div className="flex items-center gap-2 text-accent-400">
                        <Loader2 className="w-5 h-5 animate-spin" />
                        <span className="text-sm">{actionLoading === 'start' ? t('haproxy.starting') : t('haproxy.stopping')}</span>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
                
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <motion.div 
                      className={`w-12 h-12 rounded-xl flex items-center justify-center ${
                        status?.running ? 'bg-success/10' : 'bg-danger/10'
                      }`}
                      animate={status?.running ? { scale: [1, 1.05, 1] } : {}}
                      transition={{ duration: 2, repeat: Infinity }}
                    >
                      {status?.running ? (
                        <CheckCircle2 className="w-6 h-6 text-success" />
                      ) : (
                        <XCircle className="w-6 h-6 text-danger" />
                      )}
                    </motion.div>
                    <div>
                      <p className="text-sm text-dark-400">{t('haproxy.service_status')}</p>
                      <p className={`font-semibold ${status?.running ? 'text-success' : 'text-danger'}`}>
                        {status?.running ? t('haproxy.running') : t('haproxy.stopped')}
                      </p>
                    </div>
                  </div>
                  {status?.running && (
                    <motion.div
                      className="w-2 h-2 rounded-full bg-success"
                      animate={{ opacity: [1, 0.5, 1] }}
                      transition={{ duration: 1.5, repeat: Infinity }}
                    />
                  )}
                </div>
              </motion.div>
              
              <motion.div 
                className="card group hover:border-dark-700 transition-all"
                whileHover={{ scale: 1.02 }}
              >
                <div className="flex items-center gap-3">
                  <motion.div 
                    className={`w-12 h-12 rounded-xl flex items-center justify-center ${
                      status?.config_valid ? 'bg-success/10' : 'bg-danger/10'
                    }`}
                  >
                    {status?.config_valid ? (
                      <Activity className="w-6 h-6 text-success" />
                    ) : (
                      <AlertTriangle className="w-6 h-6 text-danger" />
                    )}
                  </motion.div>
                  <div>
                    <p className="text-sm text-dark-400">{t('haproxy.config_status')}</p>
                    <p className={`font-semibold ${status?.config_valid ? 'text-success' : 'text-danger'}`}>
                      {status?.config_valid ? t('haproxy.valid') : t('haproxy.invalid')}
                    </p>
                  </div>
                </div>
              </motion.div>
              
              <motion.div 
                className="card relative overflow-hidden"
                whileHover={{ scale: 1.02 }}
              >
                {/* Loading overlay for reload */}
                <AnimatePresence>
                  {actionLoading === 'reload' && (
                    <motion.div
                      className="absolute inset-0 bg-dark-900/50 backdrop-blur-sm flex items-center justify-center z-10"
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }}
                    >
                      <div className="flex items-center gap-2 text-accent-400">
                        <Loader2 className="w-5 h-5 animate-spin" />
                        <span className="text-sm">{t('haproxy.reloading')}</span>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
                
                <div className="flex items-center gap-2 flex-wrap">
                  {status?.running ? (
                    <>
                      <motion.button
                        onClick={handleReload}
                        disabled={!!actionLoading}
                        className="btn btn-secondary flex-1"
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.98 }}
                      >
                        <RefreshCw className={`w-4 h-4 ${actionLoading === 'reload' ? 'animate-spin' : ''}`} />
                        {t('haproxy.reload')}
                      </motion.button>
                      <motion.button
                        onClick={handleStopHAProxy}
                        disabled={!!actionLoading}
                        className="btn btn-danger flex-1"
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.98 }}
                      >
                        {actionLoading === 'stop' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
                        {t('haproxy.stop')}
                      </motion.button>
                    </>
                  ) : (
                    <motion.button
                      onClick={handleStartHAProxy}
                      disabled={!!actionLoading}
                      className="btn btn-primary flex-1"
                      whileHover={{ scale: 1.02 }}
                      whileTap={{ scale: 0.98 }}
                    >
                      {actionLoading === 'start' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                      {t('haproxy.start_haproxy')}
                    </motion.button>
                  )}
                </div>
              </motion.div>
            </motion.div>
            
            {/* Proxy Rules */}
            <motion.div 
              className="card mb-6"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4 }}
            >
              <div className="flex items-center justify-between mb-5">
                <button 
                  onClick={() => toggleSection('proxyRules')}
                  className="flex items-center gap-2 hover:opacity-80 transition-opacity"
                >
                  <motion.div
                    animate={{ rotate: collapsedSections.proxyRules ? -90 : 0 }}
                    transition={{ duration: 0.2 }}
                  >
                    <ChevronDown className="w-5 h-5 text-dark-400" />
                  </motion.div>
                  <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
                    <Zap className="w-5 h-5 text-accent-500" />
                    {t('haproxy.proxy_rules')}
                  </h2>
                  <span className="text-sm text-dark-500">({rules.length})</span>
                </button>
                <div className="flex items-center gap-2">
                  <motion.button
                    onClick={handleOpenConfig}
                    className="btn btn-secondary text-sm"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    <Code className="w-4 h-4" />
                    {t('haproxy.view_config')}
                  </motion.button>
                  <motion.button
                    onClick={() => {
                      setEditingRule(null)
                      setRuleForm({ name: '', rule_type: 'tcp', listen_port: '', target_ip: '', target_port: '', cert_domain: '', target_ssl: true, send_proxy: false })
                      setShowRuleForm(true)
                    }}
                    className="btn btn-primary text-sm"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    <Plus className="w-4 h-4" />
                    {t('haproxy.add_rule')}
                  </motion.button>
                </div>
              </div>
              
              <div className={`collapse-grid ${!collapsedSections.proxyRules ? 'open' : ''}`}>
              <div className="collapse-content">
              <AnimatePresence>
                {showRuleForm && (
                  <motion.div 
                    className="mb-6 p-5 bg-dark-800/50 rounded-xl border border-dark-700/50"
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.3 }}
                  >
                    <div className="flex items-center justify-between mb-4">
                      <h3 className="font-medium text-dark-200 flex items-center gap-2">
                        {editingRule ? (
                          <>
                            <Edit2 className="w-4 h-4 text-accent-500" />
                            {t('haproxy.edit_rule')}
                          </>
                        ) : (
                          <>
                            <Plus className="w-4 h-4 text-accent-500" />
                            {t('haproxy.new_rule')}
                          </>
                        )}
                      </h3>
                      <motion.button
                        onClick={() => {
                          setShowRuleForm(false)
                          setEditingRule(null)
                          setFormError('')
                        }}
                        className="p-2 hover:bg-dark-700 rounded-lg text-dark-400 transition-colors"
                        whileHover={{ scale: 1.1, rotate: 90 }}
                        whileTap={{ scale: 0.9 }}
                      >
                        <X className="w-5 h-5" />
                      </motion.button>
                    </div>
                    
                    <AnimatePresence>
                      {formError && (
                        <motion.div 
                          className="flex items-center gap-3 p-4 mb-4 bg-danger/10 border border-danger/20 rounded-xl text-danger text-sm"
                          initial={{ opacity: 0, y: -10 }}
                          animate={{ opacity: 1, y: 0 }}
                          exit={{ opacity: 0, y: -10 }}
                        >
                          <AlertTriangle className="w-4 h-4" />
                          {formError}
                        </motion.div>
                      )}
                    </AnimatePresence>
                    
                    <form onSubmit={handleSubmitRule} className="space-y-4">
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('common.name')}</label>
                          <input
                            type="text"
                            value={ruleForm.name}
                            onChange={(e) => setRuleForm(f => ({ ...f, name: e.target.value }))}
                            placeholder="my-proxy"
                            className="input"
                            required
                            disabled={!!editingRule}
                          />
                        </div>
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('common.type')}</label>
                          <select
                            value={ruleForm.rule_type}
                            onChange={(e) => {
                              const newType = e.target.value as 'tcp' | 'https'
                              setRuleForm(f => ({ 
                                ...f, 
                                rule_type: newType,
                                target_ssl: newType === 'https' ? true : false
                              }))
                            }}
                            className="input"
                          >
                            <option value="tcp">TCP</option>
                            <option value="https">Web</option>
                          </select>
                        </div>
                      </div>
                      
                      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('haproxy.listen_port')}</label>
                          <input
                            type="number"
                            value={ruleForm.listen_port}
                            onChange={(e) => setRuleForm(f => ({ ...f, listen_port: e.target.value }))}
                            placeholder="443"
                            className="input"
                            required
                          />
                        </div>
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('haproxy.target_ip')}</label>
                          <input
                            type="text"
                            value={ruleForm.target_ip}
                            onChange={(e) => setRuleForm(f => ({ ...f, target_ip: e.target.value }))}
                            placeholder="192.168.1.10"
                            className="input"
                            required
                          />
                        </div>
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('haproxy.target_port')}</label>
                          <input
                            type="number"
                            value={ruleForm.target_port}
                            onChange={(e) => setRuleForm(f => ({ ...f, target_port: e.target.value }))}
                            placeholder="8080"
                            className="input"
                            required
                          />
                        </div>
                      </div>
                      
                      <div className="flex items-center justify-between">
                        <span className="text-sm text-dark-300">{t('haproxy.send_proxy')}</span>
                        <button
                          type="button"
                          onClick={() => setRuleForm(f => ({ ...f, send_proxy: !f.send_proxy }))}
                          className={`relative w-11 h-6 rounded-full transition-colors duration-200 ${
                            ruleForm.send_proxy 
                              ? 'bg-success' 
                              : 'bg-dark-600'
                          }`}
                        >
                          <span 
                            className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform duration-200 ${
                              ruleForm.send_proxy ? 'translate-x-5' : 'translate-x-0'
                            }`}
                          />
                        </button>
                      </div>
                      
                      <AnimatePresence>
                        {ruleForm.rule_type === 'https' && (
                          <motion.div
                            initial={{ opacity: 0, height: 0 }}
                            animate={{ opacity: 1, height: 'auto' }}
                            exit={{ opacity: 0, height: 0 }}
                            className="space-y-4"
                          >
                            <div>
                              <label className="block text-sm text-dark-400 mb-2">{t('haproxy.cert_domain')}</label>
                              <input
                                type="text"
                                value={ruleForm.cert_domain}
                                onChange={(e) => setRuleForm(f => ({ ...f, cert_domain: e.target.value }))}
                                placeholder="example.com"
                                className="input"
                              />
                            </div>
                            <div className="flex items-center justify-between">
                              <span className="text-sm text-dark-300">Backend HTTPS</span>
                              <button
                                type="button"
                                onClick={() => setRuleForm(f => ({ ...f, target_ssl: !f.target_ssl }))}
                                className={`relative w-11 h-6 rounded-full transition-colors duration-200 ${
                                  ruleForm.target_ssl 
                                    ? 'bg-success' 
                                    : 'bg-dark-600'
                                }`}
                              >
                                <span 
                                  className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform duration-200 ${
                                    ruleForm.target_ssl ? 'translate-x-5' : 'translate-x-0'
                                  }`}
                                />
                              </button>
                            </div>
                          </motion.div>
                        )}
                      </AnimatePresence>
                      
                      <div className="flex gap-3 pt-2">
                        <motion.button
                          type="submit"
                          disabled={actionLoading === 'submit-rule'}
                          className="btn btn-primary"
                          whileHover={{ scale: 1.02 }}
                          whileTap={{ scale: 0.98 }}
                        >
                          {actionLoading === 'submit-rule' ? (
                            <motion.div
                              animate={{ rotate: 360 }}
                              transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                            >
                              <Loader2 className="w-4 h-4" />
                            </motion.div>
                          ) : editingRule ? (
                            t('haproxy.update_rule')
                          ) : (
                            t('haproxy.create_rule')
                          )}
                        </motion.button>
                      </div>
                    </form>
                  </motion.div>
                )}
              </AnimatePresence>
              
              {rules.length === 0 ? (
                <motion.div 
                  className="text-center py-12 text-dark-500"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                >
                  <motion.div
                    animate={{ y: [0, -5, 0] }}
                    transition={{ duration: 3, repeat: Infinity }}
                  >
                    <Shield className="w-12 h-12 mx-auto mb-3 opacity-50" />
                  </motion.div>
                  <p>{t('haproxy.no_rules')}</p>
                </motion.div>
              ) : (
                <motion.div className="space-y-3" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                  <AnimatePresence mode="popLayout">
                    {rules.map((rule, index) => (
                      <motion.div
                        key={rule.name}
                        className="flex items-center justify-between p-4 bg-dark-800/50 rounded-xl border border-dark-700/30 group hover:border-dark-600 transition-all"
                        initial={{ opacity: 0, y: 20 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, x: -100 }}
                        transition={{ delay: index * 0.05 }}
                        whileHover={{ scale: 1.01 }}
                        layout
                      >
                        <div className="flex items-center gap-4">
                          <motion.div 
                            className={`w-12 h-12 rounded-xl flex items-center justify-center ${
                              rule.rule_type === 'https' ? 'bg-success/10' : 'bg-accent-500/10'
                            }`}
                            whileHover={{ rotate: 5, scale: 1.05 }}
                          >
                            {rule.rule_type === 'https' ? (
                              <Lock className="w-5 h-5 text-success" />
                            ) : (
                              <Globe className="w-5 h-5 text-accent-500" />
                            )}
                          </motion.div>
                          <div>
                            <p className="font-medium text-dark-100 group-hover:text-white transition-colors">
                              {rule.name}
                            </p>
                            <p className="text-sm text-dark-500 font-mono">
                              :{rule.listen_port} → {rule.target_ip}:{rule.target_port}
                              {rule.cert_domain && (
                                <span className="text-dark-600"> ({rule.cert_domain})</span>
                              )}
                            </p>
                          </div>
                        </div>
                        
                        <div className="flex items-center gap-2">
                          <span className={`px-2.5 py-1 rounded-lg text-xs font-medium ${
                            rule.rule_type === 'https' 
                              ? 'bg-success/10 text-success border border-success/20' 
                              : 'bg-accent-500/10 text-accent-400 border border-accent-500/20'
                          }`}>
                            {rule.rule_type === 'https' ? 'WEB' : 'TCP'}
                          </span>
                          {rule.send_proxy && (
                            <span className="px-2.5 py-1 rounded-lg text-xs font-medium bg-warning/10 text-warning border border-warning/20">
                              PP
                            </span>
                          )}
                          <motion.button
                            onClick={() => handleEditRule(rule)}
                            className="btn btn-ghost p-2.5"
                            whileHover={{ scale: 1.1 }}
                            whileTap={{ scale: 0.9 }}
                          >
                            <Edit2 className="w-4 h-4" />
                          </motion.button>
                          <motion.button
                            onClick={() => handleDeleteRule(rule.name)}
                            disabled={actionLoading === `delete-${rule.name}`}
                            className="btn btn-ghost p-2.5 text-danger hover:bg-danger/10"
                            whileHover={{ scale: 1.1 }}
                            whileTap={{ scale: 0.9 }}
                          >
                            {actionLoading === `delete-${rule.name}` ? (
                              <motion.div
                                animate={{ rotate: 360 }}
                                transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                              >
                                <Loader2 className="w-4 h-4" />
                              </motion.div>
                            ) : (
                              <Trash2 className="w-4 h-4" />
                            )}
                          </motion.button>
                        </div>
                      </motion.div>
                    ))}
                  </AnimatePresence>
                </motion.div>
              )}
              </div>
              </div>
            </motion.div>
            
            {/* SSL Certificates */}
            <motion.div 
              className="card"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4 }}
            >
              <div className="flex items-center justify-between mb-5">
                <button 
                  onClick={() => toggleSection('sslCerts')}
                  className="flex items-center gap-2 hover:opacity-80 transition-opacity"
                >
                  <motion.div
                    animate={{ rotate: collapsedSections.sslCerts ? -90 : 0 }}
                    transition={{ duration: 0.2 }}
                  >
                    <ChevronDown className="w-5 h-5 text-dark-400" />
                  </motion.div>
                  <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
                    <Lock className="w-5 h-5 text-accent-500" />
                    {t('haproxy.ssl_certificates')}
                  </h2>
                  <span className="text-sm text-dark-500">({certs.length})</span>
                </button>
                <div className="flex items-center gap-2">
                  <motion.button
                    onClick={() => {
                      setShowCertForm('generate')
                      setCertForm({ domain: '', email: '', cert_content: '', key_content: '' })
                      setCertFormError('')
                    }}
                    className="btn btn-primary text-sm"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    <Plus className="w-4 h-4" />
                    {t('haproxy.create_cert')}
                  </motion.button>
                  <motion.button
                    onClick={() => {
                      setShowCertForm('upload')
                      setCertForm({ domain: '', email: '', cert_content: '', key_content: '' })
                      setCertFormError('')
                    }}
                    className="btn btn-secondary text-sm"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    <Upload className="w-4 h-4" />
                    {t('haproxy.upload_cert')}
                  </motion.button>
                  <motion.button
                    onClick={handleRenewCerts}
                    disabled={actionLoading === 'renew'}
                    className="btn btn-ghost text-sm"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    <motion.div
                      animate={actionLoading === 'renew' ? { rotate: 360 } : {}}
                      transition={{ duration: 1, repeat: actionLoading === 'renew' ? Infinity : 0, ease: 'linear' }}
                    >
                      <RefreshCw className="w-4 h-4" />
                    </motion.div>
                    {t('haproxy.renew_all')}
                  </motion.button>
                </div>
              </div>
              
              <div className={`collapse-grid ${!collapsedSections.sslCerts ? 'open' : ''}`}>
              <div className="collapse-content">
              {/* Certificate Forms */}
              <AnimatePresence>
                {showCertForm && (
                  <motion.div 
                    className="mb-6 p-5 bg-dark-800/50 rounded-xl border border-dark-700/50"
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.3 }}
                  >
                    <div className="flex items-center justify-between mb-4">
                      <h3 className="font-medium text-dark-200 flex items-center gap-2">
                        {showCertForm === 'generate' ? (
                          <>
                            <FileKey className="w-4 h-4 text-accent-500" />
                            {t('haproxy.create_ssl')}
                          </>
                        ) : (
                          <>
                            <Upload className="w-4 h-4 text-accent-500" />
                            {t('haproxy.upload_ssl')}
                          </>
                        )}
                      </h3>
                      <motion.button
                        onClick={() => {
                          setShowCertForm(null)
                          setCertFormError('')
                        }}
                        className="p-2 hover:bg-dark-700 rounded-lg text-dark-400 transition-colors"
                        whileHover={{ scale: 1.1, rotate: 90 }}
                        whileTap={{ scale: 0.9 }}
                      >
                        <X className="w-5 h-5" />
                      </motion.button>
                    </div>
                    
                    <AnimatePresence>
                      {certFormError && (
                        <motion.div 
                          className="mb-4 bg-danger/10 border border-danger/20 rounded-xl text-danger text-sm overflow-hidden"
                          initial={{ opacity: 0, y: -10 }}
                          animate={{ opacity: 1, y: 0 }}
                          exit={{ opacity: 0, y: -10 }}
                        >
                          <div className="flex items-center justify-between p-4">
                            <div className="flex items-center gap-3">
                              <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                              <span>{certFormError}</span>
                            </div>
                            {certErrorLog && (
                              <motion.button
                                onClick={() => setCertErrorExpanded(!certErrorExpanded)}
                                className="flex items-center gap-1 text-xs px-2 py-1 hover:bg-danger/20 rounded-lg transition-colors"
                                whileHover={{ scale: 1.05 }}
                                whileTap={{ scale: 0.95 }}
                              >
                                {certErrorExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                                {certErrorExpanded ? t('common.hide') : t('common.details')}
                              </motion.button>
                            )}
                          </div>
                          <AnimatePresence>
                            {certErrorExpanded && certErrorLog && (
                              <motion.div
                                initial={{ height: 0, opacity: 0 }}
                                animate={{ height: 'auto', opacity: 1 }}
                                exit={{ height: 0, opacity: 0 }}
                                className="border-t border-danger/20"
                              >
                                <pre className="p-4 text-xs overflow-auto max-h-64 whitespace-pre-wrap text-danger/80 bg-danger/5">
                                  {certErrorLog}
                                </pre>
                              </motion.div>
                            )}
                          </AnimatePresence>
                        </motion.div>
                      )}
                    </AnimatePresence>
                    
                    {showCertForm === 'generate' ? (
                      <form onSubmit={handleGenerateCert} className="space-y-4">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                          <div>
                            <label className="block text-sm text-dark-400 mb-2">{t('haproxy.domain')}</label>
                            <input
                              type="text"
                              value={certForm.domain}
                              onChange={(e) => setCertForm(f => ({ ...f, domain: e.target.value }))}
                              placeholder="example.com"
                              className="input"
                              required
                            />
                          </div>
                          <div>
                            <label className="block text-sm text-dark-400 mb-2">{t('haproxy.email')}</label>
                            <div className="flex gap-2">
                              <input
                                type="email"
                                value={certForm.email}
                                onChange={(e) => setCertForm(f => ({ ...f, email: e.target.value }))}
                                placeholder="admin@example.com"
                                className="input flex-1"
                              />
                              <motion.button
                                type="button"
                                onClick={() => {
                                  const randomStr = Math.random().toString(36).substring(2, 10)
                                  setCertForm(f => ({ ...f, email: `${randomStr}@gmail.com` }))
                                }}
                                className="btn btn-ghost px-3"
                                whileHover={{ scale: 1.05 }}
                                whileTap={{ scale: 0.95 }}
                                title={t('haproxy.generate_random_email')}
                              >
                                <Shuffle className="w-4 h-4" />
                              </motion.button>
                            </div>
                          </div>
                        </div>
                        <p className="text-xs text-dark-500">
                          {t('haproxy.ssl_note')}
                        </p>
                        <div className="flex gap-3 pt-2">
                          <motion.button
                            type="submit"
                            disabled={actionLoading === 'generate-cert'}
                            className="btn btn-primary"
                            whileHover={{ scale: 1.02 }}
                            whileTap={{ scale: 0.98 }}
                          >
                            {actionLoading === 'generate-cert' ? (
                              <motion.div
                                animate={{ rotate: 360 }}
                                transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                              >
                                <Loader2 className="w-4 h-4" />
                              </motion.div>
                            ) : (
                              t('haproxy.create_certificate')
                            )}
                          </motion.button>
                        </div>
                      </form>
                    ) : (
                      <form onSubmit={handleUploadCert} className="space-y-4">
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('haproxy.domain')}</label>
                          <input
                            type="text"
                            value={certForm.domain}
                            onChange={(e) => setCertForm(f => ({ ...f, domain: e.target.value }))}
                            placeholder="example.com"
                            className="input"
                            required
                          />
                        </div>
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('haproxy.cert_pem')}</label>
                          <textarea
                            value={certForm.cert_content}
                            onChange={(e) => setCertForm(f => ({ ...f, cert_content: e.target.value }))}
                            placeholder="-----BEGIN CERTIFICATE-----&#10;...&#10;-----END CERTIFICATE-----"
                            className="input font-mono text-xs h-32"
                            required
                          />
                        </div>
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('haproxy.private_key')}</label>
                          <textarea
                            value={certForm.key_content}
                            onChange={(e) => setCertForm(f => ({ ...f, key_content: e.target.value }))}
                            placeholder="-----BEGIN PRIVATE KEY-----&#10;...&#10;-----END PRIVATE KEY-----"
                            className="input font-mono text-xs h-32"
                            required
                          />
                        </div>
                        <div className="flex gap-3 pt-2">
                          <motion.button
                            type="submit"
                            disabled={actionLoading === 'upload-cert'}
                            className="btn btn-primary"
                            whileHover={{ scale: 1.02 }}
                            whileTap={{ scale: 0.98 }}
                          >
                            {actionLoading === 'upload-cert' ? (
                              <motion.div
                                animate={{ rotate: 360 }}
                                transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                              >
                                <Loader2 className="w-4 h-4" />
                              </motion.div>
                            ) : (
                              t('haproxy.upload_certificate')
                            )}
                          </motion.button>
                        </div>
                      </form>
                    )}
                  </motion.div>
                )}
              </AnimatePresence>
              
              {certs.length === 0 ? (
                <motion.div 
                  className="text-center py-12 text-dark-500"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                >
                  <motion.div
                    animate={{ y: [0, -5, 0] }}
                    transition={{ duration: 3, repeat: Infinity }}
                  >
                    <Lock className="w-12 h-12 mx-auto mb-3 opacity-50" />
                  </motion.div>
                  <p>{t('haproxy.no_certs')}</p>
                </motion.div>
              ) : (
                <motion.div className="space-y-3" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                  {certs.map((domain, index) => {
                    const cert = certDetails[domain]
                    const isExpiringSoon = cert && cert.days_left < 30
                    const isExpired = cert && cert.expired
                    const isExpanded = expandedCert === domain
                    
                    const copyToClipboard = (text: string) => {
                      navigator.clipboard.writeText(text)
                    }
                    
                    return (
                      <motion.div
                        key={domain}
                        className="bg-dark-800/50 rounded-xl border border-dark-700/30 group hover:border-dark-600 transition-all overflow-hidden"
                        initial={{ opacity: 0, y: 20 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ delay: index * 0.05 }}
                        layout
                      >
                        <div 
                          className="flex items-center justify-between p-4 cursor-pointer"
                          onClick={() => setExpandedCert(isExpanded ? null : domain)}
                        >
                          <div className="flex items-center gap-4">
                            <motion.div 
                              className={`w-12 h-12 rounded-xl flex items-center justify-center ${
                                isExpired ? 'bg-danger/10' : isExpiringSoon ? 'bg-warning/10' : 'bg-success/10'
                              }`}
                              whileHover={{ rotate: 5, scale: 1.05 }}
                            >
                              <Lock className={`w-5 h-5 ${
                                isExpired ? 'text-danger' : isExpiringSoon ? 'text-warning' : 'text-success'
                              }`} />
                            </motion.div>
                            <div>
                              <p className="font-medium text-dark-100 group-hover:text-white transition-colors flex items-center gap-2">
                                {domain}
                                <motion.span
                                  animate={{ rotate: isExpanded ? 180 : 0 }}
                                  transition={{ duration: 0.2 }}
                                >
                                  <ChevronDown className="w-4 h-4 text-dark-500" />
                                </motion.span>
                              </p>
                              {cert && (
                                <p className="text-sm text-dark-500">
                                  {t('haproxy.expires')}: {new Date(cert.expiry_date).toLocaleDateString()}
                                  {cert.source && (
                                    <span className="ml-2 text-dark-600">
                                      ({cert.source === 'letsencrypt' ? t('haproxy.letsencrypt') : t('haproxy.custom')})
                                    </span>
                                  )}
                                </p>
                              )}
                            </div>
                          </div>
                          
                          <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                            {cert && (
                              <motion.span 
                                className={`px-3 py-1.5 rounded-lg text-xs font-medium ${
                                  isExpired 
                                    ? 'bg-danger/10 text-danger border border-danger/20' 
                                    : isExpiringSoon 
                                      ? 'bg-warning/10 text-warning border border-warning/20' 
                                      : 'bg-success/10 text-success border border-success/20'
                                }`}
                                initial={{ scale: 0.8 }}
                                animate={{ scale: 1 }}
                              >
                                {isExpired ? t('haproxy.expired') : t('haproxy.days_left', { days: cert.days_left })}
                              </motion.span>
                            )}
                            <motion.button
                              onClick={() => handleRenewSingleCert(domain)}
                              disabled={!!actionLoading}
                              className="btn btn-ghost p-2.5 text-accent-400 hover:bg-accent-500/10"
                              whileHover={{ scale: 1.1 }}
                              whileTap={{ scale: 0.9 }}
                              title={t('haproxy.renew')}
                            >
                              {actionLoading === `renew-${domain}` ? (
                                <motion.div
                                  animate={{ rotate: 360 }}
                                  transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                                >
                                  <Loader2 className="w-4 h-4" />
                                </motion.div>
                              ) : (
                                <RefreshCw className="w-4 h-4" />
                              )}
                            </motion.button>
                            <motion.button
                              onClick={() => handleDeleteCert(domain)}
                              disabled={actionLoading === `delete-cert-${domain}`}
                              className="btn btn-ghost p-2.5 text-danger hover:bg-danger/10"
                              whileHover={{ scale: 1.1 }}
                              whileTap={{ scale: 0.9 }}
                            >
                              {actionLoading === `delete-cert-${domain}` ? (
                                <motion.div
                                  animate={{ rotate: 360 }}
                                  transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                                >
                                  <Loader2 className="w-4 h-4" />
                                </motion.div>
                              ) : (
                                <Trash2 className="w-4 h-4" />
                              )}
                            </motion.button>
                          </div>
                        </div>
                        
                        {/* Certificate Files Section */}
                        <AnimatePresence>
                          {isExpanded && cert?.files && (
                            <motion.div
                              initial={{ height: 0, opacity: 0 }}
                              animate={{ height: 'auto', opacity: 1 }}
                              exit={{ height: 0, opacity: 0 }}
                              transition={{ duration: 0.2 }}
                              className="border-t border-dark-700/30"
                            >
                              <div className="p-4 space-y-2">
                                <p className="text-xs text-dark-500 uppercase tracking-wider mb-3 flex items-center gap-2">
                                  <FileText className="w-3.5 h-3.5" />
                                  {t('haproxy.certificate_files')}
                                </p>
                                
                                {cert.files.pem && (
                                  <div className="flex items-center justify-between p-2.5 bg-dark-900/50 rounded-lg group/file">
                                    <div className="flex items-center gap-2 min-w-0">
                                      <span className="text-xs font-medium text-accent-400 w-20 flex-shrink-0">.pem</span>
                                      <span className="text-xs text-dark-400 font-mono truncate">{cert.files.pem}</span>
                                    </div>
                                    <motion.button
                                      onClick={() => copyToClipboard(cert.files!.pem!)}
                                      className="p-1.5 hover:bg-dark-700 rounded text-dark-500 hover:text-dark-300 opacity-0 group-hover/file:opacity-100 transition-opacity"
                                      whileHover={{ scale: 1.1 }}
                                      whileTap={{ scale: 0.9 }}
                                      title={t('common.copy')}
                                    >
                                      <Copy className="w-3.5 h-3.5" />
                                    </motion.button>
                                  </div>
                                )}
                                
                                {cert.files.key && (
                                  <div className="flex items-center justify-between p-2.5 bg-dark-900/50 rounded-lg group/file">
                                    <div className="flex items-center gap-2 min-w-0">
                                      <span className="text-xs font-medium text-warning w-20 flex-shrink-0">.key</span>
                                      <span className="text-xs text-dark-400 font-mono truncate">{cert.files.key}</span>
                                    </div>
                                    <motion.button
                                      onClick={() => copyToClipboard(cert.files!.key!)}
                                      className="p-1.5 hover:bg-dark-700 rounded text-dark-500 hover:text-dark-300 opacity-0 group-hover/file:opacity-100 transition-opacity"
                                      whileHover={{ scale: 1.1 }}
                                      whileTap={{ scale: 0.9 }}
                                      title={t('common.copy')}
                                    >
                                      <Copy className="w-3.5 h-3.5" />
                                    </motion.button>
                                  </div>
                                )}
                                
                                {cert.files.fullchain && (
                                  <div className="flex items-center justify-between p-2.5 bg-dark-900/50 rounded-lg group/file">
                                    <div className="flex items-center gap-2 min-w-0">
                                      <span className="text-xs font-medium text-success w-20 flex-shrink-0">fullchain</span>
                                      <span className="text-xs text-dark-400 font-mono truncate">{cert.files.fullchain}</span>
                                    </div>
                                    <motion.button
                                      onClick={() => copyToClipboard(cert.files!.fullchain!)}
                                      className="p-1.5 hover:bg-dark-700 rounded text-dark-500 hover:text-dark-300 opacity-0 group-hover/file:opacity-100 transition-opacity"
                                      whileHover={{ scale: 1.1 }}
                                      whileTap={{ scale: 0.9 }}
                                      title={t('common.copy')}
                                    >
                                      <Copy className="w-3.5 h-3.5" />
                                    </motion.button>
                                  </div>
                                )}
                                
                                {cert.files.cert && (
                                  <div className="flex items-center justify-between p-2.5 bg-dark-900/50 rounded-lg group/file">
                                    <div className="flex items-center gap-2 min-w-0">
                                      <span className="text-xs font-medium text-blue-400 w-20 flex-shrink-0">.crt</span>
                                      <span className="text-xs text-dark-400 font-mono truncate">{cert.files.cert}</span>
                                    </div>
                                    <motion.button
                                      onClick={() => copyToClipboard(cert.files!.cert!)}
                                      className="p-1.5 hover:bg-dark-700 rounded text-dark-500 hover:text-dark-300 opacity-0 group-hover/file:opacity-100 transition-opacity"
                                      whileHover={{ scale: 1.1 }}
                                      whileTap={{ scale: 0.9 }}
                                      title={t('common.copy')}
                                    >
                                      <Copy className="w-3.5 h-3.5" />
                                    </motion.button>
                                  </div>
                                )}
                                
                                {cert.files.chain && (
                                  <div className="flex items-center justify-between p-2.5 bg-dark-900/50 rounded-lg group/file">
                                    <div className="flex items-center gap-2 min-w-0">
                                      <span className="text-xs font-medium text-purple-400 w-20 flex-shrink-0">chain</span>
                                      <span className="text-xs text-dark-400 font-mono truncate">{cert.files.chain}</span>
                                    </div>
                                    <motion.button
                                      onClick={() => copyToClipboard(cert.files!.chain!)}
                                      className="p-1.5 hover:bg-dark-700 rounded text-dark-500 hover:text-dark-300 opacity-0 group-hover/file:opacity-100 transition-opacity"
                                      whileHover={{ scale: 1.1 }}
                                      whileTap={{ scale: 0.9 }}
                                      title={t('common.copy')}
                                    >
                                      <Copy className="w-3.5 h-3.5" />
                                    </motion.button>
                                  </div>
                                )}
                              </div>
                            </motion.div>
                          )}
                        </AnimatePresence>
                      </motion.div>
                    )
                  })}
                </motion.div>
              )}
              
              {/* Certificate Renewal Log */}
              <AnimatePresence>
                {renewLog && (
                  <motion.div 
                    className={`mt-4 rounded-xl text-sm overflow-hidden ${
                      renewLog.success 
                        ? 'bg-success/10 border border-success/20' 
                        : 'bg-danger/10 border border-danger/20'
                    }`}
                    initial={{ opacity: 0, y: -10 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -10 }}
                  >
                    <div className="flex items-center justify-between p-4">
                      <div className="flex items-center gap-3">
                        {renewLog.success ? (
                          <CheckCircle2 className="w-4 h-4 text-success flex-shrink-0" />
                        ) : (
                          <AlertTriangle className="w-4 h-4 text-danger flex-shrink-0" />
                        )}
                        <span className={renewLog.success ? 'text-success' : 'text-danger'}>
                          {renewLog.success ? t('haproxy.renew_success') : t('haproxy.renewal_failed')} ({renewLog.domain})
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        {renewLog.log && (
                          <motion.button
                            onClick={() => setRenewLogExpanded(!renewLogExpanded)}
                            className={`flex items-center gap-1 text-xs px-2 py-1 rounded-lg transition-colors ${
                              renewLog.success ? 'hover:bg-success/20' : 'hover:bg-danger/20'
                            }`}
                            whileHover={{ scale: 1.05 }}
                            whileTap={{ scale: 0.95 }}
                          >
                            {renewLogExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                            {renewLogExpanded ? t('firewall.hide') : t('firewall.details')}
                          </motion.button>
                        )}
                        <motion.button
                          onClick={() => setRenewLog(null)}
                          className={`p-1 rounded transition-colors ${
                            renewLog.success ? 'hover:bg-success/20' : 'hover:bg-danger/20'
                          }`}
                          whileHover={{ scale: 1.1 }}
                        >
                          <X className="w-3 h-3" />
                        </motion.button>
                      </div>
                    </div>
                    <AnimatePresence>
                      {renewLogExpanded && renewLog.log && (
                        <motion.div
                          initial={{ height: 0, opacity: 0 }}
                          animate={{ height: 'auto', opacity: 1 }}
                          exit={{ height: 0, opacity: 0 }}
                          className={`border-t ${renewLog.success ? 'border-success/20' : 'border-danger/20'}`}
                        >
                          <pre className={`p-4 text-xs overflow-auto max-h-64 whitespace-pre-wrap ${
                            renewLog.success ? 'text-success/80 bg-success/5' : 'text-danger/80 bg-danger/5'
                          }`}>
                            {renewLog.log}
                          </pre>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </motion.div>
                )}
              </AnimatePresence>
              </div>
              </div>
            </motion.div>
            
            {/* Config Editor Modal */}
            <AnimatePresence>
              {showConfigModal && (
                <motion.div
                  className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-dark-950/80 backdrop-blur-sm"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  onMouseDown={(e) => {
                    if (e.target === e.currentTarget) {
                      setConfigModalMouseDownOnOverlay(true)
                    }
                  }}
                  onClick={(e) => {
                    if (e.target === e.currentTarget && configModalMouseDownOnOverlay) {
                      setShowConfigModal(false)
                    }
                    setConfigModalMouseDownOnOverlay(false)
                  }}
                >
                  <motion.div
                    className="bg-dark-900 border border-dark-700 rounded-2xl shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col"
                    initial={{ opacity: 0, scale: 0.95, y: 20 }}
                    animate={{ opacity: 1, scale: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95, y: 20 }}
                    onMouseDown={() => setConfigModalMouseDownOnOverlay(false)}
                  >
                    {/* Modal Header */}
                    <div className="flex items-center justify-between p-5 border-b border-dark-700">
                      <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-xl bg-accent-500/10 flex items-center justify-center">
                          <Code className="w-5 h-5 text-accent-500" />
                        </div>
                        <div>
                          <h2 className="text-lg font-semibold text-dark-100">
                            {t('haproxy.config_editor')}
                          </h2>
                          {configPath && (
                            <p className="text-xs text-dark-500 font-mono">{configPath}</p>
                          )}
                        </div>
                      </div>
                      <motion.button
                        onClick={() => setShowConfigModal(false)}
                        className="p-2 hover:bg-dark-800 rounded-lg text-dark-400 transition-colors"
                        whileHover={{ scale: 1.1, rotate: 90 }}
                        whileTap={{ scale: 0.9 }}
                      >
                        <X className="w-5 h-5" />
                      </motion.button>
                    </div>
                    
                    {/* Modal Body */}
                    <div className="flex-1 overflow-hidden p-5">
                      {configLoading ? (
                        <div className="flex items-center justify-center h-64">
                          <div className="flex items-center gap-3 text-dark-400">
                            <Loader2 className="w-5 h-5 animate-spin" />
                            {t('haproxy.config_loading')}
                          </div>
                        </div>
                      ) : (
                        <textarea
                          value={configContent}
                          onChange={(e) => setConfigContent(e.target.value)}
                          className="w-full h-[50vh] bg-dark-950 border border-dark-700 rounded-xl p-4 
                                     font-mono text-sm text-dark-200 resize-none focus:outline-none 
                                     focus:border-accent-500/50 focus:ring-1 focus:ring-accent-500/20
                                     scrollbar-thin scrollbar-thumb-dark-700 scrollbar-track-transparent"
                          spellCheck={false}
                        />
                      )}
                      
                      {/* Status messages */}
                      <AnimatePresence>
                        {configError && (
                          <motion.div
                            className="mt-3 p-3 bg-danger/10 border border-danger/20 rounded-xl text-danger text-sm flex items-center gap-2"
                            initial={{ opacity: 0, y: -10 }}
                            animate={{ opacity: 1, y: 0 }}
                            exit={{ opacity: 0, y: -10 }}
                          >
                            <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                            {configError}
                          </motion.div>
                        )}
                        {configSuccess && (
                          <motion.div
                            className="mt-3 p-3 bg-success/10 border border-success/20 rounded-xl text-success text-sm flex items-center gap-2"
                            initial={{ opacity: 0, y: -10 }}
                            animate={{ opacity: 1, y: 0 }}
                            exit={{ opacity: 0, y: -10 }}
                          >
                            <CheckCircle2 className="w-4 h-4 flex-shrink-0" />
                            {configSuccess}
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </div>
                    
                    {/* Modal Footer */}
                    <div className="flex items-center justify-end gap-3 p-5 border-t border-dark-700">
                      <motion.button
                        onClick={() => setShowConfigModal(false)}
                        className="btn btn-ghost"
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.98 }}
                      >
                        {t('common.cancel')}
                      </motion.button>
                      <motion.button
                        onClick={handleApplyDefaultTemplate}
                        disabled={configLoading}
                        className="btn btn-secondary"
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.98 }}
                        title={t('haproxy.apply_default_template_hint')}
                      >
                        <Shuffle className="w-4 h-4" />
                        {t('haproxy.apply_default_template')}
                      </motion.button>
                      <motion.button
                        onClick={handleSaveConfig}
                        disabled={configSaving || configLoading}
                        className="btn btn-primary"
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.98 }}
                      >
                        {configSaving ? (
                          <>
                            <Loader2 className="w-4 h-4 animate-spin" />
                            {t('haproxy.saving_config')}
                          </>
                        ) : (
                          <>
                            <Save className="w-4 h-4" />
                            {t('haproxy.save_config')}
                          </>
                        )}
                      </motion.button>
                    </div>
                  </motion.div>
                </motion.div>
              )}
            </AnimatePresence>
            
            {/* Firewall Rules */}
            <motion.div 
              className="card mt-6"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4 }}
            >
              <div className="flex items-center justify-between mb-5">
                <button 
                  onClick={() => toggleSection('firewall')}
                  className="flex items-center gap-2 hover:opacity-80 transition-opacity"
                >
                  <motion.div
                    animate={{ rotate: collapsedSections.firewall ? -90 : 0 }}
                    transition={{ duration: 0.2 }}
                  >
                    <ChevronDown className="w-5 h-5 text-dark-400" />
                  </motion.div>
                  <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
                    <Flame className="w-5 h-5 text-orange-500" />
                    {t('firewall.title')}
                  </h2>
                  <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                    firewallActive 
                      ? 'bg-success/10 text-success border border-success/20' 
                      : 'bg-dark-700 text-dark-400 border border-dark-600'
                  }`}>
                    {firewallActive ? t('firewall.active') : t('firewall.inactive')}
                  </span>
                  <span className="text-sm text-dark-500">({filteredFirewallRules.length})</span>
                </button>
                <div className="flex items-center gap-2">
                  {firewallActive ? (
                    <motion.button
                      onClick={handleDisableFirewall}
                      disabled={!!actionLoading}
                      className="btn btn-danger text-sm"
                      whileHover={{ scale: 1.02 }}
                      whileTap={{ scale: 0.98 }}
                    >
                      {actionLoading === 'disable-fw' ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : (
                        <Square className="w-4 h-4" />
                      )}
                      {t('firewall.disable')}
                    </motion.button>
                  ) : (
                    <motion.button
                      onClick={handleEnableFirewall}
                      disabled={!!actionLoading}
                      className="btn btn-success text-sm"
                      whileHover={{ scale: 1.02 }}
                      whileTap={{ scale: 0.98 }}
                    >
                      {actionLoading === 'enable-fw' ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : (
                        <Play className="w-4 h-4" />
                      )}
                      {t('firewall.enable')}
                    </motion.button>
                  )}
                  <motion.button
                    onClick={() => {
                      setShowFirewallForm(true)
                      setFirewallForm({ port: '', protocol: 'any', action: 'allow', from_ip: '', direction: 'in' })
                      setFirewallFormError('')
                      setFirewallErrorLog(null)
                    }}
                    className="btn btn-primary text-sm"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    <Plus className="w-4 h-4" />
                    {t('firewall.open_port')}
                  </motion.button>
                </div>
              </div>
              
              <div className={`collapse-grid ${!collapsedSections.firewall ? 'open' : ''}`}>
              <div className="collapse-content">
              {/* Firewall Error Display (outside form) */}
              <AnimatePresence>
                {firewallFormError && !showFirewallForm && (
                  <motion.div 
                    className="mb-4 bg-danger/10 border border-danger/20 rounded-xl text-danger text-sm overflow-hidden"
                    initial={{ opacity: 0, y: -10 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -10 }}
                  >
                    <div className="flex items-center justify-between p-4">
                      <div className="flex items-center gap-3">
                        <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                        <span>{firewallFormError}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        {firewallErrorLog && (
                          <motion.button
                            onClick={() => setFirewallErrorExpanded(!firewallErrorExpanded)}
                            className="flex items-center gap-1 text-xs px-2 py-1 hover:bg-danger/20 rounded-lg transition-colors"
                            whileHover={{ scale: 1.05 }}
                            whileTap={{ scale: 0.95 }}
                          >
                            {firewallErrorExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                            {firewallErrorExpanded ? t('common.hide') : t('common.details')}
                          </motion.button>
                        )}
                        <motion.button
                          onClick={() => {
                            setFirewallFormError('')
                            setFirewallErrorLog(null)
                          }}
                          className="p-1 hover:bg-danger/20 rounded transition-colors"
                          whileHover={{ scale: 1.1 }}
                        >
                          <X className="w-3 h-3" />
                        </motion.button>
                      </div>
                    </div>
                    <AnimatePresence>
                      {firewallErrorExpanded && firewallErrorLog && (
                        <motion.div
                          initial={{ height: 0, opacity: 0 }}
                          animate={{ height: 'auto', opacity: 1 }}
                          exit={{ height: 0, opacity: 0 }}
                          className="border-t border-danger/20"
                        >
                          <pre className="p-4 text-xs overflow-auto max-h-64 whitespace-pre-wrap text-danger/80 bg-danger/5">
                            {firewallErrorLog}
                          </pre>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </motion.div>
                )}
              </AnimatePresence>
              
              {/* Firewall Form */}
              <AnimatePresence>
                {showFirewallForm && (
                  <motion.div 
                    className="mb-6 p-5 bg-dark-800/50 rounded-xl border border-dark-700/50"
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.3 }}
                  >
                    <div className="flex items-center justify-between mb-4">
                      <h3 className="font-medium text-dark-200 flex items-center gap-2">
                        <Plus className="w-4 h-4 text-orange-500" />
                        {t('firewall.open_port')}
                      </h3>
                      <motion.button
                        onClick={() => {
                          setShowFirewallForm(false)
                          setFirewallFormError('')
                          setFirewallErrorLog(null)
                        }}
                        className="p-2 hover:bg-dark-700 rounded-lg text-dark-400 transition-colors"
                        whileHover={{ scale: 1.1, rotate: 90 }}
                        whileTap={{ scale: 0.9 }}
                      >
                        <X className="w-5 h-5" />
                      </motion.button>
                    </div>
                    
                    <AnimatePresence>
                      {firewallFormError && (
                        <motion.div 
                          className="mb-4 bg-danger/10 border border-danger/20 rounded-xl text-danger text-sm overflow-hidden"
                          initial={{ opacity: 0, y: -10 }}
                          animate={{ opacity: 1, y: 0 }}
                          exit={{ opacity: 0, y: -10 }}
                        >
                          <div className="flex items-center justify-between p-4">
                            <div className="flex items-center gap-3">
                              <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                              <span>{firewallFormError}</span>
                            </div>
                            {firewallErrorLog && (
                              <motion.button
                                onClick={() => setFirewallErrorExpanded(!firewallErrorExpanded)}
                                className="flex items-center gap-1 text-xs px-2 py-1 hover:bg-danger/20 rounded-lg transition-colors"
                                whileHover={{ scale: 1.05 }}
                                whileTap={{ scale: 0.95 }}
                              >
                                {firewallErrorExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                                {firewallErrorExpanded ? t('firewall.hide') : t('firewall.details')}
                              </motion.button>
                            )}
                          </div>
                          <AnimatePresence>
                            {firewallErrorExpanded && firewallErrorLog && (
                              <motion.div
                                initial={{ height: 0, opacity: 0 }}
                                animate={{ height: 'auto', opacity: 1 }}
                                exit={{ height: 0, opacity: 0 }}
                                className="border-t border-danger/20"
                              >
                                <pre className="p-4 text-xs overflow-auto max-h-64 whitespace-pre-wrap text-danger/80 bg-danger/5">
                                  {firewallErrorLog}
                                </pre>
                              </motion.div>
                            )}
                          </AnimatePresence>
                        </motion.div>
                      )}
                    </AnimatePresence>
                    
                    <form onSubmit={handleAddFirewallRule} className="space-y-4">
                      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('firewall.port')}</label>
                          <input
                            type="number"
                            value={firewallForm.port}
                            onChange={(e) => setFirewallForm(f => ({ ...f, port: e.target.value }))}
                            placeholder="80"
                            className="input"
                            min="1"
                            max="65535"
                            required
                          />
                        </div>
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('firewall.protocol')}</label>
                          <select
                            value={firewallForm.protocol}
                            onChange={(e) => setFirewallForm(f => ({ ...f, protocol: e.target.value as 'tcp' | 'udp' | 'any' }))}
                            className="input"
                          >
                            <option value="tcp">TCP</option>
                            <option value="udp">UDP</option>
                            <option value="any">Any</option>
                          </select>
                        </div>
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('firewall.action')}</label>
                          <select
                            value={firewallForm.action}
                            onChange={(e) => setFirewallForm(f => ({ ...f, action: e.target.value as 'allow' | 'deny' }))}
                            className="input"
                          >
                            <option value="allow">{t('firewall.allow')}</option>
                            <option value="deny">{t('firewall.deny')}</option>
                          </select>
                        </div>
                      </div>
                      
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('firewall.from')} (IP)</label>
                          <input
                            type="text"
                            value={firewallForm.from_ip}
                            onChange={(e) => setFirewallForm(f => ({ ...f, from_ip: e.target.value }))}
                            placeholder={t('firewall.from_placeholder')}
                            className="input"
                          />
                          <p className="text-xs text-dark-500 mt-1">{t('firewall.from_hint')}</p>
                        </div>
                        <div>
                          <label className="block text-sm text-dark-400 mb-2">{t('firewall.direction')}</label>
                          <select
                            value={firewallForm.direction}
                            onChange={(e) => setFirewallForm(f => ({ ...f, direction: e.target.value as 'in' | 'out' }))}
                            className="input"
                          >
                            <option value="in">{t('firewall.direction_in')}</option>
                            <option value="out">{t('firewall.direction_out')}</option>
                          </select>
                        </div>
                      </div>
                      
                      <div className="flex gap-3 pt-2">
                        <motion.button
                          type="submit"
                          disabled={actionLoading === 'add-fw-rule'}
                          className="btn btn-primary"
                          whileHover={{ scale: 1.02 }}
                          whileTap={{ scale: 0.98 }}
                        >
                          {actionLoading === 'add-fw-rule' ? (
                            <motion.div
                              animate={{ rotate: 360 }}
                              transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                            >
                              <Loader2 className="w-4 h-4" />
                            </motion.div>
                          ) : (
                            t('firewall.open_port')
                          )}
                        </motion.button>
                      </div>
                    </form>
                  </motion.div>
                )}
              </AnimatePresence>
              
              {firewallRules.length === 0 ? (
                <motion.div 
                  className="text-center py-12 text-dark-500"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                >
                  <motion.div
                    animate={{ y: [0, -5, 0] }}
                    transition={{ duration: 3, repeat: Infinity }}
                  >
                    <Flame className="w-12 h-12 mx-auto mb-3 opacity-50" />
                  </motion.div>
                  <p>{t('firewall.no_rules')}</p>
                </motion.div>
              ) : (
                <motion.div className="space-y-3" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                  {filteredFirewallRules.map((rule, index) => (
                    <motion.div
                      key={`${rule.port}-${rule.protocol}-${rule.number}`}
                      className="flex items-center justify-between p-4 bg-dark-800/50 rounded-xl border border-dark-700/30 group hover:border-dark-600 transition-all"
                      initial={{ opacity: 0, y: 20 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: index * 0.05 }}
                      whileHover={{ scale: 1.01 }}
                    >
                      <div className="flex items-center gap-4">
                        <motion.div 
                          className={`w-12 h-12 rounded-xl flex items-center justify-center ${
                            rule.action === 'ALLOW' ? 'bg-success/10' : 'bg-danger/10'
                          }`}
                          whileHover={{ rotate: 5, scale: 1.05 }}
                        >
                          <Flame className={`w-5 h-5 ${
                            rule.action === 'ALLOW' ? 'text-success' : 'text-danger'
                          }`} />
                        </motion.div>
                        <div>
                          <p className="font-medium text-dark-100 group-hover:text-white transition-colors font-mono">
                            {t('firewall.port')} {rule.port}
                          </p>
                          <p className="text-sm text-dark-500">
                            {rule.protocol.toUpperCase()} • {rule.from_ip} • {rule.direction}
                          </p>
                        </div>
                      </div>
                      
                      <div className="flex items-center gap-2">
                        <span className={`px-2.5 py-1 rounded-lg text-xs font-medium ${
                          rule.action === 'ALLOW' 
                            ? 'bg-success/10 text-success border border-success/20' 
                            : 'bg-danger/10 text-danger border border-danger/20'
                        }`}>
                          {rule.action}
                        </span>
                        <motion.button
                          onClick={() => handleDeleteFirewallRule(rule.number, rule.port, rule.protocol)}
                          disabled={actionLoading === `delete-fw-${rule.number}`}
                          className="btn btn-ghost p-2.5 text-danger hover:bg-danger/10"
                          whileHover={{ scale: 1.1 }}
                          whileTap={{ scale: 0.9 }}
                          title={t('firewall.close_port')}
                        >
                          {actionLoading === `delete-fw-${rule.number}` ? (
                            <motion.div
                              animate={{ rotate: 360 }}
                              transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                            >
                              <Loader2 className="w-4 h-4" />
                            </motion.div>
                          ) : (
                            <Trash2 className="w-4 h-4" />
                          )}
                        </motion.button>
                      </div>
                    </motion.div>
                  ))}
                </motion.div>
              )}
              </div>
              </div>
            </motion.div>
            
          </motion.div>
        )}
      </AnimatePresence>
      
    </motion.div>
  )
}
