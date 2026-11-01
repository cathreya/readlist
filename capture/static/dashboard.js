/* Flashcap dashboard — dependency-free. */

(() => {
  'use strict';

  const state = {
    cards: [],
    meta: { total: 0, last_7_days: 0, categories: {}, tags: {}, category_choices: [] },
    obsidian: { enabled: false, vault: '', folder: '', notes: 0, vaults: [] },
    review: { available: 0, due: 0, new: 0, reviewed_today: 0, total: 0 },
    q: '',
    category: 'all',
    tag: '',
    loading: true,
  };

  const $ = (selector) => document.querySelector(selector);
  const cardsEl = $('#cards');
  const emptyEl = $('#empty');
  const noResultsEl = $('#no-results');
  const toolbarEl = $('.toolbar');
  const filtersEl = $('#filters');
  const pillsEl = $('#category-pills');
  const searchEl = $('#search');
  const toastEl = $('#toast');
  const dialog = $('#edit-dialog');
  const form = $('#edit-form');

  let searchTimer = null;
  let toastTimer = null;
  let editingId = null;

  // -----------------------------------------------------------------
  // Tiny DOM helpers (textContent only — never innerHTML for data)
  // -----------------------------------------------------------------
  function h(name, props = {}, ...children) {
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
  }

  const categoryLabel = (value) => String(value || 'other').replace(/-/g, ' ');

  function dotColor(category) {
    let hash = 0;
    for (const char of String(category || '')) {
      hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
    }
    return `hsl(${hash % 360} 42% 48%)`;
  }

  function timeAgo(iso) {
    if (!iso) return '';
    const date = new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
    if (Number.isNaN(date.getTime())) return '';
    const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 45) return 'just now';
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

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    let data = {};
    try {
      data = await response.json();
    } catch (_) {
      /* empty response */
    }
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function toast(message, url = '') {
    toastEl.replaceChildren(document.createTextNode(message));
    if (url) {
      toastEl.append(
        h('a', {
          href: url,
          text: ' Open note ↗',
          title: 'Open this reading note in Obsidian',
        })
      );
    }
    toastEl.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('show'), 6000);
  }

  // -----------------------------------------------------------------
  // Load + render
  // -----------------------------------------------------------------
  async function load() {
    state.loading = true;
    cardsEl.setAttribute('aria-busy', 'true');
    try {
      const obsidianPromise = api('/api/obsidian').catch(() => ({
        enabled: false,
        vault: '',
        folder: '',
        notes: 0,
        vaults: [],
      }));
      const reviewPromise = api('/api/review/stats').catch(() => ({
        available: 0,
        due: 0,
        new: 0,
        reviewed_today: 0,
        total: 0,
      }));
      const [cardsData, meta, obsidian, review] = await Promise.all([
        api('/api/cards?limit=5000'),
        api('/api/meta'),
        obsidianPromise,
        reviewPromise,
      ]);
      state.cards = cardsData.cards || [];
      state.meta = meta;
      state.obsidian = obsidian;
      state.review = review;
      state.loading = false;
      cardsEl.setAttribute('aria-busy', 'false');
      fillCategoryOptions();
      render();
      openDeepLinkedCard();
      loadHealth();
    } catch (error) {
      state.loading = false;
      cardsEl.setAttribute('aria-busy', 'false');
      showFatal(error);
    }
  }

  async function refreshMeta() {
    try {
      state.meta = await api('/api/meta');
      fillCategoryOptions();
      render();
    } catch (_) {
      /* non-critical */
    }
  }

  function visibleCards() {
    const query = state.q.toLowerCase();
    return state.cards.filter((card) => {
      if (state.category !== 'all' && card.category !== state.category) return false;
      if (state.tag && !(card.tags || []).includes(state.tag)) return false;
      if (!query) return true;
      const haystack = [
        card.front,
        card.back,
        card.category,
        card.source_title,
        card.source_site,
        (card.tags || []).join(' '),
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(query);
    });
  }

  function render() {
    if (state.loading) return;

    const visible = visibleCards();
    const hasAny = state.meta.total > 0;
    const hasVisible = visible.length > 0;

    toolbarEl.hidden = !hasAny;
    emptyEl.hidden = hasAny;
    noResultsEl.hidden = !(hasAny && !hasVisible);
    cardsEl.hidden = !hasVisible;

    $('#stat-total').textContent = String(state.meta.total ?? 0);
    $('#stat-week').textContent =
      (state.meta.last_7_days || 0) > 0
        ? `cards · ${state.meta.last_7_days} this week`
        : 'cards';

    const practiceCount = $('#practice-count');
    if (practiceCount) {
      const available = state.review.available || 0;
      practiceCount.textContent = String(available);
      practiceCount.hidden = available === 0;
      $('#practice-link').title =
        available > 0
          ? `${available} card${available === 1 ? '' : 's'} ready to practice`
          : 'Nothing due right now';
    }

    const obsidianStatus = $('#obsidian-status');
    if (obsidianStatus) {
      const ob = state.obsidian;
      const queued = ob.pending || 0;
      obsidianStatus.textContent = ob.enabled
        ? `Obsidian: ${ob.vault} · ${ob.notes} note${(ob.notes || 0) === 1 ? '' : 's'}${queued ? ` · ${queued} queued` : ''}`
        : 'Obsidian: off';
      obsidianStatus.title = ob.enabled
        ? `Reading notes go to ${ob.vault}/${ob.folder}`
        : 'Set FLASHCAP_OBSIDIAN_VAULT in .env to turn on reading notes';
    }

    renderPills();
    renderFilters(visible);
    cardsEl.replaceChildren(...visible.map((card) => cardNode(card)));
  }

  function renderPills() {
    const total = state.meta.total || 0;
    const pills = [
      h('button', {
        class: `pill${state.category === 'all' ? ' active' : ''}`,
        type: 'button',
        'aria-pressed': state.category === 'all' ? 'true' : 'false',
        onclick: () => {
          state.category = 'all';
          render();
        },
      }, 'All', h('span', { class: 'count', text: String(total) })),
    ];

    for (const [category, count] of Object.entries(state.meta.categories || {})) {
      pills.push(
        h(
          'button',
          {
            class: `pill${state.category === category ? ' active' : ''}`,
            type: 'button',
            'aria-pressed': state.category === category ? 'true' : 'false',
            onclick: () => {
              state.category = state.category === category ? 'all' : category;
              state.tag = '';
              render();
            },
          },
          categoryLabel(category),
          h('span', { class: 'count', text: String(count) })
        )
      );
    }
    pillsEl.replaceChildren(...pills);
  }

  function renderFilters(visible) {
    const active = Boolean(state.q || state.tag || state.category !== 'all');
    if (!active) {
      filtersEl.replaceChildren();
      return;
    }

    const nodes = [
      h('span', {
        class: 'filter-note',
        text: `${visible.length} of ${state.meta.total} card${state.meta.total === 1 ? '' : 's'}`,
      }),
    ];

    if (state.tag) {
      nodes.push(
        h(
          'span',
          { class: 'tag-filter' },
          `#${state.tag}`,
          h('button', {
            type: 'button',
            text: '×',
            title: 'Clear tag filter',
            'aria-label': 'Clear tag filter',
            onclick: () => {
              state.tag = '';
              render();
            },
          })
        )
      );
    }

    nodes.push(
      h('button', {
        class: 'btn btn-ghost',
        type: 'button',
        text: 'Clear',
        onclick: clearFilters,
      })
    );

    filtersEl.replaceChildren(...nodes);
  }

  function cardNode(card) {
    const sourceUrl = safeUrl(card.source_url);
    const sourceLabel =
      card.source_title ||
      card.source_site ||
      (sourceUrl ? new URL(sourceUrl).hostname.replace(/^www\./, '') : '') ||
      'unknown source';

    const badge = h('span', { class: 'cat-badge' });
    badge.style.setProperty('--dot', dotColor(card.category));
    badge.append(
      h('span', { class: 'dot' }),
      document.createTextNode(categoryLabel(card.category))
    );

    const source = h('span', { class: 'source', title: sourceLabel });
    if (sourceUrl) {
      source.append(
        h('a', {
          href: sourceUrl,
          target: '_blank',
          rel: 'noopener noreferrer',
          text: sourceLabel,
        })
      );
    } else {
      source.textContent = sourceLabel;
    }

    const review =
      card.generated_by === 'fallback' ||
      (typeof card.confidence === 'number' && card.confidence < 0.5);

    const meta = h(
      'div',
      { class: 'card-meta' },
      badge,
      review
        ? h('span', {
            class: 'cat-badge review',
            text: 'review',
            title: 'Low confidence — edit this card before exporting',
          })
        : null,
      h('span', { class: 'meta-sep', text: '·' }),
      source,
      h('span', { class: 'meta-sep', text: '·' }),
      h('time', { datetime: card.created_at || '', text: timeAgo(card.created_at) })
    );

    const tags = h('div', { class: 'tags' });
    for (const tag of card.tags || []) {
      tags.append(
        h('button', {
          class: 'tag-chip',
          type: 'button',
          text: `#${tag}`,
          title: `Filter by #${tag}`,
          onclick: () => {
            state.tag = state.tag === tag ? '' : tag;
            state.category = 'all';
            render();
          },
        })
      );
    }

    const details = h(
      'details',
      { class: 'provenance' },
      h('summary', { text: 'Context & source' })
    );
    if (card.quote && card.quote !== card.selection) {
      details.append(h('p', { class: 'quote', text: `“${card.quote}”` }));
    }
    if (card.selection) {
      details.append(
        h('p', { class: 'quote', text: `Highlight: “${card.selection}”` })
      );
    }
    if (card.hint) {
      details.append(h('p', { class: 'quote', text: `Note: ${card.hint}` }));
    }
    details.append(
      h('p', {
        class: 'quote',
        text:
          `${categoryLabel(card.category)}` +
          (card.generated_by === 'fallback' ? ' · saved unclassified' : '') +
          (card.source_site ? ` · ${card.source_site}` : ''),
      })
    );

    const noteAction = state.obsidian.enabled
      ? card.obsidian_url
        ? h('a', {
            class: 'btn btn-ghost',
            href: card.obsidian_url,
            text: 'Note ↗',
            title: card.obsidian_path || 'Open this reading note in Obsidian',
          })
        : h('button', {
            class: 'btn btn-ghost',
            type: 'button',
            text: 'Record',
            title: 'Record this passage in Obsidian (no annotation)',
            onclick: () => writeNote(card),
          })
      : null;

    const actions = h(
      'div',
      { class: 'card-actions' },
      noteAction,
      h('button', {
        class: 'btn btn-ghost',
        type: 'button',
        text: 'Edit',
        onclick: () => openEdit(card),
      }),
      h('button', {
        class: 'btn btn-ghost btn-danger',
        type: 'button',
        text: 'Delete',
        onclick: () => deleteCard(card),
      })
    );

    return h(
      'article',
      { class: 'card', 'data-id': String(card.id) },
      h(
        'div',
        { class: 'card-main' },
        meta,
        h('h2', { class: 'card-front', text: card.front }),
        h('p', { class: 'card-back', text: card.back }),
        (card.tags || []).length ? tags : null,
        card.quote || card.selection || card.hint || card.generated_by === 'fallback'
          ? details
          : null
      ),
      actions
    );
  }

  function showFatal(error) {
    cardsEl.hidden = false;
    emptyEl.hidden = true;
    noResultsEl.hidden = true;
    cardsEl.replaceChildren(
      h(
        'div',
        { class: 'empty' },
        h('h2', { text: 'Could not load cards' }),
        h('p', { text: error.message || String(error) }),
        h(
          'div',
          { class: 'empty-actions' },
          h('button', {
            class: 'btn btn-primary',
            type: 'button',
            text: 'Retry',
            onclick: () => load(),
          })
        )
      )
    );
  }

  async function loadHealth() {
    try {
      const data = await api('/api/health');
      $('#health').textContent = `● local · ${data.llm_configured ? 'LLM ready' : 'LLM off'}`;
    } catch (_) {
      $('#health').textContent = '● server offline';
    }
  }

  // -----------------------------------------------------------------
  // Edit
  // -----------------------------------------------------------------
  function fillCategoryOptions() {
    const values = new Set([
      ...(state.meta.category_choices || []),
      ...Object.keys(state.meta.categories || {}),
      ...state.cards.map((card) => card.category),
    ]);
    $('#category-options').replaceChildren(
      ...[...values].filter(Boolean).sort().map((value) => h('option', { value }))
    );
  }

  function openEdit(card) {
    editingId = card.id;
    $('#edit-front').value = card.front || '';
    $('#edit-back').value = card.back || '';
    $('#edit-category').value = card.category || '';
    $('#edit-tags').value = (card.tags || []).join(', ');
    $('#edit-meta').textContent = `Card ${card.id} · ${timeAgo(card.created_at)}`;
    $('#edit-meta').style.color = '';
    $('#edit-save').disabled = false;
    $('#edit-save').textContent = 'Save';
    if (!dialog.open) dialog.showModal();
    history.replaceState(null, '', `${location.pathname}?card=${card.id}`);
    const front = $('#edit-front');
    front.focus();
    front.setSelectionRange(front.value.length, front.value.length);
  }

  async function openDeepLinkedCard() {
    const id = new URLSearchParams(location.search).get('card');
    if (!id) return;
    let card = state.cards.find((item) => String(item.id) === String(id));
    if (!card) {
      try {
        const data = await api(`/api/cards/${id}`);
        card = data.card;
        state.cards.unshift(card);
      } catch (_) {
        return;
      }
    }
    if (card) openEdit(card);
  }

  function closeEdit() {
    if (dialog.open) dialog.close();
    editingId = null;
    if (location.search.includes('card=')) {
      history.replaceState(null, '', location.pathname);
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!editingId) return;
    const front = $('#edit-front').value.trim();
    const back = $('#edit-back').value.trim();
    const category = $('#edit-category').value.trim();
    const tags = $('#edit-tags').value.trim();
    const save = $('#edit-save');
    const meta = $('#edit-meta');

    if (!front || !back) {
      meta.textContent = 'Front and back are required.';
      meta.style.color = 'var(--danger)';
      return;
    }

    save.disabled = true;
    save.textContent = 'Saving…';
    try {
      const data = await api(`/api/cards/${editingId}`, {
        method: 'PATCH',
        body: JSON.stringify({ front, back, category, tags }),
      });
      state.cards = state.cards.map((card) =>
        card.id === data.card.id ? data.card : card
      );
      await refreshMeta();
      closeEdit();
      toast('Card saved');
    } catch (error) {
      meta.textContent = error.message || 'Could not save';
      meta.style.color = 'var(--danger)';
    } finally {
      save.disabled = false;
      save.textContent = 'Save';
    }
  });

  for (const button of document.querySelectorAll('[data-close]')) {
    button.addEventListener('click', closeEdit);
  }

  dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    const inside =
      event.clientX >= rect.left &&
      event.clientX <= rect.right &&
      event.clientY >= rect.top &&
      event.clientY <= rect.bottom;
    if (!inside) closeEdit();
  });

  dialog.addEventListener('close', () => {
    editingId = null;
    if (location.search.includes('card=')) {
      history.replaceState(null, '', location.pathname);
    }
  });

  async function deleteCard(card) {
    if (!window.confirm(`Delete this card?\n\n${card.front}`)) return;
    try {
      await api(`/api/cards/${card.id}`, { method: 'DELETE' });
      state.cards = state.cards.filter((item) => item.id !== card.id);
      await refreshMeta();
      toast('Card deleted');
    } catch (error) {
      toast(error.message || 'Could not delete card');
    }
  }

  async function refreshObsidian() {
    try {
      state.obsidian = await api('/api/obsidian');
    } catch (_) {
      /* the note action will surface a real error */
    }
  }

  const noteInFlight = new Set();

  async function writeNote(card) {
    if (noteInFlight.has(card.id)) return;
    noteInFlight.add(card.id);
    try {
      const data = await api(`/api/cards/${card.id}/obsidian`, { method: 'POST' });
      if (data.card) {
        state.cards = state.cards.map((item) =>
          item.id === data.card.id ? data.card : item
        );
      }
      await refreshObsidian();
      render();

      const note = data.note || {};
      if (note.already) {
        toast(`Reading note already saved — ${note.path}`, note.open_url);
      } else if (note.queued) {
        toast(`Annotation queued for your Mac — ${note.path}`);
      } else {
        const verb = note.appended ? 'updated' : 'saved';
        toast(`Reading note ${verb} — ${note.path}`, note.open_url);
      }
    } catch (error) {
      toast(error.message || 'Could not write the reading note');
    } finally {
      noteInFlight.delete(card.id);
    }
  }

  // -----------------------------------------------------------------
  // Filters / keyboard
  // -----------------------------------------------------------------
  function clearFilters() {
    state.q = '';
    state.category = 'all';
    state.tag = '';
    searchEl.value = '';
    render();
  }

  searchEl.addEventListener('input', () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = searchEl.value.trim();
      render();
    }, 120);
  });

  $('#clear-filters').addEventListener('click', clearFilters);

  window.addEventListener('keydown', (event) => {
    const target = event.target;
    const typing =
      target &&
      (target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.isContentEditable);

    if (event.key === '/' && !typing) {
      event.preventDefault();
      searchEl.focus();
      searchEl.select();
      return;
    }

    if ((event.key === 'n' || event.key === 'N') && !typing && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      location.href = '/quick';
      return;
    }

    if (event.key === 'Escape' && document.activeElement === searchEl) {
      searchEl.blur();
      if (searchEl.value) {
        clearFilters();
      }
    }
  });

  load();
})();
