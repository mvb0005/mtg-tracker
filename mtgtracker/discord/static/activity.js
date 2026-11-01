// The Discord Activity's lobby (mtgtracker/discord): sign in through Discord, then one room per Activity
// instance. Everyone in it sees who is here and which deck each picked, shares one set of AI opponents, and
// anyone can start: the game seats everyone with a deck plus the AIs, and each page goes to its own seat.
// Inside Discord every request goes through Discord's proxy: the tracker's API under /.proxy/ (the root URL
// mapping is the tracker's /discord/) and the table under /.proxy/table/ (mapped to /play/table/).
import { DiscordSDK } from './discord-sdk.js?v=2.5.0'; // the vendored SDK's version: change it with the file

const clientId = document.body.dataset.clientId;
const inDiscord = (() => {
  try {
    return window.parent !== window;
  } catch {
    return true;
  }
})();
const api = (path) => (inDiscord ? `/.proxy/api/${path}` : `api/${path}`);
const tableUrl = (id, ticket) =>
  `${inDiscord ? '/.proxy/table/' : '../play/table/'}${id}/?ticket=${encodeURIComponent(ticket)}`;
const POLL_MS = 1500;

const app = document.getElementById('app');
let session = null;
let sdk = null;
let instanceId = null;
let room = null;
let sent = 0; // the last change we sent: a poll answered before it landed is stale
let leaving = false;

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

async function signIn() {
  if (!sdk) {
    sdk = new DiscordSDK(clientId);
    await sdk.ready();
    instanceId = sdk.instanceId;
  }
  const { code } = await sdk.commands.authorize({
    client_id: clientId,
    response_type: 'code',
    state: '',
    prompt: 'none',
    scope: ['identify', 'guilds.members.read'],
  });
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
  if (r.started?.ticket && !leaving) {
    leaving = true;
    location.href = tableUrl(r.started.matchId, r.started.ticket);
    return say('Taking your seat…');
  }
  const me = r.members.find((m) => m.id === r.you);
  const seated = r.members.filter((m) => m.deck);
  const seats = seated.length + r.opponents.length;
  const roomForAis = r.maxSeats - seated.length;

  const players = el('ul', { class: 'choices' }, r.members.map((m) =>
    el('li', {}, el('div', { class: 'player' },
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
  } catch (e) {
    say(e.message || String(e), 'error');
  }
}

main();
