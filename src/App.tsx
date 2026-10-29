import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { Layout } from './components/layout/Layout'
import { GlossaryPage } from './pages/GlossaryPage'
import { SettingsPage } from './pages/SettingsPage'
import { TaskPage } from './pages/TaskPage'
import { TasksPage } from './pages/TasksPage'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<TasksPage />} />
          <Route path="tasks/:id" element={<TaskPage />} />
          <Route path="glossary" element={<GlossaryPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
