# Always set a timeout on external network calls

**Rule.** Every outbound network request — HTTP, database, RPC, socket — must
be given an explicit timeout. A call with no timeout can hang forever, and one
hung request pins a worker, exhausts the connection pool, and cascades into an
outage that looks like a total failure of an unrelated service.

## Why

The default for most HTTP clients is *no timeout*. `requests.get(url)` with no
`timeout=` argument will wait indefinitely if the peer accepts the connection
and then stops responding. Under load, a single slow upstream turns into every
worker blocked on that upstream, and the process stops serving traffic it could
otherwise handle. The failure is remote, silent, and total.

## What to do instead

- Pass an explicit `timeout=` on every request: `requests.get(url, timeout=5)`.
- Prefer a connect/read timeout pair over a single number when the client
  supports it, so a slow-to-accept peer and a slow-to-respond peer are handled
  distinctly.
- Choose a bound that fits the call's budget, not an arbitrary large number; an
  external call inside a user request should time out well inside the request's
  own deadline.
- Decide what happens on timeout (retry with backoff, fail the request, serve a
  cached value) — a timeout is only useful if the caller handles it.

## Applies to

Any diff that adds or edits an outbound call — `requests.get`, `requests.post`,
`httpx`, `urllib`, a database query, an RPC stub — without a `timeout` argument.
If a network call can hang unbounded, this rule would have changed the code.
