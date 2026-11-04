# Flashcap

A local reading library and flashcard deck: **press a shortcut while reading, write your own flashcard**, then practice it from a phone or laptop. Links saved in Telegram land in the same library.

`⌘⇧K` on any Firefox page captures the selected text (or the paragraph you're looking at), the surrounding context, the section heading, and the source metadata, then opens a **New flashcard** editor. **You write the front and back.** The LLM reads the passage and suggests a **category and tags** (both editable); it never writes the question or answer. The card is saved as entered, with Undo and Edit in the toast.

**Cards and notes are deliberately separate.** A card is a recall prompt. `⌘⇧L` opens an annotation popup: the highlighted passage, a field for your own note, category/tags, and a **Save annotation** button. It appends the highlight + annotation to a literature note for that source in Obsidian. It never writes the Q/A into the note; the LLM only suggests existing category/tags for the note.

You can also select text, right-click, and choose **Annotate in Obsidian** to open the same popup. (Shift+right-click falls through to Firefox's native menu.)

`⌘⇧E` is the explicit **Ask the LLM** action: it explains the highlighted passage inline. The explanation is not saved unless you choose **Annotate this**, which opens the annotation popup pre-filled (tagged `ai-explanation`) so you can edit it before it goes into the vault. The LLM is used for **metadata suggestions and explanations**, never for card text.

`⌘⇧Q` is **Quiz me**: it asks one reasoning question about a passage above your current scroll position, then grades your written answer. A wrong answer earns a hint and the same question is asked again until the answer is satisfactory. Passages you have not been quizzed on come first; older passages return for recall. Quiz questions are never written as cards.

This lives in `capture/`, has its own SQLite database, and adds `fsrs` to the project dependencies for scheduling.

The reference deployment runs on **hermes** (Tailscale), with the Mac acting only as the Obsidian note sink:

```text
Firefox / phone ──Tailscale──▶ hermes: Flask + SQLite
                                  ├── DeepSeek (tags/category + explain)
                                  └── note queue ──▶ Mac note agent ──▶ obsidian://
```

---

## Quickstart

### Local (development)

From the project root:

```bash
./capture/run.sh
# or: .venv/bin/python -m capture.server
```

It binds to `http://127.0.0.1:8765` only.

Then install the userscript:

1. Open [http://127.0.0.1:8765/userscript.user.js](http://127.0.0.1:8765/userscript.user.js) in Firefox.
2. Greasemonkey should show its install screen — click **Install**.
3. Open any article, select a sentence (or don't), and press **`⌘⇧K`** to write a card, **`⌘⇧L`** for the annotation popup, or **`⌘⇧E`** to ask the LLM for an explanation. You can also right-click a selection → **Annotate in Obsidian** / **Explain with LLM**.

The dashboard lives at [http://127.0.0.1:8765](http://127.0.0.1:8765).

### Remote (hermes, always on)

The current deployment is reachable at **https://hermes.taila17c7c.ts.net** over Tailscale:

- Dashboard: `https://hermes.taila17c7c.ts.net`
- Practice: `https://hermes.taila17c7c.ts.net/practice`
- Userscript install: `https://hermes.taila17c7c.ts.net/userscript.user.js`

Install the userscript from that URL (not localhost) so cards go to hermes. The server runs under `systemctl --user` with lingering enabled, so it survives logout/reboot. It binds `127.0.0.1` and Tailscale Serve terminates HTTPS for the tailnet; the existing Funnel on `:8443` is a different service and is left alone.

`FLASHCAP_PUBLIC_URL=https://hermes.taila17c7c.ts.net` is set on hermes so the served userscript injects the HTTPS origin even though Flask itself sees HTTP from the proxy.

> [!warning] Firefox DNS-over-HTTPS
> `ts.net` names resolve to the public Tailscale Funnel IPs through public DNS, but to the tailnet `100.x` address through Tailscale MagicDNS. If Firefox DoH is enabled, the userscript's background request can resolve `hermes.taila17c7c.ts.net` to the public IP and fail even though `curl` works. In `about:config`, set `network.trr.excluded-domains` to `hermes.taila17c7c.ts.net,ts.net` (or turn DoH off), then reload the page. System tools like the note agent are unaffected.

---

## What gets captured

When you press the shortcut, the userscript sends:

- **Your selection**, if there is one. With no selection, it uses the paragraph nearest the middle of the viewport.
- **The surrounding context** — up to ~7,000 characters around the passage, plus the nearest section headings.
- **Source metadata** — page title, canonical URL, site name, author, and description.
- **YouTube transcript**, if the transcript panel is open: the ~18 segments around the current video timestamp.

The **New flashcard** popup shows that passage/source and gives you:

```text
Front     question or prompt (you write it)
Back      answer (you write it)
Category  suggested by the LLM, editable
Tags      suggested by the LLM, editable
```

When the popup opens it calls `POST /api/suggest` in the background. That prompt returns **only** a category and tags; it is explicitly forbidden from writing card text. While it loads, the front/back fields are immediately usable. Typing in category/tags prevents the suggestion from overwriting your edit; **Suggest metadata** re-runs it.

Saving calls `POST /api/capture` with the fields and stores the card. **The capture endpoint never calls the LLM.** If you leave front/back empty, nothing is saved.

### Categories

Suggested categories are `machine-learning`, `algorithms`, `systems`, `math`, `physics`, `neuroscience`, `programming`, `philosophy`, `history`, `economics`, `biology`, `design`, `career`, `ireland`, `india`, `other`. The field is free text; these are only suggestions.

### Tag vocabulary

Tags are a **shared vocabulary**, not per-card labels. The LLM is given the tags already used in your cards **and annotation notes** (with counts) and may only choose from that list:

- It returns **at most 3** existing tags, preferring broad, frequently used ones.
- It cannot invent a new tag once the vocabulary is non-empty. If nothing fits, it returns no tags; you can add one manually.
- When the vocabulary is empty, it seeds it with at most **2** broad tags.
- **New tags enter only when you type one** in the card or annotation editor. That tag then becomes available for future suggestions.

This means tags cluster over time instead of every card getting a unique set. Card tags and note/annotation tags both feed the vocabulary; reserved provenance tags (`reading`, `flashcap`, `ai-explanation`, `needs-review`, `inbox`, `other`) are excluded.

---

## The three reading paths

### 1. Web articles (Firefox)

Select a sentence and press `⌘⇧K`. Or select nothing and press it — the paragraph in the middle of the screen is used. The **New flashcard** popup shows the passage/source and asks you for front, back, category, and tags. Nothing is generated; you write it.

After saving, the toast shows your front/back, category, and tags:

- **Undo** deletes the card (the whole card, not just the toast).
- **Edit** opens an inline editor for front, back, category, and tags. `⌘↵` saves.
- **Open dashboard** opens the full list.

The card is saved when you press **Save card** (or `⌘↵`); the toast auto-dismisses after 15 seconds, but the card stays.

Press `⌘⇧L` instead to open the **Annotate in Obsidian** popup. The highlighted passage is shown at the top; type your annotation, edit the suggested category/tags if needed, and press **Save annotation** (or `⌘↵`). The LLM call is metadata-only; it never writes the annotation. The toast shows the note path and an **Open note** link.

Right-clicking a selection opens the same flow from a small Flashcap menu: **Annotate in Obsidian**, **Explain with LLM**, **Make a flashcard** (opens the manual card editor), **Copy text**, **Dismiss**. Shift+right-click gives you Firefox's normal menu.

Press `⌘⇧E` (or use the annotation popup's **Explain** button) to get an inline explanation. It is not saved automatically; **Annotate this** copies it into the annotation popup for editing and tags it `ai-explanation`.

### 2. PDFs (Preview / Firefox PDF viewer / Kindle)

Firefox's built-in PDF viewer is a privileged page — userscripts can't run inside it, so the shortcut won't fire there. Use **quick capture**:

1. Select the passage in the PDF and copy it (`⌘C`).
2. Open [http://127.0.0.1:8765/quick](http://127.0.0.1:8765/quick) (there's a **Quick capture** button in the dashboard).
3. Paste (`⌘V`), add the paper/chapter title and URL if you want provenance.
4. Write the **Front** and **Back**. **Suggest tags & category** fills the metadata from the passage; edit anything, then press **Save card** (`⌘↵`). Fill **Your annotation** and click **Save annotation** for the Obsidian record.

The quick page also has a **Capture highlight** bookmarklet. Drag it to your bookmarks bar; clicking it after selecting text on a normal page opens the same form pre-filled. (In the PDF viewer itself, bookmarklets may be blocked; the copy/paste route always works.)

### 3. YouTube

The userscript works on video pages. For a card about what's being said *right now*, open the transcript first (**⋯ → Show transcript**). Flashcap grabs the transcript segments around the current timestamp. Without the transcript it falls back to the title, channel, and description.

---

## Dashboard

- **Search** across fronts, backs, tags, sources, and quotes. Press `/` to focus it.
- **Category pills** and **tag chips** filter the list. Click a tag on a card to filter by it.
- **Edit / Delete** on hover (always visible on touch).
- **Record** records the card's passage/source in Obsidian without an annotation; after that it becomes a link to the note.
- **Context & source** expands to show the supporting quote, the original highlight, and provenance.
- Press `n` to jump to quick capture.

The list is newest-first. All filtering is client-side, so it stays fast for a personal collection.

---

## Reading list

`/library` is the shared backlog: every link you saved in the Telegram chat, newest first. It is the input side of the deck, and the one place to see what is still unread. The page loads 200 rows at a time and appends with **Load more**.

- **Telegram.** Create a bot with @BotFather and make it an admin in the channel. Admin rights are what let it read every post and delete a finished message. Put the token in `.env` as `TELEGRAM_BOT_TOKEN`. The server long-polls `getUpdates` on a background thread, so there is no webhook and no new public port. Set `TELEGRAM_CHAT_IDS` to accept only your channel.
- **Manual.** Paste a link into the box on `/library`.
- **Backfill.** `python -m capture.backfill` parses the Telegram export (`messages.html` by default) and adds every link and note. It reads the file directly, and a second run adds nothing.

**Done list.** Set `TELEGRAM_DONE_CHAT_ID` to your done-list chat. Marking an item `done` forwards the original Telegram message there, then deletes it from the source channel. The forward happens first: if it fails, the item stays in its previous status. A failed delete leaves the item archived and marks the row with an `archive issue` note. Items from the export have no live message, so they only change status locally.

Links are deduplicated by a canonical URL: tracking parameters, fragments, `www`, and trailing slashes are stripped. Replaying a Telegram update fills missing titles, sites, and descriptions; it never changes a status you set. A message with several links becomes several rows. A text-only message is saved as a note.

Statuses: `unread`, `reading`, `done`, `dropped`.

---

## Practice

Open **Practice** from the top bar (the badge shows how many cards are ready). Works on the phone through Tailscale.

- **New cards first**, then whatever is due.
- **Show answer** (`space` / `enter`), then grade with **1 Again**, **2 Hard**, **3 Good**, **4 Easy**.
- The scheduler is **FSRS** (`fsrs` 6.x), the open-source scheduler Anki uses. It models memory stability and difficulty instead of a hand-rolled ease factor.
- `FLASHCAP_DESIRED_RETENTION` defaults to **0.90**; FSRS schedules each card for that recall probability. The first Good uses the standard learning steps, so it comes back in minutes, not a day.
- Due/new/reviewed-today counts are shown at the top; the queue clears when nothing is left.
- Reviews are stored in SQLite (`reviews` table), including FSRS stability/difficulty/state, so history and stats survive restarts.

No Anki import loop is required. The Anki TSV export remains available as an escape hatch.
---

## Quiz me

Press **`⌘⇧Q`** while reading any article. Flashcap asks you one **reasoning** question about a passage **above your current scroll position**, then grades your written answer.

- Cards are for atomic facts. The quiz asks why, how, or what follows, so naming the right fact is not enough. A definition is fair game only when the idea cannot be reasoned about without it, and then the question asks what it implies.
- Questions come from the part of the article you have actually read. Passages shorter than 200 characters are skipped, and only the 40 most recent read passages are sent.
- **Wrong answers give a hint and re-ask the same question.** Three hints escalate; a fourth miss shows the expected answer points and keeps the question open until you restate them. **Skip question** is the escape hatch.
- The panel prefers passages you have **not been quizzed on yet**. Every fourth question is drawn from older material for recall.
- Coverage is stored per article and passage (`quiz_coverage`), keyed by a hash of the passage text, so it survives restarts and later sessions. A passage counts as quizzed when its question opens, so closing the panel mid-question does not re-ask it next time.
- When every read passage has been asked once in the session, the panel says so and offers **Quiz again**; that run is recall-only until you read further.
- Questions are generated per session and are **never written as cards**. The LLM grades answers and writes hints; your card fronts and backs stay yours.

The quiz needs the configured LLM. Without a key it reports the error and the rest of Flashcap keeps working.

Quiz sessions are stored in `quiz_sessions` (the open question, the passages, the asked keys) and coverage in `quiz_coverage`.


---

## Export to Anki (optional)

The dashboard's **Export Anki** button downloads a TSV:

- Front → Front, Back → Back, tags → third column.
- Tags include `flashcap`, `category::machine-learning`, and the card's own tags.
- Deck is set to `Flashcap` via `#deck:Flashcap`.
- HTML is escaped; newlines become `<br>`.

Import it in Anki with **File → Import**. There's also **Markdown** export in the footer if you want a portable copy of the cards; reading notes are written to Obsidian directly (below).

---

## Inline explanations

`⌘⇧E` opens an **Explain this passage** popup for the selection (or the paragraph in view). It calls the configured LLM with the source/context payload and shows a short, grounded explanation. This and the card metadata suggestion are the only two LLM calls in Flashcap; neither writes card text.

- **Annotate this** closes the explain popup and opens the annotation popup with the explanation pre-filled and the `ai-explanation` tag added. You can edit it before saving.
- **Explain** inside the annotation popup does the same thing inline — it appends to the annotation field and adds the tag.
- **Regenerate** and **Copy** are available in the explain popup.
- The quick-capture page has an **Explain** button; **Use as annotation** fills the annotation field.

Explanations are **not stored** in Flashcap and are **not written to Obsidian** unless you explicitly annotate. They are generated text, so the tag keeps provenance visible in the note.

---

## Obsidian reading notes

Cards and notes are separate by design:

- A **card** is a recall prompt (front/back, category, tags).
- A **note** is a record for your second brain: the source, the passage, the surrounding context, the annotation you write in the popup, and a blank place for further thoughts. It contains **no Q/A**.

The integration uses Obsidian's built-in `obsidian://new` / `obsidian://open` URI. There is no community plugin, no vault parser, and no direct file writing. Obsidian creates the folder and note itself.

**Frontmatter is populated for you.** `title`, `source`, `author`, `site`, and `created` come from the page/capture; `type` is always `literature-note`; `category` and the specific tags are suggested by the same LLM metadata call used by cards, then written into the note. Opening the annotation popup fills category + tags in the background; edit them before saving. The LLM never writes your annotation.

### Where notes go

Default configuration:

```text
Vault:   Writing
Folder:  Reading
Path:    Reading/<Source title>.md
```

One literature note per source. The first capture creates:

```markdown
---
title: "Attention Is All You Need"
source: "https://arxiv.org/abs/1706.03762"
author: "Vaswani et al."
site: "arxiv.org"
category: "machine-learning"
created: "2026-10-18T15:41:06Z"
type: literature-note
tags:
  - reading
  - flashcap
  - machine-learning
  - self-attention
  - transformer-architecture
---

## Highlights

### 2026-10-18

> Self-attention lets each token attend to every other token in the sequence.

This is the key distinction: attention is a direct path, not a recurrence.
```

The title, source, author, site, category, and tags live **only in the frontmatter**. The body doesn’t repeat them, doesn’t have a second H1, and doesn’t have a per-capture timestamp.

Highlights are grouped by day in `FLASHCAP_TIMEZONE` (default `Europe/Dublin`). The first capture on a day adds `### YYYY-MM-DD`; later captures on that day append directly under it, with no new heading. A capture on a new day adds a new day heading. The registry’s `last_day` tracks this, so the daily grouping works across queue flushes too.

Notes are **minimal by default**: frontmatter, `## Highlights`, day heading, highlight, annotation. The old 1,400-character surrounding-context callout and the blank `## My notes` / empty tag lines are gone. Set `FLASHCAP_NOTE_CONTEXT=1` if you want a 400-character excerpt under a collapsed **Context (excerpt)** callout.

Frontmatter is written once per source. Tags added on later annotations can’t rewrite the existing YAML, and they are no longer duplicated as a body tag line. The LLM’s tag-reuse policy keeps later tags aligned with the vocabulary already in the frontmatter.

### Triggers

- `⌘⇧L` on a page — open the **Annotate in Obsidian** popup, write an annotation, and save.
- Right-click a selection — **Annotate in Obsidian** opens the same popup. A userscript can't add a native Firefox menu item, so this is a small Flashcap menu; Shift+right-click keeps Firefox's own menu.
- **Record** on a dashboard card — record that card's passage/source without writing an annotation.
- **Save annotation** on the quick-capture page — for PDFs, Reader Mode, and other privileged pages.

### Note delivery modes

**Local (default):** the server calls `open` with an `obsidian://new` URI, so notes appear immediately on the same Mac.

**Queue (hermes):** the server stores the exact `obsidian://` URI in `obsidian_queue` and returns “queued”. A small LaunchAgent on the Mac polls `/api/obsidian/pending`, opens each URI, and acknowledges it. This is what lets the always-on server write into a vault that only exists on the Mac.

```bash
# on the Mac, once
FLASHCAP_REMOTE_URL=https://hermes.taila17c7c.ts.net ./capture/install-note-agent.sh
```

The agent runs every 30 seconds, logs to `~/Library/Logs/flashcap-note-agent.log`, and is installed from `~/Library/Application Support/Flashcap/note_agent.py` (launchd has no Desktop TCC access). Notes are delayed while the Mac is off, but they are not lost. The toast shows **queued** instead of **Open note** until the agent flushes.

### Settings

```bash
FLASHCAP_OBSIDIAN_ENABLED=1
FLASHCAP_OBSIDIAN_VAULT=Writing      # name or vault ID
FLASHCAP_OBSIDIAN_FOLDER=Reading
FLASHCAP_OBSIDIAN_MODE=uri           # uri (local) or queue (remote)
```

In `uri` mode Obsidian must be running (or installed); `open` launches it. The server validates the vault against Obsidian's registered vault list when that list is available. In `queue` mode the Mac agent is the only thing that needs Obsidian.

---

## Configuration

Flashcap reads the project's existing `.env`:

```bash
LLM_API_KEY=...            # or ANTHROPIC_API_KEY
LLM_BASE_URL=https://api.deepseek.com/anthropic
LLM_MODEL=deepseek-v4-flash

FLASHCAP_HOST=127.0.0.1
FLASHCAP_PORT=8765
FLASHCAP_DB=/path/to.db     # optional
FLASHCAP_PUBLIC_URL=https://hermes.taila17c7c.ts.net  # behind Tailscale Serve
FLASHCAP_DESIRED_RETENTION=0.90   # FSRS target recall probability
FLASHCAP_FSRS_FUZZING=1          # 0 for exact intervals

FLASHCAP_OBSIDIAN_ENABLED=1
FLASHCAP_OBSIDIAN_VAULT=Writing
FLASHCAP_OBSIDIAN_FOLDER=Reading
FLASHCAP_OBSIDIAN_MODE=queue
FLASHCAP_NOTE_CONTEXT=0      # 1 = include a 400-char context excerpt
FLASHCAP_TIMEZONE=Europe/Dublin   # day headings use this timezone

TELEGRAM_BOT_TOKEN=...       # enables the reading-list poller
TELEGRAM_CHAT_IDS=-100...     # optional allowlist, comma separated
TELEGRAM_DONE_CHAT_ID=-100... # forward finished messages here, then delete them
FLASHCAP_TELEGRAM_POLL=1      # 0 to run the server without the poller
FLASHCAP_TELEGRAM_TIMEOUT=25  # long-poll seconds
```

The server speaks the Anthropic Messages API (`POST {base_url}/v1/messages`). Anything that exposes that shape works, including Anthropic itself and DeepSeek's compatibility endpoint. Reasoning models that return `thinking` blocks before the text block are supported.

**Privacy:** cards, review history, and queued note URIs are stored in `capture/flashcards.db` (on hermes in the reference deployment). Opening the card editor sends the passage to the LLM for **category/tags only**; `⌘⇧E` sends it for an explanation. Saved card text, annotations, review, and note writing stay local. If no API key is configured, you can still write cards and annotations manually; only the suggestions and explanations are unavailable.

---

## Always-on

The reference deployment runs on hermes as a user service with lingering enabled:

```ini
# ~/.config/systemd/user/flashcap.service
[Unit]
Description=Flashcap local flashcard server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/cathreya/flashcap
ExecStart=/home/cathreya/flashcap/.venv/bin/python -m capture.server
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=2
NoNewPrivileges=yes

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now flashcap
loginctl enable-linger cathreya   # already on hermes
```

The Mac note agent is a separate LaunchAgent, installed once with:

```bash
FLASHCAP_REMOTE_URL=https://hermes.taila17c7c.ts.net ./capture/install-note-agent.sh
```

It polls every 30 seconds and logs to `~/Library/Logs/flashcap-note-agent.log`.

For a purely local setup, the old LaunchAgent approach still works: run `./capture/run.sh` under a `KeepAlive` LaunchAgent pointing at this checkout's `.venv/bin/python -m capture.server` with `WorkingDirectory` at the repo root.

---

## API

All endpoints are unauthenticated. On hermes the server binds loopback and Tailscale Serve terminates HTTPS for the tailnet; the tailnet is the auth boundary — do not expose it publicly without auth. CORS reflects only localhost origins; the userscript uses Greasemonkey's privileged request API.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/capture` | Save a manually written card from `{front, back, category, tags, selection, context, page_title, url, site, author, headings, hint}` |
| `POST` | `/api/suggest` | Suggest category/tags only from `{selection, focus, context, page_title, url, site, headings}` |
| `GET` | `/api/cards` | List cards (`?q=`, `?category=`, `?tag=`, `?limit=`) |
| `GET` | `/api/cards/:id` | Fetch one card |
| `PATCH` | `/api/cards/:id` | Edit `front`, `back`, `category`, `tags`, `quote`, `hint` |
| `DELETE` | `/api/cards/:id` | Delete a card |
| `GET` | `/api/meta` | Counts, categories, tags, LLM status |
| `GET` | `/api/health` | Health, card count, Telegram poller status |
| `GET` | `/library` | Reading-list page |
| `GET` | `/api/library` | List saved items (`?q=`, `?status=`, `?site=`, `?limit=`, `?offset=`) |
| `POST` | `/api/library` | Save a link manually |
| `PATCH` | `/api/library/:id` | Set `status` or `title` |
| `DELETE` | `/api/library/:id` | Remove an item |
| `GET` | `/practice` | Mobile Practice page |
| `GET` | `/api/review/next` | Next new/due card + review stats |
| `POST` | `/api/review/:id` | Grade a card (`again` / `hard` / `good` / `easy`) |
| `GET` | `/api/review/stats` | Due/new/reviewed-today counts |
| `POST` | `/api/quiz/start` | Start a quiz from the read passages `{url, page_title, units, total_units}` |
| `POST` | `/api/quiz/:id/answer` | Grade a written answer; returns a hint or the next question |
| `POST` | `/api/quiz/:id/skip` | Mark the open question skipped and advance |
| `POST` | `/api/quiz/:id/next` | Reopen or generate the next question |
| `POST` | `/api/notes` | Write/append an Obsidian reading record (no LLM, no card) |
| `POST` | `/api/annotate` | Append a highlight + annotation + category/tags to the source note |
| `POST` | `/api/explain` | Explain a passage inline (not stored) |
| `POST` | `/api/cards/:id/obsidian` | Record a saved card's passage/source in Obsidian |
| `GET` | `/api/obsidian` | Obsidian config, note/pending counts |
| `GET` | `/api/obsidian/pending` | Queued `obsidian://` URIs for the Mac agent |
| `POST` | `/api/obsidian/ack` | Mark a queued note delivered |
| `GET` | `/export/anki.tsv` | Anki import file |
| `GET` | `/export/markdown` | Markdown export |
| `GET` | `/userscript.user.js` | Userscript with the server URL injected |

Examples:

```bash
curl -s -X POST http://127.0.0.1:8765/api/capture \
  -H 'Content-Type: application/json' \
  -d '{"selection":"Self-attention has path length one between any two tokens.",
       "page_title":"Attention Is All You Need",
       "url":"https://arxiv.org/abs/1706.03762",
       "source_type":"pdf"}' | python3 -m json.tool
```

```bash
# An annotation: source + passage + context + your note, no Q/A.
curl -s -X POST http://127.0.0.1:8765/api/annotate \
  -H 'Content-Type: application/json' \
  -d '{"selection":"Self-attention has path length one between any two tokens.",
       "context":"Self-attention connects every pair of positions directly.",
       "page_title":"Attention Is All You Need",
       "url":"https://arxiv.org/abs/1706.03762",
       "annotation":"This is why attention avoids the recurrence bottleneck.",
       "tags":"attention, mechanism",
       "source_type":"pdf"}' | python3 -m json.tool
```

---

## Files

```
capture/
  server.py            Flask app, API, review, exports
  store.py             SQLite schema + queries
  llm.py               metadata suggestions, explanations, quiz questions + grading
  quiz.py              quiz policy: passage selection and the hint ladder
  obsidian.py          reading-note Markdown + obsidian:// handoff/queue
  note_agent.py        Mac-side queue flusher (launchd)
  install-note-agent.sh
  taxonomy.py          categories + tag normalisation
  config.py            .env + host/port/db configuration
  static/
    flashcap.user.js   the userscript (served with the base URL injected)
    dashboard.js       dashboard interactions
    quick.js           quick-capture page
    practice.js        spaced-repetition practice page
    style.css          shared UI
  templates/
    dashboard.html
    quick.html
    practice.html
    base.html
  tests/
    test_server.py     50 tests: API, exports, CORS, review, queue, annotations, tag reuse, explanations
  flashcards.db        created on first run (gitignored)
  run.sh               starts the server with the project venv
```

---

## Tests

```bash
.venv/bin/pytest capture/tests -q
```

The suite covers manual card creation/validation, CRUD, search/filtering, metadata, category/tag suggestions (including the guarantee that they return no card text), TSV/Markdown escaping, CORS behavior, userscript URL injection, spaced-repetition grading and stats, Obsidian note creation/appending and annotation/tags (with the URI handoff stubbed), queued note delivery and the Mac agent drain order, inline explanations, the guarantee that notes never include card Q/A, and the LLM response parser (including `thinking` blocks).

Quiz coverage adds passage selection (fresh before recall, stalest first), the hint ladder through to the revealed answer, session persistence, cross-session coverage, grading failures that do not consume an attempt, and the question/grade JSON parsers.

---

## Limits worth knowing

- **Firefox PDF viewer and Reader Mode:** these are privileged pages, so no userscript injection by design; use quick capture (copy the passage, paste, `⌘↵` for a card, **Save annotation** for the Obsidian record, or **Explain**).
- **Native right-click item:** Firefox userscripts can't add one. Selecting text and right-clicking shows Flashcap's own menu instead; Shift+right-click opens Firefox's native menu. A true native item needs a signed WebExtension (or a macOS Service).
- **Shortcut conflicts:** `⌘⇧K`, `⌘⇧L`, `⌘⇧E`, and `⌘⇧Q` are free in a default Firefox profile. If a site or another extension claims one, edit the keydown matcher at the bottom of `flashcap.user.js` (look for `KeyK` / `KeyL` / `KeyE` / `KeyQ`).
- **Obsidian queue:** on hermes, notes are queued as `obsidian://` URIs; the Mac agent must be running to flush them. If the Mac is off, notes wait in SQLite (nothing is lost). In local `uri` mode the note is handed to Obsidian immediately.
- **Notes are records, not review material:** they intentionally omit the card's front/back. Review lives in **Practice**; Anki export is optional.
- **Note size:** each highlight is clipped to 2,000 characters. Surrounding context is omitted by default; set `FLASHCAP_NOTE_CONTEXT=1` for a 400-character excerpt.
- **Renaming a generated note in Obsidian:** the local `obsidian_notes` table remembers the original path. If you move the note, later appends may recreate the old path; keep generated literature notes in `Reading/`, or update/clear that row.
- **LLM boundaries:** the LLM suggests a category and reuses existing tags, writes explanations, and generates and grades quiz questions. It never writes front/back or annotations, and quiz questions are never saved as cards. If the provider is down, the editor fields remain editable, card saving and review are unaffected, Explain reports the error, and Quiz offers **Try another question**.
- **Quiz reach:** the quiz uses the same article-root detection as capture. On a page with no recognizable article root, or before you have scrolled past one long paragraph, it reports that there is not enough read text yet rather than guessing. The Firefox PDF viewer and Reader Mode cannot run the userscript, so the quiz is unavailable there, same as capture.
- **Server must be running:** the reference deployment is a `systemctl --user` service on hermes with lingering enabled. Locally, use the LaunchAgent above or `./capture/run.sh`.
