import { Routes, Route, Navigate } from 'react-router-dom'
import { useEffect, Suspense, lazy } from 'react'
import { motion } from 'framer-motion'
import { Activity } from 'lucide-react'
import { Toaster } from 'sonner'
import { useAuthStore } from './stores/authStore'
import { useExtStore } from './stores/_extStore'
import { useTranslation } from 'react-i18next'
import Layout from './components/Layout/Layout'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Servers from './pages/Servers'
import { isExtEnabled } from './pages/_internal'

const ServerDetails = lazy(() => import('./pages/ServerDetails'))
const HAProxy = lazy(() => import('./pages/HAProxy'))
const Traffic = lazy(() => import('./pages/Traffic'))
const Settings = lazy(() => import('./pages/Settings'))
const Updates = lazy(() => import('./pages/Updates'))
const BulkActions = lazy(() => import('./pages/BulkActions'))
const Blocklist = lazy(() => import('./pages/Blocklist'))
const Remnawave = lazy(() => import('./pages/Remnawave'))
const Alerts = lazy(() => import('./pages/Alerts'))
const Billing = lazy(() => import('./pages/Billing'))

const ExtPageLazy = isExtEnabled 
  ? lazy(() => import('./pages/_internal/ExtPage'))
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

export default function App() {
  const navItem = useExtStore(s => s.navItem)
  const extPath = navItem?.path || 'ext'
  
  return (
    <>
    <Toaster 
      theme="dark"
      position="top-right"
      toastOptions={{
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
        <Route index element={<Dashboard />} />
        <Route path="servers" element={<Servers />} />
        <Route path="bulk-actions" element={<Suspense fallback={<LoadingScreen />}><BulkActions /></Suspense>} />
        <Route path="alerts" element={<Suspense fallback={<LoadingScreen />}><Alerts /></Suspense>} />
        <Route path="billing" element={<Suspense fallback={<LoadingScreen />}><Billing /></Suspense>} />
        <Route path="blocklist" element={<Suspense fallback={<LoadingScreen />}><Blocklist /></Suspense>} />
        <Route path="remnawave" element={<Suspense fallback={<LoadingScreen />}><Remnawave /></Suspense>} />
        <Route path="server/:serverId" element={<Suspense fallback={<LoadingScreen />}><ServerDetails /></Suspense>} />
        <Route path="server/:serverId/haproxy" element={<Suspense fallback={<LoadingScreen />}><HAProxy /></Suspense>} />
        <Route path="server/:serverId/traffic" element={<Suspense fallback={<LoadingScreen />}><Traffic /></Suspense>} />
        <Route path="settings" element={<Suspense fallback={<LoadingScreen />}><Settings /></Suspense>} />
        <Route path="updates" element={<Suspense fallback={<LoadingScreen />}><Updates /></Suspense>} />
        {ExtPageLazy && (
          <Route 
            path={extPath}
            element={
              <Suspense fallback={<LoadingScreen />}>
                <ExtPageLazy />
              </Suspense>
            } 
          />
        )}
      </Route>
    </Routes>
    </>
  )
}
