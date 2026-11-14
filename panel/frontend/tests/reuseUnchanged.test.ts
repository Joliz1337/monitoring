import { describe, expect, it } from 'vitest'
import { reuseUnchanged } from '../src/utils/reuseUnchanged'

// Ответ опроса профиля: страница держит его в состоянии и перечитывает каждые 3 секунды
const polledProfile = () => ({
  id: 1,
  name: 'edge',
  rules: [{ port: 443, protocol: 'tcp' }],
  servers: [
    { server_id: 1, sync_status: 'synced', last_sync_at: '2026-11-14T10:00:00Z', targets: { web: '10.0.0.1' } },
    { server_id: 2, sync_status: 'pending', last_sync_at: null, targets: {} },
    { server_id: 3, sync_status: 'synced', last_sync_at: '2026-11-14T10:00:05Z', targets: {} },
  ],
})

describe('reuseUnchanged', () => {
  it('возвращает само прежнее состояние, если опрос не принёс нового', () => {
    const prev = polledProfile()

    expect(reuseUnchanged(prev, polledProfile())).toBe(prev)
  })

  it('оставляет неизменившиеся строки прежними объектами, когда изменилась одна', () => {
    const prev = polledProfile()
    const next = polledProfile()
    next.servers[1].sync_status = 'synced'

    const merged = reuseUnchanged(prev, next)

    expect(merged).not.toBe(prev)
    expect(merged).toEqual(next)
    expect(merged.servers[0]).toBe(prev.servers[0])
    expect(merged.servers[1]).not.toBe(prev.servers[1])
    expect(merged.servers[2]).toBe(prev.servers[2])
    expect(merged.rules).toBe(prev.rules)
  })

  it('всегда равен свежему ответу: удалённые строки и поля не задерживаются', () => {
    const prev = { ...polledProfile(), description: 'old' }
    const next = polledProfile()
    next.servers.pop()

    const merged = reuseUnchanged(prev, next)

    expect(merged).toEqual(next)
    expect(merged).not.toHaveProperty('description')
    expect(merged.servers).toHaveLength(2)
  })

  it('замечает поле, сменившее имя при том же числе полей', () => {
    const merged = reuseUnchanged({ a: 1, b: undefined }, { a: 1, c: undefined })

    expect(Object.keys(merged)).toEqual(['a', 'c'])
  })

  it('первый ответ принимает как есть', () => {
    const next = polledProfile()

    expect(reuseUnchanged(null, next)).toBe(next)
  })
})
