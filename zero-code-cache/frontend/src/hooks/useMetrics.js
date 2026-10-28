import { useState, useCallback } from 'react';

export function useMetrics() {
  const [metrics, setMetrics] = useState({
    dbQueries: 0,
    cacheHits: 0,
    hitRate: 0,
    recentQueries: [],
  });

  const refresh = useCallback(() => {
    fetch('/api/metrics')
      .then(r => r.json())
      .then(setMetrics)
      .catch(() => {});
  }, []);

  return { metrics, refresh };
}
