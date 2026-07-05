# Prefer f-strings for string formatting

**Rule.** In Python 3.6+, format strings with f-strings rather than
`str.format()`, `%`-formatting, or `+` concatenation. F-strings put the value
next to the placeholder, so the reader sees at a glance what is interpolated
where, and they are faster than the alternatives.

## Why

Compare the same log message three ways:

```python
msg = "fetched %d rows in %s ms" % (rows, elapsed)          # %-style
msg = "fetched {} rows in {} ms".format(rows, elapsed)       # str.format
msg = f"fetched {rows} rows in {elapsed} ms"                 # f-string
```

The f-string is the shortest and the only one where the variable names appear
inline with the text, so a reader does not have to count positional arguments
to know what fills each slot. Positional `%` and `.format()` calls drift out of
sync with their arguments as code is edited; f-strings cannot.

## What to do instead

- Use an f-string whenever you interpolate a value into a string literal.
- Keep expressions inside the braces short; if the expression is complex, name
  it on a prior line and interpolate the name.
- Reserve `%`-style and `.format()` for the rare case where the template is not
  known at the call site (for example a logging format loaded from config).

## Applies to

Any diff that builds a string by concatenation or `.format()` / `%` where an
f-string would read better. This is a style rule: it improves readability but
does not change behavior, so a diff that already uses f-strings throughout has
nothing for it to fix.
