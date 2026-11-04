// The shareable /play/lobby/<code>: the shared lobby (discord/static/lobby.js), polled from this page's URL.
const { createLobby, el } = await import(`/discord/static/lobby.js?v=${document.getElementById('lobby').dataset.lobby}`);

const POLL_MS = 1500;
let sent = 0; // the last change we sent: a poll answered after it is stale

async function send(change = {}) {
  const mine = Object.keys(change).length ? ++sent : sent;
  const res = await fetch(location.pathname, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(change),
  });
  if (!res.ok) throw new Error(res.status === 401 ? 'Sign in to join this lobby.' : `Lobby failed (${res.status})`);
  const body = await res.json();
  if (mine < sent) return body; // a newer change of ours is on its way
  lobby.set(body);
  return body;
}

const lobby = createLobby({
  root: document.getElementById('lobby'),
  send: (change) => send(change).catch((e) => lobby.set({ ...lobby.room, error: e.message })),
  start: async () => {
    const body = await send({ start: true });
    if (body.started?.tableUrl) location.href = body.started.tableUrl;
  },
  cardImg: (name) => `/discord/api/card-img?name=${encodeURIComponent(name)}`,
  decksTitle: 'Your decks',
  noDecks: 'No commander decks yet. Import a URL or make one under Decks.',
  note: 'subtle',
  error: 'flash err',
  top: (r) => [
    r.started?.tableUrl
      ? el('div', { class: 'lb-actions' }, el('a', { class: 'btn primary', href: r.started.tableUrl }, 'Join the table'))
      : null,
    r.started && !r.started.ticket
      ? el('p', { class: 'subtle' }, 'This lobby’s game is on. You can spectate from Play, or join the next one.')
      : null,
  ],
});

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

send().catch((e) => { document.getElementById('lobby').replaceChildren(el('p', { class: 'flash err' }, e.message)); });
setInterval(() => send().catch(() => {}), POLL_MS);
