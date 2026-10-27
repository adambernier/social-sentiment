import React, { useMemo, useState } from "react";
import { Zap } from "lucide-react";
import { cn } from "./utils";
import type { DashboardDataProps } from "../hooks/useDashboardData";

type SortMode = "buzz" | "sentiment" | "volume";

export default function TickerActivity({ state, setters }: DashboardDataProps) {
  const { leaderboard, symbol } = state;
  const { setSymbol } = setters;
  const [sortMode, setSortMode] = useState<SortMode>("buzz");

  const sortedLeaderboard = useMemo(() => {
    if (!leaderboard) return [];
    const copy = [...leaderboard];
    if (sortMode === "sentiment") {
      return copy.sort((a, b) => b.sentiment_index_4h - a.sentiment_index_4h);
    }
    if (sortMode === "volume") {
      return copy.sort((a, b) => b.post_count_4h - a.post_count_4h);
    }
    return copy.sort((a, b) => (b.buzz_z ?? -999) - (a.buzz_z ?? -999));
  }, [leaderboard, sortMode]);

  if (!leaderboard || leaderboard.length === 0) return null;

  return (
    <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-4 shadow-2xl">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <h3 className="text-sm font-semibold text-slate-300 flex items-center gap-2">
          <Zap className="w-4 h-4 text-amber-400 shrink-0" /> Ticker Activity
          <span className="text-[10px] font-normal text-slate-500 normal-case hidden sm:inline">
            (4h window vs typical baseline)
          </span>
        </h3>
        <div className="flex items-center gap-1 bg-slate-950/60 p-0.5 rounded-lg border border-white/5 text-[10px]">
          {(["buzz", "sentiment", "volume"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => setSortMode(mode)}
              className={cn(
                "px-2 py-0.5 rounded-md font-semibold capitalize transition-colors cursor-pointer",
                sortMode === mode
                  ? "bg-indigo-500/25 text-indigo-300 border border-indigo-500/30"
                  : "text-slate-400 hover:text-slate-200"
              )}
            >
              {mode}
            </button>
          ))}
        </div>
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1.5 custom-scrollbar">
        {sortedLeaderboard.map((item) => {
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
              type="button"
              onClick={() => setSymbol(item.symbol)}
              title={
                `${item.post_count_4h} posts in last 4h · ` +
                (z !== null
                  ? `${z >= 0 ? "+" : ""}${z.toFixed(1)}σ vs typical for these hours on comparable days (n=${item.baseline_samples}h)`
                  : "baseline too sparse to score")
              }
              className={cn(
                "flex-shrink-0 min-w-[116px] flex flex-col gap-1 p-2.5 rounded-xl border text-left transition-all cursor-pointer",
                active
                  ? "bg-indigo-500/20 border-indigo-500/50 shadow-[0_0_12px_rgba(99,102,241,0.18)]"
                  : "bg-slate-950/50 border-white/5 hover:bg-slate-800/60 hover:border-white/10"
              )}
            >
              <div className="flex items-center justify-between w-full min-h-[18px] gap-1.5">
                <span className="font-bold text-white text-sm">{item.symbol}</span>
                {status && (
                  <span className={cn("text-[10px] font-semibold px-1.5 py-0.5 rounded", status.cls)}>
                    {status.label}
                  </span>
                )}
              </div>
              <div className="flex items-center justify-between w-full tabular-nums">
                <span className="text-[10px] text-slate-400">{item.post_count_4h} posts</span>
                <span className={cn(
                  "text-[10px] font-semibold",
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
