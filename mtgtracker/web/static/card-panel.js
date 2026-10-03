// Card details for any element with data-card='{json}'.
// - Wide screens with a hover-capable pointer: hovering (or focusing) a card
//   shows it in the panel on the right; the last card stays until another
//   is hovered.
// - Everywhere else (phones, tablets, narrow windows): tapping/clicking a
//   card -- or Enter/Space on a focused one -- opens the same details in a
//   modal <dialog> sheet. Taps on links, buttons and form controls inside a
//   card keep their normal behavior. On phones the sheet can be swiped down
//   to dismiss, or left/right for the next/previous card (arrow keys on
//   narrow desktop windows). Everything is built with DOM APIs / textContent -- card text is
// data, never HTML.
(() => {
  const panel = document.getElementById("card-panel");
  const dialog = document.getElementById("card-dialog");
  if (!panel) return;
  const wide = window.matchMedia("(hover: hover) and (min-width: 1480px)");
  let current = null;

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };

  // "{2}{R}" / "{T}: Add {C}" -> text with Scryfall mana-symbol images.
  const withSymbols = (text, className) => {
    const node = el("span", className);
    for (const part of String(text).split(/(\{[^}]+\})/)) {
      if (!part) continue;
      const match = part.match(/^\{([^}]+)\}$/);
      if (match) {
        const img = el("img", "mana");
        img.src = `https://svgs.scryfall.io/card-symbols/${encodeURIComponent(match[1].replace(/\//g, ""))}.svg`;
        img.alt = part;
        img.referrerPolicy = "no-referrer";
        node.append(img);
      } else {
        node.append(document.createTextNode(part));
      }
    }
    return node;
  };

  const row = (label, value) => {
    if (!value) return null;
    const wrap = el("div", "card-panel-row");
    const dd = el("dd");
    if (value instanceof Node) dd.append(value);
    else dd.textContent = value;
    wrap.append(el("dt", null, label), dd);
    return wrap;
  };

  // Every deck whose list has this card: a link, how many copies that list
  // has, and -- when there's more than one deck -- which one holds it now.
  const deckList = (decks) => {
    const list = el("ul", "card-panel-decks");
    for (const deck of decks) {
      const item = el("li");
      const link = el("a", null, deck.name);
      link.href = `/decks/${encodeURIComponent(deck.id)}`;
      item.append(el("span", "card-panel-deck-count", `${deck.count}×`), link);
      if (deck.current && decks.length > 1) item.append(el("span", "tag ok", "current"));
      list.append(item);
    }
    return list;
  };

  // Friends who own this card: "Alex · 3 (3 in no deck)", each linking to
  // that card in their collection.
  const friendList = (friends, cardName) => {
    const list = el("ul", "card-panel-decks");
    for (const friend of friends) {
      const item = el("li");
      const link = el("a", null, friend.name);
      link.href = `/people/${encodeURIComponent(friend.id)}/cards?q=${encodeURIComponent(cardName.split(" // ")[0])}`;
      item.append(el("span", "card-panel-deck-count", `${friend.qty}×`), link);
      if (friend.where) item.append(el("span", "subtle", ` ${friend.where}`));
      list.append(item);
    }
    return list;
  };
  const render = (card) => {
    const body = el("div", "card-panel-body");

    // The image slot always has the card's shape (CSS aspect-ratio), even
    // before the image loads or when there's none, so the text below never
    // jumps while hovering quickly across cards.
    if (card.image) {
      const img = el("img", "card-panel-image");
      img.alt = card.name;
      img.referrerPolicy = "no-referrer";
      img.decoding = "async";
      img.src = card.image;
      body.append(img);
    } else {
      body.append(el("div", "card-panel-image card-panel-placeholder", card.name));
    }

    const faces = card.faces && card.faces.length ? card.faces : [{ name: card.name }];
    for (const face of faces) {
      const section = el("section", "card-panel-face");
      const head = el("div", "card-panel-head");
      head.append(el("h2", null, face.name || card.name));
      if (face.mana_cost) head.append(withSymbols(face.mana_cost, "card-panel-cost"));
      section.append(head);
      if (face.type_line) section.append(el("div", "card-panel-type", face.type_line));
      if (face.oracle_text) {
        const text = el("div", "card-panel-text");
        for (const para of face.oracle_text.split("\n")) text.append(withSymbols(para, "para"));
        section.append(text);
      }
      if (face.stats) section.append(el("div", "card-panel-stats", face.stats));
      body.append(section);
    }

    const meta = el("dl", "card-panel-meta");
    const printing = card.printing
      ? `${card.printing}${card.set_name ? ` · ${card.set_name}` : ""}`
      : "Not picked yet";
    for (const item of [
      // Merged cards on /cards can mix finishes: "2 (1 foil, 1 nonfoil)".
      card.wanted
        ? row("You need", `${card.qty}×`)
        : row("You have", /\d/.test(card.finish) ? `${card.qty} (${card.finish})` : `${card.qty}× ${card.finish}`),
      row("Printing", printing),
      card.decks && card.decks.length
        ? row(card.decks.length > 1 ? "Decks" : "Deck", deckList(card.decks))
        : row("Where", card.where),
      // Held somewhere that isn't one of its lists, or sold: say so too.
      card.decks && card.decks.length && !card.decks.some((d) => d.current) ? row("Now", card.where) : null,
      card.friends && card.friends.length
        ? row(card.wanted ? "Borrow from" : "Friends have", friendList(card.friends, card.name || ""))
        : null,
      row("Cost basis", card.cost),
      row("Rarity", card.rarity && card.rarity[0].toUpperCase() + card.rarity.slice(1)),
      row("Artist", card.artist),
    ]) if (item) meta.append(item);
    body.append(meta);

    if (!card.exact && card.image) {
      body.append(el("p", "card-panel-note subtle", "Image shows a likely printing; pick the exact one on the Printings page."));
    }
    return body;
  };

  const parse = (source) => {
    try {
      return JSON.parse(source.dataset.card);
    } catch {
      return null; // malformed payload: ignore
    }
  };

  const show = (target) => {
    if (!wide.matches) return;
    const source = target instanceof Element ? target.closest("[data-card]") : null;
    if (!source || source === current) return;
    const card = parse(source);
    if (!card) return;
    current = source;
    panel.replaceChildren(render(card));
  };

  // Tap-to-open sheet when there's no side panel.
  const interactive = "a, button, input, select, textarea, label, summary";
  const content = dialog && dialog.querySelector(".card-dialog-content");
  const position = dialog && dialog.querySelector(".card-dialog-position");
  let sheetSource = null; // the tile/row whose card the sheet shows

  // Cards the sheet can step through: the page's tiles or list rows, in the
  // order shown (so current sort/filters apply). Not the commander picker.
  const sheetCards = () => [...document.querySelectorAll(".tile[data-card], .deck-row[data-card]")];

  const fillSheet = (source) => {
    const card = parse(source);
    if (!card) return false;
    sheetSource = source;
    content.replaceChildren(render(card));
    const cards = sheetCards();
    const index = cards.indexOf(source);
    position.textContent = index >= 0 && cards.length > 1 ? `${index + 1} / ${cards.length}` : "";
    return true;
  };

  const openSheet = (target) => {
    if (wide.matches || !dialog || !(target instanceof Element)) return false;
    if (target.closest(interactive)) return false;
    const source = target.closest("[data-card]");
    if (!source || !fillSheet(source)) return false;
    dialog.showModal();
    dialog.scrollTop = 0;
    return true;
  };

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // Show the next (+1) or previous (-1) card. `fromX` is where a swipe left
  // the content, so the outgoing card continues from under the finger.
  // Returns false at either end of the list.
  const stepSheet = (dir, fromX = 0) => {
    const cards = sheetCards();
    const next = cards[cards.indexOf(sheetSource) + dir];
    if (!next) return false;
    const swap = () => {
      fillSheet(next);
      dialog.scrollTop = 0;
      content.style.transform = "";
    };
    if (reducedMotion.matches) {
      swap();
      return true;
    }
    const width = content.offsetWidth || 320;
    content.animate(
      [{ transform: `translateX(${fromX}px)`, opacity: 1 }, { transform: `translateX(${-dir * width}px)`, opacity: 0 }],
      { duration: 140, easing: "ease-in" },
    ).finished.then(() => {
      swap();
      content.animate(
        [{ transform: `translateX(${dir * width * 0.5}px)`, opacity: 0 }, { transform: "none", opacity: 1 }],
        { duration: 200, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" },
      );
    });
    return true;
  };

  document.addEventListener("mouseover", (event) => show(event.target));
  document.addEventListener("focusin", (event) => show(event.target));
  document.addEventListener("click", (event) => {
    if (openSheet(event.target)) event.preventDefault();
  });
  document.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && event.target.matches?.("[data-card]")) {
      if (openSheet(event.target)) event.preventDefault();
    }
  });
  if (dialog) {
    // Animated close: play the slide-down (CSS .closing), then really close.
    // With reduced motion (or no animation) it closes immediately.
    const closeSheet = () => {
      if (!dialog.open || dialog.classList.contains("closing")) return;
      dialog.classList.add("closing");
      const finish = () => {
        dialog.classList.remove("closing");
        dialog.style.transform = ""; // drop any swipe offset
        dialog.close();
      };
      if (getComputedStyle(dialog).animationName === "none") return finish();
      dialog.addEventListener("animationend", finish, { once: true });
      setTimeout(() => dialog.classList.contains("closing") && finish(), 500); // safety net
    };
    // Tapping the backdrop (outside the sheet) closes it.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) closeSheet();
    });
    // Escape ("cancel") closes; arrow keys step through cards.
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeSheet();
    });
    dialog.addEventListener("keydown", (event) => {
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        stepSheet(event.key === "ArrowRight" ? 1 : -1);
      }
    });

    // Touch gestures on the phone bottom sheet. The first ~8px of movement
    // picks the axis:
    // - Sideways: the card follows the finger; release past ~a quarter of
    //   the width (or flick) to go to the next (swipe left) or previous
    //   (swipe right) card. At either end it resists and springs back.
    // - Down: once the content is scrolled to the top, the sheet follows the
    //   finger; release past ~a third of its height (or flick) to close,
    //   otherwise it snaps back. Scrolling inside a tall sheet is left alone.
    const sheetMode = window.matchMedia("(max-width: 600px)");
    let touch = null; // {x0, y0, lastX, lastY, lastT, axis, dragY, dragX, vx, vy, dragging}

    dialog.addEventListener("touchstart", (event) => {
      if (!sheetMode.matches || event.touches.length !== 1 || dialog.classList.contains("closing")) return;
      const { clientX: x, clientY: y } = event.touches[0];
      touch = { x0: x, y0: y, lastX: x, lastY: y, lastT: performance.now(),
                axis: null, dragging: false, startY: y, dragY: 0, dragX: 0, vx: 0, vy: 0 };
    }, { passive: true });

    dialog.addEventListener("touchmove", (event) => {
      if (!touch) return;
      const { clientX: x, clientY: y } = event.touches[0];
      const now = performance.now();
      const dt = now - touch.lastT;
      if (dt > 0) {
        touch.vx = (x - touch.lastX) / dt;
        touch.vy = (y - touch.lastY) / dt;
      }
      const prevY = touch.lastY;
      touch.lastX = x;
      touch.lastY = y;
      touch.lastT = now;

      if (!touch.axis) {
        const dx = x - touch.x0;
        const dy = y - touch.y0;
        if (Math.max(Math.abs(dx), Math.abs(dy)) < 8) return;
        touch.axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
        if (touch.axis === "x") touch.x0 = x; // start the drag here
      }

      if (touch.axis === "x") {
        const cards = sheetCards();
        const index = cards.indexOf(sheetSource);
        let dx = x - touch.x0;
        const atEnd = (dx < 0 && index >= cards.length - 1) || (dx > 0 && index <= 0);
        if (atEnd) dx *= 0.3; // rubber band: nothing further that way
        touch.dragX = dx;
        content.style.transition = "none";
        content.style.transform = `translateX(${dx}px)`;
        event.preventDefault();
        return;
      }

      // Vertical: drag to dismiss once scrolled to the top and pulling down.
      if (!touch.dragging) {
        if (y > prevY && dialog.scrollTop <= 0) {
          touch.dragging = true;
          touch.startY = y;
          dialog.classList.add("dragging");
        } else {
          return; // normal content scroll
        }
      }
      touch.dragY = Math.max(0, y - touch.startY);
      dialog.style.transform = `translateY(${touch.dragY}px)`;
      event.preventDefault(); // no page/rubber-band scroll while dragging
    }, { passive: false });

    const endTouch = () => {
      const t = touch;
      touch = null;
      if (!t) return;
      if (t.axis === "x") {
        const width = content.offsetWidth || 320;
        const far = Math.abs(t.dragX) > Math.min(90, width * 0.25) || Math.abs(t.vx) > 0.5;
        const dir = t.dragX < 0 ? 1 : -1; // swipe left -> next card
        if (!(far && t.dragX !== 0 && stepSheet(dir, t.dragX))) {
          content.style.transition = "transform 200ms cubic-bezier(0.2, 0.8, 0.2, 1)";
          content.style.transform = "";
          setTimeout(() => { content.style.transition = ""; }, 220);
        } else {
          content.style.transition = "";
        }
        return;
      }
      if (!t.dragging) return;
      dialog.classList.remove("dragging");
      if (t.dragY > Math.min(160, dialog.offsetHeight * 0.33) || t.vy > 0.5) {
        closeSheet(); // slides down from where the finger left it
      } else {
        dialog.classList.add("snapping");
        dialog.style.transform = "";
        setTimeout(() => dialog.classList.remove("snapping"), 250);
      }
    };
    dialog.addEventListener("touchend", endTouch);
    dialog.addEventListener("touchcancel", endTouch);
  }
})();
