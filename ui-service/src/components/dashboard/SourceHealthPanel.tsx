import React from "react";
import { Activity } from "lucide-react";
import { cn } from "./utils";
import { platformLabels } from "./constants";
import { SourceHealth } from "../types";

const STATUS: Record<SourceHealth["status"], { dot: string; text: string }> = {
  active: { dot: "bg-emerald-500", text: "text-emerald-400" },
  quiet: { dot: "bg-amber-500", text: "text-amber-400" },
  silent: { dot: "bg-rose-500 animate-pulse", text: "text-rose-400" },
};

function ageLabel(seconds: number | null): string {
  if (seconds == null) return "no data";
  if (seconds < 90) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export default function SourceHealthPanel({ sources }: { sources: SourceHealth[] }) {
  const silentCount = sources.filter((s) => s.status === "silent").length;

  return (
    <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl">
      <div className="flex items-center justify-between mb-1">
        <h3 className="text-lg font-bold text-white flex items-center gap-2">
          <Activity className="w-4 h-4 text-indigo-400" />
          Source Health
        </h3>
        {silentCount > 0 && (
          <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20">
            {silentCount} silent
          </span>
        )}
      </div>
      <p className="text-xs text-slate-400 mb-4">Ingestion recency &amp; volume per data source</p>

      <div className="space-y-2">
        {sources.length === 0 && (
          <div className="text-center text-slate-500 text-xs py-4">No source data available.</div>
        )}
        {sources.map((s) => {
          const st = STATUS[s.status] ?? STATUS.silent;
          return (
            <div
              key={s.platform}
              className="flex items-center justify-between p-2.5 bg-white/5 rounded-xl border border-white/5"
              title={`${s.posts_1h} posts in last 1h · ${s.posts_24h} in 24h`}
            >
              <div className="flex items-center gap-2.5 min-w-0">
                <span className={cn("w-2 h-2 rounded-full shrink-0", st.dot)} />
                <span className="text-sm font-semibold text-slate-200 truncate">
                  {platformLabels[s.platform] ?? s.platform}
                </span>
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <span className="text-[10px] text-slate-500">{s.posts_24h} / 24h</span>
                <span className={cn("text-[11px] font-medium w-16 text-right", st.text)}>
                  {ageLabel(s.age_seconds)}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
