import { motion } from 'framer-motion'
import { TAB_MOTION, TAB_GRID_TWO } from './SettingsSection'
import { BackupCard } from './BackupCard'
import AutoBackupCard from './AutoBackupCard'

export function BackupsTab() {
  return (
    <motion.div {...TAB_MOTION} className={TAB_GRID_TWO}>
      <BackupCard />
      <AutoBackupCard />
    </motion.div>
  )
}
