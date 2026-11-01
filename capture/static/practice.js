/* Flashcap practice — mobile-friendly spaced repetition. */

(() => {
  'use strict';

  const state = {
    card: null,
    revealed: false,
    busy: false,
    stats: {},
  };

  const $ = (selector) => document.querySelector(selector);
  const loadingEl = $('#practice-loading');
  const cardEl = $('#practice-card');
  const doneEl = $('#practice-done');
  const revealEl = $('#practice-reveal');
  const gradesEl = $('#practice-grades');
  const progressEl = $('#practice-progress');

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  const categoryLabel = (value) =>
    String(value || 'other').replace(/-/g, ' ');

  function dotColor(category) {
    let hash = 0;
    for (const char of String(category || '')) {
      hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
    }
    return `hsl(${hash % 360} 42% 48%)`;
  }

  function safeUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'http:' || url.protocol === 'https:'
        ? url.href
        : null;
    } catch (_) {
      return null;
    }
  }

  function formatDue(iso) {
    if (!iso) return 'No cards are scheduled yet.';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return 'No cards are scheduled yet.';
    return `Next due ${date.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    })}.`;
  }

  function renderStats(stats) {
    state.stats = stats || {};
    $('#stat-new').textContent = String(stats.new ?? '—');
    $('#stat-due').textContent = String(stats.due ?? '—');
    $('#stat-reviewed').textContent = String(stats.reviewed_today ?? '—');
    const retention = Math.round((stats.desired_retention ?? 0.9) * 100);
    const available =
      (stats.available ?? 0) > 0
        ? `${stats.available} card${stats.available === 1 ? '' : 's'} available`
        : '';
    progressEl.textContent = [`FSRS · ${retention}% retention`, available]
      .filter(Boolean)
      .join(' · ');
  }

  function showCard(card) {
    state.card = card;
    state.revealed = false;
    loadingEl.hidden = true;
    doneEl.hidden = true;
    cardEl.hidden = false;
    revealEl.hidden = false;
    gradesEl.hidden = true;
    $('#practice-answer').hidden = true;
    $('#practice-quote').hidden = true;

    const badge = $('#practice-category');
    badge.textContent = categoryLabel(card.category);
    badge.style.setProperty('--dot', dotColor(card.category));

    const source = $('#practice-source');
    const url = safeUrl(card.source_url);
    source.replaceChildren();
    const label =
      card.source_title ||
      card.source_site ||
      (url ? new URL(url).hostname.replace(/^www\./, '') : '');
    if (url) {
      source.append(
        Object.assign(document.createElement('a'), {
          href: url,
          target: '_blank',
          rel: 'noopener noreferrer',
          textContent: label || url,
        })
      );
    } else {
      source.textContent = label;
    }

    $('#practice-front').textContent = card.front || '';
    $('#practice-back').textContent = card.back || '';

    const tags = $('#practice-tags');
    tags.replaceChildren(
      ...(card.tags || []).map((tag) => {
        const chip = document.createElement('span');
        chip.className = 'tag-chip';
        chip.textContent = `#${tag}`;
        return chip;
      })
    );

    const quote = $('#practice-quote');
    if (card.quote) {
      quote.textContent = `“${card.quote}”`;
      quote.hidden = false;
    }
  }

  function showDone(stats) {
    state.card = null;
    loadingEl.hidden = true;
    cardEl.hidden = true;
    doneEl.hidden = false;
    progressEl.textContent = '';

    const text =
      (stats.total || 0) === 0
        ? 'No cards yet. Capture one with ⌘⇧K, then come back.'
        : `Nothing due right now. ${formatDue(stats.next_due_at)}`;
    $('#practice-done-text').textContent = text;
  }

  async function load() {
    loadingEl.hidden = false;
    cardEl.hidden = true;
    doneEl.hidden = true;
    progressEl.textContent = '';
    try {
      const data = await api('/api/review/next');
      renderStats(data.stats || {});
      if (data.card) showCard(data.card);
      else showDone(data.stats || {});
    } catch (error) {
      loadingEl.hidden = true;
      doneEl.hidden = false;
      $('#practice-done-text').textContent = error.message || 'Could not load the queue.';
    }
  }

  function reveal() {
    if (!state.card || state.revealed) return;
    state.revealed = true;
    $('#practice-answer').hidden = false;
    revealEl.hidden = true;
    gradesEl.hidden = false;
  }

  async function grade(value) {
    if (!state.card || state.busy || !state.revealed) return;
    state.busy = true;
    progressEl.textContent = 'Saving…';
    gradesEl.querySelectorAll('button').forEach((button) => {
      button.disabled = true;
    });
    try {
      const data = await api(`/api/review/${state.card.id}`, {
        method: 'POST',
        body: JSON.stringify({ grade: value }),
      });
      renderStats(data.stats || {});
      await load();
    } catch (error) {
      progressEl.textContent = error.message || 'Could not save the review.';
    } finally {
      state.busy = false;
      gradesEl.querySelectorAll('button').forEach((button) => {
        button.disabled = false;
      });
    }
  }

  $('#show-answer').addEventListener('click', reveal);
  $('#practice-refresh').addEventListener('click', load);
  gradesEl.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-grade]');
    if (button) grade(button.dataset.grade);
  });

  window.addEventListener('keydown', (event) => {
    const target = event.target;
    if (
      target &&
      (target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.isContentEditable)
    ) {
      return;
    }

    if (!state.card) return;

    if (!state.revealed && (event.key === ' ' || event.key === 'Enter')) {
      event.preventDefault();
      reveal();
      return;
    }

    if (state.revealed && ['1', '2', '3', '4'].includes(event.key)) {
      event.preventDefault();
      grade({ 1: 'again', 2: 'hard', 3: 'good', 4: 'easy' }[event.key]);
    }
  });

  load();
})();
