import { Outlet, Link, useLocation } from 'react-router-dom'
import { Settings, Home, FileText } from 'lucide-react'
import { Button } from '@/components/ui/button'

export function Layout() {
  const location = useLocation()

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <header className="border-b">
        <div className="container flex h-14 items-center px-4">
          <Link to="/" className="flex items-center space-x-2">
            <FileText className="h-6 w-6" />
            <span className="font-bold text-lg">CS访谈翻译器</span>
          </Link>

          <nav className="flex items-center space-x-4 ml-6">
            <Link to="/">
              <Button
                variant={location.pathname === '/' ? 'secondary' : 'ghost'}
                size="sm"
              >
                <Home className="h-4 w-4 mr-2" />
                项目列表
              </Button>
            </Link>
          </nav>

          <div className="ml-auto">
            <Link to="/settings">
              <Button variant="ghost" size="icon">
                <Settings className="h-5 w-5" />
              </Button>
            </Link>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="container px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}
