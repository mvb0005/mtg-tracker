"""Play: goldfish and play your decks on the Forge game server.

A distinct module. The tracker connects people (who plays, with which deck, against whom) and keeps the
games; the game server (forge-gui-web) only runs a match it is handed. This module touches the rest of
the tracker only through users and deck exports:

- `forge`: the game server's control API (start a match from a spec, read it, end it).
- `games`: the play_games history, completed by the game server's results.
- `routes`: /play (the play page), /play/start, /play/end, and /play/results for the game server.
"""
