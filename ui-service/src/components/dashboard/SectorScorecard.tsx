import React from "react";
import { ResponsiveContainer, BarChart, XAxis, YAxis, Tooltip, ReferenceLine, Bar, Cell } from "recharts";
import { DashboardDataProps } from "../types";

export default function SectorScorecard({ computed }: DashboardDataProps) {
  const { scorecardData } = computed;

  return (
    <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl flex flex-col">
      <h3 className="text-lg font-bold mb-1 text-white">Sector Scorecard</h3>
      <p className="text-xs text-slate-400 mb-6">Relative outperformance vs industry benchmark</p>

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
  );
}
