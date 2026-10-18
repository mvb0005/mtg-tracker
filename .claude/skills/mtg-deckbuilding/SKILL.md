---
name: mtg-deckbuilding
description: Build, tune, or shop for a Magic: The Gathering Commander deck in mtgtracker -- designing a list around a commander, proposing swaps, checking a list against deckbuilding fundamentals (ratios, mana, curve, defense), pricing and budgeting, comparing a store cart, and applying changes to a deck on the site as a new version. Use whenever the user asks to make, improve, cut, upgrade, price, or buy for a deck.
---

# Commander deckbuilding in mtgtracker

Work from the deck's own context, keep to the user's rules, verify with data
instead of memory, and change the live deck only through the app's JSON API,
dry run first.

## 0. Load the context first

1. **Read the deck's notes.** The notes hold the plan, the user's rules and past decisions. Get them with the API: `GET /api/v1/decks/<id>/notes` (owner only), through `scripts/_api.py` (`_api.get(f"/decks/{id}/notes")`), or `.venv/bin/mtg deck-notes <id>` (local DB).
   - Typical rules: a budget cap per card, "don't borrow from friends", "keep my precons intact", play-style likes and dislikes.
   - Honor them without re-asking. If notes are empty, ask about budget, play style and borrowing once, then write the answers down (step 6).
2. **Get the current list.** `GET /api/v1/decks/<id>` has every card with its `lot_id`, printing, where it sits, the wanted cards and the Modify queue. The text lists are `GET /api/v1/decks/<id>/export/decklist` (also `shopping` and `borrow-<friend id>`).
3. **For ownership questions, use `scripts/who_owns.py`.** It asks the API who owns each card and where, so there's no snapshot to take. Never read the live DB file directly.

The scripts talk to the site with the credentials in the environment or the repo's gitignored `.env` (see `scripts/_api.py`: `MTG_BASE_URL`, `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`, `MTG_ACT_FOR`). Never put those on a command line or print them.

## 1. Start from the commander

- **Read the exact oracle text** of both faces from Scryfall, not memory. Fetch by set and number for double-faced cards (`scryfall.fetch_collection([{"set":..,"collector_number":..}])`).
- **Name the engine in one sentence.** For example: "make opponents draw, then cast discounted X spells at instant speed."
- **List the rules gotchas.** Typical ones:
  - A double-faced commander that leaves the battlefield comes back front face up, so blink and bounce reset a transform.
  - Commander tax goes up by {2} for each recast from the command zone.
  - Cost reducers only reduce generic mana, which includes X.
  - "Destroy all other creatures" hits your own commander.
- **Color identity covers both faces** and every mana symbol in the rules text.

## 2. Fundamentals checklist (a 99-card list plus the commander)

Use these as guardrails, not as laws. A strong theme can bend them, so say so when it does.

| Slot | Typical | Notes |
|---|---|---|
| Lands | 35–38 | 33–34 with a low curve and 10+ ramp. Count colorless utility lands against your colored needs. |
| Ramp | 10+ | Prefer 2-mana rocks and land ramp. Count catch-up ramp only if opponents usually lead on lands. |
| Card draw / advantage | 10+ | Engines over one-shots. |
| Targeted interaction | 8–10 | Removal plus counterspells, with instant speed preferred. Include some artifact/enchantment answers. |
| Board wipes | 2–4 | Essential if the deck helps opponents (group hug) or plays defensively. |
| Commander protection | 2–4 | Hexproof, indestructible, phasing, or blink if a zone change is fine. |
| Win conditions | 3–5 named | Say how each one actually ends the game. |
| Graveyard hate | 1–3 | Especially if opponents discard, wheel or reanimate. |
| Defense | — | Blockers that can stop fliers, pillowfort, fog, or wipes. Check fliers specifically. |

- **Curve:** average nonland mana value around 3 or less, and most of the deck at 4 or less.
- **Redundancy:** the deck's critical effect, the one that makes the engine run, needs 3 or more sources.
- **Colors:** count colored pips per color and set the basics ratio to match. Each fetch or tutor effect, like Plains fetchers, raises the share of that basic. Watch for tapped lands in a deck that wants to act at instant speed.
- **Hand size:** decks that draw a lot need a way to keep extra cards, like Reliquary Tower, Thought Vessel or Spellbook.
- **Synergy without a pile:** cut cards that only work with one other card unless that pair is the point of the deck.
- **Table fit:** match the user's taste. Avoid stax and locks if they want a fun or casual deck, and say when a card is likely to feel unfun. Wizards' Commander brackets and its "Game Changers" list are a useful shared vocabulary for power level.

Run `scripts/check_list.py` on any candidate list. It checks the count, singleton rule, color identity and legality, and gives an approximate count for each slot. Then reason about the gaps it reports. Its categories are heuristic and read from rules text, so confirm a borderline card by hand.

## 3. Finding cards

- **Search with Scryfall syntax** against the API, cache first:
  - `id<=wu legal:commander game:paper o:"opponent's turn"`
  - `order=edhrec` brings well-known cards to the top.
- **Be polite to Scryfall.** Send `scryfall.USER_AGENT`, wait 0.3s or more between requests, and back off on HTTP 429. Batch with `scryfall.fetch_collection`, which answers known cards from the cache and only fetches the missing ones.
  - If you get a run of 429s or "Couldn't reach Scryfall", stop and wait a minute or more. A burst of searches can get you blocked for a while.
- **Check ownership before recommending a purchase.** Run `scripts/who_owns.py <names...>` to see the user's copies and friends' copies, and which deck holds each.
  - A friend's loose copies can be borrowed, unless the notes say otherwise.
  - A copy in another of the user's decks would have to *move*. Say so.

## 4. Budget and shopping

- **Price with `scripts/prices.py`.** It reports the cheapest paper USD printing and flags anything over the budget cap from the notes.
  - Store prices often run 50–100% above market. When you compare a cart, show store price against market price.
- **Apply the user's exceptions explicitly**, for example "over $5 only if it's about the deck's theme". Name each over-cap card and the reason it qualifies.
- **For a store cart:** diff it against the intended list. Report missing cards, extra cards, cards the user already owns elsewhere, out-of-stock lines and markups. The total must reach exactly 100 including basics and borrowed cards.
- **Cheaper routes:** TCGplayer Mass Entry plus cart Optimize is cheapest, Card Kingdom is a single seller, or split the order and buy the big-markup cards online. Ask the local store for a whole-deck discount. Proxy-test cards before buying expensive ones.
- **After the user buys:** record it so the cards become owned lots with their cost: `POST /api/v1/purchases` with `{vendor, date, tax_cents, shipping_cents, total_cents, cart}` (the cart is the store CSV, the checkout text or a `2 Name, $1.25` list). Run it with `?dry_run=1` first and read the reconciliation, any `cards_not_found` names and which lines `needs_printing_because`; then post it for real. It only adds cards to the collection and never changes a deck (`wanted_by` says which decks want them); putting them in a deck is a separate step. An undo is `DELETE /api/v1/purchases/<id>` and needs a confirm.
- **After recording a purchase, assemble the deck.** `GET /api/v1/decks/<id>/assembly` is the plan: `fill` (a wanted card you now own a free copy of), `swap`, `equip`, and `pull` (takes a card out of another deck), plus `borrow`/`acquire`. Run `POST /api/v1/decks/<id>/assembly?dry_run=1` with `{"line_ids": [...]}` for the fills, swaps and equips (the lines with `default_selected`), show the user the counts and the pull list, then post it for real. It records one version only if cards were filled or swapped. **Pulls need the user's OK:** a pull empties a slot in another deck, so show which deck it comes from, dry-run `POST .../assembly/pulls`, and only after they agree repeat with `?confirm=TOKEN` (the same body). If the plan has changed (409 `stale_plan`), fetch it again and show it again. `mtg reassemble-deck <id>` prints the same plan.
- **Shopping list format:** `N Name` per line, front face names only. That's the same as the app's `shopping.txt`.

## 5. Proposing and applying changes

1. **Propose swaps as a table:** Add | price | Replaces | why. Each change should fix a named gap or improve the engine. Keep the count at exactly 100.
2. **When the user agrees, change the live deck through the API.** Never write the database directly. Use `scripts/apply_list.py DECK_ID --target LIST`:
   - It diffs the deck against the target list (or takes explicit `--pairs`), then posts one request to `POST /api/v1/decks/<id>/changes` with `expect_version`. Each add is queued as a planned ("to acquire") change, paired with a cut, and everything is applied as **one version** with a note, all or nothing.
   - **It dry-runs first and prints the plan and the diff** (`?dry_run=1` runs the real code and rolls back, leaving no trace). Run it with `--plan` to stop there, show the swaps to the user, and apply only after they agree.
   - The server refuses with `version_conflict` if someone changed the deck since it was read, and with `pending_changes` if the Modify page has a half-built queue. Re-read and tell the user; never clear their queue.
   - Every real change also gets an automatic restore point and a write-log entry (attributed to you in the audit log), so there is no snapshot-and-local-server rehearsal for deck edits. Keep `pull-db` for deploys and migrations.
3. **Watch what applying does.** Adding a card the user owns that sits in another deck *moves* it into this deck, and the other deck shows it as away.
   - `apply_list.py` always adds cards by name as planned (to acquire), so nothing moves. To move an owned copy, add it by `lot_id` through `--pairs` (`{"adds": [{"lot_id": 12}], "cuts": ["Island"]}`) and only when the user wants that card moved.
   - Cutting removes only this deck's entry, so it never touches the other deck.
4. **Verify after applying:** `apply_list.py` re-reads the deck and prints `deck now matches the target: True`. Also check that the version went up by one and other decks' counts are unchanged (`GET /api/v1/decks`).
5. **Risky actions need the user's OK.** A call the API marks risky (deleting a collection import: `DELETE /api/v1/imports/<id>`) answers `409 confirm_required` with a **preview and a confirm token**. Show the preview to the user, and only after they agree repeat the call with `confirm=<token>`. The client raises `ConfirmRequired` and never confirms by itself. `?dry_run=1` shows the preview without a token.
6. **Never deploy code** as part of deckbuilding. Deploying is a separate step that needs the user's explicit OK.

## 6. Write it down

After any decision, update the deck notes in the form on the deck page, or `PUT /api/v1/decks/<id>/notes` with `{"notes": ...}`, keeping what's already there. Record:
- the plan, in one or two lines
- the user's rules and their exceptions
- key rules interactions
- cuts the user rejected, or cards they want kept, so you don't suggest them again
- shopping status: what's bought and what's still needed

Keep the notes short and current. Edit stale lines rather than appending forever.

## Scripts (run from the repo root with `.venv/bin/python`)

- `scripts/check_list.py LIST [--commander NAME]`: count, singleton rule, color identity, legality, slot counts, curve and pips. LIST is decklist text: `Commander` and `Deck` sections, `N Name` lines.
- `scripts/prices.py [--cap 5] NAME... | -`: cheapest paper USD per card, the total, and anything over the cap.
- `scripts/who_owns.py NAME... | -`: every owner's copies of each card, and where they are (via `/api/v1/cards/owners`).
- `scripts/apply_list.py DECK_ID (--target LIST | --pairs JSON) [--plan] [--note TEXT]`: diff, dry run (printed), then apply as one version with `expect_version`, and verify.
- `scripts/_api.py`: the small API client the scripts share (config from the environment or `.env`; `get`/`post`/`put`/`delete`; raises `ApiError`, or `ConfirmRequired` carrying the preview and token). Import it for any other API call.
