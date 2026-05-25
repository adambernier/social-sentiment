import React from "react";
import { ComposedChart, ReferenceArea, XAxis, YAxis, Tooltip, Bar, Area, Line, ReferenceLine, ResponsiveContainer, CartesianGrid, Cell } from "recharts";
import { BarChart2 } from "lucide-react";
import { format } from "date-fns";
import { cn } from "./utils";
import { DashboardDataProps } from "../types";

const CustomizedOpportunityDot = (props: any) => {
  const { cx, cy, payload } = props;
  if (!payload || !payload.buySignal) return null;
  return (
    <g key={`buy-dot-${payload.timestamp}`}>
      <circle 
        cx={cx} cy={cy} r={7} fill="none" stroke="#10b981" strokeWidth={1.5} strokeOpacity={0.8}
        className="animate-ping" style={{ transformOrigin: `${cx}px ${cy}px` }}
      />
      <circle cx={cx} cy={cy} r={4.5} fill="#10b981" stroke="#0f172a" strokeWidth={1.5} />
    </g>
  );
};

export default function CorrelationChart({ state, setters }: DashboardDataProps) {
  const { symbol, hours, chartView, correlationData, showSR } = state;
  const { setChartView } = setters;

  const formatXAxis = (t: string) => {
    if (hours <= 24) return format(new Date(t), "h:mm a");
    if (hours <= 168) return format(new Date(t), "MMM d, h a");
    return format(new Date(t), "MMM d");
  };

  const renderSupportLabel = (props: any) => {
    const { viewBox } = props;
    if (!viewBox) return null;
    const x = viewBox.x + viewBox.width - 130;
    const y = viewBox.y;
    return (
      <g transform={`translate(${x}, ${y - 18})`}>
        <rect width={125} height={16} rx={4} fill="#090d16" fillOpacity={0.9} stroke="#10b981" strokeWidth={1} strokeOpacity={0.3} />
        <text x={6} y={11} fill="#10b981" fontSize={9} fontWeight="bold" letterSpacing="0.05em">
          SUPPORT: ${correlationData.supportPrice.toFixed(2)}
        </text>
      </g>
    );
  };

  const renderResistanceLabel = (props: any) => {
    const { viewBox } = props;
    if (!viewBox) return null;
    const x = viewBox.x + viewBox.width - 145;
    const y = viewBox.y;
    return (
      <g transform={`translate(${x}, ${y + 2})`}>
        <rect width={140} height={16} rx={4} fill="#090d16" fillOpacity={0.9} stroke="#f43f5e" strokeWidth={1} strokeOpacity={0.3} />
        <text x={6} y={11} fill="#f43f5e" fontSize={9} fontWeight="bold" letterSpacing="0.05em">
          RESISTANCE: ${correlationData.resistancePrice.toFixed(2)}
        </text>
      </g>
    );
  };

  return (
    <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-bold flex items-center gap-2 text-white">
            <BarChart2 className="w-5 h-5 text-indigo-400" />
            Sentiment vs. Price Correlation
          </h2>
          <div className="flex bg-slate-950/60 p-0.5 rounded-lg border border-white/5">
            <button
              onClick={() => setChartView("volume")}
              className={cn(
                "px-2.5 py-1 text-xs font-semibold rounded-md transition-all cursor-pointer",
                chartView === "volume" ? "bg-indigo-500 text-white shadow-md" : "text-slate-400 hover:text-slate-200"
              )}
            >
              Volume
            </button>
            <button
              onClick={() => setChartView("sentiment")}
              className={cn(
                "px-2.5 py-1 text-xs font-semibold rounded-md transition-all cursor-pointer",
                chartView === "sentiment" ? "bg-indigo-500 text-white shadow-md" : "text-slate-400 hover:text-slate-200"
              )}
            >
              Sentiment Trend
            </button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-4 text-xs font-medium text-slate-400">
          {chartView === "volume" ? (
            <>
              <div className="flex items-center gap-1"><div className="w-3 h-3 rounded-sm bg-emerald-500"></div> Positive</div>
              <div className="flex items-center gap-1"><div className="w-3 h-3 rounded-sm bg-slate-500"></div> Neutral</div>
              <div className="flex items-center gap-1"><div className="w-3 h-3 rounded-sm bg-rose-500"></div> Negative</div>
            </>
          ) : (
            <>
              <div className="flex items-center gap-1"><div className="w-3 h-0.5 bg-[#818cf8]"></div> Sentiment Index</div>
              <div className="flex items-center gap-1"><div className="w-3 h-0.5 bg-[#06b6d4]"></div> Sentiment SMA</div>
            </>
          )}
          <div className="hidden sm:block h-4 w-px bg-slate-700 mx-1"></div>
          <div className="flex items-center gap-1"><div className="w-3 h-0.5 bg-[#fbbf24]"></div> {symbol} Price</div>
          <div className="flex items-center gap-1"><div className="w-3 border-t-2 border-dashed border-[#8b5cf6]"></div> NQ Futures</div>
        </div>
      </div>
      <div className="h-[400px]">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={correlationData.data} margin={{ top: 10, right: 10, bottom: 0, left: -20 }}>
            <defs>
              <linearGradient id="sentimentGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#818cf8" stopOpacity={0.25}/>
                <stop offset="95%" stopColor="#818cf8" stopOpacity={0.0}/>
              </linearGradient>
            </defs>

            <CartesianGrid stroke="#1e293b" vertical={false} strokeDasharray="3 3" />
            
            {correlationData.closedRegions.map((region: any, idx: number) => (
              <ReferenceArea key={idx} x1={region.start} x2={region.end} fill="#0f172a" fillOpacity={0.6} yAxisId="left" />
            ))}
            
            <XAxis 
              dataKey="timestamp" tickFormatter={formatXAxis} stroke="#64748b" tickLine={false} axisLine={false} dy={10} minTickGap={40} tick={{ fontSize: 12 }}
            />

            <YAxis 
              yAxisId="left" stroke="#64748b" tickLine={false} axisLine={false} tick={{ fontSize: 12 }}
              domain={chartView === 'sentiment' ? [-1.1, 1.1] : ['auto', 'auto']}
              tickFormatter={chartView === 'sentiment' ? (v) => `${v > 0 ? '+' : ''}${Math.round(v * 100)}%` : undefined}
            />
            
            <YAxis 
              yAxisId="right" orientation="right" tickFormatter={(v) => `${v > 0 ? '+' : ''}${v}%`}
              stroke="#64748b" tickLine={false} axisLine={false} tick={{ fontSize: 12 }}
            />

            <Tooltip 
              contentStyle={{ backgroundColor: 'rgba(15, 23, 42, 0.9)', borderColor: '#334155', borderRadius: '8px', backdropFilter: 'blur(8px)' }}
              labelFormatter={(l) => format(new Date(l), "MMM d, yyyy h:mm a")}
              itemStyle={{ fontSize: '13px' }}
            />

            {chartView === "volume" ? (
              <>
                <Bar yAxisId="left" dataKey="positive" name="Positive Posts" stackId="a" fill="#10b981" barSize={12} radius={[0, 0, 0, 0]} />
                <Bar yAxisId="left" dataKey="neutral" name="Neutral Posts" stackId="a" fill="#64748b" radius={[0, 0, 0, 0]} />
                <Bar yAxisId="left" dataKey="negative" name="Negative Posts" stackId="a" fill="#f43f5e" radius={[4, 4, 0, 0]} />
              </>
            ) : (
              <>
                <Area yAxisId="left" type="monotone" dataKey="sentimentIndex" name="Weighted Sentiment Index" stroke="#818cf8" strokeWidth={1.5} fill="url(#sentimentGrad)" />
                <Line yAxisId="left" type="monotone" dataKey="sentimentSMA" name="Sentiment SMA" stroke="#06b6d4" strokeWidth={3} dot={false} />
              </>
            )}

            <Line 
              yAxisId="right" type="monotone" dataKey="priceChange" name={`${symbol} Price Change`} 
              stroke="#fbbf24" strokeWidth={3} dot={<CustomizedOpportunityDot />} connectNulls={true}
            />
            <Line 
              yAxisId="right" type="monotone" dataKey="futureChange" name="NQ Futures Change" 
              stroke="#8b5cf6" strokeWidth={2} strokeDasharray="5 5" dot={false} connectNulls={true}
            />
            {showSR && correlationData.supportPrice > 0 && (
              <ReferenceLine yAxisId="right" y={correlationData.supportPct} stroke="#10b981" strokeDasharray="3 3" strokeWidth={1.5} strokeOpacity={0.5} label={renderSupportLabel} />
            )}
            {showSR && correlationData.resistancePrice > 0 && (
              <ReferenceLine yAxisId="right" y={correlationData.resistancePct} stroke="#f43f5e" strokeDasharray="3 3" strokeWidth={1.5} strokeOpacity={0.5} label={renderResistanceLabel} />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {chartView === "sentiment" && (
        <div className="mt-1">
          <div className="flex items-center gap-3 px-1 mb-1">
            <span className="text-xs font-medium text-slate-400">Sentiment Momentum</span>
            <div className="flex items-center gap-2 text-[10px] text-slate-500">
              <div className="flex items-center gap-1"><div className="w-2.5 h-2.5 rounded-sm bg-emerald-500"></div> Rising</div>
              <div className="flex items-center gap-1"><div className="w-2.5 h-2.5 rounded-sm bg-rose-500"></div> Falling</div>
            </div>
          </div>
          <div className="h-[90px]">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={correlationData.data} margin={{ top: 0, right: 10, bottom: 0, left: -20 }}>
                <CartesianGrid stroke="#1e293b" vertical={false} strokeDasharray="3 3" />
                <XAxis dataKey="timestamp" tickFormatter={formatXAxis} stroke="#64748b" tickLine={false} axisLine={false} dy={6} minTickGap={40} tick={{ fontSize: 10 }} />
                <YAxis stroke="#64748b" tickLine={false} axisLine={false} tick={{ fontSize: 10 }} domain={['auto', 'auto']} />
                <Tooltip contentStyle={{ backgroundColor: 'rgba(15, 23, 42, 0.9)', borderColor: '#334155', borderRadius: '8px', backdropFilter: 'blur(8px)' }} labelFormatter={(l) => format(new Date(l), "MMM d, yyyy h:mm a")} itemStyle={{ fontSize: '13px' }} formatter={(v) => [typeof v === 'number' ? v.toFixed(4) : v, "Momentum"]} />
                <ReferenceLine y={0} stroke="#475569" />
                <Bar dataKey="sentimentHist" name="Sentiment Momentum">
                  {correlationData.data.map((entry: any, idx: number) => (
                    <Cell key={idx} fill={(entry.sentimentHist ?? 0) >= 0 ? "#10b981" : "#f43f5e"} />
                  ))}
                </Bar>
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </section>
  );
}
