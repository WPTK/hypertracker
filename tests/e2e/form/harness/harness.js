import { createTripForm } from '/static/js/tripForm.js';
import { seedFromFragment, canManage, pruneManage } from '/static/js/identity.js';

const params = new URLSearchParams(location.search);
const user = params.get('user') ? { id: 'u1', name: 'Alex' } : null;
window.__trips = [];
window.__saved = 0;
window.__seed = seedFromFragment();
if (window.__seed) document.getElementById('seed').textContent = `Seeded trip ${window.__seed.tripId}`;

async function load() {
  const r = await fetch('api/trips', { credentials: 'same-origin' });
  const data = await r.json();
  window.__trips = data.trips;
  pruneManage(new Set(data.trips.map((t) => t.id)));
  const ul = document.getElementById('trips');
  ul.replaceChildren();
  for (const t of data.trips) {
    const li = document.createElement('li');
    li.dataset.tripId = t.id;
    const legs = t.out.concat(t.ret);
    const span = document.createElement('span');
    span.textContent = `${t.owner_name}: ` + legs.map((l) => (l.flight_no || `${l.from_iata} to ${l.to_iata}`) + (l.unverified ? ' (unverified)' : '')).join(', ');
    li.append(span);
    if (canManage(t.owner_id, t.id, { me: data.me, isAdmin: data.is_admin })) {
      const e = document.createElement('button');
      e.type = 'button'; e.className = 'btn btn--sm'; e.textContent = 'Edit'; e.dataset.edit = t.id;
      e.addEventListener('click', () => form.openEdit(t.id));
      const d = document.createElement('button');
      d.type = 'button'; d.className = 'btn btn--sm'; d.textContent = 'Remove'; d.dataset.remove = t.id;
      d.addEventListener('click', () => form.openConfirmRemove(t.id));
      li.append(e, d);
    }
    ul.append(li);
  }
}

const form = createTripForm({
  user,
  getTrips: () => window.__trips,
  onSaved: () => { window.__saved += 1; load(); },
});
document.getElementById('openAdd').addEventListener('click', () => form.openNew());
window.__form = form;
await load();
document.documentElement.dataset.ready = '1';
