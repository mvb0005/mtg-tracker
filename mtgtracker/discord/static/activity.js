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
const cardImg = (commander) => (commander ? `${api('card-img')}?name=${encodeURIComponent(commander)}` : null);
const POLL_MS = 1500;

const app = document.getElementById('app');
let session = null;
let sdk = null;
let instanceId = null;
let room = null;
let sent = 0; // the last change we sent: a poll answered before it landed is stale
let table = null; // the open table's frame, and its match
let closed = null; // the match whose table this player closed: not reopened by itself
let pip = false; // Discord minimized the Activity into Picture-in-Picture
let previewItem = null; // commander card currently inspected in the modal
let importing = false; // importing a deck URL now
let importDraft = ''; // text in the URL import input across polls
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
      if (room) render();
    };
    await sdk.subscribe(Events.SPEAKING_START, (e) => on(uid(e), true), { channel_id: sdk.channelId });
    await sdk.subscribe(Events.SPEAKING_STOP, (e) => on(uid(e), false), { channel_id: sdk.channelId });
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
  for (const c of children.flat(Infinity)) if (c != null && c !== false) e.append(c);
  return e;
}

function show(...children) {
  const y = window.scrollY;
  const focused = document.activeElement?.classList?.contains('import-input');
  const start = focused ? document.activeElement.selectionStart : null;
  const end = focused ? document.activeElement.selectionEnd : null;
  app.replaceChildren(el('h1', {}, "Matt's Forge"), ...children.flat(Infinity).filter((c) => c != null && c !== false));
  if (focused) {
    const inp = app.querySelector('.import-input');
    if (inp) {
      inp.focus();
      if (start != null && end != null) inp.setSelectionRange(start, end);
    }
  }
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
    sdk.subscribe(Events.ACTIVITY_LAYOUT_MODE_UPDATE, (e) => {
      pip = e.layout_mode === 1;
      tellTable();
      if (room) render();
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
  room = data;
  render();
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

addEventListener('resize', () => { if (room) render(); });

function render() {
  const r = room;
  if (r.started?.ticket && r.started.matchId !== closed) openTable(r.started.matchId, r.started.ticket);
  if (!r.started && closed) closed = null; // that game is over: the next one opens by itself again
  const me = r.members.find((m) => m.id === r.you);
  const seated = r.members.filter((m) => m.deck);
  const seats = seated.length + r.opponents.length;
  const full = seats >= r.maxSeats;
  const ready = seated.length > 0 && seats >= 2 && seats <= r.maxSeats && !r.starting && !r.started;

  const mini = isMini();
  document.body.classList.toggle('mini', mini);
  if (mini) {
    app.replaceChildren(miniLobby(r, me, seated, seats, ready));
    return;
  }

  const aiDeck = (key) => {
    if (String(key).startsWith('deck:')) {
      const raw = String(key).slice(5);
      const d = r.decks.find((x) => String(x.id) === raw);
      return d ? { ...d, deck: d.name } : null;
    }
    return r.roster.find((o) => o.id === key) || null;
  };

  const addAi = (aiKey) => {
    if (full) return;
    const next = [...r.opponents, aiKey];
    room = { ...r, opponents: next };
    render();
    send({ opponents: next });
  };

  const removeAi = (idx) => {
    const next = r.opponents.filter((_, i) => i !== idx);
    room = { ...r, opponents: next };
    render();
    send({ opponents: next });
  };

  const openPreview = (e, item) => {
    e.preventDefault();
    e.stopPropagation();
    previewItem = item;
    render();
  };

  const players = el('ul', { class: 'choices' },
    r.members.map((m) =>
      el('li', {}, el('div', { class: 'player' },
        m.avatar
          ? el('img', { class: speaking.has(m.id) ? 'face talking' : 'face', src: face(m.avatar), alt: '' })
          : el('span', { class: speaking.has(m.id) ? 'face talking' : 'face' }),
        el('div', { class: 'player-text' },
          el('span', { class: 'name' }, m.id === r.you ? `${m.name} (you)` : m.guest ? `${m.name} (guest)` : m.name),
          el('span', { class: 'meta' }, m.deck ? `Commander: ${m.deck.commander} · ${m.deck.name}` : 'Picking a deck…')),
        m.deck?.commander ? el('img', {
          class: 'cmd-mini',
          src: cardImg(m.deck.commander),
          alt: m.deck.commander,
          title: `View ${m.deck.commander}`,
          onclick: (e) => openPreview(e, m.deck),
        }) : null))),
    r.opponents.map((key, idx) => {
      const info = aiDeck(key);
      const title = info ? info.commander : String(key);
      const sub = info ? [info.archetype, info.deck].filter(Boolean).join(' · ') : 'AI opponent';
      return el('li', {}, el('div', { class: 'player ai-row' },
        info?.commander ? el('img', {
          class: 'cmd-mini',
          src: cardImg(info.commander),
          alt: title,
          title: `View ${title}`,
          onclick: (e) => openPreview(e, info),
        }) : el('span', { class: 'face ai' }, 'AI'),
        el('div', { class: 'player-text' },
          el('span', { class: 'name' }, `${title} · AI`),
          el('span', { class: 'meta' }, sub)),
        el('button', {
          type: 'button',
          class: 'icon-btn remove-ai',
          title: `Remove ${title}`,
          onclick: () => removeAi(idx),
        }, '×')));
    }),
  );

  const deckRow = (item, pickId, aiKey, deckName) => {
    const cmd = item.commander || deckName;
    const stats = [item.manaCost, item.typeLine, item.pt].filter(Boolean).join(' · ');
    const desc = item.plan ? `${item.plan} (${deckName})` : deckName;
    return el('li', { class: 'deck-row' },
      el('label', { class: 'deck-card' },
        el('input', {
          type: 'radio',
          name: 'deck',
          value: pickId,
          checked: me?.deck?.id === pickId,
          onchange: () => send({ deckId: pickId }),
        }),
        item.commander ? el('img', {
          class: 'cmd-thumb',
          src: cardImg(item.commander),
          alt: cmd,
          loading: 'lazy',
          title: 'Click to enlarge commander card',
          onclick: (e) => openPreview(e, item),
        }) : el('span', { class: 'cmd-thumb empty' }, 'CMD'),
        el('div', { class: 'deck-body' },
          el('div', { class: 'deck-head' },
            el('span', { class: 'name' }, cmd),
            item.archetype ? el('span', { class: 'tag' }, item.archetype) : null),
          stats ? el('div', { class: 'cmd-stats' }, stats) : null,
          el('div', { class: 'meta' }, desc),
          item.oracleText ? el('div', { class: 'cmd-oracle' }, item.oracleText) : null)),
      el('button', {
        type: 'button',
        class: 'icon-btn add-ai',
        disabled: full,
        title: full ? `Table is full (${r.maxSeats} seats)` : `Add AI playing ${cmd}`,
        onclick: () => addAi(aiKey),
      }, '+'));
  };

  const submitImport = async (e) => {
    e.preventDefault();
    const url = importDraft.trim();
    if (!url || importing) return;
    importing = true;
    render();
    try {
      await send({ importUrl: url });
      if (!room?.error) importDraft = '';
    } finally {
      importing = false;
      render();
    }
  };

  const importForm = el('form', { class: 'import-bar', onsubmit: submitImport },
    el('input', {
      type: 'text',
      class: 'import-input',
      placeholder: 'Paste Moxfield, Archidekt, TappedOut, or MTGGoldfish deck URL…',
      value: importDraft,
      disabled: importing,
      oninput: (e) => { importDraft = e.target.value; },
    }),
    el('button', { type: 'submit', disabled: importing }, importing ? 'Importing…' : 'Import URL'));

  const myDecks = el('ul', { class: 'choices' },
    r.decks.map((d) => deckRow(d, d.id, `deck:${d.id}`, d.name)));

  const b2Decks = el('ul', { class: 'choices' },
    r.roster.map((o) => deckRow(o, o.id, o.id, o.deck)));

  const liveGames = (r.live || []).map((g) => {
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

  const modal = previewItem ? el('div', {
    class: 'cmd-modal',
    onclick: () => { previewItem = null; render(); },
  }, el('div', { class: 'cmd-modal-box', onclick: (e) => e.stopPropagation() },
    previewItem.commander ? el('img', { class: 'cmd-modal-img', src: cardImg(previewItem.commander), alt: previewItem.commander }) : null,
    el('div', { class: 'cmd-modal-info' },
      el('div', { class: 'name' }, previewItem.commander),
      el('div', { class: 'cmd-stats' }, [previewItem.manaCost, previewItem.typeLine, previewItem.pt].filter(Boolean).join(' · ')),
      previewItem.oracleText ? el('div', { class: 'cmd-oracle full' }, previewItem.oracleText) : null,
      el('div', { class: 'actions' },
        el('button', { type: 'button', onclick: () => { previewItem = null; render(); } }, 'Close'))))) : null;

  show(
    r.problem ? el('p', { class: 'error' }, r.problem) : null,
    liveGames.length ? el('section', {}, el('h2', {}, 'Live games'), ...liveGames) : null,
    r.started && !r.started.ticket
      ? el('p', { class: 'status' }, 'A game from this lobby is on. You can spectate above or join the next one.') : null,
    r.started?.ticket && r.started.matchId === closed && !liveGames.length
      ? el('div', { class: 'actions' }, el('button', { class: 'primary', onclick: () => { closed = null; openTable(r.started.matchId, r.started.ticket); } }, 'Back to the table'))
      : null,
    el('h2', {}, 'Players ', el('span', { class: 'status' }, `${seats} of ${r.maxSeats} seats`)),
    players,
    el('div', { class: 'actions' },
      el('button', { class: 'primary', disabled: !ready, onclick: () => send({ start: true }) },
        r.starting ? 'Starting…' : `Start game · ${seats} seat${seats === 1 ? '' : 's'}`),
      !full ? el('span', { class: 'status' }, 'Pick your commander below, or hit + to add an AI.') : null,
      seated.length < r.members.length && seated.length
        ? el('span', { class: 'status' }, 'Players without a deck watch this one.') : null),
    r.error ? el('p', { class: 'error' }, r.error) : null,
    el('h2', {}, 'Import a deck'),
    importForm,
    el('h2', {}, "Matt's decks"),
    r.decks.length ? myDecks : el('p', { class: 'status' }, 'There are no decks with a commander yet.'),
    el('h2', {}, 'Bracket 2 precons ', el('span', { class: 'status' }, `${r.roster.length} decks`)),
    b2Decks,
    modal,
  );
}

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
