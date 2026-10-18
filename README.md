# mtgtracker

Personal Magic: The Gathering collection and spending tracker. Local-first,
single-user SQLite database — the source of truth for money and ownership,
not a deckbuilder (that's Moxfield/Archidekt) or a scanner (that's ManaBox).

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Usage

```
mtg import-order path/to/Order.order   # imports the purchase, seeds known precon decks
mtg list-decks
mtg reassemble-deck <deck-id>          # what to pull back to reassemble a deck's list
mtg serve                              # web UI at http://127.0.0.1:8000
```

## Running on minikube

```
deploy/minikube.sh start               # bring the cluster back after a reboot (Docker must be running); no redeploy
deploy/minikube.sh up                  # build image into minikube, apply deploy/k8s, wait
deploy/minikube.sh load-db             # snapshot data/mtgtracker.db (+ receipts/) into the pod's volume
deploy/minikube.sh pull-db             # copy the cluster DB back to data/mtgtracker.db (old one backed up)
deploy/minikube.sh pull-restore NAME  # copy a restore point out of the pod (to data/restore/)
deploy/minikube.sh restore FILE        # put a restore point / recovered DB in place (live DB pulled to a backup first)
deploy/minikube.sh forward             # UI at http://127.0.0.1:8000
deploy/minikube.sh import Order.order  # or import straight into the cluster DB
deploy/minikube.sh down                # remove the app, keep the data volume
deploy/minikube.sh destroy             # remove everything, data included
```

Everything lives in the `mtgtracker` namespace. The Service is ClusterIP-only
and reached through port-forward, so nothing is exposed beyond this machine
unless you turn on the Cloudflare Tunnel below. Your database and receipts are
never baked into the image.

### Exposing it with Cloudflare Tunnel (optional)

**This makes the site public with no login.** Anyone with the URL can see your
purchases (totals, order numbers, payment method) and change printings and
commanders. To lock it down later, add a Cloudflare Access policy for the
hostname in the Zero Trust dashboard; nothing in the app needs to change.

1. In the Cloudflare dashboard, go to **Zero Trust → Networks → Tunnels**,
   create a tunnel (connector type *Cloudflared*), and copy its token.
2. Add a **public hostname** to that tunnel, e.g. `mtg.example.com`, with
   service type **HTTP** and URL `mtgtracker.mtgtracker.svc.cluster.local:80`.
3. Copy `.env.example` to `.env` (gitignored, and excluded from the image),
   set `CLOUDFLARE_TUNNEL_TOKEN=` to the token, then run
   `deploy/minikube.sh tunnel`. The token can also be piped in, or typed at a
   hidden prompt when run in a terminal. In the cluster it's stored only as a
   Kubernetes Secret.

`deploy/minikube.sh tunnel-down` stops the connector and deletes the token.
The tunnel only runs while minikube does.

The cluster keeps its own copy of the database on a persistent volume,
separate from the local `data/mtgtracker.db`. Printings you pick in the UI and
orders added with `import` land in the cluster copy. Run `pull-db` before
`load-db`, because `load-db` replaces the cluster copy wholesale.

## Safety net

Every change to every table is appended to a write log (`changelog`), and the
web app takes a gzipped snapshot ("restore point") into `restore/` beside the
database before a write when the newest one is over 10 minutes old, and before
every risky action. It keeps the 50 newest, plus the newest of every day, forever.

```
mtg restore-points                                   # list them
mtg recover POINT.db.gz --out recovered.db [--until-time ISO | --until-seq N | --until-audit ID]
mtg undo-batch AUDIT_ID [--yes]                      # reverse one web request (dry run without --yes)
```
`recover` rebuilds the database as of a moment from a point plus the log and
never touches the live one; put the result in place with `deploy/minikube.sh restore`.

## Modifying decks

Each deck page links to **Modify deck** (`/decks/<id>/modify`):
- **Queue inserts:** search your collection for cards to insert, or plan a card you don't own yet ("Plan to buy", looked up on Scryfall).
- **Pick replacements:** choose which card on the list each insert replaces, if any.
- **Apply:** the ready changes are applied together as the deck's next version. Replaced cards go back to your collection. Planned cards stay queued until you own a copy.
- **History:** every version is kept, with what it added and removed, and you can view any past version's list.
- **Import a list:** paste a plain-text decklist exported from Moxfield, Archidekt or Arena (`1 Sol Ring`, `1x Sol Ring`, `1 Sol Ring (CMM) 400 *F*`, with section headers) to queue every card at once. Names are matched on Scryfall in batches. Cards already in the deck or already queued are skipped. Cards you own are used first, preferring unassigned copies, and the rest are queued to buy. Sideboard and maybeboard sections are ignored.

## Tests and linting

```
.venv/bin/pytest                                           # all tests (network-free)
.venv/bin/pytest --cov=mtgtracker --cov-branch --cov-report=term-missing   # with coverage
.venv/bin/ruff check .                                     # lint (config in pyproject.toml)
.venv/bin/ruff check --fix .                               # apply safe autofixes
```

`ruff format` is deliberately not adopted: it would reflow the packed SQL
parameter tuples one value per line, for no functional gain.

## Data model, in short

- `transactions` → `products` (sealed line items) → `lots` (one row per
  physical card — no collapsed quantities, so a card can't be equipped to
  two decks at once).
- `deck_cards` is a many-to-many junction: a `(deck_id, lot_id)` row means
  "this card is on this deck's list." An `equipped` flag (enforced unique
  per lot by a partial index) marks which one deck physically holds it
  right now. Borrowing a card into another deck just moves `equipped`;
  cutting it from a deck's list deletes the row outright. `reassemble`
  reports every card on a deck's list that isn't currently equipped there.
- Cost basis is allocated evenly by card count across a product's lots
  (largest-remainder rounding, so cents always reconcile), not by market
  value — there's no live pricing in v1.

## What's not built yet

Live/nightly price snapshots, ManaBox CSV import, Moxfield/Archidekt
export, `deck_costs` entries (sleeves/playmat), and write actions in the web
UI (borrow/cut, resolving ambiguous printings) are all on the roadmap but
out of this round's scope — see the plan doc.

## Known gaps from the first real import

Importing the two Foundations Commander precons (`mtg import-order`)
surfaces a few cards Scryfall can't resolve to a single printing without
the physical box in hand:

- **Sol Ring** and **Arcane Signet** each have two `fdc`-set printings, and
  nothing in the published decklist says which one shipped in this box —
  each got a lot with `scryfall_id` left `NULL` rather than guessed.
- Basic lands (32 Mountain, 34 Island) are never resolved to a specific art
  — there's no way to know which printing shipped in the box.

Settle these from the physical cards on the web UI's **Printings** page
(`/printings`). It lists every unresolved card and offers the candidate
printings from locally cached Scryfall sets, with images. For basics, look
up the set code printed on the card (bottom-left) to fetch that set once,
then split the copies across arts.

None of this blocks cost allocation (it's by count, not by card), only
future Scryfall-id-dependent features.
