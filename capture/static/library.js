/* Flashcap reading list — dependency-free. */

(() => {
  'use strict';

  const state = {
    items: [],
    q: '',
    status: 'all',
    site: '',
    counts: { unread: 0, reading: 0, done: 0, dropped: 0, total: 0 },
    sites: [],
    total: 0,
    hasMore: false,
    pageSize: 200,
    loading: true,
  };

  const $ = (selector) => document.querySelector(selector);
  const itemsEl = $('#items');
  const emptyEl = $('#empty');
  const noResultsEl = $('#no-results');
  const moreWrap = $('#more-wrap');
  const loadMoreEl = $('#load-more');
  const pillsEl = $('#status-pills');
  const searchEl = $('#search');
  const siteEl = $('#site-filter');
  const formEl = $('#add-form');
  const urlEl = $('#add-url');
  const noticeEl = $('#notice');
  const statUnreadEl = $('#stat-unread');
  const statTotalEl = $('#stat-total');

  const STATUS_LABELS = {
    all: 'All',
    unread: 'Unread',
    reading: 'Reading',
    done: 'Done',
    dropped: 'Dropped',
  };

  const NEXT_ACTION = {
    unread: { label: 'Start', to: 'reading' },
    reading: { label: 'Done', to: 'done' },
    dropped: { label: 'Restore', to: 'unread' },
  };

  let searchTimer = null;
  let noticeTimer = null;
  let loadToken = 0;

  // -----------------------------------------------------------------
  // Tiny DOM helpers (textContent only — never innerHTML for data)
  // -----------------------------------------------------------------
  function h(name, props = {}, ...children) {
    const node = document.createElement(name);
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
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
  }

  function timeAgo(iso) {
    if (!iso) return '';
    const date = new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
    if (Number.isNaN(date.getTime())) return '';
    const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
    if (seconds < 86400 * 7) return `${Math.round(seconds / 86400)}d ago`;
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function safeUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function prettyPath(url) {
    try {
      const parsed = new URL(url);
      const segments = parsed.pathname.split('/').filter(Boolean);
      const last = decodeURIComponent(segments[segments.length - 1] || '');
      const clean = last
        .replace(/\.(html?|php|pdf|epub)$/i, '')
        .replace(/[-_]+/g, ' ')
        .trim();
      return clean.length > 2 ? clean : '';
    } catch (_) {
      return '';
    }
  }

  function firstLine(text) {
    return String(text || '')
      .split('\n')
      .map((line) => line.trim())
      .find(Boolean) || '';
  }

  function hostOf(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, '');
    } catch (_) {
      return '';
    }
  }

  function titleFor(item) {
    return (
      String(item.title || '').trim() ||
      prettyPath(item.url) ||
      firstLine(item.description) ||
      'Saved note'
    );
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function notice(message, kind = 'ok') {
    noticeEl.textContent = message;
    noticeEl.className = `lib-notice ${kind}`;
    noticeEl.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => {
      noticeEl.hidden = true;
    }, 4000);
  }

  // -----------------------------------------------------------------
  // Load + render
  // -----------------------------------------------------------------
  async function load({ append = false } = {}) {
    const token = ++loadToken;
    state.loading = true;
    itemsEl.setAttribute('aria-busy', 'true');
    const params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    if (state.status !== 'all') params.set('status', state.status);
    if (state.site) params.set('site', state.site);
    params.set('limit', String(state.pageSize));
    params.set('offset', String(append ? state.items.length : 0));

    try {
      const data = await api(`/api/library?${params}`);
      if (token !== loadToken) return;
      state.items = append ? state.items.concat(data.items || []) : data.items || [];
      state.counts = data.counts || state.counts;
      state.sites = data.sites || [];
      state.total = data.total || 0;
      state.hasMore = Boolean(data.has_more);
    } catch (error) {
      if (token !== loadToken) return;
      notice(error.message, 'err');
      if (!append) state.items = [];
    }

    if (token !== loadToken) return;
    state.loading = false;
    itemsEl.removeAttribute('aria-busy');
    render();
  }

  function render() {
    statUnreadEl.textContent = String(state.counts.unread || 0);
    statTotalEl.textContent = `${state.counts.total || 0} saved`;
    renderPills();
    renderSites();
    renderItems();
    renderMore();
  }

  function renderMore() {
    const left = Math.max(0, state.total - state.items.length);
    moreWrap.hidden = !state.hasMore || left === 0;
    loadMoreEl.textContent = `Load more (${left} left)`;
  }

  function renderPills() {
    pillsEl.replaceChildren();
    const counts = state.counts;
    for (const key of Object.keys(STATUS_LABELS)) {
      const count = key === 'all' ? counts.total || 0 : counts[key] || 0;
      pillsEl.append(
        h(
          'button',
          {
            type: 'button',
            class: `pill${state.status === key ? ' active' : ''}`,
            'data-status': key,
            'aria-pressed': state.status === key ? 'true' : 'false',
          },
          STATUS_LABELS[key],
          h('span', { class: 'count', text: String(count) })
        )
      );
    }
  }

  function renderSites() {
    const current = state.site;
    siteEl.replaceChildren(h('option', { value: '' }, 'All sites'));
    for (const entry of state.sites) {
      siteEl.append(
        h('option', { value: entry.site }, `${entry.site} (${entry.count})`)
      );
    }
    siteEl.value = current;
    if (siteEl.value !== current) {
      state.site = '';
      siteEl.value = '';
    }
  }

  function renderItems() {
    itemsEl.replaceChildren();
    const filtered = Boolean(state.q || state.site || state.status !== 'all');
    const isEmpty = !state.loading && state.items.length === 0;
    const showEmpty = isEmpty && (state.counts.total || 0) === 0 && !filtered;
    const showNoResults = isEmpty && !showEmpty;

    emptyEl.hidden = !showEmpty;
    noResultsEl.hidden = !showNoResults;

    for (const item of state.items) {
      itemsEl.append(renderRow(item));
    }
  }

  function renderRow(item) {
    const url = safeUrl(item.url);
    const title = titleFor(item);
    const question = String(item.title || '').trim();

    const titleEl = url
      ? h('a', {
          class: 'lib-title',
          href: url,
          target: '_blank',
          rel: 'noreferrer',
          text: title,
        })
      : h('span', { class: 'lib-title', text: title });

    const description = String(item.description || '').trim();
    const showDescription =
      description && description !== item.url && description !== question;

    const site = String(item.site || '').trim() || hostOf(item.url);
    const meta = h('div', { class: 'lib-meta' });
    if (site) meta.append(h('span', { class: 'lib-site', text: site }));
    if (item.saved_at) {
      meta.append(h('span', { class: 'lib-dot', text: '·' }));
      meta.append(h('span', { text: timeAgo(item.saved_at) }));
    }
    meta.append(
      h('span', { class: `status-chip chip-${item.status}`, text: STATUS_LABELS[item.status] || item.status })
    );
    if (item.archive_error) {
      meta.append(
        h('span', {
          class: 'lib-warn',
          title: item.archive_error,
          text: 'archive issue',
        })
      );
    }

    const next = NEXT_ACTION[item.status];
    const actions = h('div', { class: 'lib-actions' });
    if (item.status === 'done') {
      actions.append(
        h('button', {
          type: 'button',
          class: 'btn btn-ghost',
          'data-action': 'undo',
          'data-id': item.id,
          text: 'Undo',
        })
      );
    } else if (next) {
      actions.append(
        h('button', {
          type: 'button',
          class: 'btn btn-ghost',
          'data-action': 'status',
          'data-id': item.id,
          'data-to': next.to,
          text: next.label,
        })
      );
    }
    actions.append(
      h('button', {
        type: 'button',
        class: 'icon-btn',
        'data-action': 'delete',
        'data-id': item.id,
        'aria-label': 'Remove from list',
        title: 'Remove from list',
        text: '×',
      })
    );

    return h(
      'article',
      { class: 'lib-row' },
      h(
        'div',
        { class: 'lib-main' },
        titleEl,
        showDescription ? h('p', { class: 'lib-desc', text: description }) : null,
        meta
      ),
      actions
    );
  }

  async function loadHealth() {
    try {
      const data = await api('/api/health');
      const statusEl = $('#telegram-status');
      if (data.telegram) {
        statusEl.textContent = `Telegram: ${data.telegram.enabled ? 'polling' : 'off'}`;
      }
    } catch (_) {
      /* health is best-effort */
    }
  }

  // -----------------------------------------------------------------
  // Events
  // -----------------------------------------------------------------
  pillsEl.addEventListener('click', (event) => {
    const pill = event.target.closest('[data-status]');
    if (!pill) return;
    state.status = pill.dataset.status;
    load();
  });

  siteEl.addEventListener('change', () => {
    state.site = siteEl.value;
    load();
  });

  searchEl.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = searchEl.value.trim();
      load();
    }, 180);
  });

  $('#clear-filters').addEventListener('click', () => {
    state.q = '';
    state.status = 'all';
    state.site = '';
    searchEl.value = '';
    load();
  });

  loadMoreEl.addEventListener('click', () => load({ append: true }));

  itemsEl.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const id = button.dataset.id;

    if (button.dataset.action === 'undo') {
      if (
        !window.confirm(
          'Undo this? The link goes back to the channel and leaves the done list.'
        )
      ) {
        return;
      }
      try {
        const result = await api(`/api/library/${id}/undo`, { method: 'POST' });
        if (result.undo && result.undo.error) {
          notice(result.undo.error, 'err');
        }
        await load();
      } catch (error) {
        notice(error.message, 'err');
        await load();
      }
      return;
    }

    if (button.dataset.action === 'status') {
      try {
        const result = await api(`/api/library/${id}`, {
          method: 'PATCH',
          body: JSON.stringify({ status: button.dataset.to }),
        });
        if (result.archive && result.archive.error) {
          notice(result.archive.error, 'err');
        }
        await load();
      } catch (error) {
        notice(error.message, 'err');
        await load();
      }
      return;
    }

    if (button.dataset.action === 'delete') {
      if (!window.confirm('Remove this from the reading list?')) return;
      try {
        await api(`/api/library/${id}`, { method: 'DELETE' });
        await load();
      } catch (error) {
        notice(error.message, 'err');
      }
    }
  });

  formEl.addEventListener('submit', async (event) => {
    event.preventDefault();
    const url = urlEl.value.trim();
    if (!url) return;
    try {
      await api('/api/library', {
        method: 'POST',
        body: JSON.stringify({ url }),
      });
      urlEl.value = '';
      notice('Saved.');
      await load();
    } catch (error) {
      notice(error.message, 'err');
    }
  });

  document.addEventListener('keydown', (event) => {
    const tag = (event.target.tagName || '').toLowerCase();
    if (event.key === '/' && tag !== 'input' && tag !== 'textarea' && tag !== 'select') {
      event.preventDefault();
      searchEl.focus();
    }
  });

  loadHealth();
  load();
})();
