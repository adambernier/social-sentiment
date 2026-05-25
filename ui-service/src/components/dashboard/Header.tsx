import React from "react";
import { BarChart2 } from "lucide-react";
import { cn } from "./utils";
import { DashboardDataProps } from "../types";

export default function Header({ state, setters, computed }: DashboardDataProps) {
  const { symbol, hours, platform, selectedTopic, isConnected, showSR, leaderboard } = state;
  const { setSymbol, setHours, setPlatform, setSelectedTopic, setShowSR } = setters;
  const { marketSession } = computed;

  return (
    <header className="flex flex-col md:flex-row md:items-center justify-between pb-4">
      <div className="flex items-center gap-3">
        <div className="flex items-end gap-1 p-2 bg-white/5 rounded-lg border border-white/10 backdrop-blur-sm">
          <div className="w-1.5 h-4 bg-emerald-500 rounded-full"></div>
          <div className="w-1.5 h-3 bg-sky-500 rounded-full"></div>
          <div className="w-1.5 h-5 bg-rose-500 rounded-full"></div>
        </div>
        <h1 className="text-2xl font-bold tracking-tight text-transparent bg-clip-text bg-gradient-to-r from-white to-slate-400">
          Social Sentiment vs Market Price
        </h1>
      </div>
      <div className="flex items-center gap-4 mt-4 md:mt-0">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">Status:</span>
          <span className={cn("text-xs font-bold px-2 py-1 rounded-md", marketSession === 'regular' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-slate-800 text-slate-300')}>
            {marketSession === 'regular' ? 'MARKET OPEN' : 'MARKET CLOSED'}
          </span>
        </div>
        <div className={cn("flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold border", isConnected ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20" : "bg-rose-500/10 text-rose-400 border-rose-500/20")}>
          <div className={cn("w-2 h-2 rounded-full", isConnected ? "bg-emerald-500 animate-pulse" : "bg-rose-500")}></div>
          {isConnected ? "Live Stream" : "Disconnected"}
        </div>
        <select 
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="bg-slate-900/80 border border-white/10 text-white text-sm rounded-lg focus:ring-indigo-500 focus:border-indigo-500 block px-4 py-2 outline-none backdrop-blur-md cursor-pointer transition-colors hover:bg-slate-800"
        >
          {leaderboard && leaderboard.length > 0 ? (
            leaderboard
              .slice()
              .sort((a: any, b: any) => a.symbol.localeCompare(b.symbol))
              .map((item: any) => (
                <option key={item.symbol} value={item.symbol}>{item.symbol}</option>
              ))
          ) : (
            <option value={symbol}>{symbol}</option>
          )}
        </select>
        <select 
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
          className="bg-slate-900/80 border border-white/10 text-white text-sm rounded-lg focus:ring-indigo-500 focus:border-indigo-500 block px-4 py-2 outline-none backdrop-blur-md cursor-pointer transition-colors hover:bg-slate-800"
        >
          <option value={1}>1H</option>
          <option value={24}>24H</option>
          <option value={168}>7D</option>
          <option value={720}>30D</option>
        </select>
        <select 
          value={platform}
          onChange={(e) => setPlatform(e.target.value)}
          className="bg-slate-900/80 border border-white/10 text-white text-sm rounded-lg focus:ring-indigo-500 focus:border-indigo-500 block px-4 py-2 outline-none backdrop-blur-md cursor-pointer transition-colors hover:bg-slate-800"
        >
          <option value="all">All Sources</option>
          <option value="bluesky">Bluesky</option>
          <option value="reddit">Reddit</option>
          <option value="stocktwits">Stocktwits</option>
          <option value="yahoo">Yahoo Finance News</option>
          <option value="finnhub">Finnhub News</option>
        </select>
        <select 
          value={selectedTopic}
          onChange={(e) => setSelectedTopic(e.target.value)}
          className="bg-slate-900/80 border border-white/10 text-white text-sm rounded-lg focus:ring-indigo-500 focus:border-indigo-500 block px-4 py-2 outline-none backdrop-blur-md cursor-pointer transition-colors hover:bg-slate-800"
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
        <button
          onClick={() => setShowSR(!showSR)}
          className={cn(
            "flex items-center gap-2 px-4 py-2 text-sm font-semibold rounded-lg border transition-all backdrop-blur-md cursor-pointer",
            showSR 
              ? "bg-indigo-500/20 text-indigo-300 border-indigo-500/30 shadow-[0_0_15px_rgba(99,102,241,0.2)]" 
              : "bg-slate-900/80 text-slate-400 border-white/10 hover:bg-slate-800 hover:text-slate-300"
          )}
        >
          <BarChart2 className="w-4 h-4" />
          S&R Lines
        </button>
      </div>
    </header>
  );
}
