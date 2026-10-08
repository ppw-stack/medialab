import { ConfigProvider, theme } from "antd";
import { Routes, Route, Navigate } from "react-router-dom";
import Login from "./pages/Login";
import Layout from "./layout";
import Dashboard from "./pages/Dashboard";
import Sites from "./pages/Sites";
import Torrents from "./pages/Torrents";
import FreeSeed from "./pages/FreeSeed";
import Alerts from "./pages/Alerts";
import Cleanup from "./pages/Cleanup";
import AuditLog from "./pages/AuditLog";
import Settings from "./pages/Settings";
import { useAuth } from "./AuthProvider"

export default function App() {
  const { loading } = useAuth();

  if (loading) return null;

  return (
    <ConfigProvider
      theme={{
        algorithm: theme.darkAlgorithm,
        token: { colorPrimary: "#1677ff" },
      }}
    >
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/sites" element={<Sites />} />
          <Route path="/torrents" element={<Torrents />} />
          <Route path="/free-seed" element={<FreeSeed />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/cleanup" element={<Cleanup />} />
          <Route path="/audit" element={<AuditLog />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </ConfigProvider>
  );
}
