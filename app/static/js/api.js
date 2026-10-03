/* HTTP helpers. All URLs are relative (api/trips) because the page sets <base href>. */

const NETWORK_FAILURE = () => ({ ok: false, status: 0, data: null, networkError: true });

/**
 * request(method, url, body?, {manageToken, signal}) -> {ok, status, data}
 * Never throws. HTTP errors come back with ok:false and the parsed JSON body
 * (or null). A network failure or abort comes back as
 * {ok:false, status:0, data:null, networkError:true} (plus aborted:true when
 * the caller's signal fired).
 */
export async function request(method, url, body, { manageToken, signal } = {}) {
  const headers = { Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin", signal };
  if (body !== undefined && body !== null) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (manageToken) headers["X-Manage-Token"] = manageToken;
  let res;
  try {
    res = await fetch(url, init);
  } catch (err) {
    const out = NETWORK_FAILURE();
    if (err && err.name === "AbortError") out.aborted = true;
    return out;
  }
  let data = null;
  try {
    data = await res.json();
  } catch (_) { /* empty or non-JSON body */ }
  return { ok: res.ok, status: res.status, data };
}

/**
 * fetchTrips({etag, signal}) -> {notModified:true, etag} | {etag, data}
 * Sends If-None-Match when an etag is known. Rejects with an Error that
 * carries .status (0 for network failure or abort) and .aborted on any
 * other outcome, so the caller can keep its previous data.
 */
export async function fetchTrips({ etag, signal } = {}) {
  const headers = { Accept: "application/json" };
  if (etag) headers["If-None-Match"] = etag;
  let res;
  try {
    res = await fetch("api/trips", { headers, credentials: "same-origin", signal });
  } catch (err) {
    const e = new Error("network");
    e.status = 0;
    e.aborted = !!(err && err.name === "AbortError");
    throw e;
  }
  if (res.status === 304) return { notModified: true, etag: res.headers.get("ETag") || etag || null };
  if (!res.ok) {
    const e = new Error("http " + res.status);
    e.status = res.status;
    throw e;
  }
  let data;
  try {
    data = await res.json();
  } catch (_) {
    const e = new Error("bad json");
    e.status = res.status;
    throw e;
  }
  if (!data || !Array.isArray(data.trips)) {
    const e = new Error("bad payload");
    e.status = res.status;
    throw e;
  }
  return { etag: res.headers.get("ETag") || null, data };
}
