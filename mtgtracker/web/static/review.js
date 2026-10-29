// Suggestion review: swipe the card right to like it, left to pass, or use
// the arrow keys. Both just submit the page's own vote form (the buttons
// work without this script), so the optional note goes along.
(() => {
  const card = document.getElementById("review-card");
  if (!card) return;
  const form = card.querySelector(".review-form");
  const buttons = {
    up: form.querySelector('button[value="up"]'),
    down: form.querySelector('button[value="down"]'),
  };
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const interactive = "a, button, input, textarea, select, summary, label";
  let sent = false;

  const send = (vote) => {
    if (sent) return;
    sent = true;
    const submit = () => form.requestSubmit(buttons[vote]);
    if (reduced.matches) return submit();
    const dir = vote === "up" ? 1 : -1;
    card.dataset.lean = vote;
    card.classList.add("leaving");
    card.style.transform = `translateX(${dir * 110}vw) rotate(${dir * 12}deg)`;
    setTimeout(submit, 200);
  };

  document.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const target = event.target instanceof Element ? event.target : null;
    if (target && target.closest("input, textarea, select")) return;
    if (event.key === "ArrowRight") { event.preventDefault(); send("up"); }
    if (event.key === "ArrowLeft") { event.preventDefault(); send("down"); }
  });

  // The first ~8px decide between a sideways swipe (ours) and scrolling.
  let drag = null;
  const reset = () => {
    drag = null;
    card.dataset.lean = "";
    card.style.transform = "";
  };
  card.addEventListener("pointerdown", (event) => {
    if (sent || (event.pointerType === "mouse" && event.button !== 0)) return;
    if (event.target instanceof Element && event.target.closest(interactive)) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, t: event.timeStamp, sideways: null };
  });
  card.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (drag.sideways === null && Math.hypot(dx, dy) > 8) {
      drag.sideways = Math.abs(dx) > Math.abs(dy);
      if (!drag.sideways) return reset();
      card.setPointerCapture(event.pointerId);
    }
    if (!drag.sideways) return;
    card.dataset.lean = dx > 48 ? "up" : dx < -48 ? "down" : "";
    if (!reduced.matches) card.style.transform = `translateX(${dx}px) rotate(${dx / 30}deg)`;
  });
  card.addEventListener("pointerup", (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x;
    const speed = Math.abs(dx) / Math.max(1, event.timeStamp - drag.t);
    const far = Math.abs(dx) > Math.min(card.offsetWidth * 0.35, 220);
    if (drag.sideways && (far || (speed > 0.5 && Math.abs(dx) > 40))) {
      drag = null;
      return send(dx > 0 ? "up" : "down");
    }
    reset();
  });
  card.addEventListener("pointercancel", reset);
})();
