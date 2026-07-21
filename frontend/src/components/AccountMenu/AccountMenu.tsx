import { ArrowLeft, Check, ChevronRight, CircleHelp, LogOut, Plus, Settings } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import type { User } from '../../types'

type AccountMenuProps = {
  open: boolean
  user: User
  displayName: string
  triggerRef: RefObject<HTMLButtonElement>
  onClose: () => void
  onHelp: () => void
  onSettings: () => void
  onLogout: () => void
  onAddAccount: () => void
}

type MenuPosition = { left: number; top: number; width: number }

export default function AccountMenu({ open, user, displayName, triggerRef, onClose, onHelp, onSettings, onLogout, onAddAccount }: AccountMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null)
  const switcherRef = useRef<HTMLDivElement>(null)
  const [switcherOpen, setSwitcherOpen] = useState(false)
  const [mobile, setMobile] = useState(false)
  const [position, setPosition] = useState<MenuPosition>({ left: 8, top: 8, width: 240 })

  const closeSwitcher = useCallback(() => {
    setSwitcherOpen(false)
    window.setTimeout(() => menuRef.current?.querySelector<HTMLElement>('[aria-haspopup="menu"]')?.focus(), 0)
  }, [])

  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return
    const rect = trigger.getBoundingClientRect()
    const isMobile = window.innerWidth < 640
    const width = isMobile ? window.innerWidth - 16 : 240
    const estimatedHeight = isMobile ? 230 : 218
    const left = isMobile ? 8 : Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))
    const top = Math.max(8, Math.min(rect.top - estimatedHeight - 8, window.innerHeight - estimatedHeight - 8))
    setMobile(isMobile)
    setPosition({ left, top, width })
  }, [triggerRef])

  useEffect(() => {
    if (!open) {
      setSwitcherOpen(false)
      return
    }

    updatePosition()
    window.addEventListener('resize', updatePosition)
    const outside = (event: PointerEvent) => {
      const target = event.target as Node
      if (menuRef.current?.contains(target) || switcherRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      onClose()
    }
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      if (switcherOpen) closeSwitcher()
      else onClose()
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => {
      window.removeEventListener('resize', updatePosition)
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('keydown', escape)
    }
  }, [closeSwitcher, onClose, open, switcherOpen, triggerRef, updatePosition])

  useEffect(() => {
    if (!open) return
    const timer = window.setTimeout(() => menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus(), 0)
    return () => window.clearTimeout(timer)
  }, [open])

  useEffect(() => {
    if (!switcherOpen) return
    const timer = window.setTimeout(() => switcherRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus(), 0)
    return () => window.clearTimeout(timer)
  }, [switcherOpen])

  const keyboardNavigation = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    const currentMenu = (event.target as HTMLElement).closest('[role="menu"]')
    if (!currentMenu) return
    const items = Array.from(currentMenu.querySelectorAll<HTMLElement>('[role="menuitem"]'))
    const currentIndex = items.indexOf(document.activeElement as HTMLElement)
    const nextIndex = event.key === 'ArrowDown' ? (currentIndex + 1) % items.length : (currentIndex - 1 + items.length) % items.length
    items[nextIndex]?.focus()
  }

  const select = (action: () => void) => {
    onClose()
    action()
  }

  if (!open) return null

  const switcher = (
    <motion.div
      ref={switcherRef}
      role="menu"
      aria-label="Account switcher"
      onKeyDown={keyboardNavigation}
      initial={{ opacity: 0, x: mobile ? 0 : -6, y: mobile ? 6 : 0 }}
      animate={{ opacity: 1, x: 0, y: 0 }}
      exit={{ opacity: 0, x: mobile ? 0 : -4, y: mobile ? 4 : 0 }}
      transition={{ duration: .16 }}
      className="fixed z-[110] rounded-[14px] border border-[#e6ecf5] bg-white/95 p-1.5 text-slate-900 shadow-[0_16px_40px_rgba(37,99,235,.12)] backdrop-blur-xl"
      style={mobile ? { left: position.left, top: position.top, width: position.width } : { left: Math.min(position.left + position.width + 8, window.innerWidth - 288), top: position.top, width: 280 }}
    >
      {mobile && <button type="button" role="menuitem" onClick={closeSwitcher} className="flex h-10 w-full items-center gap-2 rounded-[9px] px-2.5 text-[12px] font-medium hover:bg-[#f3f7ff] hover:text-blue-600"><ArrowLeft size={16} />Back</button>}
      <p className="truncate px-2.5 py-2 text-[11px] font-medium text-slate-500">{user.id}</p>
      <div className="h-px bg-[#e6ecf5]" />
      <button type="button" role="menuitem" aria-current="true" className="mt-1 flex min-h-[54px] w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left hover:bg-[#f3f7ff]">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-gradient-to-br from-blue-600 to-indigo-500 text-[11px] font-semibold text-white">{user.initials}</span>
        <span className="min-w-0 flex-1"><span className="block truncate text-[12px] font-semibold">{displayName}</span><span className="block text-[10px] text-slate-500">Current account</span></span>
        <Check size={16} className="shrink-0 text-blue-600" />
      </button>
      <div className="my-1 h-px bg-[#e6ecf5]" />
      <button type="button" role="menuitem" onClick={() => select(onAddAccount)} className="flex h-[42px] w-full items-center gap-2.5 rounded-[9px] px-2.5 text-left text-[12px] font-medium hover:bg-[#f3f7ff] hover:text-blue-600"><Plus size={16} />Add another account</button>
    </motion.div>
  )

  return createPortal(
    <>
      <AnimatePresence>
        {(!mobile || !switcherOpen) && (
          <motion.div
            ref={menuRef}
            role="menu"
            aria-label="Account menu"
            onKeyDown={keyboardNavigation}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 4 }}
            transition={{ duration: .16 }}
            className="fixed z-[100] rounded-[14px] border border-[#e6ecf5] bg-white/95 p-1.5 text-slate-900 shadow-[0_16px_40px_rgba(37,99,235,.12)] backdrop-blur-xl"
            style={position}
          >
            <button type="button" role="menuitem" aria-haspopup="menu" aria-expanded={switcherOpen} onClick={() => switcherOpen ? closeSwitcher() : setSwitcherOpen(true)} className="flex min-h-[58px] w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left hover:bg-[#f3f7ff]">
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-gradient-to-br from-blue-600 to-indigo-500 text-[11px] font-semibold text-white">{user.initials}</span>
              <span className="min-w-0 flex-1"><span className="block truncate text-[12px] font-semibold">{displayName}</span><span className="block truncate text-[10px] text-slate-500">{user.id}</span></span>
              <ChevronRight size={15} className="shrink-0 text-slate-400" />
            </button>
            <div className="my-1 h-px bg-[#e6ecf5]" />
            <button type="button" role="menuitem" onClick={() => select(onHelp)} className="flex h-[42px] w-full items-center gap-2.5 rounded-[9px] px-2.5 text-left text-[12px] font-medium hover:bg-[#f3f7ff] hover:text-blue-600"><CircleHelp size={16} />Help</button>
            <button type="button" role="menuitem" onClick={() => select(onSettings)} className="flex h-[42px] w-full items-center gap-2.5 rounded-[9px] px-2.5 text-left text-[12px] font-medium hover:bg-[#f3f7ff] hover:text-blue-600"><Settings size={16} />Settings</button>
            <div className="my-1 h-px bg-[#e6ecf5]" />
            <button type="button" role="menuitem" onClick={() => select(onLogout)} className="flex h-[42px] w-full items-center gap-2.5 rounded-[9px] px-2.5 text-left text-[12px] font-medium hover:bg-slate-100 hover:text-red-600"><LogOut size={16} />Logout</button>
          </motion.div>
        )}
      </AnimatePresence>
      <AnimatePresence>{switcherOpen && switcher}</AnimatePresence>
    </>,
    document.body,
  )
}
