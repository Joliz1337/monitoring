import { useState } from 'react'
import { motion } from 'framer-motion'
import { TAB_MOTION, TAB_GRID_TWO } from './SettingsSection'
import { BackupCard } from './BackupCard'
import AutoBackupCard from './AutoBackupCard'

export function BackupsTab() {
  // Пароль архива задаётся в карточке автобэкапов, а шифрует и локальные бэкапы.
  // null — настройки ещё не пришли: о шифровании пока нечего сказать.
  const [archivePasswordSet, setArchivePasswordSet] = useState<boolean | null>(null)

  return (
    <motion.div {...TAB_MOTION} className={TAB_GRID_TWO}>
      <BackupCard archivePasswordSet={archivePasswordSet} />
      <AutoBackupCard onArchivePassword={setArchivePasswordSet} />
    </motion.div>
  )
}
