import { motion } from 'framer-motion'
import { Lock } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { Server } from '../../api/client'
import { useFAQStore } from '../../stores/faqStore'
import { describeAllowedDomains } from '../../utils/nodeCapabilities'

interface NodeRestrictedNoticeProps {
  server?: Pick<Server, 'node_capabilities'> | null
  /** closed — раздел закрыт целиком, readonly — доступен только просмотр */
  variant?: 'closed' | 'readonly'
  /** Узкая плашка для строк списков и вложенных карточек */
  compact?: boolean
}

/**
 * Заглушка на месте раздела, закрытого владельцем ноды.
 *
 * Фиолетовый, а не янтарный: янтарным в панели помечено «сломалось или
 * устарело», а здесь всё работает штатно — просто так настроено на сервере.
 */
export default function NodeRestrictedNotice({
  server,
  variant = 'closed',
  compact = false,
}: NodeRestrictedNoticeProps) {
  const { t } = useTranslation()
  const openFAQ = useFAQStore(state => state.open)
  const allowed = describeAllowedDomains(server, t)

  if (compact) {
    return (
      <div className="flex items-start gap-3 p-4 rounded-xl bg-purple/5 border border-purple/20">
        <Lock className="w-4 h-4 text-purple flex-shrink-0 mt-0.5" />
        <div className="text-sm min-w-0">
          <p className="text-dark-200 font-medium">
            {t(variant === 'readonly' ? 'node_caps.readonly_title' : 'node_caps.restricted_title')}
          </p>
          <p className="text-dark-400 mt-1">{t('node_caps.where')}</p>
        </div>
      </div>
    )
  }

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
        <Lock className="w-16 h-16 text-purple/70 mx-auto mb-4" />
      </motion.div>
      <h3 className="text-lg font-semibold text-dark-100 mb-2">
        {t(variant === 'readonly' ? 'node_caps.readonly_title' : 'node_caps.restricted_title')}
      </h3>
      <p className="text-dark-400 max-w-md mx-auto mb-2">
        {t('node_caps.restricted_body')}
      </p>
      <p className="text-dark-400 max-w-md mx-auto mb-2">
        {t('node_caps.allowed', { allowed })}
      </p>
      {/* Путь к файлу прямо здесь: справка открывается с начала статьи,
          а тултип на телефоне не показывается вовсе */}
      <p className="text-dark-500 text-sm max-w-md mx-auto mb-6 font-mono break-all">
        {t('node_caps.where')}
      </p>
      <div className="flex justify-center">
        <motion.button
          onClick={() => openFAQ('PAGE_SERVERS')}
          className="btn btn-secondary"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          {t('node_caps.restricted_action')}
        </motion.button>
      </div>
    </motion.div>
  )
}
