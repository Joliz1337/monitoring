import { useState, useEffect, FormEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { Lock, AlertCircle, Loader2, Activity, Sparkles, Shield, Eye, EyeOff } from 'lucide-react'
import { useAuthStore } from '../stores/authStore'
import { useTranslation } from 'react-i18next'

const floatingShapes = [
  { size: 300, x: '10%', y: '20%', delay: 0, duration: 20 },
  { size: 200, x: '80%', y: '60%', delay: 5, duration: 25 },
  { size: 150, x: '60%', y: '10%', delay: 10, duration: 18 },
  { size: 250, x: '30%', y: '70%', delay: 8, duration: 22 },
]

export default function Login() {
  const { uid } = useParams()
  const navigate = useNavigate()
  const { login, checkAuth, validateUid } = useAuthStore()
  const { t } = useTranslation()
  
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [isValidUid, setIsValidUid] = useState<boolean | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const [isFocused, setIsFocused] = useState(false)
  
  useEffect(() => {
    const validateAndCheck = async () => {
      if (!uid) {
        setIsValidUid(false)
        return
      }
      
      // Validate UID on backend (connection drops if invalid)
      const isValid = await validateUid(uid)
      if (!isValid) {
        setIsValidUid(false)
        return
      }
      setIsValidUid(true)
      
      const isAuth = await checkAuth()
      if (isAuth) {
        navigate(`/${uid}`, { replace: true })
      }
    }
    validateAndCheck()
  }, [uid, validateUid, checkAuth, navigate])
  
  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setIsLoading(true)
    
    const result = await login(password)
    setIsLoading(false)
    
    if (result.success) {
      navigate(`/${uid}`, { replace: true })
    } else {
      setError(result.error || t('login.invalid_password'))
      setPassword('')
    }
  }
  
  if (isValidUid === null) {
    return (
      <div className="min-h-screen bg-dark-950 flex items-center justify-center">
        <motion.div
          className="relative"
          initial={{ opacity: 0, scale: 0.8 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.5 }}
        >
          <motion.div
            className="w-12 h-12 border-2 border-accent-500/30 rounded-full"
            animate={{ rotate: 360 }}
            transition={{ duration: 2, repeat: Infinity, ease: 'linear' }}
          />
          <motion.div
            className="absolute inset-0 w-12 h-12 border-2 border-transparent border-t-accent-500 rounded-full"
            animate={{ rotate: 360 }}
            transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
          />
        </motion.div>
      </div>
    )
  }
  
  // Invalid UID - connection was dropped by nginx/backend, show nothing
  if (isValidUid === false) {
    return <div className="min-h-screen bg-dark-950" />
  }
  
  return (
    <div className="min-h-screen bg-dark-950 flex items-center justify-center p-4 overflow-hidden relative">
      {/* Animated background shapes */}
      <div className="absolute inset-0 overflow-hidden">
        {floatingShapes.map((shape, index) => (
          <motion.div
            key={index}
            className="absolute rounded-full"
            style={{
              width: shape.size,
              height: shape.size,
              left: shape.x,
              top: shape.y,
              background: `radial-gradient(circle, rgba(34, 211, 238, 0.08) 0%, transparent 70%)`,
            }}
            animate={{
              x: [0, 30, -30, 0],
              y: [0, -30, 30, 0],
              scale: [1, 1.1, 0.9, 1],
            }}
            transition={{
              duration: shape.duration,
              delay: shape.delay,
              repeat: Infinity,
              ease: 'easeInOut',
            }}
          />
        ))}
        
        {/* Grid pattern */}
        <div className="absolute inset-0 opacity-[0.02]"
          style={{
            backgroundImage: `
              linear-gradient(rgba(34, 211, 238, 0.5) 1px, transparent 1px),
              linear-gradient(90deg, rgba(34, 211, 238, 0.5) 1px, transparent 1px)
            `,
            backgroundSize: '60px 60px'
          }}
        />
      </div>
      
      <motion.div 
        className="w-full max-w-md relative z-10"
        initial={{ opacity: 0, y: 40 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: 'easeOut' }}
      >
        {/* Logo section */}
        <motion.div 
          className="text-center mb-10"
          initial={{ opacity: 0, y: -20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2, duration: 0.5 }}
        >
          <motion.div 
            className="inline-flex items-center justify-center w-20 h-20 rounded-2xl 
                       bg-gradient-to-br from-dark-800 to-dark-900 
                       border border-dark-700/50 mb-6 relative
                       shadow-2xl shadow-accent-500/10"
            whileHover={{ scale: 1.05, rotate: 5 }}
            transition={{ type: 'spring', stiffness: 400 }}
          >
            <Activity className="w-10 h-10 text-accent-400" />
            <motion.div
              className="absolute inset-0 rounded-2xl border border-accent-500/30"
              animate={{ 
                opacity: [0.3, 0.6, 0.3],
                scale: [1, 1.05, 1]
              }}
              transition={{ duration: 2, repeat: Infinity }}
            />
          </motion.div>
          
          <motion.h1 
            className="text-3xl font-bold text-dark-50 flex items-center justify-center gap-3"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.3 }}
          >
            {t('login.title')}
            <Sparkles className="w-5 h-5 text-accent-400" />
          </motion.h1>
          
          <motion.p 
            className="text-dark-400 mt-3 flex items-center justify-center gap-2"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.4 }}
          >
            <Shield className="w-4 h-4" />
            {t('login.subtitle')}
          </motion.p>
        </motion.div>
        
        {/* Login form */}
        <motion.form 
          onSubmit={handleSubmit} 
          className="card relative overflow-hidden"
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.3, duration: 0.4 }}
        >
          {/* Card glow effect */}
          <motion.div
            className="absolute inset-0 rounded-2xl"
            style={{
              background: `radial-gradient(circle at ${isFocused ? '50%' : '0%'} 50%, rgba(34, 211, 238, 0.1) 0%, transparent 50%)`,
            }}
            animate={{
              opacity: isFocused ? 1 : 0,
            }}
            transition={{ duration: 0.3 }}
          />
          
          <AnimatePresence mode="wait">
            {error && (
              <motion.div 
                className="flex items-center gap-3 p-4 mb-6 bg-danger/10 border border-danger/20 
                           rounded-xl text-danger relative overflow-hidden"
                initial={{ opacity: 0, y: -10, height: 0 }}
                animate={{ opacity: 1, y: 0, height: 'auto' }}
                exit={{ opacity: 0, y: -10, height: 0 }}
                transition={{ duration: 0.3 }}
              >
                <motion.div
                  initial={{ rotate: -90, scale: 0 }}
                  animate={{ rotate: 0, scale: 1 }}
                  transition={{ delay: 0.1, type: 'spring' }}
                >
                  <AlertCircle className="w-5 h-5 flex-shrink-0" />
                </motion.div>
                <span className="text-sm">{error}</span>
              </motion.div>
            )}
          </AnimatePresence>
          
          <div className="relative mb-6">
            <motion.div
              className={`absolute left-4 top-1/2 -translate-y-1/2 transition-colors duration-200 ${
                isFocused ? 'text-accent-400' : 'text-dark-500'
              }`}
              animate={{ scale: isFocused ? 1.1 : 1 }}
            >
              <Lock className="w-5 h-5" />
            </motion.div>
            
            <input
              type={showPassword ? 'text' : 'password'}
              name="panel-auth-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onFocus={() => setIsFocused(true)}
              onBlur={() => setIsFocused(false)}
              placeholder={t('login.password_placeholder')}
              className={`input pl-12 pr-12 h-14 text-lg transition-all duration-300 ${
                isFocused ? 'border-accent-500/50 shadow-lg shadow-accent-500/10' : ''
              }`}
              autoFocus
              disabled={isLoading}
              autoComplete="current-password"
            />
            
            <motion.button
              type="button"
              className="absolute right-4 top-1/2 -translate-y-1/2 text-dark-500 hover:text-dark-300 transition-colors"
              onClick={() => setShowPassword(!showPassword)}
              whileHover={{ scale: 1.1 }}
              whileTap={{ scale: 0.9 }}
            >
              {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
            </motion.button>
          </div>
          
          <motion.button
            type="submit"
            disabled={isLoading || !password}
            className="btn btn-primary w-full justify-center h-14 text-lg relative overflow-hidden group"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            {/* Button shine effect */}
            <motion.div
              className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent"
              initial={{ x: '-100%' }}
              whileHover={{ x: '100%' }}
              transition={{ duration: 0.6 }}
            />
            
            {isLoading ? (
              <motion.div
                animate={{ rotate: 360 }}
                transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
              >
                <Loader2 className="w-6 h-6" />
              </motion.div>
            ) : (
              <span className="relative z-10 font-semibold">{t('login.sign_in')}</span>
            )}
          </motion.button>
        </motion.form>
        
        {/* Footer */}
        <motion.p 
          className="text-center text-dark-500 text-sm mt-8 flex items-center justify-center gap-2"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.6 }}
        >
          <Lock className="w-3 h-3" />
          {t('login.secure_dashboard')}
        </motion.p>
      </motion.div>
    </div>
  )
}
