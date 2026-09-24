import { MemoryStick } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { NumaNode } from '../../api/client'
import { formatBytes } from '../../utils/format'

// Во сколько раз память одного процессора может превышать память другого,
// прежде чем раскладку считать неправильной
const IMBALANCE_RATIO = 1.5

// Узлы без ядер (память на CXL-расширителях) в сравнение не берём —
// у них своей памяти и должно быть сколько угодно
function findNumaImbalance(nodes: NumaNode[] | undefined): NumaNode[] | null {
  const socketNodes = (nodes ?? []).filter(node => node.cpus > 0)
  if (socketNodes.length < 2) return null

  const sizes = socketNodes.map(node => node.memory_total)
  const smallest = Math.min(...sizes)
  const largest = Math.max(...sizes)
  return largest >= smallest * IMBALANCE_RATIO ? socketNodes : null
}

export default function NumaMemoryWarning({ nodes }: { nodes: NumaNode[] | undefined }) {
  const { t } = useTranslation()
  const imbalanced = findNumaImbalance(nodes)
  if (!imbalanced) return null

  return (
    <div className="flex items-start gap-3 p-4 bg-warning/10 border border-warning/30 rounded-xl mb-6">
      <div className="flex-shrink-0 p-2 bg-warning/20 rounded-lg">
        <MemoryStick className="w-5 h-5 text-warning" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-warning font-medium">{t('server_details.numa_imbalance_title')}</p>
        <p className="mt-1 text-sm text-dark-300">{t('server_details.numa_imbalance_hint')}</p>
        <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-sm font-mono text-dark-200">
          {imbalanced.map(node => (
            <span key={node.node}>
              {t('server_details.numa_node', { node: node.node, cpus: node.cpus })}: {formatBytes(node.memory_total)}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}
