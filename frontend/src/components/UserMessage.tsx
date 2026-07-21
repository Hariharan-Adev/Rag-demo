import { motion } from 'framer-motion'

export default function UserMessage({ children }: { children: string }) {
  return <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .2 }} className="ml-auto max-w-[75%] whitespace-pre-wrap rounded-[18px] rounded-br-md border border-blue-100 bg-[#eef4ff] px-4 py-2.5 text-[14px] leading-6 text-slate-900 shadow-[0_5px_18px_rgba(37,99,235,.06)] max-sm:max-w-[88%]">{children}</motion.div>
}
