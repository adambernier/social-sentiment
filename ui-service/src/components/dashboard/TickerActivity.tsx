import React from "react";
import { Zap } from "lucide-react";
import { cn } from "./utils";
import type { DashboardDataProps } from "../hooks/useDashboardData";

export default function TickerActivity({ state, setters }: DashboardDataProps) {
  const { leaderboard, symbol } = state;
  const { setSymbol } = setters;

  if (!leaderboard || leaderboard.length === 0) return null;

  return (
    <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-4 shadow-2xl">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-slate-300 flex items-center gap-2">
          <Zap className="w-4 h-4 text-amber-400" /> Ticker Activity
          <span className="text-[10px] font-normal text-slate-500 normal-case">vs typical for this time on similar days</span>
        </h3>
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {leaderboard.map((item) => {
          const active = symbol === item.symbol;
          const si = item.sentiment_index_4h;
          const z = item.buzz_z;
          const status =
            z !== null && z >= 2 && item.post_count_4h >= 10
              ? { label: "Buzzing", cls: "bg-amber-500/20 text-amber-300" }
              : z !== null && z >= 1 && item.post_count_4h >= 5
              ? { label: "Active", cls: "bg-emerald-500/10 text-emerald-400" }
              : null;
              
          return (
            <button
              key={item.symbol}
              onClick={() => setSymbol(item.symbol)}
              title={
                `${item.post_count_4h} posts in last 4h · ` +
                (z !== null
                  ? `${z >= 0 ? "+" : ""}${z.toFixed(1)}σ vs typical for these hours on comparable days (n=${item.baseline_samples}h)`
                  : "baseline too sparse to score")
              }
              className={cn(
                "flex-shrink-0 min-w-[112px] flex flex-col gap-1 p-2.5 rounded-xl border text-left transition-colors cursor-pointer",
                active ? "bg-indigo-500/20 border-indigo-500/40" : "bg-slate-950/40 border-white/5 hover:bg-slate-800/60"
              )}
            >
              <div className="flex items-center justify-between w-full min-h-[18px]">
                <span className="font-bold text-white text-sm">{item.symbol}</span>
                {status && (
                  <span className={cn("text-[10px] font-semibold px-1.5 py-0.5 rounded", status.cls)}>
                    {status.label}
                  </span>
                )}
              </div>
              <div className="flex items-center justify-between w-full">
                <span className="text-[10px] text-slate-500">{item.post_count_4h} posts</span>
                <span className={cn(
                  "text-[10px] font-medium",
                  si > 0.1 ? "text-emerald-400" : si < -0.1 ? "text-rose-400" : "text-slate-400"
                )}>
                  {si > 0 ? "+" : ""}{(si * 100).toFixed(0)}%
                </span>
              </div>
            </button>
          );
        })}
      </div>
    </section>
  );
}
