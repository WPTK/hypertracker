/* Harness stand-in for app/static/js/api.js, implementing the contract signature:
   request(method, url, body?, {manageToken?, signal?}) -> {ok, status, data}.
   Never throws on HTTP errors; network failure resolves to a networkError result.
   Only an abort is re-thrown so callers can tell it from a failure. */
export async function request(method, url, body, opts = {}) {
  const { manageToken, signal } = opts;
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (manageToken) headers['X-Manage-Token'] = manageToken;
  try {
    const r = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      signal,
    });
    let data = null;
    try { data = await r.json(); } catch (e) { /* no body */ }
    return { ok: r.ok, status: r.status, data };
  } catch (e) {
    if (e && e.name === 'AbortError') throw e;
    return { ok: false, status: 0, data: null, networkError: true };
  }
}
