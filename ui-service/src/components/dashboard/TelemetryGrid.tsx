import React from "react";
import { MessageSquare, TrendingUp, TrendingDown, AlertCircle, Activity, Zap } from "lucide-react";
import { cn, formatLargeNumber } from "./utils";
import { DashboardDataProps } from "../types";

export default function TelemetryGrid({ state, computed }: DashboardDataProps) {
  const { symbol, latestQuote, primaryDelta, futureSymbol, futureQuote, futureDelta, correlationData } = state;
  const { totalMentions, bullishPct, bearishPct, divergenceStatus, vixRegime } = computed;

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4">
      <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors col-span-1">
        <div className="flex items-center gap-2 text-slate-400 mb-2">
          <MessageSquare className="w-4 h-4" />
          <span className="text-xs font-medium">Mentions</span>
        </div>
        <div className="text-2xl font-bold">{totalMentions.toLocaleString()}</div>
      </div>
      <div className="bg-white/5 backdrop-blur-md border border-emerald-500/20 rounded-2xl p-4 hover:bg-white/10 transition-colors relative overflow-hidden col-span-1">
        <div className="absolute top-0 right-0 p-4 opacity-10"><TrendingUp className="w-12 h-12 text-emerald-500" /></div>
        <div className="flex items-center gap-2 text-emerald-400 mb-2">
          <span className="text-xs font-medium">Bullish</span>
        </div>
        <div className="text-2xl font-bold text-white">{bullishPct}%</div>
      </div>
      <div className="bg-white/5 backdrop-blur-md border border-rose-500/20 rounded-2xl p-4 hover:bg-white/10 transition-colors relative overflow-hidden col-span-1">
        <div className="absolute top-0 right-0 p-4 opacity-10"><TrendingDown className="w-12 h-12 text-rose-500" /></div>
        <div className="flex items-center gap-2 text-rose-400 mb-2">
          <span className="text-xs font-medium">Bearish</span>
        </div>
        <div className="text-2xl font-bold text-white">{bearishPct}%</div>
      </div>
      <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors col-span-1">
        <div className="flex items-center gap-2 text-slate-400 mb-2">
          <AlertCircle className="w-4 h-4" />
          <span className="text-xs font-medium">Divergence</span>
        </div>
        <div className={cn("text-2xl font-bold", divergenceStatus.color)}>{divergenceStatus.label}</div>
      </div>
      
      <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors col-span-1">
        <div className="flex flex-col text-slate-400 mb-1">
          <span className="text-xs font-medium">{symbol} Price</span>
          <span className="text-[9px] text-slate-500 font-normal lowercase leading-none mt-0.5">(since last close)</span>
        </div>
        <div className="flex items-end justify-between">
          <div className="text-2xl font-bold">${latestQuote?.price?.toFixed(2) || '---'}</div>
          {primaryDelta && (
            <div className={cn("text-xs font-semibold px-2 py-0.5 rounded-md mb-1", primaryDelta.pct_change >= 0 ? "bg-emerald-500/20 text-emerald-400" : "bg-rose-500/20 text-rose-400")}>
              {primaryDelta.pct_change >= 0 ? '↑' : '↓'} {Math.abs(primaryDelta.pct_change).toFixed(2)}%
            </div>
          )}
        </div>
      </div>
      <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors col-span-1">
        <div className="flex items-center gap-2 text-slate-400 mb-2">
          <Activity className="w-4 h-4" />
          <span className="text-xs font-medium">Live Volume</span>
        </div>
        <div className="text-2xl font-bold">{formatLargeNumber(latestQuote?.volume)}</div>
      </div>
      <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors col-span-1">
        <div className="flex flex-col text-slate-400 mb-1">
          <span className="text-xs font-medium">{futureSymbol || "NQ Futures"}</span>
          <span className="text-[9px] text-slate-500 font-normal lowercase leading-none mt-0.5">(since last close)</span>
        </div>
        <div className="flex items-end justify-between">
          <div className="text-2xl font-bold">
            {futureQuote?.price !== undefined && futureQuote?.price !== null 
              ? Math.round(futureQuote.price).toLocaleString() 
              : '---'}
          </div>
          {futureDelta && (
            <div className={cn("text-xs font-semibold px-2 py-0.5 rounded-md mb-1", futureDelta.pct_change >= 0 ? "bg-emerald-500/20 text-emerald-400" : "bg-rose-500/20 text-rose-400")}>
              {futureDelta.pct_change >= 0 ? '↑' : '↓'} {Math.abs(futureDelta.pct_change).toFixed(2)}%
            </div>
          )}
        </div>
      </div>
      <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors col-span-1">
        <div className="flex items-center gap-2 text-slate-400 mb-2">
          <Zap className="w-4 h-4" />
          <span className="text-xs font-medium">VIX Regime</span>
        </div>
        <div className={cn("text-2xl font-bold", vixRegime.color)}>{vixRegime.label}</div>
      </div>

      <div className="bg-white/5 backdrop-blur-md border border-indigo-500/20 rounded-2xl p-4 hover:bg-white/10 transition-colors col-span-2 md:col-span-4 lg:col-span-2 flex flex-col justify-between relative overflow-hidden">
        <div className="absolute top-0 right-0 p-4 opacity-10"><Activity className="w-12 h-12 text-indigo-500" /></div>
        <div className="flex items-center gap-2 text-indigo-400 mb-1">
          <Zap className="w-3.5 h-3.5" />
          <span className="text-xs font-medium">Price-Sentiment Correlation</span>
        </div>
        <div className="flex items-baseline justify-between mt-1">
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
        <div className="text-[11px] text-slate-300 font-medium mt-1 truncate">
          {correlationData.correlationText}
        </div>
      </div>
    </div>
  );
}
