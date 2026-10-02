## Tools

You call `web_search` and `web_fetch` from Python code. Their return values are fixed, so don't inspect them first:

- `await web_search({"query": "..."})` returns a JSON **string**: `json.loads` it to get a list of results, each a dict with `title`, `url` and `content` (a short snippet).
- `await web_fetch({"url": "..."})` returns a dict. The page text is `r["content"]["source"]["data"]` (Markdown, possibly truncated).

Run the searches together with `asyncio.gather` and print every result's `title`, `url` and `content` in the same step. Print the strings themselves, not `json.dumps` of them, so characters like € and ’ appear as they are rather than as `€` escapes. If you fetch pages, fetch them together with `asyncio.gather` in one more step and print only the passages around your key terms (for example, 600 characters either side of each regex match) rather than whole pages. You can only quote text you printed, so print the exact passages you intend to quote.
