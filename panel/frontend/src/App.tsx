import { Routes, Route, Navigate } from 'react-router-dom'
import React, { useEffect, Suspense, lazy } from 'react'
import { Activity } from 'lucide-react'
import { Toaster, toast } from 'sonner'
import { useAuthStore } from './stores/authStore'
import { useExtStore } from './stores/_extStore'
import { useTranslation } from 'react-i18next'
import ErrorBoundary from './components/ErrorBoundary'
import Layout from './components/Layout/Layout'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Servers from './pages/Servers'
import { isExtEnabled } from './pages/_internal'

function lazyRetry<T extends React.ComponentType<unknown>>(
  factory: () => Promise<{ default: T }>,
  retries = 2
): React.LazyExoticComponent<T> {
  return lazy(async () => {
    for (let i = 0; i <= retries; i++) {
      try {
        return await factory()
      } catch (err) {
        if (i === retries) throw err
        await new Promise(r => setTimeout(r, 1000 * (i + 1)))
      }
    }
    return factory()
  })
}

const ServerDetails = lazyRetry(() => import('./pages/ServerDetails'))
const HAProxy = lazyRetry(() => import('./pages/HAProxy'))
const Traffic = lazyRetry(() => import('./pages/Traffic'))
const Settings = lazyRetry(() => import('./pages/Settings'))
const Updates = lazyRetry(() => import('./pages/Updates'))
const BulkActions = lazyRetry(() => import('./pages/BulkActions'))
const Blocklist = lazyRetry(() => import('./pages/Blocklist'))
const Remnawave = lazyRetry(() => import('./pages/Remnawave'))
const Alerts = lazyRetry(() => import('./pages/Alerts'))
const Billing = lazyRetry(() => import('./pages/Billing'))
const SSHSecurity = lazyRetry(() => import('./pages/SSHSecurity'))
const WildcardSSL = lazyRetry(() => import('./pages/WildcardSSL'))
const HAProxyConfigs = lazyRetry(() => import('./pages/HAProxyConfigs'))
const FirewallProfiles = lazyRetry(() => import('./pages/FirewallProfiles'))
const SystemOptimizations = lazyRetry(() => import('./pages/SystemOptimizations'))
const TorrentBlocker = lazyRetry(() => import('./pages/TorrentBlocker'))
const AntiDdos = lazyRetry(() => import('./pages/AntiDdos'))

const ExtPageLazy = isExtEnabled 
  ? lazyRetry(() => import('./pages/_internal/ExtPage'))
  : null

function LoadingScreen() {
  const { t } = useTranslation()

  return (
    <div className="min-h-screen bg-dark-950 flex items-center justify-center relative overflow-hidden">
      {/* Animated background — CSS-only, blur уменьшен с 80 до 48 (в 2.7x легче) */}
      <div className="absolute inset-0 pointer-events-none">
        <div
          className="loading-blob top-1/4 left-1/4 bg-accent-500/10"
        />
        <div
          className="loading-blob bottom-1/4 right-1/4 bg-purple/10"
          style={{ animationDelay: '1s', animationDuration: '4s' }}
        />
      </div>

      <div className="relative z-10 flex flex-col items-center fade-in">
        {/* Logo */}
        <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-dark-800 to-dark-900
                     flex items-center justify-center border border-dark-700/50 mb-6
                     shadow-2xl shadow-accent-500/10 loading-logo-wobble">
          <Activity className="w-8 h-8 text-accent-400" />
        </div>

        {/* Spinner — два кольца через CSS */}
        <div className="relative mb-4 w-12 h-12">
          <div className="absolute inset-0 border-2 border-accent-500/20 rounded-full" />
          <div className="absolute inset-0 border-2 border-transparent border-t-accent-500 rounded-full icon-spin" />
          <div
            className="absolute inset-1 border-2 border-transparent border-b-accent-400/50 rounded-full icon-spin"
            style={{ animationDuration: '1.5s', animationDirection: 'reverse' }}
          />
        </div>

        {/* Text */}
        <p className="text-dark-400 text-sm loading-text-pulse">
          {t('common.loading')}
        </p>
      </div>
    </div>
  )
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading, checkAuth } = useAuthStore()
  
  useEffect(() => {
    checkAuth()
  }, [checkAuth])
  
  useEffect(() => {
    if (isAuthenticated && isExtEnabled) {
      const state = useExtStore.getState() as unknown as Record<string, unknown>
      if (typeof state.initExt === 'function') {
        (state.initExt as () => Promise<void>)()
      }
    }
  }, [isAuthenticated])
  
  if (isLoading) {
    return <LoadingScreen />
  }
  
  if (!isAuthenticated) {
    const uid = window.location.pathname.split('/')[1]
    return <Navigate to={`/${uid}/login`} replace />
  }
  
  return <>{children}</>
}

function SuspenseWithBoundary({ children }: { children: React.ReactNode }) {
  return (
    <ErrorBoundary>
      <Suspense fallback={<LoadingScreen />}>
        {children}
      </Suspense>
    </ErrorBoundary>
  )
}

export default function App() {
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement
      if (target.closest('[data-close-button]')) return
      if (target.closest('[data-button]')) return
      const toastEl = target.closest('[data-sonner-toast]') as HTMLElement | null
      if (!toastEl) return
      const id = toastEl.getAttribute('data-sonner-toast')
      toast.dismiss(id || undefined)
    }
    document.addEventListener('click', handler)
    return () => document.removeEventListener('click', handler)
  }, [])

  return (
    <>
    <Toaster 
      theme="dark"
      position="top-right"
      toastOptions={{
        className: 'cursor-pointer',
        style: {
          background: 'rgba(30, 30, 40, 0.95)',
          border: '1px solid rgba(255, 255, 255, 0.08)',
          color: '#e2e2e8',
          backdropFilter: 'blur(12px)',
        },
      }}
      gap={12}
      visibleToasts={5}
      expand
      closeButton
    />
    <ErrorBoundary>
      <Routes>
        <Route path="/:uid/login" element={<Login />} />
        <Route
          path="/:uid"
          element={
            <ProtectedRoute>
              <Layout />
            </ProtectedRoute>
          }
        >
          <Route index element={<ErrorBoundary><Dashboard /></ErrorBoundary>} />
          <Route path="servers" element={<ErrorBoundary><Servers /></ErrorBoundary>} />
          <Route path="bulk-actions" element={<SuspenseWithBoundary><BulkActions /></SuspenseWithBoundary>} />
          <Route path="alerts" element={<SuspenseWithBoundary><Alerts /></SuspenseWithBoundary>} />
          <Route path="billing" element={<SuspenseWithBoundary><Billing /></SuspenseWithBoundary>} />
          <Route path="blocklist" element={<SuspenseWithBoundary><Blocklist /></SuspenseWithBoundary>} />
          <Route path="torrent-blocker" element={<SuspenseWithBoundary><TorrentBlocker /></SuspenseWithBoundary>} />
          <Route path="ssh-security" element={<SuspenseWithBoundary><SSHSecurity /></SuspenseWithBoundary>} />
          <Route path="remnawave" element={<SuspenseWithBoundary><Remnawave /></SuspenseWithBoundary>} />
          <Route path="server/:serverId" element={<SuspenseWithBoundary><ServerDetails /></SuspenseWithBoundary>} />
          <Route path="server/:serverId/haproxy" element={<SuspenseWithBoundary><HAProxy /></SuspenseWithBoundary>} />
          <Route path="server/:serverId/traffic" element={<SuspenseWithBoundary><Traffic /></SuspenseWithBoundary>} />
          <Route path="haproxy-configs" element={<SuspenseWithBoundary><HAProxyConfigs /></SuspenseWithBoundary>} />
          <Route path="firewall-profiles" element={<SuspenseWithBoundary><FirewallProfiles /></SuspenseWithBoundary>} />
          <Route path="wildcard-ssl" element={<SuspenseWithBoundary><WildcardSSL /></SuspenseWithBoundary>} />
          <Route path="settings" element={<SuspenseWithBoundary><Settings /></SuspenseWithBoundary>} />
          <Route path="updates" element={<SuspenseWithBoundary><Updates /></SuspenseWithBoundary>} />
          <Route path="system-optimizations" element={<SuspenseWithBoundary><SystemOptimizations /></SuspenseWithBoundary>} />
          <Route path="anti-ddos" element={<SuspenseWithBoundary><AntiDdos /></SuspenseWithBoundary>} />
          {ExtPageLazy && (
            <Route 
              path="ip-search"
              element={
                <SuspenseWithBoundary>
                  <ExtPageLazy />
                </SuspenseWithBoundary>
              } 
            />
          )}
        </Route>
      </Routes>
    </ErrorBoundary>
    </>
  )
}
