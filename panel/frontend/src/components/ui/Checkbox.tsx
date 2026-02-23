import { Check } from 'lucide-react'
import { MouseEvent } from 'react'

interface CheckboxProps {
  checked?: boolean
  onChange?: (e: React.ChangeEvent<HTMLInputElement>) => void
  onClick?: (e: MouseEvent) => void
  disabled?: boolean
  className?: string
  size?: 'sm' | 'md'
}

export function Checkbox({ checked, onChange, disabled, className = '', size = 'sm', onClick }: CheckboxProps) {
  const sizeClasses = size === 'md' ? 'w-5 h-5' : 'w-[18px] h-[18px]'
  const iconSize = size === 'md' ? 14 : 12

  return (
    <span
      className={`relative inline-flex items-center shrink-0 ${className}`}
      onClick={onClick}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        disabled={disabled}
        className="absolute inset-0 w-full h-full opacity-0 cursor-pointer peer"
      />
      <span className={`${sizeClasses} rounded-[5px] border transition-all duration-200 flex items-center justify-center pointer-events-none
        ${checked
          ? 'bg-accent-500 border-accent-500 shadow-[0_0_8px_rgba(6,182,212,0.3)]'
          : 'bg-dark-800/60 border-dark-600 peer-hover:border-dark-500'
        }
        ${disabled ? 'opacity-50' : ''}
        peer-focus-visible:ring-2 peer-focus-visible:ring-accent-500/40
      `}>
        {checked && <Check size={iconSize} className="text-white" strokeWidth={3} />}
      </span>
    </span>
  )
}
