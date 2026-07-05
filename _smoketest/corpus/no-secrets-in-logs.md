# Never log secrets, keys, or tokens

**Rule.** Do not write API keys, access tokens, passwords, or any other secret
value to a log line, a print statement, or an error message. Logs are copied
into aggregators, tickets, and screenshots; a secret in a log is a secret that
has leaked.

## Why

A log statement feels ephemeral but is not. The line

```python
logger.info("calling upstream with api key " + api_key)
```

ends up in stdout, in the log aggregator, in a support ticket a customer
pastes, and in the terminal scrollback of whoever was watching. Every one of
those is an uncontrolled copy of the credential. Rotating the key is the only
remediation, and rotation is expensive and often forgotten.

## What to do instead

- Never interpolate a secret into a log message, even at debug level.
- If you must confirm *which* credential is in use, log a stable fingerprint
  (for example the last four characters, or a salted hash), never the value.
- Redact the secret before the log call, not after: `key=***redacted***`.
- Treat the request URL, headers, and body the same way — an `Authorization`
  header or a `token=` query parameter is a secret too.

## Applies to

Any diff that adds or edits a `log`, `print`, `console.log`, or exception
message that references a variable named `key`, `token`, `secret`, `password`,
`credential`, or `authorization`. If a secret reaches a log sink, this rule
would have changed the code.
