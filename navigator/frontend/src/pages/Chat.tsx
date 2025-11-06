import React, { useState, useRef, useEffect } from 'react';
import { Button } from '../components/ui/button';
import { Card, CardContent } from '../components/ui/card';
import { MessageCircle, Send } from 'lucide-react';
import { valkeyApi } from '../services/valkeyApi';
import { useConnection } from '@/contexts/ConnectionContext';

interface Message {
  id: string;
  text: string;
  sender: 'user' | 'bot';
  timestamp: Date;
}

export function Chat() {
  const { activeConnection } = useConnection();
  const [messages, setMessages] = useState<Message[]>([
    {
      id: '1',
      text: 'Hello! I\'m your Valkey assistant. How can I help you today?',
      sender: 'bot',
      timestamp: new Date(),
    },
  ]);
  const [inputText, setInputText] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Update API service connection whenever active connection changes
  useEffect(() => {
    if (activeConnection) {
      console.log('🔗 Chat: Updating API service with new connection:', activeConnection.name);
      valkeyApi.setConnection(activeConnection);
    }
  }, [activeConnection]);

  const scrollToBottom = () => {
    setTimeout(() => {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, 100);
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.focus();
    }
  }, []);

  const handleSendMessage = async () => {
    if (!inputText.trim()) return;

    const userMessage: Message = {
      id: Date.now().toString(),
      text: inputText,
      sender: 'user',
      timestamp: new Date(),
    };

    setMessages((prev) => [...prev, userMessage]);
    const currentInput = inputText;
    setInputText('');
    setIsTyping(true);

    try {
      const data = await valkeyApi.sendChatMessage(currentInput);
      
      // Handle the nested response format from the API
      let botResponseText = 'No response received';
      
      if (data.response) {
        try {
          const responseStr = data.response;
          console.log('Raw API response:', responseStr);
          
          // Try to extract text from the specific format: {'role': 'assistant', 'content': [{'text': '...'}]}
          const contentMatch = responseStr.match(/'content':\s*\[\s*\{\s*'text':\s*'([^']+(?:\\'[^']*)*)'(?:\s*\}\s*\])?/);
          if (contentMatch && contentMatch[1]) {
            // Clean up escaped characters and format the text
            botResponseText = contentMatch[1]
              .replace(/\\'/g, "'")
              .replace(/\\n/g, '\n')
              .replace(/\\"/g, '"')
              .replace(/\\\\/g, '\\');
          } else {
            // Try alternative parsing approaches
            try {
              // Try to parse as JSON first
              const parsed = JSON.parse(responseStr);
              if (parsed.content && Array.isArray(parsed.content) && parsed.content[0] && parsed.content[0].text) {
                botResponseText = parsed.content[0].text;
              } else if (parsed.text) {
                botResponseText = parsed.text;
              } else {
                throw new Error('No text content found in JSON');
              }
            } catch (jsonError) {
              // Try regex for quoted text content
              const quotedTextMatch = responseStr.match(/'text':\s*'([^']+(?:\\'[^']*)*)'/) ||
                                      responseStr.match(/"text":\s*"([^"]+(?:\\"[^"]*)*)"/) ||
                                      responseStr.match(/text["']?\s*:\s*["']([^"']+)["']/);
              
              if (quotedTextMatch && quotedTextMatch[1]) {
                botResponseText = quotedTextMatch[1]
                  .replace(/\\'/g, "'")
                  .replace(/\\n/g, '\n')
                  .replace(/\\"/g, '"')
                  .replace(/\\\\/g, '\\');
              } else {
                // Last resort: if it looks like raw text, use it
                if (responseStr.length > 0 && !responseStr.includes('{') && !responseStr.includes('[')) {
                  botResponseText = responseStr;
                } else {
                  console.warn('Could not parse response format:', responseStr.substring(0, 200));
                  botResponseText = 'Sorry, I received a response but couldn\'t parse it properly. Please try again.';
                }
              }
            }
          }
        } catch (extractionError) {
          console.error('Failed to extract text from response:', extractionError);
          botResponseText = 'Sorry, there was an error processing the response. Please try again.';
        }
      } else {
        // Fallback to other possible response formats
        botResponseText = data.message || data.text || 'No response received';
      }

      const botMessage: Message = {
        id: (Date.now() + 1).toString(),
        text: botResponseText,
        sender: 'bot',
        timestamp: new Date(),
      };

      setMessages((prev) => [...prev, botMessage]);
    } catch (error) {
      console.error('Error calling chat API:', error);
      
      const errorMessage: Message = {
        id: (Date.now() + 1).toString(),
        text: 'Sorry, I encountered an error while processing your request. Please make sure the API server is running and try again.',
        sender: 'bot',
        timestamp: new Date(),
      };

      setMessages((prev) => [...prev, errorMessage]);
    } finally {
      setIsTyping(false);
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex-shrink-0 p-6 border-b">
        <div className="flex items-center gap-3">
          <MessageCircle className="w-8 h-8 text-primary" />
          <div>
            <h1 className="text-2xl font-bold">Valkey Assistant</h1>
            <p className="text-muted-foreground">Ask questions about Valkey operations and get expert assistance</p>
          </div>
        </div>
      </div>

      <div className="flex-1 flex flex-col min-h-0">
        <Card className="flex-1 m-6 flex flex-col">
          <CardContent className="flex-1 flex flex-col p-0 min-h-0">
            {/* Messages Area */}
            <div className="flex-1 overflow-y-auto p-6 space-y-4 min-h-0">
              {messages.map((message) => (
                <div
                  key={message.id}
                  className={`flex ${
                    message.sender === 'user' ? 'justify-end' : 'justify-start'
                  }`}
                >
                  <div
                    className={`max-w-[70%] rounded-lg px-4 py-3 text-sm break-words ${
                      message.sender === 'user'
                        ? 'bg-primary text-primary-foreground text-right'
                        : 'bg-muted text-muted-foreground text-left'
                    }`}
                  >
                    <div className="whitespace-pre-wrap leading-relaxed">
                      {message.text}
                    </div>
                    <div className="text-xs opacity-70 mt-2">
                      {message.timestamp.toLocaleTimeString([], { 
                        hour: '2-digit', 
                        minute: '2-digit' 
                      })}
                    </div>
                  </div>
                </div>
              ))}
              
              {/* Typing Indicator */}
              {isTyping && (
                <div className="flex justify-start">
                  <div className="bg-muted text-muted-foreground max-w-[70%] rounded-lg px-4 py-3 text-sm">
                    <div className="flex items-center gap-2">
                      <div className="flex gap-1">
                        <div className="w-2 h-2 bg-current rounded-full animate-bounce [animation-delay:-0.3s]"></div>
                        <div className="w-2 h-2 bg-current rounded-full animate-bounce [animation-delay:-0.15s]"></div>
                        <div className="w-2 h-2 bg-current rounded-full animate-bounce"></div>
                      </div>
                      <span className="text-xs opacity-70">Assistant is typing...</span>
                    </div>
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>

            {/* Input Area */}
            <div className="flex-shrink-0 border-t p-6">
              <div className="flex gap-3">
                <input
                  ref={inputRef}
                  type="text"
                  value={inputText}
                  onChange={(e) => setInputText(e.target.value)}
                  onKeyPress={handleKeyPress}
                  placeholder="Ask me about Valkey operations, commands, best practices..."
                  className="flex-1 px-4 py-3 text-sm bg-background border rounded-lg focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent resize-none"
                  disabled={isTyping}
                />
                <Button
                  size="icon"
                  onClick={handleSendMessage}
                  disabled={!inputText.trim() || isTyping}
                  className="h-12 w-12 flex-shrink-0"
                >
                  <Send className="w-5 h-5" />
                </Button>
              </div>
              <div className="text-xs text-muted-foreground mt-2 text-center">
                Press Enter to send, Shift+Enter for new line
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
