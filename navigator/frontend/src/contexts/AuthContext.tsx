/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ManageUserLogin, ManageUserLogout } from '@/services/auth';

interface AuthContextType {
  isLoggedIn: boolean;
  username: string | null;
  login: (user: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function useAuth(): AuthContextType {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider');
  return ctx;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [isLoggedIn, setIsLoggedIn] = useState<boolean>(() => {
    try {
      return localStorage.getItem('valkey-logged-in') === 'true';
    } catch {
      return false;
    }
  });
  const [username, setUsername] = useState<string | null>(() => {
    try {
      return localStorage.getItem('valkey-username');
    } catch {
      return null;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem('valkey-logged-in', isLoggedIn ? 'true' : 'false');
      if (isLoggedIn && username) localStorage.setItem('valkey-username', username);
      if (!isLoggedIn) {
        localStorage.removeItem('valkey-username');
      }
    } catch {
      // no-op
    }
  }, [isLoggedIn, username]);

  const login = async (user: string, password: string): Promise<boolean> => {
    const ok = await ManageUserLogin(user, password);
    if (ok) {
      setIsLoggedIn(true);
      setUsername(user);
    }
    return ok;
  };

  const logout = async (): Promise<void> => {
    try {
      await ManageUserLogout();
    } finally {
      setIsLoggedIn(false);
      setUsername(null);
    }
  };

  const value = useMemo(
    () => ({ isLoggedIn, username, login, logout }),
    [isLoggedIn, username]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
