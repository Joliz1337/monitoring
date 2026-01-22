import { Routes, Route, Navigate } from 'react-router-dom'
import { useEffect, Suspense, lazy } from 'react'
import { motion } from 'framer-motion'
import { Activity } from 'lucide-react'
import { useAuthStore } from './stores/authStore'
import { useTranslation } from 'react-i18next'
import Layout from './components/Layout/Layout'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import ServerDetails from './pages/ServerDetails'
import HAProxy from './pages/HAProxy'
import Traffic from './pages/Traffic'
import Settings from './pages/Settings'
import Servers from './pages/Servers'
import Updates from './pages/Updates'
import BulkActions from './pages/BulkActions'
import { isExtEnabled } from './pages/_internal'

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
  return (
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
        <Route path="bulk-actions" element={<BulkActions />} />
        <Route path="server/:serverId" element={<ServerDetails />} />
        <Route path="server/:serverId/haproxy" element={<HAProxy />} />
        <Route path="server/:serverId/traffic" element={<Traffic />} />
        <Route path="settings" element={<Settings />} />
        <Route path="updates" element={<Updates />} />
        {ExtPageLazy && (
          <Route 
            path="ext" 
            element={
              <Suspense fallback={<LoadingScreen />}>
                <ExtPageLazy />
              </Suspense>
            } 
          />
        )}
      </Route>
    </Routes>
  )
}
