import { Bell, Bot, LockKeyhole, Palette, UserRound } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { useApp } from '../context/AppContext'
import { Button } from './ui/Button'
import { Modal } from './ui/Modal'

export default function SettingsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { theme, toggleTheme, showToast, user } = useApp()
  const [language, setLanguage] = useState('English')
  const [fontSize, setFontSize] = useState('Medium')
  const [model, setModel] = useState('GPT-4o-mini')
  const [notifications, setNotifications] = useState(true)
  const save = () => { showToast('Settings saved'); onClose() }

  return <Modal open={open} onClose={onClose} title="Settings">
    <div className="space-y-5">
      <Section icon={<UserRound size={15} />} title="Profile"><Setting label="Authenticated account"><span className="max-w-48 truncate text-[11px] text-slate-500">{user.id}</span></Setting></Section>
      <Section icon={<Palette size={15} />} title="Appearance">
        <Setting label="Theme"><button type="button" onClick={toggleTheme} className="rounded-xl border border-[#e6ecf5] bg-white px-3 py-2 text-xs font-medium hover:bg-[#f5f9ff] hover:text-blue-600">{theme === 'light' ? 'Light' : 'Dark'} mode</button></Setting>
        <Setting label="Language"><select value={language} onChange={event => setLanguage(event.target.value)} className="field"><option>English</option><option>Hindi</option><option>French</option></select></Setting>
        <Setting label="Font size"><select value={fontSize} onChange={event => setFontSize(event.target.value)} className="field"><option>Small</option><option>Medium</option><option>Large</option></select></Setting>
      </Section>
      <Section icon={<Bell size={15} />} title="Notifications"><Setting label="Upload notifications"><button type="button" role="switch" aria-checked={notifications} onClick={() => setNotifications(!notifications)} className={`relative h-6 w-11 rounded-full ${notifications ? 'bg-blue-600' : 'bg-slate-300'}`}><span className={`absolute top-1 h-4 w-4 rounded-full bg-white transition-all ${notifications ? 'left-6' : 'left-1'}`} /></button></Setting></Section>
      <Section icon={<LockKeyhole size={15} />} title="Security"><Setting label="Privacy"><span className="max-w-56 text-right text-[10px] leading-4 text-slate-500">Conversation data stays in this browser.</span></Setting></Section>
      <Section icon={<Bot size={15} />} title="Application Information"><Setting label="AI model"><select value={model} onChange={event => setModel(event.target.value)} className="field"><option>GPT-4o-mini</option><option>GPT-4.1-mini</option><option>Enterprise RAG</option></select></Setting></Section>
    </div>
    <div className="mt-6 flex justify-end gap-2"><Button variant="secondary" onClick={onClose}>Cancel</Button><Button onClick={save}>Save settings</Button></div>
  </Modal>
}

function Section({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return <section><h3 className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[.08em] text-blue-600">{icon}{title}</h3><div className="divide-y divide-[#eef2f7] rounded-2xl border border-[#eef2f7] bg-white shadow-[0_5px_18px_rgba(37,99,235,.04)]">{children}</div></section>
}

function Setting({ label, children }: { label: string; children: ReactNode }) {
  return <div className="flex min-h-14 items-center justify-between gap-4 px-3 py-2.5"><p className="text-[12px] font-medium text-slate-800">{label}</p>{children}</div>
}
