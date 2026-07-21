import { ChevronDown, FileUp, MessageSquarePlus, Sparkles, X } from 'lucide-react'
import { motion } from 'framer-motion'
import { useRef, useState } from 'react'
import { useApp } from '../context/AppContext'
import { cn } from '../utils/cn'
import AccountMenu from './AccountMenu/AccountMenu'
import ChatHistory from './ChatHistory'
import { Button } from './ui/Button'

type SidebarProps = {
  onClose: () => void
  onUpload: () => void
  onSettings: () => void
  onHelp: () => void
}

export default function Sidebar({ onClose, onUpload, onSettings, onHelp }: SidebarProps) {
  const { sidebarOpen, newChat, view, user, logout } = useApp()
  const [accountMenuOpen, setAccountMenuOpen] = useState(false)
  const profileRef = useRef<HTMLButtonElement>(null)
  const email = user.id
  const displayName = email.split('@')[0].split(/[._-]/).filter(Boolean).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(' ') || 'User'
  const nav = [
    { label: 'New Chat', icon: MessageSquarePlus, onClick: newChat, active: view === 'chat' },
    { label: 'Documents', icon: FileUp, onClick: onUpload, active: view === 'library' },
  ]

  return (
    <>
      <div aria-hidden={!sidebarOpen} onClick={onClose} className={cn('fixed inset-0 z-40 bg-black/30 transition-opacity duration-200 lg:hidden', sidebarOpen ? 'opacity-100' : 'pointer-events-none opacity-0')} />
      <motion.aside
        aria-label="Application sidebar"
        initial={false}
        className={cn('fixed inset-y-0 left-0 z-50 flex w-[260px] flex-col border-r border-[#e6ecf5] bg-[#fcfcfd] transition-transform duration-200 lg:static lg:translate-x-0', sidebarOpen ? 'translate-x-0' : '-translate-x-full')}
      >
        <div className="flex h-16 shrink-0 items-center gap-2.5 px-3">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-gradient-to-br from-blue-600 to-indigo-500 text-white shadow-[0_6px_18px_rgba(37,99,235,.28)]"><Sparkles size={17} /></span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-[14px] font-semibold text-slate-900">Simple RAG</p>
            <p className="truncate text-[11px] text-slate-500">Secure Document Assistant</p>
          </div>
          <Button variant="ghost" size="icon" className="h-9 w-9 lg:hidden" onClick={onClose} aria-label="Close sidebar"><X size={17} /></Button>
        </div>

        <nav aria-label="Primary navigation" className="shrink-0 space-y-0.5 px-2 pb-3">
          {nav.map(({ label, icon: Icon, onClick, active }) => (
            <button
              key={label}
              type="button"
              aria-current={active ? 'page' : undefined}
              onClick={() => { onClick(); if (label !== 'New Chat') onClose() }}
              className={cn('flex h-10 w-full items-center gap-2.5 rounded-xl px-3 text-left text-[13px] font-medium text-slate-600 hover:bg-[#f3f7ff] hover:text-blue-600', active && 'bg-[#eef4ff] text-blue-600 shadow-[0_2px_8px_rgba(37,99,235,.05)]')}
            >
              <Icon size={17} strokeWidth={1.8} />
              {label}
            </button>
          ))}
        </nav>

        <div className="min-h-0 flex-1 overflow-y-auto border-t border-[#e6ecf5] px-2 py-3">
          <ChatHistory />
        </div>

        <div className="shrink-0 border-t border-[#e6ecf5] p-2">
          <button ref={profileRef} type="button" onClick={() => setAccountMenuOpen(current => !current)} aria-haspopup="menu" aria-expanded={accountMenuOpen} className={cn('flex min-h-[60px] w-full items-center gap-2.5 rounded-xl px-3 py-2 text-left hover:bg-[#f3f7ff]', accountMenuOpen && 'bg-[#eef4ff]')} aria-label="Open account menu">
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-gradient-to-br from-blue-600 to-indigo-500 text-[11px] font-semibold text-white shadow-sm">{user.initials}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[12px] font-semibold text-slate-900">{displayName}</span>
              <span title={email} className="block truncate text-[10px] text-slate-500">{email}</span>
            </span>
            <ChevronDown size={14} className="shrink-0 text-slate-400" />
          </button>
        </div>
      </motion.aside>
      <AccountMenu open={accountMenuOpen} user={user} displayName={displayName} triggerRef={profileRef} onClose={() => setAccountMenuOpen(false)} onHelp={onHelp} onSettings={onSettings} onLogout={logout} onAddAccount={logout} />
    </>
  )
}
