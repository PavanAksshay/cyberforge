import React, { useState } from 'react';
import {
  Layers,
  ChevronDown,
  RefreshCw,
  Key,
  Sun,
  Moon,
  Clock,
  Check,
  Building2,
  Database,
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
  const presets: { id: DatePreset; label: string; tooltip: string }[] = [
    { id: '24h', label: '24 Hours', tooltip: 'First 24 hours of dataset (Oct 1 - Oct 2)' },
    { id: '3d', label: '3 Days', tooltip: 'First 3 days of dataset (Oct 1 - Oct 4)' },
    { id: 'all', label: 'All 7 Days', tooltip: 'Full dataset coverage (Oct 1 - Oct 8)' },
  ];

  const formatTenantDisplayName = (name?: string) => {
    if (!name) return 'Select Tenant';
    const match = name.match(/\/ (\d+)$/);
    if (match) {
      return `Enterprise Tenant #${match[1]}`;
    }
    return name;
  };

  return (
    <header className="sticky top-0 z-30 flex items-center justify-between px-8 py-4 border-b bg-[#051c15]/95 backdrop-blur-md border-[#0f4838] transition-colors">
      {/* Brand & Mode */}
      <div className="flex items-center space-x-3.5">
        <div className="flex items-center justify-center w-11 h-11 rounded-2xl bg-gradient-to-tr from-emerald-600 to-emerald-500 text-white shadow-lg shadow-emerald-900/30">
          <Layers className="w-6 h-6" />
        </div>
        <div>
          <div className="flex items-center space-x-2.5">
            <span className="font-extrabold text-xl tracking-tight text-white">
              AstraRoll
            </span>
            <span className="px-2.5 py-0.5 text-xs font-bold rounded-lg bg-emerald-950/80 text-emerald-300 border border-emerald-800">
              V2 DB-Per-Tenant
            </span>
          </div>
          <span className="text-xs font-medium text-emerald-400/80">
            10M Record Physical Sharding Platform
          </span>
        </div>
      </div>

      {/* Center Controls (Tenant, Currency, Date Presets, Auto-Refresh) */}
      <div className="flex items-center space-x-3.5">
        {/* Tenant Selector Dropdown */}
        <div className="relative">
          <button
            onClick={() => setTenantDropdownOpen(!tenantDropdownOpen)}
            className="flex items-center space-x-2.5 px-3.5 py-2 rounded-xl bg-[#082920] border border-[#114f3c] text-sm font-semibold text-white hover:border-emerald-500/50 transition shadow-sm"
          >
            <Database className="w-4 h-4 text-emerald-400 shrink-0" />
            <span className="max-w-[160px] truncate">
              {formatTenantDisplayName(activeTenant?.name)}
            </span>
            <ChevronDown className="w-4 h-4 text-slate-400" />
          </button>

          {tenantDropdownOpen && (
            <div className="absolute left-0 mt-2 w-72 max-h-96 overflow-y-auto rounded-2xl bg-[#07261d] border border-[#12533f] shadow-2xl p-2 z-50">
              <div className="px-3 py-2 text-xs font-bold uppercase tracking-wider text-emerald-400 border-b border-[#0f4738] mb-1">
                Select Isolated Database ({tenants.length} Tenants)
              </div>
              <div className="space-y-1">
                {tenants.map((t) => (
                  <button
                    key={t.id}
                    onClick={() => {
                      setActiveTenant(t);
                      setTenantDropdownOpen(false);
                    }}
                    className={`w-full flex items-center justify-between px-3 py-2 text-xs rounded-xl transition ${
                      activeTenant?.id === t.id
                        ? 'bg-emerald-600 text-white font-bold'
                        : 'text-slate-300 hover:bg-[#0c382b]'
                    }`}
                  >
                    <div className="text-left truncate mr-2">
                      <div className="truncate font-medium">{formatTenantDisplayName(t.name)}</div>
                      <div className="text-[10px] text-emerald-300/70 font-mono truncate">
                        {t.id}
                      </div>
                    </div>
                    {activeTenant?.id === t.id && <Check className="w-4 h-4 shrink-0 text-white" />}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Currency Selector */}
        <div className="flex items-center rounded-xl bg-[#082920] border border-[#114f3c] p-1">
          {currencies.map((c) => (
            <button
              key={c}
              onClick={() => setCurrency(c)}
              className={`px-3 py-1.5 text-xs font-bold rounded-lg transition ${
                currency === c
                  ? 'bg-emerald-600 text-white shadow-sm'
                  : 'text-slate-300 hover:text-white'
              }`}
            >
              {c}
            </button>
          ))}
        </div>

        {/* Date Presets (24h, 3d, All 7 Days) */}
        <div className="hidden lg:flex items-center rounded-xl bg-[#082920] border border-[#114f3c] p-1">
          {presets.map((p) => (
            <button
              key={p.id}
              onClick={() => setDatePreset(p.id)}
              title={p.tooltip}
              className={`px-3 py-1.5 text-xs font-semibold rounded-lg transition ${
                datePreset === p.id
                  ? 'bg-emerald-600 text-white shadow-sm'
                  : 'text-slate-300 hover:text-white'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>

        {/* Auto Refresh Menu (With clear label explaining what it does) */}
        <div
          title="Auto-refresh periodically re-fetches analytics from the database so live CSV ingestion updates automatically without clicking refresh"
          className="flex items-center space-x-2 px-3 py-1.5 rounded-xl bg-[#082920] border border-[#114f3c] text-xs"
        >
          <Clock className="w-4 h-4 text-emerald-400" />
          <select
            value={autoRefreshInterval}
            onChange={(e) => setAutoRefreshInterval(Number(e.target.value))}
            className="bg-transparent text-xs font-semibold text-slate-200 focus:outline-none cursor-pointer"
          >
            <option value={0} className="bg-[#07261d] text-white">Auto-Refresh: Off</option>
            <option value={5} className="bg-[#07261d] text-white">Auto-Refresh: 5s</option>
            <option value={15} className="bg-[#07261d] text-white">Auto-Refresh: 15s</option>
            <option value={30} className="bg-[#07261d] text-white">Auto-Refresh: 30s</option>
          </select>
        </div>

        {/* Manual Refresh Button */}
        <button
          onClick={handleManualRefresh}
          className="p-2 rounded-xl bg-[#082920] border border-[#114f3c] text-slate-300 hover:text-white hover:bg-[#0c382b] transition"
          title="Refresh analytics data"
        >
          <RefreshCw className={`w-4 h-4 text-emerald-400 ${isRotating ? 'animate-spin' : ''}`} />
        </button>

        {/* Auth Token Key Button */}
        <button
          onClick={() => {
            setTempToken(token);
            setTokenModalOpen(true);
          }}
          className="p-2 rounded-xl bg-[#082920] border border-[#114f3c] text-slate-300 hover:text-white hover:bg-[#0c382b] transition"
          title="API Bearer Token"
        >
          <Key className="w-4 h-4 text-emerald-400" />
        </button>
      </div>

      {/* Token Modal */}
      {tokenModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
          <div className="w-full max-w-md p-6 rounded-3xl bg-[#07261d] border border-[#12533f] text-white space-y-4 shadow-2xl">
            <div className="flex items-center space-x-2.5">
              <Key className="w-5 h-5 text-emerald-400" />
              <h3 className="font-bold text-base">API Authentication Token</h3>
            </div>
            <p className="text-xs text-slate-300 leading-relaxed">
              Pre-configured with verified demo key. Passed as <code className="text-emerald-400 font-mono">Authorization: Bearer &lt;token&gt;</code> to port 8000.
            </p>
            <input
              type="text"
              value={tempToken}
              onChange={(e) => setTempToken(e.target.value)}
              className="w-full px-3.5 py-2.5 rounded-xl bg-[#051c15] border border-[#114f3c] font-mono text-xs text-emerald-300 focus:outline-none focus:border-emerald-500"
            />
            <div className="flex justify-end space-x-2 pt-2">
              <button
                onClick={() => setTokenModalOpen(false)}
                className="px-4 py-2 rounded-xl text-xs font-semibold text-slate-300 hover:bg-[#0c382b]"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  setToken(tempToken);
                  setTokenModalOpen(false);
                }}
                className="px-4 py-2 rounded-xl text-xs font-semibold bg-emerald-600 text-white hover:bg-emerald-500"
              >
                Save Token
              </button>
            </div>
          </div>
        </div>
      )}
    </header>
  );
};
