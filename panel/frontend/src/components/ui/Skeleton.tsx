import { motion } from 'framer-motion'

interface SkeletonProps {
  className?: string
}

export function Skeleton({ className = '' }: SkeletonProps) {
  return (
    <motion.div
      className={`bg-dark-700/50 rounded-lg ${className}`}
      animate={{ opacity: [0.3, 0.6, 0.3] }}
      transition={{ duration: 1.5, repeat: Infinity, ease: 'easeInOut' }}
    />
  )
}

export function ServerCardSkeleton({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return (
      <div className="card p-4">
        <div className="flex items-center gap-4">
          <Skeleton className="w-10 h-10 rounded-xl" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-3 w-24" />
          </div>
          <div className="flex items-center gap-3">
            <Skeleton className="h-4 w-16" />
            <Skeleton className="h-4 w-16" />
            <Skeleton className="h-4 w-16" />
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="card p-5">
      <div className="flex items-center gap-3 mb-4">
        <Skeleton className="w-10 h-10 rounded-xl" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-4 w-28" />
          <Skeleton className="h-3 w-20" />
        </div>
        <Skeleton className="h-5 w-14 rounded-full" />
      </div>
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <Skeleton className="h-3 w-10" />
          <Skeleton className="h-3 w-12" />
        </div>
        <Skeleton className="h-2 w-full rounded-full" />
        <div className="flex items-center justify-between">
          <Skeleton className="h-3 w-10" />
          <Skeleton className="h-3 w-12" />
        </div>
        <Skeleton className="h-2 w-full rounded-full" />
        <div className="flex items-center justify-between">
          <Skeleton className="h-3 w-10" />
          <Skeleton className="h-3 w-12" />
        </div>
        <Skeleton className="h-2 w-full rounded-full" />
      </div>
    </div>
  )
}

export function MetricCardSkeleton() {
  return (
    <div className="card">
      <div className="flex items-center gap-2 mb-3">
        <Skeleton className="w-5 h-5 rounded" />
        <Skeleton className="h-4 w-16" />
      </div>
      <Skeleton className="h-10 w-20 mb-3" />
      <Skeleton className="h-2 w-full rounded-full mb-3" />
      <Skeleton className="h-3 w-28" />
    </div>
  )
}

export function ChartSkeleton() {
  return (
    <div className="card">
      <div className="flex items-center gap-2 mb-4">
        <Skeleton className="w-4 h-4 rounded" />
        <Skeleton className="h-4 w-32" />
      </div>
      <Skeleton className="h-[250px] w-full rounded-xl" />
    </div>
  )
}
