"use client";

import React from "react";
import dynamic from "next/dynamic";
import { useDashboardData } from "./hooks/useDashboardData";
import Header from "./dashboard/Header";
import TickerActivity from "./dashboard/TickerActivity";
import TelemetryGrid from "./dashboard/TelemetryGrid";
import LagSweepChart from "./dashboard/LagSweepChart";
import CorrelationChart from "./dashboard/CorrelationChart";
import OpportunityScanner from "./dashboard/OpportunityScanner";
import FundamentalMetrics from "./dashboard/FundamentalMetrics";
import Feed from "./dashboard/Feed";
import Sidebar from "./dashboard/Sidebar";

const GlobalContextPanel =
  process.env.NEXT_PUBLIC_GLOBAL_CONTEXT_ENABLED === "true"
    ? dynamic(() => import("./dashboard/GlobalContextPanel"))
    : null;

export default function Dashboard() {
  const dashboardData = useDashboardData();
  const isInitialHydrating = !dashboardData.state.hasHydrated || !dashboardData.state.hasSetDefaultHours;

  return (
    <div className="min-h-screen terminal-bg text-slate-100 p-4 md:p-8 font-sans selection:bg-indigo-500/30 relative overflow-x-hidden">
      {isInitialHydrating && (
        <div className="fixed top-0 left-0 right-0 h-0.5 bg-gradient-to-r from-indigo-500 via-cyan-400 to-emerald-400 animate-pulse z-50" />
      )}
      <div className="max-w-[1600px] mx-auto space-y-6">
        <Header {...dashboardData} />
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-6 items-start">
          <div className="xl:col-span-7 flex flex-col gap-6">
            <TickerActivity {...dashboardData} />
            <TelemetryGrid {...dashboardData} />
          </div>
          <div className="xl:col-span-5 grid grid-cols-1 md:grid-cols-2 gap-6 h-full">
            <FundamentalMetrics {...dashboardData} />
            <OpportunityScanner {...dashboardData} />
          </div>
        </div>
        {GlobalContextPanel ? (
          <GlobalContextPanel symbol={dashboardData.state.symbol} />
        ) : null}
        <CorrelationChart {...dashboardData} />
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-6">
          <Feed {...dashboardData} />
          <Sidebar {...dashboardData} />
        </div>
        <LagSweepChart {...dashboardData} />
      </div>
    </div>
  );
}
