import React from "react";
import { format } from "date-fns";
import { cn } from "./utils";
import { topicProgressColors } from "./constants";
import { DashboardDataProps } from "../types";
import SectorScorecard from "./SectorScorecard";

export default function Sidebar({ state, computed, setters }: DashboardDataProps) {
  const { metrics } = state;
  const { sortedTopics, totalTopicCount } = computed;

  return (
    <div className="xl:col-span-4 space-y-6">

      {/* Sector Scorecard */}
      <SectorScorecard state={state} computed={computed} setters={setters} />

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
