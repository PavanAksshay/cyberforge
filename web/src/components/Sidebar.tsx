import React, { useState } from 'react';
import {
  LayoutDashboard,
  TrendingUp,
  UploadCloud,
  Bot,
  Activity,
  Network,
  Database,
  Terminal,
  Copy,
  Check,
  Code,
  ShieldCheck,
  ChevronDown,
} from 'lucide-react';
import { useApp } from '../context/AppContext';

export type PageTab = 'overview' | 'analytics' | 'ingestion' | 'assistant' | 'operations' | 'architecture';

interface SidebarProps {
  activeTab: PageTab;
  onTabChange: (tab: PageTab) => void;
}

export const Sidebar: React.FC<SidebarProps> = ({ activeTab, onTabChange }) => {
  const { activeTenant, currency, startDate, endDate, datePreset } = useApp();
  const [copied, setCopied] = useState<boolean>(false);
  const [queryMode, setQueryMode] = useState<'rollup' | 'raw' | 'psql'>('rollup');

  const navItems: { id: PageTab; label: string; icon: React.ReactNode; badge?: string }[] = [
    {
      id: 'overview',
      label: 'Command Center',
      icon: <LayoutDashboard className="w-5 h-5" />,
    },
    {
      id: 'analytics',
      label: 'Advanced Analytics',
      icon: <TrendingUp className="w-5 h-5" />,
    },
    {
      id: 'ingestion',
      label: 'CSV Ingestion',
      icon: <UploadCloud className="w-5 h-5" />,
      badge: 'Live',
    },
    {
      id: 'assistant',
      label: 'AI Assistant',
      icon: <Bot className="w-5 h-5" />,
      badge: 'Groq',
    },
    {
      id: 'operations',
      label: 'Tenant Ops & Freshness',
      icon: <Activity className="w-5 h-5" />,
    },
    {
      id: 'architecture',
      label: 'Architecture & 10M Scale',
      icon: <Network className="w-5 h-5" />,
      badge: '10M',
    },
  ];

  // Format tenant name cleanly (e.g. "Synthetic 33aa... / 33" -> "Enterprise Tenant #33")
  const formatTenantDisplayName = (name?: string) => {
    if (!name) return 'Primary Tenant';
    const match = name.match(/\/ (\d+)$/);
    if (match) {
      return `Enterprise Tenant #${match[1]}`;
    }
    return name;
  };

  // Physical database name in PostgreSQL 55433
  const rawIdClean = activeTenant ? activeTenant.id.replace(/-/g, '') : '';
  const tenantDbName = activeTenant ? `v2bench_78b1ee4a_${rawIdClean}` : 'v2bench_control_78b1ee4a';

  // Format start and end timestamps for SQL
  const formattedStart = startDate ? startDate.replace('T', ' ').replace('Z', '+00') : '2026-10-01 00:00:00+00';
  const formattedEnd = endDate ? endDate.replace('T', ' ').replace('Z', '+00') : '2026-10-08 00:00:00+00';

  // Dynamic SQL Generation based on current UI selection
  const rollupSql = `SELECT 
  SUM(order_count) AS total_orders, 
  SUM(sales_amount) AS total_shipped_sales 
FROM hourly_summary_aggregates 
WHERE currency = '${currency}' 
  AND bucket_start >= '${formattedStart}' 
  AND bucket_start < '${formattedEnd}';`;

  const rawSql = `SELECT 
  COUNT(*) AS total_orders, 
  COALESCE(SUM(CASE WHEN status='shipped' THEN amount ELSE 0 END), 0) AS total_shipped_sales 
FROM orders 
WHERE currency = '${currency}' 
  AND created_at >= '${formattedStart}' 
  AND created_at < '${formattedEnd}';`;

  const psqlOneLiner = `psql -h 127.0.0.1 -p 55433 -U astraroll -d ${tenantDbName} -c "SELECT SUM(order_count) AS total_orders, SUM(sales_amount) AS total_shipped_sales FROM hourly_summary_aggregates WHERE currency='${currency}' AND bucket_start >= '${formattedStart}' AND bucket_start < '${formattedEnd}';"`;

  const activeQueryText = queryMode === 'rollup' ? rollupSql : queryMode === 'raw' ? rawSql : psqlOneLiner;

  const handleCopy = () => {
    navigator.clipboard.writeText(activeQueryText);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <aside className="w-80 shrink-0 border-r bg-[#051c15]/95 backdrop-blur-md border-[#0f4838] flex flex-col justify-between p-4 overflow-y-auto transition-colors space-y-6">
      <div className="space-y-6">
        {/* Navigation Section */}
        <div>
          <div className="px-3 mb-3 text-xs font-bold uppercase tracking-wider text-emerald-400/80">
            Analytics Platform
          </div>
          <nav className="space-y-1.5">
            {navItems.map((item) => {
              const isActive = activeTab === item.id;
              return (
                <button
                  key={item.id}
                  onClick={() => onTabChange(item.id)}
                  className={`w-full flex items-center justify-between px-3.5 py-2.5 rounded-2xl text-sm font-semibold transition-all ${
                    isActive
                      ? 'bg-gradient-to-r from-emerald-600 to-emerald-700 text-white shadow-lg shadow-emerald-900/30'
                      : 'text-slate-300 hover:text-white hover:bg-[#092e23] border border-transparent hover:border-[#104d3b]'
                  }`}
                >
                  <div className="flex items-center space-x-3">
                    <span className={isActive ? 'text-white' : 'text-emerald-400'}>
                      {item.icon}
                    </span>
                    <span className="font-medium text-sm">{item.label}</span>
                  </div>
                  {item.badge && (
                    <span
                      className={`px-2 py-0.5 text-xs font-bold rounded-lg ${
                        isActive
                          ? 'bg-white/20 text-white'
                          : 'bg-emerald-950/80 text-emerald-300 border border-emerald-800'
                      }`}
                    >
                      {item.badge}
                    </span>
                  )}
                </button>
              );
            })}
          </nav>
        </div>

        {/* Dynamic SQL Verification Card (Live Cross-Check Generator) */}
        {activeTenant && (
          <div className="p-4 rounded-2xl bg-[#062118] border border-[#0d3d2f] shadow-lg space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <Code className="w-4 h-4 text-emerald-400" />
                <span className="text-xs font-extrabold uppercase tracking-wider text-emerald-300">
                  Live SQL Cross-Verify
                </span>
              </div>
              <button
                onClick={handleCopy}
                className="flex items-center space-x-1 px-2.5 py-1 rounded-lg bg-emerald-950 hover:bg-emerald-900 text-emerald-300 border border-emerald-800 text-[11px] font-bold font-mono transition"
                title="Copy SQL command to clipboard"
              >
                {copied ? (
                  <>
                    <Check className="w-3 h-3 text-emerald-400" />
                    <span>Copied!</span>
                  </>
                ) : (
                  <>
                    <Copy className="w-3 h-3 text-slate-300" />
                    <span>Copy</span>
                  </>
                )}
              </button>
            </div>

            {/* Target Database Info */}
            <div className="p-2 rounded-xl bg-[#03150f] border border-[#092c21] flex items-center justify-between text-[11px] font-mono">
              <span className="text-slate-400">Target DB:</span>
              <span className="text-emerald-300 font-bold truncate max-w-[170px]" title={tenantDbName}>
                {tenantDbName}
              </span>
            </div>

            {/* Mode Selector Tabs */}
            <div className="grid grid-cols-3 gap-1 p-0.5 rounded-lg bg-[#03150f] border border-[#092c21] text-[10px] font-semibold">
              <button
                onClick={() => setQueryMode('rollup')}
                className={`py-1 rounded-md transition ${
                  queryMode === 'rollup' ? 'bg-emerald-700 text-white font-bold' : 'text-slate-400 hover:text-white'
                }`}
              >
                Rollup
              </button>
              <button
                onClick={() => setQueryMode('raw')}
                className={`py-1 rounded-md transition ${
                  queryMode === 'raw' ? 'bg-emerald-700 text-white font-bold' : 'text-slate-400 hover:text-white'
                }`}
              >
                Raw Orders
              </button>
              <button
                onClick={() => setQueryMode('psql')}
                className={`py-1 rounded-md transition ${
                  queryMode === 'psql' ? 'bg-emerald-700 text-white font-bold' : 'text-slate-400 hover:text-white'
                }`}
              >
                psql CLI
              </button>
            </div>

            {/* Live Generated SQL Code Block */}
            <div className="relative group">
              <pre className="p-2.5 rounded-xl bg-[#03140e] border border-[#0a3528] text-[11px] font-mono text-emerald-300/90 leading-tight overflow-x-auto max-h-36 select-all scrollbar-thin">
                {activeQueryText}
              </pre>
            </div>

            <div className="text-[10px] text-slate-400 leading-tight flex items-center justify-between font-mono">
              <span>Preset: <strong className="text-emerald-400 uppercase">{datePreset}</strong></span>
              <span>Currency: <strong className="text-emerald-400">{currency}</strong></span>
            </div>
          </div>
        )}

        {/* Tenant Database Proof Card */}
        {activeTenant && (
          <div className="p-3.5 rounded-2xl bg-[#082920] border border-[#114f3c] shadow-sm space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <Database className="w-3.5 h-3.5 text-emerald-400" />
                <span className="text-[11px] font-bold uppercase tracking-wider text-emerald-300">
                  Active Physical Tenant
                </span>
              </div>
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-950 text-emerald-300 border border-emerald-800">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1 animate-pulse" />
                Port 55433
              </span>
            </div>

            <div>
              <div className="text-xs font-bold text-white truncate">
                {formatTenantDisplayName(activeTenant.name)}
              </div>
              <div className="text-[11px] font-mono text-emerald-400/80 truncate">
                v2bench_{activeTenant.id.slice(0, 8)}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Metric Integrity Guard */}
      <div className="p-3 rounded-2xl bg-[#07241c] border border-[#0f4536] text-[11px]">
        <div className="flex items-center space-x-1.5 text-emerald-300 font-semibold mb-0.5">
          <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
          <span className="font-bold text-[10px] uppercase tracking-wider">100% SQL Mathematical Parity</span>
        </div>
        <p className="text-slate-300 leading-snug">
          Paste the dynamic SQL directly into PostgreSQL to verify identical counts and dollars.
        </p>
      </div>
    </aside>
  );
};
