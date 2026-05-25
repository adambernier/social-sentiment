"use client";

import React, { useState, useEffect, useMemo } from "react";
import { 
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, 
  BarChart, Bar, Cell, ComposedChart, ReferenceLine, Legend, ReferenceArea, Area
} from "recharts";
import { format, parseISO } from "date-fns";
import { Activity, MessageSquare, TrendingUp, TrendingDown, Clock, Hash, Zap, HelpCircle, AlertCircle, BarChart2, Newspaper } from "lucide-react";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

function formatLargeNumber(num: number | undefined | null): string {
  if (num === undefined || num === null) return '---';
  if (num >= 1_000_000) {
    return (num / 1_000_000).toFixed(1) + 'M';
  }
  if (num >= 1_000) {
    return (num / 1_000).toFixed(1) + 'K';
  }
  return num.toLocaleString();
}

// Data Interfaces
interface Post {
  id: string; symbol: string; platform: string; text: string;
  timestamp: string; sentiment: string;
  scores: Record<string, number>; topic_label: string | null;
  engagement: number;
}
interface SentimentStat { sentiment: string; count: number; }
interface TopicStat { topic_label: string | null; count: number; }
interface MarketQuote { timestamp: string; price: number; volume: number; market_session: string; }
interface DeltaData { reference_price: number; latest_price: number; pct_change: number; abs_change: number; }
interface MetricsData {
  pe_ratio: number | null; beta: number | null; avg_return_1y: number | null;
  inflation_adj_return_1y: number | null; pe_relative_sector: number | null;
  beta_relative_sector: number | null; return_relative_sector: number | null;
  updated_at: string;
}
interface OpportunityData {
  score: number;
  classification: string;
  color: string;
  strategy: string;
  description: string;
  checklist: string[];
}
interface CorrelationBucket {
  timestamp: string; positive: number; neutral: number; negative: number;
  priceChange: number | null; futureChange: number | null;
  isMarketOpen: boolean; sentimentIndex: number; sentimentSMA: number;
  rawPrice: number | null; buySignal: boolean | null; buyScore: number | null;
  sentimentMACD?: number | null; sentimentSignal?: number | null; sentimentHist?: number | null;
}
interface ClosedRegion { start: string; end: string; }
interface LagSweepValue { lag: number; r: number; }
interface CorrelationData {
  data: CorrelationBucket[]; closedRegions: ClosedRegion[];
  supportPrice: number; supportPct: number; resistancePrice: number; resistancePct: number;
  maxR: number; bestLag: number; lagSweeps?: LagSweepValue[]; correlationText: string; correlationStrength: string;
  opportunity: OpportunityData | null;
}

const platformColors: Record<string, string> = {
  twitter: 'bg-sky-500/20 text-sky-400 border border-sky-500/10',
  bluesky: 'bg-indigo-500/20 text-indigo-400 border border-indigo-500/10',
  reddit: 'bg-orange-500/20 text-orange-400 border border-orange-500/10',
  yahoo: 'bg-purple-500/20 text-purple-400 border border-purple-500/10',
  stocktwits: 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/10',
};

const platformLabels: Record<string, string> = {
  twitter: 'X/Twitter',
  reddit: 'Reddit',
  bluesky: 'Bluesky',
  yahoo: 'News Article',
  stocktwits: 'Stocktwits',
};

const topicColors: Record<string, string> = {
  "Earnings & Guidance": "bg-emerald-500/20 text-emerald-300 border border-emerald-500/30",
  "Fed & Macro": "bg-amber-500/20 text-amber-300 border border-amber-500/30",
  "Technical Analysis": "bg-blue-500/20 text-blue-300 border border-blue-500/30",
  "AI & Compute": "bg-indigo-500/20 text-indigo-300 border border-indigo-500/30",
  "Space & Satellite": "bg-cyan-500/20 text-cyan-300 border border-cyan-500/30",
  "Management & Insider": "bg-purple-500/20 text-purple-300 border border-purple-500/30",
  "M&A & Partnerships": "bg-fuchsia-500/20 text-fuchsia-300 border border-fuchsia-500/30",
  "Options & Volatility": "bg-rose-500/20 text-rose-300 border border-rose-500/30",
  "General / Outlier": "bg-slate-500/20 text-slate-300 border border-slate-500/30",
};

const topicProgressColors: Record<string, string> = {
  "Earnings & Guidance": "bg-emerald-500",
  "Fed & Macro": "bg-amber-500",
  "Technical Analysis": "bg-blue-500",
  "AI & Compute": "bg-indigo-500",
  "Space & Satellite": "bg-cyan-500",
  "Management & Insider": "bg-purple-500",
  "M&A & Partnerships": "bg-fuchsia-500",
  "Options & Volatility": "bg-rose-500",
  "General / Outlier": "bg-slate-600",
  "General Chat": "bg-slate-600",
};

const CustomizedOpportunityDot = (props: any) => {
  const { cx, cy, payload } = props;
  if (!payload || !payload.buySignal) {
    return null;
  }
  return (
    <g key={`buy-dot-${payload.timestamp}`}>
      <circle 
        cx={cx} 
        cy={cy} 
        r={7} 
        fill="none" 
        stroke="#10b981" 
        strokeWidth={1.5} 
        strokeOpacity={0.8}
        className="animate-ping"
        style={{ transformOrigin: `${cx}px ${cy}px` }}
      />
      <circle 
        cx={cx} 
        cy={cy} 
        r={4.5} 
        fill="#10b981" 
        stroke="#0f172a" 
        strokeWidth={1.5} 
      />
    </g>
  );
};

export default function Dashboard() {
  const [symbol, setSymbol] = useState("SMH");
  const [hours, setHours] = useState(24);
  const [platform, setPlatform] = useState("all");
  const [selectedTopic, setSelectedTopic] = useState("all");
  const [isConnected, setIsConnected] = useState(false);
  const [showSR, setShowSR] = useState(true);
  const [feedTab, setFeedTab] = useState<"social" | "news">("social");
  const [chartView, setChartView] = useState<"volume" | "sentiment">("volume");
  
  // States
  const [posts, setPosts] = useState<Post[]>([]);
  const [sentimentStats, setSentimentStats] = useState<SentimentStat[]>([]);
  const [topicStats, setTopicStats] = useState<TopicStat[]>([]);
  const [marketData, setMarketData] = useState<MarketQuote[]>([]);
  const [metrics, setMetrics] = useState<MetricsData | null>(null);
  
  const [latestQuote, setLatestQuote] = useState<MarketQuote | null>(null);
  const [primaryDelta, setPrimaryDelta] = useState<DeltaData | null>(null);
  const [futureSymbol, setFutureSymbol] = useState<string | null>(null);
  const [futureQuote, setFutureQuote] = useState<MarketQuote | null>(null);
  const [futureDelta, setFutureDelta] = useState<DeltaData | null>(null);
  const [futureMarketData, setFutureMarketData] = useState<MarketQuote[]>([]);
  const [vixQuote, setVixQuote] = useState<MarketQuote | null>(null);
  const [correlationData, setCorrelationData] = useState<CorrelationData>({
    data: [], closedRegions: [], supportPrice: 0, supportPct: 0,
    resistancePrice: 0, resistancePct: 0, maxR: 0, bestLag: 0, lagSweeps: [],
    correlationText: "Insufficient data for correlation", correlationStrength: "weak",
    opportunity: null
  });

  // Dynamic host determination for API and WebSocket
  const apiBase = typeof window !== 'undefined' 
    ? `${window.location.protocol}//${window.location.hostname}:8000` 
    : 'http://localhost:8000';

  const wsBase = typeof window !== 'undefined' 
    ? `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.hostname}:8000` 
    : 'ws://localhost:8000';

  // Initial Fetch & Polling
  useEffect(() => {
    const fetchData = async () => {
      try {
        const platformParam = platform !== 'all' ? `&platform=${platform}` : '';
        const topicParam = selectedTopic !== 'all' ? `&topic=${encodeURIComponent(selectedTopic)}` : '';
        const [dashRes, corrRes] = await Promise.all([
          fetch(`${apiBase}/stats/dashboard?symbol=${symbol}&hours=${hours}${platformParam}`),
          fetch(`${apiBase}/stats/correlation?symbol=${symbol}&hours=${hours}${platformParam}${topicParam}`)
        ]);
        if (dashRes.ok) {
          const data = await dashRes.json();
          setPosts(data.posts || []);
          setSentimentStats(data.sentiment_stats || []);
          setTopicStats(data.topic_stats || []);
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
        if (corrRes.ok) {
          const corrData = await corrRes.json();
          setCorrelationData(corrData);
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
  }, [symbol, hours, platform, selectedTopic, apiBase]);

  // WebSocket Live Updates
  useEffect(() => {
    const ws = new WebSocket(`${wsBase}/stats/stream`);
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
          if (newPost.topic_label) {
            setTopicStats((prev) => {
              const exists = prev.find(t => t.topic_label === newPost.topic_label);
              if (exists) return prev.map(t => t.topic_label === newPost.topic_label ? { ...t, count: t.count + 1 } : t);
              return [...prev, { topic_label: newPost.topic_label, count: 1 }];
            });
          }
        }
      } catch (err) {}
    };
    return () => ws.close();
  }, [symbol, platform, wsBase]);

  // Calculations for Telemetry
  const totalMentions = sentimentStats.reduce((sum, s) => sum + s.count, 0);
  const bullishCount = sentimentStats.find(s => s.sentiment === "positive")?.count || 0;
  const bearishCount = sentimentStats.find(s => s.sentiment === "negative")?.count || 0;
  
  const bullishPct = totalMentions ? Math.round((bullishCount / totalMentions) * 100) : 0;
  const bearishPct = totalMentions ? Math.round((bearishCount / totalMentions) * 100) : 0;
  
  const marketSession = latestQuote?.market_session || "closed";

  // Divergence calculation comparing social vs news sentiment
  const divergenceStatus = useMemo(() => {
    const socialPosts = posts.filter(p => p.platform !== 'yahoo');
    const newsPosts = posts.filter(p => p.platform === 'yahoo');

    if (socialPosts.length < 5 || newsPosts.length < 2) {
      return { label: "Aligned", color: "text-slate-400" };
    }

    const getScore = (sentiment: string) => {
      if (sentiment === 'positive') return 1;
      if (sentiment === 'negative') return -1;
      return 0;
    };

    const socialScore = socialPosts.reduce((sum, p) => sum + getScore(p.sentiment), 0) / socialPosts.length;
    const newsScore = newsPosts.reduce((sum, p) => sum + getScore(p.sentiment), 0) / newsPosts.length;

    const diff = socialScore - newsScore;
    if (diff > 0.15) {
      return { label: "Retail Lead", color: "text-emerald-400" };
    } else if (diff < -0.15) {
      return { label: "Inst. Lead", color: "text-blue-400" };
    } else {
      return { label: "Aligned", color: "text-slate-400" };
    }
  }, [posts]);

  // VIX Regime categorization from vixQuote
  const vixRegime = useMemo(() => {
    if (!vixQuote || !vixQuote.price) {
      return { label: "Quiet", color: "text-emerald-400" };
    }
    const val = vixQuote.price;
    if (val < 15) {
      return { label: `${val.toFixed(1)} Low`, color: "text-emerald-400" };
    } else if (val <= 20) {
      return { label: `${val.toFixed(1)} Norm`, color: "text-slate-300" };
    } else if (val <= 30) {
      return { label: `${val.toFixed(1)} Elev`, color: "text-amber-400" };
    } else {
      return { label: `${val.toFixed(1)} High`, color: "text-rose-400" };
    }
  }, [vixQuote]);

  // Scorecard Data
  const scorecardData = useMemo(() => {
    if (!metrics) return [];
    return [
      { name: "Valuation", value: metrics.pe_relative_sector ? metrics.pe_relative_sector * 100 : 0 },
      { name: "Risk", value: metrics.beta_relative_sector ? metrics.beta_relative_sector * 100 : 0 },
      { name: "Returns", value: metrics.return_relative_sector ? metrics.return_relative_sector * 100 : 0 }
    ];
  }, [metrics]);

  // Filter posts based on Social Stream vs News Desk tab and Topic
  const filteredFeedPosts = useMemo(() => {
    let list = posts.filter(p => feedTab === 'news' ? p.platform === 'yahoo' : p.platform !== 'yahoo');
    if (selectedTopic !== "all") {
      if (selectedTopic === "General / Outlier") {
        list = list.filter(p => p.topic_label === "General / Outlier" || !p.topic_label);
      } else {
        list = list.filter(p => p.topic_label === selectedTopic);
      }
    }
    return list;
  }, [posts, feedTab, selectedTopic]);

  // Topic calculations
  const totalTopicCount = topicStats.reduce((sum, t) => sum + t.count, 0);
  const sortedTopics = useMemo(() => {
    return [...topicStats].sort((a, b) => b.count - a.count);
  }, [topicStats]);

  // Correlation data is now fetched from the backend via /stats/correlation

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
              <option value="IREN">IREN</option>
              <option value="NVDA">NVDA</option>
              <option value="RKLB">RKLB</option>
              <option value="SMCI">SMCI</option>
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
              <option value="bluesky">Bluesky</option>
              <option value="reddit">Reddit</option>
              <option value="stocktwits">Stocktwits</option>
              <option value="yahoo">Yahoo Finance News</option>
            </select>
            <select 
              value={selectedTopic}
              onChange={(e) => setSelectedTopic(e.target.value)}
              className="bg-slate-900/80 border border-white/10 text-white text-sm rounded-lg focus:ring-indigo-500 focus:border-indigo-500 block px-4 py-2 outline-none backdrop-blur-md cursor-pointer transition-colors hover:bg-slate-800"
            >
              <option value="all">All Topics</option>
              <option value="Earnings & Guidance">Earnings & Guidance</option>
              <option value="Fed & Macro">Fed & Macro</option>
              <option value="Technical Analysis">Technical Analysis</option>
              <option value="AI & Compute">AI & Compute</option>
              <option value="Space & Satellite">Space & Satellite</option>
              <option value="Management & Insider">Management & Insider</option>
              <option value="M&A & Partnerships">M&A & Partnerships</option>
              <option value="Options & Volatility">Options & Volatility</option>
              <option value="General / Outlier">General Chat</option>
            </select>
            <button
              onClick={() => setShowSR(!showSR)}
              className={cn(
                "flex items-center gap-2 px-4 py-2 text-sm font-semibold rounded-lg border transition-all backdrop-blur-md cursor-pointer",
                showSR 
                  ? "bg-indigo-500/20 text-indigo-300 border-indigo-500/30 shadow-[0_0_15px_rgba(99,102,241,0.2)]" 
                  : "bg-slate-900/80 text-slate-400 border-white/10 hover:bg-slate-800 hover:text-slate-300"
              )}
            >
              <BarChart2 className="w-4 h-4" />
              S&R Lines
            </button>
          </div>
        </header>

        {/* Telemetry Bento Grid */}
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

              {/* Pearson Correlation Card */}
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

            {/* Lead/Lag Correlation Sweep — per-lag Pearson r behind the single maxR/bestLag number above */}
            {correlationData.lagSweeps && correlationData.lagSweeps.length > 0 && (
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
                  <ResponsiveContainer width="100%" height={150}>
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
                  <div className="flex flex-col items-center justify-center text-center gap-1.5 h-[150px] text-slate-500">
                    <AlertCircle className="w-5 h-5 text-slate-600" />
                    <p className="text-xs font-medium text-slate-400">No market-hours price data in this window</p>
                    <p className="text-[10px] max-w-xs">Correlation needs overlapping price moves — none in range (markets closed). Try a wider window.</p>
                  </div>
                )}
              </section>
            )}

            {/* Correlation Chart */}
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
                        chartView === "volume"
                          ? "bg-indigo-500 text-white shadow-md"
                          : "text-slate-400 hover:text-slate-200"
                      )}
                    >
                      Volume
                    </button>
                    <button
                      onClick={() => setChartView("sentiment")}
                      className={cn(
                        "px-2.5 py-1 text-xs font-semibold rounded-md transition-all cursor-pointer",
                        chartView === "sentiment"
                          ? "bg-indigo-500 text-white shadow-md"
                          : "text-slate-400 hover:text-slate-200"
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
                      domain={chartView === 'sentiment' ? [-1.1, 1.1] : ['auto', 'auto']}
                      tickFormatter={chartView === 'sentiment' ? (v) => `${v > 0 ? '+' : ''}${Math.round(v * 100)}%` : undefined}
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
                      yAxisId="right"
                      type="monotone" 
                      dataKey="priceChange" 
                      name={`${symbol} Price Change`} 
                      stroke="#fbbf24" 
                      strokeWidth={3}
                      dot={<CustomizedOpportunityDot />}
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
                    {showSR && correlationData.supportPrice > 0 && (
                      <ReferenceLine
                        yAxisId="right"
                        y={correlationData.supportPct}
                        stroke="#10b981"
                        strokeDasharray="3 3"
                        strokeWidth={1.5}
                        strokeOpacity={0.5}
                        label={renderSupportLabel}
                      />
                    )}
                    {showSR && correlationData.resistancePrice > 0 && (
                      <ReferenceLine
                        yAxisId="right"
                        y={correlationData.resistancePct}
                        stroke="#f43f5e"
                        strokeDasharray="3 3"
                        strokeWidth={1.5}
                        strokeOpacity={0.5}
                        label={renderResistanceLabel}
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
                        <XAxis
                          dataKey="timestamp"
                          tickFormatter={formatXAxis}
                          stroke="#64748b"
                          tickLine={false}
                          axisLine={false}
                          dy={6}
                          minTickGap={40}
                          tick={{ fontSize: 10 }}
                        />
                        <YAxis
                          stroke="#64748b"
                          tickLine={false}
                          axisLine={false}
                          tick={{ fontSize: 10 }}
                          domain={['auto', 'auto']}
                        />
                        <Tooltip
                          contentStyle={{ backgroundColor: 'rgba(15, 23, 42, 0.9)', borderColor: '#334155', borderRadius: '8px', backdropFilter: 'blur(8px)' }}
                          labelFormatter={(l) => format(new Date(l), "MMM d, yyyy h:mm a")}
                          itemStyle={{ fontSize: '13px' }}
                          formatter={(v) => [typeof v === 'number' ? v.toFixed(4) : v, "Momentum"]}
                        />
                        <ReferenceLine y={0} stroke="#475569" />
                        <Bar dataKey="sentimentHist" name="Sentiment Momentum">
                          {correlationData.data.map((entry, idx) => (
                            <Cell key={idx} fill={(entry.sentimentHist ?? 0) >= 0 ? "#10b981" : "#f43f5e"} />
                          ))}
                        </Bar>
                      </ComposedChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              )}
            </section>

            {/* Bottom Grid Split */}
            <div className="grid grid-cols-1 xl:grid-cols-12 gap-6">
              
              {/* LEFT COLUMN - Feed */}
              <div className="xl:col-span-8">
                {/* Live Social Feed Terminal */}
                <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl flex flex-col h-[800px]">
              <div className="flex items-center justify-between mb-4 pb-2 border-b border-white/5">
                <div className="flex items-center gap-6">
                  <button
                    onClick={() => setFeedTab("social")}
                    className={cn(
                      "text-base font-bold flex items-center gap-2 pb-2 transition-all relative cursor-pointer outline-none",
                      feedTab === "social" 
                        ? "text-white" 
                        : "text-slate-400 hover:text-slate-200"
                    )}
                  >
                    <MessageSquare className="w-4 h-4 text-sky-400" />
                    Social Stream
                    {feedTab === "social" && (
                      <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-indigo-500 rounded-full"></div>
                    )}
                  </button>
                  <button
                    onClick={() => setFeedTab("news")}
                    className={cn(
                      "text-base font-bold flex items-center gap-2 pb-2 transition-all relative cursor-pointer outline-none",
                      feedTab === "news" 
                        ? "text-white" 
                        : "text-slate-400 hover:text-slate-200"
                    )}
                  >
                    <Newspaper className="w-4 h-4 text-purple-400" />
                    News Desk
                    {feedTab === "news" && (
                      <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-indigo-500 rounded-full"></div>
                    )}
                  </button>
                </div>
                <div className="text-xs text-slate-400">Showing last {filteredFeedPosts.length} items</div>
              </div>
              <div className="flex-1 overflow-y-auto space-y-3 pr-2 scrollbar-thin scrollbar-thumb-slate-700 scrollbar-track-transparent">
                {filteredFeedPosts.map((post, idx) => (
                  <div key={idx} className="bg-white/5 border border-white/5 rounded-xl p-3 text-sm hover:bg-white/10 transition-colors">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className={cn("text-[10px] uppercase font-bold px-1.5 py-0.5 rounded", platformColors[post.platform] || 'bg-slate-500/20 text-slate-400 border border-white/5')}>
                          {platformLabels[post.platform] || post.platform}
                        </span>
                        <span className={cn("text-[10px] uppercase font-bold px-1.5 py-0.5 rounded", post.sentiment === 'positive' ? 'bg-emerald-500/20 text-emerald-400' : post.sentiment === 'negative' ? 'bg-rose-500/20 text-rose-400' : 'bg-slate-500/20 text-slate-400')}>
                          {post.sentiment}
                        </span>
                        {post.topic_label && post.topic_label !== "General / Outlier" && (
                          <span className={cn("text-[10px] font-bold px-1.5 py-0.5 rounded", topicColors[post.topic_label] || 'bg-slate-500/20 text-slate-400 border border-white/5')}>
                            {post.topic_label}
                          </span>
                        )}
                      </div>
                      <span className="text-[10px] text-slate-500 shrink-0">
                        {format(new Date(post.timestamp), "MMM d, h:mm:ss a")}
                      </span>
                    </div>
                    <p className="text-slate-300 leading-relaxed">{post.text}</p>
                  </div>
                ))}
                {filteredFeedPosts.length === 0 && (
                  <div className="text-center text-slate-500 mt-10">No items found for this time window.</div>
                )}
              </div>
            </section>
          </div>

          {/* RIGHT COLUMN - SIDEBAR */}
          <div className="xl:col-span-4 space-y-6">
            
            {/* Opportunity Scanner */}
            <section className={cn(
              "bg-slate-900/40 backdrop-blur-xl border rounded-2xl p-6 shadow-2xl relative overflow-hidden flex flex-col transition-all duration-300",
              correlationData.opportunity
                ? (correlationData.opportunity.color === 'emerald' ? 'border-emerald-500/25 shadow-[0_0_20px_rgba(16,185,129,0.08)]' :
                   correlationData.opportunity.color === 'teal' ? 'border-teal-500/25 shadow-[0_0_20px_rgba(20,184,166,0.08)]' :
                   correlationData.opportunity.color === 'rose' ? 'border-rose-500/25 shadow-[0_0_20px_rgba(244,63,94,0.08)]' :
                   'border-white/5')
                : 'border-white/5'
            )}>
              <div className="absolute top-0 right-0 p-4 opacity-[0.03] pointer-events-none">
                <Zap className="w-16 h-16 text-indigo-500" />
              </div>
              <h3 className="text-lg font-bold text-white flex items-center gap-2">
                <Zap className="w-4 h-4 text-indigo-400" />
                Opportunity Scanner
              </h3>
              <p className="text-xs text-slate-400 mb-4">Real-time trade opportunity signals & strategies</p>

              {!correlationData.opportunity ? (
                <div className="text-slate-500 text-xs text-center py-8">Calculating opportunity parameters...</div>
              ) : (() => {
                const opp = correlationData.opportunity;
                
                const colorMap: Record<string, { bg: string, text: string, border: string, progress: string }> = {
                  emerald: { bg: 'bg-emerald-500/10', text: 'text-emerald-400', border: 'border-emerald-500/20', progress: 'bg-emerald-500' },
                  teal: { bg: 'bg-teal-500/10', text: 'text-teal-400', border: 'border-teal-500/20', progress: 'bg-teal-500' },
                  slate: { bg: 'bg-slate-500/10', text: 'text-slate-400', border: 'border-white/10', progress: 'bg-slate-400' },
                  rose: { bg: 'bg-rose-500/10', text: 'text-rose-400', border: 'border-rose-500/20', progress: 'bg-rose-500' },
                };
                const theme = colorMap[opp.color] || colorMap.slate;

                const checklistItems = [
                  { key: "support", text: "Price near support zone", active: opp.checklist.some(s => s.toLowerCase().includes("support")) },
                  { key: "crossover", text: "Bullish sentiment crossover", active: opp.checklist.some(s => s.toLowerCase().includes("crossover")) },
                  { key: "divergence", text: "Bullish sentiment divergence", active: opp.checklist.some(s => s.toLowerCase().includes("divergence")) },
                  { key: "valuation", text: "Favorable relative valuation", active: opp.checklist.some(s => s.toLowerCase().includes("valued")) },
                ];

                return (
                  <div className="space-y-5">
                    <div>
                      <div className="flex items-center justify-between mb-2">
                        <span className={cn("text-[10px] font-bold px-2 py-0.5 rounded border uppercase tracking-wider", theme.bg, theme.text, theme.border)}>
                          {opp.classification}
                        </span>
                        <span className="text-xs font-bold text-white">{Math.round(opp.score)}% Setup Score</span>
                      </div>
                      <div className="h-1.5 w-full bg-slate-800 rounded-full overflow-hidden">
                        <div className={cn("h-full rounded-full transition-all duration-500", theme.progress)} style={{ width: `${opp.score}%` }}></div>
                      </div>
                    </div>

                    <div className="bg-slate-950/50 border border-white/5 rounded-xl p-4">
                      <div className="text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">Recommended Strategy</div>
                      <div className="text-sm font-extrabold text-white flex items-center gap-1.5 mb-1">
                        <TrendingUp className={cn("w-4 h-4 shrink-0", opp.score >= 50.0 ? "text-emerald-400" : "text-slate-400")} />
                        {opp.strategy}
                      </div>
                      <p className="text-xs text-slate-400 leading-relaxed font-medium">{opp.description}</p>
                    </div>

                    <div className="space-y-2">
                      <div className="text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-2">Signal Checklist</div>
                      {checklistItems.map((item) => (
                        <div key={item.key} className="flex items-center gap-2.5">
                          <div className={cn(
                            "w-4 h-4 rounded-full flex items-center justify-center border text-[9px] font-bold shrink-0",
                            item.active
                              ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                              : "bg-slate-950 text-slate-600 border-white/5"
                          )}>
                            {item.active ? "✓" : "○"}
                          </div>
                          <span className={cn("text-xs font-medium", item.active ? "text-slate-200" : "text-slate-500")}>
                            {item.text}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })()}
            </section>

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
                      {scorecardData.map((entry, index) => (
                        <Cell key={`cell-${index}`} fill={entry.value >= 0 ? '#10b981' : '#f43f5e'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </section>

            {/* Topic Distribution */}
            <section className="bg-slate-900/40 backdrop-blur-xl border border-white/5 rounded-2xl p-6 shadow-2xl flex flex-col">
              <h3 className="text-lg font-bold mb-1 text-white">Topic Distribution</h3>
              <p className="text-xs text-slate-400 mb-4">NLP zero-shot classification of social volume</p>
              
              <div className="space-y-3 max-h-[220px] overflow-y-auto pr-1 scrollbar-thin scrollbar-thumb-slate-800 scrollbar-track-transparent">
                {sortedTopics.map((topic, idx) => {
                  const label = topic.topic_label || "General Chat";
                  const count = topic.count;
                  const pct = totalTopicCount ? Math.round((count / totalTopicCount) * 100) : 0;
                  const colorClass = topicProgressColors[label] || "bg-slate-500";
                  
                  return (
                    <div key={idx} className="space-y-1">
                      <div className="flex items-center justify-between text-xs font-semibold">
                        <span className="text-slate-300">{label}</span>
                        <span className="text-slate-400">{count} ({pct}%)</span>
                      </div>
                      <div className="h-1.5 w-full bg-slate-800 rounded-full overflow-hidden">
                        <div className={cn("h-full rounded-full transition-all duration-500", colorClass)} style={{ width: `${pct}%` }}></div>
                      </div>
                    </div>
                  );
                })}
                {sortedTopics.length === 0 && (
                  <div className="text-center text-slate-500 text-xs py-4">No topic data available.</div>
                )}
              </div>
            </section>

          </div>
        </div>

      </div>
    </div>
  );
}
