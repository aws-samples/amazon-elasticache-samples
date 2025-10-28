import { Outlet, useLocation } from 'react-router';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import { useAuth } from '@/contexts/AuthContext';

export function Layout() {
  const location = useLocation();
  const isChatPage = location.pathname === '/chat';
  const { isLoggedIn } = useAuth();

  return (
    <div className="flex h-screen bg-background">
      <Sidebar />
      <div className="flex flex-col flex-1 overflow-hidden">
        <Header />
        <main className={`flex-1 overflow-auto ${isChatPage ? '' : 'p-6'}`}>
          {isLoggedIn ? (
            <Outlet />
          ) : (
            <div className="w-full h-full flex items-center justify-center text-muted-foreground">
              <img src="/initial_login.png" alt="initial login"/>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
