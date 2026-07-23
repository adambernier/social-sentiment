import React from "react";
import { BarChart, Bar, Cell, CartesianGrid, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { Activity, AlertCircle } from "lucide-react";
import type { DashboardDataProps } from "../hooks/useDashboardData";

export default function LagSweepChart({ state }: DashboardDataProps) {
  const { correlationData } = state;

  if (!correlationData.lagSweeps || correlationData.lagSweeps.length === 0) return null;

  return (
    <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-4 shadow-2xl">
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-2">
          <Activity className="w-3.5 h-3.5 text-indigo-400" />
          Correlation Lag Sweep (±5h)
        </h4>
        <span className="text-[10px] text-slate-500">
          &lt; 0: price leads&nbsp;·&nbsp;&gt; 0: sentiment leads
        </span>
      </div>
      {correlationData.lagSweeps.some((s) => s.r !== 0) ? (
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={correlationData.lagSweeps} margin={{ top: 4, right: 12, bottom: 0, left: -16 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
            <XAxis
              dataKey="lag"
              tickFormatter={(lag) => (lag > 0 ? `+${lag}h` : lag === 0 ? "0" : `${lag}h`)}
              stroke="#64748b"
              fontSize={10}
            />
            <YAxis domain={[-1, 1]} ticks={[-1, -0.5, 0, 0.5, 1]} stroke="#64748b" fontSize={10} />
            <Tooltip
              contentStyle={{ backgroundColor: 'rgba(15, 23, 42, 0.9)', borderColor: '#334155', borderRadius: '8px', backdropFilter: 'blur(8px)' }}
              itemStyle={{ fontSize: '13px' }}
              formatter={(value) => [typeof value === 'number' ? value.toFixed(3) : value, "Pearson r"]}
              labelFormatter={(lag) => (lag > 0 ? `+${lag}h · sentiment leads price` : lag < 0 ? `${lag}h · price leads sentiment` : "Coincident (0h)")}
            />
            <ReferenceLine y={0} stroke="#475569" />
            <Bar dataKey="r" radius={[2, 2, 0, 0]}>
              {correlationData.lagSweeps.map((entry) => (
                <Cell
                  key={entry.lag}
                  fill={entry.lag === correlationData.bestLag && entry.r !== 0 ? "#10b981" : "#3b82f6"}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="flex flex-col items-center justify-center text-center gap-1.5 h-[220px] text-slate-500">
          <AlertCircle className="w-5 h-5 text-slate-600" />
          <p className="text-xs font-medium text-slate-400">No market-hours price data in this window</p>
          <p className="text-[10px] max-w-xs">Correlation needs overlapping price moves — none in range (markets closed). Try a wider window.</p>
        </div>
      )}

    </section>
  );
}
