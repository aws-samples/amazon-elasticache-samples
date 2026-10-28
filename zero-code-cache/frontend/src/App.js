import React, { useState, useEffect, useCallback } from 'react';
import SearchBar from './components/SearchBar';
import ProductGrid from './components/ProductGrid';
import PerformanceDashboard from './components/PerformanceDashboard';
import CategoryNav from './components/CategoryNav';
import { useMetrics } from './hooks/useMetrics';

function App() {
  const [products, setProducts] = useState([]);
  const [categories, setCategories] = useState([]);
  const [activeCategory, setActiveCategory] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [cacheEnabled, setCacheEnabled] = useState(false);
  const [loading, setLoading] = useState(false);
  const [timing, setTiming] = useState(null);
  const [agentResponse, setAgentResponse] = useState(null);
  const [agentToolCalls, setAgentToolCalls] = useState([]);
  const { metrics, refresh: refreshMetrics } = useMetrics();

  useEffect(() => {
    fetch('/api/products/categories').then(r => r.json()).then(setCategories).catch(() => {});
    fetch('/api/cache/status').then(r => r.json()).then(data => setCacheEnabled(data.enabled)).catch(() => {});
  }, []);

  const loadByCategory = useCallback(async (category) => {
    setActiveCategory(category);
    setSearchQuery('');
    setAgentResponse(null);
    setAgentToolCalls([]);
    setLoading(true);
    try {
      const start = performance.now();
      const res = await fetch(`/api/products/category/${encodeURIComponent(category)}`);
      const data = await res.json();
      const totalMs = Math.round(performance.now() - start);
      setProducts(data.data || []);
      setTiming({ totalMs, serverMs: data.serverTimeMs || 0, bedrockMs: 0, dbMs: data.dbTimeMs || 0 });
    } catch (e) {}
    setLoading(false);
    refreshMetrics();
  }, [refreshMetrics]);

  const handleSearch = async (query) => {
    setSearchQuery(query);
    setActiveCategory(null);
    setAgentResponse(null);
    setAgentToolCalls([]);
    setProducts([]);
    setLoading(true);
    try {
      const start = performance.now();
      const res = await fetch('/api/agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: query }),
      });
      const data = await res.json();
      const totalMs = Math.round(performance.now() - start);
      setAgentResponse(data.text);
      setAgentToolCalls(data.toolCalls || []);
      setTiming({ totalMs, serverMs: data.totalTimeMs || 0, bedrockMs: data.bedrockTimeMs || 0, dbMs: data.toolTimeMs || 0 });
    } catch (e) {
      setAgentResponse('Sorry, I had trouble processing that request.');
    }
    setLoading(false);
    refreshMetrics();
  };

  const resetMetrics = () => {
    fetch('/api/metrics/reset', { method: 'POST' }).then(() => refreshMetrics()).catch(() => {});
    setAgentResponse(null);
    setAgentToolCalls([]);
    setTiming(null);
    setProducts([]);
    setActiveCategory(null);
    setSearchQuery('');
  };

  return (
    <div className="h-screen w-screen overflow-hidden flex flex-col">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 flex-shrink-0 shadow-sm">
        <div className="px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 bg-indigo-600 rounded-xl flex items-center justify-center text-white text-sm font-bold">S</div>
            <h1 className="text-lg font-semibold text-gray-900">ShopMind</h1>
            <span className="text-xs bg-indigo-50 text-indigo-600 px-2 py-0.5 rounded-md font-semibold tracking-wide uppercase">Agentic AI</span>
          </div>
          <SearchBar onSearch={handleSearch} placeholder="Ask AI: 'find me a gaming laptop under $1500'" />
          <div className="text-sm text-gray-400">Demo User</div>
        </div>
      </header>

      <div className="flex flex-1 min-h-0">
        {/* Main */}
        <main className="flex-1 p-5 overflow-y-auto bg-gray-50">
          <CategoryNav categories={categories} active={activeCategory} onSelect={loadByCategory} />

          {agentResponse && (
            <div className="mb-4 rounded-lg border border-indigo-200 bg-indigo-50/50 overflow-hidden">
              <div className="px-4 py-2.5 border-b border-indigo-200/60 flex items-center gap-2">
                <div className="w-5 h-5 bg-indigo-600 rounded flex items-center justify-center">
                  <span className="text-white text-[9px] font-bold">AI</span>
                </div>
                <span className="text-xs font-semibold text-indigo-700">Agent Response</span>
                {agentToolCalls.length > 0 && (
                  <span className="text-[10px] bg-indigo-200/60 text-indigo-600 px-1.5 py-0.5 rounded font-medium">{agentToolCalls.length} tool calls</span>
                )}
              </div>
              <div className="px-4 py-3">
                <p className="text-sm text-gray-700 whitespace-pre-wrap leading-relaxed">{agentResponse}</p>
              </div>
              {agentToolCalls.length > 0 && (
                <div className="px-4 py-2.5 bg-white/60 border-t border-indigo-100">
                  <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider mb-1.5">JDBC Queries via Tool Calls</p>
                  {agentToolCalls.map((tc, i) => (
                    <div key={i} className="text-[11px] text-gray-500 font-mono py-0.5">
                      <span className="text-indigo-500">→</span> {tc.toolName}({Object.entries(tc.input || {}).map(([k,v]) => `${k}="${v}"`).join(', ')}) <span className="text-gray-300">—</span> <span className="font-semibold text-gray-700">{tc.durationMs}ms</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {loading && (
            <div className="flex items-center justify-center py-16">
              <div className="animate-spin rounded-full h-6 w-6 border-2 border-indigo-600 border-t-transparent"></div>
              <span className="ml-2.5 text-gray-400 text-sm">Agent is reasoning...</span>
            </div>
          )}

          {!loading && products.length > 0 && (
            <section>
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-gray-800">
                  {searchQuery ? `Results for "${searchQuery}"` : activeCategory || 'Products'}
                </h2>
                <span className="text-[10px] text-gray-400 bg-gray-100 px-2 py-0.5 rounded-full">{products.length} items</span>
              </div>
              <ProductGrid products={products} />
            </section>
          )}

          {!loading && products.length === 0 && !agentResponse && (
            <div className="text-center py-20">
              <div className="text-4xl mb-3">🔍</div>
              <p className="text-sm text-gray-500 mb-1">Ask the AI agent or pick a category</p>
              <p className="text-xs text-gray-400">Try "find me a gaming laptop under $1500"</p>
            </div>
          )}
        </main>

        {/* Sidebar */}
        <aside className="w-80 flex-shrink-0 bg-white border-l border-gray-200 p-5 overflow-y-auto">
          <PerformanceDashboard metrics={metrics} cacheEnabled={cacheEnabled} onReset={resetMetrics} timing={timing} />
        </aside>
      </div>
    </div>
  );
}

export default App;
