import React from "react";
import { Zap, TrendingUp } from "lucide-react";
import { cn } from "./utils";
import { DashboardDataProps } from "../types";

export default function OpportunityScanner({ state }: DashboardDataProps) {
  const { correlationData } = state;

  return (
    <section className={cn(
      "bg-slate-900/40 backdrop-blur-xl border rounded-2xl p-6 shadow-2xl relative overflow-hidden flex flex-col h-full transition-all duration-300",
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
  );
}
