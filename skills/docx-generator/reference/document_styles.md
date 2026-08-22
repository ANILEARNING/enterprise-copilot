# Document styles

Five built-in styles (`scripts/style_presets.py`). Pick one from the user's "style" answer;
if they didn't express a preference, derive it from their "tone" answer via the mapping below.

Unlike `ppt-generator`'s themes, none of these use a colored page background — a document is
meant to be read or printed, so styling here is typography (fonts, heading color, an accent
rule under the title) on a plain white page, not a full-bleed color scheme.

| Style | Feel | Heading font/color | Body font | Accent |
|---|---|---|---|---|
| `minimal` | Clean, technical | Calibri, near-black `#1A1A1A` | Calibri | blue `#2F6FED` |
| `corporate` | Formal report | Georgia, navy `#0B2545` | Calibri | steel blue `#13315C` |
| `academic` | Essay, thoughtful | Cambria, dark brown `#3B2F2F` | Cambria | tan `#7A5C3E` |
| `modern` | Casual, clean | Verdana, near-black `#0F172A` | Calibri | sky blue `#0EA5E9` |
| `editorial` | Persuasive, magazine-like | Georgia, deep red `#7A0C2E` | Georgia | crimson `#C81D4A` |

## Tone → style (used only when the user has no style preference)

| Tone | Style |
|---|---|
| Formal | `corporate` |
| Casual | `modern` |
| Persuasive | `editorial` |
| Technical | `minimal` |
| Inspirational | `academic` |

Always prefer an explicit style answer over this mapping — it's a fallback, not a rule to
override a stated preference. If the user names something outside the five built-in styles
(e.g. "make it look like a legal brief"), pick the closest match and say so, rather than
silently substituting.
