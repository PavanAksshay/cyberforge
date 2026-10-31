import React, { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import { Currency, Tenant } from '../types';
import { fetchTenants } from '../lib/api';

export type DatePreset = '24h' | '7d' | '30d' | '90d' | 'all';

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
  const now = new Date();
  // Align to complete UTC days (midnight)
  const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
  let start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));

  if (preset === '24h') {
    start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
  } else if (preset === '7d') {
    start = new Date(end.getTime() - 7 * 24 * 60 * 60 * 1000);
  } else if (preset === '30d') {
    start = new Date(end.getTime() - 30 * 24 * 60 * 60 * 1000);
  } else if (preset === '90d') {
    start = new Date(end.getTime() - 90 * 24 * 60 * 60 * 1000);
  } else if (preset === 'all') {
    start = new Date(Date.UTC(2026, 0, 1));
  }

  return {
    start: start.toISOString().replace(/\.\d{3}Z$/, 'Z'),
    end: end.toISOString().replace(/\.\d{3}Z$/, 'Z'),
  };
}

export const AppProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('astraroll_theme');
    if (saved === 'dark' || saved === 'light') return saved;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  });

  const [currency, setCurrency] = useState<Currency>('USD');
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [activeTenant, setActiveTenant] = useState<Tenant | null>(null);
  const [loadingTenants, setLoadingTenants] = useState<boolean>(true);
  const [datePreset, setDatePreset] = useState<DatePreset>('30d');
  const [dates, setDates] = useState(computeDateRange('30d'));
  const [token, setTokenState] = useState<string>(() => localStorage.getItem('astraroll_token') || '');
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
