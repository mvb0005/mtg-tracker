"""Who owns each card, and where those copies are (from a DB snapshot --
take one with deploy/minikube.sh pull-db; never read the live file).

    .venv/bin/python .claude/skills/mtg-deckbuilding/scripts/who_owns.py SNAPSHOT.db NAME... | -
"""
import collections
import sqlite3
import sys

from _common import key, read_names

conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
wanted = {key(n): n for _, n in read_names(sys.argv[2:])}
found = collections.defaultdict(list)
for owner, name, deck in conn.execute(
    """SELECT users.display_name, lots.card_name, decks.name FROM lots
       JOIN users ON users.id = lots.owner_id
       LEFT JOIN dispositions ON dispositions.lot_id = lots.id
       LEFT JOIN deck_cards ON deck_cards.lot_id = lots.id AND deck_cards.equipped = 1
       LEFT JOIN decks ON decks.id = deck_cards.deck_id
       WHERE dispositions.id IS NULL"""
):
    k = key(name)
    if k in wanted:
        found[k].append((owner, deck or "not in a deck"))
for k, n in wanted.items():
    copies = collections.Counter(found.get(k, []))
    where = "; ".join(f"{owner}: {q} in {deck}" for (owner, deck), q in sorted(copies.items())) or "nobody"
    print(f"{n}: {where}")
