# Design themes

Ten built-in palettes (`scripts/pptx_themes.py`). Pick one from the user's
"design" answer; if they didn't express a preference, derive it from their
"tone" answer via the mapping below.

| Palette | Feel | Dominant | Accent | Font |
|---|---|---|---|---|
| `midnight_executive` | Formal, boardroom | navy `#1E2761` | ice blue `#4A6FE3` | Georgia |
| `forest_moss` | Grounded, sustainable | forest `#2C5F2D` | olive `#6B8E23` | Calibri |
| `coral_energy` | Bold, persuasive, pitch-deck | navy `#2F3C7E` | coral `#F96167` | Verdana |
| `warm_terracotta` | Warm, human, editorial | terracotta `#B85042` | sage `#A7BEAE` | Georgia |
| `ocean_gradient` | Deep, technical, premium | midnight `#21295C` | teal `#1C7293` | Calibri |
| `charcoal_minimal` | Clean, neutral, technical | charcoal `#36454F` | black `#212121` | Calibri |
| `teal_trust` | Trustworthy, product-led | teal `#028090` | mint `#02C39A` | Calibri |
| `berry_cream` | Soft, approachable | berry `#6D2E46` | dusty rose `#A26769` | Georgia |
| `sage_calm` | Calm, wellness, understated | slate `#50808E` | eucalyptus `#69A297` | Calibri |
| `cherry_bold` | High-contrast, urgent, bold | cherry `#990011` | navy `#2F3C7E` | Verdana |

Every palette also carries a `chart_colors` ramp (used for chart series) and
a `card_tint` (a subtle background for `stat_callout` cards — never a
literal accent stripe).

## Tone → palette (used only when the user has no design preference)

| Tone | Palette |
|---|---|
| Formal | `midnight_executive` |
| Casual | `coral_energy` |
| Persuasive | `cherry_bold` |
| Technical | `charcoal_minimal` |
| Inspirational | `ocean_gradient` |

Always prefer an explicit design answer over this mapping — it's a
fallback, not a rule to override a stated preference. If the user names a
palette not in the table (e.g. "make it look like Apple's keynote slides"),
don't force-fit one of the ten: pick the closest match and say so in your
response, rather than silently substituting.

## Layout selection

Match the layout to what the content actually is, not habit:

| Content shape | Layout |
|---|---|
| A short list of related points | `bullets` |
| Exactly two things being compared (before/after, us/them, pros/cons) | `two_column` |
| 1-4 standalone numbers worth calling out on their own | `stat_callout` |
| A time series or category comparison the user gave real numbers for | `chart` |
| A pivot to a new topic/section within the deck | `section_divider` |
| Opening or closing the deck | the top-level `title` slide, or a closing `bullets`/`section_divider` slide |

Vary layout across the deck — never repeat the same one twice in a row if
the content supports something else.

## Avoid

- No color-bar or accent-stripe motif anywhere — no header/footer bars, no
  vertical sidebar stripes, no single-side card borders. These read as
  AI-generated filler. Set a card apart with a background tint or shadow,
  never an edge stripe.
- No accent line under a title.
- No cream/beige default background (`#F5F5DC`, `#FAF0E6`, `#FAEBD7`,
  `#FFF8E1`) — every palette here defaults to white content backgrounds;
  only title/section-divider slides go dark (the palette's `dominant`
  color).
- Don't center body text — left-align paragraphs and bullet lists; center
  only titles, subtitles, and section dividers.
- Don't fabricate chart numbers — a chart is only as trustworthy as its
  data; if the user didn't give real numbers, use `stat_callout` or
  `bullets` instead.
