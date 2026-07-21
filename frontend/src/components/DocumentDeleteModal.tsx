import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { Trash2 } from 'lucide-react'
import { useEffect, useId, useRef } from 'react'

interface DocumentDeleteModalProps {
  open: boolean
  documentName: string
  isDeleting: boolean
  error: string
  onCancel: () => void
  onConfirm: () => void
}

export default function DocumentDeleteModal({ open, documentName, isDeleting, error, onCancel, onConfirm }: DocumentDeleteModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelButtonRef = useRef<HTMLButtonElement>(null)
  const titleId = useId()
  const descriptionId = useId()
  const reducedMotion = useReducedMotion()

  useEffect(() => {
    if (!open) return
    cancelButtonRef.current?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        if (!isDeleting) onCancel()
        return
      }

      if (event.key !== 'Tab') return
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown, true)
    return () => document.removeEventListener('keydown', handleKeyDown, true)
  }, [isDeleting, onCancel, open])

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[9999] flex items-center justify-center bg-slate-900/40 p-3 backdrop-blur-[2px] sm:p-5"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reducedMotion ? 0 : 0.16 }}
          onMouseDown={event => {
            if (event.target === event.currentTarget && !isDeleting) onCancel()
          }}
        >
          <motion.div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            aria-describedby={descriptionId}
            className="w-full max-w-[430px] rounded-[18px] border border-[#e5e7eb] bg-white p-5 shadow-[0_24px_60px_rgba(15,23,42,.22)] sm:p-6"
            initial={{ opacity: 0, scale: reducedMotion ? 1 : 0.97, y: reducedMotion ? 0 : 6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: reducedMotion ? 1 : 0.97, y: reducedMotion ? 0 : 6 }}
            transition={{ duration: reducedMotion ? 0 : 0.18, ease: 'easeOut' }}
            onMouseDown={event => event.stopPropagation()}
          >
            <div className="grid h-[42px] w-[42px] place-items-center rounded-xl bg-[#fef2f2] text-[#dc2626]">
              <Trash2 size={20} />
            </div>
            <h2 id={titleId} className="mt-3.5 text-[18px] font-bold tracking-[-.02em] text-slate-900">Delete document?</h2>
            <div id={descriptionId} className="mt-2 text-[14px] leading-[1.6] text-slate-500">
              <p>Are you sure you want to delete <strong className="font-semibold text-slate-900 [overflow-wrap:anywhere]">“{documentName}”</strong>?</p>
              <p className="mt-2">This action cannot be undone.</p>
            </div>

            {error && <p role="alert" className="mt-4 rounded-[10px] bg-red-50 px-3 py-2.5 text-[12px] leading-5 text-red-700">{error}</p>}
            <span className="sr-only" aria-live="polite">{isDeleting ? 'Deleting document' : ''}</span>

            <div className="mt-6 flex justify-end gap-2.5">
              <button ref={cancelButtonRef} type="button" disabled={isDeleting} onClick={onCancel} className="h-10 rounded-[10px] border border-[#dce3ec] bg-white px-4 text-[13px] font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60">Cancel</button>
              <button type="button" disabled={isDeleting} onClick={onConfirm} className="h-10 min-w-[78px] rounded-[10px] bg-[#dc2626] px-4 text-[13px] font-semibold text-white hover:bg-[#b91c1c] disabled:cursor-not-allowed disabled:opacity-60">{isDeleting ? 'Deleting...' : 'Delete'}</button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
