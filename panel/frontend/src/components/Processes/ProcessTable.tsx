import { useState, useMemo } from 'react'
import { ChevronUp, ChevronDown, Search, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'

interface Process {
  pid: number
  name: string
  cpu_percent: number
  memory_percent: number
  status: string
}

interface ProcessTableProps {
  processes: Process[]
  className?: string
}

type SortField = 'pid' | 'name' | 'cpu_percent' | 'memory_percent' | 'status'
type SortDirection = 'asc' | 'desc'

const statusPriority: Record<string, number> = {
  running: 1,
  sleeping: 2,
  idle: 3,
  stopped: 4,
  zombie: 5,
}

const statusColors: Record<string, string> = {
  running: 'text-success',
  sleeping: 'text-dark-400',
  stopped: 'text-warning',
  zombie: 'text-danger',
  idle: 'text-dark-500',
}

export default function ProcessTable({ processes, className = '' }: ProcessTableProps) {
  const { t } = useTranslation()
  const [sortField, setSortField] = useState<SortField>('cpu_percent')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')
  const [searchQuery, setSearchQuery] = useState('')
  
  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortDirection(field === 'name' ? 'asc' : 'desc')
    }
  }
  
  const filteredAndSorted = useMemo(() => {
    let result = [...processes]
    
    // Filter by search
    if (searchQuery) {
      const query = searchQuery.toLowerCase()
      result = result.filter(p => 
        p.name.toLowerCase().includes(query) || 
        p.pid.toString().includes(query)
      )
    }
    
    // Sort
    result.sort((a, b) => {
      let comparison = 0
      
      switch (sortField) {
        case 'pid':
          comparison = a.pid - b.pid
          break
        case 'name':
          comparison = a.name.localeCompare(b.name)
          break
        case 'cpu_percent':
          comparison = a.cpu_percent - b.cpu_percent
          break
        case 'memory_percent':
          comparison = a.memory_percent - b.memory_percent
          break
        case 'status':
          comparison = (statusPriority[a.status] || 99) - (statusPriority[b.status] || 99)
          break
      }
      
      return sortDirection === 'asc' ? comparison : -comparison
    })
    
    return result
  }, [processes, sortField, sortDirection, searchQuery])
  
  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return null
    return sortDirection === 'asc' 
      ? <ChevronUp className="w-4 h-4" />
      : <ChevronDown className="w-4 h-4" />
  }
  
  const headerClass = (field: SortField) => `
    flex items-center gap-1 cursor-pointer select-none transition-colors
    ${sortField === field ? 'text-accent-400' : 'text-dark-400 hover:text-dark-200'}
  `
  
  return (
    <div className={className}>
      {/* Filters */}
      <div className="space-y-2 mb-3">
        {/* Search */}
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-dark-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t('process_table.search_placeholder')}
            className="input pl-9 pr-9 py-1.5 text-sm w-full"
          />
          {searchQuery && (
            <button
              onClick={() => setSearchQuery('')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-dark-500 hover:text-dark-300"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
        
        {/* Sort buttons */}
        <div className="flex flex-wrap bg-dark-800 rounded-lg p-0.5">
          {(['cpu_percent', 'memory_percent', 'status', 'pid'] as SortField[]).map((field) => (
            <button
              key={field}
              onClick={() => handleSort(field)}
              className={`px-2 py-1 text-xs rounded transition-all flex items-center gap-0.5 ${
                sortField === field
                  ? 'bg-dark-700 text-accent-400'
                  : 'text-dark-400 hover:text-dark-200'
              }`}
            >
              {field === 'cpu_percent' && t('process_table.cpu')}
              {field === 'memory_percent' && t('process_table.ram')}
              {field === 'status' && t('process_table.status')}
              {field === 'pid' && t('process_table.pid')}
              {sortField === field && (
                sortDirection === 'asc' 
                  ? <ChevronUp className="w-3 h-3" />
                  : <ChevronDown className="w-3 h-3" />
              )}
            </button>
          ))}
        </div>
      </div>
      
      {/* Table */}
      <div className="overflow-hidden rounded-lg border border-dark-700">
        <div className="max-h-[220px] overflow-y-auto">
          <table className="w-full">
            <thead className="sticky top-0 bg-dark-900 z-10">
              <tr className="border-b border-dark-700">
                <th className="text-left py-2 px-2 w-16">
                  <button className={headerClass('pid')} onClick={() => handleSort('pid')}>
                    {t('process_table.pid')} <SortIcon field="pid" />
                  </button>
                </th>
                <th className="text-left py-2 px-2">
                  <button className={headerClass('name')} onClick={() => handleSort('name')}>
                    {t('process_table.name')} <SortIcon field="name" />
                  </button>
                </th>
                <th className="text-right py-2 px-2 w-20">
                  <button className={`${headerClass('cpu_percent')} justify-end ml-auto`} onClick={() => handleSort('cpu_percent')}>
                    {t('process_table.cpu')} <SortIcon field="cpu_percent" />
                  </button>
                </th>
                <th className="text-right py-2 px-2 w-20">
                  <button className={`${headerClass('memory_percent')} justify-end ml-auto`} onClick={() => handleSort('memory_percent')}>
                    {t('process_table.ram')} <SortIcon field="memory_percent" />
                  </button>
                </th>
                <th className="text-right py-2 px-2 w-20">
                  <button className={`${headerClass('status')} justify-end ml-auto`} onClick={() => handleSort('status')}>
                    {t('process_table.status')} <SortIcon field="status" />
                  </button>
                </th>
              </tr>
            </thead>
            <tbody>
              <AnimatePresence>
                {filteredAndSorted.map((proc) => (
                  <motion.tr 
                    key={proc.pid}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.2 }}
                    className="border-b border-dark-800 hover:bg-dark-800/50 transition-colors"
                  >
                    <td className="py-2 px-2 font-mono text-dark-500 text-sm">
                      {proc.pid}
                    </td>
                    <td className="py-2 px-2 text-dark-200 truncate max-w-[150px] text-sm" title={proc.name}>
                      {proc.name}
                    </td>
                    <td className="py-2 px-2 text-right font-mono text-sm">
                      <span className={
                        sortField === 'cpu_percent'
                          ? (proc.cpu_percent >= 80 ? 'text-danger' : proc.cpu_percent >= 50 ? 'text-warning' : 'text-accent-400')
                          : 'text-dark-400'
                      }>
                        {proc.cpu_percent.toFixed(1)}%
                      </span>
                    </td>
                    <td className="py-2 px-2 text-right font-mono text-sm">
                      <span className={
                        sortField === 'memory_percent'
                          ? (proc.memory_percent >= 80 ? 'text-danger' : proc.memory_percent >= 50 ? 'text-warning' : 'text-accent-400')
                          : 'text-dark-400'
                      }>
                        {proc.memory_percent.toFixed(1)}%
                      </span>
                    </td>
                    <td className="py-2 px-2 text-right text-xs">
                      <span className={
                        sortField === 'status'
                          ? (statusColors[proc.status] || 'text-accent-400')
                          : (statusColors[proc.status] || 'text-dark-400')
                      }>
                        {proc.status}
                      </span>
                    </td>
                  </motion.tr>
                ))}
              </AnimatePresence>
            </tbody>
          </table>
        </div>
        
        {filteredAndSorted.length === 0 && (
          <div className="text-center py-6 text-dark-500 text-sm">
            {searchQuery ? t('process_table.no_matches') : t('process_table.no_processes')}
          </div>
        )}
      </div>
      
      {/* Footer */}
      <div className="mt-3 text-sm text-dark-500">
        {t('process_table.showing_stats', { filtered: filteredAndSorted.length, total: processes.length })}
      </div>
    </div>
  )
}
