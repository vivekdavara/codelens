# The eval set

Fourteen small pull requests with 23 seeded bugs, and two clean pull requests with none, for measuring
CodeLens's precision and recall. `codelens eval` runs them (see the main README, "Results").

## What a case is

```text
cases/<name>/before/...   the files the PR changes, as they were
cases/<name>/after/...    the same files after the PR (the static pre-pass reads these)
cases/<name>/pr.diff      `git diff -M` of the two, made by scripts/build_evals.py with real git
cases/<name>/labels.json  {"summary": ..., "bugs": [{"path", "quote", "category", "why", "also"?}]}
```

A label names its line by quoting it: the quote must be on exactly one line of `after/<path>`, and that line
must be one the diff shows, usually an added line. Two labels sit on unchanged lines (`async-jobs`): making
a method `async` makes its unchanged `time.sleep` and `self.flush()` wrong. `also` lists other lines a
reviewer could fairly cite for the same bug, such as the `return` that ignores a computed discount.
`codelens.evals.load_case` checks all of this, and `tests/test_eval_cases.py` checks that every diff still
turns `before/` into `after/` and that no diff contains the word "bug".

## How the cases were made

By hand, for this repository, on 2026-10-10. Each case is a realistic change (a feature, a refactor, new
tests) with one or two bugs of a kind that turns up in real code review: crashes on a missing key, off-by-one
pagination, a blocking call in async code, mutation during iteration, SQL and shell injection, path
traversal, timing-unsafe secret comparison, tests that can't fail. The code carries no comment or name that
hints at a bug, because the model sees the diff.

**Caveat.** The static rules and these cases were written by the same author on the same day, and about
half of the bugs are of a kind a static rule targets. So the static pre-pass's recall here is an upper bound
for these bug classes, not a measurement on real pull requests. The clean cases and the false-positive count
are the more transferable numbers; the main README also reports how often each rule fires on mature code.

## The cases

| Case | The PR | Seeded bugs (where, category: what) |
|---|---|---|
| `async-jobs` | Moves the job runner to asyncio. | `worker/jobs.py:17` performance: time.sleep in a coroutine blocks the event loop; use await asyncio.sleep.<br>`worker/jobs.py:19` bug: flush is now a coroutine function: without await it never runs, so finished jobs are never saved. |
| `clean-median` | Adds a median to the stats module (no seeded bugs). | none (clean PR) |
| `clean-slugify` | Extracts slugify into its own function and tests it (no seeded bugs). | none (clean PR) |
| `discount-codes` | Adds discount codes to order totals. | `shop/pricing.py:14` bug: An unknown or mistyped code raises KeyError instead of being rejected.<br>`shop/pricing.py:15` bug: The discounted amount is computed and never used: the total ignores the code. |
| `event-bus` | Adds one-shot subscriptions to the event bus. | `events/bus.py:18` bug: The default list is shared by every call, and subscribe appends to it.<br>`events/bus.py:29` bug: Removing from the list being iterated skips the next subscription. |
| `file-download` | Adds thumbnail generation for uploaded files. | `files/store.py:12` security: A name like ../../etc/passwd escapes UPLOAD_DIR (path traversal).<br>`files/store.py:19` security: A file name with ; or $( ) in it runs commands through the shell. |
| `http-retry` | Adds retries with backoff to the HTTP client. | `net/client.py:18` bug: The sleep comes after continue, so retries never back off.<br>`net/client.py:19` bug: After the last attempt the timeout is swallowed and None returned to callers expecting bytes. |
| `invoice-tax` | Adds tax and currency parsing to invoices. | `billing/invoice.py:18` bug: Rounds the tax to whole dollars: up to 50 cents wrong on every invoice.<br>`billing/invoice.py:24` bug: `ValueError or TypeError` is just ValueError: a None input raises AttributeError, not caught. |
| `pagination` | Adds page-based listing to the orders API. | `api/orders.py:15` bug: Floor division drops the last, partial page (and 0 pages for fewer than `size` orders).<br>`api/orders.py:22` bug: Each page returns size + 1 orders, repeating the last one on the next page. |
| `parser-tests` | Adds tests for the config parser. | `tests/test_parse.py:8` bug: An assert on a non-empty tuple always passes: the test checks nothing.<br>`tests/test_parse.py:12` test: Any exception passes, a NameError included; expect ValueError. |
| `shipping-email` | Sends a shipping notification email. | `notify/email.py:16` bug: `or "delivered"` is always true, so every status sends the email.<br>`notify/email.py:17` bug: Missing f prefix: the subject shows {order_id} literally. |
| `token-check` | Adds API token checks to the webhook endpoint. | `web/hooks.py:10` security: == on a secret leaks timing; use hmac.compare_digest. |
| `ttl-cache` | Adds expiry to the in-memory cache. | `cache/memory.py:21` bug: A missing key gives entry None, and _expired(None) raises AttributeError.<br>`cache/memory.py:32` bug: Deleting from the dict while iterating it raises RuntimeError. |
| `user-lookup` | Adds lookup by email and password checks to the user store. | `db/users.py:16` security: The email is formatted into the SQL: injection. Use a parameter.<br>`db/users.py:21` security: Unsalted MD5 is not a password hash; use a slow KDF (scrypt, argon2). |

## Changing a case

Edit `before/` or `after/`, run `.venv/bin/python scripts/build_evals.py`, and check `git diff` on the
`pr.diff` files. A changed diff changes the case's prompt, so its recording (if any) must be made again.
