import { BarChart3, BookOpen, FileUp, Menu, MessageSquareText, TrendingUp } from 'lucide-react'
import { motion } from 'framer-motion'
import { useApp } from '../context/AppContext'
import { Card } from './ui/Card'

export default function DashboardAnalytics() {
  const { documents, messages, retrievedDocuments, recentQuestions, setSidebarOpen } = useApp()
  const questionCount = messages.filter(message => message.role === 'user').length
  const cards = [
    ['Indexed Documents', documents.length, BookOpen, 'text-blue-600 bg-blue-50'],
    ['Questions Asked', questionCount, MessageSquareText, 'text-purple-600 bg-purple-50'],
    ['Documents Uploaded', documents.length, FileUp, 'text-amber-600 bg-amber-50'],
    ['Sources in Latest Answer', retrievedDocuments.length, TrendingUp, 'text-emerald-600 bg-emerald-50'],
  ] as const
  const bars = [20, 45, 35, 70, 55, 80, Math.max(12, Math.min(100, retrievedDocuments.length * 12))]

  return (
    <motion.section initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} className="min-w-0 flex-1 overflow-y-auto bg-[#f8fafc] p-5 sm:p-7">
      <div className="mx-auto max-w-6xl">
        <button type="button" onClick={() => setSidebarOpen(true)} className="mb-4 grid h-10 w-10 place-items-center rounded-[10px] text-slate-600 hover:bg-slate-100 lg:hidden" aria-label="Open sidebar"><Menu size={20}/></button>
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[.1em] text-slate-400">Workspace overview</p>
          <h2 className="mt-1 text-2xl font-bold tracking-[-.03em]">Docsense AI Dashboard</h2>
          <p className="mt-1 text-sm text-slate-500">Monitor indexed documents and your current browser conversation.</p>
        </div>
        <div className="mt-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {cards.map(([label, value, Icon, tone], index) => (
            <motion.div key={label} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * .07 }}>
              <Card className="p-5">
                <div className={`grid h-10 w-10 place-items-center rounded-xl ${tone}`}><Icon size={19} /></div>
                <p className="mt-4 text-2xl font-bold">{value}</p>
                <p className="mt-1 text-xs text-slate-500">{label}</p>
              </Card>
            </motion.div>
          ))}
        </div>
        <div className="mt-5 grid gap-5 lg:grid-cols-[1.5fr_1fr]">
          <Card className="p-5">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-bold">Retrieval activity</h3>
                <p className="text-xs text-slate-500">Local visualization for the current session</p>
              </div>
              <BarChart3 className="text-blue-600" size={20} />
            </div>
            <div className="mt-8 flex h-52 items-end justify-between gap-3 border-b border-slate-200 px-2">
              {bars.map((height, index) => (
                <div key={index} className="flex h-full flex-1 flex-col justify-end">
                  <motion.div initial={{ height: 0 }} animate={{ height: `${height}%` }} transition={{ duration: .7, delay: index * .06 }} className="rounded-t-md bg-gradient-to-t from-blue-600 to-indigo-400" />
                  <span className="mt-2 text-center text-[10px] text-slate-400">{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][index]}</span>
                </div>
              ))}
            </div>
          </Card>
          <Card className="p-5">
            <h3 className="font-bold">Recent Questions</h3>
            <p className="text-xs text-slate-500">Stored in memory for this browser session only</p>
            <div className="mt-5 space-y-4">
              {recentQuestions.slice(0, 5).map((question, index) => (
                <div key={question} className="flex gap-3">
                  <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-blue-500 ring-4 ring-blue-50" />
                  <div>
                    <p className="line-clamp-1 text-xs font-semibold">{question}</p>
                    <p className="text-[10px] text-slate-400">Asked {index ? `${index + 1} questions ago` : 'just now'}</p>
                  </div>
                </div>
              ))}
              {!recentQuestions.length && <p className="text-xs text-slate-400">No recent activity</p>}
            </div>
          </Card>
        </div>
      </div>
    </motion.section>
  )
}
