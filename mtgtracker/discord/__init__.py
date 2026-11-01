"""Discord: play from a Discord Activity (forge-gui-web docs/DISCORD.md).

A distinct module, beside `play`. Discord's proxy loads the Activity from /discord/ on this hostname, which
Cloudflare Access lets through (only /discord and /play/table are); every route here signs people in through
Discord itself, so the sign-in middleware skips exactly these paths. Players are known by their Discord id,
with no tracker account. It touches the rest of the tracker only through the owner's decks and the play
module (starting a game):

- `auth`: the code exchange, guild membership and session tokens.
- `rooms`: one lobby per Activity instance: who is here, their decks, the shared AI opponents.
- `routes`: /discord/ (the Activity page) and its API.
"""
