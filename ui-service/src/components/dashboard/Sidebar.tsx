import React from "react";
import { cn } from "./utils";
import { topicProgressColors } from "./constants";
import type { DashboardDataProps } from "../hooks/useDashboardData";
import SectorScorecard from "./SectorScorecard";

export default function Sidebar({ state, computed, setters }: DashboardDataProps) {
  const { sortedTopics, totalTopicCount } = computed;
  const { selectedTopic } = state;
  const { setSelectedTopic } = setters;

  return (
    <div className="xl:col-span-4 space-y-6">

      {/* Sector Scorecard */}
      <SectorScorecard state={state} computed={computed} setters={setters} />

      {/* Topic Distribution */}
      <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl flex flex-col">
        <div className="flex items-center justify-between mb-1">
          <h3 className="text-lg font-bold text-white">Topic Distribution</h3>
          {selectedTopic !== "all" && (
            <button
              type="button"
              onClick={() => setSelectedTopic("all")}
              className="text-[11px] font-semibold text-indigo-400 hover:text-indigo-300 cursor-pointer"
            >
              Clear filter
            </button>
          )}
        </div>
        <p className="text-xs text-slate-400 mb-4">
          NLP zero-shot classification · click a topic to filter
        </p>

        <div className="space-y-2.5 max-h-[240px] overflow-y-auto pr-1 custom-scrollbar">
          {sortedTopics.map((topic, idx) => {
            const rawLabel = topic.topic_label || "General / Outlier";
            const displayLabel = topic.topic_label === "General / Outlier" || !topic.topic_label ? "General Chat" : topic.topic_label;
            const count = topic.count;
            const pct = totalTopicCount ? Math.round((count / totalTopicCount) * 100) : 0;
            const colorClass = topicProgressColors[rawLabel] || "bg-slate-500";
            const isSelected = selectedTopic === rawLabel;

            return (
              <button
                key={idx}
                type="button"
                onClick={() => setSelectedTopic(isSelected ? "all" : rawLabel)}
                className={cn(
                  "w-full text-left p-2 rounded-xl border transition-all cursor-pointer space-y-1.5",
                  isSelected
                    ? "bg-indigo-500/15 border-indigo-500/40"
                    : "bg-white/[0.02] border-transparent hover:bg-white/5 hover:border-white/5"
                )}
              >
                <div className="flex items-center justify-between text-xs font-semibold tabular-nums">
                  <span className={cn(isSelected ? "text-white" : "text-slate-300")}>
                    {displayLabel}
                  </span>
                  <span className="text-slate-400">{count} ({pct}%)</span>
                </div>
                <div className="h-1.5 w-full bg-slate-800 rounded-full overflow-hidden">
                  <div className={cn("h-full rounded-full transition-all duration-500", colorClass)} style={{ width: `${pct}%` }}></div>
                </div>
              </button>
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
