import { useState, useEffect } from 'react';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';

interface StringEditorProps {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}

export function StringEditor({ value, onChange, disabled = false }: StringEditorProps) {
  const [stringValue, setStringValue] = useState('');
  const [isMultiline, setIsMultiline] = useState(false);

  useEffect(() => {
    const stringVal = String(value || '');
    setStringValue(stringVal);
    // Auto-detect multiline content
    setIsMultiline(stringVal.includes('\n') || stringVal.length > 100);
  }, [value]);

  const handleChange = (newValue: string) => {
    setStringValue(newValue);
    onChange(newValue);
  };

  const toggleMultiline = () => {
    setIsMultiline(!isMultiline);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-muted-foreground">
          String Value
        </h3>
        <div className="flex items-center space-x-2">
          <Label htmlFor="multiline-toggle" className="text-xs">
            Multiline
          </Label>
          <Switch
            id="multiline-toggle"
            checked={isMultiline}
            onCheckedChange={toggleMultiline}
            disabled={disabled}
          />
        </div>
      </div>

      {isMultiline ? (
        <Textarea
          value={stringValue}
          onChange={(e) => handleChange(e.target.value)}
          placeholder="Enter string value..."
          className="min-h-[300px] font-mono text-sm resize-none"
          disabled={disabled}
        />
      ) : (
        <Input
          value={stringValue}
          onChange={(e) => handleChange(e.target.value)}
          placeholder="Enter string value..."
          className="font-mono text-sm"
          disabled={disabled}
        />
      )}

      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {stringValue.length} characters
          {stringValue.includes('\n') && ` • ${stringValue.split('\n').length} lines`}
        </span>
        <div className="flex space-x-4">
          {stringValue && (
            <>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => handleChange('')}
                disabled={disabled}
                className="h-6 text-xs px-2 text-red-600 hover:text-red-700"
              >
                Clear
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  try {
                    const formatted = JSON.stringify(JSON.parse(stringValue), null, 2);
                    handleChange(formatted);
                    setIsMultiline(true);
                  } catch {
                    // Not valid JSON, ignore
                  }
                }}
                disabled={disabled}
                className="h-6 text-xs px-2"
              >
                Format JSON
              </Button>
            </>
          )}
        </div>
      </div>

      <div className="text-xs text-muted-foreground">
        💡 Toggle multiline mode for longer text or JSON content. Use "Format JSON" to prettify valid JSON strings.
      </div>
    </div>
  );
}
