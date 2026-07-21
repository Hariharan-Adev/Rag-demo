import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { Bell, CheckCheck, ChevronDown, CircleHelp, LogOut, Menu, Moon, Settings, Sparkles, Sun, User } from 'lucide-react'
import { useApp } from '../context/AppContext'
import { Button } from './ui/Button'
import { Tooltip } from './ui/Tooltip'

export default function TopNavbar({ onSettings, onHelp }: { onSettings: () => void; onHelp: () => void }) {
  const { setSidebarOpen, user, theme, toggleTheme, notifications, markNotificationsRead, showToast, logout } = useApp()
  const unread = notifications.filter(notification => !notification.read).length

  return (
    <header className="sticky top-0 z-40 flex h-[70px] shrink-0 items-center border-b border-slate-200 bg-white/95 px-4 backdrop-blur-xl lg:px-6">
      <Button variant="ghost" size="icon" className="mr-2 lg:hidden" aria-label="Open menu" onClick={() => setSidebarOpen(true)}><Menu size={20} /></Button>
      <div className="flex min-w-0 items-center gap-3 lg:w-[464px]">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-[13px] bg-gradient-to-br from-blue-600 to-indigo-600 text-white shadow-lg shadow-blue-200"><Sparkles size={20} /></div>
        <div className="hidden min-w-0 sm:block">
          <p className="truncate text-sm font-bold text-slate-900">Docsense AI</p>
          <p className="text-[11px] text-slate-500">Knowledge AI Assistant</p>
        </div>
      </div>
      <div className="min-w-0 flex-1 text-center">
        <h1 className="truncate text-base font-bold tracking-tight text-slate-900 sm:text-lg">Docsense AI Assistant</h1>
        <p className="hidden text-xs text-slate-500 sm:block">Ask questions about your uploaded documents.</p>
      </div>
      <div className="flex items-center justify-end gap-1 sm:gap-2 lg:w-[340px]">
        <Tooltip label={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}>
          <Button variant="ghost" size="icon" aria-label="Toggle theme" onClick={toggleTheme}>{theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}</Button>
        </Tooltip>
        <DropdownMenu.Root onOpenChange={open => { if (open) markNotificationsRead() }}>
          <DropdownMenu.Trigger asChild>
            <button className="relative grid h-10 w-10 place-items-center rounded-xl text-slate-600 outline-none transition hover:bg-slate-100 focus-visible:ring-2 focus-visible:ring-blue-500" aria-label={`${unread} unread notifications`}>
              <Bell size={18} />
              {unread > 0 && <span className="absolute right-1.5 top-1.5 grid h-4 min-w-4 animate-pulse place-items-center rounded-full border-2 border-white bg-red-500 px-0.5 text-[8px] text-white">{unread}</span>}
            </button>
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content align="end" sideOffset={8} className="z-[70] w-80 rounded-2xl border border-slate-200 bg-white p-2 shadow-2xl">
              <div className="flex items-center justify-between px-2 py-2"><div><p className="text-sm font-bold">Notifications</p><p className="text-[10px] text-slate-500">Local upload status only</p></div><CheckCheck size={16} className="text-blue-600" /></div>
              {notifications.length ? notifications.map(notification => (
                <DropdownMenu.Item key={notification.id} className="flex cursor-pointer gap-3 rounded-xl p-3 outline-none hover:bg-slate-50">
                  <span className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${notification.tone === 'green' ? 'bg-emerald-500' : notification.tone === 'purple' ? 'bg-purple-500' : 'bg-blue-500'}`} />
                  <div><p className="text-xs font-semibold">{notification.title}</p><p className="text-[10px] text-slate-500">{notification.description}</p><p className="mt-1 text-[9px] text-slate-400">{notification.time}</p></div>
                </DropdownMenu.Item>
              )) : <div className="rounded-xl p-3 text-xs text-slate-500">No notifications yet.</div>}
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
        <DropdownMenu.Root>
          <DropdownMenu.Trigger asChild>
            <button className="ml-1 flex items-center gap-2 rounded-xl p-1.5 text-left outline-none transition hover:bg-slate-50 focus-visible:ring-2 focus-visible:ring-blue-500">
              <div className="grid h-9 w-9 place-items-center rounded-full bg-gradient-to-br from-blue-500 to-indigo-600 text-xs font-bold text-white ring-2 ring-blue-100">{user.initials}</div>
              <div className="hidden xl:block"><p className="max-w-36 truncate text-xs font-semibold text-slate-900">{user.name}</p><p className="text-[10px] text-slate-500">{user.role}</p></div>
              <ChevronDown size={14} className="hidden text-slate-400 sm:block" />
            </button>
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content align="end" sideOffset={8} className="z-[70] min-w-48 rounded-xl border border-slate-200 bg-white p-1.5 shadow-xl">
              <DropdownMenu.Item onSelect={() => showToast('Profile settings are not enabled for this MVP.')} className="menu-item"><User size={15} /> Profile</DropdownMenu.Item>
              <DropdownMenu.Item onSelect={onSettings} className="menu-item"><Settings size={15} /> Preferences</DropdownMenu.Item>
              <DropdownMenu.Item onSelect={onHelp} className="menu-item"><CircleHelp size={15} /> Help</DropdownMenu.Item>
              <DropdownMenu.Separator className="my-1 h-px bg-slate-100" />
              <DropdownMenu.Item onSelect={logout} className="menu-item text-red-600 hover:bg-red-50"><LogOut size={15} /> Logout</DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      </div>
    </header>
  )
}
