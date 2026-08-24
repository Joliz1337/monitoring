import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  FlaskConical, Link2, FileJson, Rss, Bookmark, History, Play, Square,
  Loader2, Download, Globe, Search, ChevronDown, ChevronUp, Cpu, Save,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  serversApi,
  xrayTestApi,
  xrayTestExportUrl,
  type ServerWithMetrics,
  type XrayTestConfigPreview,
  type XrayTestCoreInfo,
  type XrayTestParseResult,
  type XrayTestSniSet,
  type XrayTestSource,
  type XrayTestSubscriptionProfile,
  type XrayTestClient,
} from '../api/client'
import { FAQIcon } from '../components/FAQ'
import { Checkbox } from '../components/ui/Checkbox'
import { ResultsTable } from '../components/xraytest/ResultsTable'
import { ProfilesTab } from '../components/xraytest/ProfilesTab'
import { HistoryTab } from '../components/xraytest/HistoryTab'
import { CoresTab } from '../components/xraytest/CoresTab'
import { LocationPicker } from '../components/xraytest/LocationPicker'
import { extractError, useTestRun } from '../components/xraytest/useTestRun'

type TabType = 'links' | 'json' | 'subscription' | 'profiles' | 'history' | 'cores'


export default function XrayTest() {
  const { t } = useTranslation()
  const [activeTab, setActiveTab] = useState<TabType>('links')

  const tabs: { id: TabType; label: string; icon: typeof Link2 }[] = [
    { id: 'links', label: t('xray_test.tab_links'), icon: Link2 },
    { id: 'json', label: t('xray_test.tab_json'), icon: FileJson },
    { id: 'subscription', label: t('xray_test.tab_subscription'), icon: Rss },
    { id: 'profiles', label: t('xray_test.tab_profiles'), icon: Bookmark },
    { id: 'history', label: t('xray_test.tab_history'), icon: History },
    { id: 'cores', label: t('xray_test.tab_cores'), icon: Cpu },
  ]

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
      <motion.div
        className="flex items-center justify-between"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-500/20 to-blue-500/20 flex items-center justify-center">
            <FlaskConical className="w-5 h-5 text-accent-400" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-dark-50 flex items-center gap-2">
              {t('xray_test.title')}
              <FAQIcon screen="PAGE_XRAY_TEST" />
            </h1>
            <p className="text-sm text-dark-400">{t('xray_test.subtitle')}</p>
          </div>
        </div>
      </motion.div>

      <div className="flex gap-1 p-1 bg-dark-900/50 rounded-xl border border-dark-800/50 w-fit">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all duration-200 ${
              activeTab === tab.id
                ? 'bg-accent-500/15 text-accent-400 shadow-sm'
                : 'text-dark-400 hover:text-dark-200 hover:bg-dark-800/50'
            }`}
          >
            <tab.icon className="w-4 h-4" />
            {tab.label}
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        {activeTab === 'profiles' ? (
          <ProfilesTab key="profiles" />
        ) : activeTab === 'history' ? (
          <HistoryTab key="history" />
        ) : activeTab === 'cores' ? (
          <CoresTab key="cores" />
        ) : (
          <TesterTab key={activeTab} source={activeTab} />
        )}
      </AnimatePresence>
    </motion.div>
  )
}

export function Section({ title, icon, children, right }: {
  title: string
  icon?: ReactNode
  children: ReactNode
  right?: ReactNode
}) {
  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} className="card">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2 text-dark-200 text-sm font-medium">
          {icon}
          {title}
        </div>
        {right}
      </div>
      {children}
    </motion.div>
  )
}

function TesterTab({ source }: { source: XrayTestSource }) {
  const { t } = useTranslation()
  const run = useTestRun()

  const [payload, setPayload] = useState('')
  const [client, setClient] = useState('')
  const [parsed, setParsed] = useState<XrayTestParseResult | null>(null)
  const [parsing, setParsing] = useState(false)
  const [selected, setSelected] = useState<Set<number>>(new Set())

  const [sniText, setSniText] = useState('')
  const [syncHost, setSyncHost] = useState(false)
  const [locations, setLocations] = useState<string[]>(['panel'])
  const [concurrency, setConcurrency] = useState(4)
  const [fullMode, setFullMode] = useState(true)
  const [tlsInspect, setTlsInspect] = useState(true)
  const [measureSpeed, setMeasureSpeed] = useState(false)
  const [showLog, setShowLog] = useState(false)

  const [servers, setServers] = useState<ServerWithMetrics[]>([])
  const [cores, setCores] = useState<XrayTestCoreInfo[]>([])
  const [sniSets, setSniSets] = useState<XrayTestSniSet[]>([])
  const [clients, setClients] = useState<XrayTestClient[]>([])
  const [sources, setSources] = useState<XrayTestSubscriptionProfile[]>([])
  const [activeSource, setActiveSource] = useState<XrayTestSubscriptionProfile | null>(null)

  const loadSources = useCallback(() => {
    xrayTestApi.subscriptions()
      .then(({ data }) => setSources(data.profiles))
      .catch(() => setSources([]))
  }, [])

  useEffect(() => {
    serversApi.list().then(({ data }) => setServers(data.servers)).catch(() => setServers([]))
    xrayTestApi.cores().then(({ data }) => setCores(data.cores)).catch(() => setCores([]))
    xrayTestApi.sniSets().then(({ data }) => setSniSets(data.profiles)).catch(() => setSniSets([]))
    xrayTestApi.clients().then(({ data }) => {
      setClients(data.clients)
      setClient(current => current || data.default)
    }).catch(() => setClients([]))
    loadSources()
  }, [loadSources])

  // Источник хранит либо адрес подписки, либо список ссылок — на вкладку JSON
  // подставлять нечего
  const sourceKind = source === 'subscription' ? 'url' : 'links'
  const ownSources = useMemo(
    () => (source === 'json' ? [] : sources.filter(item => item.kind === sourceKind)),
    [sources, source, sourceKind],
  )

  const applySource = (profile: XrayTestSubscriptionProfile) => {
    setPayload(profile.payload)
    setActiveSource(profile)
    setParsed(null)
    if (profile.client) setClient(profile.client)
  }

  const saveCurrentSource = async () => {
    const name = window.prompt(t('xray_test.profile_name'))
    if (!name?.trim()) return
    try {
      await xrayTestApi.createSubscription({
        name: name.trim(),
        kind: sourceKind,
        payload: payload.trim(),
        client: source === 'subscription' ? client : null,
      })
      toast.success(t('xray_test.profile_saved'))
      loadSources()
    } catch (error) {
      toast.error(extractError(error))
    }
  }

  const sniList = useMemo(
    () => sniText.split(/[\s,;]+/).map(item => item.trim()).filter(Boolean),
    [sniText],
  )

  const supported = useMemo(
    () => (parsed?.configs || []).filter(config => !config.unsupported),
    [parsed],
  )

  const totalCells = useMemo(() => {
    const chosen = selected.size || supported.length
    return chosen * Math.max(1, sniList.length)
  }, [selected.size, supported.length, sniList.length])

  const handleParse = useCallback(async () => {
    if (!payload.trim()) return
    setParsing(true)
    try {
      const { data } = await xrayTestApi.parse({
        source,
        payload: payload.trim(),
        client: source === 'subscription' ? client : null,
        profile_id: activeSource?.id ?? null,
      })
      setParsed(data)
      setSelected(new Set(data.configs.filter(c => !c.unsupported).map(c => c.index)))
      if (!data.configs.length) toast.error(t('xray_test.nothing_parsed'))
    } catch (error) {
      setParsed(null)
      toast.error(extractError(error))
    } finally {
      setParsing(false)
    }
  }, [payload, source, client, t])

  const handleRun = useCallback(async () => {
    if (!parsed) {
      toast.error(t('xray_test.parse_first'))
      return
    }
    const indices = selected.size ? [...selected] : supported.map(c => c.index)
    if (!indices.length) {
      toast.error(t('xray_test.select_configs'))
      return
    }

    await run.start({
      source,
      payload: payload.trim(),
      client: source === 'subscription' ? client : null,
      source_name: activeSource?.name ?? null,
      profile_id: activeSource?.id ?? null,
      selected: indices,
      sni_list: sniList,
      sync_transport_host: syncHost,
      locations,
      concurrency,
      full: fullMode,
      tls_inspect: tlsInspect,
      measure_speed: measureSpeed,
    })
  }, [parsed, selected, supported, run, source, payload, client, activeSource, sniList,
      syncHost, locations, concurrency, fullMode, tlsInspect, measureSpeed, t])

  const toggleConfig = (index: number) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  const missingCore = cores.find(core => !core.ready && core.resolved)
  const progress = run.total ? Math.round((run.cells.length / run.total) * 100) : 0

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      className="space-y-4"
    >
      <Section
        title={t(`xray_test.input_${source}`)}
        icon={source === 'links' ? <Link2 className="w-4 h-4" /> : source === 'json'
          ? <FileJson className="w-4 h-4" /> : <Rss className="w-4 h-4" />}
        right={
          <div className="flex items-center gap-2">
            {source !== 'json' && payload.trim() && (
              <button className="btn btn-ghost text-xs" onClick={saveCurrentSource}>
                <Save className="w-3.5 h-3.5" />
                {t('xray_test.save_source')}
              </button>
            )}
            <button className="btn btn-secondary text-xs" onClick={handleParse} disabled={parsing || !payload.trim()}>
              {parsing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Search className="w-3.5 h-3.5" />}
              {t('xray_test.parse')}
            </button>
          </div>
        }
      >
        {!!ownSources.length && (
          <div className="flex flex-wrap items-center gap-1.5 mb-3">
            <span className="text-[11px] text-dark-500 mr-1">{t('xray_test.saved_pick')}</span>
            {ownSources.map(profile => (
              <button
                key={profile.id}
                onClick={() => applySource(profile)}
                className={`px-2 py-0.5 rounded text-[11px] border transition-colors ${
                  activeSource?.id === profile.id
                    ? 'border-accent-500/40 bg-accent-500/15 text-accent-400'
                    : 'border-dark-800/60 bg-dark-800/40 text-dark-300 hover:text-accent-400'
                }`}
              >
                {profile.name}
                {profile.last_count ? (
                  <span className="text-dark-500 ml-1">· {profile.last_count}</span>
                ) : null}
              </button>
            ))}
          </div>
        )}

        {source === 'subscription' ? (
          <div className="space-y-3">
            <input
              className="input w-full"
              placeholder="https://example.com/sub/token"
              value={payload}
              onChange={event => setPayload(event.target.value)}
            />
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-dark-400">{t('xray_test.client')}</span>
              <select
                className="input py-1 text-xs"
                value={client}
                onChange={event => setClient(event.target.value)}
              >
                {clients.map(item => (
                  <option key={item.id} value={item.id}>
                    {item.title}{item.sends_hwid ? ' · HWID' : ''}
                  </option>
                ))}
              </select>
              <span className="text-dark-500">
                {clients.find(item => item.id === client)?.sends_hwid
                  ? t('xray_test.client_hwid_on')
                  : t('xray_test.client_hwid_off')}
              </span>
            </div>
          </div>
        ) : (
          <textarea
            className="input w-full font-mono text-xs h-40"
            placeholder={source === 'links' ? 'vless://…\ntrojan://…\nhysteria2://…' : '{ "outbounds": [ … ] }'}
            value={payload}
            onChange={event => setPayload(event.target.value)}
          />
        )}

        {parsed && (
          <div className="mt-4 space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-dark-300">
                {t('xray_test.parsed_count', { count: parsed.configs.length })}
              </span>
              {parsed.format && (
                <span className="px-2 py-0.5 rounded bg-dark-800/60 text-dark-300">
                  {t('xray_test.format')}: {parsed.format}
                </span>
              )}
              {!!parsed.dropped_sections.length && (
                <span className="px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">
                  {t('xray_test.dropped')}: {parsed.dropped_sections.join(', ')}
                </span>
              )}
              <button
                className="ml-auto text-accent-400 hover:text-accent-300"
                onClick={() => setSelected(new Set(
                  selected.size === supported.length ? [] : supported.map(c => c.index),
                ))}
              >
                {selected.size === supported.length ? t('xray_test.unselect_all') : t('xray_test.select_all')}
              </button>
            </div>

            <div className="max-h-64 overflow-auto rounded-lg border border-dark-800/60 divide-y divide-dark-800/40">
              {parsed.configs.map(config => (
                <ConfigRow
                  key={config.index}
                  config={config}
                  checked={selected.has(config.index)}
                  onToggle={() => toggleConfig(config.index)}
                />
              ))}
            </div>

            {!!parsed.errors.length && (
              <details className="text-xs">
                <summary className="cursor-pointer text-amber-400">
                  {t('xray_test.line_errors', { count: parsed.errors.length })}
                </summary>
                <div className="mt-2 space-y-1 max-h-32 overflow-auto">
                  {parsed.errors.map(error => (
                    <div key={error.line} className="text-dark-400 font-mono text-[11px]">
                      {t('xray_test.line')} {error.line}: {error.reason} — {error.preview}
                    </div>
                  ))}
                </div>
              </details>
            )}
          </div>
        )}
      </Section>

      <Section title={t('xray_test.options')} icon={<FlaskConical className="w-4 h-4" />}>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-3">
            <div>
              <label className="block text-xs text-dark-400 mb-1.5">{t('xray_test.where')}</label>
              <LocationPicker servers={servers} value={locations} onChange={setLocations} />
            </div>

            <div>
              <label className="block text-xs text-dark-400 mb-1.5">
                {t('xray_test.multi_sni')}
              </label>
              <textarea
                className="input w-full font-mono text-xs h-20"
                placeholder="www.microsoft.com&#10;www.apple.com"
                value={sniText}
                onChange={event => setSniText(event.target.value)}
              />
              {!!sniSets.length && (
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {sniSets.map(set => (
                    <button
                      key={set.id}
                      onClick={() => setSniText(set.sni_list.join('\n'))}
                      className="px-2 py-0.5 rounded bg-dark-800/60 text-dark-300 hover:text-accent-400 text-[11px]"
                    >
                      {set.name}
                    </button>
                  ))}
                </div>
              )}
              <label className="flex items-start gap-2 mt-2 text-xs text-dark-300 cursor-pointer select-none">
                <span className="mt-0.5">
                  <Checkbox checked={syncHost} onChange={event => setSyncHost(event.target.checked)} />
                </span>
                <span>
                  {t('xray_test.sync_host')}
                  <span className="block text-dark-500 text-[11px]">
                    {t('xray_test.sync_host_hint')}
                  </span>
                </span>
              </label>
            </div>
          </div>

          <div className="space-y-3">
            <div>
              <label className="block text-xs text-dark-400 mb-1.5">
                {t('xray_test.concurrency')}: {concurrency}
              </label>
              <input
                type="range"
                min={1}
                max={8}
                value={concurrency}
                onChange={event => setConcurrency(Number(event.target.value))}
                className="w-full accent-accent-500"
              />
            </div>

            <div className="space-y-2 text-xs text-dark-300">
              <Toggle
                checked={fullMode}
                onChange={setFullMode}
                label={t('xray_test.full_mode')}
                hint={t('xray_test.full_mode_hint')}
              />
              <Toggle
                checked={tlsInspect}
                onChange={setTlsInspect}
                label={t('xray_test.tls_inspect')}
                hint={t('xray_test.tls_inspect_hint')}
              />
              <Toggle
                checked={measureSpeed}
                onChange={setMeasureSpeed}
                label={t('xray_test.measure_speed')}
                hint={t('xray_test.measure_speed_hint')}
              />
            </div>

            {missingCore && (
              <div className="text-[11px] text-dark-400">
                {t('xray_test.core_will_download', { core: missingCore.core, version: missingCore.resolved })}
              </div>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3 mt-4 pt-4 border-t border-dark-800/50">
          {run.running ? (
            <button className="btn btn-danger" onClick={run.cancel}>
              <Square className="w-4 h-4" />
              {t('xray_test.stop')}
            </button>
          ) : (
            <button className="btn btn-primary" onClick={handleRun} disabled={!parsed}>
              <Play className="w-4 h-4" />
              {t('xray_test.start')}
            </button>
          )}
          <span className="text-xs text-dark-400">
            {t('xray_test.will_check', { count: totalCells })}
          </span>

          {run.running && (
            <div className="flex items-center gap-2 ml-auto text-xs text-dark-300">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              {run.cells.length} / {run.total} ({progress}%)
            </div>
          )}
        </div>

        {run.total > 0 && (
          <div className="mt-3 h-1.5 rounded-full bg-dark-800 overflow-hidden">
            <div
              className="h-full bg-accent-500 transition-all duration-300"
              style={{ width: `${progress}%` }}
            />
          </div>
        )}
      </Section>

      {(run.cells.length > 0 || run.summary) && (
        <Section
          title={t('xray_test.results')}
          icon={<Globe className="w-4 h-4" />}
          right={
            run.jobId && !run.running ? (
              <div className="flex items-center gap-2">
                <ExportButton jobId={run.jobId} fmt="links" label={t('xray_test.export_links')} />
                <ExportButton jobId={run.jobId} fmt="subscription" label={t('xray_test.export_subscription')} />
                <ExportButton jobId={run.jobId} fmt="csv" label={t('xray_test.export_csv')} />
              </div>
            ) : undefined
          }
        >
          {run.summary && (
            <div className="flex flex-wrap gap-3 mb-4 text-xs">
              <Stat label={t('xray_test.verdict_ok')} value={run.summary.ok} tone="text-emerald-400" />
              <Stat label={t('xray_test.verdict_degraded')} value={run.summary.degraded} tone="text-amber-400" />
              <Stat label={t('xray_test.verdict_fail')} value={run.summary.fail} tone="text-red-400" />
            </div>
          )}
          <ResultsTable cells={run.cells} groupBySni={sniList.length > 1} />

          {!!run.log.length && (
            <div className="mt-4">
              <button
                className="flex items-center gap-1.5 text-xs text-dark-400 hover:text-dark-200"
                onClick={() => setShowLog(value => !value)}
              >
                {showLog ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                {t('xray_test.log')}
              </button>
              {showLog && (
                <pre className="mt-2 text-[11px] font-mono text-dark-400 bg-dark-950/60 rounded p-2 max-h-40 overflow-auto whitespace-pre-wrap">
                  {run.log.join('\n')}
                </pre>
              )}
            </div>
          )}
        </Section>
      )}
    </motion.div>
  )
}

function ConfigRow({ config, checked, onToggle }: {
  config: XrayTestConfigPreview
  checked: boolean
  onToggle: () => void
}) {
  const { t } = useTranslation()
  return (
    <label
      className={`flex items-center gap-3 px-3 py-2 text-xs cursor-pointer select-none transition-colors ${
        config.unsupported ? 'opacity-60' : 'hover:bg-dark-800/30'
      } ${checked && !config.unsupported ? 'bg-accent-500/[0.06]' : ''}`}
    >
      <Checkbox checked={checked} disabled={!!config.unsupported} onChange={onToggle} />
      <span className="flex-1 min-w-0">
        <span className="block text-dark-200 truncate">
          {config.remark || `${config.address}:${config.port}`}
        </span>
        <span className="block text-dark-500 text-[11px] font-mono truncate">
          {config.address}:{config.port} · {config.protocol} · {config.transport} · {config.security}
          {config.flow ? ` · ${config.flow}` : ''}
          {config.sni ? ` · ${config.sni}` : ''}
        </span>
      </span>
      {config.unsupported ? (
        <span className="px-2 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/20 shrink-0">
          {t('xray_test.unsupported')}
        </span>
      ) : (
        <span className="px-2 py-0.5 rounded bg-dark-800/60 text-dark-300 shrink-0">{config.core}</span>
      )}
    </label>
  )
}

function Toggle({ checked, onChange, label, hint }: {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
  hint?: string
}) {
  return (
    <label className="flex items-start gap-2 cursor-pointer select-none">
      <span className="mt-0.5">
        <Checkbox checked={checked} onChange={event => onChange(event.target.checked)} />
      </span>
      <span>
        {label}
        {hint && <span className="block text-dark-500 text-[11px]">{hint}</span>}
      </span>
    </label>
  )
}

function Stat({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="px-3 py-1.5 rounded-lg bg-dark-900/60 border border-dark-800/50">
      <span className="text-dark-400">{label}: </span>
      <span className={`font-semibold ${tone}`}>{value}</span>
    </div>
  )
}

function ExportButton({ jobId, fmt, label }: { jobId: string; fmt: string; label: string }) {
  return (
    <a
      href={xrayTestExportUrl(jobId, fmt, true)}
      target="_blank"
      rel="noreferrer"
      className="btn btn-secondary text-xs"
    >
      <Download className="w-3.5 h-3.5" />
      {label}
    </a>
  )
}
