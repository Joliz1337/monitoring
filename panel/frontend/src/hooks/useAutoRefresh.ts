import { useEffect, useRef, useCallback, useState } from 'react'
import { useSettingsStore } from '../stores/settingsStore'

const BACKGROUND_INTERVAL = 60000 // 60 seconds for background refresh (reduced load)

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
 * - Uses fixed 60-second interval (reduced load)
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
 * Auto refresh hook with visibility awareness.
 * By default, stops polling when the browser tab is hidden to reduce server load.
 * 
 * Options:
 * - pauseWhenHidden: Stop polling when tab is hidden (default: true)
 * - refreshOnVisible: Refresh data when tab becomes visible again (default: true)
 */
export function useAutoRefresh(
  callback: () => void | Promise<void>,
  options?: {
    enabled?: boolean
    immediate?: boolean
    customInterval?: number
    pauseWhenHidden?: boolean
    refreshOnVisible?: boolean
  }
) {
  const { refreshInterval } = useSettingsStore()
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const callbackRef = useRef(callback)
  const [isPageVisible, setIsPageVisible] = useState(!document.hidden)
  
  callbackRef.current = callback
  
  const interval = options?.customInterval ?? refreshInterval * 1000
  const enabled = options?.enabled ?? true
  const pauseWhenHidden = options?.pauseWhenHidden ?? true
  const refreshOnVisible = options?.refreshOnVisible ?? true
  
  const refresh = useCallback(async () => {
    await callbackRef.current()
  }, [])
  
  const clearCurrentInterval = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }
  }, [])
  
  // Handle visibility change
  useEffect(() => {
    if (!pauseWhenHidden) return
    
    const handleVisibilityChange = () => {
      setIsPageVisible(!document.hidden)
    }
    
    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange)
  }, [pauseWhenHidden])
  
  // Set up interval based on visibility
  useEffect(() => {
    if (!enabled) {
      clearCurrentInterval()
      return
    }
    
    // If pauseWhenHidden is enabled and page is not visible, stop polling
    if (pauseWhenHidden && !isPageVisible) {
      clearCurrentInterval()
      return
    }
    
    clearCurrentInterval()
    
    // Refresh immediately when becoming visible (if refreshOnVisible is true)
    // or on initial mount (if immediate is not false)
    if (options?.immediate !== false) {
      refresh()
    }
    
    intervalRef.current = setInterval(refresh, interval)
    
    return clearCurrentInterval
  }, [enabled, interval, refresh, isPageVisible, pauseWhenHidden, clearCurrentInterval, options?.immediate])
  
  // Refresh when page becomes visible again
  useEffect(() => {
    if (pauseWhenHidden && refreshOnVisible && isPageVisible && enabled) {
      // Small delay to avoid double refresh with the interval setup
      const timeoutId = setTimeout(() => {
        if (isPageVisible && enabled) {
          refresh()
        }
      }, 100)
      return () => clearTimeout(timeoutId)
    }
  }, [isPageVisible, pauseWhenHidden, refreshOnVisible, enabled, refresh])
  
  return { refresh, isPageVisible }
}
