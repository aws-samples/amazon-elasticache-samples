import { NavLink } from 'react-router';
import { cn } from '@/lib/utils';
import { 
  Database, 
  Home, 
  Settings, 
  Terminal, 
  Activity,
  Clock,
  Zap,
  Command,
  MessageCircle
} from 'lucide-react';
import type { NavItem } from '@/types';

const navigation: NavItem[] = [
  {
    title: 'Home',
    href: '/',
    icon: 'Home',
    description: 'Dashboard and overview'
  },
  {
    title: 'Connections',
    href: '/connections',
    icon: 'Database',
    description: 'Manage cluster connections'
  },
  {
    title: 'Monitoring',
    href: '/monitoring',
    icon: 'Activity',
    description: 'View metrics and performance'
  },
  {
    title: 'Key Browser',
    href: '/cli',
    icon: 'Command',
    description: 'Browse and edit Valkey keys'
  },
  {
    title: 'CLI Interface',
    href: '/cmdline',
    icon: 'Terminal',
    description: 'Execute Valkey commands directly'
  },
  {
    title: 'Command Log',
    href: '/commandlog',
    icon: 'Clock',
    description: 'View command execution logs'
  },
  {
    title: 'Help & Documentation',
    href: '/help',
    icon: 'MessageCircle',
    description: 'Read help docs and guides'
  }
];

const iconMap = {
  Home,
  Database,
  Settings,
  Terminal,
  Command,
  Activity,
  Clock,
  Zap,
  MessageCircle
};

export function Sidebar() {
  return (
    <div className="hidden border-r bg-muted/40 md:block w-64">
      <div className="flex h-full max-h-screen flex-col gap-2">
        <div className="flex h-14 items-center border-b px-4 lg:h-[60px] lg:px-6">
          <div className="flex items-center gap-2 font-semibold">
            <img src="/valkeylogo.png" alt="Valkey" className="h-6 w-6" />
            <span>ValkeyNavigator</span>
          </div>
        </div>
        <div className="flex-1">
          <nav className="grid gap-2 px-2 py-2">
            {navigation.map((item) => {
              const Icon = iconMap[item.icon as keyof typeof iconMap];
              return (
                <NavLink
                  key={item.href}
                  to={item.href}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-all hover:bg-accent hover:text-accent-foreground",
                      isActive 
                        ? "bg-accent text-accent-foreground" 
                        : "text-muted-foreground"
                    )
                  }
                >
                  <Icon className="h-4 w-4" />
                  {item.title}
                </NavLink>
              );
            })}
          </nav>
        </div>
      </div>
    </div>
  );
}
