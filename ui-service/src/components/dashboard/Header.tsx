import React from "react";
import Link from "next/link";
import { BarChart2, RotateCcw, Settings } from "lucide-react";
import { cn } from "./utils";
import type { DashboardDataProps } from "../hooks/useDashboardData";

export default function Header({ state, setters, computed }: DashboardDataProps) {
  const { symbol, hours, platform, selectedTopic, selectedHour, isConnected, showSR, leaderboard } = state;
  const { setSymbol, setHours, setPlatform, setSelectedTopic, setSelectedHour, setShowSR } = setters;
  const { marketSession } = computed;

  const hasActiveFilters = platform !== "all" || selectedTopic !== "all" || selectedHour !== null;

  const resetFilters = () => {
    setPlatform("all");
    setSelectedTopic("all");
    setSelectedHour(null);
  };

  return (
    <header className="flex flex-col lg:flex-row lg:items-center justify-between gap-4 pb-2">
      <div className="flex items-center gap-3">
        <div className="flex items-end gap-1 p-2 bg-white/5 rounded-lg border border-white/10 backdrop-blur-sm shrink-0">
          <div className="w-1.5 h-4 bg-emerald-500 rounded-full"></div>
          <div className="w-1.5 h-3 bg-sky-500 rounded-full"></div>
          <div className="w-1.5 h-5 bg-rose-500 rounded-full"></div>
        </div>
        <div className="flex flex-col">
          <h1 className="text-xl sm:text-2xl font-bold tracking-tight text-transparent bg-clip-text bg-gradient-to-r from-white to-slate-400">
            Social Sentiment vs Market Price
          </h1>
          <span className="text-[10px] text-slate-500 font-medium tracking-wide">
            Built with <span className="text-rose-500">♥</span> by Adam
          </span>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:inline">Status:</span>
          <span className={cn("text-xs font-bold px-2.5 py-1 rounded-md border", marketSession === 'regular' ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/25' : 'bg-slate-800/90 text-slate-300 border-white/5')}>
            {marketSession === 'regular' ? 'MARKET OPEN' : 'MARKET CLOSED'}
          </span>
        </div>
        <div className={cn("flex items-center gap-2 px-3 py-1 rounded-full text-xs font-semibold border", isConnected ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20" : "bg-rose-500/10 text-rose-400 border-rose-500/20")}>
          <div className={cn("w-2 h-2 rounded-full", isConnected ? "bg-emerald-500 animate-pulse" : "bg-rose-500")}></div>
          {isConnected ? "Live Stream" : "Disconnected"}
        </div>
        <select 
          aria-label="Select Ticker Symbol"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="bg-slate-900/90 border border-white/10 text-white text-sm font-semibold rounded-lg focus:ring-indigo-500 focus:border-indigo-500 block px-3 py-1.5 outline-none backdrop-blur-md cursor-pointer transition-colors hover:bg-slate-800"
        >
          {leaderboard && leaderboard.length > 0 ? (
            leaderboard
              .slice()
              .sort((a, b) => a.symbol.localeCompare(b.symbol))
              .map((item) => (
                <option key={item.symbol} value={item.symbol}>{item.symbol}</option>
              ))
          ) : (
            <option value={symbol}>{symbol}</option>
          )}
        </select>
        <select 
          aria-label="Select Time Horizon"
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
          className="bg-slate-900/90 border border-white/10 text-white text-sm rounded-lg focus:ring-indigo-500 focus:border-indigo-500 block px-3 py-1.5 outline-none backdrop-blur-md cursor-pointer transition-colors hover:bg-slate-800"
        >
          <option value={1}>1H</option>
          <option value={24}>24H</option>
          <option value={168}>7D</option>
          <option value={720}>30D</option>
        </select>
        <select 
          aria-label="Filter by Platform Source"
          value={platform}
          onChange={(e) => setPlatform(e.target.value)}
          className={cn(
            "bg-slate-900/90 border text-sm rounded-lg focus:ring-indigo-500 focus:border-indigo-500 block px-3 py-1.5 outline-none backdrop-blur-md cursor-pointer transition-colors hover:bg-slate-800",
            platform !== "all" ? "border-indigo-500/40 text-indigo-300" : "border-white/10 text-white"
          )}
        >
          <option value="all">All Sources</option>
          <option value="bluesky">Bluesky</option>
          <option value="reddit">Reddit</option>
          <option value="stocktwits">Stocktwits</option>
          <option value="finnhub">News Article</option>
        </select>
        <select 
          aria-label="Filter by Topic"
          value={selectedTopic}
          onChange={(e) => setSelectedTopic(e.target.value)}
          className={cn(
            "bg-slate-900/90 border text-sm rounded-lg focus:ring-indigo-500 focus:border-indigo-500 block px-3 py-1.5 outline-none backdrop-blur-md cursor-pointer transition-colors hover:bg-slate-800",
            selectedTopic !== "all" ? "border-indigo-500/40 text-indigo-300" : "border-white/10 text-white"
          )}
        >
          <option value="all">All Topics</option>
          <option value="Earnings & Guidance">Earnings & Guidance</option>
          <option value="Fed & Macro">Fed & Macro</option>
          <option value="Technical Analysis">Technical Analysis</option>
          <option value="AI & Compute">AI & Compute</option>
          <option value="Space & Satellite">Space & Satellite</option>
          <option value="Management & Insider">Management & Insider</option>
          <option value="M&A & Partnerships">M&A & Partnerships</option>
          <option value="Options & Volatility">Options & Volatility</option>
          <option value="General / Outlier">General Chat</option>
        </select>
        {hasActiveFilters && (
          <button
            type="button"
            onClick={resetFilters}
            title="Clear source, topic, and hour filters"
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-semibold rounded-lg bg-amber-500/15 text-amber-300 border border-amber-500/30 hover:bg-amber-500/25 transition-colors cursor-pointer"
          >
            <RotateCcw className="w-3.5 h-3.5" />
            Reset
          </button>
        )}
        <button
          type="button"
          onClick={() => setShowSR(!showSR)}
          className={cn(
            "flex items-center gap-2 px-3.5 py-1.5 text-sm font-semibold rounded-lg border transition-all backdrop-blur-md cursor-pointer",
            showSR 
              ? "bg-indigo-500/20 text-indigo-300 border-indigo-500/30 shadow-[0_0_15px_rgba(99,102,241,0.2)]" 
              : "bg-slate-900/90 text-slate-400 border-white/10 hover:bg-slate-800 hover:text-slate-300"
          )}
        >
          <BarChart2 className="w-4 h-4" />
          S&R Lines
        </button>
        <Link 
          href="/admin" 
          className="flex items-center justify-center p-2 rounded-lg bg-slate-900/90 border border-white/10 text-slate-400 hover:text-white hover:bg-slate-800 transition-colors"
          title="Admin Configuration"
          aria-label="Admin Configuration"
        >
          <Settings className="w-4 h-4" />
        </Link>
      </div>
    </header>
  );
}
