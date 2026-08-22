# Turning answers into a document outline

Once the pre-flight questions (see `../SKILL.md`) are answered, build the `sections` array
for `scripts/generate_docx.py` using these conventions rather than inventing structure ad hoc.

## Shape by document type

The `doc_type` answer changes the expected section list — don't use one generic outline for
everything:

- **Report**: Executive Summary → Background → Findings/Analysis → Recommendations → Conclusion
- **Proposal**: Overview → Problem → Proposed Solution → Timeline/Scope → Cost/Ask → Next Steps
- **Memo**: Purpose → Summary → Details → Action Items *(short — usually 3-5 short paragraphs
  total, not one per section)*
- **Whitepaper**: Introduction → Problem Space → Approach → Evidence/Data → Implications →
  Conclusion
- **Meeting notes**: Attendees/Context → Discussion Points (one section per topic) → Decisions
  → Action Items
- **Letter**: no section headings at all — a single flowing body, opening/closing lines styled
  as plain paragraphs, not headings

If the user named specific sections in the pre-flight answers, those take precedence over the
`doc_type` default — use their list, in their order, and don't drop any to fit a template.

## Paragraph length and count

Body paragraphs are real paragraphs (2-6 sentences), not bullet fragments — this generates a
document, not a slide deck. Match `length` to paragraph count per section: a "1-page" doc is
roughly 1 short paragraph per section; a "3-5 page" report is 2-4 paragraphs per section.
Default to 1-2 solid paragraphs per section if no length was given.

## Tone in the copy, not just the style

Tone should shape the actual prose, not only the heading color:
- **Formal**: complete sentences, no contractions, third person where natural.
- **Casual**: contractions fine, direct address ("you"), shorter sentences.
- **Persuasive**: lead each section with the claim/benefit, back it with the specifics after.
- **Technical**: precise terms and numbers over adjectives; define jargon on first use.
- **Inspirational**: forward-looking framing, vision before mechanics.
