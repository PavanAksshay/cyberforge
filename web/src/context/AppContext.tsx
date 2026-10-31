import React, { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import { Currency, Tenant } from '../types';
import { fetchTenants } from '../lib/api';

export type DatePreset = '24h' | '3d' | '7d' | '30d' | '90d' | 'all';

interface AppContextType {
  theme: 'light' | 'dark';
  setTheme: (t: 'light' | 'dark') => void;
  toggleTheme: () => void;
  currency: Currency;
  setCurrency: (c: Currency) => void;
  tenants: Tenant[];
  activeTenant: Tenant | null;
  setActiveTenant: (t: Tenant) => void;
  loadingTenants: boolean;
  datePreset: DatePreset;
  setDatePreset: (p: DatePreset) => void;
  startDate: string;
  endDate: string;
  setCustomDates: (start: string, end: string) => void;
  token: string;
  setToken: (tok: string) => void;
  autoRefreshInterval: number; // 0 for off, seconds otherwise
  setAutoRefreshInterval: (s: number) => void;
  refreshKey: number;
  triggerRefresh: () => void;
}

const AppContext = createContext<AppContextType | undefined>(undefined);

function computeDateRange(preset: DatePreset): { start: string; end: string } {
  // Benchmark dataset window: 2026-10-01T00:00:00Z to 2026-10-08T00:00:00Z
  const baseStart = new Date(Date.UTC(2026, 9, 1, 0, 0, 0));
  const baseEnd = new Date(Date.UTC(2026, 9, 8, 0, 0, 0));

  let start = baseStart;
  let end = baseEnd;

  if (preset === '24h') {
    start = baseStart;
    end = new Date(Date.UTC(2026, 9, 2, 0, 0, 0)); // 24h: Oct 1 - Oct 2
  } else if (preset === '3d') {
    start = baseStart;
    end = new Date(Date.UTC(2026, 9, 4, 0, 0, 0)); // 3 Days: Oct 1 - Oct 4
  } else if (preset === '7d' || preset === 'all' || preset === '30d' || preset === '90d') {
    start = baseStart;
    end = baseEnd; // All 7 Days: Oct 1 - Oct 8
  }

  return {
    start: start.toISOString().replace(/\.\d{3}Z$/, 'Z'),
    end: end.toISOString().replace(/\.\d{3}Z$/, 'Z'),
  };
}

export const AppProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('astraroll_theme');
    if (saved === 'light') return 'light';
    return 'dark'; // Dark & Green default
  });

  const [currency, setCurrency] = useState<Currency>('USD');
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [activeTenant, setActiveTenant] = useState<Tenant | null>(null);
  const [loadingTenants, setLoadingTenants] = useState<boolean>(true);
  const [datePreset, setDatePreset] = useState<DatePreset>('all');
  const [dates, setDates] = useState(computeDateRange('all'));
  const [token, setTokenState] = useState<string>(() => localStorage.getItem('astraroll_token') || 'smoke-test-admin-key-astraroll-v2');
  const [autoRefreshInterval, setAutoRefreshInterval] = useState<number>(0);
  const [refreshKey, setRefreshKey] = useState<number>(0);

  const setToken = (tok: string) => {
    setTokenState(tok);
    localStorage.setItem('astraroll_token', tok);
  };

  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
  };

  useEffect(() => {
    localStorage.setItem('astraroll_theme', theme);
    if (theme === 'dark') {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  }, [theme]);

  const setDatePresetHandler = (p: DatePreset) => {
    setDatePreset(p);
    setDates(computeDateRange(p));
  };

  const setCustomDates = (start: string, end: string) => {
    setDates({ start, end });
  };

  const triggerRefresh = () => {
    setRefreshKey((k) => k + 1);
  };

  // Load tenants on mount and when token changes
  useEffect(() => {
    let isMounted = true;
    setLoadingTenants(true);
    fetchTenants(token)
      .then((data) => {
        if (!isMounted) return;
        setTenants(data);
        if (data.length > 0 && !activeTenant) {
          setActiveTenant(data[0]);
        }
      })
      .catch((err) => {
        console.warn('Error loading tenants:', err);
      })
      .finally(() => {
        if (isMounted) setLoadingTenants(false);
      });

    return () => {
      isMounted = false;
    };
  }, [token, refreshKey]);

  // Auto-refresh timer
  useEffect(() => {
    if (autoRefreshInterval <= 0) return;
    const intervalId = setInterval(() => {
      triggerRefresh();
    }, autoRefreshInterval * 1000);
    return () => clearInterval(intervalId);
  }, [autoRefreshInterval]);

  return (
    <AppContext.Provider
      value={{
        theme,
        setTheme,
        toggleTheme,
        currency,
        setCurrency,
        tenants,
        activeTenant,
        setActiveTenant,
        loadingTenants,
        datePreset,
        setDatePreset: setDatePresetHandler,
        startDate: dates.start,
        endDate: dates.end,
        setCustomDates,
        token,
        setToken,
        autoRefreshInterval,
        setAutoRefreshInterval,
        refreshKey,
        triggerRefresh,
      }}
    >
      {children}
    </AppContext.Provider>
  );
};

export const useApp = () => {
  const context = useContext(AppContext);
  if (!context) {
    throw new Error('useApp must be used within an AppProvider');
  }
  return context;
};

