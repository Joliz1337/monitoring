import { motion } from 'framer-motion'
import { TAB_MOTION } from './SettingsSection'
import { BackupCard } from './BackupCard'
import AutoBackupCard from './AutoBackupCard'

export function BackupsTab() {
  return (
    <motion.div {...TAB_MOTION} className="space-y-6">
      <BackupCard />
      <AutoBackupCard />
    </motion.div>
  )
}
