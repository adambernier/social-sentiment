import React from "react";
import { MessageSquare, Newspaper } from "lucide-react";
import { format } from "date-fns";
import { cn } from "./utils";
import { platformColors, platformLabels, topicColors } from "./constants";
import type { DashboardDataProps } from "../hooks/useDashboardData";

export default function Feed({ state, setters, computed }: DashboardDataProps) {
  const { feedTab, selectedHour, selectedTopic, platform, isDrillDownLoading } = state;
  const { setFeedTab, setSelectedHour, setSelectedTopic, setPlatform } = setters;
  const { filteredFeedPosts, socialFeedCount, newsFeedCount } = computed;

  return (
    <div className="xl:col-span-8">
      <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-4 sm:p-6 shadow-2xl flex flex-col h-[700px] sm:h-[800px]">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4 pb-2 border-b border-white/5">
          <div className="flex items-center gap-4 sm:gap-6">
            <button
              type="button"
              onClick={() => setFeedTab("social")}
              className={cn(
                "text-sm sm:text-base font-bold flex items-center gap-2 pb-2 transition-all relative cursor-pointer outline-none",
                feedTab === "social" ? "text-white" : "text-slate-400 hover:text-slate-200"
              )}
            >
              <MessageSquare className="w-4 h-4 text-sky-400 shrink-0" />
              <span>Social Stream</span>
              <span className={cn(
                "text-[11px] font-semibold px-1.5 py-0.5 rounded-full tabular-nums",
                feedTab === "social" ? "bg-indigo-500/20 text-indigo-300" : "bg-white/5 text-slate-400"
              )}>
                {socialFeedCount}
              </span>
              {feedTab === "social" && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-indigo-500 rounded-full"></div>}
            </button>
            <button
              type="button"
              onClick={() => setFeedTab("news")}
              className={cn(
                "text-sm sm:text-base font-bold flex items-center gap-2 pb-2 transition-all relative cursor-pointer outline-none",
                feedTab === "news" ? "text-white" : "text-slate-400 hover:text-slate-200"
              )}
            >
              <Newspaper className="w-4 h-4 text-purple-400 shrink-0" />
              <span>News Desk</span>
              <span className={cn(
                "text-[11px] font-semibold px-1.5 py-0.5 rounded-full tabular-nums",
                feedTab === "news" ? "bg-indigo-500/20 text-indigo-300" : "bg-white/5 text-slate-400"
              )}>
                {newsFeedCount}
              </span>
              {feedTab === "news" && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-indigo-500 rounded-full"></div>}
            </button>
          </div>
          <div className="text-xs text-slate-400 flex flex-wrap items-center gap-2">
            {platform !== "all" && (
              <span className="bg-sky-500/15 text-sky-300 px-2 py-0.5 rounded border border-sky-500/30 flex items-center gap-1.5">
                Source: {platformLabels[platform] || platform}
                <button type="button" onClick={() => setPlatform("all")} className="hover:text-white transition-colors cursor-pointer">✕</button>
              </span>
            )}
            {selectedTopic !== "all" && (
              <span className="bg-purple-500/15 text-purple-300 px-2 py-0.5 rounded border border-purple-500/30 flex items-center gap-1.5">
                Topic: {selectedTopic}
                <button type="button" onClick={() => setSelectedTopic("all")} className="hover:text-white transition-colors cursor-pointer">✕</button>
              </span>
            )}
            {selectedHour && (
              <span className="bg-indigo-500/20 text-indigo-300 px-2 py-0.5 rounded border border-indigo-500/30 flex items-center gap-1.5">
                {format(new Date(selectedHour), "h:mm a")} - {format(new Date(new Date(selectedHour).getTime() + 60*60*1000), "h:mm a")}
                <button type="button" onClick={() => setSelectedHour(null)} className="hover:text-white transition-colors cursor-pointer">✕</button>
              </span>
            )}
            <span className="tabular-nums">Showing {filteredFeedPosts.length} items</span>
          </div>
        </div>
        
        {isDrillDownLoading ? (
          <div className="flex-1 flex flex-col items-center justify-center text-slate-500">
            <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin mb-4"></div>
            Loading historical posts...
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto space-y-3 pr-2 custom-scrollbar">
            {filteredFeedPosts.map((post, idx) => (
            <div key={idx} className="bg-white/5 border border-white/5 rounded-xl p-3 text-sm hover:bg-white/10 transition-colors">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={cn("text-[10px] uppercase font-bold px-1.5 py-0.5 rounded", platformColors[post.platform] || 'bg-slate-500/20 text-slate-400 border border-white/5')}>
                    {platformLabels[post.platform] || post.platform}
                  </span>
                  <span className={cn("text-[10px] uppercase font-bold px-1.5 py-0.5 rounded", post.sentiment === 'positive' ? 'bg-emerald-500/20 text-emerald-400' : post.sentiment === 'negative' ? 'bg-rose-500/20 text-rose-400' : 'bg-slate-500/20 text-slate-400')}>
                    {post.sentiment}
                  </span>
                  {post.topic_label && post.topic_label !== "General / Outlier" && (
                    <span className={cn("text-[10px] font-bold px-1.5 py-0.5 rounded", topicColors[post.topic_label] || 'bg-slate-500/20 text-slate-400 border border-white/5')}>
                      {post.topic_label}
                    </span>
                  )}
                </div>
                <span className="text-[10px] text-slate-500 shrink-0">
                  {format(new Date(post.timestamp), "MMM d, h:mm:ss a")}
                </span>
              </div>
              <p className="text-slate-300 leading-relaxed">{post.text}</p>
            </div>
          ))}
            {filteredFeedPosts.length === 0 && (
              <div className="text-center text-slate-500 mt-10">No items found for this time window.</div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
