import { motion } from 'framer-motion'
import { Check, Copy, Pencil, Send, Share2, X } from 'lucide-react'
import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useApp } from '../context/AppContext'
import type { ChatItem } from '../types'
import { Tooltip } from './ui/Tooltip'

interface UserMessageProps {
  message: ChatItem
  isEditing: boolean
  onEditStart: () => void
  onEditEnd: () => void
  onSubmit: (content: string) => void
}

export default function UserMessage({ message, isEditing, onEditStart, onEditEnd, onSubmit }: UserMessageProps) {
  const { showToast } = useApp()
  const [editedText, setEditedText] = useState(message.content)
  const [copied, setCopied] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (!isEditing) return
    setEditedText(message.content)
    window.requestAnimationFrame(() => textareaRef.current?.focus())
  }, [isEditing, message.content])

  const copyMessage = async () => {
    try {
      await navigator.clipboard.writeText(message.content)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      showToast('Unable to copy the message.')
    }
  }

  const shareMessage = async () => {
    try {
      if (navigator.share) await navigator.share({ title: 'Docsense AI conversation', text: message.content })
      else await navigator.clipboard.writeText(message.content)
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) showToast('Unable to share the message.')
    }
  }

  const submitEdit = () => {
    const normalized = editedText.trim()
    if (!normalized) {
      showToast('Message cannot be empty.')
      textareaRef.current?.focus()
      return
    }
    onSubmit(normalized)
  }

  const handleEditorKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      setEditedText(message.content)
      onEditEnd()
    } else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault()
      submitEdit()
    }
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: .2 }}
      className="user-message-group relative ml-auto flex w-full justify-end pb-1 outline-none"
      tabIndex={0}
    >
      <div className={`relative flex w-full flex-col items-end ${isEditing ? 'max-w-[780px]' : 'max-w-[75%] max-sm:max-w-[88%]'}`}>
        {isEditing ? (
          <div className="w-full rounded-[24px] border border-slate-200 bg-white p-3 shadow-sm">
            <textarea
              ref={textareaRef}
              value={editedText}
              onChange={event => setEditedText(event.target.value)}
              onKeyDown={handleEditorKeyDown}
              rows={3}
              aria-label="Edit message"
              className="block max-h-48 min-h-20 w-full resize-y rounded-xl bg-slate-50 px-3 py-2 text-[14px] leading-6 text-slate-900 outline-none focus:ring-0 focus-visible:ring-0 focus-visible:ring-offset-0"
            />
            <div className="mt-2 flex justify-end gap-2">
              <button type="button" onClick={onEditEnd} className="inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs font-semibold text-slate-600 hover:bg-slate-100 focus-visible:ring-2 focus-visible:ring-blue-500"><X size={14} />Cancel</button>
              <button type="button" onClick={submitEdit} disabled={!editedText.trim()} className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-slate-900 px-3 text-xs font-semibold text-white hover:bg-slate-800 focus-visible:ring-2 focus-visible:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-40"><Send size={14} />Save &amp; Submit</button>
            </div>
          </div>
        ) : (
          <div className="whitespace-pre-wrap rounded-[18px] rounded-br-md bg-slate-200/80 px-4 py-2.5 text-[14px] leading-6 text-slate-900 shadow-[0_4px_14px_rgba(15,23,42,.05)]">{message.content}</div>
        )}

        {!isEditing && (
          <div className="user-message-actions absolute right-0 top-full z-10 flex items-center justify-end gap-1" aria-label="User message actions">
            <Tooltip label={copied ? 'Copied' : 'Copy'}>
              <button type="button" onClick={copyMessage} aria-label={copied ? 'Copied' : 'Copy message'} title={copied ? 'Copied' : 'Copy'} className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 transition-colors hover:bg-slate-200/70 hover:text-slate-800 focus-visible:ring-2 focus-visible:ring-blue-500">{copied ? <Check size={18} /> : <Copy size={18} />}</button>
            </Tooltip>
            <Tooltip label="Share">
              <button type="button" onClick={shareMessage} aria-label="Share message" title="Share" className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 transition-colors hover:bg-slate-200/70 hover:text-slate-800 focus-visible:ring-2 focus-visible:ring-blue-500"><Share2 size={18} /></button>
            </Tooltip>
            <Tooltip label="Edit">
              <button type="button" onClick={onEditStart} aria-label="Edit message" title="Edit" className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 transition-colors hover:bg-slate-200/70 hover:text-slate-800 focus-visible:ring-2 focus-visible:ring-blue-500"><Pencil size={18} /></button>
            </Tooltip>
          </div>
        )}
      </div>
    </motion.div>
  )
}
