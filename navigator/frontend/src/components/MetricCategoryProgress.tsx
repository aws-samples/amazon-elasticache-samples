import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { 
  CheckCircle, 
  XCircle, 
  Clock, 
  Loader2, 
  RefreshCw,
  AlertTriangle,
  Database,
  HardDrive,
  Users,
  Zap,
  Layers,
  Activity,
  Hash
} from 'lucide-react';
import type { MetricCategory, CategoryState } from '@/types';

interface MetricCategoryProgressProps {
  category: MetricCategory;
  state: CategoryState;
  onRetry?: (category: MetricCategory) => void;
  compact?: boolean;
}

const CATEGORY_CONFIGS = {
  server: {
    label: 'Server Info',
    icon: Database,
    description: 'System information and uptime'
  },
  memory: {
    label: 'Memory Usage',
    icon: HardDrive,
    description: 'Memory allocation and usage stats'
  },
  connections: {
    label: 'Connections',
    icon: Users,
    description: 'Client connections and network info'
  },
  commands: {
    label: 'Commands',
    icon: Zap,
    description: 'Command processing and throughput'
  },
  cluster: {
    label: 'Cluster',
    icon: Layers,
    description: 'Cluster topology and replication'
  },
  performance: {
    label: 'Performance',
    icon: Activity,
    description: 'Hit rates and response times'
  },
  keyspace: {
    label: 'Keyspace',
    icon: Hash,
    description: 'Database keys and expiration data'
  }
} as const;

const STATUS_CONFIGS = {
  pending: {
    color: 'bg-gray-100 text-gray-600',
    badgeVariant: 'secondary' as const,
    icon: Clock,
    label: 'Pending'
  },
  loading: {
    color: 'bg-blue-100 text-blue-600',
    badgeVariant: 'default' as const,
    icon: Loader2,
    label: 'Loading'
  },
  complete: {
    color: 'bg-green-100 text-green-600',
    badgeVariant: 'default' as const,
    icon: CheckCircle,
    label: 'Complete'
  },
  failed: {
    color: 'bg-red-100 text-red-600',
    badgeVariant: 'destructive' as const,
    icon: XCircle,
    label: 'Failed'
  },
  timeout: {
    color: 'bg-yellow-100 text-yellow-600',
    badgeVariant: 'default' as const,
    icon: AlertTriangle,
    label: 'Timeout'
  }
} as const;

export function MetricCategoryProgress({ 
  category, 
  state, 
  onRetry, 
  compact = false 
}: MetricCategoryProgressProps) {
  const config = CATEGORY_CONFIGS[category];
  const statusConfig = STATUS_CONFIGS[state.status];
  const IconComponent = config.icon;
  const StatusIcon = statusConfig.icon;

  const canRetry = (state.status === 'failed' || state.status === 'timeout') && onRetry;
  const isAnimated = state.status === 'loading';

  if (compact) {
    return (
      <div className="flex items-center space-x-3 p-3 rounded-lg border bg-card">
        <div className={`p-2 rounded-full ${statusConfig.color}`}>
          <IconComponent className="h-4 w-4" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-medium truncate">{config.label}</h4>
            <div className="flex items-center space-x-2">
              {state.timing && (
                <span className="text-xs text-muted-foreground font-mono">
                  {state.timing}
                </span>
              )}
              <Badge 
                variant={statusConfig.badgeVariant}
                className="text-xs"
              >
                <StatusIcon 
                  className={`h-3 w-3 mr-1 ${isAnimated ? 'animate-spin' : ''}`} 
                />
                {statusConfig.label}
              </Badge>
            </div>
          </div>
          {state.error && (
            <p className="text-xs text-red-600 mt-1 truncate" title={state.error}>
              {state.error}
            </p>
          )}
        </div>
        {canRetry && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => onRetry(category)}
            className="h-8 w-8 p-0"
          >
            <RefreshCw className="h-3 w-3" />
          </Button>
        )}
      </div>
    );
  }

  return (
    <Card className="relative overflow-hidden">
      <CardContent className="p-4">
        <div className="flex items-start space-x-3">
          <div className={`p-3 rounded-full ${statusConfig.color}`}>
            <IconComponent className="h-5 w-5" />
          </div>
          
          <div className="flex-1 min-w-0">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-semibold">{config.label}</h3>
              <Badge variant={statusConfig.badgeVariant}>
                <StatusIcon 
                  className={`h-3 w-3 mr-1 ${isAnimated ? 'animate-spin' : ''}`} 
                />
                {statusConfig.label}
              </Badge>
            </div>
            
            <p className="text-xs text-muted-foreground mb-3">
              {config.description}
            </p>

            {/* Timing and Performance Info */}
            <div className="space-y-2">
              {state.timing && (
                <div className="flex justify-between text-xs">
                  <span className="text-muted-foreground">Duration:</span>
                  <span className="font-mono">{state.timing}</span>
                </div>
              )}
              
              {state.retryCount && state.retryCount > 0 && (
                <div className="flex justify-between text-xs">
                  <span className="text-muted-foreground">Retries:</span>
                  <span className="font-mono">{state.retryCount}</span>
                </div>
              )}
              
              {state.lastAttempt && (
                <div className="flex justify-between text-xs">
                  <span className="text-muted-foreground">Last attempt:</span>
                  <span className="font-mono text-xs">
                    {state.lastAttempt.toLocaleTimeString()}
                  </span>
                </div>
              )}
            </div>

            {/* Error Message */}
            {state.error && (
              <div className="mt-3 p-2 bg-red-50 border border-red-200 rounded text-xs text-red-700">
                {state.error}
              </div>
            )}

            {/* Progress Bar for Loading State */}
            {state.status === 'loading' && (
              <div className="mt-3">
                <Progress value={undefined} className="h-1" />
              </div>
            )}

            {/* Retry Button */}
            {canRetry && (
              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onRetry(category)}
                  className="w-full"
                >
                  <RefreshCw className="h-3 w-3 mr-2" />
                  Retry {config.label}
                </Button>
              </div>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// Grid component for displaying multiple category progress cards
interface MetricProgressGridProps {
  categoryStates: Record<MetricCategory, CategoryState>;
  onRetryCategory?: (category: MetricCategory) => void;
  compact?: boolean;
}

export function MetricProgressGrid({ 
  categoryStates, 
  onRetryCategory, 
  compact = false 
}: MetricProgressGridProps) {
  const categories = Object.keys(categoryStates) as MetricCategory[];

  if (compact) {
    return (
      <div className="space-y-2">
        {categories.map(category => (
          <MetricCategoryProgress
            key={category}
            category={category}
            state={categoryStates[category]}
            onRetry={onRetryCategory}
            compact
          />
        ))}
      </div>
    );
  }

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      {categories.map(category => (
        <MetricCategoryProgress
          key={category}
          category={category}
          state={categoryStates[category]}
          onRetry={onRetryCategory}
        />
      ))}
    </div>
  );
}
