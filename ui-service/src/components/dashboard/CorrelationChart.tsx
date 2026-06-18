import React from "react";
import { ComposedChart, ReferenceArea, XAxis, YAxis, Tooltip, Bar, Area, Line, ReferenceLine, ResponsiveContainer, CartesianGrid, Cell } from "recharts";
import { BarChart2 } from "lucide-react";
import { format } from "date-fns";
import { cn } from "./utils";
import { DashboardDataProps } from "../types";

// A buy signal fires wherever the algo score clears its threshold; the marker's
// GLYPH (not its color) reports how that signal turned out, so it never reads as
// the emerald/rose the sentiment bars use. The accent is a deliberately
// non-sentiment sky/amber/slate, secondary to the shape:
//   up-arrow (sky)    = good    -> price rose after the signal (profitable)
//   down-arrow (amber)= bad     -> price dropped (false signal)
//   dot (slate)       = pending -> not enough forward data yet (pulses)
const CustomizedOpportunityDot = (props: any) => {
  const { cx, cy, payload, value } = props;
  if (!payload || !payload.buySignal || value == null) return null;
  if (cx == null || cy == null) return null;

  const quality = payload.signalQuality === "bad" ? "bad" : payload.signalQuality === "pending" ? "pending" : "good";
  const accent = quality === "good" ? "#38bdf8" : quality === "bad" ? "#fb923c" : "#94a3b8";
  const r = 6.5;

  return (
    <g key={`buy-dot-${payload.timestamp}`}>
      {quality === "pending" && (
        <circle
          cx={cx} cy={cy} r={r} fill="none" stroke={accent} strokeWidth={1.5} strokeOpacity={0.7}
          className="animate-ping" style={{ transformOrigin: `${cx}px ${cy}px` }}
        />
      )}
      {/* Dark badge so the glyph reads against both the yellow price line and the chart background. */}
      <circle cx={cx} cy={cy} r={r} fill="#0f172a" stroke={accent} strokeWidth={1.5} />
      {quality === "good" && (
        <path
          d={`M ${cx} ${cy - 3.3} L ${cx - 3.2} ${cy + 2.4} L ${cx + 3.2} ${cy + 2.4} Z`}
          fill={accent} stroke={accent} strokeWidth={0.5} strokeLinejoin="round"
        />
      )}
      {quality === "bad" && (
        <path
          d={`M ${cx} ${cy + 3.3} L ${cx - 3.2} ${cy - 2.4} L ${cx + 3.2} ${cy - 2.4} Z`}
          fill={accent} stroke={accent} strokeWidth={0.5} strokeLinejoin="round"
        />
      )}
      {quality === "pending" && (
        <circle cx={cx} cy={cy} r={1.7} fill={accent} />
      )}
    </g>
  );
};

export default function CorrelationChart({ state, setters }: DashboardDataProps) {
  const { symbol, hours, chartView, correlationData, showSR, selectedHour, hideExtended } = state;
  const { setChartView, setSelectedHour, setHideExtended } = setters;

  const displayData = React.useMemo(() => {
    if (!correlationData?.data) return [];
    const rows = hideExtended
      ? correlationData.data.filter((d: any) => d.isMarketOpen)
      : correlationData.data;
    // Volume bars render as a 100% stacked chart: normalize the sentiment mix to
    // percentages so each hour's bar is the same height and the positive/neutral/
    // negative split is directly comparable regardless of absolute volume. The raw
    // counts are preserved as *Count for the tooltip.
    return rows.map((d: any) => {
      const pos = d.positive ?? 0;
      const neu = d.neutral ?? 0;
      const neg = d.negative ?? 0;
      const total = pos + neu + neg;
      return {
        ...d,
        positiveCount: pos,
        neutralCount: neu,
        negativeCount: neg,
        positive: total ? (pos / total) * 100 : 0,
        neutral: total ? (neu / total) * 100 : 0,
        negative: total ? (neg / total) * 100 : 0,
      };
    });
  }, [correlationData, hideExtended]);

  // The right axis now carries only the NQ Futures line (cumulative % vs the
  // latest future price). Fit it to that series with a little padding; price and
  // support/resistance moved to the left dollar axis.
  const rightDomain = React.useMemo<[number | string, number | string]>(() => {
    const vals: number[] = [];
    for (const d of displayData ?? []) {
      if (typeof d.futurePct === "number") vals.push(d.futurePct);
    }
    if (vals.length === 0) return ["auto", "auto"];
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const pad = (hi - lo) * 0.1 || 1;
    return [lo - pad, hi + pad];
  }, [displayData]);

  const formatXAxis = (t: string) => {
    if (hours <= 24) return format(new Date(t), "h:mm a");
    if (hours <= 168) return format(new Date(t), "MMM d, h a");
    return format(new Date(t), "MMM d");
  };

  // Dynamic Support and Resistance lines are now rendered as curve series,
  // and their local values are displayed inside the tooltip for each bucket.

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
            <div className="w-px h-4 bg-slate-700/50 mx-1 my-auto"></div>
            <button
              onClick={() => setHideExtended(!hideExtended)}
              className={cn(
                "px-2.5 py-1 text-xs font-semibold rounded-md transition-all cursor-pointer",
                hideExtended ? "bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 shadow-md" : "text-slate-400 hover:text-slate-200"
              )}
            >
              {hideExtended ? "Show All Hours" : "Hide Closed"}
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
          <div className="hidden sm:block h-4 w-px bg-slate-700 mx-1"></div>
          <div className="flex items-center gap-1" title="Buy signal that paid off — price rose within 2h">
            <svg width="13" height="13" viewBox="0 0 13 13" className="shrink-0">
              <circle cx="6.5" cy="6.5" r="5.6" fill="#0f172a" stroke="#38bdf8" strokeWidth="1.2" />
              <path d="M6.5 3.2 L4 9 L9 9 Z" fill="#38bdf8" stroke="#38bdf8" strokeWidth="0.4" strokeLinejoin="round" />
            </svg>
            Signal hit
          </div>
          <div className="flex items-center gap-1" title="Buy signal that failed — price dropped within 2h">
            <svg width="13" height="13" viewBox="0 0 13 13" className="shrink-0">
              <circle cx="6.5" cy="6.5" r="5.6" fill="#0f172a" stroke="#fb923c" strokeWidth="1.2" />
              <path d="M6.5 9.8 L4 4 L9 4 Z" fill="#fb923c" stroke="#fb923c" strokeWidth="0.4" strokeLinejoin="round" />
            </svg>
            Signal missed
          </div>
          {showSR && (
            <>
              <div className="hidden sm:block h-4 w-px bg-slate-700 mx-1"></div>
              <div className="flex items-center gap-1"><div className="w-3 border-t border-dashed border-[#10b981]"></div> Support</div>
              <div className="flex items-center gap-1"><div className="w-3 border-t border-dashed border-[#f43f5e]"></div> Resistance</div>
            </>
          )}
        </div>
      </div>
      <div className="h-[400px]">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart 
            data={displayData} 
            margin={{ top: 10, right: 10, bottom: 0, left: -20 }}
            onClick={(e: any) => {
              // Recharts passes the chart state. We can use activeLabel or activePayload
              const ts = e?.activePayload?.[0]?.payload?.timestamp || e?.activeLabel;
              if (ts) {
                setSelectedHour(ts);
              }
            }}
            style={{ cursor: 'pointer' }}
          >
            <defs>
              <linearGradient id="sentimentGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#818cf8" stopOpacity={0.25}/>
                <stop offset="95%" stopColor="#818cf8" stopOpacity={0.0}/>
              </linearGradient>
            </defs>

            <CartesianGrid stroke="#1e293b" vertical={false} strokeDasharray="3 3" />
            
            {!hideExtended && correlationData.closedRegions.map((region: any, idx: number) => (
              <ReferenceArea key={idx} x1={region.start} x2={region.end} fill="#0f172a" fillOpacity={0.6} yAxisId="left" />
            ))}

            {selectedHour && (
              <ReferenceArea x1={selectedHour} x2={selectedHour} fill="#818cf8" fillOpacity={0.2} yAxisId="left" />
            )}
            
            <XAxis 
              dataKey="timestamp" tickFormatter={formatXAxis} stroke="#64748b" tickLine={false} axisLine={false} dy={10} minTickGap={40} tick={{ fontSize: 12 }}
            />

            {/* Volume view: left axis is the price in whole dollars. */}
            <YAxis
              yAxisId="dollar" orientation="left" hide={chartView === 'sentiment'}
              stroke="#64748b" tickLine={false} axisLine={false} tick={{ fontSize: 12 }}
              domain={['auto', 'auto']} tickFormatter={(v) => `$${Math.round(v)}`}
            />
            {/* Sentiment view: left axis is the sentiment index (±100%). */}
            <YAxis
              yAxisId="left" orientation="left" hide={chartView !== 'sentiment'}
              stroke="#64748b" tickLine={false} axisLine={false} tick={{ fontSize: 12 }}
              domain={[-1.1, 1.1]}
              tickFormatter={(v) => `${v > 0 ? '+' : ''}${Math.round(v * 100)}%`}
            />
            {/* Hidden scale for the de-emphasized social-volume bars. Bars are
                normalized to a 100% stack, so the domain is fixed at 0–100. */}
            <YAxis yAxisId="volume" orientation="left" hide domain={[0, 100]} />
            
            <YAxis
              yAxisId="right" orientation="right" tickFormatter={(v) => `${v > 0 ? '+' : ''}${Number(v).toFixed(1)}%`}
              stroke="#64748b" tickLine={false} axisLine={false} tick={{ fontSize: 12 }}
              domain={rightDomain}
            />

            <Tooltip
              content={({ active, payload, label }: any) => {
                if (active && payload && payload.length) {
                  const data = payload[0].payload;
                  return (
                    <div className="bg-slate-900/90 border border-slate-700 p-3 rounded-lg backdrop-blur-md shadow-xl text-sm min-w-[200px]">
                      <p className="text-slate-300 font-medium mb-3 pb-2 border-b border-white/5">{format(new Date(label), "MMM d, yyyy h:mm a")}</p>
                      {payload.map((p: any) => {
                        const dk = p.dataKey;
                        // Price + Support/Resistance live on the left axis in whole dollars.
                        const isDollar = dk === "rawPrice" || dk === "supportPrice" || dk === "resistancePrice";
                        const isFuture = dk === "futurePct";
                        const isSentIndex = typeof p.name === "string" && p.name.includes("Index");
                        const isVolume = dk === "positive" || dk === "neutral" || dk === "negative";
                        let display: string;
                        if (typeof p.value !== "number") {
                          display = `${p.value}`;
                        } else if (isDollar) {
                          display = `$${Math.round(p.value)}`;
                        } else if (isFuture) {
                          display = `${p.value > 0 ? "+" : ""}${p.value.toFixed(1)}%`;
                        } else if (isSentIndex) {
                          display = `${p.value.toFixed(2)}%`;
                        } else if (isVolume) {
                          // Bars are proportions; pair the % with the raw count stashed in displayData.
                          const count = data[`${dk}Count`] ?? 0;
                          display = `${Math.round(p.value)}% (${count} ${count === 1 ? "post" : "posts"})`;
                        } else {
                          display = Number.isInteger(p.value) ? `${p.value}` : p.value.toFixed(2);
                        }
                        return (
                          <div key={p.name} className="flex justify-between items-center gap-4 mb-1.5">
                            <div className="flex items-center gap-2">
                              <div className="w-2 h-2 rounded-full shadow-sm" style={{ backgroundColor: p.color }}></div>
                              <span className="text-slate-400 text-xs">{p.name}</span>
                            </div>
                            <span className="text-white font-semibold text-xs">{display}</span>
                          </div>
                        );
                      })}
                      {data.buySignal && (
                        <div className="mt-3 pt-3 border-t border-white/5">
                          <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1">Algorithmic Signal</div>
                          <div className={cn(
                            "flex items-center gap-2 text-xs font-semibold px-2.5 py-1.5 rounded-md",
                            data.signalQuality === 'good' ? 'bg-sky-500/10 text-sky-400 border border-sky-500/20' :
                            data.signalQuality === 'bad' ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20' :
                            'bg-slate-500/10 text-slate-300 border border-slate-500/20'
                          )}>
                            <span className={cn("w-3 text-center text-sm font-bold leading-none", data.signalQuality === 'pending' && 'animate-pulse')}>
                              {data.signalQuality === 'good' ? '▲' : data.signalQuality === 'bad' ? '▼' : '⋯'}
                            </span>
                            {data.signalQuality === 'good' ? 'Profitable (Price Rose)' :
                             data.signalQuality === 'bad' ? 'False Signal (Price Dropped)' :
                             'Pending (Awaiting Data)'}
                          </div>
                        </div>
                      )}
                    </div>
                  );
                }
                return null;
              }}
            />

            {chartView === "volume" ? (
              <>
                <Bar yAxisId="volume" dataKey="positive" name="Positive Posts" stackId="a" fill="#10b981" fillOpacity={0.35} barSize={12} radius={[0, 0, 0, 0]} />
                <Bar yAxisId="volume" dataKey="neutral" name="Neutral Posts" stackId="a" fill="#64748b" fillOpacity={0.35} radius={[0, 0, 0, 0]} />
                <Bar yAxisId="volume" dataKey="negative" name="Negative Posts" stackId="a" fill="#f43f5e" fillOpacity={0.35} radius={[4, 4, 0, 0]} />
              </>
            ) : (
              <>
                <Area yAxisId="left" type="monotone" dataKey="sentimentIndex" name="Weighted Sentiment Index" stroke="#818cf8" strokeWidth={1.5} fill="url(#sentimentGrad)" />
                <Line yAxisId="left" type="monotone" dataKey="sentimentSMA" name="Sentiment SMA" stroke="#06b6d4" strokeWidth={3} dot={false} />
              </>
            )}

            <Line
              yAxisId="dollar" type="monotone" dataKey="rawPrice" name={`${symbol} Price`}
              stroke="#fbbf24" strokeWidth={3} dot={<CustomizedOpportunityDot fullData={correlationData.data} />} connectNulls={true}
            />
            <Line
              yAxisId="right" type="monotone" dataKey="futurePct" name="NQ Futures"
              stroke="#8b5cf6" strokeWidth={2} strokeDasharray="5 5" dot={false} connectNulls={true}
            />
            {showSR && correlationData.supportPrice > 0 && (
              <Line
                yAxisId="dollar" type="monotone" dataKey="supportPrice" name="Support Level"
                stroke="#10b981" strokeWidth={1.5} strokeDasharray="3 3" dot={false} connectNulls={true}
              />
            )}
            {showSR && correlationData.resistancePrice > 0 && (
              <Line
                yAxisId="dollar" type="monotone" dataKey="resistancePrice" name="Resistance Level"
                stroke="#f43f5e" strokeWidth={1.5} strokeDasharray="3 3" dot={false} connectNulls={true}
              />
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
                <Tooltip contentStyle={{ backgroundColor: 'rgba(15, 23, 42, 0.9)', borderColor: '#334155', borderRadius: '8px', backdropFilter: 'blur(8px)' }} labelFormatter={(l) => format(new Date(l), "MMM d, yyyy h:mm a")} itemStyle={{ fontSize: '13px' }} formatter={(v) => [typeof v === 'number' ? v.toFixed(2) : v, "Momentum"]} />
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
