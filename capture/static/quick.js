/* Flashcap quick capture — for PDFs and anything the userscript can't see. */

(() => {
  'use strict';

  const form = document.getElementById('quick-form');
  const textEl = document.getElementById('quick-text');
  const titleEl = document.getElementById('quick-title');
  const urlEl = document.getElementById('quick-url');
  const hintEl = document.getElementById('quick-hint');
  const frontEl = document.getElementById('quick-front');
  const backEl = document.getElementById('quick-back');
  const categoryEl = document.getElementById('quick-category');
  const cardTagsEl = document.getElementById('quick-card-tags');
  const suggestBtnEl = document.getElementById('quick-suggest');
  const suggestStatusEl = document.getElementById('quick-suggest-status');
  const annotationEl = document.getElementById('quick-annotation');
  const noteTagsEl = document.getElementById('quick-note-tags');
  const submitEl = document.getElementById('quick-submit');
  const noteBtn = document.getElementById('quick-note');
  const errorEl = document.getElementById('quick-error');
  const resultEl = document.getElementById('quick-result');
  const resultFront = document.getElementById('result-front');
  const resultBack = document.getElementById('result-back');
  const resultTags = document.getElementById('result-tags');
  const resultWarn = document.getElementById('result-warn');
  const resultEdit = document.getElementById('result-edit');
  const captureAnother = document.getElementById('capture-another');
  const noteResult = document.getElementById('note-result');
  const notePath = document.getElementById('note-path');
  const noteOpen = document.getElementById('note-open');
  const noteAnother = document.getElementById('note-another');
  const explainBtn = document.getElementById('quick-explain');
  const explainResult = document.getElementById('explain-result');
  const explainText = document.getElementById('explain-text');
  const explainAnnotate = document.getElementById('explain-annotate');
  const explainClear = document.getElementById('explain-clear');
  let currentExplanation = '';
  let explainSeq = 0;

  const showError = (message) => {
    errorEl.textContent = message;
    errorEl.classList.add('show');
  };

  const clearError = () => {
    errorEl.textContent = '';
    errorEl.classList.remove('show');
  };

  const setBusy = (busy, which = 'card') => {
    submitEl.disabled = busy;
    noteBtn.disabled = busy;
    explainBtn.disabled = busy;
    suggestBtnEl.disabled = busy;
    submitEl.textContent =
      busy && which === 'card' ? 'Saving card…' : 'Save card';
    noteBtn.textContent =
      busy && which === 'note' ? 'Saving annotation…' : 'Save annotation';
  };

  const hideResults = () => {
    explainSeq += 1;
    explainBtn.textContent = 'Explain';
    resultEl.classList.remove('show');
    noteResult.classList.remove('show');
    explainResult.classList.remove('show');
  };

  // -----------------------------------------------------------------
  // Bookmarklet
  // -----------------------------------------------------------------
  const origin = location.origin;
  const bookmarkletCode =
    "javascript:(()=>{const t=String(getSelection()).slice(0,12000);" +
    `window.open('${origin}/quick?text='+encodeURIComponent(t)+` +
    "'&url='+encodeURIComponent(location.href)+'&title='+encodeURIComponent(document.title)," +
    "'flashcap','width=560,height=760,menubar=no,toolbar=no,resizable=yes')})()";

  const bookmarklet = document.getElementById('bookmarklet');
  bookmarklet.setAttribute('href', bookmarkletCode);
  document.getElementById('bookmarklet-code').textContent = bookmarkletCode;

  // -----------------------------------------------------------------
  // Prefill from ?text=&title=&url=&hint=
  // -----------------------------------------------------------------
  const params = new URLSearchParams(location.search);
  const prefill = {
    text: params.get('text') || '',
    title: params.get('title') || '',
    url: params.get('url') || '',
    hint: params.get('hint') || '',
  };

  if (prefill.text) textEl.value = prefill.text.slice(0, 20000);
  if (prefill.title) titleEl.value = prefill.title.slice(0, 400);
  if (prefill.url) urlEl.value = prefill.url.slice(0, 2000);
  if (prefill.hint) hintEl.value = prefill.hint.slice(0, 500);

  // The passage arrived in the query string. Drop it from the address bar so
  // it does not linger in browser history or leak through a copied link.
  if (location.search) {
    history.replaceState(null, '', location.pathname);
  }

  if (prefill.text) {
    textEl.focus();
    textEl.setSelectionRange(textEl.value.length, textEl.value.length);
  }

  // -----------------------------------------------------------------
  // Clipboard
  // -----------------------------------------------------------------
  document.getElementById('paste-btn').addEventListener('click', async () => {
    try {
      const clip = await navigator.clipboard.readText();
      if (!clip.trim()) {
        showError('The clipboard is empty.');
        return;
      }
      textEl.value = textEl.value.trim()
        ? `${textEl.value.trim()}\n\n${clip}`
        : clip;
      textEl.focus();
      textEl.setSelectionRange(textEl.value.length, textEl.value.length);
      clearError();
    } catch (_) {
      showError('Clipboard access was blocked — press ⌘V in the passage box.');
      textEl.focus();
    }
  });

  // -----------------------------------------------------------------
  // Capture
  // -----------------------------------------------------------------
  const suggestQuick = async () => {
    const text = textEl.value.trim();
    if (!text) {
      showError('Paste or select a passage first.');
      textEl.focus();
      return;
    }

    suggestBtnEl.disabled = true;
    suggestStatusEl.textContent = 'Suggesting category and tags…';
    try {
      const response = await fetch('/api/suggest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          selection: text,
          context: text,
          page_title: titleEl.value.trim(),
          url: urlEl.value.trim(),
          source_type: 'quick',
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (data.category) categoryEl.value = data.category;
      if (data.tags && data.tags.length) {
        const joined = data.tags.join(', ');
        cardTagsEl.value = joined;
        if (!noteTagsEl.value.trim()) noteTagsEl.value = joined;
      }
      suggestStatusEl.textContent = data.tags && data.tags.length
        ? '✦ Reused existing tags — edit anything.'
        : 'No existing tag fits — add one manually.';
    } catch (error) {
      suggestStatusEl.textContent = 'No suggestions — fill these in.';
    } finally {
      suggestBtnEl.disabled = false;
    }
  };

  suggestBtnEl.addEventListener('click', suggestQuick);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    clearError();

    const text = textEl.value.trim();
    if (!text) {
      showError('Paste or select a passage first.');
      textEl.focus();
      return;
    }
    const front = frontEl.value.trim();
    const back = backEl.value.trim();
    if (!front || !back) {
      showError('Write a front and a back before saving the card.');
      (front ? backEl : frontEl).focus();
      return;
    }

    setBusy(true, 'card');
    hideResults();
    resultWarn.hidden = true;

    try {
      const response = await fetch('/api/capture', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          selection: text,
          context: text,
          page_title: titleEl.value.trim(),
          url: urlEl.value.trim(),
          hint: hintEl.value.trim(),
          front,
          back,
          category: categoryEl.value.trim(),
          tags: cardTagsEl.value.trim(),
          source_type: 'quick',
        }),
      });

      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }

      const card = data.card;
      resultFront.textContent = card.front;
      resultBack.textContent = card.back;
      resultTags.replaceChildren();

      const badge = document.createElement('span');
      badge.className = 'cat-badge';
      badge.textContent = String(card.category || 'other').replace(/-/g, ' ');
      resultTags.append(badge);

      for (const tag of card.tags || []) {
        const chip = document.createElement('span');
        chip.className = 'tag-chip';
        chip.textContent = `#${tag}`;
        resultTags.append(chip);
      }

      if (data.warning) {
        resultWarn.textContent = data.warning;
        resultWarn.hidden = false;
      }

      resultEdit.setAttribute('href', `/?card=${card.id}`);
      resultEl.classList.add('show');
      resultEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

      // Keep the passage/source; clear the written card for the next one.
      frontEl.value = '';
      backEl.value = '';
      categoryEl.value = '';
      cardTagsEl.value = '';
    } catch (error) {
      showError(error.message || 'Could not save the card.');
    } finally {
      setBusy(false);
    }
  });

  form.addEventListener('keydown', (event) => {
    if (event.target.tagName === 'TEXTAREA' && event.key === 'Enter' && !event.metaKey && !event.ctrlKey) {
      return; // normal newline
    }
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  // -----------------------------------------------------------------
  // Reading note (record, not a flashcard)
  // -----------------------------------------------------------------
  const saveNote = async () => {
    clearError();
    const text = textEl.value.trim();
    if (!text) {
      showError('Paste or select a passage first.');
      textEl.focus();
      return;
    }

    setBusy(true, 'note');
    hideResults();

    try {
      const response = await fetch('/api/annotate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          selection: text,
          context: text,
          page_title: titleEl.value.trim(),
          url: urlEl.value.trim(),
          hint: hintEl.value.trim(),
          annotation: annotationEl.value.trim(),
          category: categoryEl.value.trim(),
          tags: noteTagsEl.value.trim(),
          source_type: 'quick',
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);

      const note = data.note || {};
      notePath.textContent =
        `${note.vault || ''}/${note.path || ''}` +
        (note.queued ? ' · queued for your Mac' : '');
      noteOpen.hidden = Boolean(note.queued);
      noteOpen.setAttribute('href', note.open_url || '#');
      noteResult.classList.add('show');
      noteResult.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

      textEl.value = '';
      hintEl.value = '';
      annotationEl.value = '';
      noteTagsEl.value = '';
    } catch (error) {
      showError(error.message || 'Could not save the annotation.');
    } finally {
      setBusy(false);
    }
  };

  noteBtn.addEventListener('click', saveNote);

  // -----------------------------------------------------------------
  // Inline explanation (not saved until you use it as an annotation)
  // -----------------------------------------------------------------
  const runExplain = async () => {
    clearError();
    const text = textEl.value.trim();
    if (!text) {
      showError('Paste or select a passage first.');
      textEl.focus();
      return;
    }

    hideResults();
    const seq = ++explainSeq;
    explainResult.classList.add('show');
    explainText.textContent = 'Thinking…';
    explainBtn.disabled = true;
    explainBtn.textContent = 'Thinking…';
    currentExplanation = '';

    try {
      const response = await fetch('/api/explain', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          selection: text,
          context: text,
          page_title: titleEl.value.trim(),
          url: urlEl.value.trim(),
          hint: hintEl.value.trim(),
          source_type: 'quick',
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (seq !== explainSeq) return;
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);

      currentExplanation = (data.explanation || '').trim();
      explainText.textContent =
        currentExplanation || '(No explanation returned.)';
      explainResult.classList.add('show');
      explainResult.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (error) {
      if (seq !== explainSeq) return;
      currentExplanation = '';
      explainResult.classList.remove('show');
      showError(error.message || 'Could not explain the passage.');
    } finally {
      if (seq === explainSeq) {
        explainBtn.disabled = false;
        explainBtn.textContent = 'Explain';
      }
    }
  };

  explainBtn.addEventListener('click', runExplain);

  explainAnnotate.addEventListener('click', () => {
    if (!currentExplanation) return;
    const existing = annotationEl.value.trim();
    annotationEl.value = existing
      ? `${existing}\n\n${currentExplanation}`
      : currentExplanation;

    const tagValue = noteTagsEl.value.trim();
    if (!/(^|,\s*)ai-explanation(,|$)/i.test(tagValue)) {
      noteTagsEl.value = [tagValue, 'ai-explanation']
        .filter(Boolean)
        .join(', ');
    }
    annotationEl.focus();
    explainResult.classList.remove('show');
    annotationEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });

  explainClear.addEventListener('click', () => {
    explainSeq += 1;
    explainResult.classList.remove('show');
    explainBtn.disabled = false;
    explainBtn.textContent = 'Explain';
  });

  captureAnother.addEventListener('click', () => {
    clearError();
    hideResults();
    textEl.value = '';
    hintEl.value = '';
    frontEl.value = '';
    backEl.value = '';
    categoryEl.value = '';
    cardTagsEl.value = '';
    annotationEl.value = '';
    noteTagsEl.value = '';
    suggestStatusEl.textContent = '';
    currentExplanation = '';
    textEl.focus();
  });

  noteAnother.addEventListener('click', () => {
    clearError();
    hideResults();
    textEl.value = '';
    hintEl.value = '';
    frontEl.value = '';
    backEl.value = '';
    categoryEl.value = '';
    cardTagsEl.value = '';
    annotationEl.value = '';
    noteTagsEl.value = '';
    suggestStatusEl.textContent = '';
    currentExplanation = '';
    textEl.focus();
  });
})();
