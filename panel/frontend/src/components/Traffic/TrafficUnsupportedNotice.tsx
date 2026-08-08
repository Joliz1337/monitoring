import { motion } from 'framer-motion'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowUpCircle, PackageOpen } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface TrafficUnsupportedNoticeProps {
  nodeVersion: string | null
  minVersion: string
}

export default function TrafficUnsupportedNotice({ nodeVersion, minVersion }: TrafficUnsupportedNoticeProps) {
  const { uid } = useParams()
  const navigate = useNavigate()
  const { t } = useTranslation()

  return (
    <motion.div
      className="card text-center py-16"
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
    >
      <motion.div
        animate={{ y: [0, -5, 0] }}
        transition={{ duration: 3, repeat: Infinity }}
      >
        <PackageOpen className="w-16 h-16 text-warning/70 mx-auto mb-4" />
      </motion.div>
      <h3 className="text-lg font-semibold text-dark-100 mb-2">
        {t('traffic.unsupported_title')}
      </h3>
      <p className="text-dark-400 max-w-md mx-auto mb-6">
        {t('traffic.unsupported_hint', {
          min: minVersion,
          current: nodeVersion || t('traffic.unsupported_version_unknown'),
        })}
      </p>
      <div className="flex justify-center">
        <motion.button
          onClick={() => navigate(`/${uid}/updates`)}
          className="btn btn-primary"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          <ArrowUpCircle className="w-4 h-4" />
          {t('traffic.unsupported_action')}
        </motion.button>
      </div>
    </motion.div>
  )
}
