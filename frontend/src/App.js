import "@/App.css";
import { BrowserRouter, HashRouter, Routes, Route, Navigate } from "react-router-dom";
import { ThemeProvider } from "next-themes";
import { Analytics } from "@vercel/analytics/react";
import { AuthProvider, useAuth } from "@/context/AuthContext";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { AppLayout } from "@/components/AppLayout";
import { Toaster } from "@/components/ui/sonner";
import AuthPage from "@/pages/AuthPage";
import Dashboard from "@/pages/Dashboard";
import Chat from "@/pages/Chat";
import Files from "@/pages/Files";
import CalendarPage from "@/pages/CalendarPage";
import Settings from "@/pages/Settings";
import TeamPage from "@/pages/TeamPage";
import PrivacyPolicy from "@/pages/PrivacyPolicy";
import TermsOfService from "@/pages/TermsOfService";
import TodoPage from "@/pages/TodoPage";

function LoginRoute() {
  const { user, ready } = useAuth();
  if (ready && user) return <Navigate to="/" replace />;
  return <AuthPage />;
}

// When running from file:// or Electron, HashRouter is required so
// react-router works without a web server.
const isFile = typeof window !== "undefined" && window.location.protocol === "file:";
const isElectron = typeof window !== "undefined" && window.electronAPI?.isElectron;
const AppRouter = isElectron || isFile ? HashRouter : BrowserRouter;

function App() {
  return (
    <ThemeProvider attribute="class" defaultTheme="light" enableSystem>
      <AppRouter>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<LoginRoute />} />
            <Route path="/privacy" element={<PrivacyPolicy />} />
            <Route path="/terms" element={<TermsOfService />} />
            <Route
              element={
                <ProtectedRoute>
                  <AppLayout />
                </ProtectedRoute>
              }
            >
              <Route path="/" element={<Dashboard />} />
              <Route path="/chat" element={<Chat />} />
              <Route path="/files" element={<Files />} />
              <Route path="/calendar" element={<CalendarPage />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/todos" element={<TodoPage />} />
              <Route path="/team" element={<TeamPage />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          <Toaster position="top-right" richColors />
          {!isFile && <Analytics />}
        </AuthProvider>
      </AppRouter>
    </ThemeProvider>
  );
}

export default App;
