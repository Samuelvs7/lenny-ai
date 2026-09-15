# HTML artifact skill

Produce a single, complete, self-contained HTML document based on the
conversation and the supplied transcript passages.

## Output contract

- Output **only** HTML. No Markdown fences, no explanation before or after.
- Start with `<!DOCTYPE html>` and end with `</html>`.
- All CSS goes in one `<style>` block in the `<head>`. No external stylesheets.
- Give the document a meaningful `<title>`.

## What you may not use

The renderer runs this document inside a locked-down sandbox. These will be
stripped or blocked, so writing them wastes output and produces a broken page:

- `<script>` of any kind, and `on*` event attributes (`onclick`, `onload`, …).
- `javascript:` URLs.
- External resources: no `<img src="http…">`, no web fonts, no CDN links, no
  `fetch`. Network requests are blocked by policy.
- `<iframe>`, `<object>`, `<embed>`, `<form>`.

Build something that is complete and good *as static HTML and CSS*. That is the
constraint; work within it rather than around it.

## What makes a good artifact here

- A clear visual hierarchy: one `h1`, meaningful section headings, generous
  spacing. Never make everything the same weight.
- System font stack (`system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif`)
  — web fonts cannot load.
- Readable measure: cap body text around `70ch`.
- Responsive: it must work at 400px wide. Use flex/grid with wrapping, relative
  units, and no fixed widths wider than the viewport.
- Use CSS for any visual element you need — borders, gradients, shapes. No
  images are available.
- Restrained colour. Pick one accent and use it deliberately.

## Grounding

Any factual claim, quote, or statistic must come from the supplied passages.
Attribute quotes to the person who said them and name the episode. Do not
invent numbers, quotes, or sources. If you need a fact the passages do not
contain, leave it out.
