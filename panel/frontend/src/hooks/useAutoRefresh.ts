import { useEffect, useRef, useCallback, useState } from 'react'
import { useSettingsStore } from '../stores/settingsStore'

const BACKGROUND_INTERVAL = 5000 // 5 seconds for background refresh

interface UseSmartRefreshOptions {
  enabled?: boolean
  immediate?: boolean
  customInterval?: number
}

interface UseSmartRefreshResult {
  refresh: () => Promise<void>
  isPageVisible: boolean
}

/**
 * Smart refresh hook that uses different strategies based on page visibility.
 * 
 * When page is VISIBLE (user is actively viewing):
 * - Uses user's refresh interval setting
 * - Calls the liveCallback for direct node requests
 * 
 * When page is HIDDEN (user switched tabs/minimized):
 * - Uses fixed 5-second interval
 * - Calls the cachedCallback for database cached data
 */
export function useSmartRefresh(
  liveCallback: () => void | Promise<void>,
  cachedCallback: () => void | Promise<void>,
  options?: UseSmartRefreshOptions
): UseSmartRefreshResult {
  const { refreshInterval } = useSettingsStore()
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const liveCallbackRef = useRef(liveCallback)
  const cachedCallbackRef = useRef(cachedCallback)
  const [isPageVisible, setIsPageVisible] = useState(!document.hidden)
  
  liveCallbackRef.current = liveCallback
  cachedCallbackRef.current = cachedCallback
  
  const userInterval = options?.customInterval ?? refreshInterval * 1000
  const enabled = options?.enabled ?? true
  
  const clearCurrentInterval = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }
  }, [])
  
  const refreshLive = useCallback(async () => {
    await liveCallbackRef.current()
  }, [])
  
  const refreshCached = useCallback(async () => {
    await cachedCallbackRef.current()
  }, [])
  
  // Handle visibility change
  useEffect(() => {
    const handleVisibilityChange = () => {
      const visible = !document.hidden
      setIsPageVisible(visible)
    }
    
    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange)
  }, [])
  
  // Set up interval based on visibility
  useEffect(() => {
    if (!enabled) {
      clearCurrentInterval()
      return
    }
    
    clearCurrentInterval()
    
    if (isPageVisible) {
      // Page is visible - use live data with user's interval
      if (options?.immediate !== false) {
        refreshLive()
      }
      intervalRef.current = setInterval(refreshLive, userInterval)
    } else {
      // Page is hidden - use cached data with 5 second interval
      intervalRef.current = setInterval(refreshCached, BACKGROUND_INTERVAL)
    }
    
    return clearCurrentInterval
  }, [enabled, isPageVisible, userInterval, refreshLive, refreshCached, clearCurrentInterval, options?.immediate])
  
  return { refresh: refreshLive, isPageVisible }
}

/**
 * Original auto refresh hook for backward compatibility.
 * Always uses the same callback regardless of page visibility.
 */
export function useAutoRefresh(
  callback: () => void | Promise<void>,
  options?: {
    enabled?: boolean
    immediate?: boolean
    customInterval?: number
  }
) {
  const { refreshInterval } = useSettingsStore()
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const callbackRef = useRef(callback)
  
  callbackRef.current = callback
  
  const interval = options?.customInterval ?? refreshInterval * 1000
  const enabled = options?.enabled ?? true
  
  const refresh = useCallback(async () => {
    await callbackRef.current()
  }, [])
  
  useEffect(() => {
    if (!enabled) {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
      return
    }
    
    if (options?.immediate !== false) {
      refresh()
    }
    
    intervalRef.current = setInterval(refresh, interval)
    
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
  }, [enabled, interval, refresh, options?.immediate])
  
  return { refresh }
}
