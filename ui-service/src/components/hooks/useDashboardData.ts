import { useState, useEffect, useMemo } from 'react';
import { 
  Post, SentimentStat, TopicStat, LeaderboardEntry, 
  MarketQuote, DeltaData, MetricsData, CorrelationData 
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
  const [drillDownPosts, setDrillDownPosts] = useState<Post[]>([]);
  const [isDrillDownLoading, setIsDrillDownLoading] = useState(false);
  
  // States
  const [posts, setPosts] = useState<Post[]>([]);
  const [sentimentStats, setSentimentStats] = useState<SentimentStat[]>([]);
  const [topicStats, setTopicStats] = useState<TopicStat[]>([]);
  const [leaderboard, setLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [marketData, setMarketData] = useState<MarketQuote[]>([]);
  const [metrics, setMetrics] = useState<MetricsData | null>(null);
  
  const [hasSetDefaultHours, setHasSetDefaultHours] = useState(false);
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

  const apiBase = typeof window !== 'undefined' 
    ? `${window.location.protocol}//${window.location.hostname}:8000` 
    : 'http://localhost:8000';

  const wsBase = typeof window !== 'undefined' 
    ? `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.hostname}:8000` 
    : 'ws://localhost:8000';

  useEffect(() => {
    const fetchData = async () => {
      try {
        const platformParam = platform !== 'all' ? `&platform=${platform}` : '';
        const topicParam = selectedTopic !== 'all' ? `&topic=${encodeURIComponent(selectedTopic)}` : '';
        const [dashRes, corrRes, leaderRes] = await Promise.all([
          fetch(`${apiBase}/stats/dashboard?symbol=${symbol}&hours=${hours}${platformParam}`),
          fetch(`${apiBase}/stats/correlation?symbol=${symbol}&hours=${hours}${platformParam}${topicParam}`),
          fetch(`${apiBase}/stats/leaderboard`)
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
        if (leaderRes.ok) {
          setLeaderboard(await leaderRes.json());
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
      return;
    }
    const fetchDrillDown = async () => {
      setIsDrillDownLoading(true);
      try {
        const start = new Date(selectedHour);
        const end = new Date(start.getTime() + 60 * 60 * 1000);
        const platformParam = platform !== 'all' ? `&platform=${platform}` : '';
        const res = await fetch(`${apiBase}/posts?symbol=${symbol}&start_time=${start.toISOString()}&end_time=${end.toISOString()}${platformParam}&limit=1000`);
        if (res.ok) {
          const data = await res.json();
          setDrillDownPosts(data || []);
        }
      } catch (err) {
        console.error("Failed to fetch historical posts", err);
      } finally {
        setIsDrillDownLoading(false);
      }
    };
    fetchDrillDown();
  }, [selectedHour, symbol, platform, apiBase]);

  useEffect(() => {
    if (!hasSetDefaultHours && latestQuote) {
      if (latestQuote.market_session !== 'regular') {
        setHours(168);
      }
      setHasSetDefaultHours(true);
    }
  }, [latestQuote, hasSetDefaultHours]);

  const totalMentions = sentimentStats.reduce((sum, s) => sum + s.count, 0);
  const bullishCount = sentimentStats.find(s => s.sentiment === "positive")?.count || 0;
  const bearishCount = sentimentStats.find(s => s.sentiment === "negative")?.count || 0;
  
  const bullishPct = totalMentions ? Math.round((bullishCount / totalMentions) * 100) : 0;
  const bearishPct = totalMentions ? Math.round((bearishCount / totalMentions) * 100) : 0;
  
  const marketSession = latestQuote?.market_session || "closed";

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
      posts, sentimentStats, topicStats, leaderboard, marketData, metrics,
      latestQuote, primaryDelta, futureSymbol, futureQuote, futureDelta, futureMarketData,
      vixQuote, correlationData, selectedHour, isDrillDownLoading
    },
    setters: {
      setSymbol, setHours, setPlatform, setSelectedTopic, setShowSR, setFeedTab, setChartView, setSelectedHour
    },
    computed: {
      totalMentions, bullishPct, bearishPct, marketSession, divergenceStatus,
      vixRegime, scorecardData, filteredFeedPosts, totalTopicCount, sortedTopics
    }
  };
}
