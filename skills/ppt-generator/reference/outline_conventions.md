# Turning answers into a slide outline

Once the pre-flight questions (see `../SKILL.md`) are answered, build the `slides` array for
`scripts/generate_ppt.py` using these conventions rather than inventing structure ad hoc.

## Default shape

1. **Title slide** — not part of the `slides` array; comes from `spec.title` / `spec.subtitle`.
2. **Agenda/overview slide** — one slide, 3-6 bullets naming the sections to come. Skip it only
   for a very short deck (≤ 4 content slides total) where an agenda would outnumber the content.
3. **Content slides** — one per topic/section the user named (or, if they gave none, one per
   natural subdivision of the topic). Each slide: a short title (≤ 6 words) + 3-5 bullets.
   Bullets are phrases, not paragraphs — if a point needs more than ~20 words, split it into two
   bullets or push detail to speaker notes instead of cramming the slide.
4. **Closing slide** — summary/key-takeaways or a call-to-action, matching the tone (e.g.
   "Questions?" for a technical/internal deck, a clear ask for a persuasive/pitch deck).

## Slide count

Map the user's stated length preference directly (`"5-8 slides"` → aim for 6-7 including title/
closing). If they gave no preference, default to **6-8 total slides** (title + agenda + 3-5
content + closing) — long enough to cover a topic, short enough to stay skimmable.

## Must-include content

If the user named specific sections/points in the pre-flight answers, each becomes its own
content slide in the order given — don't merge or reorder them without a reason, and don't
drop any of them to hit a slide-count target; adjust bullets-per-slide instead.

## Tone in the copy, not just the theme

Tone should show up in the bullet wording itself, not only the color theme:
- **Formal**: complete phrases, no slang, third person.
- **Casual**: contractions okay, direct address ("you"), shorter sentences.
- **Persuasive**: lead each bullet with the benefit/outcome, not the feature.
- **Technical**: precise terms, numbers/specifics over adjectives.
- **Inspirational**: forward-looking language, vision-first framing.
