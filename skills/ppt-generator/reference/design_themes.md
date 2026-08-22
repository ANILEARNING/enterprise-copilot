# Design themes

Five built-in themes (`scripts/theme_presets.py`). Pick one from the user's "design" answer;
if they didn't express a preference, derive it from their "tone" answer via the mapping below.

| Theme | Feel | Background | Title | Accent | Font |
|---|---|---|---|---|---|
| `minimal` | Clean, neutral, technical | white `#FFFFFF` | near-black `#1A1A1A` | blue `#2F6FED` | Calibri |
| `corporate` | Formal, trustworthy | pale slate `#F4F6FA` | navy `#0B2545` | steel blue `#13315C` | Georgia |
| `vibrant` | Bold, persuasive, pitch-deck | deep violet `#1B1035` | white | coral `#FF6B6B` | Verdana |
| `dark` | Inspirational, high-contrast | near-black `#121212` | white | green `#4ADE80` | Calibri |
| `playful` | Casual, friendly | warm cream `#FFF7E6` | magenta `#D6336C` | amber `#FFB703` | Comic Sans MS |

## Tone → theme (used only when the user has no design preference)

| Tone | Theme |
|---|---|
| Formal | `corporate` |
| Casual | `playful` |
| Persuasive | `vibrant` |
| Technical | `minimal` |
| Inspirational | `dark` |

Always prefer an explicit design answer over this mapping — it's a fallback, not a rule to
override a stated preference. If the user names a theme not in the table (e.g. "make it look
like Apple's keynote slides"), don't force-fit one of the five: pick the closest match and say
so in your response, rather than silently substituting.
