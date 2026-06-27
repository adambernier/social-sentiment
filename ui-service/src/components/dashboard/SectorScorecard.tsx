import React, { useState } from "react";
import { ResponsiveContainer, BarChart, XAxis, YAxis, Tooltip, ReferenceLine, Bar, Cell } from "recharts";
import { Share2, X } from "lucide-react";
import { DashboardDataProps } from "../types";
import { format } from "date-fns";

export default function SectorScorecard({ state, computed }: DashboardDataProps) {
  const { scorecardData } = computed;
  const { symbol, metrics, latestQuote } = state;
  const [isShareModalOpen, setIsShareModalOpen] = useState(false);

  return (
    <>
      <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl flex flex-col relative group">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-lg font-bold mb-1 text-white">Sector Scorecard</h3>
            <p className="text-xs text-slate-400">Relative outperformance vs industry benchmark</p>
          </div>
          <button
            onClick={() => setIsShareModalOpen(true)}
            className="p-2 text-slate-400 hover:text-white bg-white/5 hover:bg-white/10 rounded-lg transition-colors"
            title="Generate Shareable Social Card"
          >
            <Share2 className="w-4 h-4" />
          </button>
        </div>

        <div className="h-[250px] w-full mt-4">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={scorecardData} layout="vertical" margin={{ top: 0, right: 20, left: 20, bottom: 0 }}>
              <XAxis
                type="number"
                domain={[-150, 150]}
                tickFormatter={(v) => `${Math.round(v)}%`}
                stroke="#475569"
                tickLine={false}
                axisLine={{ stroke: '#334155' }}
                tick={{ fontSize: 11 }}
              />
              <YAxis
                dataKey="name"
                type="category"
                stroke="#94a3b8"
                tickLine={false}
                axisLine={false}
                tick={{ fontSize: 12, fontWeight: 500 }}
                width={70}
              />
              <Tooltip
                cursor={{fill: 'rgba(255,255,255,0.02)'}}
                contentStyle={{ backgroundColor: 'rgba(15, 23, 42, 0.9)', borderColor: '#334155', borderRadius: '8px', backdropFilter: 'blur(8px)' }}
                formatter={(value: any) => [`${value > 0 ? '+' : ''}${Number(value).toFixed(1)}%`, 'vs. Sector']}
                itemStyle={{ fontSize: '13px' }}
              />
              <ReferenceLine x={0} stroke="#64748b" />
              <Bar dataKey="value" barSize={24} radius={[0, 4, 4, 0]}>
                {scorecardData.map((entry: any, index: number) => (
                  <Cell key={`cell-${index}`} fill={entry.value >= 0 ? '#10b981' : '#f43f5e'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      {/* Share Card Modal */}
      {isShareModalOpen && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-md flex items-center justify-center p-4 z-50">
          <div className="flex flex-col gap-4 items-center">
            {/* Modal Header */}
            <div className="flex justify-between items-center w-full max-w-[500px]">
              <span className="text-xs text-slate-400 font-semibold uppercase tracking-widest">Social Media Scorecard Preview</span>
              <button 
                onClick={() => setIsShareModalOpen(false)}
                className="p-1.5 text-slate-400 hover:text-white bg-white/5 hover:bg-white/10 rounded-lg transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* The 1:1 Share Card */}
            <div className="w-[500px] h-[500px] bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-slate-900 via-[#0f111a] to-black border border-white/10 rounded-3xl p-8 flex flex-col justify-between shadow-2xl relative overflow-hidden select-none">
              
              {/* Glow Accent */}
              <div className="absolute top-0 right-0 w-48 h-48 bg-indigo-500/10 rounded-full blur-[80px] pointer-events-none" />
              <div className="absolute bottom-0 left-0 w-48 h-48 bg-emerald-500/5 rounded-full blur-[80px] pointer-events-none" />

              {/* Card Header */}
              <div className="flex justify-between items-start">
                <div>
                  <h2 className="text-4xl font-extrabold text-white tracking-tight">{symbol}</h2>
                  <p className="text-xs text-slate-400 font-semibold tracking-wider uppercase mt-1">Sector: {metrics?.sector || "General Market"}</p>
                </div>
                <div className="text-right">
                  <span className="text-[10px] text-slate-500 font-bold uppercase tracking-widest bg-white/5 px-2.5 py-1 rounded-md border border-white/5">
                    Sector Scorecard
                  </span>
                </div>
              </div>

              {/* Metrics Row */}
              <div className="grid grid-cols-3 gap-3">
                <div className="bg-slate-950/50 border border-white/5 rounded-2xl p-3 flex flex-col items-center">
                  <span className="text-[10px] text-slate-500 font-bold uppercase tracking-wider">Price</span>
                  <span className="text-lg font-bold text-white mt-1">
                    {latestQuote?.price !== undefined && latestQuote?.price !== null ? `$${latestQuote.price.toFixed(2)}` : '---'}
                  </span>
                </div>
                <div className="bg-slate-950/50 border border-white/5 rounded-2xl p-3 flex flex-col items-center">
                  <span className="text-[10px] text-slate-500 font-bold uppercase tracking-wider">P/E Ratio</span>
                  <span className="text-lg font-bold text-white mt-1">
                    {metrics?.pe_ratio !== undefined && metrics?.pe_ratio !== null ? `${metrics.pe_ratio.toFixed(1)}x` : '---'}
                  </span>
                </div>
                <div className="bg-slate-950/50 border border-white/5 rounded-2xl p-3 flex flex-col items-center">
                  <span className="text-[10px] text-slate-500 font-bold uppercase tracking-wider">Beta</span>
                  <span className="text-lg font-bold text-white mt-1">
                    {metrics?.beta !== undefined && metrics?.beta !== null ? metrics.beta.toFixed(2) : '---'}
                  </span>
                </div>
              </div>

              {/* Performance Chart Section */}
              <div className="flex-1 flex flex-col justify-center my-4">
                <div className="text-center mb-3">
                  <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Performance vs Sector Benchmark</span>
                </div>
                <div className="h-[180px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={scorecardData} margin={{ top: 10, right: 10, left: 10, bottom: 5 }}>
                      <XAxis 
                        dataKey="name" 
                        stroke="#64748b" 
                        tickLine={false} 
                        axisLine={false}
                        tick={{ fontSize: 10, fontWeight: 600 }}
                      />
                      <YAxis 
                        domain={[-150, 150]} 
                        stroke="#475569" 
                        tickLine={false}
                        axisLine={false}
                        tickFormatter={(v) => `${v > 0 ? '+' : ''}${v}%`}
                        tick={{ fontSize: 9 }}
                      />
                      <ReferenceLine y={0} stroke="#334155" />
                      <Bar dataKey="value" barSize={36} radius={[4, 4, 0, 0]}>
                        {scorecardData.map((entry: any, index: number) => (
                          <Cell 
                            key={`cell-${index}`} 
                            fill={entry.value >= 0 ? '#10b981' : '#f43f5e'} 
                          />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>

              {/* Footer & Disclaimer */}
              <div className="border-t border-white/5 pt-4 flex flex-col gap-2">
                <div className="flex justify-between items-center text-[9px] text-slate-500 font-medium">
                  <span>LAST UPDATED: {metrics?.updated_at ? format(new Date(metrics.updated_at), "MMM d, h:mm a 'ET'") : '---'}</span>
                  <span>social-sentiment-dashboard</span>
                </div>
                <div className="bg-slate-950/40 border border-white/5 rounded-lg p-2 text-center text-[9px] text-slate-400 font-medium tracking-wide">
                  ⚠️ Disclaimer: Not financial advice. For informational purposes only. Data is delayed.
                </div>
              </div>

            </div>

            {/* Explanatory Help Text */}
            <p className="text-xs text-slate-400 max-w-[500px] text-center mt-2 leading-relaxed">
              💡 <strong>Tip:</strong> Take a screenshot of this square preview card to share on social media. All data and disclaimers are burned directly into the image boundaries.
            </p>
          </div>
        </div>
      )}
    </>
  );
}

