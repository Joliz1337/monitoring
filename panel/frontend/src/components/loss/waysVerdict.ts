import type { TFunction } from 'i18next'
import type { LossWay } from '../../api/client'
import { LOSS_BAD_PCT, LOSS_WARN_PCT } from '../ui/LossProbeBadge'

// По меньшему числу попыток судить рано: одна потерянная из трёх ничего не значит
const MIN_SAMPLES = 10

export type WaysVerdict =
  // С выхода правила плохо, а с другого адреса релея нормально
  | { kind: 'exit'; silent: boolean; worse: LossWay; better: LossWay }
  // Плохо со всех адресов релея
  | { kind: 'everywhere' }

const isBad = (way: LossWay): boolean => way.answers === false || way.loss_pct > LOSS_BAD_PCT
const isClean = (way: LossWay): boolean => way.answers !== false && way.loss_pct <= LOSS_WARN_PCT
const badness = (way: LossWay): number => (way.answers === false ? 100 : way.loss_pct)

/** Что видно из сравнения адресов релея. С выхода правила плохо, а с другого адреса нормально —
 *  скорее всего, дело в этом выходе или пути с него; плохо со всех — в сервере или дороге целиком.
 *  Это подсказка, а не диагноз: null — сравнивать не с чем или уверенного вывода нет */
export function waysVerdict(ways: LossWay[]): WaysVerdict | null {
  const judged = ways.filter(way => way.samples >= MIN_SAMPLES)
  const used = judged.filter(way => way.used)
  const spare = judged.filter(way => !way.used)
  const bad = used.filter(isBad)
  if (bad.length === 0 || spare.length === 0) return null

  const silent = bad.some(way => way.answers === false)
  // Сервер молчит с выхода: «с другого адреса лучше» честно сказать, только если оттуда он именно
  // ответил — небольшие потери при этом не мешают. Потери сравниваются с чистым адресом
  const better = spare.find(way => (silent ? way.answers === true && !isBad(way) : isClean(way)))
  if (better) {
    const worse = bad.reduce((worst, way) => (badness(way) > badness(worst) ? way : worst))
    return { kind: 'exit', silent, worse, better }
  }
  return bad.length === used.length && spare.every(isBad) ? { kind: 'everywhere' } : null
}

export function verdictText(verdict: WaysVerdict, t: TFunction): string {
  if (verdict.kind === 'everywhere') return t('loss.ways_verdict_everywhere')
  const name = (way: LossWay) => way.local_ip ?? t('loss.ways_default_address')
  return t(verdict.silent ? 'loss.ways_verdict_exit_silent' : 'loss.ways_verdict_exit_loss', {
    worse: name(verdict.worse), better: name(verdict.better),
    loss: verdict.worse.loss_pct, betterLoss: verdict.better.loss_pct,
  })
}
