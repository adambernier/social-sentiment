import React from "react";
import { format } from "date-fns";
import { cn } from "./utils";
import { DashboardDataProps } from "../types";

export default function FundamentalMetrics({ state }: DashboardDataProps) {
  const { metrics } = state;

  return (
    <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl h-full">
      <h3 className="text-lg font-bold mb-5 text-white">Fundamental Metrics</h3>
      <div className="space-y-3">
        <div className="flex items-center justify-between p-3 bg-white/5 rounded-xl border border-white/5">
          <span className="text-slate-400 font-medium text-sm">P/E Ratio</span>
          <span className="font-bold text-white">{metrics?.pe_ratio !== undefined && metrics?.pe_ratio !== null ? metrics.pe_ratio.toFixed(0) : '---'}</span>
        </div>
        <div className="flex items-center justify-between p-3 bg-white/5 rounded-xl border border-white/5">
          <span className="text-slate-400 font-medium text-sm">Beta</span>
          <span className="font-bold text-white">{metrics?.beta !== undefined && metrics?.beta !== null ? metrics.beta.toFixed(2) : '---'}</span>
        </div>
        <div className="flex items-center justify-between p-3 bg-white/5 rounded-xl border border-white/5">
          <span className="text-slate-400 font-medium text-sm">Annual Return</span>
          <span className={cn("font-bold", metrics?.avg_return_1y && metrics.avg_return_1y >= 0 ? "text-emerald-400" : "text-rose-400")}>
            {metrics?.avg_return_1y !== undefined && metrics?.avg_return_1y !== null ? `${(metrics.avg_return_1y * 100).toFixed(0)}%` : '---'}
          </span>
        </div>
        <div className="flex items-center justify-between p-3 bg-white/5 rounded-xl border border-white/5">
          <span className="text-slate-400 font-medium text-sm">Inflation Adj (1Y)</span>
          <span className={cn("font-bold", metrics?.inflation_adj_return_1y && metrics.inflation_adj_return_1y >= 0 ? "text-emerald-400" : "text-rose-400")}>
            {metrics?.inflation_adj_return_1y !== undefined && metrics?.inflation_adj_return_1y !== null ? `${(metrics.inflation_adj_return_1y * 100).toFixed(0)}%` : '---'}
          </span>
        </div>
      </div>
      <div className="mt-5 text-xs text-center text-slate-500 font-medium tracking-wide">
        LAST UPDATED: {metrics?.updated_at ? format(new Date(metrics.updated_at), "MMM d, h:mm a 'ET'") : '---'}
      </div>
    </section>
  );
}
