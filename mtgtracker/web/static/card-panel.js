// Card detail panel: on wide screens with a hover-capable pointer, hovering
// (or focusing) any element with data-card='{json}' shows that card's details
// in the fixed panel on the right. The last card stays shown until another
// is hovered. Everything is built with DOM APIs / textContent -- card text is
// data, never HTML.
(() => {
  const panel = document.getElementById("card-panel");
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
    wrap.append(el("dt", null, label), el("dd", null, value));
    return wrap;
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
      row("You have", `${card.qty}× ${card.finish}`),
      row("Printing", printing),
      row("Where", card.where),
      row("Cost basis", card.cost),
      row("Rarity", card.rarity && card.rarity[0].toUpperCase() + card.rarity.slice(1)),
      row("Artist", card.artist),
    ]) if (item) meta.append(item);
    body.append(meta);

    if (!card.exact && card.image) {
      body.append(el("p", "card-panel-note subtle", "Image shows a likely printing; pick the exact one on the Printings page."));
    }
    panel.replaceChildren(body);
  };

  const show = (target) => {
    if (!wide.matches) return;
    const source = target instanceof Element ? target.closest("[data-card]") : null;
    if (!source || source === current) return;
    current = source;
    try {
      render(JSON.parse(source.dataset.card));
    } catch {
      // Malformed payload: leave the previous card up.
    }
  };

  document.addEventListener("mouseover", (event) => show(event.target));
  document.addEventListener("focusin", (event) => show(event.target));
})();
