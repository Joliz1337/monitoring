import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import { DndContext, DragOverlay } from '@dnd-kit/core'
import { SortableContext, rectSortingStrategy, verticalListSortingStrategy } from '@dnd-kit/sortable'
import {
  Server as ServerIcon, 
  LayoutGrid, 
  List,
  Plus,
  Activity,
  Wifi,
  WifiOff,
  Zap,
  Database,
  Minus,
  Equal,
  AlignJustify,
  Grid3x3,
  Square,
  PowerOff,
  FolderPlus,
  X,
  Search,
  type LucideIcon,
} from 'lucide-react'
import { useNavigate, useParams } from 'react-router-dom'
import { useServersStore, type ServerWithMetrics } from '../stores/serversStore'
import { useSettingsStore } from '../stores/settingsStore'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { useFolderBoard, FOLDER_SORTABLE_PREFIX } from '../hooks/useFolderBoard'
import { useCollapsedFolders } from '../hooks/useCollapsedFolders'
import ServerCard, { ServerCardOverlay } from '../components/Dashboard/ServerCard'
import FleetSummary from '../components/Dashboard/FleetSummary'
import { FolderLoadBadges, FolderStatusCounts } from '../components/Dashboard/FolderStats'
import { SortableFolder, UnfolderDropZone, FolderDragPreview } from '../components/folders/SortableFolder'
import { FolderDialogs } from '../components/folders/FolderDialogs'
import { ServerCardSkeleton } from '../components/ui/Skeleton'
import { Tooltip } from '../components/ui/Tooltip'
import { useTranslation } from 'react-i18next'
import { FAQIcon } from '../components/FAQ'
import { collectFolders, groupByFolder } from '../utils/folders'

const COLLAPSED_KEY = 'dashboard_collapsed_folders'

type StatusFilter = 'online' | 'offline' | 'disabled'

const STATUS_FILTERS: { key: StatusFilter; Icon: LucideIcon; text: string; active: string }[] = [
  { key: 'online', Icon: Wifi, text: 'text-success', active: 'bg-success/15 ring-1 ring-success/40' },
  { key: 'offline', Icon: WifiOff, text: 'text-danger', active: 'bg-danger/15 ring-1 ring-danger/40' },
  { key: 'disabled', Icon: PowerOff, text: 'text-dark-500', active: 'bg-dark-700/60 ring-1 ring-dark-500/50' },
]

const NO_SERVERS: ServerWithMetrics[] = []

const matchesSearch = (server: ServerWithMetrics, query: string): boolean =>
  server.name.toLowerCase().includes(query) || server.url.toLowerCase().includes(query)

export default function Dashboard() {
  const { uid } = useParams()
  const navigate = useNavigate()
  const { servers, fetchServersWithMetrics, isLoading } = useServersStore()
  const { refreshInterval, compactView, setCompactView, detailLevel, cardScale, setDetailLevel, setCardScale } = useSettingsStore()
  const { t } = useTranslation()

  const initialLoadDone = useRef(false)
  const board = useFolderBoard(servers)
  const { isDraggingRef } = board
  const [collapsed, toggleCollapsed] = useCollapsedFolders(COLLAPSED_KEY)
  const [searchQuery, setSearchQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<StatusFilter | null>(null)

  useEffect(() => {
    fetchServersWithMetrics().then(() => { initialLoadDone.current = true })
  }, [fetchServersWithMetrics])
  
  // На больших флотах поллить чаще, чем собираются метрики (~10с), бессмысленно —
  // поднимаем минимальный интервал, чтобы не гонять тяжёлый ответ зря.
  const effectiveInterval = useMemo(() => {
    const count = servers.length
    const floorSec = count > 300 ? 15 : count > 120 ? 10 : 0
    return Math.max(refreshInterval, floorSec) * 1000
  }, [servers.length, refreshInterval])

  // Во время drag поллинг пропускаем: замена массива серверов меняет высоты карточек
  // и лейаут, а dnd-kit меряет ректы droppable-зон на старте drag — коллизии уезжают
  const refreshServers = useCallback(async () => {
    if (isDraggingRef.current) return
    await fetchServersWithMetrics()
  }, [fetchServersWithMetrics, isDraggingRef])

  const { isPageVisible } = useAutoRefresh(
    refreshServers,
    { immediate: false, pauseWhenHidden: true, refreshOnVisible: true, customInterval: effectiveInterval }
  )

  const { displayedServers } = board

  const { activeServers, disabledServers, statusCounts } = useMemo(() => {
    const active = displayedServers.filter(s => s.is_active)
    const disabled = displayedServers.filter(s => !s.is_active)
    return {
      activeServers: active,
      disabledServers: disabled,
      statusCounts: {
        online: active.filter(s => s.status === 'online').length,
        offline: active.filter(s => s.status === 'offline').length,
        disabled: disabled.length,
      } satisfies Record<StatusFilter, number>,
    }
  }, [displayedServers])

  const normalizedQuery = searchQuery.toLowerCase().trim()
  const isSearching = normalizedQuery.length > 0
  const isFiltering = isSearching || statusFilter !== null

  const toggleStatusFilter = (filter: StatusFilter) =>
    setStatusFilter(prev => (prev === filter ? null : filter))

  // Отключённые серверы на дашборде видны только через свой фильтр
  const statusServers = useMemo(() => {
    if (statusFilter === 'disabled') return disabledServers
    if (statusFilter) return activeServers.filter(s => s.status === statusFilter)
    return activeServers
  }, [activeServers, disabledServers, statusFilter])

  const visibleServers = useMemo(
    () => (isSearching ? statusServers.filter(s => matchesSearch(s, normalizedQuery)) : statusServers),
    [statusServers, isSearching, normalizedQuery],
  )

  // При поиске и фильтре пустые папки не показываем — в них нечего искать
  const folders = useMemo(
    () => collectFolders(visibleServers, isFiltering ? [] : board.emptyFolders, board.folderOrder),
    [visibleServers, isFiltering, board.emptyFolders, board.folderOrder],
  )

  const folderSortableIds = useMemo(
    () => folders.map(f => `${FOLDER_SORTABLE_PREFIX}${f}`),
    [folders]
  )

  const grouped = useMemo(() => groupByFolder(visibleServers), [visibleServers])

  // Бейджи папки описывают её целиком, как счётчики в шапке: поиск и фильтр
  // меняют только показанные карточки
  const activeGrouped = useMemo(
    () => (visibleServers === activeServers ? grouped : groupByFolder(activeServers)),
    [visibleServers, activeServers, grouped],
  )

  const activeServer = board.activeServerId === null
    ? null
    : activeServers.find(s => s.id === board.activeServerId)
  const { activeFolderName } = board

  const subtitle = activeServers.length === 1 
    ? t('dashboard.subtitle_one', { count: activeServers.length })
    : t('dashboard.subtitle_other', { count: activeServers.length })

  const gridClass = compactView 
    ? 'space-y-3' 
    : cardScale === 'small'
      ? 'grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4'
      : cardScale === 'large'
        ? 'grid grid-cols-1 lg:grid-cols-2 gap-6'
        : 'grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5'

  const unfolderedServers = grouped.get(null) || []

  return (
    <div className="animate-page-enter">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-8">
        <div>
          <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-3">
            <Activity className="w-7 h-7 text-accent-400" />
            {t('dashboard.title')}
            <FAQIcon screen="PAGE_DASHBOARD" />
          </h1>
          <div className="text-dark-400 mt-1 flex items-center gap-1">
            <span className="mr-1">{subtitle}</span>
            {STATUS_FILTERS.map(({ key, Icon, text, active }) => {
              const count = statusCounts[key]
              const isActive = statusFilter === key
              // Кнопка активного фильтра не пропадает, когда счётчик обнулился, — иначе его нечем снять
              if (key !== 'online' && count === 0 && !isActive) return null
              return (
                <Tooltip key={key} label={t(isActive ? 'dashboard.filter_reset' : `dashboard.filter_${key}`)}>
                  <button
                    onClick={() => toggleStatusFilter(key)}
                    aria-pressed={isActive}
                    className={`flex items-center gap-1.5 px-2 py-0.5 rounded-md transition-colors ${isActive ? active : 'hover:bg-dark-800/60'}`}
                  >
                    <Icon className={`w-3.5 h-3.5 ${text}`} />
                    <span className={text}>{count}</span>
                  </button>
                </Tooltip>
              )
            })}
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex items-center bg-dark-800/60 rounded-xl p-1 border border-dark-700/50">
            <Tooltip label={t('dashboard.grid_view')}>
              <button
                onClick={() => setCompactView(false)}
                className={`btn-scale p-2.5 rounded-lg transition-colors ${!compactView ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' : 'text-dark-400 hover:text-dark-200'}`}
              >
                <LayoutGrid className="w-4 h-4" />
              </button>
            </Tooltip>
            <Tooltip label={t('dashboard.list_view')}>
              <button
                onClick={() => setCompactView(true)}
                className={`btn-scale p-2.5 rounded-lg transition-colors ${compactView ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' : 'text-dark-400 hover:text-dark-200'}`}
              >
                <List className="w-4 h-4" />
              </button>
            </Tooltip>
          </div>

          {!compactView && (
            <div className="hidden md:flex items-center bg-dark-800/60 rounded-xl p-1 border border-dark-700/50">
              {(['minimal', 'standard', 'detailed'] as const).map(level => (
                <Tooltip key={level} label={t(`dashboard.detail_${level}`)}>
                  <button
                    onClick={() => setDetailLevel(level)}
                    className={`btn-scale p-2.5 rounded-lg transition-colors ${detailLevel === level ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' : 'text-dark-400 hover:text-dark-200'}`}
                  >
                    {level === 'minimal' ? <Minus className="w-4 h-4" /> : level === 'standard' ? <Equal className="w-4 h-4" /> : <AlignJustify className="w-4 h-4" />}
                  </button>
                </Tooltip>
              ))}
            </div>
          )}

          {!compactView && (
            <div className="hidden lg:flex items-center bg-dark-800/60 rounded-xl p-1 border border-dark-700/50">
              {(['small', 'medium', 'large'] as const).map(scale => (
                <Tooltip key={scale} label={t(`dashboard.scale_${scale}`)}>
                  <button
                    onClick={() => setCardScale(scale)}
                    className={`btn-scale p-2.5 rounded-lg transition-colors ${cardScale === scale ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' : 'text-dark-400 hover:text-dark-200'}`}
                  >
                    {scale === 'small' ? <Grid3x3 className="w-4 h-4" /> : scale === 'medium' ? <LayoutGrid className="w-4 h-4" /> : <Square className="w-4 h-4" />}
                  </button>
                </Tooltip>
              ))}
            </div>
          )}

          <div className="text-xs text-dark-500 hidden sm:flex items-center gap-1.5 bg-dark-800/40 px-3 py-2 rounded-lg live-mode-pulse">
            {isPageVisible ? (
              <>
                <Zap className="w-3.5 h-3.5 text-accent-500" />
                <span className="text-accent-400">{t('dashboard.live_mode')}</span>
                <span className="text-dark-600">•</span>
                <span>{refreshInterval}s</span>
              </>
            ) : (
              <>
                <Database className="w-3.5 h-3.5 text-dark-500" />
                <span>{t('dashboard.background_mode')}</span>
              </>
            )}
          </div>

          <FAQIcon screen="DASHBOARD_FOLDERS" size="sm" />

          <Tooltip label={t('dashboard.create_folder')}>
            <button
              onClick={board.openCreateFolder}
              className="btn-scale p-2.5 bg-dark-800/60 rounded-xl border border-dark-700/50 text-dark-400 hover:text-white transition-colors"
            >
              <FolderPlus className="w-4 h-4" />
            </button>
          </Tooltip>

          <button
            onClick={() => navigate(`/${uid}/servers`)}
            className="btn btn-primary"
          >
            <Plus className="w-4 h-4" />
            <span className="hidden sm:inline">{t('common.add_server')}</span>
          </button>
        </div>
      </div>

      {/* Fleet summary */}
      <FleetSummary servers={servers} />

      {/* Search */}
      {(activeServers.length > 0 || statusFilter !== null) && (
        <div className="flex items-center gap-3 mb-4">
          <div className="flex-1 flex items-center gap-2 bg-dark-800/50 border border-dark-700/50 rounded-xl px-3 py-2">
            <Search className="w-4 h-4 text-dark-400 shrink-0" />
            <input
              type="text"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              placeholder={t('dashboard.search_placeholder')}
              className="bg-transparent text-sm text-dark-100 placeholder-dark-500 outline-none w-full"
            />
            {searchQuery && (
              <button
                onClick={() => setSearchQuery('')}
                className="text-dark-500 hover:text-dark-300 transition-colors shrink-0"
              >
                <X className="w-4 h-4" />
              </button>
            )}
          </div>
          {isFiltering && (
            <span className="text-xs text-dark-500 hidden sm:inline">{t('dashboard.filter_drag_hint')}</span>
          )}
        </div>
      )}

      {/* Content */}
      {isLoading && servers.length === 0 ? (
        <div className={`${gridClass} fade-in`} key="loading">
          {Array.from({ length: 6 }).map((_, i) => (
            <ServerCardSkeleton key={i} compact={compactView} />
          ))}
        </div>
      ) : activeServers.length === 0 && statusFilter === null ? (
        <div className="card text-center py-20 fade-in" key="empty">
          <div>
            <div className="icon-float inline-block">
              <ServerIcon className="w-20 h-20 text-dark-600 mx-auto mb-6" />
            </div>
            <h2 className="text-xl font-semibold text-dark-200 mb-2">{t('dashboard.no_servers')}</h2>
            <p className="text-dark-400 mb-8">{t('dashboard.add_first')}</p>
            <button onClick={() => navigate(`/${uid}/servers`)} className="btn btn-primary mx-auto btn-scale">
              <Plus className="w-4 h-4" />
              {t('common.add_server')}
            </button>
          </div>
        </div>
      ) : isFiltering && visibleServers.length === 0 ? (
        <div className="card text-center py-16 fade-in" key="no-results">
          <Search className="w-12 h-12 text-dark-600 mx-auto mb-3" />
          <p className="text-dark-400">{t('common.no_results')}</p>
        </div>
      ) : (
        <DndContext {...board.dndContextProps(folders, !isFiltering)}>
          <div className="space-y-6 fade-in" key="servers">
              {/* Sortable folder list */}
              <SortableContext items={folderSortableIds} strategy={verticalListSortingStrategy}>
                {folders.map(folderName => {
                  const folderServers = grouped.get(folderName) || []
                  const folderActiveServers = activeGrouped.get(folderName) ?? NO_SERVERS
                  return (
                    <SortableFolder
                      key={folderName}
                      name={folderName}
                      collapsed={!isFiltering && collapsed.has(folderName)}
                      isDropTarget={board.isDropTarget(folderName)}
                      onToggle={() => toggleCollapsed(folderName)}
                      onRename={() => board.openRenameFolder(folderName)}
                      onDelete={() => board.deleteFolder(folderName)}
                      badges={
                        <>
                          <FolderStatusCounts servers={folderActiveServers} />
                          <FolderLoadBadges servers={folderActiveServers} className="hidden sm:flex flex-shrink-0" />
                        </>
                      }
                      // На телефоне рядом с именем места нет — загрузка уходит второй строкой
                      footer={<FolderLoadBadges servers={folderActiveServers} className="sm:hidden basis-full pl-7" />}
                    >
                      {folderServers.length > 0 ? (
                        <SortableContext items={folderServers.map(s => s.id)} strategy={rectSortingStrategy}>
                          <div className={gridClass}>
                            {folderServers.map((server, index) => (
                              <ServerCard key={server.id} server={server} compact={compactView} detailLevel={detailLevel} index={index} />
                            ))}
                          </div>
                        </SortableContext>
                      ) : (
                        <div className="py-6 text-center text-dark-500 text-xs">{t('dashboard.no_servers')}</div>
                      )}
                    </SortableFolder>
                  )
                })}
              </SortableContext>

              {/* Servers without folder */}
              <UnfolderDropZone isOver={board.isDropTarget(null)} hasServers={unfolderedServers.length > 0} hasFolders={folders.length > 0}>
                <SortableContext items={unfolderedServers.map(s => s.id)} strategy={rectSortingStrategy}>
                  <div className={gridClass}>
                    {unfolderedServers.map((server, index) => (
                      <ServerCard key={server.id} server={server} compact={compactView} detailLevel={detailLevel} index={index} />
                    ))}
                  </div>
                </SortableContext>
            </UnfolderDropZone>
          </div>

          <DragOverlay>
            {activeServer && (
              <div className="opacity-90">
                <ServerCardOverlay server={activeServer} compact={compactView} detailLevel={detailLevel} index={0} />
              </div>
            )}
            {activeFolderName && (
              <FolderDragPreview name={activeFolderName} count={(grouped.get(activeFolderName) || []).length} />
            )}
          </DragOverlay>
        </DndContext>
      )}

      <FolderDialogs board={board} existingFolders={folders} />
    </div>
  )
}
