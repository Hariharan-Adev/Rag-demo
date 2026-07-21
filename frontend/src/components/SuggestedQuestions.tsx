import { FileSearch, ListChecks, Sparkles } from 'lucide-react'

const styles = [
  { icon: FileSearch, tone: 'text-blue-600 bg-blue-50' },
  { icon: Sparkles, tone: 'text-purple-600 bg-purple-50' },
  { icon: ListChecks, tone: 'text-emerald-600 bg-emerald-50' },
]

export default function SuggestedQuestions({ suggestions, onSelect }: { suggestions: string[]; onSelect: (question: string) => void }) {
  return (
    <div className="mx-auto flex max-w-[760px] flex-wrap justify-center gap-2 px-4">
      {suggestions.map((question, index) => {
        const { icon: Icon, tone } = styles[index % styles.length]
        return <button key={question} type="button" onClick={() => onSelect(question)} className="group inline-flex items-center gap-2 rounded-full border border-[#e6ecf5] bg-white px-3.5 py-2 text-[12px] font-medium text-slate-600 shadow-[0_2px_8px_rgba(15,23,42,.03)] hover:-translate-y-0.5 hover:border-blue-200 hover:bg-[#f5f9ff] hover:text-blue-600 hover:shadow-[0_6px_16px_rgba(37,99,235,.08)]">
          <span className={`grid h-6 w-6 place-items-center rounded-full ${tone}`}><Icon size={13} /></span>{question}
        </button>
      })}
    </div>
  )
}
