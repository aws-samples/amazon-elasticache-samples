import { useState, useEffect } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { 
  Key,
  Loader2,
  Search,
  Edit,
  Save,
  X,
  ChevronLeft,
  RefreshCw,
  ChevronRight,
  Trash2
} from 'lucide-react';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { valkeyApi, type PageCache } from '@/services/valkeyApi';
import { TypedEditor } from '@/components/editors/TypedEditor';
import { generateRedisCommands, deepEqual } from '@/utils/redisCommands';

// Simplified in-memory cache for key types
interface KeyTypeCache {
  [key: string]: {
    type: string;
    lastFetched: number;
  };
}

class SimpleKeyTypeManager {
  private cache: KeyTypeCache = {};
  private readonly CACHE_TTL = 300000; // 5 minutes

  // Get cached type or fetch if needed
  async getKeyType(key: string): Promise<string> {
    const cached = this.cache[key];
    
    // Return cached type if it's fresh
    if (cached && (Date.now() - cached.lastFetched) < this.CACHE_TTL) {
      return cached.type;
    }

    // Fetch type directly with timeout
    try {
      console.log(`🔍 Fetching type for key: "${key}"`);
      const startTime = Date.now();
      
      const result = await Promise.race([
        valkeyApi.getKeyType(key),
        new Promise<{ type: string; error?: string }>((_, reject) => 
          setTimeout(() => reject(new Error('Type detection timeout')), 5000)
        )
      ]);
      
      const duration = Date.now() - startTime;
      const type = result.type || 'unknown';
      
      console.log(`✅ Type detection for "${key}": ${type} (${duration}ms)`);
      
      if (result.error) {
        console.warn(`⚠️ Type detection warning for "${key}":`, result.error);
      }

      // Cache the result
      this.cache[key] = {
        type,
        lastFetched: Date.now()
      };

      return type;
      
    } catch (error) {
      console.error(`❌ Failed to get type for key "${key}":`, error);
      
      // Cache as unknown to avoid repeated failures
      this.cache[key] = {
        type: 'unknown',
        lastFetched: Date.now()
      };
      
      return 'unknown';
    }
  }

  // Get cached type without fetching (for immediate UI updates)
  getCachedType(key: string): string {
    const cached = this.cache[key];
    if (cached && (Date.now() - cached.lastFetched) < this.CACHE_TTL) {
      return cached.type;
    }
    return 'unknown';
  }

  // Batch fetch types for multiple keys (simplified)
  async batchFetchTypes(keys: string[]): Promise<void> {
    console.log(`🔄 Batch fetching types for ${keys.length} keys`);
    
    // Filter keys that need type information
    const keysToFetch = keys.filter(key => {
      const cached = this.cache[key];
      return !cached || (Date.now() - cached.lastFetched) >= this.CACHE_TTL;
    });
    
    if (keysToFetch.length === 0) {
      console.log('✅ All key types are already cached');
      return;
    }

    console.log(`📦 Fetching types for ${keysToFetch.length} keys`);
    
    // Process in smaller batches with limited concurrency
    const BATCH_SIZE = 10;
    for (let i = 0; i < keysToFetch.length; i += BATCH_SIZE) {
      const batch = keysToFetch.slice(i, i + BATCH_SIZE);
      
      // Process batch with Promise.allSettled to handle individual failures
      const results = await Promise.allSettled(
        batch.map(key => this.getKeyType(key))
      );
      
      console.log(`✅ Processed batch ${Math.floor(i / BATCH_SIZE) + 1}: ${results.length} keys`);
      
      // Small delay between batches to avoid overwhelming the server
      if (i + BATCH_SIZE < keysToFetch.length) {
        await new Promise(resolve => setTimeout(resolve, 100));
      }
    }
    
    console.log(`✅ Batch fetch completed for ${keysToFetch.length} keys`);
  }

  // Clear cache
  clear() {
    this.cache = {};
    console.log('🗑️ Key type cache cleared');
  }

  // Get cache stats
  getStats() {
    const cacheSize = Object.keys(this.cache).length;
    const freshEntries = Object.values(this.cache).filter(
      entry => (Date.now() - entry.lastFetched) < this.CACHE_TTL
    ).length;
    
    return {
      cacheSize,
      freshEntries,
      staleEntries: cacheSize - freshEntries
    };
  }
}

// Create global cache instance
const keyTypeManager = new SimpleKeyTypeManager();

export function CLI() {
  // Pagination state
  const [keyPattern] = useState('*');
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [pageCache, setPageCache] = useState<PageCache>({});
  const [, setNextCursor] = useState('0'); // Cursor for the next page
  const [hasNextPage, setHasNextPage] = useState(true);
  const [isComplete, setIsComplete] = useState(false);
  
  // Loading states
  const [loadingPage, setLoadingPage] = useState<number | null>(null);
  const [initialLoad, setInitialLoad] = useState(true);
  
  // Edit state
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [keyValues, setKeyValues] = useState<{[key: string]: any}>({});
  const [committedValues, setCommittedValues] = useState<{[key: string]: any}>({});
  const [savingKeys, setSavingKeys] = useState<Set<string>>(new Set());
  const [editingKeyType, setEditingKeyType] = useState<string>('string');
  const [loadingKeyType, setLoadingKeyType] = useState<boolean>(false);
  
  // TTL state
  const [keyTTLs, setKeyTTLs] = useState<{[key: string]: number}>({});
  const [loadingTTL] = useState<boolean>(false);
  const [editingTTL, setEditingTTL] = useState<boolean>(false);
  const [editTTLValue, setEditTTLValue] = useState<string>('');
  const [savingTTL, setSavingTTL] = useState<boolean>(false);
  
  // Cache for key metadata to avoid repeated API calls
  const [keyCache, setKeyCache] = useState<{[key: string]: {
    value: any;
    type: string;
    ttl: number;
    lastFetched: number;
  }}>({});
  
  
  // Search and filter state
  const [searchQuery, setSearchQuery] = useState('');
  const [lastSearchPattern, setLastSearchPattern] = useState('*');

  // Auto-load first page on component mount
  useEffect(() => {
    loadPage(1);
  }, []); // Empty dependency array means this runs once on mount

  // Reset to first page when search pattern changes
  useEffect(() => {
    const currentPattern = searchQuery.trim() || '*';
    if (currentPattern !== lastSearchPattern) {
      setLastSearchPattern(currentPattern);
      setCurrentPage(1);
      setPageCache({});
      setNextCursor('0');
      setHasNextPage(true);
      setIsComplete(false);
      keyTypeManager.clear();
      loadPage(1);
    }
  }, [searchQuery, lastSearchPattern]);

  // Note: Removed background type fetching - now done synchronously during page load

  // Load a specific page using pagination
  const loadPage = async (pageNum: number) => {
    // Prevent concurrent loading, but allow if it's the same page being requested
    if (loadingPage !== null && loadingPage !== pageNum) {
      console.log(`🚫 Already loading page ${loadingPage}, skipping request for page ${pageNum}`);
      return; 
    }
    
    // Check if page is already cached
    if (pageCache[pageNum]) {
      console.log(`📋 Using cached data for page ${pageNum}`);
      setCurrentPage(pageNum);
      setInitialLoad(false);
      return;
    }
    
    // Set loading state with timeout protection
    setLoadingPage(pageNum);
    
    // Add timeout protection to prevent stuck loading state
    const timeoutId = setTimeout(() => {
      console.warn(`⚠️ Page ${pageNum} loading timed out, clearing loading state`);
      setLoadingPage(null);
    }, 30000); // 30 second timeout
    
    const startTime = Date.now();
    const pattern = searchQuery.trim() || keyPattern;
    
    try {
      // Determine cursor to use based on page
      let cursor = '0';
      if (pageNum === 1) {
        cursor = '0'; // Always start from 0 for first page
      } else {
        // For subsequent pages, use the nextCursor from previous page
        const prevPageData = pageCache[pageNum - 1];
        if (prevPageData && prevPageData.nextCursor) {
          cursor = prevPageData.nextCursor;
        } else {
          // If we don't have the previous page cached, we can't load this page
          console.error(`❌ Cannot load page ${pageNum}: missing cursor from page ${pageNum - 1}`);
          setHasNextPage(false);
          setIsComplete(true);
          setLoadingPage(null); // Reset loading state before early return
          return;
        }
      }
      
      console.log(`🔍 Loading page ${pageNum} with cursor: ${cursor}`);
      
      const result = await valkeyApi.getPaginatedKeys(pattern, cursor, pageSize);
      
      if (result && result.keys && Array.isArray(result.keys)) {
        const scanTime = Date.now() - startTime;
        
        // Handle backend returning more keys than requested by slicing to pageSize
        let keysToDisplay = result.keys;
        let backendReturnedExtra = false;
        
        if (result.keys.length > pageSize) {
          console.log(`⚠️ Backend returned ${result.keys.length} keys but only ${pageSize} were requested - slicing to correct size`);
          keysToDisplay = result.keys.slice(0, pageSize);
          backendReturnedExtra = true;
        }
        
        // Wait for type detection to complete BEFORE showing keys to avoid "unknown" types
        if (keysToDisplay.length > 0) {
          console.log(`🔄 Fetching types for ${keysToDisplay.length} keys before displaying page ${pageNum}`);
          const typeStartTime = Date.now();
          await keyTypeManager.batchFetchTypes(keysToDisplay);
          const typeDuration = Date.now() - typeStartTime;
          console.log(`✅ Type detection completed for page ${pageNum} in ${typeDuration}ms`);
        }
        
        // Cache the page data with cursor information using sliced keys
        const pageData = {
          keys: keysToDisplay,  // Use sliced keys for display
          cursor: cursor,           // The cursor used to fetch this page
          nextCursor: result.cursor, // The cursor returned for the next page
          timestamp: Date.now(),
          complete: result.complete,
          backendReturnedExtra: backendReturnedExtra // Track if backend returned extra keys
        };
        
        setPageCache(prev => ({
          ...prev,
          [pageNum]: pageData
        }));
        
        console.log(`💾 Cached page ${pageNum} with cursor data:`, {
          inputCursor: cursor,
          outputCursor: result.cursor,
          keysCount: result.keys.length,
          complete: result.complete
        });
        
        // Store the next cursor for navigation
        setNextCursor(result.cursor);
        
        // Analyze if we have more pages available (fixed pagination logic)
        const gotNoKeys = result.keys.length === 0;
        const cursorIsZero = result.cursor === '0';
        
        // For pagination logic, consider what we got from backend vs what we're displaying
        const backendReturnedAtLeastPageSize = result.keys.length >= pageSize;
        const displayingFullPage = keysToDisplay.length === pageSize;
        
        // Debug pagination response
        console.log(`🔍 Pagination analysis for page ${pageNum}:`, {
          rawKeysReceived: result.keys.length,
          keysToDisplay: keysToDisplay.length,
          pageSize: pageSize,
          returnedCursor: result.cursor,
          cursorIsZero: cursorIsZero,
          backendComplete: result.complete,
          gotNoKeys: gotNoKeys,
          backendReturnedAtLeastPageSize: backendReturnedAtLeastPageSize,
          displayingFullPage: displayingFullPage,
          backendReturnedExtra: backendReturnedExtra
        });
        
        // Improved pagination logic that handles backend returning extra keys:
        // We definitely DON'T have a next page if:
        // - We got no keys at all, OR
        // - The backend explicitly says it's complete AND cursor is '0', OR
        // - Backend returned less than requested (partial page, even if we're displaying full page due to slicing)
        const definitelyNoMore = gotNoKeys || 
                                (result.complete === true && cursorIsZero) ||
                                (result.keys.length < pageSize && !backendReturnedExtra);
        
        // We likely HAVE a next page if:
        // - Backend returned at least a full page of results AND
        // - The returned cursor is not '0' (there's a valid next cursor) AND
        // - Backend didn't explicitly say it's complete
        const likelyHasMore = backendReturnedAtLeastPageSize && 
                             !cursorIsZero && 
                             result.complete !== true;
        
        // Set pagination state with logic that works with sliced results
        const shouldHaveNext = !definitelyNoMore && likelyHasMore;
        const scanComplete = definitelyNoMore;
        
        console.log(`📊 Pagination decision for page ${pageNum}:`, {
          definitelyNoMore: definitelyNoMore,
          likelyHasMore: likelyHasMore,
          shouldHaveNext: shouldHaveNext,
          scanComplete: scanComplete
        });
        
        setHasNextPage(shouldHaveNext);
        setIsComplete(scanComplete);
        setCurrentPage(pageNum);
        setInitialLoad(false);
        
        console.log(`✅ Page ${pageNum} loaded: Found ${result.count} keys (${scanTime}ms)`, {
          cursor,
          nextCursor: result.cursor,
          hasNext: shouldHaveNext,
          complete: scanComplete
        });
        
      } else {
        console.warn(`⚠️ Page ${pageNum} returned no data`);
        setHasNextPage(false);
        setIsComplete(true);
      }
      
    } catch (error) {
      console.error(`❌ Error loading page ${pageNum}:`, error);
      
      // Handle specific errors
      if (error instanceof Error) {
        if (error.message.includes('503')) {
          alert('Valkey connection lost. Please check your connection settings.');
        } else if (error.message.includes('500')) {
          alert('Pagination failed. This may be due to network issues or invalid pattern syntax.');
        }
      }
      
      setHasNextPage(false);
      setIsComplete(true);
    } finally {
      // Clear timeout and reset loading state
      clearTimeout(timeoutId);
      setLoadingPage(null);
    }
  };

  // Refresh all pages (clear cache and reload current page)
  const refreshPages = async () => {
    // Force clear any stuck loading state
    setLoadingPage(null);
    
    setPageCache({});
    setNextCursor('0');
    setHasNextPage(true);
    setIsComplete(false);
    keyTypeManager.clear();
    setCurrentPage(1);
    
    console.log('🔄 Refreshing all pages and clearing stuck states');
    await loadPage(1);
  };

  // Rebuild cursor chain from page 1 to target page
  const rebuildCursorChain = async (targetPage: number): Promise<boolean> => {
    console.log(`🔄 Rebuilding cursor chain to page ${targetPage}`);
    
    const pattern = searchQuery.trim() || keyPattern;
    let cursor = '0';
    
    try {
      // Load pages sequentially from 1 to targetPage
      for (let page = 1; page <= targetPage; page++) {
        if (pageCache[page]) {
          // Use cached cursor for next iteration
          cursor = pageCache[page].nextCursor || '0';
          console.log(`📋 Using cached page ${page}, cursor: ${cursor}`);
          continue;
        }
        
        console.log(`🔍 Loading page ${page} with cursor: ${cursor}`);
        const result = await valkeyApi.getPaginatedKeys(pattern, cursor, pageSize);
        
        if (!result || !result.keys || !Array.isArray(result.keys)) {
          console.error(`❌ Failed to rebuild cursor chain at page ${page}`);
          return false;
        }
        
        // Wait for type detection for consistency (optional for rebuild, but ensures cache consistency)
        if (result.keys.length > 0) {
          await keyTypeManager.batchFetchTypes(result.keys);
        }
        
        // Cache the page data
        setPageCache(prev => ({
          ...prev,
          [page]: {
            keys: result.keys,
            cursor: cursor,
            nextCursor: result.cursor,
            timestamp: Date.now(),
            complete: result.complete
          }
        }));
        
        cursor = result.cursor;
        
        // Check if we've reached the end
        if (cursor === '0' || result.complete) {
          console.log(`✅ Reached end of data at page ${page}`);
          setHasNextPage(false);
          setIsComplete(true);
          if (page < targetPage) {
            console.warn(`⚠️ Cannot reach target page ${targetPage}, data ends at page ${page}`);
            return false;
          }
          break;
        }
      }
      
      console.log(`✅ Successfully rebuilt cursor chain to page ${targetPage}`);
      return true;
      
    } catch (error) {
      console.error(`❌ Error rebuilding cursor chain:`, error);
      return false;
    }
  };

  // Navigate to previous page (use cached data or load if needed)
  const goToPreviousPage = async () => {
    if (currentPage <= 1) {
      console.log('🚫 Already on first page, cannot go to previous page');
      return;
    }

    const targetPage = currentPage - 1;
    
    // If previous page is cached, use it immediately
    if (pageCache[targetPage]) {
      const cachedPageData = pageCache[targetPage];
      
      // Restore full pagination state from cached data
      setCurrentPage(targetPage);
      setNextCursor(cachedPageData.nextCursor || '0');
      
      // Recalculate pagination state based on cached data and available pages
      const hasNextPageCached = pageCache[targetPage + 1] !== undefined;
      const cachedDataSuggestsMore = Boolean(
        cachedPageData.nextCursor && 
        cachedPageData.nextCursor !== '0' && 
        cachedPageData.complete !== true
      );
      const shouldHaveNext = hasNextPageCached || cachedDataSuggestsMore;
      
      setHasNextPage(shouldHaveNext);
      const isPageComplete = Boolean(
        cachedPageData.complete === true && 
        (cachedPageData.nextCursor ?? '') === '0'
      );
      setIsComplete(isPageComplete);
      
      console.log(`📋 Navigating to cached page ${targetPage} with restored state:`, {
        nextCursor: cachedPageData.nextCursor,
        hasNextPage: shouldHaveNext,
        isComplete: isPageComplete,
        keysCount: cachedPageData.keys.length
      });
      return;
    }

    // Previous page not cached - need to rebuild cursor chain
    console.log(`🔄 Previous page ${targetPage} not cached, rebuilding cursor chain...`);
    
    // Set loading state with timeout protection
    setLoadingPage(targetPage);
    
    // Add timeout protection to prevent stuck loading state
    const timeoutId = setTimeout(() => {
      console.warn(`⚠️ Previous page navigation timed out, clearing loading state`);
      setLoadingPage(null);
    }, 30000); // 30 second timeout
    
    try {
      const success = await Promise.race([
        rebuildCursorChain(targetPage),
        new Promise<boolean>((_, reject) => 
          setTimeout(() => reject(new Error('Previous page navigation timeout')), 25000)
        )
      ]);
      
      clearTimeout(timeoutId);
      
      if (success) {
        setCurrentPage(targetPage);
        console.log(`✅ Successfully navigated to rebuilt page ${targetPage}`);
      } else {
        console.warn(`⚠️ Failed to rebuild cursor chain to page ${targetPage}, staying on current page`);
        // Don't reset to page 1, just stay on current page
      }
      
    } catch (error) {
      clearTimeout(timeoutId);
      console.error(`❌ Error during previous page navigation:`, error);
      
      // Show user-friendly error message
      if (error instanceof Error && error.message.includes('timeout')) {
        alert('Previous page navigation timed out. Please try again or refresh the page.');
      } else {
        alert('Failed to navigate to previous page. Please try refreshing the page.');
      }
      
      // Don't automatically reset to page 1, let user decide
      
    } finally {
      // Always reset loading state - this is critical
      setLoadingPage(null);
    }
  };

  // Navigate to next page
  const goToNextPage = async () => {
    if (hasNextPage) {
      await loadPage(currentPage + 1);
    }
  };

  // Get badge color and text for Valkey data type
  const getValueTypeBadge = (type: string) => {
    const normalizedType = type.toLowerCase();
    
    const typeConfig: { [key: string]: { text: string, color: string } } = {
      'string': { text: 'string', color: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200' },
      'json': { text: 'json', color: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200' },
      'rejson-rl': { text: 'json', color: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200' },
      'rejson': { text: 'json', color: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200' },
      'hash': { text: 'hash', color: 'bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200' },
      'list': { text: 'list', color: 'bg-pink-100 text-pink-800 dark:bg-pink-900 dark:text-pink-200' },
      'set': { text: 'set', color: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200' },
      'zset': { text: 'zset', color: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200' },
      'stream': { text: 'stream', color: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-200' },
      'bloomfltr': { text: 'bloom filter', color: 'bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200' },
      'none': { text: 'none', color: 'bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200' },
      'unknown': { text: 'unknown', color: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200' }
    };
    
    return typeConfig[normalizedType] || { text: 'unknown', color: 'bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200' };
  };

  // Format TTL for display
  const formatTTL = (ttlSeconds: number): string => {
    if (ttlSeconds === -1) {
      return 'No Expiry';
    }
    if (ttlSeconds === -2) {
      return 'Expired';
    }
    if (ttlSeconds <= 0) {
      return 'Invalid TTL';
    }

    const days = Math.floor(ttlSeconds / 86400);
    const hours = Math.floor((ttlSeconds % 86400) / 3600);
    const minutes = Math.floor((ttlSeconds % 3600) / 60);
    const seconds = ttlSeconds % 60;

    const parts = [];
    if (days > 0) parts.push(`${days}d`);
    if (hours > 0) parts.push(`${hours}h`);
    if (minutes > 0) parts.push(`${minutes}m`);
    if (seconds > 0 || parts.length === 0) parts.push(`${seconds}s`);

    return parts.join(' ');
  };

  // Key editing functions - Simplified and optimized for performance
  const startEditing = async (keyname: string) => {
    if (editingKey === keyname) return;
    
    setEditingKey(keyname);
    
    // Check if we have cached data that's still fresh (60 seconds)
    const now = Date.now();
    const cachedData = keyCache[keyname];
    const isCacheValid = cachedData && (now - cachedData.lastFetched) < 60000; // 1 minute TTL
    
    if (isCacheValid) {
      // Use cached data immediately for instant loading
      console.log(`⚡ Using cached data for instant loading: "${keyname}"`);
      
      // Batch state updates for better performance
      setEditingKeyType(cachedData.type);
      setKeyValues(prev => ({ ...prev, [keyname]: cachedData.value }));
      setCommittedValues(prev => ({ ...prev, [keyname]: cachedData.value }));
      setKeyTTLs(prev => ({ ...prev, [keyname]: cachedData.ttl }));
      
      return; // No background refresh needed for cached data
    }
    
    // Show minimal loading state
    setLoadingKeyType(true);
    
    try {
      console.log(`🔄 Fetching key data: "${keyname}"`);
      
      // Get the correct type from keyTypeManager (already cached from table display)
      const correctType = keyTypeManager.getCachedType(keyname);
      console.log(`🔍 Using cached type from keyTypeManager: "${correctType}"`);
      
      // Fetch the key value (but don't trust its data_type field)
      const keyData = await valkeyApi.getCacheKey(keyname);
      
      if (keyData && keyData.found) {
        // Use the correct type from keyTypeManager, not from getCacheKey
        const actualType = correctType !== 'unknown' ? correctType : (keyData.data_type || 'string');
        const value = keyData.value !== undefined ? keyData.value : '';
        
        console.log(`✅ Loaded key "${keyname}": type=${actualType} (corrected from getCacheKey type: ${keyData.data_type})`);
        
        // Batch all state updates together
        setEditingKeyType(actualType);
        setKeyValues(prev => ({ ...prev, [keyname]: value }));
        setCommittedValues(prev => ({ ...prev, [keyname]: value }));
        setKeyTTLs(prev => ({ ...prev, [keyname]: -1 })); // Default TTL, will load separately if needed
        
        // Update cache
        setKeyCache(prev => ({
          ...prev,
          [keyname]: {
            value: value,
            type: actualType,
            ttl: -1,
            lastFetched: now
          }
        }));
        
        // Load TTL in background (non-blocking)
        loadTTLInBackground(keyname);
        
      } else {
        console.warn(`⚠️ Key "${keyname}" not found or empty`);
        
        // Use the correct type from keyTypeManager even if key is not found
        const fallbackType = correctType !== 'unknown' ? correctType : 'string';
        setEditingKeyType(fallbackType);
        setKeyValues(prev => ({ ...prev, [keyname]: '' }));
        setCommittedValues(prev => ({ ...prev, [keyname]: '' }));
        setKeyTTLs(prev => ({ ...prev, [keyname]: -1 }));
      }
      
    } catch (error) {
      console.error(`❌ Error loading key "${keyname}":`, error);
      
      // Use the correct type from keyTypeManager even in error case
      const correctType = keyTypeManager.getCachedType(keyname);
      const fallbackType = correctType !== 'unknown' ? correctType : 'string';
      setEditingKeyType(fallbackType);
      setKeyValues(prev => ({ ...prev, [keyname]: '' }));
      setCommittedValues(prev => ({ ...prev, [keyname]: '' }));
      setKeyTTLs(prev => ({ ...prev, [keyname]: -1 }));
      
    } finally {
      setLoadingKeyType(false);
    }
  };
  
  // Background TTL loading (non-blocking)
  const loadTTLInBackground = async (keyname: string) => {
    try {
      const ttlResult = await valkeyApi.executeRedisCommand(`TTL ${keyname}`);
      if (ttlResult.success && ttlResult.stdout) {
        const ttlValue = parseInt(ttlResult.stdout.trim());
        setKeyTTLs(prev => ({ ...prev, [keyname]: ttlValue }));
        
        // Update cache with TTL
        setKeyCache(prev => ({
          ...prev,
          [keyname]: {
            ...prev[keyname],
            ttl: ttlValue
          }
        }));
      }
    } catch (error) {
      console.warn(`⚠️ Background TTL fetch failed for "${keyname}":`, error);
    }
  };

  const cancelEditing = () => {
    setEditingKey(null);
    setEditingTTL(false);
    setEditTTLValue('');
  };

  const saveKeyValue = async (keyname: string) => {
    setSavingKeys(prev => new Set(prev).add(keyname));
    
    try {
      const newValue = keyValues[keyname];
      const oldValue = committedValues[keyname];
      const dataType = editingKeyType;
      
      // Check if the value has actually changed
      if (deepEqual(oldValue, newValue)) {
        console.log(`⏭️ No changes detected for "${keyname}", skipping save`);
        setEditingKey(null);
        return;
      }
      
      console.log(`🔄 Saving changes for key "${keyname}" (type: ${dataType})`, {
        oldValue,
        newValue
      });
      
      if (dataType === 'string') {
        // Use existing setCacheKey for strings (backward compatibility)
        await valkeyApi.setCacheKey(keyname, newValue);
        console.log(`✅ String saved using setCacheKey`);
      } else {
        // Use new Valkey-specific approach for complex data types
        const commands = generateRedisCommands(keyname, dataType, oldValue, newValue);
        
        console.log(`📋 Generated ${commands.length} Valkey command(s):`, commands);
        
        // Execute all commands sequentially with enhanced debugging
        for (let i = 0; i < commands.length; i++) {
          const command = commands[i];
          console.log(`⚡ Executing command ${i + 1}/${commands.length}: ${command}`);
          console.log(`📊 Command details:`, {
            command: command,
            length: command.length,
            keyname: keyname,
            dataType: dataType
          });
          
          const result = await valkeyApi.executeRedisCommand(command);
          
          console.log(`📋 Command result:`, result);
          
          if (!result.success) {
            console.error(`❌ Valkey command failed:`, {
              command: command,
              result: result,
              stdout: result.stdout,
              stderr: result.stderr,
              message: result.message,
              return_code: result.return_code
            });
            throw new Error(`Valkey command failed: ${command}\nReturn Code: ${result.return_code}\nStdout: ${result.stdout}\nStderr: ${result.stderr || result.message}`);
          }
          
          console.log(`✅ Command ${i + 1}/${commands.length} executed successfully:`, {
            stdout: result.stdout,
            execution_time: result.execution_time
          });
        }
        
        console.log(`🎉 All ${commands.length} Valkey command(s) executed successfully for "${keyname}"`);
        
        // Verify the data was actually saved by re-reading from Valkey
        console.log(`🔍 Verifying data persistence for key "${keyname}"...`);
        try {
          const verificationResult = await valkeyApi.getCacheKey(keyname);
          console.log(`📊 Verification result:`, verificationResult);
          
          if (verificationResult && verificationResult.found) {
            console.log(`✅ Data verification successful - key "${keyname}" was saved to Valkey`);
            console.log(`📋 Saved value:`, verificationResult.value);
            
            // Compare what we saved vs what we got back
            if (!deepEqual(verificationResult.value, newValue)) {
              console.warn(`⚠️ WARNING: Saved value differs from expected value for "${keyname}"`, {
                expected: newValue,
                actual: verificationResult.value
              });
            }
          } else {
            console.error(`❌ Data verification failed - key "${keyname}" was not found in Valkey after save!`);
          }
        } catch (verifyError) {
          console.error(`❌ Error during data verification for "${keyname}":`, verifyError);
        }
      }
      
      // Update committed state to the new value
      setCommittedValues(prev => ({ ...prev, [keyname]: newValue }));
      
      // Update cache with the new saved value
      setKeyCache(prev => ({
        ...prev,
        [keyname]: {
          value: newValue,
          type: dataType,
          ttl: keyTTLs[keyname] || -1,
          lastFetched: Date.now()
        }
      }));
      
      console.log(`✅ Successfully saved key "${keyname}" with type: ${dataType}`);
      
      setEditingKey(null);
      
    } catch (error) {
      console.error(`❌ Error saving key "${keyname}":`, error);
      
      // Show user-friendly error message
      alert(`Failed to save key "${keyname}": ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setSavingKeys(prev => {
        const newSet = new Set(prev);
        newSet.delete(keyname);
        return newSet;
      });
    }
  };

  const saveTTL = async (keyname: string) => {
    setSavingTTL(true);
    
    try {
      const ttlValue = editTTLValue.trim();
      let command = '';
      
      // Parse TTL input
      if (ttlValue === '' || ttlValue === '0' || ttlValue === '-1') {
        // Remove expiry (PERSIST command)
        command = `PERSIST ${keyname}`;
      } else {
        // Set expiry (EXPIRE command)
        const seconds = parseInt(ttlValue);
        if (isNaN(seconds) || seconds < 0) {
          throw new Error('TTL must be a positive number (seconds), 0 for no expiry, or -1 to remove expiry');
        }
        command = `EXPIRE ${keyname} ${seconds}`;
      }
      
      console.log(`🔄 Executing TTL command: ${command}`);
      
      const result = await valkeyApi.executeRedisCommand(command);
      
      if (!result.success) {
        throw new Error(`TTL command failed: ${result.stderr || result.message}`);
      }
      
      // Refresh TTL value
      const ttlResult = await valkeyApi.executeRedisCommand(`TTL ${keyname}`);
      if (ttlResult.success && ttlResult.stdout) {
        const newTTL = parseInt(ttlResult.stdout.trim());
        setKeyTTLs(prev => ({ ...prev, [keyname]: newTTL }));
      }
      
      // Reset editing state
      setEditingTTL(false);
      setEditTTLValue('');
      
      console.log(`✅ TTL successfully updated for key "${keyname}"`);
      
    } catch (error) {
      console.error(`❌ Error saving TTL for key "${keyname}":`, error);
      alert(`Failed to save TTL for key "${keyname}": ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setSavingTTL(false);
    }
  };

  const startEditingTTL = (keyname: string) => {
    const currentTTL = keyTTLs[keyname];
    if (currentTTL === -1) {
      setEditTTLValue(''); // No expiry
    } else {
      setEditTTLValue(currentTTL.toString());
    }
    setEditingTTL(true);
  };

  const cancelEditingTTL = () => {
    setEditingTTL(false);
    setEditTTLValue('');
  };

  const deleteKey = async (keyname: string) => {
    if (!confirm(`Are you sure you want to delete key "${keyname}"?`)) return;
    
    try {
      await valkeyApi.deleteCacheKey(keyname);
      
      // Remove from all cache state
      setKeyValues(prev => {
        const newValues = { ...prev };
        delete newValues[keyname];
        return newValues;
      });
      setCommittedValues(prev => {
        const newValues = { ...prev };
        delete newValues[keyname];
        return newValues;
      });
      setKeyTTLs(prev => {
        const newValues = { ...prev };
        delete newValues[keyname];
        return newValues;
      });
      setKeyCache(prev => {
        const newCache = { ...prev };
        delete newCache[keyname];
        return newCache;
      });
      
      // Refresh current page to remove the deleted key from the display
      await loadPage(currentPage);
      
      console.log(`✅ Successfully deleted key "${keyname}" and refreshed page`);
      
    } catch (error) {
      console.error('Error deleting key:', error);
    }
  };

  // Get current page keys from cache
  const getCurrentPageKeys = () => {
    const currentPageData = pageCache[currentPage];
    return currentPageData ? currentPageData.keys : [];
  };

  const currentPageKeys = getCurrentPageKeys();
  const totalKeysLoaded = Object.values(pageCache).reduce((sum, page) => sum + page.keys.length, 0);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Key Browser</h1>
          <p className="text-muted-foreground">
            Browse and edit Valkey keys with pagination
          </p>
        </div>
        <div className="flex items-center space-x-2">
          <Button variant="outline" size="sm" onClick={refreshPages} disabled={loadingPage !== null}>
            {loadingPage !== null ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Loading...
              </>
            ) : (
              <>
                <RefreshCw className="mr-2 h-4 w-4" />
                Refresh
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Keys Table */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center">
            <Key className="mr-2 h-5 w-5" />
            Keys (Page {currentPage})
          </CardTitle>
          <CardDescription>
            Browse and edit Valkey keys using cursor-based pagination
          </CardDescription>
        </CardHeader>
        <CardContent>
          {/* Search and Filter Controls */}
          <div className="flex items-center space-x-4 p-3 bg-muted/20 rounded-lg border mb-4">
            <div className="flex items-center space-x-2 flex-1">
              <Search className="h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search keys (user:*, session:*, etc.)"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="h-8"
              />
            </div>
            <div className="flex items-center space-x-2">
              <label className="text-sm font-medium">Per page:</label>
              <Select 
                value={pageSize.toString()} 
                onValueChange={(value) => {
                  setPageSize(parseInt(value));
                  // Clear cache and reload page 1 when page size changes
                  setPageCache({});
                  setNextCursor('0');
                  setCurrentPage(1);
                  loadPage(1);
                }}
              >
                <SelectTrigger className="w-16 h-8">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="10">10</SelectItem>
                  <SelectItem value="25">25</SelectItem>
                  <SelectItem value="50">50</SelectItem>
                  <SelectItem value="100">100</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          {/* Results Summary */}
          <div className="flex items-center justify-between text-sm text-muted-foreground mb-4">
            <span>
              {initialLoad ? 'Loading keys...' : 
                `Page ${currentPage} • ${currentPageKeys.length} keys • ${totalKeysLoaded} total loaded${isComplete ? ' (complete)' : ' (more available)'}`
              }
            </span>
            <div className="flex items-center space-x-2">
              {loadingPage && (
                <span className="text-blue-600">
                  <Loader2 className="h-3 w-3 animate-spin inline mr-1" />
                  Loading page {loadingPage}...
                </span>
              )}
            </div>
          </div>

          {/* Table or Empty State */}
          {initialLoad || currentPageKeys.length === 0 ? (
            <div className="rounded-lg border border-dashed border-muted-foreground/25 p-12">
              <div className="flex flex-col items-center justify-center text-center space-y-4">
                <div className="flex items-center justify-center w-16 h-16 rounded-full bg-muted/50">
                  <Key className="h-8 w-8 text-muted-foreground" />
                </div>
                <div className="space-y-2">
                  <h3 className="text-lg font-semibold text-foreground">
                    {initialLoad ? 'Loading Keys...' : 'No Keys Found'}
                  </h3>
                  <p className="text-muted-foreground max-w-md">
                    {initialLoad 
                      ? 'Loading the first page of keys using pagination...' 
                      : 'No keys match the current search pattern.'
                    }
                  </p>
                </div>
                {!initialLoad && (
                  <Button onClick={refreshPages} className="mt-4">
                    <RefreshCw className="mr-2 h-4 w-4" />
                    Refresh
                  </Button>
                )}
              </div>
            </div>
          ) : (
            <>
              <div className="rounded-lg border">
                <ScrollArea className="h-96 w-full">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead className="w-[50%]">Key Name</TableHead>
                        <TableHead className="w-[20%]">Type</TableHead>
                        <TableHead className="w-[30%]">Actions</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {currentPageKeys.map((keyname: string) => {
                        const keyType = keyTypeManager.getCachedType(keyname);
                        const typeBadge = getValueTypeBadge(keyType);
                        return (
                          <TableRow key={keyname} className="hover:bg-muted/50">
                            <TableCell className="font-mono text-sm">
                              <div className="flex items-center space-x-2 max-w-full">
                                <span 
                                  className="text-green-600 font-medium truncate"
                                  title={keyname}
                                >
                                  "{keyname}"
                                </span>
                              </div>
                            </TableCell>
                            <TableCell className="text-sm">
                              {keyType === 'loading' ? (
                                <div className="flex items-center">
                                  <div className="w-12 h-5 bg-gray-200 rounded animate-pulse"></div>
                                </div>
                              ) : (
                                <span className={`inline-flex items-center px-2 py-1 rounded text-xs font-medium ${typeBadge.color}`}>
                                  {typeBadge.text}
                                  {keyType === 'unknown' && (
                                    <span className="ml-1" title="Type detection failed - check console for details">
                                      ⚠️
                                    </span>
                                  )}
                                </span>
                              )}
                            </TableCell>
                            <TableCell>
                              <div className="flex items-center space-x-1">
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => startEditing(keyname)}
                                  className="h-8 px-2"
                                  title="Edit key value"
                                >
                                  <Edit className="h-4 w-4" />
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => deleteKey(keyname)}
                                  className="h-8 px-2 text-red-600 hover:text-red-700 hover:bg-red-100"
                                  title="Delete key"
                                >
                                  <Trash2 className="h-4 w-4" />
                                </Button>
                              </div>
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </ScrollArea>
              </div>

              {/* Pagination Controls */}
              <div className="flex items-center justify-between mt-4">
                <div className="text-sm text-muted-foreground">
                  Page {currentPage} • {currentPageKeys.length} keys on this page
                </div>
                <div className="flex items-center space-x-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={goToPreviousPage}
                    disabled={currentPage === 1 || loadingPage !== null}
                  >
                    <ChevronLeft className="h-4 w-4" />
                    Previous
                  </Button>
                  <span className="text-sm text-muted-foreground px-2">
                    Page {currentPage}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={goToNextPage}
                    disabled={!hasNextPage || loadingPage !== null}
                  >
                    Next
                    <ChevronRight className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            </>
          )}

          {/* Edit Modal */}
          {editingKey && (
            <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
              <Card className="w-[90vw] max-w-2xl max-h-[80vh] overflow-hidden">
                <CardHeader>
                  <CardTitle className="flex items-center justify-between">
                    <div className="flex-1 min-w-0 pr-4">
                      <div className="truncate mb-2" title={editingKey}>
                        <span className="text-base font-semibold">Edit Key:</span>
                        <span className="ml-2 font-mono text-sm text-muted-foreground">{editingKey}</span>
                      </div>
                      <div className="flex items-center space-x-2 flex-wrap">
                        {loadingKeyType || loadingTTL ? (
                          <div className="flex items-center space-x-2">
                            <Loader2 className="h-4 w-4 animate-spin text-blue-500" />
                            <span className="text-xs text-muted-foreground">Loading metadata...</span>
                          </div>
                        ) : (
                          <>
                            <span className={`inline-flex items-center px-2 py-1 rounded text-xs font-medium ${getValueTypeBadge(editingKeyType).color}`}>
                              {getValueTypeBadge(editingKeyType).text}
                            </span>
                            {editingKey && (
                              editingTTL ? (
                                <div className="flex items-center space-x-2">
                                  <Input
                                    value={editTTLValue}
                                    onChange={(e) => setEditTTLValue(e.target.value)}
                                    placeholder="TTL in seconds (0 = no expiry)"
                                    className="h-6 w-32 text-xs"
                                  />
                                  <Button
                                    size="sm"
                                    onClick={() => saveTTL(editingKey)}
                                    disabled={savingTTL}
                                    className="h-6 px-2 text-xs"
                                  >
                                    {savingTTL ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={cancelEditingTTL}
                                    disabled={savingTTL}
                                    className="h-6 px-2 text-xs"
                                  >
                                    <X className="h-3 w-3" />
                                  </Button>
                                </div>
                              ) : loadingTTL ? (
                                <span className="inline-flex items-center px-2 py-1 rounded text-xs font-medium bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400">
                                  <Loader2 className="h-3 w-3 animate-spin mr-1" />
                                  TTL: Loading...
                                </span>
                              ) : keyTTLs[editingKey] !== undefined ? (
                                <span 
                                  className="inline-flex items-center px-2 py-1 rounded text-xs font-medium bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200 cursor-pointer hover:bg-amber-200 dark:hover:bg-amber-800"
                                  onClick={() => startEditingTTL(editingKey)}
                                  title="Click to edit TTL"
                                >
                                  TTL: {formatTTL(keyTTLs[editingKey])}
                                </span>
                              ) : (
                                <span 
                                  className="inline-flex items-center px-2 py-1 rounded text-xs font-medium bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400 cursor-pointer hover:bg-gray-200 dark:hover:bg-gray-700"
                                  onClick={() => startEditingTTL(editingKey)}
                                  title="TTL data unavailable - Click to set TTL"
                                >
                                  TTL: Unknown
                                </span>
                              )
                            )}
                          </>
                        )}
                      </div>
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={cancelEditing}
                      className="h-8 w-8 p-0 flex-shrink-0"
                    >
                      <X className="h-4 w-4" />
                    </Button>
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="space-y-4">
                    {loadingKeyType ? (
                      <div className="flex items-center justify-center py-8">
                        <Loader2 className="h-8 w-8 animate-spin text-blue-500" />
                        <span className="ml-2 text-muted-foreground">Loading key data...</span>
                      </div>
                    ) : (
                      <>
                        <TypedEditor
                          value={keyValues[editingKey]}
                          onChange={(newValue) => setKeyValues(prev => ({ ...prev, [editingKey]: newValue }))}
                          redisType={editingKeyType}
                          disabled={savingKeys.has(editingKey)}
                          keyName={editingKey}
                        />
                        <div className="flex items-center justify-between">
                          <div className="text-xs text-muted-foreground">
                            💡 Use Ctrl+Enter to save, Escape to cancel • Click TTL badge to edit expiry
                          </div>
                          <div className="text-xs text-muted-foreground font-medium">
                            {keyValues[editingKey] ? String(keyValues[editingKey]).length : 0} characters
                          </div>
                        </div>
                        <div className="flex items-center space-x-2 justify-end">
                          <Button
                            variant="outline"
                            onClick={cancelEditing}
                          >
                            Cancel
                          </Button>
                          <Button
                            onClick={() => saveKeyValue(editingKey)}
                            disabled={savingKeys.has(editingKey)}
                          >
                            {savingKeys.has(editingKey) ? (
                              <>
                                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                                Saving...
                              </>
                            ) : (
                              <>
                                <Save className="h-4 w-4 mr-2" />
                                Save
                              </>
                            )}
                          </Button>
                        </div>
                      </>
                    )}
                  </div>
                </CardContent>
              </Card>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
