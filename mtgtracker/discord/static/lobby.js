// The lobby both lobby pages draw: the tracker's shareable /play/lobby/<code> (web/static/play-lobby.js) and
// the Discord Activity's (activity.js). Who is here, the open seats, the AIs, the decks to pick from, a deck
// import and a commander preview. It lives under /discord/ because inside Discord only that is reachable.
// Each page imports it at the version its template names (data-lobby) and supplies what differs: how to
// reach the server, the URLs (inside Discord they go through its proxy), avatars, and what goes above the
// list. Both servers send the same lobby shape (play/routes.py lobby_view).

export function el(tag, attrs = {}, ...children) {
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

/**
 * opts:
 *   root            the element the lobby draws into
 *   send(change)    posts a change to the server; the page hands the answer back with set()
 *   start()         starts the game
 *   cardImg(name)   a commander's image URL
 *   face(member)    the member's picture, or null
 *   decksTitle      the heading over the decks you can pick
 *   noDecks         what to say when there are none
 *   note, error     the page's classes for quiet text and errors
 *   title()         nodes above everything (optional)
 *   takeover(room)  an element to show instead of the lobby, or null (optional)
 *   top(room, me)   nodes between the problem line and the players (optional)
 *   notes(room)     notes beside the start button (optional)
 */
export function createLobby(opts) {
  const { root } = opts;
  let room = null;
  let preview = null; // the commander card shown enlarged
  let importing = false;
  let importDraft = ''; // the import box's text, kept across redraws

  // Each commander's art loads once, outside the redraws (the lobby redraws on every poll). Until it has
  // loaded, or if Scryfall doesn't know the name, the row shows a placeholder.
  const art = new Map();
  const hasArt = (name) => {
    if (!name) return false;
    if (!art.has(name)) {
      art.set(name, 'loading');
      const img = new Image();
      img.onload = () => { art.set(name, 'ok'); render(); };
      img.onerror = () => art.set(name, 'missing');
      img.src = opts.cardImg(name);
    }
    return art.get(name) === 'ok';
  };

  const stats = (item) => [item.manaCost, item.typeLine, item.pt].filter(Boolean).join(' · ');

  function show(...children) {
    const y = window.scrollY;
    const focused = document.activeElement?.classList?.contains('lb-import-input');
    const start = focused ? document.activeElement.selectionStart : null;
    const end = focused ? document.activeElement.selectionEnd : null;
    root.replaceChildren(...[opts.title?.(), ...children].flat(Infinity).filter((c) => c != null && c !== false));
    if (focused) {
      const inp = root.querySelector('.lb-import-input');
      if (inp) {
        inp.focus();
        if (start != null && end != null) inp.setSelectionRange(start, end);
      }
    }
    window.scrollTo(0, y);
  }

  function change(next) {
    room = { ...room, ...next };
    render();
    opts.send(next);
  }

  function openPreview(e, item) {
    e.preventDefault();
    e.stopPropagation();
    preview = item;
    render();
  }

  function closePreview() {
    preview = null;
    render();
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && preview) closePreview();
  });

  function aiDeck(key) {
    if (String(key).startsWith('deck:')) {
      const raw = String(key).slice(5);
      const d = room.decks.find((x) => String(x.id) === raw);
      return d ? { ...d, deck: d.name } : null;
    }
    return room.roster.find((o) => o.id === key) || null;
  }

  function render() {
    const r = room;
    if (!r) return;
    const own = opts.takeover?.(r);
    if (own) {
      root.replaceChildren(own);
      return;
    }
    const me = r.members.find((m) => m.id === r.you);
    const seated = r.members.filter((m) => m.deck);
    const seats = seated.length + r.opponents.length;
    const full = seats >= r.maxSeats;
    const ready = seated.length > 0 && seats >= 2 && seats <= r.maxSeats && !r.starting && !r.started;

    const mini = (item, fallback) => (hasArt(item?.commander)
      ? el('img', {
          class: 'lb-mini',
          src: opts.cardImg(item.commander),
          alt: item.commander,
          title: `View ${item.commander}`,
          onclick: (e) => openPreview(e, item),
        })
      : el('span', { class: 'lb-mini empty' }, fallback));

    const seat = (cls, ...children) => el('li', {}, el('div', { class: `lb-seat ${cls}` }, ...children));
    const players = el('ul', { class: 'lb-seats' },
      r.members.map((m) => {
        const pic = opts.face?.(m);
        const name = m.id === r.you ? `${m.name} (you)` : m.guest ? `${m.name} (guest)` : m.name;
        return seat('',
          mini(m.deck, '?'),
          pic,
          el('div', { class: 'lb-text' },
            el('span', { class: 'lb-name' }, name),
            el('span', { class: 'lb-meta' }, m.deck ? `${m.deck.commander} · ${m.deck.name}` : 'Picking a deck…')));
      }),
      r.opponents.map((key, idx) => {
        const info = aiDeck(key);
        const title = info ? info.commander : String(key);
        const sub = info ? [info.archetype, info.deck || info.name].filter(Boolean).join(' · ') : 'AI';
        return seat('ai',
          mini(info, 'AI'),
          el('div', { class: 'lb-text' },
            el('span', { class: 'lb-name' }, `${title} · AI`),
            el('span', { class: 'lb-meta' }, sub)),
          el('button', {
            type: 'button',
            class: 'icon-btn lb-remove',
            title: `Remove ${title}`,
            onclick: () => change({ opponents: r.opponents.filter((_, i) => i !== idx) }),
          }, '×'));
      }),
      // Open seats keep the list at the table's size, so adding an AI doesn't push the page down.
      Array.from({ length: Math.max(0, r.maxSeats - r.members.length - r.opponents.length) }, () =>
        seat('open',
          el('span', { class: 'lb-mini open' }),
          el('div', { class: 'lb-text' },
            el('span', { class: 'lb-name' }, 'Open seat'),
            el('span', { class: 'lb-meta' }, 'Pick a deck below, or hit + to add an AI')))),
    );

    const deckRow = (item, pickId, aiKey, deckName) => {
      const cmd = item.commander || deckName;
      const line = stats(item);
      return el('li', { class: 'lb-deck' },
        el('label', { class: 'lb-pick' },
          el('input', {
            type: 'radio',
            name: 'deck',
            value: String(pickId),
            checked: String(me?.deck?.id) === String(pickId),
            onchange: () => opts.send({ deckId: pickId }),
          }),
          hasArt(item.commander) ? el('img', {
            class: 'lb-thumb',
            src: opts.cardImg(item.commander),
            alt: cmd,
            title: 'Click to enlarge the commander',
            onclick: (e) => openPreview(e, item),
          }) : el('span', { class: 'lb-thumb empty' }, 'CMD'),
          el('div', { class: 'lb-deck-body' },
            el('div', { class: 'lb-deck-head' },
              el('span', { class: 'lb-name' }, cmd),
              item.archetype ? el('span', { class: 'lb-tag' }, item.archetype) : null),
            line ? el('div', { class: 'lb-stats' }, line) : null,
            el('div', { class: 'lb-meta' }, item.plan ? `${item.plan} (${deckName})` : deckName),
            item.oracleText ? el('div', { class: 'lb-oracle' }, item.oracleText) : null)),
        el('button', {
          type: 'button',
          class: 'icon-btn lb-add',
          disabled: full,
          title: full ? `Table is full (${r.maxSeats} seats)` : `Add AI playing ${cmd}`,
          onclick: () => { if (!full) change({ opponents: [...r.opponents, aiKey] }); },
        }, '+'));
    };

    const submitImport = async (e) => {
      e.preventDefault();
      const url = importDraft.trim();
      if (!url || importing) return;
      importing = true;
      render();
      try {
        await opts.send({ importUrl: url });
        if (!room?.error) importDraft = '';
      } finally {
        importing = false;
        render();
      }
    };

    const modal = preview ? el('div', { class: 'lb-modal', onclick: closePreview },
      el('div', { class: 'lb-modal-box', onclick: (e) => e.stopPropagation() },
        hasArt(preview.commander)
          ? el('img', { class: 'lb-modal-img', src: opts.cardImg(preview.commander), alt: preview.commander })
          : null,
        el('div', { class: 'lb-modal-info' },
          el('span', { class: 'lb-name' }, preview.commander),
          stats(preview) ? el('span', { class: 'lb-stats' }, stats(preview)) : null,
          preview.oracleText ? el('p', { class: 'lb-oracle full' }, preview.oracleText) : null,
          el('div', {}, el('button', { type: 'button', onclick: closePreview }, 'Close'))))) : null;

    show(
      r.problem ? el('p', { class: opts.error }, r.problem) : null,
      opts.top?.(r, me),
      el('h2', {}, 'Players ', el('span', { class: opts.note }, `${seats} of ${r.maxSeats} seats`)),
      players,
      el('div', { class: 'lb-actions' },
        el('button', { type: 'button', class: 'primary', disabled: !ready, onclick: () => opts.start() },
          r.starting ? 'Starting…' : `Start game · ${seats} seat${seats === 1 ? '' : 's'}`),
        !full ? el('span', { class: opts.note }, 'Pick your commander below, or hit + to add an AI.') : null,
        opts.notes?.(r, seated)),
      r.error ? el('p', { class: opts.error }, r.error) : null,
      el('h2', {}, 'Import a deck'),
      el('form', { class: 'lb-import', onsubmit: submitImport },
        el('input', {
          type: 'text',
          class: 'lb-import-input',
          placeholder: 'Paste Moxfield, Archidekt, TappedOut, or MTGGoldfish deck URL…',
          value: importDraft,
          disabled: importing,
          oninput: (e) => { importDraft = e.target.value; },
        }),
        el('button', { type: 'submit', disabled: importing }, importing ? 'Importing…' : 'Import URL')),
      el('h2', {}, opts.decksTitle),
      r.decks.length
        ? el('ul', { class: 'lb-decks' }, r.decks.map((d) => deckRow(d, d.id, `deck:${d.id}`, d.name)))
        : el('p', { class: opts.note }, opts.noDecks),
      el('h2', {}, 'Bracket 2 precons ', el('span', { class: opts.note }, `${r.roster.length} decks`)),
      el('ul', { class: 'lb-decks' }, r.roster.map((o) => deckRow(o, o.id, o.id, o.deck))),
      modal,
    );
  }

  return {
    get room() { return room; },
    set(next) { room = next; render(); },
    render,
  };
}
