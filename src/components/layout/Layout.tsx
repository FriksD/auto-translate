import { NavLink, Outlet } from 'react-router-dom'
import { BookText, CalendarDays, ListChecks, Settings } from 'lucide-react'
import { cn } from '@/lib/utils'

const NAV = [
  { to: '/', label: '任务', icon: ListChecks, end: true },
  { to: '/matches', label: '比赛', icon: CalendarDays, end: false },
  { to: '/glossary', label: '术语表', icon: BookText, end: false },
  { to: '/settings', label: '设置', icon: Settings, end: false },
]

export function Layout() {
  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b bg-background">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-6 px-6">
          <span className="font-semibold">CS2 采访翻译</span>
          <nav className="flex items-center gap-1">
            {NAV.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    'inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors',
                    isActive ? 'bg-secondary font-medium' : 'text-muted-foreground hover:text-foreground',
                  )
                }
              >
                <Icon className="h-4 w-4" />
                {label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-6">
        <Outlet />
      </main>
    </div>
  )
}
