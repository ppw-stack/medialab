import { createContext, useContext, useEffect, useState, useCallback, type ReactNode } from "react";

interface AuthState {
  authenticated: boolean;
  username: string | null;
  loading: boolean;
}

interface AuthContextValue extends AuthState {
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  authenticated: false,
  username: null,
  loading: true,
  login: async () => {},
  logout: async () => {},
  refresh: async () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    authenticated: false,
    username: null,
    loading: true,
  });

  const refresh = useCallback(async () => {
    try {
      const resp = await fetch("/api/v1/auth/status");
      const data = await resp.json();
      setState({
        authenticated: data.authenticated,
        username: data.username,
        loading: false,
      });
    } catch {
      setState({ authenticated: false, username: null, loading: false });
    }
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const resp = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || "登录失败");
    }
    await refresh();
  }, []);

  const logout = useCallback(async () => {
    await fetch("/api/v1/auth/logout", { method: "POST" });
    setState({ authenticated: false, username: null, loading: false });
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <AuthContext.Provider value={{ ...state, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}
