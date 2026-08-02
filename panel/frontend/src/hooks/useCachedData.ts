import { useCallback, useState } from 'react'

interface CacheEntry<T> {
  data: T
  cachedAt: string
}

interface UseCachedDataReturn<T> {
  isCached: boolean
  cachedAt: Date | null
  saveToCache: (data: T) => void
  loadFromCache: () => T | null
  clearCache: () => void
  setIsCached: (value: boolean) => void
  setCachedAt: (date: Date | null) => void
}

/**
 * Hook for managing cached data in localStorage
 * Used to show last known data when server is unavailable
 */
export function useCachedData<T>(cacheKey: string): UseCachedDataReturn<T> {
  const [isCached, setIsCached] = useState(false)
  const [cachedAt, setCachedAt] = useState<Date | null>(null)

  const getFullKey = useCallback(() => `cache_${cacheKey}`, [cacheKey])

  const saveToCache = useCallback((data: T) => {
    try {
      const entry: CacheEntry<T> = {
        data,
        cachedAt: new Date().toISOString()
      }
      localStorage.setItem(getFullKey(), JSON.stringify(entry))
      setIsCached(false)
      setCachedAt(null)
    } catch (e) {
      // localStorage might be full or disabled
      console.warn('Failed to save to cache:', e)
    }
  }, [getFullKey])

  const loadFromCache = useCallback((): T | null => {
    try {
      const stored = localStorage.getItem(getFullKey())
      if (!stored) return null

      const entry: CacheEntry<T> = JSON.parse(stored)
      if (entry.data && entry.cachedAt) {
        setIsCached(true)
        setCachedAt(new Date(entry.cachedAt))
        return entry.data
      }
      return null
    } catch (e) {
      console.warn('Failed to load from cache:', e)
      return null
    }
  }, [getFullKey])

  const clearCache = useCallback(() => {
    try {
      localStorage.removeItem(getFullKey())
      setIsCached(false)
      setCachedAt(null)
    } catch (e) {
      console.warn('Failed to clear cache:', e)
    }
  }, [getFullKey])

  return {
    isCached,
    cachedAt,
    saveToCache,
    loadFromCache,
    clearCache,
    setIsCached,
    setCachedAt
  }
}

/**
 * Helper to create cache key for server-specific data
 */
export function createServerCacheKey(serverId: number | string, dataType: 'haproxy' | 'traffic' | 'metrics'): string {
  return `${dataType}_${serverId}`
}
