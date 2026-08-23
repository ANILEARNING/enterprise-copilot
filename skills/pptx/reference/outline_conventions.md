# Turning answers into a slide outline

Once the pre-flight questions (see `../SKILL.md`) are answered, build the
`slides` array for `scripts/generate_pptx.py` using these conventions
rather than inventing structure ad hoc.

## Default shape

1. **Title slide** — not part of the `slides` array; comes from
   `spec.title` / `spec.subtitle`.
2. **Agenda/overview slide** — one `bullets` slide, 3-6 items naming the
   sections to come. Skip it only for a very short deck (≤ 4 content slides
   total) where an agenda would outnumber the content.
3. **Content slides** — one per topic/section the user named (or, if they
   gave none, one per natural subdivision of the topic). Pick the layout per
   `../reference/design_themes.md`'s content-shape table rather than
   defaulting every slide to `bullets`.
4. **Closing slide** — a `section_divider` or `bullets` summary/
   key-takeaways slide, matching the tone (e.g. "Questions?" for a
   technical/internal deck, a clear ask for a persuasive/pitch deck).

## Slide count

Map the user's stated `length` directly (`"5-8 slides"` → aim for 6-7
including title/closing). If they gave no preference, default to **6-8
total slides** — long enough to cover a topic, short enough to stay
skimmable.

## Must-include content

If the user named specific `sections`, each becomes its own content slide
in the order given — don't merge, reorder, or drop any of them to hit a
slide-count target; adjust bullets-per-slide instead.

## Layout variety (`layout_style`)

- **"Varied (mixed layouts)"** (default if unanswered): apply the
  content-shape table freely — mix `bullets`, `two_column`, `stat_callout`,
  `chart`, and `section_divider` as the content calls for each.
- **"Mostly bullets, occasional visual"**: default every content slide to
  `bullets`; use exactly one richer layout (a `stat_callout` or `chart`)
  only where the content is genuinely numeric.
- **"Bold/visual-heavy"**: minimize plain `bullets` slides — prefer
  `two_column`, `stat_callout`, and `chart` wherever the content plausibly
  supports it, and use `section_divider` between every major topic shift,
  not just once.

## Charts (`include_charts` / `chart_data`)

Only emit a `chart` slide when the user answered `chart_data` with real
numbers (or the topic itself supplies real numbers, e.g. from a document
the user shared). Never invent data to justify a chart — a `stat_callout`
or `bullets` slide is always the safer fallback for content that's
numeric-flavored but not a genuine series/comparison.

## Tone in the copy, not just the palette

Tone should show up in the bullet wording itself, not only the color
palette:

- **Formal**: complete phrases, no slang, third person.
- **Casual**: contractions okay, direct address ("you"), shorter sentences.
- **Persuasive**: lead each bullet with the benefit/outcome, not the
  feature.
- **Technical**: precise terms, numbers/specifics over adjectives.
- **Inspirational**: forward-looking language, vision-first framing.
