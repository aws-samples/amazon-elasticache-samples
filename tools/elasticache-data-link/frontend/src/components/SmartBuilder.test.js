import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { formatInline } from './SmartBuilder';

jest.mock('axios');

const html = (line) => renderToStaticMarkup(<span>{formatInline(line)}</span>);

test('bold and inline code still render as elements', () => {
  expect(html('use **ecdl apply** then `ecdl status`'))
    .toBe('<span>use <strong>ecdl apply</strong> then '
      + '<code style="background:#f0f0f0;padding:1px 5px;border-radius:3px;font-size:12px">'
      + 'ecdl status</code></span>');
});

test('markup in a message is shown as text, never inserted as HTML', () => {
  const out = html('<img src=x onerror="alert(1)"> and **<b>x</b>**');
  expect(out).not.toContain('<img');
  expect(out).not.toContain('<b>');
  expect(out).toContain('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
  expect(out).toContain('<strong>&lt;b&gt;x&lt;/b&gt;</strong>');
});

test('plain text passes through unchanged', () => {
  expect(html('no formatting here')).toBe('<span>no formatting here</span>');
});
