import { Routes, Route, Navigate } from 'react-router-dom'
import React, { useEffect, Suspense, lazy } from 'react'
import { motion } from 'framer-motion'
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
const XrayMonitor = lazyRetry(() => import('./pages/XrayMonitor'))

const ExtPageLazy = isExtEnabled 
  ? lazyRetry(() => import('./pages/_internal/ExtPage'))
  : null

function LoadingScreen() {
  const { t } = useTranslation()
  
  return (
    <div className="min-h-screen bg-dark-950 flex items-center justify-center relative overflow-hidden">
      {/* Animated background */}
      <div className="absolute inset-0">
        <motion.div
          className="absolute top-1/4 left-1/4 w-64 h-64 bg-accent-500/10 rounded-full blur-[80px]"
          animate={{
            scale: [1, 1.2, 1],
            opacity: [0.3, 0.5, 0.3],
          }}
          transition={{ duration: 3, repeat: Infinity, ease: 'easeInOut' }}
        />
        <motion.div
          className="absolute bottom-1/4 right-1/4 w-64 h-64 bg-purple/10 rounded-full blur-[80px]"
          animate={{
            scale: [1, 1.3, 1],
            opacity: [0.2, 0.4, 0.2],
          }}
          transition={{ duration: 4, repeat: Infinity, ease: 'easeInOut', delay: 1 }}
        />
      </div>
      
      <motion.div
        className="relative z-10 flex flex-col items-center"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
      >
        {/* Logo */}
        <motion.div 
          className="w-16 h-16 rounded-2xl bg-gradient-to-br from-dark-800 to-dark-900 
                     flex items-center justify-center border border-dark-700/50 mb-6
                     shadow-2xl shadow-accent-500/10"
          animate={{
            rotate: [0, 5, -5, 0],
          }}
          transition={{ duration: 4, repeat: Infinity, ease: 'easeInOut' }}
        >
          <Activity className="w-8 h-8 text-accent-400" />
        </motion.div>
        
        {/* Spinner */}
        <div className="relative mb-4">
          <motion.div
            className="w-12 h-12 border-2 border-accent-500/20 rounded-full"
            animate={{ rotate: 360 }}
            transition={{ duration: 3, repeat: Infinity, ease: 'linear' }}
          />
          <motion.div
            className="absolute inset-0 w-12 h-12 border-2 border-transparent border-t-accent-500 rounded-full"
            animate={{ rotate: 360 }}
            transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
          />
          <motion.div
            className="absolute inset-1 w-10 h-10 border-2 border-transparent border-b-accent-400/50 rounded-full"
            animate={{ rotate: -360 }}
            transition={{ duration: 1.5, repeat: Infinity, ease: 'linear' }}
          />
        </div>
        
        {/* Text */}
        <motion.p
          className="text-dark-400 text-sm"
          animate={{ opacity: [0.5, 1, 0.5] }}
          transition={{ duration: 1.5, repeat: Infinity }}
        >
          {t('common.loading')}
        </motion.p>
      </motion.div>
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
      gap={8}
      visibleToasts={4}
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
          <Route path="remnawave" element={<SuspenseWithBoundary><Remnawave /></SuspenseWithBoundary>} />
          <Route path="xray-monitor" element={<SuspenseWithBoundary><XrayMonitor /></SuspenseWithBoundary>} />
          <Route path="server/:serverId" element={<SuspenseWithBoundary><ServerDetails /></SuspenseWithBoundary>} />
          <Route path="server/:serverId/haproxy" element={<SuspenseWithBoundary><HAProxy /></SuspenseWithBoundary>} />
          <Route path="server/:serverId/traffic" element={<SuspenseWithBoundary><Traffic /></SuspenseWithBoundary>} />
          <Route path="settings" element={<SuspenseWithBoundary><Settings /></SuspenseWithBoundary>} />
          <Route path="updates" element={<SuspenseWithBoundary><Updates /></SuspenseWithBoundary>} />
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
