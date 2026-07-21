import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '../../utils/cn'

const buttonVariants = cva('inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[10px] text-sm font-semibold transition-colors duration-150 disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2', {
  variants: {
    variant: {
      primary: 'bg-gradient-to-br from-blue-600 to-indigo-500 text-white shadow-[0_4px_14px_rgba(37,99,235,.2)] hover:-translate-y-0.5 hover:shadow-[0_7px_18px_rgba(37,99,235,.25)]',
      secondary: 'border border-[#e6ecf5] bg-white text-slate-700 shadow-[0_2px_8px_rgba(15,23,42,.03)] hover:-translate-y-0.5 hover:bg-[#f5f9ff] hover:text-blue-600',
      ghost: 'text-slate-500 hover:bg-[#f1f5f9] hover:text-blue-600',
      danger: 'text-red-600 hover:bg-red-50',
    },
    size: { sm: 'h-9 px-3', md: 'h-11 px-4', icon: 'h-10 w-10 p-0' },
  },
  defaultVariants: { variant: 'primary', size: 'md' },
})

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof buttonVariants>
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, ...props }, ref) => (
  <button ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />
))
Button.displayName = 'Button'
