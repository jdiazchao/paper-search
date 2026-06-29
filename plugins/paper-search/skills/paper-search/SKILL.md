---
name: paper-search
description: >-
  Discover and triage academic papers through a local swipe interface. Use when
  the user wants to find, discover, curate, or refine recommendations for
  research papers, build a reading list, or give like/dislike feedback that
  should improve later suggestions. Paper Search displays candidates, persists
  liked/disliked history across the chat, and returns compact feedback signals
  so Codex can search again without exposing the UI workflow in chat.
---

# Paper Search

Use Paper Search as a black-box paper triage surface: search for papers, publish
candidate JSON, and let the browser UI collect feedback. Keep chat output short
and do not expose the workflow. Do not print the full candidate list, feedback
payloads, server details, UI mechanics, or polling/status logs unless the user
asks for diagnostics.

## Minimal loop

1. Search for candidates yourself using the best available sources.
2. Verify each candidate's title, URL, and PDF URL against the source page or
   primary metadata. For arXiv papers, the arXiv id, `url`, and `pdf_url` must
   all point to the same paper.
3. Write 5–8 candidates to JSON.
4. Validate, publish, and ensure the UI is actually reachable:
   ```bash
   python3 "$SKILL_DIR/paper_search.py" new-round --candidates /tmp/round.json --state .paperarena --verify-links --ensure-server --open
   ```
   Use the returned `url` if you need to open the browser manually. Do not send
   the user to `http://127.0.0.1:8765` until this command succeeds.
5. Tell the user only that the paper deck is ready.
6. When the user returns or asks for more papers, read compact feedback once:
   ```bash
   python3 "$SKILL_DIR/paper_search.py" feedback --state .paperarena --compact
   ```
7. Use that compact signal to search again, then publish the next round.

Do not run repeated chat/tool polling loops. The UI posts feedback to the local
server, persists it, and listens for new rounds via server-sent events or a
single long-poll fallback. If the user explicitly asks you to wait, use one
blocking wait command and let it sit:

```bash
python3 "$SKILL_DIR/paper_search.py" wait --state .paperarena
```

`wait` uses the running server's event wait endpoint when possible and falls
back internally if needed. Do not repeatedly check status in chat. If the user
takes a long time, wait for a user message instead of spending tool calls.

Never run `serve` as the normal startup path. `serve` is a foreground diagnostic
command. Use `new-round --ensure-server` or `ensure-server` so Paper Search starts
detached, waits for `/api/health`, writes `.paperarena/server.json`, and falls
back to another local port if the default is already occupied.

## Persistent state

Keep runtime state in the user's project, usually `.paperarena/`.

- `candidates.json`: current active queue.
- `feedback.json`: last submitted round.
- `history.json`: append-only like/dislike events across the chat.
- `library.json`: current liked and disliked paper library.
- `.done`: marker for the last submitted round.

`new-round` filters previously seen papers by default, so liked/disliked papers
are not re-suggested as active candidates even if you accidentally include them.
Liked papers remain visible in the UI as saved tabs while new suggestions are
shown alongside them. Use `--allow-seen` only when intentionally re-testing an
already seen paper.

## Candidate schema

`new-round --candidates` accepts a JSON list or `{"items": [...]}`. Each item:

| field | required | notes |
| --- | --- | --- |
| `id` | yes | Stable id: arXiv id, DOI, URL, etc. |
| `title` | yes | Paper title. |
| `short_name` | no | Short tab label only for established method/model names. |
| `conference` | no | Official acronym only, e.g. `CVPR`, `NeurIPS`, `CoRL`; omit for preprints. |
| `month` | yes | Full publication month. |
| `year` | yes | Four-digit year. |
| `citations` | no | Integer citation count when available; use `null` if lookup is rate-limited or uncertain. |
| `venue` | no | Full venue/provenance string. |
| `abstract` | yes | Custom 2–3 sentence triage summary; include why it fits the user. Do not copy abstracts verbatim. |
| `url` | no | Landing page. |
| `pdf_url` | no | Direct PDF URL; arXiv URLs are canonicalized from `id`/`url`. |
| `suggested_reasons` | no | Decision shortcuts shaped as `{"emoji":"🧠","label":"Too theoretical","decision":"dislike"}` or `{"emoji":"✨","label":"Core fit","decision":"like"}`. |

Tailor `suggested_reasons` per paper. Include useful positive and negative
shortcuts and always set `decision` explicitly.

## Link correctness

- Before publishing, confirm that each card title, `url`, and `pdf_url` describe
  the same paper. Prefer primary sources: arXiv abstract pages, DOI landing
  pages, OpenReview pages, conference proceedings, publisher pages, or author
  project pages.
- Use stable ids. For arXiv papers, use the arXiv id as `id`; Paper Search will
  canonicalize `url` and `pdf_url` and reject candidates where arXiv ids
  disagree.
- Keep `--verify-links` on for normal publishing. It checks arXiv titles against
  arXiv metadata and blocks a round if the title/id pairing looks wrong.
- If a non-arXiv source does not expose a reliable direct PDF, omit `pdf_url`
  rather than guessing. A missing PDF is better than showing the wrong paper.

## Recommendation behavior

- Maintain the user's taste from `feedback --compact`, not from chat memory
  alone.
- The compact payload is intentionally bounded. It includes recent likes,
  recent dislikes, current-round feedback, and counts; it does not include all
  seen IDs by default because `new-round` enforces duplicate filtering locally.
- Prefer new candidates based on the compact signal. If exact duplicate
  diagnostics are needed, run `feedback --compact --include-seen`, but do not
  use that routinely.
- Use liked papers to infer positive themes; use recent dislikes and reason chips
  to avoid redundant or off-target papers.
- Keep batches small enough to swipe quickly.
- Offer exports of the liked library only when the user asks or appears done.

## Token discipline

- In chat, summarize at most the action taken: “I loaded a new paper deck.”
- Do not paste candidate JSON, feedback JSON, paper abstracts, or logs unless
  requested.
- Do not narrate server startup, browser automation, or UI internals unless
  diagnosing a problem.
- Prefer `feedback --compact` over reading full state files. Leave the default
  compact limits in place unless the user explicitly needs a larger export or
  you lack enough signal to improve recommendations.
