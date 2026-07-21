import { Bookmark, Check, Copy, FileText, RefreshCw, Share2, Sparkles, ThumbsDown, ThumbsUp } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { useState } from 'react'
import { useApp } from '../context/AppContext'
import type { ChatItem } from '../types'
import MarkdownContent from './MarkdownContent'
import { Tooltip } from './ui/Tooltip'

export default function AIMessage({ message }: { message: ChatItem }) {
  const { showToast, updateMessage, regenerate, documents, setSelectedDocument } = useApp()
  const [copied, setCopied] = useState(false)
  const [feedback, setFeedback] = useState(false)

  const copy = async () => {
    await navigator.clipboard?.writeText(`${message.content} ${message.detail ?? ''}`)
    setCopied(true)
    showToast('Copied successfully')
    window.setTimeout(() => setCopied(false), 1500)
  }
  const like = () => { updateMessage(message.id, { liked: !message.liked, disliked: false }); showToast(message.liked ? 'Like removed' : 'Response liked') }
  const dislike = () => { updateMessage(message.id, { disliked: !message.disliked, liked: false }); setFeedback(!message.disliked); showToast(message.disliked ? 'Dislike removed' : 'Tell us what went wrong') }
  const bookmark = () => { updateMessage(message.id, { bookmarked: !message.bookmarked }); showToast(message.bookmarked ? 'Bookmark removed' : 'Response bookmarked') }
  const share = async () => {
    if (navigator.share) {
      try { await navigator.share({ title: 'Simple RAG answer', text: `${message.content}\n${message.detail ?? ''}` }) } catch { return }
    } else await navigator.clipboard?.writeText(message.content)
    showToast('Share text copied')
  }
  const openSource = () => {
    const document = documents.find(item => item.id === message.source?.id || item.name === message.source?.name)
    if (document) setSelectedDocument(document)
  }

  const actions = [
    { label: 'Copy answer', icon: copied ? <Check size={15} className="text-emerald-600" /> : <Copy size={15} />, action: copy, active: false },
    { label: 'Helpful', icon: <ThumbsUp size={15} />, action: like, active: message.liked },
    { label: 'Not helpful', icon: <ThumbsDown size={15} />, action: dislike, active: message.disliked },
    { label: 'Regenerate', icon: <RefreshCw size={15} />, action: () => regenerate(message.id), active: false },
    { label: 'Share', icon: <Share2 size={15} />, action: share, active: false },
    { label: 'Bookmark', icon: <Bookmark size={15} fill={message.bookmarked ? 'currentColor' : 'none'} />, action: bookmark, active: message.bookmarked },
  ]

  return (
    <motion.article initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .2 }} className="flex min-w-0 gap-3">
      <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full bg-gradient-to-br from-blue-600 to-indigo-500 text-white shadow-[0_5px_14px_rgba(37,99,235,.2)]"><Sparkles size={14} /></span>
      <div className="min-w-0 flex-1 rounded-2xl border border-[#eef2f7] bg-white p-4 shadow-[0_8px_30px_rgba(37,99,235,.06)] sm:p-5">
        <MarkdownContent content={message.content} />
        {message.detail && <div className="mt-3 text-slate-600"><MarkdownContent content={message.detail} /></div>}
        {message.source && (
          <button type="button" onClick={openSource} className="mt-4 flex max-w-md items-center gap-2.5 rounded-xl border border-[#e6ecf5] bg-[#f8fbff] px-3 py-2.5 text-left hover:border-blue-200 hover:bg-[#eef4ff]">
            <FileText size={15} className="shrink-0 text-blue-500" />
            <span className="min-w-0"><span className="block truncate text-[11px] font-semibold text-slate-700">{message.source.name}</span><span className="block text-[10px] text-slate-400">{message.source.section}</span></span>
            <span className="ml-auto text-[10px] font-semibold text-slate-500">{message.source.score}%</span>
          </button>
        )}
        <AnimatePresence>{feedback && <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="overflow-hidden"><div className="mt-3 rounded-xl bg-slate-50 p-3"><p className="text-[11px] font-semibold text-slate-700">What could be improved?</p><div className="mt-2 flex flex-wrap gap-1.5">{['Incorrect', 'Unclear', 'Missing source', 'Not relevant'].map(tag => <button key={tag} type="button" onClick={() => { setFeedback(false); showToast('Thanks for your feedback') }} className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[10px] text-slate-600 hover:bg-slate-100">{tag}</button>)}</div></div></motion.div>}</AnimatePresence>
        <div className="mt-3 flex flex-wrap items-center gap-0.5 text-slate-400">
          {actions.map(action => <Tooltip label={action.label} key={action.label}><button type="button" onClick={action.action} aria-label={action.label} className={`grid h-8 w-8 place-items-center rounded-lg hover:bg-slate-100 hover:text-slate-700 ${action.active ? 'bg-slate-100 text-slate-800' : ''}`}>{action.icon}</button></Tooltip>)}
          <time className="ml-2 text-[9px] text-slate-400">{new Date(message.id).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>
        </div>
      </div>
    </motion.article>
  )
}
