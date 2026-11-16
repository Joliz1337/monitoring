import { lossApi, type QuarantineItem } from '../../api/client'

// Возврат — запрос на каждую пару «релей → адрес», а адрес бывает отключён на сотнях релеев:
// запросы идут пачками, а не все разом
const RELEASE_BATCH = 10

// false — хотя бы одну пару вернуть не удалось
export async function releaseItems(items: QuarantineItem[]): Promise<boolean> {
  let allReleased = true
  for (let start = 0; start < items.length; start += RELEASE_BATCH) {
    const outcomes = await Promise.allSettled(
      items.slice(start, start + RELEASE_BATCH).map(item => lossApi.releaseQuarantine(item.server_id, item.target)),
    )
    if (outcomes.some(outcome => outcome.status === 'rejected')) allReleased = false
  }
  return allReleased
}
