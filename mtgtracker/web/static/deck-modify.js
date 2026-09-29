// Modify page: the shared replacement picker (#cut-picker). Each change's
// "Choose a card to replace" button moves the one picker under that change
// and points its form at the change's /cut endpoint; clicking a card tile
// submits it. Without JS the buttons stay hidden and a plain <select>
// (in <noscript>) does the job.
(() => {
  const picker = document.getElementById("cut-picker");
  if (!picker) return;
  const form = picker.querySelector("form");
  const title = picker.querySelector(".cut-picker-title");
  let owner = null;

  for (const button of document.querySelectorAll("[data-pick-cut]")) button.hidden = false;

  const close = () => {
    picker.hidden = true;
    if (owner) owner.setAttribute("aria-expanded", "false");
    owner = null;
  };

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-pick-cut]");
    if (button) {
      event.preventDefault();
      if (owner === button) return close();
      if (owner) owner.setAttribute("aria-expanded", "false");
      owner = button;
      button.setAttribute("aria-expanded", "true");
      form.action = button.dataset.pickCut;
      title.textContent = button.dataset.pickTitle;
      button.closest(".change").after(picker);
      picker.hidden = false;
      picker.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (event.target.closest("[data-picker-close]")) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !picker.hidden) close();
  });
})();
