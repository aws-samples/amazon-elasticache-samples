/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';

export interface AppSettings {
  dockerEndpoint: string; // default "localhost"
  influxUrl: string; // e.g., http://influxdb
  influxPort: number; // default 8086
  influxToken: string; // secret token
  influxBucket: string; // default ''
  influxOrg: string; // default ''
}

interface SettingsContextType {
  settings: AppSettings;
  setSettings: (s: AppSettings) => void;
  updateSettings: (partial: Partial<AppSettings>) => void;
}

const DEFAULT_SETTINGS: AppSettings = {
  dockerEndpoint: 'localhost',
  influxUrl: '',
  influxPort: 8086,
  influxToken: '',
  influxBucket: '',
  influxOrg: '',
};

const SettingsContext = createContext<SettingsContextType | undefined>(undefined);

export function useSettings() {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error('useSettings must be used within SettingsProvider');
  return ctx;
}

export function SettingsProvider({ children }: { children: ReactNode }) {
  // Initialize from localStorage synchronously to avoid a flash of defaults on first render
  const [settings, setSettingsState] = useState<AppSettings>(() => {
    try {
      const raw = localStorage.getItem('app-settings');
      if (raw) {
        const parsed = JSON.parse(raw);
        return { ...DEFAULT_SETTINGS, ...parsed } as AppSettings;
      }
    } catch {
      // ignore
    }
    return DEFAULT_SETTINGS;
  });

  // Persist to localStorage whenever settings change
  useEffect(() => {
    try {
      localStorage.setItem('app-settings', JSON.stringify(settings));
    } catch {
      // ignore
    }
  }, [settings]);

  const setSettings = (s: AppSettings) => setSettingsState({ ...DEFAULT_SETTINGS, ...s });
  const updateSettings = (partial: Partial<AppSettings>) => setSettingsState(prev => ({ ...prev, ...partial }));

  return (
    <SettingsContext.Provider value={{ settings, setSettings, updateSettings }}>
      {children}
    </SettingsContext.Provider>
  );
}
