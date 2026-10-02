import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import {
  Download,
  RefreshCw,
  CheckCircle2,
  XCircle,
  Loader2,
  Server as ServerIcon,
  Package,
  ArrowUpCircle,
  AlertTriangle,
  Clock,
  Check,
  Rocket,
  Upload,
  Folder,
  FolderOpen,
  ChevronDown,
  ChevronRight,
  X,
  Globe,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import {
  systemApi, nodeImageApi, haproxyUpgradeApi, VersionBaseInfo, SingleNodeVersion, ImageDeliveryJobInfo, RemnawaveInstallJobInfo,
  NodeUpdateProgress,
} from '../api/client'
import { Skeleton } from '../components/ui/Skeleton'
import { Tooltip } from '../components/ui/Tooltip'
import { Checkbox } from '../components/ui/Checkbox'
import { FAQIcon } from '../components/FAQ'
import { useSettingsStore } from '../stores/settingsStore'
import { orderFolders } from '../utils/folders'
import { useCollapsedFolders } from '../hooks/useCollapsedFolders'
import DeliverImageModal from '../components/servers/DeliverImageModal'
import BulkDeliverImageModal from '../components/servers/BulkDeliverImageModal'
import NodeUpdateCard, { NodeState } from '../components/updates/NodeUpdateCard'
import HAProxyUpgradeModal, { HAProxyUpgradeTarget } from '../components/updates/HAProxyUpgradeModal'
import DownloadProxyModal, { DownloadProxyTarget } from '../components/updates/DownloadProxyModal'

// Пока идёт обновление ноды, SSH-доставка или обновление HAProxy хоть на одной ноде — статусы на карточках обновляются с этим шагом
const JOB_POLL_INTERVAL_MS = 3_000
// После смены прокси нода перезапускает Docker — версию перечитываем, когда агент снова поднялся
const PROXY_REFRESH_DELAY_MS = 40_000
const COLLAPSED_FOLDERS_KEY = 'updates_collapsed_folders'
const NODE_GRID_CLASS = 'grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3'

// После запуска обновления панель уходит в перезапуск (updater-контейнер пересобирает образ).
// Ждём 10с прежде чем начать опрос — за это время старая панель успевает погаснуть.
const PANEL_REBOOT_INITIAL_DELAY_MS = 10_000
const PANEL_REBOOT_POLL_INTERVAL_MS = 5_000
const PANEL_PROBE_TIMEOUT_MS = 4_000

const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

const toHAProxyTarget = (node: NodeState): HAProxyUpgradeTarget => ({
  id: node.id, name: node.name, version: node.haproxyVersion, targetVersion: node.haproxyTarget,
  hasSshCreds: node.hasSshCreds,
})

// /health из браузера недоступен (nginx отдаёт его только внутренним IP), поэтому живость
// бэкенда проверяем лёгким /api/auth/check напрямую через fetch — минуя axios-интерсепторы
// (редирект на /login при 401 и ретраи нам тут не нужны). Любой HTTP-ответ кроме шлюзовой
// ошибки означает, что бэкенд снова поднялся; сетевой сбой или 502/503/504 — ещё лежит.
async function isPanelBackendAlive(): Promise<boolean> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), PANEL_PROBE_TIMEOUT_MS)
  try {
    const resp = await fetch('/api/auth/check', {
      method: 'GET',
      cache: 'no-store',
      credentials: 'include',
      signal: controller.signal,
    })
    return resp.status !== 502 && resp.status !== 503 && resp.status !== 504
  } catch {
    return false
  } finally {
    clearTimeout(timer)
  }
}

export default function Updates() {
  const { t } = useTranslation()
  const { updateBranch, fetchSettings } = useSettingsStore()
  // На dev-ветке версии между пушами могут не меняться — обновление разрешено всегда
  const isDevChannel = updateBranch === 'dev'

  const [baseInfo, setBaseInfo] = useState<VersionBaseInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [nodes, setNodes] = useState<Map<number, NodeState>>(new Map())

  const [updatingPanel, setUpdatingPanel] = useState(false)
  const [updatingNodes, setUpdatingNodes] = useState<Set<number>>(new Set())
  const [updatingAll, setUpdatingAll] = useState(false)
  const [updatingEverything, setUpdatingEverything] = useState(false)

  const [updateResults, setUpdateResults] = useState<Record<string, { success: boolean; message: string }>>({})

  const [isChecking, setIsChecking] = useState(false)
  const [deliverTarget, setDeliverTarget] = useState<{ id: number; name: string } | null>(null)
  const [bulkDeliverOpen, setBulkDeliverOpen] = useState(false)
  // Последняя SSH-доставка по каждому серверу: идущая или завершённая недавно
  const [deliveryJobs, setDeliveryJobs] = useState<Map<number, ImageDeliveryJobInfo>>(new Map())
  // Завершённые доставки, после которых запустили обычное обновление: на карточке
  // они уступают его статусу, иначе старая «SSH: ошибка» прячет идущее обновление
  const [supersededDeliveries, setSupersededDeliveries] = useState<Set<string>>(new Set())
  // Последнее обновление HAProxy по каждому серверу: идущее или завершённое недавно
  const [haproxyJobs, setHaproxyJobs] = useState<Map<number, RemnawaveInstallJobInfo>>(new Map())
  // Идущие обновления нод через агента — их держит панель, поэтому видны и после перезагрузки страницы
  const [nodeUpdates, setNodeUpdates] = useState<Map<number, NodeUpdateProgress>>(new Map())
  const [haproxyModal, setHaproxyModal] = useState<{ targets: HAProxyUpgradeTarget[]; jobId: string | null } | null>(null)
  const [proxyModal, setProxyModal] = useState<DownloadProxyTarget[] | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [collapsedFolders, toggleFolderCollapsed] = useCollapsedFolders(COLLAPSED_FOLDERS_KEY)

  const abortRef = useRef(false)
  const rebootWaitCancelRef = useRef(false)
  const deliveryStatusRef = useRef<Map<string, ImageDeliveryJobInfo['status']>>(new Map())
  const haproxyStatusRef = useRef<Map<string, RemnawaveInstallJobInfo['status']>>(new Map())
  const nodeUpdateIdsRef = useRef<Set<number>>(new Set())
  const lastActivityRef = useRef(Date.now())
  const IDLE_THRESHOLD = 5000
  const AUTO_REFRESH_INTERVAL = 12000

  useEffect(() => () => { rebootWaitCancelRef.current = true }, [])

  useEffect(() => {
    const markActive = () => { lastActivityRef.current = Date.now() }
    const events = ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart'] as const
    events.forEach(e => document.addEventListener(e, markActive))
    return () => { events.forEach(e => document.removeEventListener(e, markActive)) }
  }, [])

  const fetchNodeVersion = useCallback(async (nodeId: number) => {
    setNodes(prev => {
      const next = new Map(prev)
      const existing = next.get(nodeId)
      if (existing) next.set(nodeId, { ...existing, loadState: 'loading' })
      return next
    })

    try {
      const resp = await systemApi.getNodeVersionById(nodeId)
      const data: SingleNodeVersion = resp.data

      setNodes(prev => {
        const existing = prev.get(nodeId)
        if (!existing) return prev
        const next = new Map(prev)
        next.set(nodeId, {
          ...existing,
          name: data.name,
          url: data.url,
          loadState: 'loaded',
          version: data.version,
          status: data.status,
          haproxyVersion: data.haproxy?.version ?? null,
          haproxyTarget: data.haproxy?.target_version ?? null,
          downloadProxy: data.download_proxy ?? null,
        })
        return next
      })
    } catch {
      setNodes(prev => {
        const next = new Map(prev)
        const existing = next.get(nodeId)
        if (existing) {
          next.set(nodeId, { ...existing, loadState: 'error', status: 'offline' })
        }
        return next
      })
    }
  }, [])

  const fetchBase = useCallback(async (showCheckingState = false) => {
    try {
      setError('')
      if (showCheckingState) setIsChecking(true)
      abortRef.current = false

      const resp = await systemApi.getVersionBase()
      const data = resp.data
      setBaseInfo(data)

      const initialNodes = new Map<number, NodeState>()
      for (const n of data.nodes) {
        initialNodes.set(n.id, {
          id: n.id,
          name: n.name,
          url: n.url,
          folder: n.folder,
          hasSshCreds: n.has_ssh_creds,
          loadState: 'pending',
          version: null,
          status: 'offline',
          haproxyVersion: null,
          haproxyTarget: null,
          downloadProxy: null,
        })
      }
      setNodes(initialNodes)

      // Promise pool: ограничиваем параллельные запросы к нодам, иначе при 100+ нодах
      // autorefresh каждые 12с создаёт лавину одновременных HTTP, и ноды мигают онлайн/оффлайн.
      const queue = [...data.nodes]
      const POOL_SIZE = 12
      const worker = async () => {
        while (queue.length > 0) {
          if (abortRef.current) return
          const node = queue.shift()
          if (!node) return
          await fetchNodeVersion(node.id)
        }
      }
      const workers = Array.from({ length: Math.min(POOL_SIZE, queue.length) }, worker)
      Promise.all(workers).catch(() => { /* per-node ошибки уже обработаны внутри fetchNodeVersion */ })
    } catch {
      setError(t('updates.failed_fetch'))
    } finally {
      setLoading(false)
      setIsChecking(false)
    }
  }, [t, fetchNodeVersion])

  useEffect(() => {
    fetchSettings()
    fetchBase()
    return () => { abortRef.current = true }
  }, [fetchSettings, fetchBase])

  const fetchDeliveryJobs = useCallback(async () => {
    try {
      const { data } = await nodeImageApi.jobs()
      const byServer = new Map<number, ImageDeliveryJobInfo>()
      for (const job of data.jobs) {
        byServer.set(job.server_id, job)
        const prevStatus = deliveryStatusRef.current.get(job.job_id)
        // Нода только что поднялась на доставленном образе — сразу показать её новую версию
        if (job.status === 'success' && prevStatus && prevStatus !== 'success') fetchNodeVersion(job.server_id)
        deliveryStatusRef.current.set(job.job_id, job.status)
      }
      setDeliveryJobs(byServer)
    } catch {
      // статусы SSH-доставок — дополнение к странице, без них она работает как раньше
    }
  }, [fetchNodeVersion])

  useEffect(() => { fetchDeliveryJobs() }, [fetchDeliveryJobs])

  const hasActiveDelivery = Array.from(deliveryJobs.values())
    .some(j => j.status === 'queued' || j.status === 'running')

  useEffect(() => {
    if (!hasActiveDelivery) return
    const id = setInterval(fetchDeliveryJobs, JOB_POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [hasActiveDelivery, fetchDeliveryJobs])

  const fetchHaproxyJobs = useCallback(async () => {
    try {
      const { data } = await haproxyUpgradeApi.jobs()
      const byServer = new Map<number, RemnawaveInstallJobInfo>()
      for (const job of data.jobs) {
        byServer.set(job.server_id, job)
        const prevStatus = haproxyStatusRef.current.get(job.job_id)
        // Обновление HAProxy только что закончилось — показать новую версию на карточке
        if (job.status !== 'running' && prevStatus === 'running') fetchNodeVersion(job.server_id)
        haproxyStatusRef.current.set(job.job_id, job.status)
      }
      setHaproxyJobs(byServer)
    } catch {
      // статусы обновлений HAProxy — дополнение к странице, без них она работает как раньше
    }
  }, [fetchNodeVersion])

  useEffect(() => { fetchHaproxyJobs() }, [fetchHaproxyJobs])

  const hasActiveHaproxyUpgrade = Array.from(haproxyJobs.values()).some(j => j.status === 'running')

  useEffect(() => {
    if (!hasActiveHaproxyUpgrade) return
    const id = setInterval(fetchHaproxyJobs, JOB_POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [hasActiveHaproxyUpgrade, fetchHaproxyJobs])

  const fetchNodeUpdates = useCallback(async () => {
    try {
      const { data } = await systemApi.nodeUpdates()
      const byServer = new Map(data.updates.map(update => [update.server_id, update]))
      const finished = [...nodeUpdateIdsRef.current].filter(id => !byServer.has(id))
      nodeUpdateIdsRef.current = new Set(byServer.keys())
      setNodeUpdates(byServer)
      if (finished.length === 0) return
      // Обновление закончилось: показать новую версию, а при провале — запущенное панелью обновление по SSH
      finished.forEach(id => fetchNodeVersion(id))
      fetchDeliveryJobs()
    } catch {
      // статусы обновлений — дополнение к странице, без них она работает как раньше
    }
  }, [fetchNodeVersion, fetchDeliveryJobs])

  useEffect(() => { fetchNodeUpdates() }, [fetchNodeUpdates])

  const hasActiveNodeUpdate = nodeUpdates.size > 0

  useEffect(() => {
    if (!hasActiveNodeUpdate) return
    const id = setInterval(fetchNodeUpdates, JOB_POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [hasActiveNodeUpdate, fetchNodeUpdates])

  useEffect(() => {
    const id = setInterval(() => {
      const isIdle = Date.now() - lastActivityRef.current > IDLE_THRESHOLD
      const isVisible = !document.hidden
      const isBusy = updatingPanel || updatingNodes.size > 0 || updatingAll || updatingEverything || isChecking
      if (!isIdle || !isVisible || isBusy) return
      fetchBase()
      // Панель сама запускает обновление по SSH, если нода не обновилась через агента
      fetchDeliveryJobs()
      fetchNodeUpdates()
    }, AUTO_REFRESH_INTERVAL)
    return () => clearInterval(id)
  }, [fetchBase, fetchDeliveryJobs, fetchNodeUpdates, updatingPanel, updatingNodes, updatingAll, updatingEverything, isChecking])

  const handleRefresh = useCallback(() => {
    abortRef.current = true
    setUpdateResults({})
    fetchBase(true)
    fetchDeliveryJobs()
    fetchHaproxyJobs()
    fetchNodeUpdates()
  }, [fetchBase, fetchDeliveryJobs, fetchHaproxyJobs, fetchNodeUpdates])

  // Дожидаемся, пока панель сначала уйдёт в перезапуск (бэкенд недоступен), а затем снова
  // поднимется, и только тогда перезагружаем страницу. Требование "сначала увидеть падение"
  // защищает от reload на ещё живой старой панели, пока updater пересобирает образ.
  const waitForPanelRebootAndReload = useCallback(async () => {
    rebootWaitCancelRef.current = false
    setUpdateResults(prev => ({
      ...prev,
      panel: { success: true, message: t('updates.waiting_reboot') }
    }))

    await sleep(PANEL_REBOOT_INITIAL_DELAY_MS)

    let sawDown = false
    while (!rebootWaitCancelRef.current) {
      const alive = await isPanelBackendAlive()
      if (!alive) {
        sawDown = true
      } else if (sawDown) {
        setUpdateResults(prev => ({
          ...prev,
          panel: { success: true, message: t('updates.panel_back') }
        }))
        window.location.reload()
        return
      }
      await sleep(PANEL_REBOOT_POLL_INTERVAL_MS)
    }
  }, [t])

  const handleUpdatePanel = async () => {
    if (updatingPanel) return

    setUpdatingPanel(true)
    setUpdateResults(prev => ({ ...prev, panel: { success: true, message: t('updates.in_progress') } }))

    try {
      const response = await systemApi.updatePanel()
      setUpdateResults(prev => ({
        ...prev,
        panel: { success: true, message: response.data.message }
      }))
      toast.success(t('updates.panel_restarting'))
      waitForPanelRebootAndReload()
    } catch (err: any) {
      setUpdateResults(prev => ({
        ...prev,
        panel: { success: false, message: err.response?.data?.detail || t('updates.failed_update') }
      }))
      toast.error(err.response?.data?.detail || t('updates.failed_update'))
      setUpdatingPanel(false)
    }
  }

  const handleUpdateNode = async (nodeId: number, nodeName: string) => {
    if (updatingNodes.has(nodeId)) return

    const delivery = deliveryJobs.get(nodeId)
    if (delivery && (delivery.status === 'success' || delivery.status === 'error')) {
      setSupersededDeliveries(prev => new Set(prev).add(delivery.job_id))
    }
    setUpdatingNodes(prev => new Set(prev).add(nodeId))
    setUpdateResults(prev => ({
      ...prev,
      [`node-${nodeId}`]: { success: true, message: t('updates.in_progress') }
    }))

    try {
      await systemApi.updateNode(nodeId)
      // Дальше статус ведёт панель: этап обновления приходит из списка идущих обновлений
      setUpdateResults(prev => {
        const next = { ...prev }
        delete next[`node-${nodeId}`]
        return next
      })
      await fetchNodeUpdates()
    } catch (err: any) {
      setUpdateResults(prev => ({
        ...prev,
        [`node-${nodeId}`]: { success: false, message: err.response?.data?.detail || t('updates.failed_update') }
      }))
      toast.error(`${nodeName}: ${err.response?.data?.detail || t('updates.failed_update')}`)
    } finally {
      setUpdatingNodes(prev => {
        const next = new Set(prev)
        next.delete(nodeId)
        return next
      })
    }
  }

  const collectNodeUpdateTargets = () => Array.from(nodes.values()).filter(canAgentUpdate)

  const handleUpdateAllNodes = async () => {
    if (updatingAll || !baseInfo) return

    setUpdatingAll(true)
    await Promise.all(collectNodeUpdateTargets().map(n => handleUpdateNode(n.id, n.name)))
    setUpdatingAll(false)
  }

  // «Обновить всё»: сначала рассылаются запуски обновления на все подходящие ноды
  // (каждая обновляет себя сама, результат отдельной ноды не блокирует процесс),
  // и как только все запросы отправлены — панель запускает обновление самой себя.
  const handleUpdateEverything = async () => {
    if (updatingEverything || updatingAll || updatingPanel || !baseInfo) return

    setUpdatingEverything(true)
    try {
      const targets = collectNodeUpdateTargets()
      if (targets.length > 0) {
        toast.info(t('updates.everything_nodes_started', { count: targets.length }))
        setUpdatingAll(true)
        await Promise.all(targets.map(n => handleUpdateNode(n.id, n.name)))
        setUpdatingAll(false)
      }

      if (panelCanUpdate) {
        await handleUpdatePanel()
      } else {
        toast.success(t('updates.everything_panel_skipped'))
      }
    } finally {
      setUpdatingEverything(false)
    }
  }

  const getNodeNeedsUpdate = (node: NodeState): boolean => {
    if (!node.version || !baseInfo?.node.latest_version) return false
    return node.version !== baseInfo.node.latest_version
  }

  // Обновление через агента ноды: только онлайн, не идущее уже, и на стабильном канале — только отстающие
  const canAgentUpdate = (node: NodeState): boolean =>
    node.loadState === 'loaded' && node.status === 'online' && !nodeUpdates.has(node.id)
    && (isDevChannel || getNodeNeedsUpdate(node))

  const toggleSelected = (ids: number[], on: boolean) => {
    setSelected(prev => {
      const next = new Set(prev)
      for (const id of ids) {
        if (on) next.add(id)
        else next.delete(id)
      }
      return next
    })
  }

  const loadedNodes = Array.from(nodes.values())
  const nodesNeedUpdate = loadedNodes.filter(n =>
    n.loadState === 'loaded' && n.status === 'online' && getNodeNeedsUpdate(n)
  ).length
  const onlineNodesCount = loadedNodes.filter(n =>
    n.loadState === 'loaded' && n.status === 'online'
  ).length
  // На dev-канале кнопка «Обновить все» доступна для всех онлайн-нод
  const updateAllCount = isDevChannel ? onlineNodesCount : nodesNeedUpdate
  const panelCanUpdate = isDevChannel || !!baseInfo?.panel.update_available
  const canUpdateEverything = updateAllCount > 0 || panelCanUpdate

  const allNodesLoaded = loadedNodes.every(n => n.loadState === 'loaded' || n.loadState === 'error')

  const selectedNodes = loadedNodes.filter(n => selected.has(n.id))
  const selectedAgentTargets = selectedNodes.filter(canAgentUpdate)
  const selectedProxyTargets = selectedNodes.filter(n => n.status === 'online' && n.downloadProxy !== null)
  const selectedHaproxyTargets = selectedNodes.filter(n =>
    n.status === 'online' && n.haproxyTarget && haproxyJobs.get(n.id)?.status !== 'running'
  )
  const allSelected = loadedNodes.length > 0 && selectedNodes.length === loadedNodes.length

  // Папки — как на дашборде: порядок пользователя, внутри папки — порядок серверов
  const nodeGroups = useMemo(() => {
    const byFolder = new Map<string, NodeState[]>()
    const unfoldered: NodeState[] = []
    for (const node of nodes.values()) {
      if (!node.folder) {
        unfoldered.push(node)
        continue
      }
      const members = byFolder.get(node.folder)
      if (members) members.push(node)
      else byFolder.set(node.folder, [node])
    }
    const folders = orderFolders([...byFolder.keys()]).map(name => ({ name, members: byFolder.get(name)! }))
    return { folders, unfoldered }
  }, [nodes])

  const handleUpdateSelected = async () => {
    const targets = selectedAgentTargets
    setSelected(new Set())
    await Promise.all(targets.map(n => handleUpdateNode(n.id, n.name)))
  }

  const handleBulkDeliveryStarted = () => {
    setSelected(new Set())
    fetchDeliveryJobs()
  }

  const visibleDeliveryJob = (nodeId: number) => {
    const job = deliveryJobs.get(nodeId)
    return job && !supersededDeliveries.has(job.job_id) ? job : undefined
  }

  const renderNodeCard = (node: NodeState, index: number) => (
    <NodeUpdateCard
      key={node.id}
      node={node}
      index={index}
      needsUpdate={node.loadState === 'loaded' && getNodeNeedsUpdate(node)}
      isDevChannel={isDevChannel}
      isUpdating={updatingNodes.has(node.id)}
      updateResult={updateResults[`node-${node.id}`]}
      agentUpdate={nodeUpdates.get(node.id)}
      deliveryJob={visibleDeliveryJob(node.id)}
      haproxyJob={haproxyJobs.get(node.id)}
      selected={selected.has(node.id)}
      onToggleSelect={() => toggleSelected([node.id], !selected.has(node.id))}
      onUpdate={() => handleUpdateNode(node.id, node.name)}
      onOpenDelivery={() => setDeliverTarget({ id: node.id, name: node.name })}
      onOpenHAProxy={() => setHaproxyModal({ targets: [toHAProxyTarget(node)], jobId: haproxyJobs.get(node.id)?.job_id ?? null })}
      onOpenProxy={() => setProxyModal([{ id: node.id, name: node.name }])}
    />
  )

  const renderFolder = (name: string, members: NodeState[]) => {
    const isCollapsed = collapsedFolders.has(name)
    const selectedInFolder = members.filter(n => selected.has(n.id)).length
    const FolderIcon = isCollapsed ? Folder : FolderOpen
    const Chevron = isCollapsed ? ChevronRight : ChevronDown
    return (
      <div key={name} className="rounded-xl border bg-dark-900/50 border-dark-800/50 overflow-hidden">
        <div className="flex items-center gap-3 px-4 py-3">
          <Checkbox
            checked={selectedInFolder === members.length}
            indeterminate={selectedInFolder > 0 && selectedInFolder < members.length}
            onChange={() => toggleSelected(members.map(n => n.id), selectedInFolder < members.length)}
          />
          <button onClick={() => toggleFolderCollapsed(name)} className="flex items-center gap-2.5 flex-1 min-w-0 group">
            <div className="w-8 h-8 rounded-lg bg-blue-500/15 flex items-center justify-center flex-shrink-0">
              <FolderIcon className="w-4 h-4 text-blue-400" />
            </div>
            <span className="text-sm font-semibold text-white truncate group-hover:text-blue-300 transition">{name}</span>
            <span className="text-xs text-dark-500 flex-shrink-0">{members.length}</span>
            <Chevron className="w-3.5 h-3.5 text-dark-600 flex-shrink-0" />
          </button>
        </div>
        <AnimatePresence initial={false}>
          {!isCollapsed && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="px-3 pb-3">
                <div className={NODE_GRID_CLASS}>{members.map(renderNodeCard)}</div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    )
  }

  if (loading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
        <div className="flex items-center gap-3 mb-6">
          <Skeleton className="w-10 h-10 rounded-xl" />
          <div>
            <Skeleton className="h-6 w-44 mb-2" />
            <Skeleton className="h-4 w-64" />
          </div>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="card">
              <Skeleton className="h-5 w-32 mb-4" />
              <Skeleton className="h-20 w-full" />
            </div>
          ))}
        </div>
      </motion.div>
    )
  }

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
      {/* Header */}
      <motion.div
        className="flex items-center justify-between mb-8"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <div>
          <motion.h1
            className="text-2xl font-bold text-dark-50 flex items-center gap-3"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
          >
            <Package className="w-7 h-7 text-accent-400" />
            {t('updates.title')}
            {isDevChannel && (
              <span className="px-2.5 py-1 text-xs font-medium rounded-full bg-warning/15 text-warning border border-warning/20">
                {t('updates.dev_channel_badge')}
              </span>
            )}
            <FAQIcon screen="PAGE_UPDATES" />
          </motion.h1>
          <motion.p
            className="text-dark-400 mt-1"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.1 }}
          >
            {t('updates.subtitle')}
          </motion.p>
        </div>

        <div className="flex items-center gap-3">
          <motion.button
            onClick={handleRefresh}
            className="btn btn-secondary"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
            disabled={loading || isChecking}
          >
            <RefreshCw className={`w-4 h-4 ${isChecking ? 'animate-spin' : ''}`} />
            {isChecking ? t('updates.checking') : t('common.refresh')}
          </motion.button>

          {canUpdateEverything && (
            <Tooltip label={t('updates.update_everything_hint')} maxWidth={320}>
            <motion.button
              onClick={handleUpdateEverything}
              disabled={updatingEverything || updatingAll || updatingPanel || updatingNodes.size > 0}
              className="btn btn-primary"
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              {updatingEverything ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Rocket className="w-4 h-4" />
              )}
              {updatingEverything ? t('updates.updating_everything') : t('updates.update_everything')}
            </motion.button>
            </Tooltip>
          )}
        </div>
      </motion.div>

      {/* Error */}
      <AnimatePresence>
        {error && (
          <motion.div
            className="flex items-center gap-3 p-4 mb-6 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
          >
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm">{error}</span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Panel Version Card */}
      <motion.div
        className="card mb-6 group hover:border-dark-700 transition-all"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <motion.div
              className="w-14 h-14 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ rotate: 5, scale: 1.05 }}
            >
              <Package className="w-6 h-6 text-accent-500" />
            </motion.div>
            <div>
              <h2 className="text-lg font-semibold text-dark-100">{t('updates.panel')}</h2>
              <div className="flex items-center gap-3 mt-1">
                <span className="text-dark-400 text-sm">
                  {t('updates.current_version')}:
                  <span className="text-dark-200 ml-1 font-mono">
                    v{baseInfo?.panel.version || '?'}
                  </span>
                </span>
                {baseInfo?.panel.latest_version && (
                  <span className="text-dark-500 text-sm">
                    {t('updates.latest')}:
                    <span className="text-dark-300 ml-1 font-mono">
                      v{baseInfo.panel.latest_version}
                    </span>
                  </span>
                )}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <AnimatePresence>
              {updateResults.panel && (
                <motion.div
                  className={`flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg ${
                    updateResults.panel.success
                      ? 'text-success bg-success/10'
                      : 'text-danger bg-danger/10'
                  }`}
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.8 }}
                >
                  {updatingPanel ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : updateResults.panel.success ? (
                    <CheckCircle2 className="w-4 h-4" />
                  ) : (
                    <XCircle className="w-4 h-4" />
                  )}
                  <span className="max-w-[200px] truncate">{updateResults.panel.message}</span>
                </motion.div>
              )}
            </AnimatePresence>

            {baseInfo?.panel.update_available && !updatingPanel && (
              <motion.span
                className="px-3 py-1 text-xs font-medium bg-accent-500/20 text-accent-400 rounded-full"
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
              >
                {t('updates.update_available')}
              </motion.span>
            )}

            {!baseInfo?.panel.update_available && !updatingPanel && (
              <span className="flex items-center gap-1.5 text-sm text-success">
                <Check className="w-4 h-4" />
                {t('updates.up_to_date')}
              </span>
            )}

            <motion.button
              onClick={handleUpdatePanel}
              disabled={updatingPanel || (!baseInfo?.panel.update_available && !isDevChannel)}
              className="btn btn-primary"
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              {updatingPanel ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Download className="w-4 h-4" />
              )}
              {t('updates.update_panel')}
            </motion.button>
          </div>
        </div>
      </motion.div>

      {/* Nodes Section */}
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
        <div className="flex items-center justify-between gap-3 flex-wrap mb-4">
          <div>
            <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
              <ServerIcon className="w-5 h-5 text-accent-500" />
              {t('updates.nodes')}
              <span className="text-dark-500 font-normal text-sm">
                ({loadedNodes.length})
              </span>
            </h2>
            {baseInfo?.node.latest_version && (
              <p className="text-sm text-dark-500 mt-1 ml-7">
                {t('updates.latest')}:
                <span className="text-dark-300 ml-1 font-mono">
                  v{baseInfo.node.latest_version}
                </span>
              </p>
            )}
          </div>

          <div className="flex items-center gap-3">
            {loadedNodes.length > 0 && (
              <label className="flex items-center gap-2 text-sm text-dark-400 cursor-pointer select-none">
                <Checkbox
                  checked={allSelected}
                  indeterminate={selectedNodes.length > 0 && !allSelected}
                  onChange={() => setSelected(allSelected ? new Set() : new Set(loadedNodes.map(n => n.id)))}
                />
                {t('updates.select_all')}
              </label>
            )}
            {updateAllCount > 0 && (
              <motion.button
                onClick={handleUpdateAllNodes}
                disabled={updatingAll}
                className="btn btn-secondary"
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
              >
                {updatingAll ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <ArrowUpCircle className="w-4 h-4" />
                )}
                {t('updates.update_all_nodes')} ({updateAllCount})
              </motion.button>
            )}
          </div>
        </div>

        <AnimatePresence>
          {selectedNodes.length > 0 && (
            <motion.div
              className="sticky top-2 z-20 mb-4 flex flex-wrap items-center justify-between gap-3 px-4 py-3 rounded-xl bg-dark-900/95 backdrop-blur border border-accent-500/30 shadow-lg"
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
            >
              <span className="text-sm text-dark-200">{t('updates.selected_count', { count: selectedNodes.length })}</span>
              <div className="flex items-center gap-2 flex-wrap">
                <Tooltip label={t('updates.update_selected_hint')} maxWidth={300}>
                  <button
                    onClick={handleUpdateSelected}
                    disabled={selectedAgentTargets.length === 0}
                    className="btn btn-secondary text-sm"
                  >
                    <Download className="w-4 h-4" />
                    {t('updates.update_selected', { count: selectedAgentTargets.length })}
                  </button>
                </Tooltip>
                <Tooltip label={t('imageDelivery.deliver_hint')} maxWidth={300}>
                  <button onClick={() => setBulkDeliverOpen(true)} className="btn btn-primary text-sm">
                    <Upload className="w-4 h-4" />
                    {t('updates.update_selected_ssh', { count: selectedNodes.length })}
                  </button>
                </Tooltip>
                <Tooltip label={t('updates.haproxy_selected_hint')} maxWidth={300}>
                  <button
                    onClick={() => setHaproxyModal({ targets: selectedHaproxyTargets.map(toHAProxyTarget), jobId: null })}
                    disabled={selectedHaproxyTargets.length === 0}
                    className="btn btn-secondary text-sm"
                  >
                    <ArrowUpCircle className="w-4 h-4" />
                    {t('updates.haproxy_selected', { count: selectedHaproxyTargets.length })}
                  </button>
                </Tooltip>
                <Tooltip label={t('updates.proxy_selected_hint')} maxWidth={300}>
                  <button
                    onClick={() => setProxyModal(selectedProxyTargets.map(n => ({ id: n.id, name: n.name })))}
                    disabled={selectedProxyTargets.length === 0}
                    className="btn btn-secondary text-sm"
                  >
                    <Globe className="w-4 h-4" />
                    {t('updates.proxy_selected', { count: selectedProxyTargets.length })}
                  </button>
                </Tooltip>
                <Tooltip label={t('updates.clear_selection')}>
                  <button
                    onClick={() => setSelected(new Set())}
                    className="p-2 text-dark-400 hover:text-dark-200 transition rounded-lg hover:bg-dark-800/50"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </Tooltip>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {loadedNodes.length === 0 ? (
          <motion.div
            className="card text-center py-12"
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
          >
            <ServerIcon className="w-12 h-12 text-dark-600 mx-auto mb-3" />
            <p className="text-dark-400">{t('updates.no_nodes')}</p>
          </motion.div>
        ) : (
          <div className="space-y-4">
            {nodeGroups.folders.map(({ name, members }) => renderFolder(name, members))}
            {nodeGroups.unfoldered.length > 0 && (
              <div className={NODE_GRID_CLASS}>{nodeGroups.unfoldered.map(renderNodeCard)}</div>
            )}
          </div>
        )}
      </motion.div>

      {/* All Up To Date Card */}
      {baseInfo && allNodesLoaded && !baseInfo.panel.update_available && nodesNeedUpdate === 0 && (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
          className="card bg-success/5 border-success/20 mt-6"
        >
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-full bg-success/10 flex items-center justify-center">
              <CheckCircle2 className="w-5 h-5 text-success" />
            </div>
            <div>
              <p className="text-sm text-dark-200 font-medium">
                {t('updates.all_up_to_date')}
              </p>
              <p className="text-sm text-dark-500">
                {t('updates.all_up_to_date_desc')}
              </p>
            </div>
          </div>
        </motion.div>
      )}

      {/* Info Card */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="card bg-dark-800/30 border-dark-700/30 mt-6"
      >
        <div className="flex items-start gap-3">
          <AlertTriangle className="w-5 h-5 text-warning flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm text-dark-300 font-medium mb-1">
              {t('updates.warning_title')}
            </p>
            <p className="text-sm text-dark-500">
              {t('updates.warning_text')}
            </p>
          </div>
        </div>

        <div className="flex items-start gap-3 mt-4 pt-4 border-t border-dark-700/30">
          <Clock className="w-5 h-5 text-accent-400 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm text-dark-300 font-medium mb-1">
              {t('updates.duration_title')}
            </p>
            <p className="text-sm text-dark-500">
              {t('updates.duration_text')}
            </p>
          </div>
        </div>
      </motion.div>

      {deliverTarget && (
        <DeliverImageModal
          key={deliverTarget.id}
          serverId={deliverTarget.id}
          serverName={deliverTarget.name}
          jobId={deliveryJobs.get(deliverTarget.id)?.job_id ?? null}
          onStarted={fetchDeliveryJobs}
          onClose={() => setDeliverTarget(null)}
        />
      )}

      {proxyModal && (
        <DownloadProxyModal
          targets={proxyModal}
          onChanged={(ids) => {
            if (proxyModal.length > 1) setSelected(new Set())
            setTimeout(() => ids.forEach(fetchNodeVersion), PROXY_REFRESH_DELAY_MS)
          }}
          onClose={() => setProxyModal(null)}
        />
      )}

      {haproxyModal && (
        <HAProxyUpgradeModal
          targets={haproxyModal.targets}
          jobId={haproxyModal.jobId}
          onStarted={() => {
            if (haproxyModal.targets.length > 1) setSelected(new Set())
            fetchHaproxyJobs()
          }}
          onClose={() => setHaproxyModal(null)}
        />
      )}

      {bulkDeliverOpen && (
        <BulkDeliverImageModal
          servers={selectedNodes.map(n => ({ id: n.id, name: n.name, hasSshCreds: n.hasSshCreds }))}
          onStarted={handleBulkDeliveryStarted}
          onClose={() => setBulkDeliverOpen(false)}
        />
      )}
    </motion.div>
  )
}
