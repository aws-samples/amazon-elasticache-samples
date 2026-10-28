const pptxgen = require('pptxgenjs');
const pptx = new pptxgen();

pptx.layout = 'LAYOUT_WIDE';
pptx.defineLayout({ name: 'WIDE', width: 13.33, height: 7.5 });

const COLORS = {
  bg: '0F172A', white: 'FFFFFF', gray: '94A3B8', darkGray: '1E293B',
  cardBg: '1E293B', border: '334155',
  indigo: '6366F1', indigoLight: '818CF8', indigoBg: '312E81',
  emerald: '10B981', emeraldBg: '064E3B',
  red: 'EF4444', redBg: '7F1D1D',
  purple: 'A78BFA', amber: 'F59E0B', orange: 'F97316',
  cyan: '06B6D4', violet: '8B5CF6',
};

function addBg(slide) {
  slide.background = { color: COLORS.bg };
}

// ── Slide 1: Title ──
let s1 = pptx.addSlide();
addBg(s1);
s1.addText('AWS Livestream Demo', { x: 3.5, y: 1.2, w: 6, h: 0.4, fontSize: 14, color: COLORS.indigo, align: 'center', fontFace: 'Arial' });
s1.addText('Zero Code Cache:', { x: 1, y: 2, w: 11, h: 0.8, fontSize: 40, color: COLORS.white, align: 'center', fontFace: 'Arial', bold: true });
s1.addText('Supercharge Your Agentic AI Apps', { x: 1, y: 2.8, w: 11, h: 0.7, fontSize: 32, color: COLORS.indigoLight, align: 'center', fontFace: 'Arial', bold: true });
s1.addText('JDBC Query Caching with Amazon ElastiCache for Valkey', { x: 2, y: 3.8, w: 9, h: 0.5, fontSize: 18, color: COLORS.gray, align: 'center', fontFace: 'Arial' });

// Service icons row
const svcY = 5.2;
[
  { x: 3.2, label: 'ElastiCache\nfor Valkey', color: COLORS.orange },
  { x: 5.7, label: 'AWS JDBC\nWrapper', color: COLORS.indigo },
  { x: 8.2, label: 'Amazon\nBedrock', color: COLORS.purple },
].forEach(svc => {
  s1.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: svc.x, y: svcY, w: 1.8, h: 1, fill: { color: svc.color + '33' }, line: { color: svc.color, width: 1.5 }, rectRadius: 0.1 });
  s1.addText(svc.label, { x: svc.x, y: svcY + 0.15, w: 1.8, h: 0.7, fontSize: 11, color: COLORS.white, align: 'center', fontFace: 'Arial' });
});

// ── Slide 2: The Problem ──
let s2 = pptx.addSlide();
addBg(s2);
s2.addText('The Problem', { x: 0.8, y: 0.5, w: 5, h: 0.6, fontSize: 28, color: COLORS.red, fontFace: 'Arial', bold: true });
s2.addText("Your AI agent's hidden performance bottleneck", { x: 0.8, y: 1.1, w: 8, h: 0.4, fontSize: 16, color: COLORS.gray, fontFace: 'Arial' });

// Left card - queries
s2.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 0.8, y: 1.8, w: 5.5, h: 4.5, fill: { color: COLORS.cardBg }, line: { color: COLORS.border, width: 1 }, rectRadius: 0.15 });
s2.addText('One Agent Request =', { x: 1.1, y: 2, w: 5, h: 0.4, fontSize: 16, color: COLORS.white, fontFace: 'Arial', bold: true });
const queries = [
  'User profile lookup — 30ms',
  'Product catalog search — 173ms',
  'Category browse — 60ms',
  'Recommendation query 1 — 50ms',
  'Recommendation query 2 — 45ms',
  'Complementary products — 35ms',
];
queries.forEach((q, i) => {
  s2.addText('🔴  ' + q, { x: 1.3, y: 2.6 + i * 0.45, w: 4.8, h: 0.4, fontSize: 13, color: COLORS.white, fontFace: 'Arial' });
});
s2.addText('370ms total DB time per request', { x: 1.3, y: 5.5, w: 4.5, h: 0.4, fontSize: 18, color: COLORS.red, fontFace: 'Arial', bold: true });

// Right card - scale
s2.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 6.8, y: 1.8, w: 5.5, h: 4.5, fill: { color: COLORS.cardBg }, line: { color: COLORS.border, width: 1 }, rectRadius: 0.15 });
s2.addText('At Scale', { x: 7.1, y: 2, w: 5, h: 0.4, fontSize: 16, color: COLORS.white, fontFace: 'Arial', bold: true });
[
  { num: '1,000', desc: 'concurrent users' },
  { num: '10,000', desc: 'DB queries per second' },
  { num: '$$$', desc: 'database costs skyrocket' },
].forEach((item, i) => {
  s2.addText(item.num, { x: 7.3, y: 2.7 + i * 1.2, w: 4.5, h: 0.5, fontSize: 28, color: COLORS.red, fontFace: 'Arial', bold: true });
  s2.addText(item.desc, { x: 7.3, y: 3.15 + i * 1.2, w: 4.5, h: 0.3, fontSize: 13, color: COLORS.gray, fontFace: 'Arial' });
});

// ── Slide 3: Architecture Without Cache ──
let s3 = pptx.addSlide();
addBg(s3);
s3.addText('Architecture — Without Cache', { x: 0.8, y: 0.5, w: 8, h: 0.6, fontSize: 28, color: COLORS.white, fontFace: 'Arial', bold: true });
s3.addText('Every query hits the database', { x: 0.8, y: 1.1, w: 8, h: 0.4, fontSize: 16, color: COLORS.red, fontFace: 'Arial' });

const boxes3 = [
  { x: 0.5, w: 2, label: 'User\nBrowser', color: '3B82F6' },
  { x: 3.2, w: 2.5, label: 'Spring Boot\nAmazon Bedrock Agent\n(tool calls)', color: COLORS.purple },
  { x: 6.4, w: 2.2, label: 'JDBC Driver\norg.postgresql', color: COLORS.indigo },
  { x: 9.5, w: 2.5, label: 'Aurora\nPostgreSQL', color: COLORS.red },
];
boxes3.forEach(b => {
  s3.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: b.x, y: 2.5, w: b.w, h: 1.5, fill: { color: b.color + '22' }, line: { color: b.color, width: 2 }, rectRadius: 0.1 });
  s3.addText(b.label, { x: b.x, y: 2.65, w: b.w, h: 1.2, fontSize: 12, color: COLORS.white, align: 'center', fontFace: 'Arial', valign: 'middle' });
});
// Arrows
[2.5, 5.7, 8.6].forEach(x => {
  s3.addText('→', { x: x, y: 2.8, w: 0.7, h: 1, fontSize: 28, color: COLORS.indigo, align: 'center', fontFace: 'Arial', valign: 'middle' });
});
// Bottom warning
s3.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 2, y: 4.8, w: 9, h: 1.2, fill: { color: COLORS.redBg + '44' }, line: { color: COLORS.red + '55', width: 1 }, rectRadius: 0.1 });
s3.addText('5-10 DB round-trips per agent request\nSame queries repeated across users — all hitting the database', { x: 2.2, y: 4.9, w: 8.6, h: 1, fontSize: 14, color: COLORS.red, align: 'center', fontFace: 'Arial' });

// ── Slide 4: Architecture With Cache ──
let s4 = pptx.addSlide();
addBg(s4);
s4.addText('Architecture — With Cache', { x: 0.8, y: 0.5, w: 8, h: 0.6, fontSize: 28, color: COLORS.white, fontFace: 'Arial', bold: true });
s4.addText('Repeated queries served from Valkey', { x: 0.8, y: 1.1, w: 8, h: 0.4, fontSize: 16, color: COLORS.emerald, fontFace: 'Arial' });

const boxes4 = [
  { x: 0.3, y: 2.8, w: 1.8, h: 1.3, label: 'User\nBrowser', color: '3B82F6' },
  { x: 2.6, y: 2.8, w: 2.2, h: 1.3, label: 'Spring Boot\nAmazon Bedrock Agent', color: COLORS.purple },
  { x: 5.3, y: 2.8, w: 2.5, h: 1.3, label: 'AWS JDBC Wrapper\n+ Remote Query\nCache Plugin', color: COLORS.indigo },
  { x: 8.5, y: 2, w: 2.5, h: 1.2, label: 'ElastiCache\nValkey\nHIT: <1ms', color: COLORS.emerald },
  { x: 8.5, y: 4.2, w: 2.5, h: 1.2, label: 'Aurora\nPostgreSQL\n(miss only)', color: COLORS.gray },
];
boxes4.forEach(b => {
  s4.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: b.x, y: b.y, w: b.w, h: b.h, fill: { color: b.color + '22' }, line: { color: b.color, width: 2 }, rectRadius: 0.1 });
  s4.addText(b.label, { x: b.x, y: b.y + 0.1, w: b.w, h: b.h - 0.2, fontSize: 11, color: COLORS.white, align: 'center', fontFace: 'Arial', valign: 'middle' });
});
s4.addText('→', { x: 2.1, y: 3, w: 0.5, h: 0.8, fontSize: 24, color: COLORS.indigo, align: 'center', valign: 'middle' });
s4.addText('→', { x: 4.8, y: 3, w: 0.5, h: 0.8, fontSize: 24, color: COLORS.indigo, align: 'center', valign: 'middle' });
s4.addText('→ HIT', { x: 7.8, y: 2.2, w: 0.8, h: 0.8, fontSize: 11, color: COLORS.emerald, align: 'center', valign: 'middle' });
s4.addText('↓ MISS', { x: 9.2, y: 3.4, w: 1, h: 0.7, fontSize: 11, color: COLORS.gray, align: 'center', valign: 'middle' });

s4.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 2, y: 5.8, w: 9, h: 0.9, fill: { color: COLORS.emeraldBg + '44' }, line: { color: COLORS.emerald + '55', width: 1 }, rectRadius: 0.1 });
s4.addText('First request: DB + async write to Valkey  |  Every subsequent request: Valkey in <1ms', { x: 2.2, y: 5.85, w: 8.6, h: 0.8, fontSize: 13, color: COLORS.emerald, align: 'center', fontFace: 'Arial' });

// ── Slide 5: ElastiCache for Valkey ──
let s5 = pptx.addSlide();
addBg(s5);
s5.addText('Amazon ElastiCache for Valkey', { x: 0.8, y: 0.5, w: 10, h: 0.6, fontSize: 28, color: COLORS.orange, fontFace: 'Arial', bold: true });
s5.addText('Fully managed, sub-millisecond caching', { x: 0.8, y: 1.1, w: 8, h: 0.4, fontSize: 16, color: COLORS.gray, fontFace: 'Arial' });

const features5 = [
  { icon: '⚡', title: 'Sub-millisecond', desc: 'In-memory reads and writes at microsecond latency' },
  { icon: '🔒', title: 'Secure', desc: 'TLS encryption, IAM authentication, VPC isolation' },
  { icon: '📈', title: 'Serverless', desc: 'Auto-scales with demand. No capacity planning' },
  { icon: '🔄', title: 'High Availability', desc: 'Automatic failover, replication, and backup' },
  { icon: '🌐', title: 'Open-source', desc: 'Valkey — community-driven successor to Redis' },
  { icon: '💰', title: 'Cost Efficient', desc: 'Reduce DB load by up to 90%+ in this demo configuration (see Slide 11). Pay only for what you use' },
];
features5.forEach((f, i) => {
  const col = i % 3;
  const row = Math.floor(i / 3);
  const x = 0.8 + col * 4;
  const y = 1.9 + row * 2.3;
  s5.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x, y, w: 3.6, h: 1.9, fill: { color: COLORS.cardBg }, line: { color: COLORS.border, width: 1 }, rectRadius: 0.1 });
  s5.addText(f.icon, { x, y: y + 0.2, w: 3.6, h: 0.5, fontSize: 24, align: 'center' });
  s5.addText(f.title, { x, y: y + 0.7, w: 3.6, h: 0.35, fontSize: 14, color: COLORS.white, align: 'center', fontFace: 'Arial', bold: true });
  s5.addText(f.desc, { x: x + 0.2, y: y + 1.1, w: 3.2, h: 0.6, fontSize: 11, color: COLORS.gray, align: 'center', fontFace: 'Arial' });
});

// ── Slide 6: AWS JDBC Wrapper ──
let s6 = pptx.addSlide();
addBg(s6);
s6.addText('AWS Advanced JDBC Wrapper', { x: 0.8, y: 0.5, w: 10, h: 0.6, fontSize: 28, color: COLORS.indigo, fontFace: 'Arial', bold: true });
s6.addText('Open-source. Transparent. Production-ready.', { x: 0.8, y: 1.1, w: 8, h: 0.4, fontSize: 16, color: COLORS.gray, fontFace: 'Arial' });

s6.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 0.8, y: 1.8, w: 11.5, h: 1.2, fill: { color: COLORS.cardBg }, line: { color: COLORS.border, width: 1 }, rectRadius: 0.1 });
s6.addText('An open-source JDBC driver that wraps your existing PostgreSQL or MySQL driver and adds cloud-native capabilities — without changing your application code.', { x: 1.1, y: 1.95, w: 11, h: 0.9, fontSize: 15, color: COLORS.gray, fontFace: 'Arial' });

const caps = [
  { title: 'Failover Handling', desc: 'Automatic reconnection on Aurora failover', highlight: false },
  { title: 'Read-Write Splitting', desc: 'Route reads to replicas automatically', highlight: false },
  { title: 'IAM Authentication', desc: 'Token-based auth without passwords', highlight: false },
  { title: '🆕 Remote Query Cache', desc: 'Transparent query caching in Valkey — v4.4.0', highlight: true },
];
caps.forEach((c, i) => {
  const x = 0.8 + (i % 2) * 6;
  const y = 3.4 + Math.floor(i / 2) * 1.8;
  const borderColor = c.highlight ? COLORS.indigo : COLORS.border;
  const bgColor = c.highlight ? COLORS.indigoBg + '44' : COLORS.cardBg;
  s6.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x, y, w: 5.5, h: 1.4, fill: { color: bgColor }, line: { color: borderColor, width: c.highlight ? 2 : 1 }, rectRadius: 0.1 });
  s6.addText(c.title, { x: x + 0.3, y: y + 0.2, w: 4.9, h: 0.4, fontSize: 14, color: c.highlight ? COLORS.indigoLight : COLORS.indigo, fontFace: 'Arial', bold: true });
  s6.addText(c.desc, { x: x + 0.3, y: y + 0.65, w: 4.9, h: 0.4, fontSize: 12, color: COLORS.gray, fontFace: 'Arial' });
});

// ── Slide 7: How the Plugin Works ──
let s7 = pptx.addSlide();
addBg(s7);
s7.addText('Remote Query Cache Plugin', { x: 0.8, y: 0.5, w: 10, h: 0.6, fontSize: 28, color: COLORS.white, fontFace: 'Arial', bold: true });
s7.addText('How it works — step by step', { x: 0.8, y: 1.1, w: 8, h: 0.4, fontSize: 16, color: COLORS.gray, fontFace: 'Arial' });

const steps = [
  { num: '1', title: 'You add a SQL hint', desc: '/* CACHE_PARAM(ttl=300s) */ SELECT * FROM products ...', color: COLORS.indigo },
  { num: '2', title: 'Wrapper intercepts the query', desc: 'Parses hint, extracts TTL, generates cache key', color: COLORS.indigo },
  { num: '3', title: 'Cache HIT → returns in <1ms', desc: 'Database never touched. Result from Valkey.', color: COLORS.emerald },
  { num: '4', title: 'Cache MISS → queries database', desc: 'Returns result, writes to Valkey async in background.', color: COLORS.red },
  { num: '5', title: 'Next identical query → instant hit', desc: 'Served from Valkey until TTL expires.', color: COLORS.emerald },
];
steps.forEach((step, i) => {
  const y = 1.7 + i * 1.05;
  s7.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 0.8, y, w: 11.5, h: 0.85, fill: { color: COLORS.cardBg }, line: { color: COLORS.border, width: 1 }, rectRadius: 0.08 });
  s7.addShape(pptx.shapes.OVAL, { x: 1.1, y: y + 0.15, w: 0.55, h: 0.55, fill: { color: step.color } });
  s7.addText(step.num, { x: 1.1, y: y + 0.15, w: 0.55, h: 0.55, fontSize: 14, color: COLORS.white, align: 'center', valign: 'middle', fontFace: 'Arial', bold: true });
  s7.addText(step.title, { x: 1.9, y: y + 0.1, w: 5, h: 0.35, fontSize: 13, color: COLORS.white, fontFace: 'Arial', bold: true });
  s7.addText(step.desc, { x: 1.9, y: y + 0.45, w: 10, h: 0.3, fontSize: 11, color: COLORS.gray, fontFace: 'Arial' });
});

// ── Slide 8: Code Changes ──
let s8 = pptx.addSlide();
addBg(s8);
s8.addText('The Code Changes', { x: 0.8, y: 0.5, w: 8, h: 0.6, fontSize: 28, color: COLORS.white, fontFace: 'Arial', bold: true });
s8.addText("Two changes. That's it.", { x: 0.8, y: 1.1, w: 8, h: 0.4, fontSize: 16, color: COLORS.gray, fontFace: 'Arial' });

// Before
s8.addText('BEFORE — Plain JDBC', { x: 0.8, y: 1.7, w: 5.5, h: 0.35, fontSize: 12, color: COLORS.red, fontFace: 'Arial', bold: true });
s8.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 0.8, y: 2.1, w: 5.5, h: 3.5, fill: { color: COLORS.cardBg }, line: { color: COLORS.red + '55', width: 1.5 }, rectRadius: 0.1 });
s8.addText(
  '// DataSource\ndriver = "org.postgresql.Driver"\nurl = "jdbc:postgresql://..."\n\n// Query\nSELECT * FROM products\nWHERE category = \'Laptops\'',
  { x: 1.1, y: 2.3, w: 5, h: 3, fontSize: 12, color: COLORS.red, fontFace: 'Courier New', lineSpacingMultiple: 1.3 }
);

// After
s8.addText('AFTER — With Cache', { x: 6.8, y: 1.7, w: 5.5, h: 0.35, fontSize: 12, color: COLORS.emerald, fontFace: 'Arial', bold: true });
s8.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 6.8, y: 2.1, w: 5.5, h: 3.5, fill: { color: COLORS.cardBg }, line: { color: COLORS.emerald + '55', width: 1.5 }, rectRadius: 0.1 });
s8.addText(
  '// DataSource\ndriver = "software.amazon.jdbc.Driver"\nurl = "jdbc:aws-wrapper:postgresql://..."\nwrapperPlugins = "remoteQueryCache"\ncacheEndpointAddrRw = "mycache:6379"\n\n// Query\n/* CACHE_PARAM(ttl=300s) */\nSELECT * FROM products\nWHERE category = \'Laptops\'',
  { x: 7.1, y: 2.3, w: 5, h: 3, fontSize: 12, color: COLORS.emerald, fontFace: 'Courier New', lineSpacingMultiple: 1.3 }
);

s8.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 2.5, y: 6, w: 8, h: 0.8, fill: { color: COLORS.indigoBg + '44' }, line: { color: COLORS.indigo + '55', width: 1 }, rectRadius: 0.08 });
s8.addText('Zero application logic changes. The agent code doesn\'t change at all.', { x: 2.7, y: 6.05, w: 7.6, h: 0.7, fontSize: 14, color: COLORS.indigoLight, align: 'center', fontFace: 'Arial', bold: true });

// ── Slide 9: Agent Flow Without Cache ──
let s9 = pptx.addSlide();
addBg(s9);
s9.addText('Agent Flow — Without Cache', { x: 0.8, y: 0.5, w: 10, h: 0.6, fontSize: 28, color: COLORS.white, fontFace: 'Arial', bold: true });

s9.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 0.8, y: 1.3, w: 11.5, h: 5.5, fill: { color: COLORS.cardBg }, line: { color: COLORS.border, width: 1 }, rectRadius: 0.12 });
s9.addText('👤  "Find me a gaming laptop under $1000"', { x: 1.2, y: 1.5, w: 10, h: 0.4, fontSize: 14, color: COLORS.gray, fontFace: 'Arial' });
s9.addText('🤖  Amazon Bedrock Agent (Claude Haiku) — 3,000ms', { x: 1.5, y: 2.2, w: 8, h: 0.35, fontSize: 13, color: COLORS.purple, fontFace: 'Arial', bold: true });
s9.addText('Reasoning, tool selection, response generation', { x: 1.8, y: 2.55, w: 8, h: 0.3, fontSize: 11, color: COLORS.gray, fontFace: 'Arial' });

// Tool call 1
s9.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 1.5, y: 3.2, w: 10.5, h: 1, fill: { color: COLORS.redBg + '33' }, line: { color: COLORS.red + '44', width: 1 }, rectRadius: 0.08 });
s9.addText('🔴  search_products(query="gaming laptop under $1000")', { x: 1.8, y: 3.3, w: 7, h: 0.4, fontSize: 12, color: COLORS.white, fontFace: 'Courier New' });
s9.addText('173ms → PostgreSQL', { x: 9.5, y: 3.3, w: 2.3, h: 0.4, fontSize: 13, color: COLORS.red, fontFace: 'Arial', bold: true, align: 'right' });

// Tool call 2
s9.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 1.5, y: 4.5, w: 10.5, h: 1, fill: { color: COLORS.redBg + '33' }, line: { color: COLORS.red + '44', width: 1 }, rectRadius: 0.08 });
s9.addText('🔴  browse_category(category="Laptops")', { x: 1.8, y: 4.6, w: 7, h: 0.4, fontSize: 12, color: COLORS.white, fontFace: 'Courier New' });
s9.addText('60ms → PostgreSQL', { x: 9.5, y: 4.6, w: 2.3, h: 0.4, fontSize: 13, color: COLORS.red, fontFace: 'Arial', bold: true, align: 'right' });

s9.addText('Total JDBC time:', { x: 1.5, y: 5.9, w: 4, h: 0.4, fontSize: 14, color: COLORS.gray, fontFace: 'Arial' });
s9.addText('211ms', { x: 8, y: 5.7, w: 3.5, h: 0.6, fontSize: 28, color: COLORS.red, fontFace: 'Arial', bold: true, align: 'right' });

// ── Slide 10: Agent Flow With Cache ──
let s10 = pptx.addSlide();
addBg(s10);
s10.addText('Agent Flow — With Cache (2nd Request)', { x: 0.8, y: 0.5, w: 10, h: 0.6, fontSize: 28, color: COLORS.white, fontFace: 'Arial', bold: true });

s10.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 0.8, y: 1.3, w: 11.5, h: 5.5, fill: { color: COLORS.cardBg }, line: { color: COLORS.border, width: 1 }, rectRadius: 0.12 });
s10.addText('👤  "Find me a gaming laptop under $1000"', { x: 1.2, y: 1.5, w: 10, h: 0.4, fontSize: 14, color: COLORS.gray, fontFace: 'Arial' });
s10.addText('🤖  Amazon Bedrock Agent (Claude Haiku) — 3,000ms', { x: 1.5, y: 2.2, w: 8, h: 0.35, fontSize: 13, color: COLORS.purple, fontFace: 'Arial', bold: true });
s10.addText('Same reasoning time (LLM doesn\'t change)', { x: 1.8, y: 2.55, w: 8, h: 0.3, fontSize: 11, color: COLORS.gray, fontFace: 'Arial' });

// Tool call 1 - HIT
s10.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 1.5, y: 3.2, w: 10.5, h: 1, fill: { color: COLORS.emeraldBg + '33' }, line: { color: COLORS.emerald + '44', width: 1 }, rectRadius: 0.08 });
s10.addText('🟢  search_products(query="gaming laptop under $1000")', { x: 1.8, y: 3.3, w: 7, h: 0.4, fontSize: 12, color: COLORS.white, fontFace: 'Courier New' });
s10.addText('4ms → Valkey HIT', { x: 9.5, y: 3.3, w: 2.3, h: 0.4, fontSize: 13, color: COLORS.emerald, fontFace: 'Arial', bold: true, align: 'right' });

// Tool call 2 - HIT
s10.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 1.5, y: 4.5, w: 10.5, h: 1, fill: { color: COLORS.emeraldBg + '33' }, line: { color: COLORS.emerald + '44', width: 1 }, rectRadius: 0.08 });
s10.addText('🟢  browse_category(category="Laptops")', { x: 1.8, y: 4.6, w: 7, h: 0.4, fontSize: 12, color: COLORS.white, fontFace: 'Courier New' });
s10.addText('6ms → Valkey HIT', { x: 9.5, y: 4.6, w: 2.3, h: 0.4, fontSize: 13, color: COLORS.emerald, fontFace: 'Arial', bold: true, align: 'right' });

s10.addText('Total JDBC time:', { x: 1.5, y: 5.9, w: 4, h: 0.4, fontSize: 14, color: COLORS.gray, fontFace: 'Arial' });
s10.addText('10ms', { x: 6, y: 5.7, w: 2, h: 0.6, fontSize: 28, color: COLORS.emerald, fontFace: 'Arial', bold: true, align: 'right' });
s10.addText('(was 211ms — 21x faster)', { x: 8.2, y: 5.85, w: 3.5, h: 0.4, fontSize: 13, color: COLORS.gray, fontFace: 'Arial' });

// ── Slide 11: Performance Results ──
let s11 = pptx.addSlide();
addBg(s11);
s11.addText('Performance Results', { x: 0.8, y: 0.5, w: 8, h: 0.6, fontSize: 28, color: COLORS.white, fontFace: 'Arial', bold: true });
s11.addText('Real numbers from the live demo', { x: 0.8, y: 1.1, w: 8, h: 0.4, fontSize: 16, color: COLORS.gray, fontFace: 'Arial' });

const tableRows = [
  ['Metric', 'Without Cache', 'With Cache', 'Improvement'],
  ['Category browse', '60ms', '6ms', '10x faster'],
  ['Product search', '173ms', '4ms', '43x faster'],
  ['Agent tool calls', '211ms', '10ms', '21x faster'],
  ['Cache hit rate', '0%', '50-100%', '—'],
  ['Database load', 'Every query', 'First only', 'up to 90%+ reduction'],
];
s11.addTable(tableRows, {
  x: 0.8, y: 1.8, w: 11.5,
  fontSize: 13, fontFace: 'Arial',
  color: COLORS.white,
  border: { type: 'solid', pt: 0.5, color: COLORS.border },
  colW: [3, 2.8, 2.8, 2.9],
  rowH: [0.5, 0.6, 0.6, 0.6, 0.6, 0.6],
  autoPage: false,
  headerRow: true,
  fill: { color: COLORS.cardBg },
  headerFill: { color: COLORS.border },
});
s11.addText(
  'Results are from this specific live demo configuration (local Valkey + PostgreSQL, warm cache). Actual numbers vary with workload, cache hit rate, data size, and network. Illustrative, not guaranteed.',
  { x: 0.8, y: 5.9, w: 11.5, h: 0.6, fontSize: 10, italic: true, color: COLORS.gray, fontFace: 'Arial' }
);

// ── Slide 12: Production Features ──
let s12 = pptx.addSlide();
addBg(s12);
s12.addText('Production-Ready Features', { x: 0.8, y: 0.5, w: 10, h: 0.6, fontSize: 28, color: COLORS.white, fontFace: 'Arial', bold: true });
s12.addText('Built into the plugin — no code required', { x: 0.8, y: 1.1, w: 8, h: 0.4, fontSize: 16, color: COLORS.gray, fontFace: 'Arial' });

const prodFeatures = [
  { title: 'Async Write-Back', desc: 'No added latency on cache miss' },
  { title: 'TTL Expiration', desc: 'Per-query configurable time-to-live' },
  { title: 'Connection Pooling', desc: 'Managed Valkey connection pool' },
  { title: 'TLS Encryption', desc: 'Secure cache connections' },
  { title: 'IAM Authentication', desc: 'ElastiCache IAM support' },
  { title: 'Health Monitoring', desc: 'HEALTHY → SUSPECT → DEGRADED' },
  { title: 'Graceful Degradation', desc: 'Falls back to DB if cache is down' },
  { title: 'Key Isolation', desc: 'Prefix-based multi-tenancy' },
  { title: 'OpenTelemetry Metrics', desc: 'Built-in observability' },
];
prodFeatures.forEach((f, i) => {
  const col = i % 3;
  const row = Math.floor(i / 3);
  const x = 0.8 + col * 4;
  const y = 1.8 + row * 1.7;
  s12.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x, y, w: 3.6, h: 1.3, fill: { color: COLORS.cardBg }, line: { color: COLORS.border, width: 1 }, rectRadius: 0.1 });
  s12.addText('✓', { x: x + 0.3, y: y + 0.2, w: 0.5, h: 0.4, fontSize: 18, color: COLORS.emerald, fontFace: 'Arial' });
  s12.addText(f.title, { x: x + 0.8, y: y + 0.2, w: 2.5, h: 0.4, fontSize: 14, color: COLORS.white, fontFace: 'Arial', bold: true });
  s12.addText(f.desc, { x: x + 0.8, y: y + 0.65, w: 2.5, h: 0.4, fontSize: 11, color: COLORS.gray, fontFace: 'Arial' });
});

// ── Slide 13: Getting Started ──
let s13 = pptx.addSlide();
addBg(s13);
s13.addText('Getting Started', { x: 0.8, y: 0.5, w: 8, h: 0.6, fontSize: 28, color: COLORS.white, fontFace: 'Arial', bold: true });
s13.addText('5 steps to production-ready caching', { x: 0.8, y: 1.1, w: 8, h: 0.4, fontSize: 16, color: COLORS.gray, fontFace: 'Arial' });

const gsSteps = [
  { title: 'Add the JDBC wrapper dependency', desc: 'aws-advanced-jdbc-wrapper v4.4.0 + valkey-glide + commons-pool2' },
  { title: 'Switch the JDBC driver and URL', desc: 'software.amazon.jdbc.Driver + jdbc:aws-wrapper:postgresql://' },
  { title: 'Add 3 properties', desc: 'wrapperPlugins, cacheEndpointAddrRw, cacheUseSSL' },
  { title: 'Add SQL hints to cacheable queries', desc: '/* CACHE_PARAM(ttl=300s) */ SELECT ...' },
  { title: 'Deploy — done', desc: 'Your queries are now cached in Valkey' },
];
gsSteps.forEach((step, i) => {
  const y = 1.7 + i * 1.05;
  const isLast = i === gsSteps.length - 1;
  const bgCol = isLast ? COLORS.emeraldBg + '44' : COLORS.cardBg;
  const borderCol = isLast ? COLORS.emerald : COLORS.border;
  s13.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x: 0.8, y, w: 11.5, h: 0.85, fill: { color: bgCol }, line: { color: borderCol, width: 1 }, rectRadius: 0.08 });
  s13.addShape(pptx.shapes.OVAL, { x: 1.1, y: y + 0.15, w: 0.55, h: 0.55, fill: { color: isLast ? COLORS.emerald : COLORS.indigo } });
  s13.addText(String(i + 1), { x: 1.1, y: y + 0.15, w: 0.55, h: 0.55, fontSize: 14, color: COLORS.white, align: 'center', valign: 'middle', fontFace: 'Arial', bold: true });
  s13.addText(step.title, { x: 1.9, y: y + 0.1, w: 5, h: 0.35, fontSize: 13, color: isLast ? COLORS.emerald : COLORS.white, fontFace: 'Arial', bold: true });
  s13.addText(step.desc, { x: 1.9, y: y + 0.45, w: 10, h: 0.3, fontSize: 11, color: COLORS.gray, fontFace: 'Arial' });
});

// ── Slide 14: Summary ──
let s14 = pptx.addSlide();
s14.background = { color: '0F172A' };
s14.addText('Two Code Changes.\nZero Complexity.\nReal Results.', { x: 1.5, y: 0.8, w: 10, h: 2.2, fontSize: 36, color: COLORS.white, fontFace: 'Arial', bold: true, align: 'center', lineSpacingMultiple: 1.2 });

const summary = [
  { icon: '🔌', title: 'AWS JDBC Wrapper', desc: 'Open-source, transparent, production-ready' },
  { icon: '⚡', title: 'ElastiCache for Valkey', desc: 'Fully managed, sub-millisecond, scalable' },
  { icon: '🤖', title: 'Agentic AI Ready', desc: 'Your agent gets faster without knowing it' },
];
summary.forEach((s, i) => {
  const x = 1 + i * 4;
  s14.addShape(pptx.shapes.ROUNDED_RECTANGLE, { x, y: 3.5, w: 3.5, h: 2, fill: { color: COLORS.cardBg + '88' }, line: { color: COLORS.border, width: 1 }, rectRadius: 0.12 });
  s14.addText(s.icon, { x, y: 3.7, w: 3.5, h: 0.5, fontSize: 24, align: 'center' });
  s14.addText(s.title, { x, y: 4.2, w: 3.5, h: 0.4, fontSize: 14, color: COLORS.white, align: 'center', fontFace: 'Arial', bold: true });
  s14.addText(s.desc, { x: x + 0.2, y: 4.65, w: 3.1, h: 0.5, fontSize: 11, color: COLORS.gray, align: 'center', fontFace: 'Arial' });
});

s14.addText('A low-friction path to better database performance for Java applications.', { x: 1.5, y: 5.8, w: 10, h: 0.5, fontSize: 16, color: COLORS.gray, align: 'center', fontFace: 'Arial' });
s14.addText('github.com/aws/aws-advanced-jdbc-wrapper', { x: 2, y: 6.5, w: 4.5, h: 0.4, fontSize: 12, color: COLORS.indigo, fontFace: 'Arial' });
s14.addText('Plugin Documentation →', { x: 7, y: 6.5, w: 4, h: 0.4, fontSize: 12, color: COLORS.indigo, fontFace: 'Arial', align: 'right' });

// ── Generate ──
pptx.writeFile({ fileName: 'ShopMind-JDBC-Caching-Demo.pptx' })
  .then(() => console.log('✅ Presentation saved: ShopMind-JDBC-Caching-Demo.pptx'))
  .catch(err => console.error('Error:', err));
