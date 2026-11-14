import { afterEach, describe, expect, it, vi } from 'vitest'
import { extractHost, formatBitsPerSec, formatBytes, formatTimeAgo, formatUptime } from '../src/utils/format'

describe('formatBytes', () => {
  it('подбирает единицу по размеру', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1024)).toBe('1 KB')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(5 * 1024 ** 3)).toBe('5 GB')
  })

  it('округляет до заданного числа знаков', () => {
    expect(formatBytes(1234567, 0)).toBe('1 MB')
    expect(formatBytes(1234567, 2)).toBe('1.18 MB')
  })
})

describe('formatBitsPerSec', () => {
  it('переводит байты в секунду в биты с десятичными приставками', () => {
    expect(formatBitsPerSec(0)).toBe('0 bit/s')
    expect(formatBitsPerSec(125)).toBe('1 Kbit/s')
    expect(formatBitsPerSec(125_000)).toBe('1 Mbit/s')
  })
})

describe('formatUptime', () => {
  it('показывает две старшие единицы', () => {
    expect(formatUptime(59)).toBe('0m')
    expect(formatUptime(3600 + 2 * 60)).toBe('1h 2m')
    expect(formatUptime(3 * 86400 + 2 * 3600 + 59 * 60)).toBe('3d 2h')
  })
})

describe('formatTimeAgo', () => {
  const NOW = new Date('2026-01-01T12:00:00Z')
  const ago = (seconds: number) => new Date(NOW.getTime() - seconds * 1000).toISOString()

  afterEach(() => {
    vi.useRealTimers()
  })

  it('считает давность от текущего времени', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)

    expect(formatTimeAgo(ago(30))).toBe('Just now')
    expect(formatTimeAgo(ago(5 * 60))).toBe('5m ago')
    expect(formatTimeAgo(ago(2 * 3600 + 5 * 60))).toBe('2h 5m ago')
    expect(formatTimeAgo(ago(3 * 86400))).toBe('3d ago')
  })

  it('без даты отвечает Never', () => {
    expect(formatTimeAgo(null)).toBe('Never')
  })
})

describe('extractHost', () => {
  it('достаёт хост из адреса ноды', () => {
    expect(extractHost('https://10.0.0.1:9100')).toBe('10.0.0.1')
    expect(extractHost('https://node.example.com:9100/api')).toBe('node.example.com')
  })
})
