import { useState, useCallback } from 'react';
import { valkeyApi } from '@/services/valkeyApi';

interface UseRecommendationsResult {
  recommendation: string | null;
  isLoading: boolean;
  error: string | null;
  getRecommendations: (prompt: string) => Promise<void>;
  clear: () => void;
}

export function useRecommendations(): UseRecommendationsResult {
  const [recommendation, setRecommendation] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const getRecommendations = useCallback(async (prompt: string) => {
    if (!prompt.trim()) {
      setError('Prompt cannot be empty');
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      const response = await valkeyApi.getRecommendations(prompt);
      
      // Extract recommendation text from response
      // Handle various possible response formats
      let recommendationText: string;
      
      if (typeof response === 'string') {
        recommendationText = response;
      } else if (response?.recommendation) {
        recommendationText = response.recommendation;
      } else if (response?.response) {
        recommendationText = response.response;
      } else if (response?.text) {
        recommendationText = response.text;
      } else if (response?.message) {
        recommendationText = response.message;
      } else if (response?.data) {
        // Handle nested data structure
        if (typeof response.data === 'string') {
          recommendationText = response.data;
        } else if (response.data.recommendation) {
          recommendationText = response.data.recommendation;
        } else if (response.data.response) {
          recommendationText = response.data.response;
        } else {
          recommendationText = JSON.stringify(response.data, null, 2);
        }
      } else {
        // Fallback: stringify the entire response
        recommendationText = JSON.stringify(response, null, 2);
      }

      // Check if the response is a JSON array of strings (common AI response format)
      if (typeof recommendationText === 'string' && recommendationText.trim().startsWith('[')) {
        try {
          const parsed = JSON.parse(recommendationText);
          if (Array.isArray(parsed)) {
            // Convert array of strings to formatted text
            recommendationText = parsed
              .map(line => {
                if (typeof line === 'string') {
                  // Remove leading/trailing quotes and unescape quotes
                  return line.replace(/^"/, '').replace(/"$/, '').replace(/\\"/g, '"');
                }
                return line;
              })
              .join('\n')
              .trim();
          }
        } catch (e) {
          // If parsing fails, use the original text
          console.warn('Failed to parse JSON array response:', e);
        }
      }

      setRecommendation(recommendationText);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to get recommendations';
      setError(errorMessage);
      console.error('Recommendations error:', err);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const clear = useCallback(() => {
    setRecommendation(null);
    setError(null);
  }, []);

  return {
    recommendation,
    isLoading,
    error,
    getRecommendations,
    clear
  };
}
