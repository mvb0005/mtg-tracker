// The Discord Activity's lobby (mtgtracker/discord): sign in through Discord, then one room per Activity
// instance. Everyone in it sees who is here and which deck each picked, shares one set of AI opponents, and
// anyone can start: the game seats everyone with a deck plus the AIs, and each page opens its own seat.
// Inside Discord every request goes through Discord's proxy: the tracker's API under /.proxy/ (the root URL
// mapping is the tracker's /discord/) and the table under /.proxy/table/ (mapped to /play/table/).
// Discord's connection belongs to this page and can't be made again by another one, so the table opens in a
// frame over the lobby, which stays loaded: it follows who is talking and tells the table (postMessage), and
// the table asks it to close the frame to come back.
import { DiscordSDK, Events } from './discord-sdk.js?v=2.5.0'; // the vendored SDK's version: change it with the file

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
let room = null;
let sent = 0; // the last change we sent: a poll answered before it landed is stale
let table = null; // the open table's frame, and its match
let closed = null; // the match whose table this player closed: not reopened by itself
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
  if (room) render();
}

function tellTable() {
  table?.frame.contentWindow?.postMessage({ type: 'forge-speaking', ids: [...speaking] }, location.origin);
}

window.addEventListener('message', (e) => {
  if (e.origin !== location.origin || e.source !== table?.frame.contentWindow) return;
  if (e.data?.type === 'forge-back') closeTable();
  if (e.data?.type === 'forge-hello') tellTable();
});

function trace(step, detail = '') {
  fetch(api('trace'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ step, detail }) }).catch(() => {});
}

/** Who is talking, for the table's faces: needs the voice scope, which Discord asks the player for once. */
async function listenForVoice() {
  try {
    const { code } = await sdk.commands.authorize({
      client_id: clientId,
      response_type: 'code',
      state: '',
      scope: ['identify', 'guilds.members.read', 'rpc.voice.read'],
    });
    const { status, data } = await call('session', { code });
    if (status !== 200) return trace('voice session refused', String(status));
    session = data.session;
    await sdk.commands.authenticate({ access_token: data.accessToken });
    if (!sdk.channelId) return trace('voice: no channel');
    const on = (id, talking) => {
      if (talking) speaking.add(id);
      else speaking.delete(id);
      tellTable();
    };
    await sdk.subscribe(Events.SPEAKING_START, (e) => on(e.user_id, true), { channel_id: sdk.channelId });
    await sdk.subscribe(Events.SPEAKING_STOP, (e) => on(e.user_id, false), { channel_id: sdk.channelId });
    trace('voice listening', `channel ${sdk.channelId}`);
  } catch (e) {
    trace('voice failed', e?.message || JSON.stringify(e));
  }
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, '');
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) e.append(c);
  return e;
}

function show(...children) {
  const y = window.scrollY;
  app.replaceChildren(el('h1', {}, "Matt's the Gathering"), ...children);
  window.scrollTo(0, y);
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
  }
  // Only what the lobby needs; the table asks for voice (who is talking) itself, and may show Discord's prompt.
  const { code } = await within(15000, sdk.commands.authorize({
    client_id: clientId,
    response_type: 'code',
    state: '',
    prompt: 'none',
    scope: ['identify', 'guilds.members.read'],
  }), 'Discord sign-in');
  const { status, data } = await call('session', { code });
  if (status !== 200) throw new Error(data.error?.message || `Sign-in failed (${status})`);
  session = data.session;
  await sdk.commands.authenticate({ access_token: data.accessToken });
  return data;
}

async function send(change = {}) {
  const mine = change && Object.keys(change).length ? ++sent : sent;
  const { status, data } = await call('room', { instanceId, ...change });
  if (status !== 200) {
    say(data.error?.message || `The lobby isn't answering (${status})`, 'error');
    return;
  }
  if (mine < sent) return; // a newer change of ours is on its way
  room = data;
  render();
}

function render() {
  const r = room;
  if (r.started?.ticket && r.started.matchId !== closed) openTable(r.started.matchId, r.started.ticket);
  if (!r.started && closed) closed = null; // that game is over: the next one opens by itself again
  const me = r.members.find((m) => m.id === r.you);
  const seated = r.members.filter((m) => m.deck);
  const seats = seated.length + r.opponents.length;
  const roomForAis = r.maxSeats - seated.length;

  const players = el('ul', { class: 'choices' }, r.members.map((m) =>
    el('li', {}, el('div', { class: 'player' },
      m.avatar ? el('img', { class: 'face', src: face(m.avatar), alt: '' }) : el('span', { class: 'face' }),
      el('span', { class: 'name' }, m.id === r.you ? `${m.name} (you)` : m.name),
      el('span', { class: 'meta' }, m.deck ? `${m.deck.commander} · ${m.deck.name}` : 'Picking a deck…')))));

  const decks = el('ul', { class: 'choices' }, r.decks.map((d) =>
    el('li', {}, el('label', {},
      el('input', { type: 'radio', name: 'deck', value: d.id, checked: me?.deck?.id === d.id,
        onchange: () => send({ deckId: d.id }) }),
      el('span', { class: 'name' }, d.commander.split(',')[0]),
      el('span', { class: 'meta' }, d.name)))));

  const opponents = el('ul', { class: 'choices' }, r.roster.map((o) => {
    const on = r.opponents.includes(o.id);
    return el('li', {}, el('label', { class: !on && r.opponents.length >= roomForAis ? 'disabled' : '' },
      el('input', { type: 'checkbox', value: o.id, checked: on, disabled: !on && r.opponents.length >= roomForAis,
        onchange: (e) => {
          const next = e.target.checked ? [...r.opponents, o.id] : r.opponents.filter((x) => x !== o.id);
          room = { ...r, opponents: next };
          render();
          send({ opponents: next });
        } }),
      el('span', { class: 'name' }, `${o.commander.split(',')[0]} · ${o.archetype}`),
      el('span', { class: 'meta' }, `${o.plan} ${o.deck}.`)));
  }));

  const ready = seated.length > 0 && seats >= 2 && seats <= r.maxSeats && !r.starting && !r.started;
  show(
    r.problem ? el('p', { class: 'error' }, r.problem) : null,
    r.started && !r.started.ticket
      ? el('p', { class: 'status' }, 'A game from this lobby is on. You can join the next one.') : null,
    r.started?.ticket && r.started.matchId === closed
      ? el('div', { class: 'actions' }, el('button', { class: 'primary', onclick: () => { closed = null; openTable(r.started.matchId, r.started.ticket); } }, 'Back to the table'))
      : null,
    el('h2', {}, 'Players'),
    players,
    el('h2', {}, 'Your deck'),
    r.decks.length ? decks : el('p', { class: 'status' }, 'There are no decks with a commander yet.'),
    el('h2', {}, 'AI opponents ', el('span', { class: 'status' }, `everyone sees these · up to ${Math.max(roomForAis, 0)}`)),
    opponents,
    el('div', { class: 'actions' },
      el('button', { class: 'primary', disabled: !ready, onclick: () => send({ start: true }) },
        r.starting ? 'Starting…' : `Start game · ${seats} seat${seats === 1 ? '' : 's'}`),
      seated.length < r.members.length && seated.length
        ? el('span', { class: 'status' }, 'Players without a deck watch this one.') : null),
    r.error ? el('p', { class: 'error' }, r.error) : null,
  );
}

async function main() {
  if (!inDiscord) return say("Open this from Discord: it is the Activity for Matt's the Gathering.");
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
