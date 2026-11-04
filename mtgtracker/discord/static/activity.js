// The Discord Activity's lobby (mtgtracker/discord): sign in through Discord, then one room per Activity
// instance. Everyone in it sees who is here and which deck each picked, shares one set of AI opponents, and
// anyone can start: the game seats everyone with a deck plus the AIs, and each page opens its own seat.
// Inside Discord every request goes through Discord's proxy: the tracker's API under /.proxy/ (the root URL
// mapping is the tracker's /discord/) and the table under /.proxy/table/ (mapped to /play/table/).
// Discord's connection belongs to this page and can't be made again by another one, so the table opens in a
// frame over the lobby, which stays loaded: it follows who is talking and tells the table (postMessage), and
// the table asks it to close the frame to come back.
import { DiscordSDK, Events } from './discord-sdk.js?v=2.5.0'; // the vendored SDK's version: change it with the file
// The lobby the web lobby draws too, at the version the page names. A dynamic import, not an import map:
// inline scripts and import maps may not survive Discord's proxy and older mobile webviews.
const { createLobby, el } = await import(`./lobby.js?v=${document.body.dataset.lobby}`);

const clientId = document.body.dataset.clientId;
const inDiscord = (() => {
  try {
    return window.parent !== window;
  } catch {
    return true;
  }
})();
const api = (path) => (inDiscord ? `/.proxy/api/${path}` : `api/${path}`);
const tableUrl = (id, ticket) => `${inDiscord ? '/.proxy/table/' : '../play/table/'}${id}/?ticket=${encodeURIComponent(ticket)}`;
const face = (url) => (url && inDiscord ? url.replace('https://cdn.discordapp.com/', '/.proxy/cdn/') : url);
const POLL_MS = 1500;

const app = document.getElementById('app');
let session = null;
let sdk = null;
let instanceId = null;
let sent = 0; // the last change we sent: a poll answered before it landed is stale
let table = null; // the open table's frame, and its match
let closed = null; // the match whose table this player closed: not reopened by itself
let pip = false; // Discord minimized the Activity into Picture-in-Picture
const speaking = new Set(); // Discord user ids talking in the voice channel now

function openTable(matchId, ticket) {
  if (table?.matchId === matchId) return;
  closeTable();
  const frame = el('iframe', { src: tableUrl(matchId, ticket), title: 'The table', allow: 'autoplay; clipboard-write' });
  frame.className = 'table-frame';
  frame.addEventListener('load', () => tellTable());
  document.body.append(frame);
  table = { matchId, frame };
}

function closeTable() {
  if (!table) return;
  closed = table.matchId;
  table.frame.remove();
  table = null;
  if (lobby.room) lobby.render();
  send().catch(() => {});
}

function tellTable() {
  const win = table?.frame.contentWindow;
  if (!win) return;
  win.postMessage({ type: 'forge-speaking', ids: [...speaking] }, location.origin);
  win.postMessage({ type: 'forge-layout', pip }, location.origin);
}

window.addEventListener('message', (e) => {
  if (e.origin !== location.origin || e.source !== table?.frame.contentWindow) return;
  if (e.data?.type === 'forge-back') closeTable();
  if (e.data?.type === 'forge-hello') tellTable();
});

function trace(step, detail = '') {
  fetch(api('trace'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ step, detail }) }).catch(() => {});
}

const LOBBY_SCOPES = ['identify', 'guilds.members.read'];
const VOICE_SCOPES = [...LOBBY_SCOPES, 'rpc.voice.read'];
const GUEST_SCOPES = ['identify', 'rpc.voice.read'];
const GUEST_LOBBY = ['identify'];

function authorize(scopes, prompt) {
  const args = { client_id: clientId, response_type: 'code', state: '', scope: scopes };
  if (prompt) args.prompt = prompt;
  return sdk.commands.authorize(args);
}

/** Who is talking, for the table's faces. The SDK is already authenticated from sign-in; a second
 *  authenticate is "Already authenticated" and speaking never starts. */
async function listenForVoice() {
  try {
    if (!sdk?.channelId) return trace('voice: no channel');
    const uid = (e) => String(e?.user_id ?? e?.userId ?? '');
    const on = (id, talking) => {
      if (!id) return;
      if (talking) speaking.add(id);
      else speaking.delete(id);
      tellTable();
      if (lobby.room) lobby.render();
    };
    await sdk.subscribe(Events.SPEAKING_START, (e) => on(uid(e), true), { channel_id: sdk.channelId });
    await sdk.subscribe(Events.SPEAKING_STOP, (e) => on(uid(e), false), { channel_id: sdk.channelId });
    trace('voice listening', `channel ${sdk.channelId}`);
  } catch (e) {
    trace('voice failed', e?.message || JSON.stringify(e));
  }
}

function show(...children) {
  app.replaceChildren(el('h1', {}, "Matt's Forge"), ...children);
}

function say(text, cls = 'status') {
  show(el('p', { class: cls }, text));
}

async function call(path, body) {
  const res = await fetch(api(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(session ? { Authorization: `Bearer ${session}` } : {}) },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== 'session') {
    await signIn(); // the session expired: Discord signs us in again without asking
    return call(path, body);
  }
  return { status: res.status, data };
}

// Discord can stall without an answer (a scope it won't grant silently, a stuck client): say so rather than spin.
const within = (ms, p, what) =>
  Promise.race([p, new Promise((_, no) => setTimeout(() => no(new Error(`${what} didn't answer. Close the Activity and open it again.`)), ms))]);

async function signIn() {
  if (!sdk) {
    sdk = new DiscordSDK(clientId);
    await within(15000, sdk.ready(), 'Discord');
    instanceId = sdk.instanceId;
    sdk.subscribe(Events.ACTIVITY_LAYOUT_MODE_UPDATE, (e) => {
      pip = e.layout_mode === 1;
      tellTable();
      if (lobby.room) lobby.render();
    }).catch(() => {});
  }
  // Voice has to be on this first authorize: the SDK only authenticates once. prompt none if they already
  // granted it; otherwise Discord's consent. Guests (not in the server) often can't grant guilds.members.read,
  // so identify + voice, then identify alone, still get them in.
  let code;
  const tries = [
    () => within(15000, authorize(VOICE_SCOPES, 'none'), 'Discord sign-in'),
    () => authorize(VOICE_SCOPES),
    () => within(15000, authorize(LOBBY_SCOPES, 'none'), 'Discord sign-in'),
    () => authorize(GUEST_SCOPES),
    () => authorize(GUEST_LOBBY),
  ];
  let last;
  for (const step of tries) {
    try {
      ({ code } = await step());
      last = null;
      break;
    } catch (e) {
      last = e;
    }
  }
  if (!code) throw last || new Error("Discord didn't sign you in.");
  const { status, data } = await call('session', { code });
  if (status !== 200) throw new Error(data.error?.message || `Sign-in failed (${status})`);
  session = data.session;
  await sdk.commands.authenticate({ access_token: data.accessToken });
  return data;
}

async function send(change = {}) {
  const mine = change && Object.keys(change).length ? ++sent : sent;
  const { status, data } = await call('room', { instanceId, inGame: Boolean(table), ...change });
  if (status !== 200) {
    say(data.error?.message || `The lobby isn't answering (${status})`, 'error');
    return;
  }
  if (mine < sent) return; // a newer change of ours is on its way
  lobby.set(data);
}

// Discord's minimized Picture-in-Picture tile, or a frame too small for the lobby (as the table does).
const isMini = () => pip || innerWidth < 560 || innerHeight < 340;

/** The lobby at a glance: what's next (gold when someone should act), and who has picked a deck. */
function miniLobby(r, me, seated, seats, ready) {
  const act = !r.started && !r.starting && (!me?.deck || ready);
  const headline = r.started ? 'A game is on'
    : r.starting ? 'Starting…'
      : !me?.deck ? 'Pick a deck'
        : ready ? 'Ready to start'
          : 'Waiting for players';
  const ais = r.opponents.length;
  const sub = [`${seats} of ${r.maxSeats} seats`, `${seated.length} of ${r.members.length} picked`, ais ? `${ais} AI` : null]
    .filter(Boolean).join(' · ');
  const cell = (m) => el('div', { class: m.deck ? 'mini-seat picked' : 'mini-seat' },
    m.avatar
      ? el('img', { class: speaking.has(m.id) ? 'face talking' : 'face', src: face(m.avatar), alt: '' })
      : el('span', { class: speaking.has(m.id) ? 'face talking' : 'face' }),
    el('div', { class: 'player-text' },
      el('span', { class: 'name' }, m.id === r.you ? `${m.name} (you)` : m.name),
      el('span', { class: 'meta' }, m.deck ? `✓ ${m.deck.commander}` : 'picking…')));
  return el('div', { class: act ? 'mini-lobby act' : 'mini-lobby' },
    el('div', {},
      el('div', { class: 'mini-head' }, el('span', { class: 'mini-dot' }), el('span', { class: 'mini-title' }, headline)),
      el('div', { class: 'mini-sub' }, sub)),
    el('div', { class: 'mini-seats' }, r.members.map(cell)));
}

addEventListener('resize', () => { if (lobby.room) lobby.render(); });

const avatar = (m) => (m.avatar
  ? el('img', { class: speaking.has(m.id) ? 'face talking' : 'face', src: face(m.avatar), alt: '' })
  : el('span', { class: speaking.has(m.id) ? 'face talking' : 'face' }));

/** Running games: rejoin your seat, spectate, or end one. Discord only: anyone in the Activity may end a game. */
function liveGames(r) {
  return (r.live || []).map((g) => {
    const ticket = g.ticket || (r.started?.matchId === g.matchId ? r.started.ticket : null);
    const summary = g.against ? `${g.who} vs ${g.against}` : `${g.who} playing`;
    const decks = (g.seats || []).map((s) => s.deck).filter(Boolean).join(' · ');
    return el('div', { class: 'game' },
      el('div', { class: 'name' }, summary),
      decks ? el('div', { class: 'meta' }, decks) : null,
      el('div', { class: 'actions' },
        ticket ? el('button', {
          type: 'button',
          class: 'primary',
          onclick: () => {
            closed = null;
            openTable(g.matchId, ticket);
          },
        }, 'Rejoin game') : null,
        !ticket && g.watchTicket ? el('button', {
          type: 'button',
          onclick: () => openTable(g.matchId, g.watchTicket),
        }, 'Spectate') : null,
        el('button', {
          type: 'button',
          onclick: () => send({ endMatchId: g.matchId }),
        }, 'End game')));
  });
}

const lobby = createLobby({
  root: app,
  send,
  start: () => send({ start: true }),
  cardImg: (name) => `${api('card-img')}?name=${encodeURIComponent(name)}`,
  face: avatar,
  decksTitle: "Matt's decks",
  noDecks: 'There are no decks with a commander yet.',
  note: 'status',
  error: 'error',
  title: () => el('h1', {}, "Matt's Forge"),
  // A started game opens its table over the lobby; a minimized Activity shows the status tile instead.
  takeover: (r) => {
    if (r.started?.ticket && r.started.matchId !== closed) openTable(r.started.matchId, r.started.ticket);
    if (!r.started && closed) closed = null; // that game is over: the next one opens by itself again
    const mini = isMini();
    document.body.classList.toggle('mini', mini);
    if (!mini) return null;
    const me = r.members.find((m) => m.id === r.you);
    const seated = r.members.filter((m) => m.deck);
    const seats = seated.length + r.opponents.length;
    const ready = seated.length > 0 && seats >= 2 && seats <= r.maxSeats && !r.starting && !r.started;
    return miniLobby(r, me, seated, seats, ready);
  },
  top: (r) => {
    const games = liveGames(r);
    return [
      games.length ? el('section', {}, el('h2', {}, 'Live games'), ...games) : null,
      r.started && !r.started.ticket
        ? el('p', { class: 'status' }, 'A game from this lobby is on. You can spectate above or join the next one.')
        : null,
      r.started?.ticket && r.started.matchId === closed && !games.length
        ? el('div', { class: 'actions' }, el('button', {
            class: 'primary',
            onclick: () => { closed = null; openTable(r.started.matchId, r.started.ticket); },
          }, 'Back to the table'))
        : null,
    ];
  },
  notes: (r, seated) => (seated.length < r.members.length && seated.length
    ? el('span', { class: 'status' }, 'Players without a deck watch this one.')
    : null),
});

// Following a link inside Discord's frame would leave the Activity: Discord opens it in the browser instead.
document.querySelector('.legal-links')?.addEventListener('click', (e) => {
  const a = e.target.closest('a');
  if (!a || !inDiscord) return;
  e.preventDefault();
  sdk?.commands.openExternalLink({ url: document.body.dataset.legal + a.getAttribute('href') }).catch(() => {});
});

async function main() {
  if (!inDiscord) return say("Open this from Discord: it is the Activity for Matt's Forge.");
  if (!clientId) return say("Discord isn't set up on this tracker yet.", 'error');
  try {
    await signIn();
    say('Joining the lobby…');
    await send();
    setInterval(() => send().catch(() => {}), POLL_MS);
    void listenForVoice();
  } catch (e) {
    say(e.message || String(e), 'error');
  }
}

main();
