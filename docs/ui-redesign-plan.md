# Chat Audit Core UI Redesign Plan

Branch: `codex/ui-redesign-chat-console-20260721`
Scope: desktop-first redesign of the existing chat audit console, beginning with `/`.

## Contract freeze (P0)

- Preserve all existing API paths, request methods, response fields, authentication headers, CSRF behavior, and media URLs.
- Preserve hash context: `#robot=<robot_id>` and `#room=<room_id>`.
- Preserve current user flows: account switching, room selection, search, media filters, message detail, reply/forward preview, selection export, import/export, offline audit, settings, backup, adapters, tokens, users, sessions, theme, and logs.
- Treat `app/static/assets/app.js` and `app/static/assets/app.css` as source assets; regenerate `app.min.js` and `app.min.css` through `scripts/minify_static_assets.py`.

## P0 implementation boundary

- Only the desktop chat audit workspace is implemented first.
- Backend files remain unchanged: `app/api.py`, `app/schemas.py`, `app/services/`, `app/main.py`.
- Existing component IDs and JS event bindings remain compatible unless a thin mapping is added.
- Keep the working tree's unrelated QQNT/backend changes intact.

## P1 desktop workspace

Files:

- `app/static/index.html`
- `app/static/assets/app.css`
- `app/static/assets/app.js`

Components:

- Account rail and account status
- Workspace context header
- Room list and room item states
- Search/filter toolbar
- Dashboard summary strip
- Chat timeline and message bubbles
- Selection/export toolbar
- Message detail, image preview, and forward preview surfaces

Goals:

- Establish a quiet evidence-console visual language rather than a marketing layout.
- Reduce nested cards, gradients, and wide shadows.
- Prioritize account → room → message context.
- Keep desktop density high enough for audit work while improving scanability.
- Preserve existing actions and interaction behavior.

## P1 status and accessibility pass

- Make loading, empty, error, success, offline, and stale-refresh states visible in context.
- Add consistent focus-visible treatment and keyboard-safe overlays.
- Keep all icon-only controls labeled.
- Preserve reduced-motion behavior without decorative transitions.

## P2 system consolidation

Files:

- `app/static/assets/app.css`
- `scripts/minify_static_assets.py`
- `tests/test_web_console.py`

Goals:

- Consolidate color, spacing, radius, control-height, border, focus, and state tokens.
- Standardize button, input, status, card, modal, and report components.
- Add responsive rules for narrow desktop windows and tablet layouts.
- Reduce full-list re-rendering and improve live-refresh feedback in a later batch.
- Update resource and UI contract tests after the visual batch stabilizes.

## Validation gates

- After each UI batch: `node --check app/static/assets/app.js` and regenerate minified assets.
- Run `git diff --check` and the local Impeccable detector on the changed UI files.
- Before final handoff: run the targeted Web Console test and verify desktop widths, overflow, loading/empty/error states, keyboard focus, dark theme, and reduced motion.