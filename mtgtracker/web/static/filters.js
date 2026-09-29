// Filter sidebar (form[data-autosubmit], see facet_sidebar in
// _card_tiles.html). Submits as soon as any control changes -- including the
// Sort select in the results bar, which belongs to the form via
// form="facet-form" -- so the Apply button is only needed without JS. The
// name search still submits on Enter. Empty fields and the default sort are
// left out of the URL (disabled controls aren't submitted) to keep links
// clean. (Folding the sidebar on phones is an inline script in
// facet_sidebar, so it happens before first paint.)
(() => {
  for (const form of document.querySelectorAll("form[data-autosubmit]")) {
    form.classList.add("js-autosubmit");
    form.addEventListener("submit", () => {
      for (const field of form.elements) {
        if (!field.name) continue;
        const isDefault = (field.name === "sort" && field.value === "mv")
          || (field.name.endsWith("_mode") && field.value === "any");
        if (field.value === "" || isDefault) field.disabled = true;
      }
    });
    for (const field of form.elements) {
      if (field.tagName === "SELECT" || field.type === "checkbox" || field.type === "radio") {
        field.addEventListener("change", () => form.requestSubmit());
      }
    }
  }
})();
