import { useEffect, useRef, useCallback, useState } from 'react';

export interface LoadStage<T> {
  id: string;
  load: () => Promise<T>;
  priority: number;
  delayMs?: number;
  dependsOn?: string[];
  onComplete?: (data: T) => void;
  onError?: (error: Error) => void;
}

interface StageState<T> {
  status: 'pending' | 'loading' | 'complete' | 'error';
  data?: T;
  error?: Error;
  startTime?: number;
  endTime?: number;
}

interface UseStaggeredDataLoaderOptions {
  autoStart?: boolean;
  concurrency?: number;
}

export function useStaggeredDataLoader<T extends Record<string, any>>(
  stages: LoadStage<any>[],
  options: UseStaggeredDataLoaderOptions = {}
) {
  const { autoStart = true, concurrency = 2 } = options;
  
  const [stageStates, setStageStates] = useState<Record<string, StageState<any>>>(() => {
    const initial: Record<string, StageState<any>> = {};
    stages.forEach(stage => {
      initial[stage.id] = { status: 'pending' };
    });
    return initial;
  });
  
  const [isLoading, setIsLoading] = useState(false);
  const [completedCount, setCompletedCount] = useState(0);
  const activeLoadsRef = useRef<Set<string>>(new Set());
  const abortControllerRef = useRef<AbortController | null>(null);

  const updateStageState = useCallback((id: string, update: Partial<StageState<any>>) => {
    setStageStates(prev => ({
      ...prev,
      [id]: { ...prev[id], ...update }
    }));
  }, []);

  const canLoadStage = useCallback((stage: LoadStage<any>, states: Record<string, StageState<any>>) => {
    // Check if dependencies are complete
    if (stage.dependsOn) {
      for (const dep of stage.dependsOn) {
        if (states[dep]?.status !== 'complete') {
          return false;
        }
      }
    }
    return true;
  }, []);

  const loadStage = useCallback(async (stage: LoadStage<any>) => {
    const startTime = Date.now();
    updateStageState(stage.id, { status: 'loading', startTime });
    activeLoadsRef.current.add(stage.id);

    try {
      // Apply delay if specified
      if (stage.delayMs) {
        await new Promise(resolve => setTimeout(resolve, stage.delayMs));
      }

      const data = await stage.load();
      
      if (abortControllerRef.current?.signal.aborted) {
        return;
      }

      updateStageState(stage.id, { 
        status: 'complete', 
        data,
        endTime: Date.now()
      });
      
      setCompletedCount(prev => prev + 1);
      
      if (stage.onComplete) {
        stage.onComplete(data);
      }
    } catch (error) {
      if (abortControllerRef.current?.signal.aborted) {
        return;
      }

      const err = error instanceof Error ? error : new Error(String(error));
      updateStageState(stage.id, { 
        status: 'error', 
        error: err,
        endTime: Date.now()
      });
      
      if (stage.onError) {
        stage.onError(err);
      }
    } finally {
      activeLoadsRef.current.delete(stage.id);
    }
  }, [updateStageState]);

  const processQueue = useCallback(async () => {
    if (abortControllerRef.current?.signal.aborted) {
      return;
    }

    // Sort stages by priority (lower number = higher priority)
    const sortedStages = [...stages].sort((a, b) => a.priority - b.priority);
    
    for (const stage of sortedStages) {
      if (abortControllerRef.current?.signal.aborted) {
        break;
      }

      const currentState = stageStates[stage.id];
      
      // Skip if already processed or loading
      if (currentState.status !== 'pending') {
        continue;
      }

      // Check dependencies
      if (!canLoadStage(stage, stageStates)) {
        continue;
      }

      // Check concurrency limit
      if (activeLoadsRef.current.size >= concurrency) {
        // Wait for a slot to open
        await new Promise(resolve => setTimeout(resolve, 100));
        continue;
      }

      // Start loading this stage
      loadStage(stage);
    }

    // Check if there are more stages to process
    const hasPending = Object.values(stageStates).some(state => state.status === 'pending');
    const hasActive = activeLoadsRef.current.size > 0;

    if (hasPending || hasActive) {
      // Continue processing after a short delay
      setTimeout(() => processQueue(), 100);
    } else {
      setIsLoading(false);
    }
  }, [stages, stageStates, canLoadStage, loadStage, concurrency]);

  const start = useCallback(() => {
    abortControllerRef.current = new AbortController();
    setIsLoading(true);
    processQueue();
  }, [processQueue]);

  const reset = useCallback(() => {
    // Abort any ongoing operations
    abortControllerRef.current?.abort();
    activeLoadsRef.current.clear();
    
    // Reset all states
    const resetStates: Record<string, StageState<any>> = {};
    stages.forEach(stage => {
      resetStates[stage.id] = { status: 'pending' };
    });
    setStageStates(resetStates);
    setCompletedCount(0);
    setIsLoading(false);
  }, [stages]);

  const retry = useCallback((stageId: string) => {
    updateStageState(stageId, { status: 'pending', error: undefined });
    if (!isLoading) {
      start();
    }
  }, [updateStageState, isLoading, start]);

  // Auto-start if enabled
  useEffect(() => {
    if (autoStart) {
      start();
    }
    
    return () => {
      abortControllerRef.current?.abort();
    };
  }, []); // Only run on mount

  // Compute aggregate data
  const data = Object.entries(stageStates).reduce((acc, [id, state]) => {
    if (state.status === 'complete' && state.data !== undefined) {
      (acc as any)[id] = state.data;
    }
    return acc;
  }, {} as T);

  const hasData = Object.keys(data).length > 0;
  const progress = stages.length > 0 ? (completedCount / stages.length) * 100 : 0;
  const hasErrors = Object.values(stageStates).some(state => state.status === 'error');

  return {
    data,
    stageStates,
    isLoading,
    hasData,
    hasErrors,
    progress,
    completedCount,
    totalStages: stages.length,
    start,
    reset,
    retry
  };
}
