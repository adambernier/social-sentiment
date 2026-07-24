import {
  ArrowUpRight,
  Clock3,
  Globe2,
  RefreshCw,
  Waves,
} from "lucide-react";
import { useGlobalContext } from "../hooks/useGlobalContext";
import type { GlobalFactor } from "../types";
import { cn } from "./utils";


interface GlobalContextPanelProps {
  symbol: string;
}

function signedPercent(value: number | null, digits = 2) {
  if (value === null) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

function relativeTime(timestamp: string | null) {
  if (!timestamp) return "No update";
  const seconds = Math.max(
    0,
    Math.round((Date.now() - new Date(timestamp).getTime()) / 1000),
  );
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function movementColor(value: number | null) {
  if (value === null) return "text-slate-500";
  if (value > 0) return "text-emerald-300";
  if (value < 0) return "text-rose-300";
  return "text-slate-300";
}

function relationshipColor(factor: GlobalFactor) {
  const correlation = factor.relationship.correlation;
  if (correlation === null) return "border-slate-800 text-slate-500";
  if (factor.relationship.strength === "strong") {
    return "border-amber-400/40 text-amber-200";
  }
  if (factor.relationship.strength === "moderate") {
    return "border-cyan-400/30 text-cyan-200";
  }
  return "border-slate-700 text-slate-300";
}

function LoadingPanel() {
  return (
    <section
      aria-label="Loading overnight global context"
      className="overflow-hidden rounded-2xl border border-cyan-400/10 bg-slate-950/70"
    >
      <div className="h-1 animate-pulse bg-gradient-to-r from-cyan-500/20 via-amber-300/70 to-indigo-500/20" />
      <div className="space-y-4 p-6">
        <div className="h-5 w-56 animate-pulse rounded bg-slate-800" />
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {[0, 1, 2, 3].map((item) => (
            <div
              key={item}
              className="h-24 animate-pulse rounded-xl border border-white/5 bg-slate-900"
            />
          ))}
        </div>
      </div>
    </section>
  );
}

export default function GlobalContextPanel({
  symbol,
}: GlobalContextPanelProps) {
  const {
    data,
    status,
    error,
    horizon,
    setHorizon,
    retry,
  } = useGlobalContext(symbol);

  if (status === "disabled" || status === "empty") return null;
  if (status === "loading" && !data) return <LoadingPanel />;

  if (status === "error" && !data) {
    return (
      <section className="rounded-2xl border border-rose-400/20 bg-slate-950/70 p-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-sm font-semibold text-slate-200">
              Overnight global context unavailable
            </p>
            <p className="mt-1 text-xs text-slate-500">
              Core stock and sentiment data are unaffected.
            </p>
          </div>
          <button
            type="button"
            onClick={retry}
            className="inline-flex items-center gap-2 rounded-lg border border-white/10 px-3 py-2 text-xs font-semibold text-slate-300 transition-colors hover:border-cyan-400/40 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-400"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            Retry
          </button>
        </div>
      </section>
    );
  }

  if (!data) return null;

  const relationships = data.factors
    .filter((factor) => factor.relationship.correlation !== null)
    .sort(
      (left, right) =>
        Math.abs(right.relationship.correlation ?? 0)
        - Math.abs(left.relationship.correlation ?? 0),
    )
    .slice(0, 4);

  return (
    <section
      aria-labelledby="global-context-title"
      className="overflow-hidden rounded-2xl border border-cyan-400/15 bg-[linear-gradient(140deg,rgba(8,15,30,0.96),rgba(15,23,42,0.82))] shadow-2xl"
    >
      <div
        className="grid h-1 grid-cols-4"
        aria-label="Asia close, currencies, materials, then the next U.S. close"
      >
        <span className="bg-cyan-400/80" />
        <span className="bg-sky-500/55" />
        <span className="bg-amber-300/75" />
        <span className="bg-indigo-500/55" />
      </div>

      <div className="p-5 md:p-6">
        <header className="flex flex-col gap-4 border-b border-white/5 pb-5 md:flex-row md:items-start md:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <Globe2 className="h-4 w-4 text-cyan-300" aria-hidden="true" />
              <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.24em] text-cyan-300/80">
                Asia close → U.S. next close
              </p>
            </div>
            <h2
              id="global-context-title"
              className="mt-2 text-xl font-bold tracking-tight text-white"
            >
              Overnight Global Pulse
            </h2>
            <p className="mt-1 max-w-2xl text-xs leading-5 text-slate-400">
              Curated context for {symbol}. {data.currency_orientation}
            </p>
          </div>

          <div className="flex items-center gap-3">
            <div
              className="inline-flex rounded-lg border border-white/10 bg-black/20 p-1"
              aria-label="Relationship horizon"
            >
              {([30, 90] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={horizon === value}
                  onClick={() => setHorizon(value)}
                  className={cn(
                    "rounded-md px-2.5 py-1 text-[11px] font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-400",
                    horizon === value
                      ? "bg-cyan-300 text-slate-950"
                      : "text-slate-400 hover:text-white",
                  )}
                >
                  {value} sessions
                </button>
              ))}
            </div>
            <span
              className={cn(
                "inline-flex items-center gap-1.5 text-[11px]",
                data.freshness.status === "stale"
                  ? "text-amber-300"
                  : "text-slate-500",
              )}
            >
              <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
              {relativeTime(data.freshness.latest_factor_at)}
            </span>
          </div>
        </header>

        {status === "error" ? (
          <div
            role="status"
            className="mt-4 flex items-center justify-between rounded-lg border border-amber-400/20 bg-amber-400/5 px-3 py-2 text-xs text-amber-200"
          >
            <span>Showing the last response. {error}</span>
            <button type="button" onClick={retry} className="font-semibold">
              Refresh
            </button>
          </div>
        ) : null}

        <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-7">
          {data.factors.map((factor) => (
            <article
              key={factor.instrument_key}
              title={factor.exposure_reason}
              className="min-w-0 rounded-xl border border-white/5 bg-black/20 p-3"
            >
              <div className="flex items-start justify-between gap-2">
                <p className="truncate text-xs font-semibold text-slate-300">
                  {factor.display_name}
                </p>
                <span className="font-mono text-[9px] uppercase text-slate-600">
                  {factor.asset_class}
                </span>
              </div>
              <p
                className={cn(
                  "mt-3 font-mono text-lg font-semibold tabular-nums",
                  movementColor(factor.current_move_pct),
                )}
              >
                {signedPercent(factor.current_move_pct)}
              </p>
              <p className="mt-1 truncate text-[10px] text-slate-600">
                {factor.provider ?? "Awaiting source"} ·{" "}
                {factor.current_as_of
                  ? relativeTime(factor.current_as_of)
                  : "awaiting bars"}
              </p>
            </article>
          ))}
        </div>

        <div className="mt-6 grid gap-6 xl:grid-cols-12">
          <div className="xl:col-span-5">
            <div className="mb-3 flex items-center gap-2">
              <Waves className="h-4 w-4 text-amber-300" aria-hidden="true" />
              <h3 className="text-sm font-semibold text-slate-200">
                Strongest measured relationships
              </h3>
            </div>
            {relationships.length ? (
              <div className="space-y-2">
                {relationships.map((factor) => (
                  <div
                    key={factor.instrument_key}
                    className={cn(
                      "grid grid-cols-[1fr_auto_auto] items-center gap-3 rounded-lg border bg-black/15 px-3 py-2.5",
                      relationshipColor(factor),
                    )}
                  >
                    <span className="truncate text-xs font-semibold">
                      {factor.display_name}
                    </span>
                    <span className="font-mono text-xs tabular-nums">
                      r={factor.relationship.correlation?.toFixed(2)} · β=
                      {factor.relationship.beta?.toFixed(2) ?? "—"}
                    </span>
                    <span className="text-[10px] text-slate-500">
                      lag {factor.relationship.selected_lag} · n=
                      {factor.relationship.sample_count}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="rounded-lg border border-dashed border-white/10 px-4 py-6 text-center text-xs text-slate-500">
                At least 20 aligned observations are needed.
              </div>
            )}
            <p className="mt-3 text-[10px] leading-4 text-slate-600">
              {data.disclaimer} Pearson r and regression beta use close-to-close
              returns at lags 0–2.
            </p>
          </div>

          <div className="xl:col-span-7">
            <h3 className="mb-3 text-sm font-semibold text-slate-200">
              Relevant event signals
            </h3>
            {data.events.length ? (
              <ol className="relative space-y-3 border-l border-cyan-400/15 pl-4">
                {data.events.slice(0, 6).map((event) => (
                  <li key={event.id} className="relative">
                    <span className="absolute -left-[19px] top-1.5 h-2 w-2 rounded-full border border-cyan-300 bg-slate-950" />
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      {event.canonical_url ? (
                        <a
                          href={event.canonical_url}
                          target="_blank"
                          rel="noreferrer"
                          className="group max-w-[75%] text-xs font-medium leading-5 text-slate-300 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-400"
                        >
                          {event.title}
                          <ArrowUpRight
                            className="ml-1 inline h-3 w-3 text-slate-600 group-hover:text-cyan-300"
                            aria-hidden="true"
                          />
                        </a>
                      ) : (
                        <p className="max-w-[75%] text-xs font-medium leading-5 text-slate-300">
                          {event.title}
                        </p>
                      )}
                      <span
                        className={cn(
                          "font-mono text-[10px] tabular-nums",
                          movementColor(event.next_close_move_pct),
                        )}
                      >
                        {event.reaction_label}:{" "}
                        {signedPercent(event.next_close_move_pct)}
                      </span>
                    </div>
                    <p className="mt-1 text-[10px] text-slate-600">
                      {event.rule_names.join(" · ")} ·{" "}
                      {relativeTime(event.occurred_at)} · {event.provider}
                    </p>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="rounded-lg border border-dashed border-white/10 px-4 py-6 text-center text-xs text-slate-500">
                No recent signals matched the configured rules.
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
