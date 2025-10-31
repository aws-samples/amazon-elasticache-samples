import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import MarkdownRenderer from '@/components/MarkdownRenderer';

// Import markdown files as raw strings via Vite ?raw
import overviewMd from '@/content/help/overview.md?raw';
import settingsMd from '@/content/help/settings.md?raw';
import connectionsMd from '@/content/help/connections.md?raw';
import aiAgentRecommendationMd from '@/content/help/ai-agent-recommendation.md?raw';

export function HelpDocs() {
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Help & Documentation</h1>
      <Tabs defaultValue="overview" className="space-y-4">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
          <TabsTrigger value="connections">Connections</TabsTrigger>
          <TabsTrigger value="ai-agent-recommendation">AI Agent Recommendation</TabsTrigger>
        </TabsList>
        <TabsContent value="overview" className="space-y-4">
          <MarkdownRenderer markdown={overviewMd} />
        </TabsContent>
        <TabsContent value="settings" className="space-y-4">
          <MarkdownRenderer markdown={settingsMd} />
        </TabsContent>
        <TabsContent value="connections" className="space-y-4">
          <MarkdownRenderer markdown={connectionsMd} />
        </TabsContent>
        <TabsContent value="ai-agent-recommendation" className="space-y-4">
          <MarkdownRenderer markdown={aiAgentRecommendationMd} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

export default HelpDocs;
