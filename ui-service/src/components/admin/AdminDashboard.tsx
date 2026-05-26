"use client";

import React, { useState, useEffect } from "react";
import { ShieldAlert, Plus, Edit2, Trash2, Power, PowerOff, Save, X, ArrowLeft, Activity } from "lucide-react";
import { cn } from "../dashboard/utils";
import Link from "next/link";

interface TrackedSymbol {
  symbol: string;
  keywords: string[];
  future: string | null;
  sector: string | null;
  require_uppercase: boolean;
  block_phrases: string[];
  is_active: boolean;
}

export default function AdminDashboard() {
  const [symbols, setSymbols] = useState<TrackedSymbol[]>([]);
  const [loading, setLoading] = useState(true);
  const [apiKey, setApiKey] = useState("");
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  
  // Modal state
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingSymbol, setEditingSymbol] = useState<TrackedSymbol | null>(null);
  const [formData, setFormData] = useState<Partial<TrackedSymbol>>({});
  const [rawKeywords, setRawKeywords] = useState("");
  const [rawBlockPhrases, setRawBlockPhrases] = useState("");

  useEffect(() => {
    const storedKey = localStorage.getItem("ADMIN_API_KEY");
    if (storedKey) {
      setApiKey(storedKey);
      fetchSymbols(storedKey);
    } else {
      setLoading(false);
    }
  }, []);

  const fetchSymbols = async (key: string) => {
    setLoading(true);
    try {
      const res = await fetch("http://localhost:8000/api/admin/symbols", {
        headers: { "X-API-Key": key },
      });
      if (res.status === 403) {
        setIsAuthenticated(false);
        localStorage.removeItem("ADMIN_API_KEY");
      } else if (res.ok) {
        const data = await res.json();
        setSymbols(data);
        setIsAuthenticated(true);
        localStorage.setItem("ADMIN_API_KEY", key);
      }
    } catch (err) {
      console.error(err);
    }
    setLoading(false);
  };

  const handleLogin = (e: React.FormEvent) => {
    e.preventDefault();
    fetchSymbols(apiKey);
  };

  const openModal = (symbol?: TrackedSymbol) => {
    if (symbol) {
      setEditingSymbol(symbol);
      setFormData(symbol);
      setRawKeywords(symbol.keywords.join(", "));
      setRawBlockPhrases(symbol.block_phrases.join(", "));
    } else {
      setEditingSymbol(null);
      setFormData({
        symbol: "",
        future: "",
        sector: "",
        require_uppercase: false,
        is_active: true
      });
      setRawKeywords("");
      setRawBlockPhrases("");
    }
    setIsModalOpen(true);
  };

  const saveSymbol = async () => {
    const method = editingSymbol ? "PUT" : "POST";
    const url = editingSymbol 
      ? `http://localhost:8000/api/admin/symbols/${editingSymbol.symbol}`
      : `http://localhost:8000/api/admin/symbols`;

    const payload = {
      ...formData,
      keywords: rawKeywords.split(",").map(k => k.trim()).filter(Boolean),
      block_phrases: rawBlockPhrases.split(",").map(k => k.trim()).filter(Boolean)
    };

    try {
      const res = await fetch(url, {
        method,
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": apiKey
        },
        body: JSON.stringify(payload)
      });
      
      if (res.ok) {
        setIsModalOpen(false);
        fetchSymbols(apiKey);
      } else {
        alert("Failed to save symbol");
      }
    } catch (e) {
      console.error(e);
    }
  };

  const toggleActive = async (symbol: TrackedSymbol) => {
    try {
      await fetch(`http://localhost:8000/api/admin/symbols/${symbol.symbol}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
        body: JSON.stringify({ ...symbol, is_active: !symbol.is_active })
      });
      fetchSymbols(apiKey);
    } catch (e) {
      console.error(e);
    }
  };

  const deleteSymbol = async (symbolStr: string) => {
    if (!confirm("Are you sure you want to disable this symbol?")) return;
    try {
      await fetch(`http://localhost:8000/api/admin/symbols/${symbolStr}`, {
        method: "DELETE",
        headers: { "X-API-Key": apiKey },
      });
      fetchSymbols(apiKey);
    } catch (e) {
      console.error(e);
    }
  };

  if (loading) {
    return <div className="flex items-center justify-center h-64 text-slate-400">Loading...</div>;
  }

  if (!isAuthenticated) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[70vh]">
        <div className="bg-slate-900/60 p-8 rounded-2xl border border-white/10 shadow-2xl w-full max-w-md backdrop-blur-xl">
          <div className="flex flex-col items-center gap-4 mb-8 text-center">
            <div className="p-4 bg-indigo-500/10 rounded-full text-indigo-400">
              <ShieldAlert className="w-12 h-12" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-white mb-2">Admin Access</h1>
              <p className="text-sm text-slate-400">Please enter the master API key to configure the tracking engine.</p>
            </div>
          </div>
          <form onSubmit={handleLogin} className="flex flex-col gap-4">
            <input
              type="password"
              placeholder="Enter API Key"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className="w-full bg-slate-950 border border-white/10 rounded-xl px-4 py-3 text-white focus:outline-none focus:ring-2 focus:ring-indigo-500/50"
            />
            <button
              type="submit"
              className="w-full bg-indigo-500 hover:bg-indigo-600 text-white font-semibold py-3 rounded-xl transition-colors shadow-lg shadow-indigo-500/20"
            >
              Authenticate
            </button>
            <Link href="/" className="text-center text-sm text-slate-500 mt-4 hover:text-white transition-colors">
              Return to Dashboard
            </Link>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      <header className="flex items-center justify-between">
        <div>
          <Link href="/" className="inline-flex items-center gap-2 text-indigo-400 hover:text-indigo-300 text-sm font-medium mb-4 transition-colors">
            <ArrowLeft className="w-4 h-4" /> Back to Dashboard
          </Link>
          <h1 className="text-3xl font-bold text-white">Tracking Engine Configuration</h1>
          <p className="text-slate-400 mt-2">Manage the symbols, keywords, and futures the producers actively ingest.</p>
        </div>
        <div className="flex items-center gap-3">
          <a
            href="http://localhost:3001"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-2 bg-slate-800 hover:bg-slate-700 text-slate-300 px-4 py-2.5 rounded-xl font-semibold transition-colors border border-white/5"
            title="Open Grafana Dashboards"
          >
            <Activity className="w-4 h-4" /> Grafana
          </a>
          <button
            onClick={() => {
              localStorage.removeItem("ADMIN_API_KEY");
              setIsAuthenticated(false);
              setApiKey("");
            }}
            className="flex items-center gap-2 bg-slate-800 hover:bg-slate-700 text-slate-300 px-4 py-2.5 rounded-xl font-semibold transition-colors border border-white/5"
            title="Log out of Admin Panel"
          >
            <PowerOff className="w-4 h-4" /> Log Out
          </button>
          <button
            onClick={() => openModal()}
            className="flex items-center gap-2 bg-indigo-500 hover:bg-indigo-600 text-white px-5 py-2.5 rounded-xl font-semibold transition-all shadow-lg shadow-indigo-500/20"
          >
            <Plus className="w-5 h-5" /> Add Symbol
          </button>
        </div>
      </header>

      <div className="bg-slate-900/60 rounded-2xl border border-white/10 overflow-hidden backdrop-blur-xl">
        <table className="w-full text-left text-sm text-slate-300">
          <thead className="bg-slate-950/50 text-slate-400 text-xs uppercase tracking-wider font-semibold border-b border-white/5">
            <tr>
              <th className="px-6 py-4">Symbol</th>
              <th className="px-6 py-4">Status</th>
              <th className="px-6 py-4">Sector / Future</th>
              <th className="px-6 py-4">Keywords</th>
              <th className="px-6 py-4 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/5">
            {symbols.map((s) => (
              <tr key={s.symbol} className="hover:bg-white/[0.02] transition-colors">
                <td className="px-6 py-4">
                  <div className="font-bold text-white text-base">{s.symbol}</div>
                  {s.require_uppercase && <div className="text-[10px] text-amber-500 mt-1 uppercase tracking-wider font-semibold">Strict Case</div>}
                </td>
                <td className="px-6 py-4">
                  <span className={cn(
                    "px-2.5 py-1 text-xs font-semibold rounded-md border",
                    s.is_active ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20" : "bg-slate-800 text-slate-400 border-white/5"
                  )}>
                    {s.is_active ? "Tracking Active" : "Paused"}
                  </span>
                </td>
                <td className="px-6 py-4 text-slate-400">
                  <div className="flex flex-col gap-1 text-xs">
                    <div>Sector: <span className="text-slate-200">{s.sector || "N/A"}</span></div>
                    <div>Future: <span className="text-slate-200">{s.future || "N/A"}</span></div>
                  </div>
                </td>
                <td className="px-6 py-4">
                  <div className="flex flex-wrap gap-1.5">
                    {s.keywords.slice(0, 3).map((kw, i) => (
                      <span key={i} className="px-2 py-0.5 bg-indigo-500/10 text-indigo-300 rounded text-xs border border-indigo-500/20">{kw}</span>
                    ))}
                    {s.keywords.length > 3 && (
                      <span className="px-2 py-0.5 bg-slate-800 text-slate-400 rounded text-xs border border-white/5">+{s.keywords.length - 3}</span>
                    )}
                  </div>
                </td>
                <td className="px-6 py-4">
                  <div className="flex items-center justify-end gap-2">
                    <button 
                      onClick={() => toggleActive(s)}
                      className={cn("p-2 rounded-lg transition-colors", s.is_active ? "text-amber-400 hover:bg-amber-400/10" : "text-emerald-400 hover:bg-emerald-400/10")}
                      title={s.is_active ? "Pause Ingestion" : "Resume Ingestion"}
                    >
                      {s.is_active ? <PowerOff className="w-4 h-4" /> : <Power className="w-4 h-4" />}
                    </button>
                    <button 
                      onClick={() => openModal(s)}
                      className="p-2 text-sky-400 hover:bg-sky-400/10 rounded-lg transition-colors"
                      title="Edit Configuration"
                    >
                      <Edit2 className="w-4 h-4" />
                    </button>
                    <button 
                      onClick={() => deleteSymbol(s.symbol)}
                      className="p-2 text-rose-400 hover:bg-rose-400/10 rounded-lg transition-colors"
                      title="Soft Delete"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="bg-slate-900/60 rounded-2xl border border-white/10 overflow-hidden backdrop-blur-xl mt-8">
        <div className="p-4 border-b border-white/5 bg-slate-950/50">
          <h2 className="text-lg font-bold text-white flex items-center gap-2">
            <Activity className="w-5 h-5 text-indigo-400" /> System Metrics & Health
          </h2>
        </div>
        <iframe 
          src="http://localhost:3001/d/system-health/system-health?orgId=1&theme=dark&kiosk=tv" 
          width="100%" 
          height="800" 
          className="border-none bg-slate-950"
        />
      </div>

      {isModalOpen && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 z-50">
          <div className="bg-slate-900 border border-white/10 rounded-2xl w-full max-w-xl shadow-2xl overflow-hidden">
            <div className="flex items-center justify-between p-6 border-b border-white/5">
              <h2 className="text-xl font-bold text-white">
                {editingSymbol ? `Edit ${editingSymbol.symbol}` : "Add New Symbol"}
              </h2>
              <button onClick={() => setIsModalOpen(false)} className="text-slate-400 hover:text-white transition-colors">
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="p-6 space-y-5 max-h-[70vh] overflow-y-auto">
              <div>
                <label className="block text-xs font-semibold text-slate-400 mb-1.5 uppercase tracking-wider">Ticker Symbol</label>
                <input 
                  type="text" 
                  value={formData.symbol || ""}
                  onChange={(e) => setFormData({...formData, symbol: e.target.value.toUpperCase()})}
                  disabled={!!editingSymbol}
                  placeholder="e.g. TSLA"
                  className="w-full bg-slate-950 border border-white/10 rounded-lg px-4 py-2.5 text-white focus:ring-2 focus:ring-indigo-500/50 disabled:opacity-50"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-semibold text-slate-400 mb-1.5 uppercase tracking-wider">Sector ETF</label>
                  <input 
                    type="text" 
                    value={formData.sector || ""}
                    onChange={(e) => setFormData({...formData, sector: e.target.value.toUpperCase()})}
                    placeholder="e.g. XLY"
                    className="w-full bg-slate-950 border border-white/10 rounded-lg px-4 py-2.5 text-white focus:ring-2 focus:ring-indigo-500/50"
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-slate-400 mb-1.5 uppercase tracking-wider">Futures Ticker</label>
                  <input 
                    type="text" 
                    value={formData.future || ""}
                    onChange={(e) => setFormData({...formData, future: e.target.value.toUpperCase()})}
                    placeholder="e.g. NQ=F"
                    className="w-full bg-slate-950 border border-white/10 rounded-lg px-4 py-2.5 text-white focus:ring-2 focus:ring-indigo-500/50"
                  />
                </div>
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-400 mb-1.5 uppercase tracking-wider">Keywords (comma separated)</label>
                <textarea 
                  value={rawKeywords}
                  onChange={(e) => setRawKeywords(e.target.value)}
                  placeholder="Tesla, Elon Musk, Cybertruck"
                  rows={2}
                  className="w-full bg-slate-950 border border-white/10 rounded-lg px-4 py-2.5 text-white focus:ring-2 focus:ring-indigo-500/50"
                />
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-400 mb-1.5 uppercase tracking-wider">Block Phrases (comma separated)</label>
                <textarea 
                  value={rawBlockPhrases}
                  onChange={(e) => setRawBlockPhrases(e.target.value)}
                  placeholder="e.g. Nikola Tesla, Tesla Coil"
                  rows={2}
                  className="w-full bg-slate-950 border border-white/10 rounded-lg px-4 py-2.5 text-white focus:ring-2 focus:ring-indigo-500/50"
                />
              </div>
              <div className="flex items-center gap-3">
                <input 
                  type="checkbox" 
                  id="require_uppercase"
                  checked={formData.require_uppercase || false}
                  onChange={(e) => setFormData({...formData, require_uppercase: e.target.checked})}
                  className="w-4 h-4 rounded border-white/10 bg-slate-950 text-indigo-500 focus:ring-indigo-500/50"
                />
                <label htmlFor="require_uppercase" className="text-sm font-medium text-slate-300">
                  Require Strict Uppercase Ticker Match
                </label>
              </div>
            </div>
            <div className="p-6 border-t border-white/5 bg-slate-950/50 flex justify-end gap-3">
              <button 
                onClick={() => setIsModalOpen(false)}
                className="px-5 py-2.5 text-slate-400 hover:text-white font-semibold transition-colors"
              >
                Cancel
              </button>
              <button 
                onClick={saveSymbol}
                className="flex items-center gap-2 bg-indigo-500 hover:bg-indigo-600 text-white px-6 py-2.5 rounded-xl font-semibold transition-colors"
              >
                <Save className="w-4 h-4" /> Save Configuration
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
