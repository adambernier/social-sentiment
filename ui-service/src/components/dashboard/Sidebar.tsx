import React from "react";
import { Zap, TrendingUp } from "lucide-react";
import { format } from "date-fns";
import { ResponsiveContainer, BarChart, XAxis, YAxis, Tooltip, ReferenceLine, Bar, Cell } from "recharts";
import { cn } from "./utils";
import { topicProgressColors } from "./constants";
import { DashboardDataProps } from "../types";
import SourceHealthPanel from "./SourceHealthPanel";

export default function Sidebar({ state, computed }: DashboardDataProps) {
  const { correlationData, metrics, sourceHealth } = state;
  const { scorecardData, sortedTopics, totalTopicCount } = computed;

  return (
    <div className="xl:col-span-4 space-y-6">

      {/* Pipeline / data-source health (read-only ops view) */}
      <SourceHealthPanel sources={sourceHealth || []} />

      {/* Opportunity Scanner */}
      <section className={cn(
        "bg-slate-900/40 backdrop-blur-xl border rounded-2xl p-6 shadow-2xl relative overflow-hidden flex flex-col transition-all duration-300",
        correlationData.opportunity
          ? (correlationData.opportunity.color === 'emerald' ? 'border-emerald-500/25 shadow-[0_0_20px_rgba(16,185,129,0.08)]' :
             correlationData.opportunity.color === 'teal' ? 'border-teal-500/25 shadow-[0_0_20px_rgba(20,184,166,0.08)]' :
             correlationData.opportunity.color === 'rose' ? 'border-rose-500/25 shadow-[0_0_20px_rgba(244,63,94,0.08)]' :
             'border-white/5')
          : 'border-white/5'
      )}>
        <div className="absolute top-0 right-0 p-4 opacity-[0.03] pointer-events-none">
          <Zap className="w-16 h-16 text-indigo-500" />
        </div>
        <h3 className="text-lg font-bold text-white flex items-center gap-2">
          <Zap className="w-4 h-4 text-indigo-400" />
          Opportunity Scanner
        </h3>
        <p className="text-xs text-slate-400 mb-4">Real-time trade opportunity signals & strategies</p>

        {!correlationData.opportunity ? (
          <div className="text-slate-500 text-xs text-center py-8">Calculating opportunity parameters...</div>
        ) : (() => {
          const opp = correlationData.opportunity;
          
          const colorMap: Record<string, { bg: string, text: string, border: string, progress: string }> = {
            emerald: { bg: 'bg-emerald-500/10', text: 'text-emerald-400', border: 'border-emerald-500/20', progress: 'bg-emerald-500' },
            teal: { bg: 'bg-teal-500/10', text: 'text-teal-400', border: 'border-teal-500/20', progress: 'bg-teal-500' },
            slate: { bg: 'bg-slate-500/10', text: 'text-slate-400', border: 'border-white/10', progress: 'bg-slate-400' },
            rose: { bg: 'bg-rose-500/10', text: 'text-rose-400', border: 'border-rose-500/20', progress: 'bg-rose-500' },
          };
          const theme = colorMap[opp.color] || colorMap.slate;

          const checklistItems = [
            { key: "support", text: "Price near support zone", active: opp.checklist.some((s: string) => s.toLowerCase().includes("support")) },
            { key: "crossover", text: "Bullish sentiment crossover", active: opp.checklist.some((s: string) => s.toLowerCase().includes("crossover")) },
            { key: "divergence", text: "Bullish sentiment divergence", active: opp.checklist.some((s: string) => s.toLowerCase().includes("divergence")) },
            { key: "valuation", text: "Favorable relative valuation", active: opp.checklist.some((s: string) => s.toLowerCase().includes("valued")) },
          ];

          return (
            <div className="space-y-5">
              <div>
                <div className="flex items-center justify-between mb-2">
                  <span className={cn("text-[10px] font-bold px-2 py-0.5 rounded border uppercase tracking-wider", theme.bg, theme.text, theme.border)}>
                    {opp.classification}
                  </span>
                  <span className="text-xs font-bold text-white">{Math.round(opp.score)}% Setup Score</span>
                </div>
                <div className="h-1.5 w-full bg-slate-800 rounded-full overflow-hidden">
                  <div className={cn("h-full rounded-full transition-all duration-500", theme.progress)} style={{ width: `${opp.score}%` }}></div>
                </div>
              </div>

              <div className="bg-slate-950/50 border border-white/5 rounded-xl p-4">
                <div className="text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">Recommended Strategy</div>
                <div className="text-sm font-extrabold text-white flex items-center gap-1.5 mb-1">
                  <TrendingUp className={cn("w-4 h-4 shrink-0", opp.score >= 50.0 ? "text-emerald-400" : "text-slate-400")} />
                  {opp.strategy}
                </div>
                <p className="text-xs text-slate-400 leading-relaxed font-medium">{opp.description}</p>
              </div>

              <div className="space-y-2">
                <div className="text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-2">Signal Checklist</div>
                {checklistItems.map((item) => (
                  <div key={item.key} className="flex items-center gap-2.5">
                    <div className={cn(
                      "w-4 h-4 rounded-full flex items-center justify-center border text-[9px] font-bold shrink-0",
                      item.active
                        ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                        : "bg-slate-950 text-slate-600 border-white/5"
                    )}>
                      {item.active ? "✓" : "○"}
                    </div>
                    <span className={cn("text-xs font-medium", item.active ? "text-slate-200" : "text-slate-500")}>
                      {item.text}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          );
        })()}
      </section>

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

      {/* Relative Performance Scorecard */}
      <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl flex flex-col">
        <h3 className="text-lg font-bold mb-1 text-white">Sector Scorecard</h3>
        <p className="text-xs text-slate-400 mb-6">Relative outperformance vs industry benchmark</p>
        
        <div className="h-[250px] w-full mt-4">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={scorecardData} layout="vertical" margin={{ top: 0, right: 20, left: 20, bottom: 0 }}>
              <XAxis 
                type="number" 
                domain={[-150, 150]} 
                tickFormatter={(v) => `${Math.round(v)}%`} 
                stroke="#475569" 
                tickLine={false} 
                axisLine={{ stroke: '#334155' }}
                tick={{ fontSize: 11 }}
              />
              <YAxis 
                dataKey="name" 
                type="category" 
                stroke="#94a3b8" 
                tickLine={false} 
                axisLine={false}
                tick={{ fontSize: 12, fontWeight: 500 }}
                width={70}
              />
              <Tooltip 
                cursor={{fill: 'rgba(255,255,255,0.02)'}} 
                contentStyle={{ backgroundColor: 'rgba(15, 23, 42, 0.9)', borderColor: '#334155', borderRadius: '8px', backdropFilter: 'blur(8px)' }} 
                formatter={(value: any) => [`${value > 0 ? '+' : ''}${Number(value).toFixed(1)}%`, 'vs. Sector']}
                itemStyle={{ fontSize: '13px' }}
              />
              <ReferenceLine x={0} stroke="#64748b" />
              <Bar dataKey="value" barSize={24} radius={[0, 4, 4, 0]}>
                {scorecardData.map((entry: any, index: number) => (
                  <Cell key={`cell-${index}`} fill={entry.value >= 0 ? '#10b981' : '#f43f5e'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
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
