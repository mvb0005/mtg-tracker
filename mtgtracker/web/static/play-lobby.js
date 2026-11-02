const root = document.getElementById('lobby');
const POLL_MS = 1500;
let room = null;
let sent = 0;
let importing = false;
let importDraft = '';

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
  root.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false));
  if (focused) {
    const inp = root.querySelector('.import-input');
    if (inp) {
      inp.focus();
      if (start != null && end != null) inp.setSelectionRange(start, end);
    }
  }
  window.scrollTo(0, y);
}

async function send(change = {}) {
  const n = ++sent;
  const res = await fetch(location.pathname, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(change),
  });
  if (!res.ok) throw new Error(res.status === 401 ? 'Sign in to join this lobby.' : `Lobby failed (${res.status})`);
  const body = await res.json();
  if (n !== sent && Object.keys(change).length === 0) return body;
  room = body;
  render();
  return body;
}

function aiDeck(key) {
  const r = room;
  if (String(key).startsWith('deck:')) {
    const raw = String(key).slice(5);
    const d = r.decks.find((x) => String(x.id) === raw);
    return d ? { ...d, deck: d.name } : null;
  }
  return r.roster.find((o) => o.id === key) || null;
}

function render() {
  const r = room;
  if (!r) return;
  const me = r.members.find((m) => m.id === r.you);
  const seated = r.members.filter((m) => m.deck);
  const seats = seated.length + r.opponents.length;
  const full = seats >= r.maxSeats;
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
  const players = el('ul', { class: 'play-choices' },
    r.members.map((m) =>
      el('li', {},
        el('div', { class: 'play-player' },
          el('span', { class: 'play-name' }, m.id === r.you ? `${m.name} (you)` : m.name),
          el('span', { class: 'play-meta' },
            m.deck ? `${m.deck.commander} · ${m.deck.name}` : 'Picking a deck…')))),
    r.opponents.map((key, idx) => {
      const info = aiDeck(key);
      const title = info ? info.commander : String(key);
      const sub = info ? [info.archetype, info.deck || info.name].filter(Boolean).join(' · ') : 'AI';
      return el('li', {},
        el('div', { class: 'play-player' },
          el('span', { class: 'play-name' }, `${title} · AI`),
          el('span', { class: 'play-meta' }, sub),
          el('button', { type: 'button', class: 'icon-btn', title: `Remove ${title}`, onclick: () => removeAi(idx) }, '×')));
    }),
  );
  const deckRow = (item, pickId, aiKey, deckName) => {
    const cmd = item.commander || deckName;
    return el('li', { class: 'deck-row' },
      el('label', { class: 'deck-card' },
        el('input', {
          type: 'radio',
          name: 'deck',
          value: String(pickId),
          checked: String(me?.deck?.id) === String(pickId),
          onchange: () => send({ deckId: pickId }),
        }),
        el('div', { class: 'deck-body' },
          el('span', { class: 'play-name' }, cmd),
          item.archetype ? el('span', { class: 'tag' }, item.archetype) : null,
          el('span', { class: 'play-meta' }, item.plan ? `${item.plan} (${deckName})` : deckName))),
      el('button', {
        type: 'button',
        class: 'icon-btn',
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
  const ready = seated.length > 0 && seats >= 2 && seats <= r.maxSeats && !r.starting && !r.started;
  show(
    r.problem ? el('p', { class: 'flash err' }, r.problem) : null,
    r.started?.tableUrl
      ? el('div', { class: 'play-actions' },
          el('a', { class: 'btn primary', href: r.started.tableUrl }, 'Join the table'))
      : null,
    r.started && !r.started.ticket
      ? el('p', { class: 'subtle' }, 'This lobby’s game is on. You can spectate from Play, or join the next one.')
      : null,
    el('h2', {}, 'Players ', el('span', { class: 'subtle' }, `${seats} of ${r.maxSeats} seats`)),
    players,
    el('div', { class: 'play-actions' },
      el('button', {
        type: 'button',
        class: 'primary',
        disabled: !ready,
        onclick: async () => {
          const body = await send({ start: true });
          if (body.started?.tableUrl) location.href = body.started.tableUrl;
        },
      }, r.starting ? 'Starting…' : `Start game · ${seats} seat${seats === 1 ? '' : 's'}`),
      !full ? el('span', { class: 'subtle' }, 'Pick your commander below, or hit + to add an AI.') : null),
    r.error ? el('p', { class: 'flash err' }, r.error) : null,
    el('h2', {}, 'Import a deck'),
    el('form', { class: 'import-bar', onsubmit: submitImport },
      el('input', {
        type: 'text',
        class: 'import-input',
        placeholder: 'Paste Moxfield, Archidekt, TappedOut, or MTGGoldfish deck URL…',
        value: importDraft,
        disabled: importing,
        oninput: (e) => { importDraft = e.target.value; },
      }),
      el('button', { type: 'submit', disabled: importing }, importing ? 'Importing…' : 'Import URL')),
    el('h2', {}, 'Your decks'),
    r.decks.length
      ? el('ul', { class: 'play-choices' }, r.decks.map((d) => deckRow(d, d.id, `deck:${d.id}`, d.name)))
      : el('p', { class: 'empty' }, 'No commander decks yet. Import a URL or make one under Decks.'),
    el('h2', {}, 'Bracket 2 precons ', el('span', { class: 'subtle' }, `${r.roster.length} decks`)),
    el('ul', { class: 'play-choices' }, r.roster.map((o) => deckRow(o, o.id, o.id, o.deck))),
  );
}

const copyBtn = document.getElementById('lobby-copy');
const linkInp = document.getElementById('lobby-link');
if (copyBtn && linkInp) {
  copyBtn.addEventListener('click', async () => {
    linkInp.select();
    try {
      await navigator.clipboard.writeText(linkInp.value);
      copyBtn.textContent = 'Copied';
    } catch {
      copyBtn.textContent = 'Select and copy';
    }
    setTimeout(() => { copyBtn.textContent = 'Copy link'; }, 1500);
  });
}

send().catch((e) => show(el('p', { class: 'flash err' }, e.message || String(e))));
setInterval(() => send().catch(() => {}), POLL_MS);
