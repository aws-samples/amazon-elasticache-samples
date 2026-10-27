import React from 'react';

function PerformanceDashboard({ metrics, cacheEnabled, onReset, timing }) {
  const queries = metrics.recentQueries || [];

  return (
    <div>
      <div className="flex items-center justify-between mb-5">
        <h2 className="text-sm font-semibold text-gray-900 uppercase tracking-wider flex items-center gap-2">
          <span className="text-base">⚡</span> Performance
        </h2>
        <button onClick={onReset} className="text-xs text-gray-400 hover:text-indigo-600 transition-colors font-medium">Reset</button>
      </div>

      {/* Cache */}
      <div className="mb-4 p-3 bg-gray-50 rounded-xl border border-gray-100">
        <div className="flex items-center justify-between">
          <div>
            <span className="text-sm font-medium text-gray-700 block">Cache</span>
            <span className="text-xs text-gray-400">{cacheEnabled ? 'JDBC Wrapper + Valkey' : 'Direct PostgreSQL'}</span>
          </div>
          <span className={`px-2.5 py-1 rounded-lg text-xs font-bold ${cacheEnabled ? 'bg-emerald-100 text-emerald-700' : 'bg-gray-200 text-gray-500'}`}>
            {cacheEnabled ? 'ON' : 'OFF'}
          </span>
        </div>
      </div>

      {/* Breakdown */}
      {timing && timing.totalMs > 0 && (
        <div className="mb-4 p-3 bg-gray-50 rounded-xl border border-gray-100">
          <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Response Breakdown</p>
          <Bar label="Total Round-Trip" ms={timing.totalMs} color="bg-indigo-500" />
          {timing.bedrockMs > 0 && <Bar label="Bedrock Agent" ms={timing.bedrockMs} color="bg-amber-500" />}
          <Bar label="JDBC Query" ms={timing.dbMs} color="bg-violet-500" />
          <Bar label="Network + Overhead" ms={Math.max(0, timing.bedrockMs > 0 ? timing.totalMs - timing.serverMs : timing.totalMs - timing.dbMs)} color="bg-gray-300" />
        </div>
      )}

      {/* Stats */}
      <div className="grid grid-cols-2 gap-3 mb-4">
        <div className="p-3 bg-red-50 rounded-xl border border-red-100 text-center">
          <p className="text-xs text-red-400 mb-1">🔴 DB Queries</p>
          <p className="text-2xl font-bold text-red-600">{metrics.dbQueries}</p>
        </div>
        <div className="p-3 bg-emerald-50 rounded-xl border border-emerald-100 text-center">
          <p className="text-xs text-emerald-400 mb-1">🟢 Cache Hits</p>
          <p className="text-2xl font-bold text-emerald-600">{metrics.cacheHits}</p>
        </div>
      </div>

      {/* Hit Rate */}
      {(metrics.dbQueries + metrics.cacheHits) > 0 && (
        <div className="mb-4 p-3 bg-gray-50 rounded-xl border border-gray-100">
          <div className="flex justify-between text-xs mb-1.5">
            <span className="text-gray-500">Cache Hit Rate</span>
            <span className={`font-bold ${metrics.hitRate > 50 ? 'text-emerald-600' : 'text-gray-400'}`}>{metrics.hitRate}%</span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2">
            <div className="bg-emerald-500 h-2 rounded-full transition-all duration-700" style={{ width: `${metrics.hitRate}%` }} />
          </div>
        </div>
      )}

      {/* Feed */}
      <div>
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Live Query Feed</h3>
        <div className="space-y-1 max-h-60 overflow-y-auto">
          {queries.map((q, i) => (
            <div key={`${q.timestamp}-${i}`}
              className={`flex items-center gap-2 text-xs py-2 px-3 rounded-lg ${i === 0 ? 'bg-indigo-50 border border-indigo-100' : 'bg-gray-50'}`}>
              <span className="flex-shrink-0">{q.cacheHit ? '🟢' : '🔴'}</span>
              <span className="text-gray-500 truncate flex-1 font-mono">{q.query}</span>
              <span className={`whitespace-nowrap font-mono font-bold ${q.cacheHit ? 'text-emerald-600' : 'text-gray-400'}`}>{q.durationMs}ms</span>
            </div>
          ))}
          {queries.length === 0 && <p className="text-xs text-gray-400 text-center py-6">No queries yet.</p>}
        </div>
      </div>
    </div>
  );
}

function Bar({ label, ms, color }) {
  return (
    <div className="mb-2">
      <div className="flex justify-between text-xs mb-1">
        <span className="text-gray-500">{label}</span>
        <span className="font-mono font-medium text-gray-700">{ms}ms</span>
      </div>
      <div className="w-full bg-gray-200 rounded-full h-1.5">
        <div className={`h-1.5 rounded-full transition-all duration-500 ${color}`} style={{ width: `${Math.min((ms / 5000) * 100, 100)}%` }} />
      </div>
    </div>
  );
}

export default PerformanceDashboard;
