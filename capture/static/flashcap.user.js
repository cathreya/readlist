// ==UserScript==
// @name         Flashcap — capture a flashcard
// @namespace    local.flashcap
// @version      1.9.1
// @description  Cmd/Ctrl+Shift+K write a card · Shift+L annotate (LLM reuses tags) · Shift+E explain.
// @author       local
// @match        *://*/*
// @run-at       document-idle
// @grant        GM.xmlHttpRequest
// @grant        GM.registerMenuCommand
// @connect      127.0.0.1
// @connect      localhost
// @connect      100.86.133.33
// @connect      hermes.taila17c7c.ts.net
// @noframes
// ==/UserScript==

(function () {
  'use strict';

  const BASE = '__FLASHCAP_BASE__';
  const IS_MAC = /mac|iphone|ipad|ipod/i.test(navigator.platform || navigator.userAgent || '');
  const HOTKEY_LABEL = IS_MAC ? '⌘⇧K' : 'Ctrl+Shift+K';
  const NOTE_LABEL = IS_MAC ? '⌘⇧L' : 'Ctrl+Shift+L';
  const EXPLAIN_LABEL = IS_MAC ? '⌘⇧E' : 'Ctrl+Shift+E';
  const MAX_CONTEXT = 7000;
  // Shift+right-click always falls through to Firefox's native menu.
  const ENABLE_CONTEXT_MENU = true;

  // Only the top frame captures; iframes are usually chrome, embeds, or ads.
  try {
    if (window.top !== window.self) return;
  } catch (_) {
    /* cross-origin frame; leave it alone */
  }

  // Don't run on Flashcap's own UI (works whether it was opened via
  // 127.0.0.1 or localhost).
  try {
    const own = new URL(BASE);
    const localHost = location.hostname === '127.0.0.1' || location.hostname === 'localhost';
    if (location.port === own.port && localHost) return;
  } catch (_) {
    /* ignore malformed BASE */
  }

  if (document.documentElement.dataset.flashcapInstalled === '1') return;
  document.documentElement.dataset.flashcapInstalled = '1';

  let ui = null;

  // -------------------------------------------------------------------
  // DOM helpers
  // -------------------------------------------------------------------
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();

  const clip = (value, limit) => {
    const text = String(value || '');
    if (text.length <= limit) return text;
    const cut = text.slice(0, limit);
    const at = cut.lastIndexOf(' ');
    return (at > limit * 0.6 ? cut.slice(0, at) : cut).trim() + '…';
  };

  const textOf = (element) =>
    clean(element ? element.innerText || element.textContent || '' : '');

  const tag = (name, props = {}, ...children) => {
    const node = document.createElement(name);
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'style') node.style.cssText = value;
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else {
        node.setAttribute(key, String(value));
      }
    }
    for (const child of children.flat(3)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  };

  const stopPageKeyboard = (host, close) => {
    // Shadow-DOM key events are composed and bubble to the page. YouTube's
    // shortcuts listen on document/window, so stop them at the modal host;
    // otherwise typing "k" or space in a textarea controls the video.
    host.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
      }
      event.stopPropagation();
    });
    host.addEventListener('keypress', (event) => event.stopPropagation());
    host.addEventListener('keyup', (event) => event.stopPropagation());
  };

  // -------------------------------------------------------------------
  // Page context extraction
  // -------------------------------------------------------------------
  const BLOCK_SELECTOR =
    'p, li, blockquote, dd, td, th, pre, figcaption, h1, h2, h3, h4, h5, h6';
  const ROOT_SELECTOR =
    'article, main, [role="main"], .post, .entry-content, .markdown-body, ' +
    '.article-body, .post-content, .story-body, .article__body, #content';

  const inPageChrome = (element) =>
    Boolean(
      element &&
        element.closest(
          'nav, header, footer, aside, form, button, [role="navigation"], ' +
            '[role="complementary"], .sidebar, .menu, #comments, .comments'
        )
    );

  const isVisible = (element) => {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    return (
      rect.width > 0 &&
      rect.height > 0 &&
      rect.bottom > 0 &&
      rect.top < window.innerHeight
    );
  };

  const closestBlock = (element) => {
    if (!element || !element.closest) return null;
    return (
      element.closest(BLOCK_SELECTOR) ||
      element.closest(ROOT_SELECTOR) ||
      element.closest('div') ||
      document.body
    );
  };

  const articleRoot = (element) => {
    return (
      (element && element.closest(ROOT_SELECTOR)) ||
      document.querySelector(ROOT_SELECTOR) ||
      document.querySelector('article, main, [role="main"]') ||
      document.body
    );
  };

  const selectionInfo = () => {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
      return { text: '', element: null };
    }
    const text = clean(selection.toString());
    if (!text) return { text: '', element: null };

    const range = selection.getRangeAt(0);
    let node = range.commonAncestorContainer;
    let element = node && node.nodeType === 1 ? node : node && node.parentElement;
    if (element && inPageChrome(element)) element = null;
    return { text, element };
  };

  const blockFromPoint = (x, y) => {
    // caretPositionFromPoint finds the paragraph even when the point is in
    // whitespace; elementFromPoint is the fallback.
    let element = null;
    if (document.caretPositionFromPoint) {
      const position = document.caretPositionFromPoint(x, y);
      if (position && position.offsetNode) {
        element =
          position.offsetNode.nodeType === 1
            ? position.offsetNode
            : position.offsetNode.parentElement;
      }
    } else if (document.caretRangeFromPoint) {
      const range = document.caretRangeFromPoint(x, y);
      if (range) {
        element =
          range.startContainer.nodeType === 1
            ? range.startContainer
            : range.startContainer.parentElement;
      }
    }
    if (!element) element = document.elementFromPoint(x, y);
    if (!element || inPageChrome(element)) return null;
    const block = closestBlock(element);
    return block && textOf(block).length >= 20 ? block : null;
  };

  const bestVisibleParagraph = () => {
    const nodes = document.querySelectorAll(
      'article p, main p, [role="main"] p, .post p, .entry-content p, ' +
        '.markdown-body p, .article-body p, .post-content p, .story-body p'
    );
    const viewportCenter = window.innerHeight / 2;
    let best = null;
    let bestScore = 0;

    for (const node of nodes) {
      const text = textOf(node);
      if (text.length < 40 || !isVisible(node) || inPageChrome(node)) continue;
      const rect = node.getBoundingClientRect();
      const visible =
        Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0);
      const center = (rect.top + rect.bottom) / 2;
      const score = visible * Math.min(text.length, 1200) / (1 + Math.abs(center - viewportCenter) / 200);
      if (score > bestScore) {
        bestScore = score;
        best = node;
      }
    }
    return best;
  };

  const focusElement = () => {
    const x = window.innerWidth / 2;
    for (const fraction of [0.45, 0.55, 0.35, 0.65]) {
      const block = blockFromPoint(x, window.innerHeight * fraction);
      if (block) return block;
    }
    return bestVisibleParagraph();
  };

  const headingTrail = (root, block) => {
    const headings = [];
    for (const heading of root.querySelectorAll('h1, h2, h3, h4')) {
      if (heading === block) continue;
      // Is the heading before the block in document order?
      if (heading.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING) {
        const text = textOf(heading);
        if (text) headings.push(text);
      }
    }
    return clip([...new Set(headings)].slice(-3).join(' › '), 500);
  };

  const contextAround = (root, block, focusText, selection) => {
    const rootText = textOf(root);
    if (!block) return clip(rootText, MAX_CONTEXT);
    const needle = (selection || focusText).slice(0, 140);
    const index = needle ? rootText.indexOf(needle) : -1;

    if (index >= 0) {
      const start = Math.max(0, index - 1400);
      const end = Math.min(
        rootText.length,
        index + Math.max(selection.length, focusText.length) + 2000
      );
      return clip(rootText.slice(start, end), MAX_CONTEXT);
    }

    // The block's text is not in the root's flat text (inline markup,
    // collapsed whitespace, or an unusual layout). Use siblings instead.
    const parts = [];
    let previous = block.previousElementSibling;
    let count = 0;
    while (previous && count < 2 && parts.join(' ').length < 1200) {
      const text = textOf(previous);
      if (text) {
        parts.unshift(text);
        count += 1;
      }
      previous = previous.previousElementSibling;
    }

    parts.push(focusText);

    let next = block.nextElementSibling;
    count = 0;
    while (next && count < 2 && parts.join(' ').length < 5000) {
      const text = textOf(next);
      if (text) {
        parts.push(text);
        count += 1;
      }
      next = next.nextElementSibling;
    }
    return clip(parts.filter(Boolean).join('\n\n'), MAX_CONTEXT);
  };

  const metaContent = (...names) => {
    for (const name of names) {
      const element = document.querySelector(
        `meta[property="${name}"], meta[name="${name}"], meta[itemprop="${name}"]`
      );
      if (!element) continue;
      const value = clean(element.getAttribute('content') || element.getAttribute('value'));
      if (value) return value;
    }
    return '';
  };

  const cleanUrl = (value) => {
    try {
      const url = new URL(value);
      url.hash = '';
      for (const key of [
        'utm_source',
        'utm_medium',
        'utm_campaign',
        'utm_term',
        'utm_content',
        'fbclid',
        'gclid',
      ]) {
        url.searchParams.delete(key);
      }
      return url.href;
    } catch (_) {
      return value || '';
    }
  };

  const isYouTube = () =>
    /(^|\.)youtube\.com$/i.test(location.hostname) ||
    /(^|\.)youtu\.be$/i.test(location.hostname);

  const youtubeTranscript = () => {
    const segments = [...document.querySelectorAll('ytd-transcript-segment-renderer')];
    if (!segments.length) return '';

    const video = document.querySelector('video');
    const now = video && Number.isFinite(video.currentTime) ? video.currentTime : 0;
    const rows = [];

    for (const segment of segments) {
      const stamp = textOf(segment.querySelector('.segment-timestamp'));
      const text =
        textOf(segment.querySelector('#segment-text')) || textOf(segment);
      if (!text) continue;
      const seconds = stamp
        ? stamp.split(':').reduce((total, part) => total * 60 + Number(part || 0), 0)
        : 0;
      rows.push({ seconds, text });
    }
    if (!rows.length) return '';

    let index = rows.findIndex((row) => row.seconds >= now);
    if (index < 0) index = rows.length - 1;
    return clip(
      rows
        .slice(Math.max(0, index - 8), index + 10)
        .map((row) => row.text)
        .join(' '),
      3000
    );
  };

  const collect = () => {
    const { text: selection, element: selectionElement } = selectionInfo();
    const anchor = selectionElement || focusElement() || document.body;
    const block = closestBlock(anchor) || document.body;
    const root = articleRoot(block);
    let focus = clip(textOf(block), 2400);
    let context = contextAround(root, block, focus, selection);

    const title =
      metaContent('og:title', 'twitter:title') ||
      textOf(document.querySelector('h1')) ||
      clean(document.title);
    const url = cleanUrl(
      (document.querySelector('link[rel="canonical"]') || {}).href || location.href
    );
    const site =
      metaContent('og:site_name', 'application-name') ||
      location.hostname.replace(/^www\./, '');
    const author = metaContent(
      'author',
      'article:author',
      'twitter:creator',
      'byl'
    );
    const description = metaContent(
      'description',
      'og:description',
      'twitter:description'
    );

    let sourceType = 'web';
    if (isYouTube()) {
      sourceType = 'youtube';
      const transcript = youtubeTranscript();
      const videoDescription = clip(textOf(document.querySelector('#description-inline-expander')), 2500);
      if (transcript) {
        if (!selection) focus = transcript.slice(0, 2400);
        context = clip([videoDescription, context, transcript].filter(Boolean).join('\n\n'), MAX_CONTEXT);
      } else if (!selection && focus.length < 80) {
        focus = videoDescription.slice(0, 1200);
        context = clip([videoDescription, description].filter(Boolean).join('\n\n'), MAX_CONTEXT);
      }
    }

    return {
      selection,
      focus,
      context,
      page_title: clip(title, 400),
      url,
      site: clip(site, 200),
      author: clip(author, 300),
      headings: clip(headingTrail(root, block), 600),
      description: clip(description, 1500),
      source_type: sourceType,
      lang: document.documentElement.lang || '',
    };
  };

  // -------------------------------------------------------------------
  // HTTP
  // -------------------------------------------------------------------
  const request = (method, path, body) =>
    new Promise((resolve, reject) => {
      const url = BASE + path;
      const payload = body ? JSON.stringify(body) : null;

      if (typeof GM !== 'undefined' && GM.xmlHttpRequest) {
        GM.xmlHttpRequest({
          method,
          url,
          headers: { 'Content-Type': 'application/json' },
          data: payload,
          timeout: 80000,
          onload(response) {
            let data = {};
            try {
              data = JSON.parse(response.responseText);
            } catch (_) {
              /* fall through */
            }
            if (response.status >= 200 && response.status < 300) {
              resolve(data);
            } else {
              reject(new Error(data.error || `HTTP ${response.status}`));
            }
          },
          onerror() {
            reject(new Error('Flashcap server is not running'));
          },
          ontimeout() {
            reject(new Error('Flashcap timed out'));
          },
        });
        return;
      }

      fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: payload,
      })
        .then(async (response) => {
          let data = {};
          try {
            data = await response.json();
          } catch (_) {
            /* fall through */
          }
          if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
          resolve(data);
        })
        .catch((error) => reject(error));
    });

  // -------------------------------------------------------------------
  // Toast UI
  // -------------------------------------------------------------------
  const CSS = `
    .panel {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 13px;
      line-height: 1.5;
      color: #f2f0e9;
      background: #191a16;
      border: 1px solid #35382e;
      border-radius: 14px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, .45), 0 2px 8px rgba(0, 0, 0, .25);
      padding: 13px 14px;
      box-sizing: border-box;
      animation: fc-in .16s ease-out;
    }
    @keyframes fc-in {
      from { opacity: 0; transform: translateY(6px) scale(.985); }
      to   { opacity: 1; transform: translateY(0) scale(1); }
    }
    @keyframes fc-spin { to { transform: rotate(360deg); } }
    .head { display: flex; align-items: center; gap: 8px; min-height: 20px; }
    .head strong { font-size: 13px; font-weight: 650; letter-spacing: .01em; }
    .spacer { flex: 1; }
    .muted { color: #a5aa9b; }
    .sub { color: #a5aa9b; font-size: 11.5px; margin-top: 3px; overflow-wrap: anywhere; }
    .sub code { color: #d5d9c9; background: #24261f; padding: 1px 4px; border-radius: 4px; font-size: 11px; }
    .spinner {
      width: 13px; height: 13px; flex: 0 0 auto; border-radius: 50%;
      border: 2px solid #464b3c; border-top-color: #9ad9ad;
      animation: fc-spin .8s linear infinite;
    }
    .check {
      width: 18px; height: 18px; flex: 0 0 auto; border-radius: 50%;
      background: #2e5c3e; color: #bdf3cc; display: grid; place-items: center;
      font-size: 11px; font-weight: 700;
    }
    .bang {
      width: 18px; height: 18px; flex: 0 0 auto; border-radius: 50%;
      background: #5c2f2a; color: #f3b7ad; display: grid; place-items: center;
      font-size: 11px; font-weight: 700;
    }
    .close {
      appearance: none; border: 0; background: transparent; color: #8d9384;
      font-size: 18px; line-height: 1; cursor: pointer; padding: 0 2px;
      border-radius: 4px;
    }
    .close:hover { color: #fff; }
    .catrow { display: flex; align-items: center; gap: 7px; margin: 10px 0 5px; flex-wrap: wrap; }
    .cat {
      background: #26332a; color: #a9e0b7; border: 1px solid #36533f;
      border-radius: 20px; padding: 1px 8px; font-size: 10.5px;
      text-transform: lowercase; white-space: nowrap;
    }
    .src { color: #8b9182; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .front {
      font-size: 13.5px; font-weight: 600; color: #f5f2e9; margin: 5px 0 3px;
      display: -webkit-box; -webkit-line-clamp: 5; -webkit-box-orient: vertical;
      overflow: hidden; overflow-wrap: anywhere;
    }
    .back {
      color: #c2c7b8; font-size: 12px; margin-top: 2px;
      display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
      overflow: hidden; overflow-wrap: anywhere;
    }
    .tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 7px; }
    .tag {
      font-size: 10.5px; color: #b3b9a8; background: #23251e;
      border: 1px solid #34382c; border-radius: 5px; padding: 0 5px;
    }
    .warn {
      color: #e5c47d; background: #2a2518; border: 1px solid #494026;
      border-radius: 7px; padding: 5px 7px; font-size: 11px; margin-top: 8px;
      display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .actions { display: flex; gap: 6px; align-items: center; margin-top: 11px; }
    button.act {
      appearance: none; font: inherit; font-size: 11.5px; color: #e7e4db;
      background: #24261f; border: 1px solid #3a3e31; border-radius: 7px;
      padding: 4px 9px; cursor: pointer;
    }
    button.act:hover { background: #2d3026; border-color: #4d5341; }
    button.act.primary { background: #2f5f41; border-color: #3b7451; color: #eafff0; }
    button.act.primary:hover { background: #376e4b; }
    a.act-link {
      color: #a9b0a0; font-size: 11.5px; margin-left: auto; text-decoration: none;
      border-bottom: 1px dotted #5a6052; padding-bottom: 1px;
    }
    a.act-link:hover { color: #fff; border-color: #fff; }
    .field { margin-top: 9px; }
    .field label { display: block; color: #9da391; font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 3px; }
    textarea, input {
      box-sizing: border-box; width: 100%; font: inherit; font-size: 12px;
      color: #f0eee6; background: #10110e; border: 1px solid #3a3e31;
      border-radius: 7px; padding: 6px 7px; resize: vertical;
    }
    textarea:focus, input:focus { outline: 1px solid #5b916d; border-color: #5b916d; }
    .row2 { display: grid; grid-template-columns: 1fr 1.4fr; gap: 7px; }
    .err-detail { color: #c9cec0; font-size: 11px; margin-top: 5px; overflow-wrap: anywhere; }
    .removed { color: #a5aa9b; font-size: 12px; margin-top: 6px; }

    .modal {
      width: min(560px, calc(100vw - 32px));
      max-height: min(86vh, 760px);
      overflow: auto;
      padding: 16px 17px;
    }
    .modal .head { margin-bottom: 2px; }
    .modal .sub { margin-bottom: 9px; }
    .modal-quote {
      margin: 8px 0 2px;
      padding: 8px 10px;
      border-left: 2px solid #5b916d;
      border-radius: 0 7px 7px 0;
      background: #10110e;
      color: #cbd0c2;
      font-family: ui-serif, Georgia, "Iowan Old Style", serif;
      font-size: 12.5px;
      line-height: 1.55;
      max-height: 150px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .modal textarea { min-height: 96px; }
    .modal .field { margin-top: 10px; }
    .modal-hint { color: #8b9182; font-size: 10.5px; margin-top: 8px; line-height: 1.45; }
    .modal-error { color: #f3b7ad; font-size: 11px; margin-top: 7px; min-height: 1em; }
    .modal-actions { display: flex; gap: 6px; align-items: center; margin-top: 11px; flex-wrap: wrap; }
    .explain-text {
      margin: 8px 0 2px;
      padding: 9px 10px;
      border: 1px solid #2f3328;
      border-radius: 8px;
      background: #10110e;
      color: #d8dccf;
      font-size: 12.5px;
      line-height: 1.62;
      white-space: pre-wrap;
      max-height: 300px;
      overflow: auto;
    }
    .label-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 3px;
    }
    .label-row label { margin-bottom: 0; }
    .label-row .act {
      padding: 2px 7px;
      font-size: 10.5px;
      color: #a9e0b7;
    }
    button.act:disabled { opacity: .55; cursor: wait; }

    .ctx-backdrop { position: fixed; inset: 0; }
    .ctx {
      position: fixed;
      min-width: 220px;
      padding: 5px;
      background: #191a16;
      border: 1px solid #35382e;
      border-radius: 10px;
      box-shadow: 0 18px 50px rgba(0, 0, 0, .45);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 12.5px;
      line-height: 1.35;
      color: #f2f0e9;
    }
    .ctx-title {
      padding: 3px 9px 6px;
      color: #8b9182;
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: .08em;
    }
    .ctx-item {
      display: block;
      width: 100%;
      padding: 7px 9px;
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: #e7e4db;
      font: inherit;
      text-align: left;
      cursor: pointer;
    }
    .ctx-item:hover { background: #2d3026; }
    .ctx-item.primary { color: #b9e7c7; }
    .ctx-sep { height: 1px; margin: 4px 5px; background: #35382e; }
  `;

  const injectStyles = (root) => {
    if (typeof CSSStyleSheet === 'function' && 'adoptedStyleSheets' in root) {
      try {
        const sheet = new CSSStyleSheet();
        sheet.replaceSync(CSS);
        root.adoptedStyleSheets = [sheet];
        return;
      } catch (_) {
        /* fall through */
      }
    }
    const style = document.createElement('style');
    style.textContent = CSS;
    root.append(style);
  };

  const panelHost = () => {
    if (ui && document.documentElement.contains(ui.host)) return ui;

    const host = document.createElement('div');
    host.id = 'flashcap-host';
    host.style.cssText =
      'all: initial; position: fixed; z-index: 2147483647; right: 18px; ' +
      'bottom: 18px; width: min(380px, calc(100vw - 36px));';
    const root = host.attachShadow({ mode: 'open' });
    injectStyles(root);
    const panel = tag('div', { class: 'panel', role: 'status', 'aria-live': 'polite' });
    root.append(panel);
    document.documentElement.appendChild(host);

    ui = { host, root, panel, timer: null, dismiss: null, hover: false };
    panel.addEventListener('mouseenter', () => {
      ui.hover = true;
      if (ui.dismiss) {
        clearTimeout(ui.dismiss);
        ui.dismiss = null;
      }
    });
    panel.addEventListener('mouseleave', () => {
      ui.hover = false;
      if (ui.mode === 'saved') scheduleDismiss(ui, 6000);
    });
    return ui;
  };

  const clearTimers = (target) => {
    if (!target) return;
    if (target.timer) {
      clearInterval(target.timer);
      target.timer = null;
    }
    if (target.dismiss) {
      clearTimeout(target.dismiss);
      target.dismiss = null;
    }
  };

  const scheduleDismiss = (target, milliseconds) => {
    if (!target || target.hover) return;
    if (target.dismiss) clearTimeout(target.dismiss);
    target.dismiss = setTimeout(() => {
      if (ui === target) hideUI();
    }, milliseconds);
  };

  const hideUI = () => {
    if (!ui) return;
    clearTimers(ui);
    ui.host.remove();
    ui = null;
  };

  const head = (icon, title, target) =>
    tag(
      'div',
      { class: 'head' },
      icon,
      tag('strong', { text: title }),
      tag('div', { class: 'spacer' }),
      tag('button', {
        class: 'close',
        title: 'Dismiss',
        'aria-label': 'Dismiss',
        text: '×',
        onclick: () => hideUI(),
      })
    );

  const showError = (target, error, kind = 'card') => {
    const current = target && ui === target ? target : panelHost();
    clearTimers(current);
    current.mode = 'error';
    const message = error.message || String(error);
    const offline = /not running|unreachable|failed to fetch|network|timed out/i.test(message);

    const children = [
      head(
        tag('span', { class: 'bang', text: '!' }),
        kind === 'note' ? "Couldn't save note" : "Couldn't capture"
      ),
      tag('div', { class: 'err-detail', text: message }),
    ];
    if (offline) {
      children.push(
        tag('div', { class: 'sub' }, 'Start the server: ', tag('code', { text: 'python -m capture.server' }))
      );
    }
    children.push(
      tag(
        'div',
        { class: 'actions' },
        tag('button', {
          class: 'act primary',
          text: 'Retry',
          onclick: () => (kind === 'note' ? annotateFromPage() : captureCard()),
        }),
        tag('button', { class: 'act', text: 'Dismiss', onclick: () => hideUI() })
      )
    );

    current.panel.replaceChildren(...children);
    scheduleDismiss(current, 15000);
  };

  const showSaved = (target, card, warning) => {
    if (!target || ui !== target) return;
    clearTimers(target);
    target.mode = 'saved';
    target.card = card;

    const children = [
      head(tag('span', { class: 'check', text: '✓' }), 'Flashcard saved'),
      tag(
        'div',
        { class: 'catrow' },
        tag('span', { class: 'cat', text: (card.category || 'other').replace(/-/g, ' ') }),
        tag('span', { class: 'src', text: card.source_site || card.source_title || '' })
      ),
      tag('div', { class: 'front', text: card.front }),
      tag('div', { class: 'back', text: card.back }),
    ];

    if (card.tags && card.tags.length) {
      children.push(
        tag(
          'div',
          { class: 'tags' },
          ...card.tags.map((value) => tag('span', { class: 'tag', text: `#${value}` }))
        )
      );
    }

    if (warning) {
      children.push(tag('div', { class: 'warn', text: warning }));
    }

    children.push(
      tag(
        'div',
        { class: 'actions' },
        tag('button', {
          class: 'act',
          text: 'Undo',
          onclick: () => undoCard(target, card.id),
        }),
        tag('button', {
          class: 'act primary',
          text: 'Edit',
          onclick: () => showEdit(target, card),
        }),
        tag('a', {
          class: 'act-link',
          href: `${BASE}/`,
          target: '_blank',
          rel: 'noopener',
          text: 'Open dashboard ↗',
        })
      )
    );

    target.panel.replaceChildren(...children);
    scheduleDismiss(target, 15000);
  };

  const showNoteSaved = (target, note) => {
    if (!target || ui !== target) return;
    clearTimers(target);
    target.mode = 'note';

    const path = note.path || '';
    const openUrl = note.queued ? '' : note.open_url || '';
    const label = note.already
      ? 'Already recorded'
      : note.queued
        ? 'Annotation queued'
        : note.appended
          ? 'Annotation added'
          : 'Annotation saved';

    target.panel.replaceChildren(
      head(tag('span', { class: 'check', text: '✓' }), label),
      tag('div', {
        class: 'sub',
        text: path
          ? `${note.vault || ''}/${path}${note.queued ? ' · waiting for your Mac' : ''}`
          : 'Saved in Obsidian',
      }),
      tag(
        'div',
        { class: 'actions' },
        tag('button', { class: 'act', text: 'Done', onclick: () => hideUI() }),
        openUrl
          ? tag('a', {
              class: 'act-link',
              href: openUrl,
              text: 'Open note ↗',
            })
          : null
      )
    );
    scheduleDismiss(target, 15000);
  };

  const undoCard = async (target, cardId) => {
    try {
      await request('DELETE', `/api/cards/${cardId}`);
      if (ui !== target) return;
      clearTimers(target);
      target.mode = 'removed';
      target.panel.replaceChildren(
        head(tag('span', { class: 'check', text: '✓' }), 'Card removed'),
        tag('div', { class: 'removed', text: 'The capture was discarded.' })
      );
      scheduleDismiss(target, 3000);
    } catch (error) {
      showError(target, error);
    }
  };

  const showEdit = (target, card) => {
    if (!target || ui !== target) return;
    clearTimers(target);
    target.mode = 'edit';

    const front = tag('textarea', { rows: 2 });
    front.value = card.front || '';
    const back = tag('textarea', { rows: 3 });
    back.value = card.back || '';
    const category = tag('input', { type: 'text', list: 'flashcap-categories' });
    category.value = card.category || '';
    const tags = tag('input', { type: 'text' });
    tags.value = (card.tags || []).join(', ');

    const datalist = tag('datalist', { id: 'flashcap-categories' });
    for (const choice of [
      'machine-learning', 'algorithms', 'systems', 'math', 'physics',
      'neuroscience', 'programming', 'philosophy', 'history', 'economics',
      'biology', 'design', 'career', 'other',
    ]) {
      datalist.append(tag('option', { value: choice }));
    }

    const save = tag('button', {
      class: 'act primary',
      text: 'Save',
      onclick: () => saveEdit(target, card, { front, back, category, tags, save }),
    });

    target.panel.replaceChildren(
      head(tag('span', { class: 'check', text: '✎' }), 'Edit flashcard'),
      tag('div', { class: 'field' }, tag('label', { text: 'Front' }), front),
      tag('div', { class: 'field' }, tag('label', { text: 'Back' }), back),
      tag(
        'div',
        { class: 'field row2' },
        tag('div', {}, tag('label', { text: 'Category' }), category, datalist),
        tag('div', {}, tag('label', { text: 'Tags' }), tags)
      ),
      tag(
        'div',
        { class: 'actions' },
        save,
        tag('button', { class: 'act', text: 'Cancel', onclick: () => showSaved(target, card) })
      )
    );

    front.focus();
    front.setSelectionRange(front.value.length, front.value.length);

    for (const field of [front, back, category, tags]) {
      field.addEventListener('keydown', (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
          event.preventDefault();
          saveEdit(target, card, { front, back, category, tags, save });
        }
      });
    }
  };

  const saveEdit = async (target, card, fields) => {
    if (fields.save.disabled) return;
    fields.save.disabled = true;
    fields.save.textContent = 'Saving…';
    try {
      const data = await request('PATCH', `/api/cards/${card.id}`, {
        front: fields.front.value,
        back: fields.back.value,
        category: fields.category.value,
        tags: fields.tags.value,
      });
      if (ui === target) showSaved(target, data.card);
    } catch (error) {
      fields.save.disabled = false;
      fields.save.textContent = 'Save';
      if (ui !== target) return;
      const existing = target.panel.querySelector('.edit-error');
      if (existing) existing.remove();
      const warning = tag('div', {
        class: 'warn edit-error',
        text: error.message || 'Could not save the card',
      });
      const actions = target.panel.querySelector('.actions');
      if (actions) actions.before(warning);
      else target.panel.append(warning);
    }
  };

  // -------------------------------------------------------------------
  // Manual card editor (no LLM)
  // -------------------------------------------------------------------
  let cardModal = null;

  const closeCardEditor = () => {
    if (!cardModal) return;
    cardModal.host.remove();
    cardModal = null;
  };

  const saveCard = async () => {
    if (!cardModal || cardModal.saving) return;
    const current = cardModal;
    const front = current.front.value.trim();
    const back = current.back.value.trim();
    if (!front || !back) {
      current.error.textContent = 'Front and back are required.';
      return;
    }

    current.saving = true;
    current.save.disabled = true;
    current.save.textContent = 'Saving…';
    current.error.textContent = '';

    try {
      const data = await request('POST', '/api/capture', {
        ...current.payload,
        front,
        back,
        category: current.category.value.trim(),
        tags: current.tags.value.trim(),
      });
      closeCardEditor();
      showSaved(panelHost(), data.card, null);
    } catch (error) {
      if (cardModal !== current) return;
      current.saving = false;
      current.save.disabled = false;
      current.save.textContent = 'Save card';
      current.error.textContent = error.message || 'Could not save the card';
    }
  };

  const suggestMetadata = async (current, payload, force = false) => {
    if (!current) return;
    current.suggestStatus.textContent = 'Suggesting category and tags…';
    try {
      const data = await request('POST', '/api/suggest', payload);
      if (cardModal !== current) return;
      const suggestedTags = data.tags || [];
      if (!current.categoryTouched || force) {
        current.category.value = data.category || '';
      }
      if (suggestedTags.length && (!current.tagsTouched || force)) {
        current.tags.value = suggestedTags.join(', ');
      }
      current.suggestStatus.textContent = suggestedTags.length
        ? '✦ Reused existing tags — edit anything.'
        : 'No existing tag fits — add one manually if needed.';
    } catch (error) {
      if (cardModal !== current) return;
      current.suggestStatus.textContent =
        'No suggestions — fill category and tags yourself.';
    }
  };

  const openCardEditor = (payload) => {
    closeCardEditor();

    const host = document.createElement('div');
    host.id = 'flashcap-card-host';
    host.style.cssText =
      'all: initial; position: fixed; inset: 0; z-index: 2147483647; ' +
      'display: flex; align-items: center; justify-content: center; ' +
      'padding: 16px; background: rgba(10, 10, 8, .42);';
    const root = host.attachShadow({ mode: 'open' });
    injectStyles(root);

    const quoteText = payload.selection || payload.focus || payload.context || '';
    const sourceText = [payload.page_title, payload.site].filter(Boolean).join(' · ');
    const front = tag('textarea', {
      rows: 3,
      placeholder: 'Question or prompt',
    });
    const back = tag('textarea', {
      rows: 4,
      placeholder: 'Answer',
    });
    const category = tag('input', {
      type: 'text',
      list: 'flashcap-card-categories',
      placeholder: 'category',
    });
    const tags = tag('input', {
      type: 'text',
      placeholder: 'comma, separated',
    });
    const error = tag('div', { class: 'modal-error' });
    const suggestStatus = tag('div', {
      class: 'modal-hint',
      text: 'Suggesting category and tags…',
    });
    const suggestBtn = tag('button', {
      class: 'act',
      type: 'button',
      text: 'Suggest metadata',
      onclick: () => suggestMetadata(cardModal, payload, true),
    });
    const save = tag('button', {
      class: 'act primary',
      type: 'button',
      text: 'Save card',
      onclick: () => saveCard(),
    });
    const datalist = tag('datalist', { id: 'flashcap-card-categories' });
    for (const choice of [
      'machine-learning', 'algorithms', 'systems', 'math', 'physics',
      'neuroscience', 'programming', 'philosophy', 'history', 'economics',
      'biology', 'design', 'career', 'ireland', 'india', 'other',
    ]) {
      datalist.append(tag('option', { value: choice }));
    }

    const panel = tag(
      'div',
      {
        class: 'panel modal',
        role: 'dialog',
        'aria-modal': 'true',
        'aria-label': 'New flashcard',
      },
      tag(
        'div',
        { class: 'head' },
        tag('span', { class: 'check', text: '+' }),
        tag('strong', { text: 'New flashcard' }),
        tag('div', { class: 'spacer' }),
        tag('button', {
          class: 'close',
          text: '×',
          title: 'Close',
          'aria-label': 'Close',
          onclick: () => closeCardEditor(),
        })
      ),
      tag('div', { class: 'sub', text: sourceText || 'Reading record' }),
      tag('blockquote', {
        class: 'modal-quote',
        text: quoteText || '(no passage captured)',
      }),
      tag(
        'div',
        { class: 'field' },
        tag('label', { text: 'Front' }),
        front
      ),
      tag(
        'div',
        { class: 'field' },
        tag('label', { text: 'Back' }),
        back
      ),
      tag(
        'div',
        { class: 'field row2' },
        tag('div', {}, tag('label', { text: 'Category' }), category, datalist),
        tag('div', {}, tag('label', { text: 'Tags' }), tags)
      ),
      tag('div', {
        class: 'modal-hint',
        text: 'You write the front and back. The LLM only suggests category and tags.',
      }),
      suggestStatus,
      error,
      tag(
        'div',
        { class: 'modal-actions' },
        save,
        suggestBtn,
        tag('button', { class: 'act', text: 'Cancel', onclick: () => closeCardEditor() })
      )
    );

    root.append(panel);
    document.documentElement.append(host);
    stopPageKeyboard(host, closeCardEditor);

    cardModal = {
      host,
      payload,
      front,
      back,
      category,
      tags,
      error,
      save,
      suggestStatus,
      suggestBtn,
      categoryTouched: false,
      tagsTouched: false,
      saving: false,
    };

    category.addEventListener('input', () => {
      if (cardModal) cardModal.categoryTouched = true;
    });
    tags.addEventListener('input', () => {
      if (cardModal) cardModal.tagsTouched = true;
    });
    host.addEventListener('click', (event) => {
      if (!event.composedPath().includes(panel)) closeCardEditor();
    });
    for (const field of [front, back, category, tags]) {
      field.addEventListener('keydown', (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
          event.preventDefault();
          saveCard();
        }
      });
    }
    front.focus();
    suggestMetadata(cardModal, payload);
    return cardModal;
  };

  const captureCard = () => openCardEditor(collect());

  // -------------------------------------------------------------------
  // Annotate modal + selection context menu
  // -------------------------------------------------------------------
  let modal = null;
  let contextMenu = null;

  const closeAnnotate = () => {
    if (!modal) return;
    modal.host.remove();
    modal = null;
  };

  const suggestNoteMetadata = async (current, payload, force = false) => {
    if (!current) return;
    current.suggestStatus.textContent = 'Suggesting category and tags…';
    try {
      const data = await request('POST', '/api/suggest', payload);
      if (modal !== current) return;
      const suggestedTags = data.tags || [];
      if (!current.categoryTouched || force) {
        current.category.value = data.category || '';
      }
      if (suggestedTags.length && (!current.tagsTouched || force)) {
        current.tags.value = suggestedTags.join(', ');
      }
      current.suggestStatus.textContent = suggestedTags.length
        ? '✦ Reused existing tags — edit anything.'
        : 'No existing tag fits — add one manually if needed.';
    } catch (error) {
      if (modal !== current) return;
      current.suggestStatus.textContent =
        'No suggestions — fill category and tags yourself.';
    }
  };

  const saveAnnotate = async () => {
    if (!modal || modal.saving) return;
    const current = modal;
    current.saving = true;
    current.save.disabled = true;
    current.save.textContent = 'Saving…';
    current.error.textContent = '';

    try {
      const data = await request('POST', '/api/annotate', {
        ...current.payload,
        annotation: current.annotation.value.trim(),
        category: current.category.value.trim(),
        tags: current.tags.value.trim(),
      });
      closeAnnotate();
      showNoteSaved(panelHost(), data.note || {});
    } catch (error) {
      if (modal !== current) return;
      current.saving = false;
      current.save.disabled = false;
      current.save.textContent = 'Save annotation';
      current.error.textContent = error.message || 'Could not save the annotation';
    }
  };

  const openAnnotate = (payload) => {
    closeAnnotate();

    const host = document.createElement('div');
    host.id = 'flashcap-modal-host';
    host.style.cssText =
      'all: initial; position: fixed; inset: 0; z-index: 2147483647; ' +
      'display: flex; align-items: center; justify-content: center; ' +
      'padding: 16px; background: rgba(10, 10, 8, .42);';
    const root = host.attachShadow({ mode: 'open' });
    injectStyles(root);

    const quoteText = payload.selection || payload.focus || payload.context || '';
    const sourceText = [payload.page_title, payload.site].filter(Boolean).join(' · ');
    const annotation = tag('textarea', {
      rows: 4,
      placeholder: 'What do you want to remember about this?',
    });
    const tags = tag('input', {
      type: 'text',
      placeholder: 'comma, separated (optional)',
    });
    const category = tag('input', {
      type: 'text',
      list: 'flashcap-note-categories',
      placeholder: 'category',
    });
    const datalist = tag('datalist', { id: 'flashcap-note-categories' });
    for (const choice of [
      'machine-learning', 'algorithms', 'systems', 'math', 'physics',
      'neuroscience', 'programming', 'philosophy', 'history', 'economics',
      'biology', 'design', 'career', 'ireland', 'india', 'other',
    ]) {
      datalist.append(tag('option', { value: choice }));
    }
    const suggestStatus = tag('div', {
      class: 'modal-hint',
      text: 'Suggesting category and tags…',
    });
    const suggestBtn = tag('button', {
      class: 'act',
      type: 'button',
      text: 'Suggest metadata',
      onclick: () => suggestNoteMetadata(modal, payload, true),
    });
    const error = tag('div', { class: 'modal-error' });
    const save = tag('button', {
      class: 'act primary',
      text: 'Save annotation',
      onclick: () => saveAnnotate(),
    });
    const current = {
      host,
      payload,
      annotation,
      tags,
      category,
      error,
      save,
      suggestStatus,
      suggestBtn,
      categoryTouched: false,
      tagsTouched: false,
      explaining: false,
    };
    const explainBtn = tag('button', {
      class: 'act',
      type: 'button',
      text: 'Explain',
      title: 'Draft an explanation with the LLM',
      onclick: () => fillFromLLM(),
    });
    current.explainBtn = explainBtn;
    modal = current;

    const fillFromLLM = async () => {
      if (modal !== current || current.explaining) return;
      current.explaining = true;
      explainBtn.disabled = true;
      explainBtn.textContent = 'Thinking…';
      current.error.textContent = '';
      try {
        const data = await request('POST', '/api/explain', current.payload);
        if (modal !== current) return;
        const text = data.explanation || '';
        if (!text) throw new Error('No explanation returned');
        const existing = current.annotation.value.trim();
        current.annotation.value = existing ? `${existing}\n\n${text}` : text;
        const tagValue = current.tags.value.trim();
        if (!/(^|,\s*)ai-explanation(,|$)/i.test(tagValue)) {
          current.tags.value = [tagValue, 'ai-explanation']
            .filter(Boolean)
            .join(', ');
        }
        current.tagsTouched = true;
        current.annotation.focus();
      } catch (error) {
        if (modal === current) {
          current.error.textContent = error.message || 'Explanation failed';
        }
      } finally {
        if (modal === current) {
          current.explaining = false;
          explainBtn.disabled = false;
          explainBtn.textContent = 'Explain';
        }
      }
    };

    const panel = tag(
      'div',
      {
        class: 'panel modal',
        role: 'dialog',
        'aria-modal': 'true',
        'aria-label': 'Annotate in Obsidian',
      },
      tag(
        'div',
        { class: 'head' },
        tag('span', { class: 'check', text: '✎' }),
        tag('strong', { text: 'Annotate in Obsidian' }),
        tag('div', { class: 'spacer' }),
        tag('button', {
          class: 'close',
          text: '×',
          title: 'Close',
          'aria-label': 'Close',
          onclick: () => closeAnnotate(),
        })
      ),
      tag('div', { class: 'sub', text: sourceText || 'Reading record' }),
      tag('blockquote', {
        class: 'modal-quote',
        text: quoteText || '(no passage captured)',
      }),
      tag(
        'div',
        { class: 'field' },
        tag(
          'div',
          { class: 'label-row' },
          tag('label', { text: 'Your annotation' }),
          explainBtn
        ),
        annotation
      ),
      tag(
        'div',
        { class: 'field row2' },
        tag('div', {}, tag('label', { text: 'Category' }), category, datalist),
        tag('div', {}, tag('label', { text: 'Tags' }), tags)
      ),
      tag('div', {
        class: 'modal-hint',
        text: 'The LLM suggests category and tags; the annotation is yours. Explain drafts an AI explanation you can edit. Appends to the source note in your Writing vault. ⌘↵ saves.',
      }),
      suggestStatus,
      error,
      tag(
        'div',
        { class: 'modal-actions' },
        save,
        suggestBtn,
        tag('button', { class: 'act', text: 'Cancel', onclick: () => closeAnnotate() })
      )
    );

    root.append(panel);
    document.documentElement.append(host);
    stopPageKeyboard(host, closeAnnotate);

    host.addEventListener('click', (event) => {
      // Event.target is retargeted to the host at the shadow boundary, so
      // use composedPath to tell "inside the panel" from "backdrop".
      if (!event.composedPath().includes(panel)) closeAnnotate();
    });
    category.addEventListener('input', () => {
      if (modal) modal.categoryTouched = true;
    });
    tags.addEventListener('input', () => {
      if (modal) modal.tagsTouched = true;
    });
    for (const field of [annotation, category, tags]) {
      field.addEventListener('keydown', (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
          event.preventDefault();
          saveAnnotate();
        }
      });
    }
    annotation.focus();
    suggestNoteMetadata(current, payload);
    return current;
  };

  const annotateFromPage = () => openAnnotate(collect());

  // -------------------------------------------------------------------
  // Inline explanation
  // -------------------------------------------------------------------
  let explainModal = null;

  const closeExplain = () => {
    if (!explainModal) return;
    explainModal.host.remove();
    explainModal = null;
  };

  const openExplain = (payload) => {
    closeExplain();

    const host = document.createElement('div');
    host.id = 'flashcap-explain-host';
    host.style.cssText =
      'all: initial; position: fixed; inset: 0; z-index: 2147483647; ' +
      'display: flex; align-items: center; justify-content: center; ' +
      'padding: 16px; background: rgba(10, 10, 8, .42);';
    const root = host.attachShadow({ mode: 'open' });
    injectStyles(root);

    const quoteText = payload.selection || payload.focus || payload.context || '';
    const sourceText = [payload.page_title, payload.site].filter(Boolean).join(' · ');
    const explanation = tag('div', { class: 'explain-text', text: 'Thinking…' });
    const error = tag('div', { class: 'modal-error' });
    const annotateBtn = tag('button', {
      class: 'act primary',
      type: 'button',
      text: 'Annotate this',
      disabled: 'disabled',
      onclick: () => {
        const text = (explainModal && explainModal.text) || '';
        if (!text) return;
        const savedPayload = explainModal.payload;
        closeExplain();
        const next = openAnnotate(savedPayload);
        if (next) {
          next.annotation.value = text;
          next.tags.value = 'ai-explanation';
          next.annotation.focus();
        }
      },
    });
    const copyBtn = tag('button', {
      class: 'act',
      type: 'button',
      text: 'Copy',
      onclick: () => copyText((explainModal && explainModal.text) || ''),
    });
    const againBtn = tag('button', {
      class: 'act',
      type: 'button',
      text: 'Regenerate',
      onclick: () => explainModal && explainModal.run(),
    });

    const panel = tag(
      'div',
      {
        class: 'panel modal',
        role: 'dialog',
        'aria-modal': 'true',
        'aria-label': 'Explain this passage',
      },
      tag(
        'div',
        { class: 'head' },
        tag('span', { class: 'check', text: '?' }),
        tag('strong', { text: 'Explain this passage' }),
        tag('div', { class: 'spacer' }),
        tag('button', {
          class: 'close',
          text: '×',
          title: 'Close',
          'aria-label': 'Close',
          onclick: () => closeExplain(),
        })
      ),
      tag('div', { class: 'sub', text: sourceText || 'Reading record' }),
      tag('blockquote', {
        class: 'modal-quote',
        text: quoteText || '(no passage captured)',
      }),
      explanation,
      tag('div', {
        class: 'modal-hint',
        text: 'AI explanation from your configured LLM. Nothing is saved unless you annotate it.',
      }),
      error,
      tag(
        'div',
        { class: 'modal-actions' },
        annotateBtn,
        againBtn,
        copyBtn,
        tag('button', { class: 'act', text: 'Close', onclick: () => closeExplain() })
      )
    );

    root.append(panel);
    document.documentElement.append(host);
    stopPageKeyboard(host, closeExplain);

    const current = {
      host,
      payload,
      explanation,
      error,
      annotateBtn,
      text: '',
      token: 0,
    };
    explainModal = current;

    current.run = async () => {
      if (explainModal !== current) return;
      const token = ++current.token;
      current.explanation.textContent = 'Thinking…';
      current.error.textContent = '';
      current.annotateBtn.disabled = true;

      try {
        const data = await request('POST', '/api/explain', current.payload);
        if (explainModal !== current || token !== current.token) return;
        current.text = (data.explanation || '').trim();
        current.explanation.textContent = current.text || '(No explanation returned.)';
        current.annotateBtn.disabled = !current.text;
      } catch (error) {
        if (explainModal !== current || token !== current.token) return;
        current.text = '';
        current.explanation.textContent = '';
        current.error.textContent = error.message || 'Explanation failed';
        current.annotateBtn.disabled = true;
      }
    };

    host.addEventListener('click', (event) => {
      if (!event.composedPath().includes(panel)) closeExplain();
    });
    current.run();
    return current;
  };

  const explainFromPage = () => openExplain(collect());

  const copyText = (text) => {
    if (!text) return;
    const fallback = () => {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.cssText = 'position: fixed; left: -9999px; top: 0; opacity: 0;';
      document.documentElement.append(area);
      area.select();
      try {
        document.execCommand('copy');
      } catch (_) {
        /* clipboard unavailable */
      }
      area.remove();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(fallback);
    } else {
      fallback();
    }
  };

  const closeContextMenu = () => {
    if (!contextMenu) return;
    contextMenu.host.remove();
    contextMenu = null;
  };

  const openContextMenu = (event, payload) => {
    closeContextMenu();

    const host = document.createElement('div');
    host.id = 'flashcap-ctx-host';
    host.style.cssText =
      'all: initial; position: fixed; inset: 0; z-index: 2147483646;';
    const root = host.attachShadow({ mode: 'open' });
    injectStyles(root);

    const backdrop = tag('div', { class: 'ctx-backdrop' });
    const left = Math.max(8, Math.min(event.clientX, window.innerWidth - 240));
    const top = Math.max(8, Math.min(event.clientY, window.innerHeight - 200));
    const menu = tag(
      'div',
      { class: 'ctx', style: `left: ${left}px; top: ${top}px;` },
      tag('div', { class: 'ctx-title', text: 'Flashcap' }),
      tag('button', {
        class: 'ctx-item primary',
        text: 'Annotate in Obsidian',
        onclick: () => {
          closeContextMenu();
          openAnnotate(payload);
        },
      }),
      tag('button', {
        class: 'ctx-item',
        text: 'Explain with LLM',
        onclick: () => {
          closeContextMenu();
          openExplain(payload);
        },
      }),
      tag('button', {
        class: 'ctx-item',
        text: 'Make a flashcard',
        onclick: () => {
          closeContextMenu();
          openCardEditor(payload);
        },
      }),
      tag('div', { class: 'ctx-sep' }),
      tag('button', {
        class: 'ctx-item',
        text: 'Copy text',
        onclick: () => {
          closeContextMenu();
          copyText(payload.selection || payload.focus || '');
        },
      }),
      tag('button', {
        class: 'ctx-item',
        text: 'Dismiss',
        onclick: () => closeContextMenu(),
      })
    );

    backdrop.addEventListener('click', closeContextMenu);
    root.append(backdrop, menu);
    document.documentElement.append(host);
    contextMenu = { host };
  };

  // -------------------------------------------------------------------
  // Wiring
  // -------------------------------------------------------------------
  window.addEventListener(
    'keydown',
    (event) => {
      if (event.key === 'Escape') {
        if (explainModal) {
          event.preventDefault();
          event.stopPropagation();
          closeExplain();
          return;
        }
        if (cardModal) {
          event.preventDefault();
          event.stopPropagation();
          closeCardEditor();
          return;
        }
        if (modal) {
          event.preventDefault();
          event.stopPropagation();
          closeAnnotate();
          return;
        }
        if (contextMenu) {
          event.preventDefault();
          event.stopPropagation();
          closeContextMenu();
          return;
        }
        if (ui) {
          event.preventDefault();
          event.stopPropagation();
          hideUI();
          return;
        }
      }

      if (cardModal || modal || explainModal || contextMenu) return;
      if (event.repeat) return;
      const key = String(event.key || '').toLowerCase();
      const isK = event.code === 'KeyK' || key === 'k';
      const isL = event.code === 'KeyL' || key === 'l';
      const isE = event.code === 'KeyE' || key === 'e';
      if (!isK && !isL && !isE) return;
      const matches = IS_MAC
        ? event.metaKey && event.shiftKey && !event.altKey && !event.ctrlKey
        : event.ctrlKey && event.shiftKey && !event.altKey && !event.metaKey;
      if (!matches) return;
      event.preventDefault();
      event.stopPropagation();
      if (isK) captureCard();
      else if (isL) annotateFromPage();
      else explainFromPage();
    },
    true
  );

  // A userscript cannot add a native Firefox context-menu item. Right-clicking
  // a selection shows a small Flashcap menu instead; Shift+right-click keeps
  // Firefox's native menu.
  window.addEventListener(
    'contextmenu',
    (event) => {
      if (!ENABLE_CONTEXT_MENU || event.shiftKey) return;
      const selection = selectionInfo();
      if (!selection.text || !selection.element) return;

      const target = event.target;
      if (!selection.element.contains(target)) return;
      if (
        target &&
        target.closest &&
        target.closest(
          'input, textarea, [contenteditable="true"], #flashcap-host, ' +
            '#flashcap-modal-host, #flashcap-ctx-host'
        )
      ) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      const payload = collect();
      openContextMenu(event, payload);
    },
    true
  );

  const registerMenu = () => {
    try {
      if (typeof GM !== 'undefined' && GM.registerMenuCommand) {
        GM.registerMenuCommand(`New flashcard (${HOTKEY_LABEL})`, captureCard);
        GM.registerMenuCommand(`Annotate in Obsidian (${NOTE_LABEL})`, annotateFromPage);
        GM.registerMenuCommand(`Explain passage (${EXPLAIN_LABEL})`, explainFromPage);
      } else if (typeof GM_registerMenuCommand === 'function') {
        GM_registerMenuCommand(`New flashcard (${HOTKEY_LABEL})`, captureCard);
        GM_registerMenuCommand(`Annotate in Obsidian (${NOTE_LABEL})`, annotateFromPage);
        GM_registerMenuCommand(`Explain passage (${EXPLAIN_LABEL})`, explainFromPage);
      }
    } catch (_) {
      /* menu command is a nice-to-have */
    }
  };

  registerMenu();
})();
