import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Bell, Settings, User } from 'lucide-react';
import { useConnection } from '@/contexts/ConnectionContext';
import { SettingsDialog } from '@/components/settings/SettingsDialog';
import { useAuth } from '@/contexts/AuthContext';
import { LoginDialog } from '@/components/auth/LoginDialog';

export function Header() {
  const { activeConnection, connectionStatus } = useConnection();
  const { isLoggedIn } = useAuth();
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [isLoginOpen, setIsLoginOpen] = useState(false);
  
  return (
    <header className="flex h-14 items-center gap-4 border-b bg-muted/40 px-4 lg:h-[60px] lg:px-6">
      <div className="flex-1">
        {/* Search box removed */}
      </div>
      <div className="flex items-center gap-2">
        {activeConnection && (
          <Badge variant="outline" className="text-xs">
            <div className={`w-2 h-2 rounded-full mr-1 ${
              connectionStatus?.connected ? 'bg-green-500' : 'bg-red-500'
            }`}></div>
            {activeConnection.name}
          </Badge>
        )}
        <Button variant="outline" size="icon">
          <Bell className="h-4 w-4" />
        </Button>
        <Button variant="outline" size="icon" onClick={() => setIsSettingsOpen(true)}>
          <Settings className="h-4 w-4" />
        </Button>
        <Button
          variant="outline"
          size="icon"
          className={`${isLoggedIn ? 'bg-green-500 text-white' : 'bg-red-500 text-white'} hover:opacity-90`}
          onClick={() => setIsLoginOpen(true)}
          title={isLoggedIn ? 'Logged in' : 'Logged out'}
          aria-label="User Settings"
        >
          <User className="h-4 w-4" />
        </Button>
      </div>
      <SettingsDialog open={isSettingsOpen} onOpenChange={setIsSettingsOpen} />
      <LoginDialog open={isLoginOpen} onOpenChange={setIsLoginOpen} />
    </header>
  );
}
