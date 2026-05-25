"use client";

import React from "react";
import { useDashboardData } from "./hooks/useDashboardData";
import Header from "./dashboard/Header";
import TickerActivity from "./dashboard/TickerActivity";
import TelemetryGrid from "./dashboard/TelemetryGrid";
import LagSweepChart from "./dashboard/LagSweepChart";
import CorrelationChart from "./dashboard/CorrelationChart";
import Feed from "./dashboard/Feed";
import Sidebar from "./dashboard/Sidebar";

export default function Dashboard() {
  const dashboardData = useDashboardData();

  if (!dashboardData.state.hasHydrated || !dashboardData.state.hasSetDefaultHours) {
    return (
      <div className="min-h-screen bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-slate-900 via-[#0f111a] to-black text-slate-100 flex items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <div className="w-8 h-8 rounded-full border-2 border-indigo-500/30 border-t-indigo-500 animate-spin"></div>
          <p className="text-slate-400 text-sm animate-pulse">Loading dashboard...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-slate-900 via-[#0f111a] to-black text-slate-100 p-4 md:p-8 font-sans selection:bg-indigo-500/30">
      <div className="max-w-[1600px] mx-auto space-y-6">
        <Header {...dashboardData} />
        <TickerActivity {...dashboardData} />
        <TelemetryGrid {...dashboardData} />
        <LagSweepChart {...dashboardData} />
        <CorrelationChart {...dashboardData} />
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-6">
          <Feed {...dashboardData} />
          <Sidebar {...dashboardData} />
        </div>
      </div>
    </div>
  );
}
