import { Menu, Sparkles } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { useEffect, useRef, useState } from 'react'
import { useApp } from '../context/AppContext'
import AIMessage from './AIMessage'
import ChatInput from './ChatInput'
import SuggestedQuestions from './SuggestedQuestions'
import UserMessage from './UserMessage'

export default function ChatArea({ onUpload }: { onUpload: () => void }) {
  const { messages, loading, sendMessage, suggestions, setSidebarOpen } = useApp()
  const bottomRef = useRef<HTMLDivElement>(null)
  const [editingMessageId, setEditingMessageId] = useState<number | null>(null)
  const hasConversation = messages.some(message => message.role === 'user')

  useEffect(() => {
    if (hasConversation) bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [hasConversation, messages, loading])

  return (
    <section className="relative flex h-screen min-w-0 flex-1 flex-col overflow-hidden bg-[#f8fafc]">
      <button type="button" onClick={() => setSidebarOpen(true)} className="absolute left-3 top-3 z-20 grid h-10 w-10 place-items-center rounded-xl bg-white text-slate-500 shadow-sm hover:bg-blue-50 hover:text-blue-600 lg:hidden" aria-label="Open sidebar"><Menu size={20} /></button>

      {!hasConversation ? (
        <div className="relative flex min-h-0 flex-1 flex-col items-center justify-center overflow-y-auto px-3 pb-16 before:pointer-events-none before:absolute before:left-1/2 before:top-1/2 before:h-[420px] before:w-[620px] before:-translate-x-1/2 before:-translate-y-1/2 before:rounded-full before:bg-blue-100/35 before:blur-3xl">
          <div className="relative z-10 mb-7 grid h-16 w-16 place-items-center rounded-full bg-gradient-to-br from-blue-600 to-indigo-500 text-white shadow-[0_12px_32px_rgba(37,99,235,.28)]"><Sparkles size={26} /><span className="absolute -right-1 top-0 text-sm text-blue-400">✦</span></div>
          <h1 className="relative z-10 mb-8 max-w-[760px] text-center text-[clamp(30px,3.4vw,38px)] font-bold leading-[1.2] tracking-[-.035em] text-[#0f172a]">What would you like to know about <span className="text-blue-600">your documents?</span></h1>
          <div className="relative z-10 w-full"><ChatInput onUpload={onUpload} /></div>
          <div className="relative z-10 mt-3"><SuggestedQuestions suggestions={suggestions} onSelect={sendMessage} /></div>
        </div>
      ) : (
        <>
          <div className="chat-scroll min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-6">
            <div className="mx-auto flex w-full max-w-[820px] flex-col gap-7">
              <AnimatePresence initial={false}>
                {messages.map(message => message.role === 'user' ? (
                  <UserMessage
                    key={message.id}
                    message={message}
                    isEditing={editingMessageId === message.id}
                    onEditStart={() => setEditingMessageId(message.id)}
                    onEditEnd={() => setEditingMessageId(null)}
                    onSubmit={content => {
                      setEditingMessageId(null)
                      void sendMessage(content, message.id)
                    }}
                  />
                ) : <AIMessage key={message.id} message={message} />)}
              </AnimatePresence>
              {loading && <motion.div initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} className="flex items-center gap-3 py-2 text-[13px] text-slate-500"><span className="grid h-7 w-7 place-items-center rounded-full bg-slate-900 text-white"><Sparkles size={13} /></span><span className="flex gap-1" aria-label="Generating answer"><i className="typing-dot" /><i className="typing-dot [animation-delay:150ms]" /><i className="typing-dot [animation-delay:300ms]" /></span></motion.div>}
              <div ref={bottomRef} />
            </div>
          </div>
          <div className="shrink-0 bg-gradient-to-t from-[#f8fafc] via-[#f8fafc] to-[#f8fafc]/80 pt-2"><ChatInput onUpload={onUpload} /></div>
        </>
      )}
    </section>
  )
}
