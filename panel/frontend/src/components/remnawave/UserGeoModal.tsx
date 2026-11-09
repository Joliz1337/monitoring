import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { motion } from 'framer-motion'
import { User, X } from 'lucide-react'
import { remnawaveApi, type RemnawaveUserDetails } from '../../api/client'
import UserGeoDetails from './UserGeoDetails'

interface UserGeoModalProps {
  userId: number
  onClose: () => void
}

// Карточка пользователя поверх карты: работает и для тех, кто сейчас не в сети
// (их нет в списке вкладки «Пользователи», но есть в истории IP)
export default function UserGeoModal({ userId, onClose }: UserGeoModalProps) {
  const { t } = useTranslation()
  const [details, setDetails] = useState<RemnawaveUserDetails | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    setDetails(null)
    setFailed(false)
    remnawaveApi.getUserStats(userId)
      .then(res => { if (alive) setDetails(res.data) })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false }
  }, [userId])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <motion.div
        className="card !bg-dark-950 w-full max-w-5xl max-h-[90vh] overflow-y-auto"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
            <User className="w-5 h-5 text-accent-500" />
            {details?.username || `#${userId}`}
            {details?.username && <span className="text-dark-500 text-sm font-normal">ID: {userId}</span>}
            {details?.status && <span className="text-xs font-medium text-dark-400">{details.status}</span>}
          </h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-200" aria-label={t('common.close')}>
            <X className="w-5 h-5" />
          </button>
        </div>
        {failed ? (
          <div className="text-dark-500 text-sm">{t('remnawave.noData')}</div>
        ) : details ? (
          <UserGeoDetails details={details} />
        ) : (
          <div className="space-y-2">
            {[1, 2, 3].map(i => <div key={i} className="h-8 bg-dark-700/50 rounded-lg animate-pulse" />)}
          </div>
        )}
      </motion.div>
    </div>
  )
}
