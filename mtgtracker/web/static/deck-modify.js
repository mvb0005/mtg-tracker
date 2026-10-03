// Modify page pickers. Each [data-picker] button moves its one shared
// picker (#cut-picker: what a queued change replaces; #own-picker: one of
// your cards to replace a card you don't own) under the element it belongs
// to (closest data-anchor), points the picker's form at it (data-action
// and/or a data-want-id hidden field) and, for #own-picker, puts the
// section matching the card's type first. Clicking a card tile submits.
// Without JS the buttons stay hidden and <noscript> forms do the job.
(() => {
  const buttons = document.querySelectorAll("[data-picker]");
  for (const button of buttons) button.hidden = false;
  let owner = null;

  const close = () => {
    if (!owner) return;
    document.getElementById(owner.dataset.picker).hidden = true;
    owner.setAttribute("aria-expanded", "false");
    owner = null;
  };

  const open = (button) => {
    const picker = document.getElementById(button.dataset.picker);
    if (!picker) return;
    const form = picker.querySelector("form");
    if (button.dataset.action) form.action = button.dataset.action;
    const want = form.querySelector('input[name="want_id"]');
    if (want) want.value = button.dataset.wantId || "";
    const title = picker.querySelector(".cut-picker-title");
    if (title && button.dataset.title) title.textContent = button.dataset.title;

    const category = button.dataset.category;
    const sections = [...picker.querySelectorAll(".picker-section")];
    for (const section of sections) section.classList.toggle("same", section.dataset.category === category);
    const first = sections.find((section) => section.dataset.category === category);
    if (first && sections[0] !== first) sections[0].before(first);

    button.closest(button.dataset.anchor || "*").after(picker);
    picker.hidden = false;
    owner = button;
    button.setAttribute("aria-expanded", "true");
    picker.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-picker]");
    if (button) {
      event.preventDefault();
      const reopen = owner !== button;
      close();
      if (reopen) open(button);
      return;
    }
    if (event.target.closest("[data-picker-close]")) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });
})();
