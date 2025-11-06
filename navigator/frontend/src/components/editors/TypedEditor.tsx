import { HashEditor } from './HashEditor';
import { ListEditor } from './ListEditor';
import { StringEditor } from './StringEditor';
import { SetEditor } from './SetEditor';
import { SortedSetEditor } from './SortedSetEditor';
import { BloomFilterEditor } from './BloomFilterEditor';
import { Textarea } from '@/components/ui/textarea';

interface TypedEditorProps {
  value: any;
  onChange: (value: any) => void;
  redisType: string;
  disabled?: boolean;
  keyName?: string;
}

export function TypedEditor({ value, onChange, redisType, disabled = false, keyName }: TypedEditorProps) {
  const normalizedType = redisType?.toLowerCase() || 'string';

  // For hash types, expect object-like data
  if (normalizedType === 'hash') {
    const hashValue = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    return (
      <HashEditor
        value={hashValue}
        onChange={onChange}
        disabled={disabled}
      />
    );
  }

  // For list types, expect array data
  if (normalizedType === 'list') {
    const listValue = Array.isArray(value) ? value : [];
    return (
      <ListEditor
        value={listValue}
        onChange={onChange}
        disabled={disabled}
      />
    );
  }

  // For string types, use the string editor
  if (normalizedType === 'string') {
    const stringValue = typeof value === 'string' ? value : String(value || '');
    return (
      <StringEditor
        value={stringValue}
        onChange={onChange}
        disabled={disabled}
      />
    );
  }

  // For JSON types, provide dedicated JSON editor
  if (normalizedType === 'json') {
    return (
      <div className="space-y-4">
        <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-3">
          <div className="flex items-center">
            <div className="text-emerald-800 text-sm">
              <strong>JSON Editor</strong> - Edit JSON data with syntax validation
            </div>
          </div>
        </div>
        <Textarea
          value={typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value || '')}
          onChange={(e) => {
            let valueToSave = e.target.value;
            
            // Try to parse JSON for immediate validation
            try {
              if (valueToSave.trim()) {
                const parsed = JSON.parse(valueToSave);
                onChange(parsed);
              } else {
                onChange('');
              }
            } catch (error) {
              // Keep the raw string value if JSON is invalid
              // This allows users to edit incomplete JSON without losing their work
              onChange(valueToSave);
            }
          }}
          placeholder="Enter JSON data..."
          className="min-h-[300px] font-mono text-sm"
          disabled={disabled}
        />
        <div className="text-xs text-muted-foreground">
          💡 JSON will be validated and parsed automatically. Invalid JSON will be saved as text.
        </div>
      </div>
    );
  }

  // For set types, use the dedicated SetEditor
  if (normalizedType === 'set') {
    const setValue = Array.isArray(value) ? value : [];
    return (
      <SetEditor
        value={setValue}
        onChange={onChange}
        disabled={disabled}
      />
    );
  }

  // For zset (sorted set) types, use the dedicated SortedSetEditor
  if (normalizedType === 'zset') {
    return (
      <SortedSetEditor
        value={value}
        onChange={onChange}
        disabled={disabled}
      />
    );
  }

  // For stream types
  if (normalizedType === 'stream') {
    return (
      <div className="space-y-4">
        <div className="bg-purple-50 border border-purple-200 rounded-lg p-3">
          <div className="flex items-center">
            <div className="text-purple-800 text-sm">
              <strong>Valkey Stream Editor</strong> - Stream editing interface coming soon
            </div>
          </div>
        </div>
        <Textarea
          value={typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value || '')}
          onChange={(e) => {
            try {
              const parsed = JSON.parse(e.target.value);
              onChange(parsed);
            } catch {
              onChange(e.target.value);
            }
          }}
          placeholder="Enter stream data as JSON..."
          className="min-h-[300px] font-mono text-sm"
          disabled={disabled}
        />
      </div>
    );
  }

  // For bloom filter types, use the dedicated BloomFilterEditor
  if (normalizedType === 'bloomfltr' || normalizedType === 'bloom_filter' || normalizedType === 'bloom') {
    return (
      <BloomFilterEditor
        value={value}
        onChange={onChange}
        disabled={disabled}
        keyName={keyName}
      />
    );
  }

  // Fallback for unknown types or 'none'
  return (
    <div className="space-y-4">
      {normalizedType !== 'unknown' && (
        <div className="bg-gray-50 border border-gray-200 rounded-lg p-3">
          <div className="flex items-center">
            <div className="text-gray-800 text-sm">
              <strong>Generic Editor</strong> - Valkey type: {normalizedType}
            </div>
          </div>
        </div>
      )}
      <Textarea
        value={typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value || '')}
        onChange={(e) => {
          let valueToSave = e.target.value;
          
          // Try to parse JSON if it looks like JSON
          if (valueToSave.trim().startsWith('{') || valueToSave.trim().startsWith('[')) {
            try {
              valueToSave = JSON.parse(valueToSave);
            } catch {
              // Keep as string if JSON parsing fails
            }
          }
          
          onChange(valueToSave);
        }}
        placeholder="Enter value..."
        className="min-h-[300px] font-mono text-sm"
        disabled={disabled}
      />
    </div>
  );
}
