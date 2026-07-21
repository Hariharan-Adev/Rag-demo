import { Clock3, Trash2 } from 'lucide-react'

type RecentQuestionsProps = {
  questions: string[]
  onSelect: (question: string) => void
  onClear: () => void
}

export default function RecentQuestions({ questions, onSelect, onClear }: RecentQuestionsProps) {
  return (
    <section aria-labelledby="recent-questions-title">
      <div className="flex items-center justify-between px-2">
        <h2 id="recent-questions-title" className="text-[10px] font-semibold uppercase tracking-[.12em] text-slate-400">Recent Questions</h2>
        {questions.length > 0 && <button type="button" onClick={onClear} className="grid h-6 w-6 place-items-center rounded-md text-slate-400 hover:bg-red-50 hover:text-red-600" aria-label="Clear recent questions"><Trash2 size={12} /></button>}
      </div>
      <div className="mt-2 space-y-0.5">
        {questions.map(question => (
          <button key={question} type="button" onClick={() => onSelect(question)} title={question} className="flex h-9 w-full items-center gap-2 rounded-[10px] px-2 text-left text-[11px] text-slate-600 hover:bg-[#f3f7ff] hover:text-blue-600">
            <Clock3 size={13} className="shrink-0 text-blue-400" />
            <span className="truncate">{question}</span>
          </button>
        ))}
        {questions.length === 0 && <p className="px-2 py-3 text-center text-[10px] text-slate-400">No recent questions</p>}
      </div>
    </section>
  )
}
