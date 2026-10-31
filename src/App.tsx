import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { Layout } from './components/layout/Layout'
import { GlossaryPage } from './pages/GlossaryPage'
import { MatchesPage } from './pages/MatchesPage'
import { ReviewPage } from './pages/ReviewPage'
import { SettingsPage } from './pages/SettingsPage'
import { TaskPage } from './pages/TaskPage'
import { TasksPage } from './pages/TasksPage'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* 在线精校：不带导航的单独页面，经内网穿透在手机上打开 */}
        <Route path="review/:id" element={<ReviewPage />} />
        <Route path="/" element={<Layout />}>
          <Route index element={<TasksPage />} />
          <Route path="matches" element={<MatchesPage />} />
          <Route path="tasks/:id" element={<TaskPage />} />
          <Route path="glossary" element={<GlossaryPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
