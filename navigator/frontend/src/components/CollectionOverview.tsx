import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { 
  Clock, 
  CheckCircle, 
  XCircle, 
  AlertTriangle,
  RefreshCw,
  Activity,
  Zap,
  TrendingUp
} from 'lucide-react';
import type { CollectionInfo, MetricCollectionState, CollectionPerformanceMetrics } from '@/types';

interface CollectionOverviewProps {
  collectionInfo: CollectionInfo | null;
  categoryStates: MetricCollectionState;
  performanceMetrics?: CollectionPerformanceMetrics | null;
  isLoading: boolean;
  onRetryAll?: () => void;
  onRetryFailed?: () => void;
  partialResults?: boolean;
}

export function CollectionOverview({
  collectionInfo,
  categoryStates,
  performanceMetrics,
  isLoading,
  onRetryAll,
  onRetryFailed,
  partialResults = false
}: CollectionOverviewProps) {
  if (!collectionInfo && !isLoading) {
    return null;
  }

  // Calculate current state statistics
  const totalCategories = Object.keys(categoryStates).length;
  const completedCategories = Object.values(categoryStates).filter(s => s.status === 'complete').length;
  const failedCategories = Object.values(categoryStates).filter(s => s.status === 'failed' || s.status === 'timeout').length;
  const loadingCategories = Object.values(categoryStates).filter(s => s.status === 'loading').length;
  const progressPercentage = (completedCategories / totalCategories) * 100;

  // Success rate calculation
  const successRate = totalCategories > 0 ? (completedCategories / totalCategories) * 100 : 0;
  const hasFailures = failedCategories > 0;
  const isComplete = completedCategories === totalCategories && loadingCategories === 0;

  return (
    <div className="space-y-4">
      {/* Main Progress Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="flex items-center space-x-2">
                <Activity className="h-5 w-5" />
                <span>Metrics Collection Status</span>
              </CardTitle>
              <CardDescription>
                {collectionInfo?.method === 'parallel' ? 'Parallel' : 'Sequential'} collection of 7 metric categories
              </CardDescription>
            </div>
            <div className="flex items-center space-x-2">
              {collectionInfo && (
                <Badge variant="outline" className="font-mono">
                  {collectionInfo.total_duration_seconds.toFixed(2)}s
                </Badge>
              )}
              {partialResults && (
                <Badge variant="outline" className="text-orange-600">
                  Partial Results
                </Badge>
              )}
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Progress Bar */}
          <div className="space-y-2">
            <div className="flex justify-between text-sm">
              <span>Progress</span>
              <span>{completedCategories}/{totalCategories} categories</span>
            </div>
            <Progress 
              value={isLoading && progressPercentage === 0 ? undefined : progressPercentage} 
              className="h-2" 
            />
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>0%</span>
              <span>100%</span>
            </div>
          </div>

          {/* Status Summary */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="text-center">
              <div className="flex items-center justify-center space-x-1 text-green-600">
                <CheckCircle className="h-4 w-4" />
                <span className="text-2xl font-bold">{completedCategories}</span>
              </div>
              <p className="text-xs text-muted-foreground">Completed</p>
            </div>
            
            <div className="text-center">
              <div className="flex items-center justify-center space-x-1 text-blue-600">
                <Clock className="h-4 w-4" />
                <span className="text-2xl font-bold">{loadingCategories}</span>
              </div>
              <p className="text-xs text-muted-foreground">Loading</p>
            </div>
            
            <div className="text-center">
              <div className="flex items-center justify-center space-x-1 text-red-600">
                <XCircle className="h-4 w-4" />
                <span className="text-2xl font-bold">{failedCategories}</span>
              </div>
              <p className="text-xs text-muted-foreground">Failed</p>
            </div>
            
            <div className="text-center">
              <div className="flex items-center justify-center space-x-1 text-purple-600">
                <TrendingUp className="h-4 w-4" />
                <span className="text-2xl font-bold">{successRate.toFixed(0)}%</span>
              </div>
              <p className="text-xs text-muted-foreground">Success Rate</p>
            </div>
          </div>

          {/* Performance Metrics */}
          {performanceMetrics && (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 pt-4 border-t">
              <div className="text-center">
                <div className="text-lg font-semibold text-blue-600">
                  {performanceMetrics.totalCollections}
                </div>
                <p className="text-xs text-muted-foreground">Total Collections</p>
              </div>
              <div className="text-center">
                <div className="text-lg font-semibold text-green-600">
                  {performanceMetrics.averageCollectionTime.toFixed(2)}s
                </div>
                <p className="text-xs text-muted-foreground">Average Duration</p>
              </div>
              <div className="text-center">
                <div className="text-lg font-semibold text-purple-600">
                  {collectionInfo ? 
                    (collectionInfo.total_duration_seconds < performanceMetrics.averageCollectionTime ? '↑' : '↓') + 
                    Math.abs(((collectionInfo.total_duration_seconds - performanceMetrics.averageCollectionTime) / performanceMetrics.averageCollectionTime) * 100).toFixed(0) + '%'
                    : 'N/A'
                  }
                </div>
                <p className="text-xs text-muted-foreground">vs Average</p>
              </div>
            </div>
          )}

          {/* Action Buttons */}
          <div className="flex flex-wrap gap-2 pt-4 border-t">
            {onRetryAll && (
              <Button 
                variant="outline" 
                size="sm" 
                onClick={onRetryAll}
                disabled={isLoading}
              >
                <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
                Refresh All
              </Button>
            )}
            
            {onRetryFailed && hasFailures && (
              <Button 
                variant="outline" 
                size="sm" 
                onClick={onRetryFailed}
                disabled={isLoading}
                className="text-red-600 border-red-200 hover:bg-red-50"
              >
                <XCircle className="h-4 w-4 mr-2" />
                Retry Failed ({failedCategories})
              </Button>
            )}
          </div>

          {/* Alerts */}
          {hasFailures && !isLoading && (
            <Alert className="border-orange-200 bg-orange-50">
              <AlertTriangle className="h-4 w-4 text-orange-600" />
              <AlertDescription className="text-orange-700">
                {failedCategories} metric {failedCategories === 1 ? 'category' : 'categories'} failed to collect. 
                {partialResults ? ' Displaying partial results.' : ' Some data may be incomplete.'}
              </AlertDescription>
            </Alert>
          )}
          
          {isComplete && !hasFailures && (
            <Alert className="border-green-200 bg-green-50">
              <CheckCircle className="h-4 w-4 text-green-600" />
              <AlertDescription className="text-green-700">
                All metrics collected successfully in {collectionInfo?.total_duration_seconds.toFixed(2)}s using {collectionInfo?.method} processing.
              </AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      {/* Detailed Timing Information */}
      {collectionInfo && collectionInfo.task_timings && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center space-x-2">
              <Zap className="h-4 w-4" />
              <span>Category Timing Details</span>
            </CardTitle>
            <CardDescription>
              Individual collection times for each metric category
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3">
              {Object.entries(collectionInfo.task_timings).map(([category, timing]) => {
                const isTimeout = timing.includes('timeout');
                const isFailed = timing.includes('failed');
                const duration = parseFloat(timing.replace(/[^\d.]/g, ''));
                
                return (
                  <div key={category} className="flex items-center justify-between py-2 border-b last:border-0">
                    <span className="text-sm capitalize font-medium">{category}</span>
                    <div className="flex items-center space-x-2">
                      <Badge 
                        variant={
                          isTimeout ? 'default' : 
                          isFailed ? 'destructive' : 
                          'secondary'
                        }
                        className="font-mono text-xs"
                      >
                        {timing}
                      </Badge>
                      {!isTimeout && !isFailed && (
                        <div className="w-20 bg-gray-200 rounded-full h-1">
                          <div 
                            className="bg-blue-600 h-1 rounded-full transition-all duration-300"
                            style={{ 
                              width: `${Math.min((duration / Math.max(...Object.values(collectionInfo.task_timings).map(t => parseFloat(t.replace(/[^\d.]/g, '')) || 0))) * 100, 100)}%` 
                            }}
                          />
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
