import React from 'react';
import { Info } from 'lucide-react';

interface MetricCardProps {
  title: string;
  value: string;
  subtitle?: string;
  icon: React.ReactNode;
  trend?: {
    value: string;
    isPositive?: boolean;
    label?: string;
  };
  tooltip?: string;
  highlight?: boolean;
}

export const MetricCard: React.FC<MetricCardProps> = ({
  title,
  value,
  subtitle,
  icon,
  trend,
  tooltip,
  highlight,
}) => {
  return (
    <div
      className={`relative p-5 rounded-2xl border transition duration-200 group ${
        highlight
          ? 'bg-gradient-to-br from-emerald-50/50 via-white to-white dark:from-emerald-950/20 dark:via-[#07261d] dark:to-[#07261d] border-emerald-200 dark:border-emerald-800/80 shadow-sm'
          : 'bg-white dark:bg-[#07261d] border-slate-200/80 dark:border-slate-800 shadow-sm hover:border-slate-300 dark:hover:border-slate-700'
      }`}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center space-x-2">
          <span className="text-sm font-bold text-slate-300 dark:text-emerald-300/90">
            {title}
          </span>
          {tooltip && (
            <div className="relative group/tip">
              <Info className="w-3.5 h-3.5 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 cursor-help transition" />
              <div className="absolute left-1/2 -translate-x-1/2 bottom-full mb-1.5 hidden group-hover/tip:block w-48 p-2 rounded-lg bg-slate-900 text-white text-[11px] leading-relaxed shadow-xl z-50 pointer-events-none">
                {tooltip}
              </div>
            </div>
          )}
        </div>
        <div className="p-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/80 text-emerald-600 dark:text-emerald-400 group-hover:scale-105 transition-transform">
          {icon}
        </div>
      </div>

      <div className="flex items-baseline space-x-2">
        <span className="text-3xl font-black text-slate-900 dark:text-white tracking-tight font-mono">
          {value}
        </span>
      </div>

      {(subtitle || trend) && (
        <div className="flex items-center justify-between mt-3 pt-3 border-t border-slate-100 dark:border-slate-800/80 text-xs">
          {subtitle && (
            <span className="text-slate-500 dark:text-slate-400 truncate max-w-[170px]">
              {subtitle}
            </span>
          )}
          {trend && (
            <span
              className={`font-semibold text-xs ml-auto ${
                trend.isPositive
                  ? 'text-emerald-600 dark:text-emerald-400'
                  : 'text-amber-600 dark:text-amber-400'
              }`}
            >
              {trend.value} <span className="text-[10px] text-slate-400 font-normal">{trend.label}</span>
            </span>
          )}
        </div>
      )}
    </div>
  );
};

