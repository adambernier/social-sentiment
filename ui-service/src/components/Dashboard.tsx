"use client";

import React, { useState, useEffect, useMemo } from "react";
import { 
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, 
  BarChart, Bar, Cell, ComposedChart, ReferenceLine, Legend, ReferenceArea
} from "recharts";
import { format, parseISO, startOfHour } from "date-fns";
import { Activity, MessageSquare, TrendingUp, TrendingDown, Clock, Hash, Zap, HelpCircle, AlertCircle, BarChart2 } from "lucide-react";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Data Interfaces
interface Post {
  id: string; symbol: string; platform: string; text: string;
  timestamp: string; sentiment: string;
  scores: Record<string, number>; topic_label: string | null;
}
interface SentimentStat { sentiment: string; count: number; }
interface MarketQuote { timestamp: string; price: number; volume: number; market_session: string; }
interface DeltaData { reference_price: number; latest_price: number; pct_change: number; abs_change: number; }
interface MetricsData {
  pe_ratio: number | null; beta: number | null; avg_return_1y: number | null;
  inflation_adj_return_1y: number | null; pe_relative_sector: number | null;
  beta_relative_sector: number | null; return_relative_sector: number | null;
  updated_at: string;
}

export default function Dashboard() {
  const [symbol, setSymbol] = useState("SMH");
  const [hours, setHours] = useState(24);
  const [platform, setPlatform] = useState("all");
  const [isConnected, setIsConnected] = useState(false);
  
  // States
  const [posts, setPosts] = useState<Post[]>([]);
  const [sentimentStats, setSentimentStats] = useState<SentimentStat[]>([]);
  const [marketData, setMarketData] = useState<MarketQuote[]>([]);
  const [metrics, setMetrics] = useState<MetricsData | null>(null);
  
  const [latestQuote, setLatestQuote] = useState<MarketQuote | null>(null);
  const [primaryDelta, setPrimaryDelta] = useState<DeltaData | null>(null);
  const [futureSymbol, setFutureSymbol] = useState<string | null>(null);
  const [futureQuote, setFutureQuote] = useState<MarketQuote | null>(null);
  const [futureDelta, setFutureDelta] = useState<DeltaData | null>(null);
  const [futureMarketData, setFutureMarketData] = useState<MarketQuote[]>([]);
  const [vixQuote, setVixQuote] = useState<MarketQuote | null>(null);

  // Initial Fetch & Polling
  useEffect(() => {
    const fetchData = async () => {
      try {
        const platformParam = platform !== 'all' ? `&platform=${platform}` : '';
        const res = await fetch(`http://localhost:8000/stats/dashboard?symbol=${symbol}&hours=${hours}${platformParam}`);
        if (res.ok) {
          const data = await res.json();
          // To prevent the WebSocket feed from being completely overwritten and losing fresh un-polled posts,
          // we only update posts if we don't have any, or we can just let the API overwrite them. 
          // Actually, since the API returns the 500 most recent, overwriting is fine and keeps things synced.
          setPosts(data.posts || []);
          setSentimentStats(data.sentiment_stats || []);
          setMarketData(data.market_data || []);
          setMetrics(data.metrics_data || null);
          setLatestQuote(data.latest_quote || null);
          setPrimaryDelta(data.primary_delta || null);
          setFutureSymbol(data.primary_future_symbol || null);
          setFutureQuote(data.primary_future_quote || null);
          setFutureDelta(data.primary_future_delta || null);
          setFutureMarketData(data.primary_future_market_data || []);
          setVixQuote(data.vix_quote || null);
        }
      } catch (err) {
        console.error("Failed to fetch dashboard data", err);
      }
    };
    
    // Fetch immediately on mount or dependency change
    fetchData();
    
    // Poll every 60 seconds to keep pricing and market session status fresh
    const intervalId = setInterval(fetchData, 60000);
    return () => clearInterval(intervalId);
  }, [symbol, hours, platform]);

  // WebSocket Live Updates
  useEffect(() => {
    const ws = new WebSocket("ws://localhost:8000/stats/stream");
    ws.onopen = () => setIsConnected(true);
    ws.onclose = () => setIsConnected(false);
    
    ws.onmessage = (event) => {
      try {
        const newPost: Post = JSON.parse(event.data);
        if (newPost.symbol === symbol && (platform === 'all' || newPost.platform === platform)) {
          setPosts((prev) => [newPost, ...prev].slice(0, 500));
          setSentimentStats((prev) => {
            const exists = prev.find(s => s.sentiment === newPost.sentiment);
            if (exists) return prev.map(s => s.sentiment === newPost.sentiment ? { ...s, count: s.count + 1 } : s);
            return [...prev, { sentiment: newPost.sentiment, count: 1 }];
          });
        }
      } catch (err) {}
    };
    return () => ws.close();
  }, [symbol]);

  // Calculations for Telemetry
  const totalMentions = sentimentStats.reduce((sum, s) => sum + s.count, 0);
  const bullishCount = sentimentStats.find(s => s.sentiment === "positive")?.count || 0;
  const bearishCount = sentimentStats.find(s => s.sentiment === "negative")?.count || 0;
  
  const bullishPct = totalMentions ? Math.round((bullishCount / totalMentions) * 100) : 0;
  const bearishPct = totalMentions ? Math.round((bearishCount / totalMentions) * 100) : 0;
  
  const marketSession = latestQuote?.market_session || "closed";

  // Scorecard Data
  const scorecardData = useMemo(() => {
    if (!metrics) return [];
    return [
      { name: "Valuation", value: metrics.pe_relative_sector ? metrics.pe_relative_sector * 100 : 0 },
      { name: "Risk", value: metrics.beta_relative_sector ? metrics.beta_relative_sector * 100 : 0 },
      { name: "Returns", value: metrics.return_relative_sector ? metrics.return_relative_sector * 100 : 0 }
    ];
  }, [metrics]);

  // Correlation Chart Data (Binning posts by hour)
  const correlationData = useMemo(() => {
    if (!marketData.length) return { data: [], closedRegions: [] };
    
    const buckets: Record<string, any> = {};
    const refPrice = marketData[0].price;
    const refFuturePrice = futureMarketData.length > 0 ? futureMarketData[0].price : 1;

    // Pre-fill buckets for every hour in the window to prevent dropping overnight posts
    const now = new Date();
    for (let i = hours; i >= 0; i--) {
      const d = new Date(now.getTime() - i * 60 * 60 * 1000);
      const ts = startOfHour(d).toISOString();
      buckets[ts] = { timestamp: ts, positive: 0, neutral: 0, negative: 0, priceChange: null, futureChange: null, isMarketOpen: false };
    }

    marketData.forEach(q => {
      const ts = startOfHour(new Date(q.timestamp)).toISOString();
      if (buckets[ts]) {
        buckets[ts].priceChange = ((q.price - refPrice) / refPrice) * 100;
        buckets[ts].isMarketOpen = true;
      }
    });

    futureMarketData.forEach(q => {
      const ts = startOfHour(new Date(q.timestamp)).toISOString();
      if (buckets[ts]) {
        buckets[ts].futureChange = ((q.price - refFuturePrice) / refFuturePrice) * 100;
      }
    });

    posts.forEach(p => {
      const ts = startOfHour(new Date(p.timestamp)).toISOString();
      if (buckets[ts]) {
        if (p.sentiment === 'positive') buckets[ts].positive += 1;
        else if (p.sentiment === 'negative') buckets[ts].negative += 1;
        else buckets[ts].neutral += 1;
      }
    });

    const sortedData = Object.values(buckets).sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
    
    const closedRegions: { start: string, end: string }[] = [];
    let currentStart: string | null = null;
    
    sortedData.forEach((d: any, idx) => {
      if (!d.isMarketOpen) {
        if (!currentStart) currentStart = d.timestamp;
      } else {
        if (currentStart) {
          closedRegions.push({ start: currentStart, end: sortedData[idx - 1].timestamp });
          currentStart = null;
        }
      }
    });
    if (currentStart) {
      closedRegions.push({ start: currentStart, end: sortedData[sortedData.length - 1].timestamp });
    }

    // If the market is currently open, do not shade the leading edge of the chart (false positives due to data lag)
    if (latestQuote?.market_session === 'regular' && closedRegions.length > 0) {
      const lastRegion = closedRegions[closedRegions.length - 1];
      if (lastRegion.end === sortedData[sortedData.length - 1].timestamp) {
        closedRegions.pop();
      }
    }

    // Filter out "closed" regions that are less than 8 hours long.
    // Overnight gaps are ~17.5 hours. Anything smaller is a missing data gap.
    const actualClosedRegions = closedRegions.filter(r => {
      const diffMs = new Date(r.end).getTime() - new Date(r.start).getTime();
      return diffMs >= 8 * 60 * 60 * 1000;
    });

    return { data: sortedData, closedRegions: actualClosedRegions };
  }, [marketData, futureMarketData, posts, hours, latestQuote]);

  const formatXAxis = (t: string) => {
    if (hours <= 24) return format(new Date(t), "h:mm a");
    if (hours <= 168) return format(new Date(t), "MMM d, h a");
    return format(new Date(t), "MMM d");
  };

  return (
    <div className="min-h-screen bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-slate-900 via-[#0f111a] to-black text-slate-100 p-4 md:p-8 font-sans selection:bg-indigo-500/30">
      <div className="max-w-[1600px] mx-auto space-y-6">
        
        {/* Header */}
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
              <option value="AMD">AMD</option>
              <option value="ASTS">ASTS</option>
              <option value="CRWV">CRWV</option>
              <option value="INTC">INTC</option>
              <option value="NVDA">NVDA</option>
              <option value="RKLB">RKLB</option>
              <option value="SMH">SMH</option>
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
              <option value="twitter">X (Twitter)</option>
              <option value="reddit">Reddit</option>
              <option value="bluesky">Bluesky</option>
            </select>
          </div>
        </header>

        {/* Bento Grid Layout */}
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-6">
          
          {/* LEFT COLUMN - MAIN STAGE */}
          <div className="xl:col-span-8 space-y-6">
            
            {/* Telemetry Bento Grid */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors">
                <div className="flex items-center gap-2 text-slate-400 mb-2">
                  <MessageSquare className="w-4 h-4" />
                  <span className="text-xs font-medium">Mentions</span>
                </div>
                <div className="text-2xl font-bold">{totalMentions.toLocaleString()}</div>
              </div>
              <div className="bg-white/5 backdrop-blur-md border border-emerald-500/20 rounded-2xl p-4 hover:bg-white/10 transition-colors relative overflow-hidden">
                <div className="absolute top-0 right-0 p-4 opacity-10"><TrendingUp className="w-12 h-12 text-emerald-500" /></div>
                <div className="flex items-center gap-2 text-emerald-400 mb-2">
                  <span className="text-xs font-medium">Bullish</span>
                </div>
                <div className="text-2xl font-bold text-white">{bullishPct}%</div>
              </div>
              <div className="bg-white/5 backdrop-blur-md border border-rose-500/20 rounded-2xl p-4 hover:bg-white/10 transition-colors relative overflow-hidden">
                <div className="absolute top-0 right-0 p-4 opacity-10"><TrendingDown className="w-12 h-12 text-rose-500" /></div>
                <div className="flex items-center gap-2 text-rose-400 mb-2">
                  <span className="text-xs font-medium">Bearish</span>
                </div>
                <div className="text-2xl font-bold text-white">{bearishPct}%</div>
              </div>
              <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors">
                <div className="flex items-center gap-2 text-slate-400 mb-2">
                  <AlertCircle className="w-4 h-4" />
                  <span className="text-xs font-medium">Divergence</span>
                </div>
                <div className="text-2xl font-bold text-slate-500">N/A</div>
              </div>
              
              <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors">
                <div className="flex items-center gap-2 text-slate-400 mb-1">
                  <span className="text-xs font-medium">{symbol} Price</span>
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
              <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors">
                <div className="flex items-center gap-2 text-slate-400 mb-2">
                  <Activity className="w-4 h-4" />
                  <span className="text-xs font-medium">Live Volume</span>
                </div>
                <div className="text-2xl font-bold">{latestQuote?.volume?.toLocaleString() || 0}</div>
              </div>
              <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors">
                <div className="flex items-center gap-2 text-slate-400 mb-1">
                  <span className="text-xs font-medium">{futureSymbol || "NQ Futures"}</span>
                </div>
                <div className="flex items-end justify-between">
                  <div className="text-2xl font-bold">{futureQuote?.price?.toLocaleString() || '---'}</div>
                  {futureDelta && (
                    <div className={cn("text-xs font-semibold px-2 py-0.5 rounded-md mb-1", futureDelta.pct_change >= 0 ? "bg-emerald-500/20 text-emerald-400" : "bg-rose-500/20 text-rose-400")}>
                      {futureDelta.pct_change >= 0 ? '↑' : '↓'} {Math.abs(futureDelta.pct_change).toFixed(2)}%
                    </div>
                  )}
                </div>
              </div>
              <div className="bg-white/5 backdrop-blur-md border border-white/5 rounded-2xl p-4 hover:bg-white/10 transition-colors">
                <div className="flex items-center gap-2 text-slate-400 mb-2">
                  <Zap className="w-4 h-4" />
                  <span className="text-xs font-medium">VIX Regime</span>
                </div>
                <div className="text-2xl font-bold text-slate-500">N/A</div>
              </div>
            </div>

            {/* Correlation Chart */}
            <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl">
              <div className="flex items-center justify-between mb-6">
                <h2 className="text-lg font-bold flex items-center gap-2 text-white">
                  <BarChart2 className="w-5 h-5 text-indigo-400" />
                  Sentiment vs. Price Correlation
                </h2>
                <div className="flex flex-wrap items-center gap-4 text-xs font-medium text-slate-400">
                  <div className="flex items-center gap-1"><div className="w-3 h-3 rounded-sm bg-emerald-500"></div> Positive</div>
                  <div className="flex items-center gap-1"><div className="w-3 h-3 rounded-sm bg-slate-500"></div> Neutral</div>
                  <div className="flex items-center gap-1"><div className="w-3 h-3 rounded-sm bg-rose-500"></div> Negative</div>
                  <div className="hidden sm:block h-4 w-px bg-slate-700 mx-1"></div>
                  <div className="flex items-center gap-1"><div className="w-3 h-0.5 bg-[#fbbf24]"></div> {symbol} Price</div>
                  <div className="flex items-center gap-1"><div className="w-3 border-t-2 border-dashed border-[#8b5cf6]"></div> NQ Futures</div>
                </div>
              </div>
              <div className="h-[400px]">
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={correlationData.data} margin={{ top: 10, right: 10, bottom: 0, left: -20 }}>
                    <CartesianGrid stroke="#1e293b" vertical={false} strokeDasharray="3 3" />
                    
                    {correlationData.closedRegions.map((region, idx) => (
                      <ReferenceArea 
                        key={idx} 
                        x1={region.start} 
                        x2={region.end} 
                        fill="#0f172a" 
                        fillOpacity={0.6} 
                        yAxisId="left" 
                      />
                    ))}
                    
                    <XAxis 
                      dataKey="timestamp" 
                      tickFormatter={formatXAxis}
                      stroke="#64748b"
                      tickLine={false}
                      axisLine={false}
                      dy={10}
                      minTickGap={40}
                      tick={{ fontSize: 12 }}
                    />

                    <YAxis 
                      yAxisId="left"
                      stroke="#64748b"
                      tickLine={false}
                      axisLine={false}
                      tick={{ fontSize: 12 }}
                    />
                    
                    <YAxis 
                      yAxisId="right"
                      orientation="right"
                      tickFormatter={(v) => `${v > 0 ? '+' : ''}${v}%`}
                      stroke="#64748b"
                      tickLine={false}
                      axisLine={false}
                      tick={{ fontSize: 12 }}
                    />

                    <Tooltip 
                      contentStyle={{ backgroundColor: 'rgba(15, 23, 42, 0.9)', borderColor: '#334155', borderRadius: '8px', backdropFilter: 'blur(8px)' }}
                      labelFormatter={(l) => format(new Date(l), "MMM d, yyyy h:mm a")}
                      itemStyle={{ fontSize: '13px' }}
                    />

                    <Bar yAxisId="left" dataKey="positive" name="Positive Posts" stackId="a" fill="#10b981" barSize={12} radius={[0, 0, 0, 0]} />
                    <Bar yAxisId="left" dataKey="neutral" name="Neutral Posts" stackId="a" fill="#64748b" radius={[0, 0, 0, 0]} />
                    <Bar yAxisId="left" dataKey="negative" name="Negative Posts" stackId="a" fill="#f43f5e" radius={[4, 4, 0, 0]} />

                    <Line 
                      yAxisId="right"
                      type="monotone" 
                      dataKey="priceChange" 
                      name={`${symbol} Price Change`} 
                      stroke="#fbbf24" 
                      strokeWidth={3}
                      dot={false} 
                      connectNulls={true}
                    />
                    <Line 
                      yAxisId="right"
                      type="monotone" 
                      dataKey="futureChange" 
                      name="NQ Futures Change" 
                      stroke="#8b5cf6" 
                      strokeWidth={2}
                      strokeDasharray="5 5"
                      dot={false} 
                      connectNulls={true}
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            </section>

            {/* Live Social Feed Terminal */}
            <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl flex flex-col h-[400px]">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-bold flex items-center gap-2 text-white">
                  <MessageSquare className="w-5 h-5 text-sky-400" />
                  Live Social Feed
                </h2>
                <div className="text-xs text-slate-400">Showing last {posts.length} posts</div>
              </div>
              <div className="flex-1 overflow-y-auto space-y-3 pr-2 scrollbar-thin scrollbar-thumb-slate-700 scrollbar-track-transparent">
                {posts.map((post, idx) => (
                  <div key={idx} className="bg-white/5 border border-white/5 rounded-xl p-3 text-sm hover:bg-white/10 transition-colors">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <span className={cn("text-[10px] uppercase font-bold px-1.5 py-0.5 rounded", post.platform === 'twitter' ? 'bg-sky-500/20 text-sky-400' : post.platform === 'reddit' ? 'bg-orange-500/20 text-orange-400' : 'bg-blue-500/20 text-blue-400')}>
                          {post.platform}
                        </span>
                        <span className={cn("text-[10px] uppercase font-bold px-1.5 py-0.5 rounded", post.sentiment === 'positive' ? 'bg-emerald-500/20 text-emerald-400' : post.sentiment === 'negative' ? 'bg-rose-500/20 text-rose-400' : 'bg-slate-500/20 text-slate-400')}>
                          {post.sentiment}
                        </span>
                      </div>
                      <span className="text-[10px] text-slate-500">
                        {format(new Date(post.timestamp), "MMM d, h:mm:ss a")}
                      </span>
                    </div>
                    <p className="text-slate-300 leading-relaxed">{post.text}</p>
                  </div>
                ))}
                {posts.length === 0 && (
                  <div className="text-center text-slate-500 mt-10">No posts found for this time window.</div>
                )}
              </div>
            </section>
          </div>

          {/* RIGHT COLUMN - SIDEBAR */}
          <div className="xl:col-span-4 space-y-6">
            
            {/* Fundamental Metrics */}
            <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl">
              <h3 className="text-lg font-bold mb-5 text-white">Fundamental Metrics</h3>
              <div className="space-y-3">
                <div className="flex items-center justify-between p-3 bg-white/5 rounded-xl border border-white/5">
                  <span className="text-slate-400 font-medium text-sm">P/E Ratio</span>
                  <span className="font-bold text-white">{metrics?.pe_ratio !== undefined && metrics?.pe_ratio !== null ? metrics.pe_ratio.toFixed(0) : '---'}</span>
                </div>
                <div className="flex items-center justify-between p-3 bg-white/5 rounded-xl border border-white/5">
                  <span className="text-slate-400 font-medium text-sm">Beta</span>
                  <span className="font-bold text-white">{metrics?.beta !== undefined && metrics?.beta !== null ? metrics.beta.toFixed(2) : '---'}</span>
                </div>
                <div className="flex items-center justify-between p-3 bg-white/5 rounded-xl border border-white/5">
                  <span className="text-slate-400 font-medium text-sm">Annual Return</span>
                  <span className={cn("font-bold", metrics?.avg_return_1y && metrics.avg_return_1y >= 0 ? "text-emerald-400" : "text-rose-400")}>
                    {metrics?.avg_return_1y !== undefined && metrics?.avg_return_1y !== null ? `${(metrics.avg_return_1y * 100).toFixed(0)}%` : '---'}
                  </span>
                </div>
                <div className="flex items-center justify-between p-3 bg-white/5 rounded-xl border border-white/5">
                  <span className="text-slate-400 font-medium text-sm">Inflation Adj (1Y)</span>
                  <span className={cn("font-bold", metrics?.inflation_adj_return_1y && metrics.inflation_adj_return_1y >= 0 ? "text-emerald-400" : "text-rose-400")}>
                    {metrics?.inflation_adj_return_1y !== undefined && metrics?.inflation_adj_return_1y !== null ? `${(metrics.inflation_adj_return_1y * 100).toFixed(0)}%` : '---'}
                  </span>
                </div>
              </div>
              <div className="mt-5 text-xs text-center text-slate-500 font-medium tracking-wide">
                LAST UPDATED: {metrics?.updated_at ? format(new Date(metrics.updated_at), "MMM d, h:mm a 'ET'") : '---'}
              </div>
            </section>

            {/* Relative Performance Scorecard */}
            <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl flex flex-col">
              <h3 className="text-lg font-bold mb-1 text-white">Sector Scorecard</h3>
              <p className="text-xs text-slate-400 mb-6">Relative outperformance vs industry benchmark</p>
              
              <div className="h-[250px] w-full mt-4">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={scorecardData} layout="vertical" margin={{ top: 0, right: 20, left: 20, bottom: 0 }}>
                    <XAxis 
                      type="number" 
                      domain={[-150, 150]} 
                      tickFormatter={(v) => `${v}%`} 
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
                      {scorecardData.map((entry, index) => (
                        <Cell key={`cell-${index}`} fill={entry.value >= 0 ? '#10b981' : '#f43f5e'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </section>

          </div>
        </div>

      </div>
    </div>
  );
}
