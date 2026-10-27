import React from "react";
import { TrendingUp, TrendingDown, AlertCircle, Activity, Zap } from "lucide-react";
import { cn, formatLargeNumber } from "./utils";
import type { DashboardDataProps } from "../hooks/useDashboardData";

const InfoTooltip = ({ text }: { text: string }) => (
  <div className="group relative ml-1.5 flex items-center justify-center">
    <button
      type="button"
      tabIndex={0}
      aria-label={text}
      className="w-3.5 h-3.5 rounded-full bg-white/10 text-[9px] flex items-center justify-center text-slate-400 cursor-help hover:bg-white/20 hover:text-white focus:outline-none focus:ring-1 focus:ring-indigo-400 transition-colors"
    >
      ?
    </button>
    <div className="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-44 sm:w-48 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 bg-slate-900 text-slate-200 text-xs rounded-lg p-2.5 border border-slate-700 shadow-xl z-50 text-center font-normal tracking-normal leading-relaxed">
      {text}
      <div className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-slate-900"></div>
    </div>
  </div>
);

export default function TelemetryGrid({ state, computed }: DashboardDataProps) {
  const { symbol, latestQuote, primaryDelta, futureSymbol, futureQuote, futureDelta, correlationData } = state;
  const {
    totalMentions,
    bullishCount,
    bearishCount,
    neutralCount,
    bullishPct,
    bearishPct,
    neutralPct,
    divergenceStatus,
    vixRegime,
  } = computed;

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-4 xl:grid-cols-2 2xl:grid-cols-4 gap-4 w-full">
      {/* ROW 1: Basics (Price, Volume, Bullish, Bearish) */}
      <div className="bg-slate-900/40 backdrop-blur-md border border-blue-500/20 rounded-2xl p-4 hover:bg-slate-900/60 transition-colors col-span-1 flex flex-col justify-between">
        <div className="flex flex-col text-slate-400 mb-1">
          <span className="text-xs font-medium text-blue-400 flex items-center">{symbol} Price <InfoTooltip text="The current market price of the asset. Updates in real-time." /></span>
          <span className="text-[9px] text-slate-500 font-normal lowercase leading-none mt-0.5">(since last close)</span>
        </div>
        <div className="flex items-end justify-between gap-1 tabular-nums">
          <div className="text-2xl font-bold text-white">${latestQuote?.price?.toFixed(2) || '---'}</div>
          {primaryDelta && (
            <div className={cn("text-xs font-semibold px-2 py-0.5 rounded-md mb-0.5", primaryDelta.pct_change >= 0 ? "bg-emerald-500/20 text-emerald-400" : "bg-rose-500/20 text-rose-400")}>
              {primaryDelta.pct_change >= 0 ? '↑' : '↓'} {Math.abs(primaryDelta.pct_change).toFixed(2)}%
            </div>
          )}
        </div>
      </div>
      
      <div className="bg-slate-900/40 backdrop-blur-md border border-blue-500/20 rounded-2xl p-4 hover:bg-slate-900/60 transition-colors col-span-1 flex flex-col justify-between">
        <div className="flex items-center gap-2 text-blue-400 mb-2">
          <Activity className="w-4 h-4 shrink-0" />
          <span className="text-xs font-medium flex items-center">Live Volume <InfoTooltip text="Cumulative trading volume for the current trading session." /></span>
        </div>
        <div className="text-2xl font-bold text-white tabular-nums">{formatLargeNumber(latestQuote?.volume)}</div>
      </div>

      <div className="bg-slate-900/40 backdrop-blur-md border border-emerald-500/20 rounded-2xl p-4 hover:bg-slate-900/60 transition-colors relative col-span-1 flex flex-col justify-between overflow-hidden">
        <div className="absolute top-0 right-0 p-3 opacity-10 pointer-events-none"><TrendingUp className="w-11 h-11 text-emerald-500" /></div>
        <div className="flex items-center gap-2 text-emerald-400 mb-1">
          <span className="text-xs font-medium flex items-center">Bullish Sentiment <InfoTooltip text="Percentage of social chatter expressing positive or bullish sentiment over the selected time window." /></span>
        </div>
        <div className="flex items-baseline justify-between gap-2 tabular-nums">
          <div className="text-2xl font-bold text-white">{bullishPct}%</div>
          <span className="text-[10px] text-slate-400">{bullishCount} pos · {neutralPct}% neu</span>
        </div>
        <div className="mt-2 h-1 w-full bg-slate-800 rounded-full overflow-hidden flex" title={`Bullish: ${bullishPct}% (${bullishCount}) · Neutral: ${neutralPct}% (${neutralCount}) · Bearish: ${bearishPct}% (${bearishCount})`}>
          <div className="bg-emerald-500 h-full transition-all duration-300" style={{ width: `${bullishPct}%` }} />
          <div className="bg-slate-500 h-full transition-all duration-300" style={{ width: `${neutralPct}%` }} />
          <div className="bg-rose-500/40 h-full transition-all duration-300" style={{ width: `${bearishPct}%` }} />
        </div>
      </div>
      
      <div className="bg-slate-900/40 backdrop-blur-md border border-rose-500/20 rounded-2xl p-4 hover:bg-slate-900/60 transition-colors relative col-span-1 flex flex-col justify-between overflow-hidden">
        <div className="absolute top-0 right-0 p-3 opacity-10 pointer-events-none"><TrendingDown className="w-11 h-11 text-rose-500" /></div>
        <div className="flex items-center gap-2 text-rose-400 mb-1">
          <span className="text-xs font-medium flex items-center">Bearish Sentiment <InfoTooltip text="Percentage of social chatter expressing negative or bearish sentiment over the selected time window." /></span>
        </div>
        <div className="flex items-baseline justify-between gap-2 tabular-nums">
          <div className="text-2xl font-bold text-white">{bearishPct}%</div>
          <span className="text-[10px] text-slate-400">{bearishCount} neg · {totalMentions} total</span>
        </div>
        <div className="mt-2 h-1 w-full bg-slate-800 rounded-full overflow-hidden flex" title={`Bearish: ${bearishPct}% (${bearishCount}) · Neutral: ${neutralPct}% (${neutralCount}) · Bullish: ${bullishPct}% (${bullishCount})`}>
          <div className="bg-rose-500 h-full transition-all duration-300" style={{ width: `${bearishPct}%` }} />
          <div className="bg-slate-500 h-full transition-all duration-300" style={{ width: `${neutralPct}%` }} />
          <div className="bg-emerald-500/40 h-full transition-all duration-300" style={{ width: `${bullishPct}%` }} />
        </div>
      </div>

      {/* ROW 2: Alpha (Divergence, Correlation, Futures, VIX) */}
      <div className="bg-slate-900/40 backdrop-blur-md border border-amber-500/20 rounded-2xl p-4 hover:bg-slate-900/60 transition-colors col-span-1 flex flex-col justify-between">
        <div className="flex items-center gap-2 text-amber-400 mb-2">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span className="text-xs font-medium flex items-center">Retail Divergence <InfoTooltip text="Detects when retail sentiment moves strongly in the opposite direction of news/institutional sentiment." /></span>
        </div>
        <div className={cn("text-xl font-bold", divergenceStatus.color)}>{divergenceStatus.label}</div>
      </div>

      <div className="bg-slate-900/40 backdrop-blur-md border border-indigo-500/20 rounded-2xl p-4 hover:bg-slate-900/60 transition-colors col-span-1 flex flex-col justify-between relative overflow-hidden">
        <div className="absolute top-0 right-0 p-3 opacity-10 pointer-events-none"><Activity className="w-11 h-11 text-indigo-500" /></div>
        <div className="flex items-center gap-2 text-indigo-400 mb-1">
          <Zap className="w-3.5 h-3.5 shrink-0" />
          <span className="text-xs font-medium flex items-center">Price Correlation <InfoTooltip text="Measures how closely the asset's price movements align with changes in social sentiment." /></span>
        </div>
        <div className="flex items-baseline justify-between mt-1 tabular-nums">
          <div className={cn("text-2xl font-bold tracking-tight", 
            correlationData.maxR === 0 ? "text-slate-400" :
            correlationData.maxR > 0 ? "text-emerald-400" : "text-rose-400"
          )}>
            {correlationData.maxR === 0 ? '0.00' : `${correlationData.maxR > 0 ? '+' : ''}${correlationData.maxR.toFixed(2)}`}
          </div>
          <span className={cn("text-[9px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider",
            correlationData.correlationStrength === 'strong' ? 'bg-indigo-500/20 text-indigo-300 border border-indigo-500/20' :
            correlationData.correlationStrength === 'moderate' ? 'bg-blue-500/20 text-blue-300 border border-blue-500/20' :
            'bg-slate-800 text-slate-400'
          )}>
            {correlationData.correlationStrength}
          </span>
        </div>
      </div>

      <div className="bg-slate-900/40 backdrop-blur-md border border-blue-500/20 rounded-2xl p-4 hover:bg-slate-900/60 transition-colors col-span-1 flex flex-col justify-between">
        <div className="flex flex-col text-slate-400 mb-1">
          <span className="text-xs font-medium text-blue-400 flex items-center">{futureSymbol || "NQ Futures"} <InfoTooltip text="The current price of the associated primary futures contract." /></span>
          <span className="text-[9px] text-slate-500 font-normal lowercase leading-none mt-0.5">(since last close)</span>
        </div>
        <div className="flex items-end justify-between gap-1 tabular-nums">
          <div className="text-2xl font-bold text-white">
            {futureQuote?.price !== undefined && futureQuote?.price !== null 
              ? Math.round(futureQuote.price).toLocaleString() 
              : '---'}
          </div>
          {futureDelta && (
            <div className={cn("text-xs font-semibold px-2 py-0.5 rounded-md mb-0.5", futureDelta.pct_change >= 0 ? "bg-emerald-500/20 text-emerald-400" : "bg-rose-500/20 text-rose-400")}>
              {futureDelta.pct_change >= 0 ? '↑' : '↓'} {Math.abs(futureDelta.pct_change).toFixed(2)}%
            </div>
          )}
        </div>
      </div>

      <div className="bg-slate-900/40 backdrop-blur-md border border-blue-500/20 rounded-2xl p-4 hover:bg-slate-900/60 transition-colors col-span-1 flex flex-col justify-between">
        <div className="flex items-center gap-2 text-blue-400 mb-2">
          <Zap className="w-4 h-4 shrink-0" />
          <span className="text-xs font-medium flex items-center">VIX Regime <InfoTooltip text="Current state of market volatility (VIX). High VIX indicates fear/stress, low VIX indicates complacency." /></span>
        </div>
        <div className={cn("text-2xl font-bold tabular-nums", vixRegime.color)}>{vixRegime.label}</div>
      </div>
    </div>
  );
}
