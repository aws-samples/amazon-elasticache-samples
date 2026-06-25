import React from 'react';

// Very lightweight Markdown renderer to cover basic needs for this feature.
// Supports: headings (#, ##, ###), paragraphs, links, images (png/jpg/gif/svg), bold/italic, code blocks (fenced), inline code, lists.
// This is intentionally minimal to avoid adding new dependencies.

function escapeHtml(str: string) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function markdownToHtml(md: string): string {
  // Normalize line endings
  let txt = md.replace(/\r\n?/g, '\n');

  // Code blocks (```)
  txt = txt.replace(/```([\s\S]*?)```/g, (_, code) => {
    return `<pre><code>${escapeHtml(code)}</code></pre>`;
  });

  // Images ![alt](src)
  txt = txt.replace(/!\[(.*?)\]\(([^\s)]+)(?:\s+"(.*?)")?\)/g, (_m, alt, src, title) => {
    const t = title ? ` title="${escapeHtml(title)}"` : '';
    return `<img src="${src}" alt="${escapeHtml(alt)}"${t} class="my-2 max-w-full"/>`;
  });

  // Links [text](href)
  txt = txt.replace(/\[(.*?)\]\(([^\s)]+)(?:\s+"(.*?)")?\)/g, (_m, text, href, title) => {
    const t = title ? ` title="${escapeHtml(title)}"` : '';
    return `<a href="${href}"${t} class="text-primary underline" target="_blank" rel="noreferrer noopener">${escapeHtml(text)}</a>`;
  });

  // Bold **text**
  txt = txt.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  // Italic *text*
  txt = txt.replace(/(^|\s)\*(.*?)\*(\s|$)/g, '$1<em>$2</em>$3');
  // Inline code `code`
  txt = txt.replace(/`([^`]+)`/g, '<code class="bg-muted px-1 rounded">$1</code>');

  // Headings
  txt = txt.replace(/^###\s+(.*)$/gm, '<h3 class="text-lg font-semibold mt-4">$1</h3>');
  txt = txt.replace(/^##\s+(.*)$/gm, '<h2 class="text-xl font-bold mt-6">$1</h2>');
  txt = txt.replace(/^#\s+(.*)$/gm, '<h1 class="text-2xl font-bold mt-8">$1</h1>');

  // Unordered lists
  // Convert blocks of lines starting with - or * into <ul><li>...</li></ul>
  txt = txt.replace(/(?:^|\n)((?:\s*[-*]\s+.*\n?)+)/g, (_m, listBlock) => {
    const items = listBlock
      .trimEnd()
      .split(/\n/)
      .map((line: string) => line.replace(/^\s*[-*]\s+/, ''))
      .map((item: string) => `<li class="list-disc ml-6">${item}</li>`) // items may already contain inline HTML replacements
      .join('');
    return `\n<ul class="my-2">${items}</ul>`;
  });

  // Paragraphs: wrap blocks separated by blank lines into <p>
  const blocks = txt.split(/\n\s*\n/);
  const html = blocks
    .map(block => {
      // If block already contains block-level tags, don't wrap
      if (/^\s*<(h\d|ul|pre)/.test(block)) return block;
      return `<p class="my-2 leading-7">${block.replace(/\n/g, '<br/>')}</p>`;
    })
    .join('\n');

  return html;
}

export function MarkdownRenderer({ markdown }: { markdown: string }) {
  const html = React.useMemo(() => markdownToHtml(markdown), [markdown]);
  return (
    <div className="prose max-w-none text-left" dangerouslySetInnerHTML={{ __html: html }} />
  );
}

export default MarkdownRenderer;
