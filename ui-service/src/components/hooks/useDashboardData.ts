import { useState, useEffect, useMemo } from 'react';
import {
  Post, SentimentStat, TopicStat, LeaderboardEntry,
  MarketQuote, DeltaData, MetricsData, CorrelationData, SourceHealth
} from '../types';
import { isNewsPlatform } from '../dashboard/constants';

export function useDashboardData() {
  const [symbol, setSymbol] = useState("SMH");
  const [hours, setHours] = useState(24);
  const [platform, setPlatform] = useState("all");
  const [selectedTopic, setSelectedTopic] = useState("all");
  const [isConnected, setIsConnected] = useState(false);
  const [showSR, setShowSR] = useState(true);
  const [feedTab, setFeedTab] = useState<"social" | "news">("social");
  const [chartView, setChartView] = useState<"volume" | "sentiment">("volume");
  const [selectedHour, setSelectedHour] = useState<string | null>(null);
  const [hideExtended, setHideExtended] = useState(true);
  const [drillDownPosts, setDrillDownPosts] = useState<Post[]>([]);
  const [isDrillDownLoading, setIsDrillDownLoading] = useState(false);
  const [hasHydrated, setHasHydrated] = useState(false);
  
  // Persist symbol across reloads
  useEffect(() => {
    if (typeof window !== 'undefined') {
      const savedSymbol = localStorage.getItem('social_sentiment_symbol');
      if (savedSymbol) {
        setSymbol(savedSymbol);
      }
      setHasHydrated(true);
    }
  }, []);

  useEffect(() => {
    if (hasHydrated) {
      localStorage.setItem('social_sentiment_symbol', symbol);
    }
  }, [symbol, hasHydrated]);
  
  // States
  const [posts, setPosts] = useState<Post[]>([]);
  const [sentimentStats, setSentimentStats] = useState<SentimentStat[]>([]);
  const [topicStats, setTopicStats] = useState<TopicStat[]>([]);
  const [leaderboard, setLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [sourceHealth, setSourceHealth] = useState<SourceHealth[]>([]);
  const [marketData, setMarketData] = useState<MarketQuote[]>([]);
  const [metrics, setMetrics] = useState<MetricsData | null>(null);
  
  const [hasSetDefaultHours, setHasSetDefaultHours] = useState(false);
  const [latestQuote, setLatestQuote] = useState<MarketQuote | null | undefined>(undefined);
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

  const apiBase = '/api';

  const wsBase = typeof window !== 'undefined' 
    ? `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api` 
    : 'ws://localhost:3000/api';

  useEffect(() => {
    const fetchData = async () => {
      try {
        const platformParam = platform !== 'all' ? `&platform=${platform}` : '';
        const topicParam = selectedTopic !== 'all' ? `&topic=${encodeURIComponent(selectedTopic)}` : '';
        const [dashRes, corrRes, leaderRes, sourcesRes] = await Promise.all([
          fetch(`${apiBase}/stats/dashboard?symbol=${symbol}&hours=${hours}${platformParam}`),
          fetch(`${apiBase}/stats/correlation?symbol=${symbol}&hours=${hours}${platformParam}${topicParam}`),
          fetch(`${apiBase}/stats/leaderboard`),
          fetch(`${apiBase}/stats/sources`)
        ]);
        
        let currentLeaderboard = [];
        if (leaderRes.ok) {
          currentLeaderboard = await leaderRes.json();
          setLeaderboard(currentLeaderboard);
        }

        // Auto-switch away from default 'SMH' if it's not active
        if (currentLeaderboard.length > 0) {
          const symbolExists = currentLeaderboard.find((l: any) => l.symbol === symbol);
          if (!symbolExists && symbol === "SMH") {
            setSymbol(currentLeaderboard[0].symbol);
            return; // Abort this fetch and let the symbol change trigger a new one
          }
        }

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
        if (sourcesRes.ok) {
          setSourceHealth(await sourcesRes.json());
        }
      } catch (err) {
        console.error("Failed to fetch dashboard data", err);
      }
    };
    
    fetchData();
    const intervalId = setInterval(fetchData, 60000);
    return () => clearInterval(intervalId);
  }, [symbol, hours, platform, selectedTopic, apiBase]);

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

  useEffect(() => {
    if (!selectedHour) {
      setDrillDownPosts([]);
      setIsDrillDownLoading(false);
      return;
    }
    const fetchDrillDown = async () => {
      setIsDrillDownLoading(true);
      try {
        const platformParam = platform !== 'all' ? `&platform=${platform}` : '';
        const topicParam = selectedTopic !== 'all' ? `&topic=${encodeURIComponent(selectedTopic)}` : '';
        const res = await fetch(`${apiBase}/stats/posts?symbol=${symbol}&hour=${selectedHour}${platformParam}${topicParam}`);
        if (res.ok) {
          setDrillDownPosts(await res.json());
        }
      } catch (err) {
        console.error(err);
      }
      setIsDrillDownLoading(false);
    };
    fetchDrillDown();
  }, [symbol, selectedHour, platform, selectedTopic, apiBase]);

  const QUOTE_STALE_MS = 15 * 60 * 1000;
  const latestQuoteFresh = latestQuote 
    ? Date.now() - new Date(latestQuote.timestamp).getTime() < QUOTE_STALE_MS
    : false;

  useEffect(() => {
    if (!hasSetDefaultHours && latestQuote !== undefined) {
      if (latestQuote && (latestQuote.market_session !== 'regular' || !latestQuoteFresh)) {
        setHours(168);
      }
      setHasSetDefaultHours(true);
    }
  }, [latestQuote, hasSetDefaultHours, latestQuoteFresh]);

  const totalMentions = sentimentStats.reduce((sum, s) => sum + s.count, 0);
  const bullishCount = sentimentStats.find(s => s.sentiment === "positive")?.count || 0;
  const bearishCount = sentimentStats.find(s => s.sentiment === "negative")?.count || 0;
  
  const bullishPct = totalMentions ? Math.round((bullishCount / totalMentions) * 100) : 0;
  const bearishPct = totalMentions ? Math.round((bearishCount / totalMentions) * 100) : 0;
  
  // Override a stale 'regular' (weekend/holiday/stopped producer) to 'closed' so
  // the badge reflects whether the market is open *now*, not at the last quote.
  const marketSession =
    latestQuote?.market_session === "regular" && !latestQuoteFresh
      ? "closed"
      : latestQuote?.market_session || "closed";

  const divergenceStatus = useMemo(() => {
    const socialPosts = posts.filter(p => !isNewsPlatform(p.platform));
    const newsPosts = posts.filter(p => isNewsPlatform(p.platform));

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

  const scorecardData = useMemo(() => {
    if (!metrics) return [];
    return [
      { name: "Valuation", value: metrics.pe_relative_sector ? metrics.pe_relative_sector * 100 : 0 },
      { name: "Risk", value: metrics.beta_relative_sector ? metrics.beta_relative_sector * 100 : 0 },
      { name: "Returns", value: metrics.return_relative_sector ? metrics.return_relative_sector * 100 : 0 }
    ];
  }, [metrics]);

  const filteredFeedPosts = useMemo(() => {
    const sourcePosts = selectedHour ? drillDownPosts : posts;
    let list = sourcePosts.filter(p => feedTab === 'news' ? isNewsPlatform(p.platform) : !isNewsPlatform(p.platform));
    if (selectedTopic !== "all") {
      if (selectedTopic === "General / Outlier") {
        list = list.filter(p => p.topic_label === "General / Outlier" || !p.topic_label);
      } else {
        list = list.filter(p => p.topic_label === selectedTopic);
      }
    }
    return list;
  }, [posts, drillDownPosts, selectedHour, feedTab, selectedTopic]);

  const totalTopicCount = topicStats.reduce((sum, t) => sum + t.count, 0);
  const sortedTopics = useMemo(() => {
    return [...topicStats].sort((a, b) => b.count - a.count);
  }, [topicStats]);

  return {
    state: {
      symbol, hours, platform, selectedTopic, isConnected, showSR, feedTab, chartView,
      posts, sentimentStats, topicStats, leaderboard, sourceHealth, marketData, metrics,
      latestQuote, primaryDelta, futureSymbol, futureQuote, futureDelta, futureMarketData,
      vixQuote, correlationData, selectedHour, isDrillDownLoading, hasHydrated, hasSetDefaultHours, hideExtended
    },
    setters: {
      setSymbol, setHours, setPlatform, setSelectedTopic, setShowSR, setFeedTab, setChartView, setSelectedHour, setHideExtended
    },
    computed: {
      totalMentions, bullishPct, bearishPct, marketSession, divergenceStatus,
      vixRegime, scorecardData, filteredFeedPosts, totalTopicCount, sortedTopics
    }
  };
}
