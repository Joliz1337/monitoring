import type { LossTarget } from '../../api/client'
import type { AddressGroup } from './quarantineGroups'

// На скольких релеях из тех, где стоит адрес, он не работает
export interface Spread {
  failing: number
  total: number
}

export type SpreadLevel = 'limited' | 'most' | 'all'

// Замеры релеев из таблицы потерь: по адресу и по IP — для строк, где порты не различаются
export interface ProbeIndex {
  byTarget: Map<string, LossTarget>
  byIp: Map<string, LossTarget[]>
}

export function indexProbes(targets: LossTarget[]): ProbeIndex {
  const byTarget = new Map<string, LossTarget>()
  const byIp = new Map<string, LossTarget[]>()
  for (const target of targets) {
    byTarget.set(`${target.ip}:${target.port}`, target)
    const sameIp = byIp.get(target.ip) ?? []
    byIp.set(target.ip, sameIp)
    sameIp.push(target)
  }
  return { byTarget, byIp }
}

// Не работает — релей есть в списке автоотключения либо его замер прямо сейчас говорит то же:
// сервер не отвечает на проверку «как клиент» или потери не ниже порога отключения. Так считаются
// и релеи, которым отключать запрещено или у которых автоотключение приостановлено.
// null — адреса нет в таблице потерь (сколько релеев его держат, неизвестно) или он везде работает
export function spreadOf(
  group: AddressGroup, mergePorts: boolean, probes: ProbeIndex, lossThreshold: number | null,
): Spread | null {
  const probed = mergePorts
    ? probes.byIp.get(group.ip) ?? []
    : group.ports.flatMap(port => probes.byTarget.get(`${group.ip}:${port}`) ?? [])
  if (probed.length === 0) return null

  const failing = new Set(group.items.map(item => item.server_id))
  const holding = new Set(failing)
  for (const { relays } of probed) {
    for (const relay of relays) {
      holding.add(relay.server_id)
      const lossy = lossThreshold !== null && relay.loss_pct >= lossThreshold
      if (relay.answers === false || lossy) failing.add(relay.server_id)
    }
  }
  return failing.size > 0 ? { failing: failing.size, total: holding.size } : null
}

// Единственный релей — одна точка зрения: по ней о бане везде судить нельзя
export function spreadLevel({ failing, total }: Spread): SpreadLevel {
  if (failing === total && total > 1) return 'all'
  return failing > 1 && failing * 2 >= total ? 'most' : 'limited'
}
