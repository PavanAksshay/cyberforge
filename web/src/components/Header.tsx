import React, { useState } from 'react';
import {
  Sun,
  Moon,
  RefreshCw,
  Database,
  Layers,
  Key,
  Clock,
  ChevronDown,
  Check,
} from 'lucide-react';
import { useApp, DatePreset } from '../context/AppContext';
import { Currency } from '../types';

export const Header: React.FC = () => {
  const {
    theme,
    toggleTheme,
    currency,
    setCurrency,
    tenants,
    activeTenant,
    setActiveTenant,
    datePreset,
    setDatePreset,
    autoRefreshInterval,
    setAutoRefreshInterval,
    triggerRefresh,
    token,
    setToken,
  } = useApp();

  const [tenantDropdownOpen, setTenantDropdownOpen] = useState(false);
  const [tokenModalOpen, setTokenModalOpen] = useState(false);
  const [tempToken, setTempToken] = useState(token);
  const [isRotating, setIsRotating] = useState(false);

  const handleManualRefresh = () => {
    setIsRotating(true);
    triggerRefresh();
    setTimeout(() => setIsRotating(false), 600);
  };

  // Primary settlement currencies pre-aggregated in 100 tenant DBs
  const currencies: Currency[] = ['USD', 'EUR'];
  const presets: { id: DatePreset; label: string }[] = [
    { id: '24h', label: '24h' },
    { id: '7d', label: '7d' },
    { id: 'all', label: 'All Time' },
  ];

  return (
    <header className="sticky top-0 z-30 flex items-center justify-between px-6 py-3 border-b bg-white/80 dark:bg-[#060B08]/90 backdrop-blur-md border-slate-200 dark:border-slate-800 transition-colors">
      {/* Brand & Mode */}
      <div className="flex items-center space-x-3">
        <div className="flex items-center justify-center w-9 h-9 rounded-xl bg-gradient-to-tr from-emerald-600 to-emerald-500 text-white shadow-md shadow-emerald-500/20">
          <Layers className="w-5 h-5" />
        </div>
        <div>
          <div className="flex items-center space-x-2">
            <span className="font-extrabold text-lg tracking-tight text-slate-900 dark:text-white">
              AstraRoll
            </span>
            <span className="px-2 py-0.5 text-xs font-semibold rounded-md bg-emerald-50 dark:bg-emerald-950/60 text-emerald-600 dark:text-emerald-400 border border-emerald-200/60 dark:border-emerald-800/60">
              V2 DB-Per-Tenant
            </span>
          </div>
          <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
            Enterprise Analytics Control Center
          </span>
        </div>
      </div>

      {/* Controls & Selectors */}
      <div className="flex items-center space-x-3">
        {/* Tenant Switcher */}
        <div className="relative">
          <button
            onClick={() => setTenantDropdownOpen(!tenantDropdownOpen)}
            className="flex items-center space-x-2 px-3 py-1.5 text-xs font-semibold rounded-lg bg-slate-100 hover:bg-slate-200/80 dark:bg-slate-800/80 dark:hover:bg-slate-700/80 text-slate-700 dark:text-slate-200 border border-slate-200/80 dark:border-slate-700 transition"
          >
            <Database className="w-3.5 h-3.5 text-emerald-500" />
            <span className="max-w-[140px] truncate">
              {activeTenant ? activeTenant.name : 'Select Tenant'}
            </span>
            <ChevronDown className="w-3 h-3 text-slate-400" />
          </button>

          {tenantDropdownOpen && (
            <div className="absolute left-0 mt-1.5 w-64 p-1 rounded-xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 shadow-xl z-50 animate-in fade-in zoom-in-95">
              <div className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400 border-b border-slate-100 dark:border-slate-800">
                Active Tenant Databases ({tenants.length})
              </div>
              <div className="max-h-56 overflow-y-auto py-1">
                {tenants.map((t) => (
                  <button
                    key={t.id}
                    onClick={() => {
                      setActiveTenant(t);
                      setTenantDropdownOpen(false);
                    }}
                    className={`w-full flex items-center justify-between px-3 py-2 text-xs rounded-lg transition ${
                      activeTenant?.id === t.id
                        ? 'bg-emerald-50 dark:bg-emerald-950/60 text-emerald-600 dark:text-emerald-400 font-semibold'
                        : 'text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800/60'
                    }`}
                  >
                    <div className="text-left truncate mr-2">
                      <div className="truncate font-medium">{t.name}</div>
                      <div className="text-[10px] text-slate-400 dark:text-slate-500 font-mono truncate">
                        {t.id}
                      </div>
                    </div>
                    {activeTenant?.id === t.id && <Check className="w-4 h-4 shrink-0 text-emerald-600 dark:text-emerald-400" />}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Currency Selector */}
        <div className="flex items-center rounded-lg bg-slate-100 dark:bg-slate-800/80 border border-slate-200/80 dark:border-slate-700 p-0.5">
          {currencies.map((c) => (
            <button
              key={c}
              onClick={() => setCurrency(c)}
              className={`px-2.5 py-1 text-xs font-semibold rounded-md transition ${
                currency === c
                  ? 'bg-white dark:bg-emerald-600 text-slate-900 dark:text-white shadow-sm'
                  : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white'
              }`}
            >
              {c}
            </button>
          ))}
        </div>

        {/* Date Presets */}
        <div className="hidden lg:flex items-center rounded-lg bg-slate-100 dark:bg-slate-800/80 border border-slate-200/80 dark:border-slate-700 p-0.5">
          {presets.map((p) => (
            <button
              key={p.id}
              onClick={() => setDatePreset(p.id)}
              className={`px-2.5 py-1 text-xs font-semibold rounded-md transition ${
                datePreset === p.id
                  ? 'bg-white dark:bg-emerald-600 text-slate-900 dark:text-white shadow-sm'
                  : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>

        {/* Auto Refresh Menu */}
        <div className="flex items-center space-x-1.5 px-2 py-1 rounded-lg bg-slate-100 dark:bg-slate-800/80 border border-slate-200/80 dark:border-slate-700 text-xs">
          <Clock className="w-3.5 h-3.5 text-slate-400" />
          <select
            value={autoRefreshInterval}
            onChange={(e) => setAutoRefreshInterval(Number(e.target.value))}
            className="bg-transparent text-xs font-semibold text-slate-700 dark:text-slate-300 focus:outline-none cursor-pointer"
          >
            <option value={0}>Manual</option>
            <option value={5}>Every 5s</option>
            <option value={15}>Every 15s</option>
            <option value={30}>Every 30s</option>
          </select>
        </div>

        {/* Refresh Button */}
        <button
          onClick={handleManualRefresh}
          title="Refresh Data"
          className="p-2 rounded-lg bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-600 dark:text-slate-300 border border-slate-200/80 dark:border-slate-700 transition"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${isRotating ? 'animate-spin text-emerald-600 dark:text-emerald-400' : ''}`} />
        </button>

        {/* Auth / Token Modal Trigger */}
        <button
          onClick={() => {
            setTempToken(token);
            setTokenModalOpen(true);
          }}
          title="API Authentication Key"
          className={`p-2 rounded-lg border transition ${
            token
              ? 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-600 dark:text-emerald-400 border-emerald-200 dark:border-emerald-800/60'
              : 'bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-200/80 dark:border-slate-700'
          }`}
        >
          <Key className="w-3.5 h-3.5" />
        </button>

        {/* Theme Toggle */}
        <button
          onClick={toggleTheme}
          title={`Switch to ${theme === 'dark' ? 'Light' : 'Dark'} Theme`}
          className="p-2 rounded-lg bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-600 dark:text-slate-300 border border-slate-200/80 dark:border-slate-700 transition"
        >
          {theme === 'dark' ? <Sun className="w-3.5 h-3.5 text-amber-400" /> : <Moon className="w-3.5 h-3.5 text-emerald-600" />}
        </button>
      </div>

      {/* Auth Token Modal */}
      {tokenModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm animate-in fade-in">
          <div className="w-full max-w-md p-6 rounded-2xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 shadow-2xl">
            <div className="flex items-center space-x-3 mb-4">
              <div className="p-2.5 rounded-xl bg-emerald-50 dark:bg-emerald-950/50 text-emerald-600 dark:text-emerald-400">
                <Key className="w-5 h-5" />
              </div>
              <div>
                <h3 className="font-bold text-base text-slate-900 dark:text-white">
                  Analytics Bearer Authentication
                </h3>
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  Configure server-issued bearer key for tenant routes
                </p>
              </div>
            </div>

            <p className="text-xs text-slate-600 dark:text-slate-300 mb-4 leading-relaxed">
              In AstraRoll V2, tenant analytics are cryptographically authorized against SHA-256 digests in <code className="px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-[11px] font-mono">ANALYTICS_API_KEYS_JSON</code>.
            </p>

            <div className="space-y-3 mb-6">
              <label className="block text-xs font-semibold text-slate-700 dark:text-slate-300">
                Bearer Token (Opaque Key)
              </label>
              <input
                type="text"
                placeholder="Enter issued bearer token..."
                value={tempToken}
                onChange={(e) => setTempToken(e.target.value)}
                className="w-full px-3.5 py-2.5 text-xs font-mono rounded-lg bg-slate-50 dark:bg-slate-800/80 border border-slate-200 dark:border-slate-700 text-slate-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-emerald-500"
              />
            </div>

            <div className="flex items-center justify-end space-x-2">
              <button
                onClick={() => setTokenModalOpen(false)}
                className="px-4 py-2 text-xs font-semibold rounded-lg text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 transition"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  setToken(tempToken.trim());
                  setTokenModalOpen(false);
                  triggerRefresh();
                }}
                className="px-4 py-2 text-xs font-semibold rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white shadow-md shadow-emerald-600/20 transition"
              >
                Save Credentials
              </button>
            </div>
          </div>
        </div>
      )}
    </header>
  );
};

