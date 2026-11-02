"""Play: goldfish and play your decks on the Forge game server.

A distinct module. The tracker connects people (who plays, with which deck, against whom) and keeps the
games; the game server (forge-gui-web) only runs a match it is handed. This module touches the rest of
the tracker only through users and deck exports:

- `forge`: the game server's control API (start a match from a spec, read it, end it).
- `tables`: where games run: a game server pod per game (with a warm spare) or one shared server.
- `games`: the play_games history, completed by the game server's results.
- `lobbies`: in-memory /play/lobby/<code> rooms where signed-in friends sit down together.
- `routes`: /play (goldfish), /play/lobby, /play/start, /play/end, and /play/results for the game server.
"""
