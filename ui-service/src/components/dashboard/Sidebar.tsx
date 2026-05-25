import React from "react";
import { format } from "date-fns";
import { cn } from "./utils";
import { topicProgressColors } from "./constants";
import { DashboardDataProps } from "../types";

export default function Sidebar({ state, computed }: DashboardDataProps) {
  const { metrics } = state;
  const { sortedTopics, totalTopicCount } = computed;

  return (
    <div className="xl:col-span-4 space-y-6">

      {/* Fundamental Metrics */}
      <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl">
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

      {/* Topic Distribution */}
      <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl flex flex-col">
        <h3 className="text-lg font-bold mb-1 text-white">Topic Distribution</h3>
        <p className="text-xs text-slate-400 mb-4">NLP zero-shot classification of social volume</p>

        <div className="space-y-3 max-h-[220px] overflow-y-auto pr-1 scrollbar-thin scrollbar-thumb-slate-800 scrollbar-track-transparent">
          {sortedTopics.map((topic: any, idx: number) => {
            const label = topic.topic_label || "General Chat";
            const count = topic.count;
            const pct = totalTopicCount ? Math.round((count / totalTopicCount) * 100) : 0;
            const colorClass = topicProgressColors[label] || "bg-slate-500";

            return (
              <div key={idx} className="space-y-1">
                <div className="flex items-center justify-between text-xs font-semibold">
                  <span className="text-slate-300">{label}</span>
                  <span className="text-slate-400">{count} ({pct}%)</span>
                </div>
                <div className="h-1.5 w-full bg-slate-800 rounded-full overflow-hidden">
                  <div className={cn("h-full rounded-full transition-all duration-500", colorClass)} style={{ width: `${pct}%` }}></div>
                </div>
              </div>
            );
          })}
          {sortedTopics.length === 0 && (
            <div className="text-center text-slate-500 text-xs py-4">No topic data available.</div>
          )}
        </div>
      </section>

    </div>
  );
}
