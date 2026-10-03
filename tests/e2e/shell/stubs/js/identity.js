/* Throwaway stub of js/identity.js for the shell harness. */
export function identity() { return { uid: null, name: "" }; }
export function saveIdentity() {}
export function manageToken() { return null; }
export function rememberManage() {}
export function forgetManage() {}
export function pruneManage(ids) { (window.__pruned = window.__pruned || []).push([...ids]); }
export function canManage(ownerId, tripId, { me, isAdmin } = {}) { return !!isAdmin || (!!me && me === ownerId); }
export function proofToken() { return null; }
export function seedFromFragment() {
  const m = /^#manage=(\d+)\.(.+)$/.exec(location.hash);
  if (!m) return null;
  history.replaceState(null, "", location.pathname + location.search);
  return { tripId: m[1] };
}
