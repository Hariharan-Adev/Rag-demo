import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { MessageSquare, MoreHorizontal, Pencil, Trash2 } from 'lucide-react'
import { useMemo, useState, type KeyboardEvent, type MouseEvent } from 'react'
import { useApp } from '../context/AppContext'
import type { Conversation } from '../types'
import { cn } from '../utils/cn'
import { Button } from './ui/Button'
import { Modal } from './ui/Modal'

const groupOrder = ['Today', 'Yesterday', 'Previous 7 Days', 'Older'] as const
type GroupName = typeof groupOrder[number]

function conversationGroup(value: string): GroupName {
  const date = new Date(value)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  date.setHours(0, 0, 0, 0)
  const days = Math.floor((today.getTime() - date.getTime()) / 86_400_000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days <= 7) return 'Previous 7 Days'
  return 'Older'
}

export default function ChatHistory() {
  const { conversations, activeConversationId, view, selectConversation, renameConversation, deleteConversation } = useApp()
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null)

  const groups = useMemo(() => {
    const sorted = [...conversations].sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
    return groupOrder
      .map(name => ({ name, conversations: sorted.filter(conversation => conversationGroup(conversation.updatedAt) === name) }))
      .filter(group => group.conversations.length)
  }, [conversations])

  const beginRename = (conversation: Conversation) => {
    setRenamingId(conversation.id)
    setRenameValue(conversation.title)
  }

  const cancelRename = () => {
    setRenamingId(null)
    setRenameValue('')
  }

  const saveRename = () => {
    if (!renamingId || !renameValue.trim()) return
    renameConversation(renamingId, renameValue)
    cancelRename()
  }

  const handleRenameKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      saveRename()
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      cancelRename()
    }
  }

  const stopRowAction = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation()
  }

  const confirmDelete = () => {
    if (!deleteTarget) return
    deleteConversation(deleteTarget.id)
    setDeleteTarget(null)
  }

  const row = (conversation: Conversation) => {
    const active = view === 'chat' && conversation.id === activeConversationId
    const renaming = renamingId === conversation.id

    return (
      <div
        key={conversation.id}
        className={cn(
          'group flex h-10 w-full min-w-0 items-center gap-2 rounded-[10px] px-2.5 text-slate-600 hover:bg-[#f3f4f6] hover:text-slate-900 focus-within:bg-[#f3f4f6]',
          active && 'bg-[#eef4ff] text-[#2563eb]',
        )}
      >
        {renaming ? (
          <>
            <MessageSquare size={14} className="shrink-0" />
            <input
              autoFocus
              value={renameValue}
              onChange={event => setRenameValue(event.target.value)}
              onKeyDown={handleRenameKeyDown}
              onBlur={cancelRename}
              onFocus={event => event.currentTarget.select()}
              aria-label="Conversation title"
              className="h-8 min-w-0 flex-1 rounded-lg border border-blue-300 bg-white px-2 text-[12px] text-slate-900 outline-none focus:ring-2 focus:ring-blue-500/35"
            />
          </>
        ) : (
          <button
            type="button"
            onClick={() => selectConversation(conversation.id)}
            aria-current={active ? 'page' : undefined}
            aria-pressed={active}
            className="flex min-w-0 flex-1 items-center gap-2 self-stretch text-left text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-blue-500/35"
          >
            <MessageSquare size={14} className="shrink-0" />
            <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{conversation.title}</span>
          </button>
        )}

        {!renaming && (
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                onClick={stopRowAction}
                aria-label={`Actions for ${conversation.title}`}
                aria-haspopup="menu"
                className="grid h-[30px] w-[30px] shrink-0 place-items-center rounded-lg text-slate-500 opacity-100 outline-none hover:bg-[#f3f4f6] hover:text-slate-800 focus-visible:ring-2 focus-visible:ring-blue-500/35 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100 data-[state=open]:opacity-100"
              >
                <MoreHorizontal size={17} />
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                role="menu"
                align="end"
                sideOffset={5}
                collisionPadding={8}
                className="z-[75] w-[170px] rounded-xl border border-[#e5e7eb] bg-white p-1.5 shadow-[0_8px_24px_rgba(0,0,0,.12)]"
              >
                <DropdownMenu.Item
                  role="menuitem"
                  onSelect={() => beginRename(conversation)}
                  className="flex h-[38px] cursor-pointer items-center gap-2 rounded-lg px-2.5 text-[12px] text-slate-700 outline-none hover:bg-[#f3f4f6] focus:bg-[#f3f4f6]"
                >
                  <Pencil size={15} />
                  Rename
                </DropdownMenu.Item>
                <DropdownMenu.Item
                  role="menuitem"
                  onSelect={() => setDeleteTarget(conversation)}
                  className="flex h-[38px] cursor-pointer items-center gap-2 rounded-lg px-2.5 text-[12px] text-[#dc2626] outline-none hover:bg-[#fef2f2] focus:bg-[#fef2f2]"
                >
                  <Trash2 size={15} />
                  Delete
                </DropdownMenu.Item>
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>
        )}
      </div>
    )
  }

  return (
    <section aria-labelledby="chat-history-title">
      <h2 id="chat-history-title" className="px-2 text-[10px] font-semibold uppercase tracking-[.12em] text-slate-400">Chat History</h2>
      <div className="mt-2 space-y-4">
        {groups.map(group => (
          <div key={group.name}>
            <p className="mb-1 px-2 text-[10px] font-semibold uppercase tracking-[.08em] text-slate-400">{group.name}</p>
            <div className="space-y-0.5">{group.conversations.map(row)}</div>
          </div>
        ))}
        {!conversations.length && <p className="px-2 py-4 text-center text-[10px] leading-4 text-slate-400">Your conversations will appear here after you ask your first question.</p>}
      </div>

      <Modal open={deleteTarget !== null} onClose={() => setDeleteTarget(null)} title="Delete this conversation?">
        <p className="text-sm leading-6 text-slate-600">This will permanently delete only this conversation. Your uploaded documents and other chats will remain unchanged.</p>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={() => setDeleteTarget(null)}>Cancel</Button>
          <Button variant="danger" size="sm" onClick={confirmDelete}>Delete</Button>
        </div>
      </Modal>
    </section>
  )
}
