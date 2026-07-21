import { X } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { Button } from './Button'
import type { ReactNode } from 'react'

export function Modal({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  useEffect(()=>{if(!open)return;const close=(event:KeyboardEvent)=>{if(event.key==='Escape')onClose()};window.addEventListener('keydown',close);return()=>window.removeEventListener('keydown',close)},[open,onClose])
  return createPortal(<AnimatePresence>{open && <motion.div className="fixed inset-0 z-[80] grid place-items-center bg-slate-900/25 p-3 backdrop-blur-[3px] sm:p-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={onClose}><motion.div role="dialog" aria-modal="true" aria-labelledby="modal-title" className="max-h-[calc(100vh-24px)] w-full max-w-lg overflow-y-auto rounded-[18px] border border-[#e6ecf5] bg-white/95 p-5 shadow-[0_24px_70px_rgba(15,23,42,.16)] backdrop-blur-xl sm:p-6" initial={{ opacity: 0, y: 12, scale: .99 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: .99 }} transition={{ duration: .2 }} onMouseDown={e => e.stopPropagation()}><div className="flex items-center justify-between gap-3"><h2 id="modal-title" className="text-[17px] font-bold tracking-[-.02em] text-slate-900">{title}</h2><Button variant="ghost" size="icon" className="h-9 w-9" onClick={onClose} aria-label="Close dialog"><X size={18} /></Button></div><div className="mt-5">{children}</div></motion.div></motion.div>}</AnimatePresence>, document.body)
}
