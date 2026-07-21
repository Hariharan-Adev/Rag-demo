import { ArrowUp, Plus } from 'lucide-react'
import { useLayoutEffect, useRef } from 'react'
import { useForm } from 'react-hook-form'
import { useApp } from '../context/AppContext'

type FormValues = { question: string }

const MAX_TEXTAREA_HEIGHT = 150

function resizeTextarea(textarea: HTMLTextAreaElement | null) {
  if (!textarea) return

  textarea.style.height = 'auto'
  const nextHeight = Math.min(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT)
  textarea.style.height = `${nextHeight}px`
  textarea.style.overflowY = textarea.scrollHeight > MAX_TEXTAREA_HEIGHT ? 'auto' : 'hidden'
}

export default function ChatInput({ onUpload }: { onUpload: () => void }) {
  const { sendMessage, loading } = useApp()
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const { register, handleSubmit, reset, watch } = useForm<FormValues>({ defaultValues: { question: '' } })
  const question = watch('question')
  const field = register('question')
  const canSend = Boolean(question.trim()) && !loading

  useLayoutEffect(() => {
    resizeTextarea(textareaRef.current)
  }, [question])

  const submit = ({ question: value }: FormValues) => {
    if (!value.trim() || loading) return
    void sendMessage(value.trim())
    reset()
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.overflowY = 'hidden'
    }
  }

  return (
    <form onSubmit={handleSubmit(submit)} className="w-full">
      <div className="mx-auto w-full max-w-[812px] px-2 sm:px-4">
        <div className="chat-composer">
          <div className="chat-composer__input-area">
            <textarea
              {...field}
              ref={element => {
                field.ref(element)
                textareaRef.current = element
              }}
              rows={1}
              className="chat-composer__textarea"
              placeholder="Ask anything about your uploaded documents..."
              aria-label="Message input"
              onInput={event => resizeTextarea(event.currentTarget)}
              onKeyDown={event => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault()
                  if (canSend) void handleSubmit(submit)()
                }
              }}
            />
          </div>

          <div className="chat-composer__actions">
            <div className="chat-composer__actions-left">
              <button type="button" onClick={onUpload} className="chat-composer__icon-button" aria-label="Attach document">
                <Plus size={20} />
              </button>
            </div>

            <div className="chat-composer__actions-right">
              <button type="submit" disabled={!canSend} className="chat-composer__send-button" aria-label={loading ? 'Sending message' : 'Send message'}>
                <ArrowUp size={18} strokeWidth={2.3} />
              </button>
            </div>
          </div>
        </div>

        <p className="py-2 text-center text-[10px] text-slate-400">Answers are generated from retrieved document context.</p>
      </div>
    </form>
  )
}
