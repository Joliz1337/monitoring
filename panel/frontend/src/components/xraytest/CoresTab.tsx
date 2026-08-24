import { useCallback, useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import {
  Cpu, Download, Loader2, RefreshCw, Trash2, Check, AlertTriangle, ShieldCheck,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  xrayTestApi,
  type XrayTestCoreInfo,
  type XrayTestCoreRelease,
} from '../../api/client'
import { Section } from '../../pages/XrayTest'
import { extractError } from './useTestRun'

const LATEST = 'latest'

export function CoresTab() {
  const [cores, setCores] = useState<XrayTestCoreInfo[]>([])
  const [arch, setArch] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const { data } = await xrayTestApi.cores()
      setCores(data.cores)
      setArch(data.arch)
    } catch (error) {
      toast.error(extractError(error))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <Loader2 className="w-6 h-6 animate-spin text-dark-500" />
      </div>
    )
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      className="space-y-4"
    >
      {cores.map(core => (
        <CoreCard key={core.core} info={core} arch={arch} onChanged={load} />
      ))}
    </motion.div>
  )
}

function CoreCard({ info, arch, onChanged }: {
  info: XrayTestCoreInfo
  arch: string
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const [releases, setReleases] = useState<XrayTestCoreRelease[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)

  const loadReleases = useCallback(async (refresh = false) => {
    setLoading(true)
    try {
      const { data } = await xrayTestApi.coreReleases(info.core, refresh)
      setReleases(data.releases)
    } catch (error) {
      toast.error(extractError(error))
    } finally {
      setLoading(false)
    }
  }, [info.core])

  useEffect(() => { loadReleases() }, [loadReleases])

  const choose = async (version: string) => {
    setBusy('select')
    try {
      await xrayTestApi.setCoreVersion(info.core, version)
      toast.success(t('xray_test.core_version_saved'))
      onChanged()
    } catch (error) {
      toast.error(extractError(error))
    } finally {
      setBusy(null)
    }
  }

  const download = async (version?: string) => {
    setBusy(version || 'download')
    try {
      const { data } = await xrayTestApi.downloadCore(info.core, version)
      toast.success(t('xray_test.core_downloaded', { version: data.version }))
      await Promise.all([loadReleases(), onChanged()])
    } catch (error) {
      toast.error(extractError(error))
    } finally {
      setBusy(null)
    }
  }

  const remove = async (version: string) => {
    setBusy(version)
    try {
      await xrayTestApi.deleteCoreVersion(info.core, version)
      await Promise.all([loadReleases(), onChanged()])
    } catch (error) {
      toast.error(extractError(error))
    } finally {
      setBusy(null)
    }
  }

  const latest = releases.find(item => item.available)

  return (
    <Section
      title={info.core}
      icon={<Cpu className="w-5 h-5" />}
      right={
        <div className="flex items-center gap-2">
          <span className="text-[13px] text-dark-500">{arch}</span>
          <button
            className="btn btn-secondary text-sm"
            onClick={() => loadReleases(true)}
            disabled={loading}
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            {t('xray_test.refresh_releases')}
          </button>
        </div>
      }
    >
      <div className="flex flex-wrap items-center gap-3 mb-4 text-sm">
        <StatusChip info={info} />
        {info.error && (
          <span className="flex items-center gap-1.5 text-amber-400">
            <AlertTriangle className="w-4 h-4" />
            {info.error}
          </span>
        )}
        {info.resolved && !info.ready && (
          <button
            className="btn btn-primary text-sm"
            onClick={() => download()}
            disabled={busy !== null}
          >
            {busy === 'download' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
            {t('xray_test.download_now', { version: info.resolved })}
          </button>
        )}
      </div>

      <div className="space-y-1.5">
        <VersionRow
          active={info.selected === LATEST}
          title={t('xray_test.always_latest')}
          subtitle={
            latest
              ? t('xray_test.always_latest_hint', { version: latest.version })
              : t('xray_test.always_latest_plain')
          }
          onSelect={() => choose(LATEST)}
          busy={busy === 'select'}
        />

        {loading && !releases.length ? (
          <div className="flex justify-center py-6">
            <Loader2 className="w-5 h-5 animate-spin text-dark-500" />
          </div>
        ) : (
          <div className="max-h-80 overflow-auto space-y-1.5 pr-1">
            {releases.map(release => (
              <VersionRow
                key={release.version}
                active={info.selected === release.version}
                disabled={!release.available}
                title={release.version}
                badges={
                  <>
                    {release.prerelease && (
                      <Badge tone="amber">{t('xray_test.prerelease')}</Badge>
                    )}
                    {release.version === info.pinned && (
                      <Badge tone="dark">{t('xray_test.verified')}</Badge>
                    )}
                    {release.installed && (
                      <Badge tone="emerald">{t('xray_test.installed')}</Badge>
                    )}
                    {!release.verifiable && (
                      <Badge tone="dark" icon={<ShieldCheck className="w-3.5 h-3.5" />}>
                        {t('xray_test.direct_only')}
                      </Badge>
                    )}
                  </>
                }
                subtitle={[
                  release.published_at ? release.published_at.slice(0, 10) : null,
                  release.size ? `${Math.round(release.size / 1024 / 1024)} MB` : null,
                ].filter(Boolean).join(' · ')}
                onSelect={() => choose(release.version)}
                busy={busy === 'select'}
                actions={
                  <>
                    {!release.installed && release.available && (
                      <button
                        className="text-dark-400 hover:text-accent-400 p-1"
                        title={t('xray_test.download')}
                        onClick={event => { event.stopPropagation(); download(release.version) }}
                        disabled={busy !== null}
                      >
                        {busy === release.version
                          ? <Loader2 className="w-4 h-4 animate-spin" />
                          : <Download className="w-4 h-4" />}
                      </button>
                    )}
                    {release.installed && (
                      <button
                        className="text-dark-500 hover:text-red-400 p-1"
                        title={t('xray_test.remove_version')}
                        onClick={event => { event.stopPropagation(); remove(release.version) }}
                        disabled={busy !== null}
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    )}
                  </>
                }
              />
            ))}
          </div>
        )}
      </div>
    </Section>
  )
}

function StatusChip({ info }: { info: XrayTestCoreInfo }) {
  const { t } = useTranslation()
  if (info.ready) {
    return (
      <span className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
        <Check className="w-4 h-4" />
        {t('xray_test.core_ready', { version: info.resolved })}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-dark-800/60 text-dark-300 border border-dark-700/60">
      {t('xray_test.core_not_downloaded')}
    </span>
  )
}

function Badge({ children, tone, icon }: {
  children: React.ReactNode
  tone: 'amber' | 'emerald' | 'dark'
  icon?: React.ReactNode
}) {
  const styles = {
    amber: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    emerald: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
    dark: 'bg-dark-800/70 text-dark-400 border-dark-700/60',
  }[tone]
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-1 rounded border text-xs ${styles}`}>
      {icon}
      {children}
    </span>
  )
}

function VersionRow({ active, disabled, title, subtitle, badges, actions, onSelect, busy }: {
  active: boolean
  disabled?: boolean
  title: string
  subtitle?: string
  badges?: React.ReactNode
  actions?: React.ReactNode
  onSelect: () => void
  busy?: boolean
}) {
  return (
    <div
      onClick={() => !disabled && !busy && onSelect()}
      className={`flex items-center gap-3 px-3 py-2.5 rounded-lg border transition-colors ${
        disabled ? 'opacity-50 cursor-not-allowed border-dark-800/40' : 'cursor-pointer'
      } ${
        active
          ? 'border-accent-500/40 bg-accent-500/[0.08]'
          : 'border-dark-800/60 hover:border-dark-700/60 hover:bg-dark-800/20'
      }`}
    >
      <span className={`w-5 h-5 rounded-full border flex items-center justify-center shrink-0 ${
        active ? 'border-accent-500 bg-accent-500' : 'border-dark-600'
      }`}>
        {active && <span className="w-1.5 h-1.5 rounded-full bg-white" />}
      </span>

      <span className="flex-1 min-w-0">
        <span className="flex items-center gap-1.5 flex-wrap">
          <span className="text-base text-dark-200 font-mono">{title}</span>
          {badges}
        </span>
        {subtitle && <span className="block text-[13px] text-dark-500">{subtitle}</span>}
      </span>

      <span className="flex items-center gap-1 shrink-0">{actions}</span>
    </div>
  )
}
