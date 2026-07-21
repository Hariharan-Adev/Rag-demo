import { Bookmark, Trash2 } from 'lucide-react'
import { useApp } from '../context/AppContext'
import { Modal } from './ui/Modal'

export default function BookmarksModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { bookmarks, updateMessage } = useApp()
  return <Modal open={open} onClose={onClose} title="Bookmarked answers">
    <div className="max-h-[60vh] space-y-2 overflow-y-auto">
      {bookmarks.map(message => <article key={message.id} className="group rounded-xl border border-[#eef2f7] bg-white p-4 shadow-[0_5px_18px_rgba(37,99,235,.04)] hover:-translate-y-0.5 hover:bg-[#f8fbff]"><div className="flex items-start gap-3"><Bookmark size={15} className="mt-1 shrink-0 text-blue-600" fill="currentColor" /><div className="min-w-0 flex-1"><p className="line-clamp-3 text-[12px] font-medium leading-5 text-slate-800">{message.content}</p>{message.detail && <p className="mt-1 line-clamp-2 text-[10px] leading-4 text-slate-500">{message.detail}</p>}</div><button type="button" onClick={() => updateMessage(message.id, { bookmarked: false })} className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-slate-400 opacity-60 hover:bg-red-50 hover:text-red-600 group-hover:opacity-100" aria-label="Remove bookmark"><Trash2 size={14} /></button></div></article>)}
      {!bookmarks.length && <div className="py-12 text-center"><Bookmark className="mx-auto text-slate-300" /><p className="mt-3 text-[13px] font-medium text-slate-700">No bookmarks yet</p><p className="mt-1 text-[10px] text-slate-400">Bookmark useful AI answers to find them here.</p></div>}
    </div>
  </Modal>
}
