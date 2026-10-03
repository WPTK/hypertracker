/* Throwaway stub of js/tripForm.js: records calls and opens a real <dialog>. */
export function createTripForm({ user, getTrips, onSaved }) {
  window.__formCalls = [];
  window.__formSaved = onSaved;
  function open(kind, id) {
    window.__formCalls.push([kind, id]);
    const d = document.createElement("dialog");
    d.className = "dialog";
    d.setAttribute("aria-label", "Stub form");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "Close stub";
    b.addEventListener("click", () => { d.close(); d.remove(); });
    d.append(b);
    document.body.append(d);
    d.showModal();
  }
  return {
    openNew: () => open("new"),
    openEdit: (id) => open("edit", id),
    openConfirmRemove: (id) => open("remove", id),
  };
}
