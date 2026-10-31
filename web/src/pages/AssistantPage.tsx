import React, { useState, useRef, useEffect } from 'react';
import {
  Bot,
  Send,
  Sparkles,
  ShieldCheck,
  Cpu,
  CornerDownLeft,
  CheckCircle2,
  AlertCircle,
  HelpCircle,
  Link as LinkIcon,
} from 'lucide-react';
import { useApp } from '../context/AppContext';
import { askAssistant } from '../lib/api';
import { AssistantResponse } from '../types';

interface ChatMessage {
  id: string;
  sender: 'user' | 'assistant';
  content: string;
  timestamp: string;
  provider?: string;
  model?: string;
  endpoints?: string[];
  data?: Record<string, any>;
}

function cleanMarkdownText(str: string): string {
  if (!str) return '';
  return str
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/^>\s*(\[!.*?\])?\s*/gm, '')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\*([^*]+)\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\u2011/g, '-')
    .trim();
}

export const AssistantPage: React.FC = () => {
  const { activeTenant, currency, startDate, endDate, token } = useApp();

  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      sender: 'assistant',
      content:
        `### 👋 Hello! I am your AstraRoll Analytics AI Assistant.\n\n` +
        `I answer questions directly grounded in your verified tenant database rollups.\n\n` +
        `- **Metric Semantics**: Sales strictly count *shipped* orders under AstraRoll V2 frozen rules.\n` +
        `- **Data Boundaries**: Grouped by UTC order creation date across your selected window.\n` +
        `- **Engine**: Integrated with Groq LLaMA 3.3 API with deterministic grounded fallback.\n\n` +
        `Choose a prompt below or type your question!`,
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      provider: 'grounded_rule_engine',
      model: 'rule-based-grounded-v2',
      endpoints: ['/analytics/summary'],
    },
  ]);

  const [inputQuery, setInputQuery] = useState<string>('');
  const [isTyping, setIsTyping] = useState<boolean>(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, isTyping]);

  const promptChips = [
    'What is our total revenue and order volume?',
    'Show me top selling products',
    'What is our order status breakdown?',
    'Show daily sales trends',
    'How fresh is our aggregate data?',
  ];

  const handleSend = async (queryText?: string) => {
    const q = queryText || inputQuery;
    if (!q.trim() || !activeTenant || isTyping) return;

    const userMsg: ChatMessage = {
      id: String(Date.now()),
      sender: 'user',
      content: q.trim(),
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    };

    setMessages((prev) => [...prev, userMsg]);
    setInputQuery('');
    setIsTyping(true);

    try {
      const resp: AssistantResponse = await askAssistant(
        activeTenant.id,
        q.trim(),
        currency,
        startDate,
        endDate,
        token
      );

      const assistantMsg: ChatMessage = {
        id: String(Date.now() + 1),
        sender: 'assistant',
        content: resp.reply,
        timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        provider: resp.provider,
        model: resp.model,
        endpoints: resp.endpoints_called,
        data: resp.data,
      };

      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err: any) {
      const errorMsg: ChatMessage = {
        id: String(Date.now() + 1),
        sender: 'assistant',
        content: `⚠️ **Analytics Query Error**: ${err.message || 'Unable to retrieve analytics response.'}`,
        timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsTyping(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex flex-col h-[calc(100vh-140px)] rounded-2xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-sm overflow-hidden">
      {/* Header bar */}
      <div className="flex items-center justify-between px-6 py-3.5 border-b border-slate-100 dark:border-slate-800 bg-slate-50/50 dark:bg-[#092c22]/50">
        <div className="flex items-center space-x-3">
          <div className="p-2 rounded-xl bg-emerald-600 text-white shadow-md shadow-emerald-600/20">
            <Bot className="w-4 h-4" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <span className="font-bold text-sm text-slate-900 dark:text-white">
                AstraRoll AI Analytics Assistant
              </span>
              <span className="px-2 py-0.5 text-[10px] font-bold rounded-full bg-emerald-50 dark:bg-emerald-950/60 text-emerald-600 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-800">
                Grounded Engine
              </span>
            </div>
            <p className="text-[11px] text-slate-500 dark:text-slate-400">
              Strictly cited from authorized endpoints • Zero SQL hallucinations
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 text-xs text-slate-500">
          <ShieldCheck className="w-4 h-4 text-emerald-500" />
          <span className="font-medium hidden sm:inline">V2 Policy Enforced</span>
        </div>
      </div>

      {/* Messages Stream */}
      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        {messages.map((m) => {
          const isUser = m.sender === 'user';
          return (
            <div
              key={m.id}
              className={`flex items-start space-x-3 ${isUser ? 'flex-row-reverse space-x-reverse' : ''}`}
            >
              {/* Avatar */}
              <div
                className={`w-8 h-8 rounded-xl shrink-0 flex items-center justify-center shadow-sm ${
                  isUser
                    ? 'bg-slate-900 dark:bg-slate-700 text-white'
                    : 'bg-emerald-600 text-white shadow-emerald-600/20'
                }`}
              >
                {isUser ? <span className="text-xs font-bold">You</span> : <Bot className="w-4 h-4" />}
              </div>

              {/* Message Bubble */}
              <div
                className={`max-w-2xl rounded-2xl p-4 space-y-2 text-xs leading-relaxed ${
                  isUser
                    ? 'bg-emerald-600 text-white rounded-tr-none'
                    : 'bg-slate-50 dark:bg-[#092c22] text-slate-800 dark:text-slate-200 border border-slate-200/80 dark:border-slate-800 rounded-tl-none'
                }`}
              >
                {/* Clean content with no markdown characters */}
                <div className="space-y-2 text-xs leading-relaxed">
                  {cleanMarkdownText(m.content).split('\n\n').map((para, i) => {
                    const cleaned = cleanMarkdownText(para);
                    if (!cleaned) return null;
                    if (cleaned.startsWith('- ') || cleaned.includes('\n- ')) {
                      return (
                        <ul key={i} className="list-disc pl-4 space-y-1 my-1">
                          {cleaned.split('\n').map((item, idx) => {
                            const bulletText = cleanMarkdownText(item.replace(/^- \s*/, ''));
                            if (!bulletText) return null;
                            return <li key={idx} className="text-slate-800 dark:text-slate-200">{bulletText}</li>;
                          })}
                        </ul>
                      );
                    }
                    return (
                      <p key={i} className="text-slate-800 dark:text-slate-200 whitespace-pre-line">
                        {cleaned}
                      </p>
                    );
                  })}
                </div>

                {/* Metadata & Citations */}
                {!isUser && (
                  <div className="pt-2 mt-2 border-t border-slate-200/60 dark:border-slate-800/80 flex flex-wrap items-center justify-between gap-2 text-[10px] text-slate-400">
                    <div className="flex items-center space-x-2">
                      <span className="font-mono text-emerald-600 dark:text-emerald-400 font-medium">
                        {m.model || 'grounded-engine'}
                      </span>
                      <span>•</span>
                      <span>{m.timestamp}</span>
                    </div>

                    {m.endpoints && m.endpoints.length > 0 && (
                      <div className="flex items-center space-x-1">
                        <LinkIcon className="w-3 h-3 text-slate-400" />
                        <span>Cited: {m.endpoints.join(', ')}</span>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}

        {isTyping && (
          <div className="flex items-start space-x-3 animate-in fade-in">
            <div className="w-8 h-8 rounded-xl bg-emerald-600 text-white flex items-center justify-center">
              <Bot className="w-4 h-4 animate-pulse" />
            </div>
            <div className="p-3.5 rounded-2xl bg-slate-50 dark:bg-[#092c22] border border-slate-200 dark:border-slate-800 rounded-tl-none">
              <div className="flex items-center space-x-1.5 text-xs text-slate-500">
                <span className="w-2 h-2 rounded-full bg-emerald-600 animate-bounce" />
                <span className="w-2 h-2 rounded-full bg-emerald-600 animate-bounce [animation-delay:0.2s]" />
                <span className="w-2 h-2 rounded-full bg-emerald-600 animate-bounce [animation-delay:0.4s]" />
                <span className="ml-2 font-medium">Verifying authorized analytics...</span>
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Suggested Prompt Chips */}
      <div className="px-6 py-2 border-t border-slate-100 dark:border-slate-800/80 bg-slate-50/50 dark:bg-[#092c22]/30 flex items-center space-x-2 overflow-x-auto">
        <Sparkles className="w-3.5 h-3.5 text-emerald-500 shrink-0" />
        <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400 shrink-0">
          Suggested:
        </span>
        {promptChips.map((chip, i) => (
          <button
            key={i}
            onClick={() => handleSend(chip)}
            disabled={isTyping}
            className="shrink-0 px-2.5 py-1 text-xs rounded-full bg-white dark:bg-slate-800 hover:bg-emerald-50 dark:hover:bg-emerald-950/60 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-700 transition"
          >
            {chip}
          </button>
        ))}
      </div>

      {/* Input Bar */}
      <div className="p-4 border-t border-slate-100 dark:border-slate-800 bg-white dark:bg-[#07261d]">
        <div className="flex items-center space-x-2 p-1.5 rounded-2xl bg-slate-50 dark:bg-slate-800/80 border border-slate-200 dark:border-slate-700 focus-within:ring-2 focus-within:ring-emerald-500 transition">
          <textarea
            rows={1}
            value={inputQuery}
            onChange={(e) => setInputQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask questions about sales, top products, trends, or freshness..."
            className="flex-1 px-3 py-1.5 text-xs bg-transparent text-slate-900 dark:text-white placeholder:text-slate-400 focus:outline-none resize-none"
          />
          <button
            onClick={() => handleSend()}
            disabled={!inputQuery.trim() || isTyping}
            className="p-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white shadow-md shadow-emerald-600/20 transition"
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
};

