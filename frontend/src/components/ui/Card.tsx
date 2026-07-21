import type { HTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('rounded-2xl border border-[#eef2f7] bg-white shadow-[0_8px_30px_rgba(37,99,235,0.06)]', className)} {...props} />
}
