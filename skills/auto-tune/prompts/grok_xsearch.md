# Grok X-search prompt

The Grok provider in `scripts/discover.py` sends this prompt with the role substituted in.
Edit the body below to tune the search; the substitution token is `{{ROLE}}`.

---

You have access to real-time X (Twitter) post data. Search X posts from the last 30 days that recommend or announce Claude Code skills, agents, sub-agents, or `.md` skill files useful for a **{{ROLE}}**.

For each relevant post you find, return ONLY a JSON array (no prose, no markdown fences) of objects with these keys:

- `name` — short identifier for the skill / agent (max 60 chars)
- `description` — one sentence summary (max 220 chars)
- `author_handle` — the X handle that posted it (with @)
- `x_post_url` — full URL to the X post
- `posted_date` — ISO date of the post
- `urls` — array of every github.com, gist.github.com, or raw.githubusercontent.com URL referenced anywhere in the post or thread

Only include posts where the author shares actual installable content (a repo link, a gist, a raw README). Skip posts that are only opinion or hype without artifacts. If you find fewer than 10 quality posts, return only the ones you found; do not fabricate.

Return `[]` (an empty array) if you have low confidence. Do not include any text outside the JSON.
