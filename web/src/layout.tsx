import { useState } from "react";
import { Layout as AntLayout, Menu, Avatar, Dropdown, Typography } from "antd";
import {
  DashboardOutlined,
  CloudServerOutlined,
  DownloadOutlined,
  GiftOutlined,
  BellOutlined,
  DeleteOutlined,
  AuditOutlined,
  SettingOutlined,
  LogoutOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Outlet, useNavigate, useLocation } from "react-router-dom";
import { useAuth } from "./AuthProvider";

const { Header, Sider, Content } = AntLayout;

const menuItems = [
  { key: "/", icon: <DashboardOutlined />, label: "总览" },
  { key: "/sites", icon: <CloudServerOutlined />, label: "站点" },
  { key: "/torrents", icon: <DownloadOutlined />, label: "任务" },
  { key: "/free-seed", icon: <GiftOutlined />, label: "免费种" },
  { key: "/alerts", icon: <BellOutlined />, label: "告警" },
  { key: "/cleanup", icon: <DeleteOutlined />, label: "清理" },
  { key: "/audit", icon: <AuditOutlined />, label: "审计" },
  { key: "/settings", icon: <SettingOutlined />, label: "设置" },
];

export default function Layout() {
  const { username, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);

  if (!username) {
    navigate("/login");
    return null;
  }

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  return (
    <AntLayout style={{ minHeight: "100vh" }}>
      <Sider collapsible collapsed={collapsed} onCollapse={setCollapsed}>
        <div
          style={{
            height: 48,
            margin: 12,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#1677ff",
            fontWeight: "bold",
            fontSize: collapsed ? 16 : 18,
          }}
        >
          {collapsed ? "SH" : "SeedHelper"}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <AntLayout>
        <Header
          style={{
            display: "flex",
            justifyContent: "flex-end",
            alignItems: "center",
            padding: "0 24px",
          }}
        >
          <Dropdown
            menu={{
              items: [
                {
                  key: "logout",
                  icon: <LogoutOutlined />,
                  label: "退出登录",
                  onClick: handleLogout,
                },
              ],
            }}
          >
            <span style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 8 }}>
              <Avatar icon={<UserOutlined />} />
              <Typography.Text style={{ color: "white" }}>{username}</Typography.Text>
            </span>
          </Dropdown>
        </Header>
        <Content style={{ margin: 16, padding: 24, background: "#141414", borderRadius: 8 }}>
          <Outlet />
        </Content>
      </AntLayout>
    </AntLayout>
  );
}

