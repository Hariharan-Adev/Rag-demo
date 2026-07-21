import type { ReactNode } from 'react'

export default function MainLayout({ children }: { children: ReactNode }) {
  return <div className="flex h-screen w-screen overflow-hidden bg-[#f8fafc] text-slate-900"><main className="flex min-h-0 min-w-0 flex-1">{children}</main></div>
}
