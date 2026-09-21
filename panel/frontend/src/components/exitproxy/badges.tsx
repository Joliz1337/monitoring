import {
  AlertTriangle, Ban, CheckCircle2, Clock, Cloud, Globe, Loader2, Lock, ShieldAlert, XCircle, type LucideIcon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ExitProxyInstallStatus } from '../../api/client'
import { Tooltip } from '../ui/Tooltip'

export const BADGE = 'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border whitespace-nowrap'

export const TONE = {
  green: 'text-green-400 bg-green-500/10 border-green-500/20',
  yellow: 'text-yellow-400 bg-yellow-500/10 border-yellow-500/20',
  red: 'text-red-400 bg-red-500/10 border-red-500/20',
  // Фиолетовый — «так настроено на ноде», как у NodeRestrictedNotice
  purple: 'text-purple bg-purple/10 border-purple/20',
  orange: 'text-orange-400 bg-orange-500/10 border-orange-500/20',
  amber: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
  dark: 'text-dark-400 bg-dark-700/30 border-dark-600/40',
} as const

type Tone = keyof typeof TONE

const INSTALL: Record<ExitProxyInstallStatus, { tone: Tone; Icon: LucideIcon; spin?: boolean }> = {
  off: { tone: 'dark', Icon: Clock },
  pending: { tone: 'yellow', Icon: Loader2, spin: true },
  active: { tone: 'green', Icon: CheckCircle2 },
  failed: { tone: 'red', Icon: XCircle },
  denied: { tone: 'purple', Icon: Lock },
  unsupported: { tone: 'orange', Icon: AlertTriangle },
}

function Badge({ tone, Icon, spin, label }: { tone: Tone; Icon: LucideIcon; spin?: boolean; label: string }) {
  return (
    <span className={`${BADGE} ${TONE[tone]}`}>
      <Icon className={`w-3 h-3 ${spin ? 'animate-spin' : ''}`} />
      {label}
    </span>
  )
}

export function InstallBadge({ status, hint }: { status: ExitProxyInstallStatus; hint?: string }) {
  const { t } = useTranslation()
  const { tone, Icon, spin } = INSTALL[status]
  const badge = <Badge tone={tone} Icon={Icon} spin={spin} label={t(`exit_proxy.install_${status}`)} />
  return hint ? <Tooltip label={hint}><span>{badge}</span></Tooltip> : badge
}

export function HealthBadge({ healthy, enabled = true }: { healthy: boolean | null; enabled?: boolean }) {
  const { t } = useTranslation()
  if (!enabled) return <Badge tone="dark" Icon={Ban} label={t('exit_proxy.health_disabled')} />
  if (healthy === true) return <Badge tone="green" Icon={CheckCircle2} label={t('exit_proxy.health_ok')} />
  if (healthy === false) return <Badge tone="red" Icon={XCircle} label={t('exit_proxy.health_bad')} />
  return <Badge tone="dark" Icon={Clock} label={t('exit_proxy.health_unknown')} />
}

export function SelfTestBadge({ ok }: { ok: boolean | null }) {
  const { t } = useTranslation()
  if (ok === true) return <Badge tone="green" Icon={CheckCircle2} label={t('exit_proxy.self_test_ok')} />
  if (ok === false) return <Badge tone="red" Icon={XCircle} label={t('exit_proxy.self_test_fail')} />
  return <Badge tone="dark" Icon={Clock} label={t('exit_proxy.self_test_unknown')} />
}

export function CaptchaChip() {
  const { t } = useTranslation()
  return <Badge tone="amber" Icon={ShieldAlert} label={t('exit_proxy.captcha')} />
}

export function CheckChip({ name, ok, detail }: { name: string; ok: boolean | null; detail?: string | null }) {
  const tone: Tone = ok === true ? 'green' : ok === false ? 'red' : 'dark'
  const Icon = ok === true ? CheckCircle2 : ok === false ? XCircle : Clock
  const chip = <Badge tone={tone} Icon={Icon} label={name} />
  return detail ? <Tooltip label={detail}><span>{chip}</span></Tooltip> : chip
}

export function KindIcon({ kind, className = 'w-4 h-4' }: { kind: 'ip' | 'warp'; className?: string }) {
  const Icon = kind === 'warp' ? Cloud : Globe
  return <Icon className={`${className} text-dark-400 shrink-0`} />
}
