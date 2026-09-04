# Decision journal — account C

One `YYYY-MM-DD.jsonl` per day, one line per cycle, plus a `-analysis.log` rendering of
the same cycles for a person to read. Together they are the record of every decision the
agent made: what it observed, which gate ended each candidate's candidacy, what the news
classifier returned, what it submitted, and what filled.

`state.json` is deliberately **not** shipped. It is live runtime state — the open-structure
registry and the equity high-water mark — tied to one specific account's positions at one
moment. A fresh clone should start with an empty registry and build its own; shipping ours
would make your first `make preflight-c` fail on legs that are in our account and not yours.
