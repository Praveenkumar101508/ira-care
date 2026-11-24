# IRA CARE design system

One look across the dashboard, the phone app and the browser watch. The
values below are the source of truth; the dashboard reads them from
`dashboard/src/app/globals.css` and the phone from
`mobile/src/appearance/theme.ts`. A change is made in both or not at all; the
phone's `appearance.test.ts` and the dashboard's build keep them honest.

## Colours

| Token | Light | Dark | Meaning |
|---|---|---|---|
| bg | `#FAF8F4` | `#14181B` | page background (warm off-white, not pure white) |
| card | `#FFFFFF` | `#1D2327` | a card or a field |
| text | `#1F2A24` | `#EEF1F0` | body text (contrast 14:1 / 13:1 on card) |
| muted | `#5B6B63` | `#A6B3AC` | secondary text (contrast ≥ 5.5:1) |
| border | `#E1DED8` | `#2C343A` | hairlines |
| notice / accent | `#2F5D8A` | `#8AB6E0` for text, `#2F5D8A` for fills | links, selected tab, ordinary actions |
| ok | `#2E7D32` | `#7BC67E` text, `#2E7D32` fills | all well, I'm OK, confirmed |
| urgent | `#B35C00` | `#F0A868` text, `#B35C00` fills | needs attention soon |
| help / sos | `#B3261E` | `#D32F2F` | Help, SOS, escalation (white text ≥ 4.5:1 on both) |

The phone can put a photo or a gradient behind the cards; the cards keep the
`card` colour and a scrim keeps text contrast. The dashboard follows the
system's light or dark setting; the phone follows the person's own setting,
synced across their devices (phase 10).

## Type

System font (San Francisco, Roboto, Segoe UI). Body 16 px, line height 1.45.
Headings 18–24 px. The phone applies the person's text scale (0.85–1.6) to
everything; the dashboard respects the browser's zoom. Large, plain words:
"Called first", not "tier 1"; "Care server", not "API base URL".

## Shape and spacing

Radius 12 px on cards, 10 px on buttons and fields, 999 px on pills and tabs.
Spacing in steps of 4 px; 12 px between cards; 16–20 px page padding. Every
tap or click target is at least 40 × 40 px (44 on the watch).

## Components

- **Card**: card colour, hairline border, radius 12.
- **Pill**: a state in one word, filled with its meaning's colour, white text.
- **Button**: filled, white text, colour by meaning (notice by default; ok;
  sos; grey for secondary). Disabled at 50 %.
- **Tabs**: pill shaped, selected one filled with notice.
- **Advanced disclosure**: anything a carer rarely needs (the server's
  address) sits behind "Advanced", closed by default.
- **Watch face** (`/watch`): black round screen inside a 46 mm dark case with a
  red Help button, the words IRA says in the centre, I'm OK (ok) and Help
  (sos) below, Talk to IRA when nothing is being asked.

## Words a screen never shows

API, token, raw identifiers, "tier N", JSON, inject, simulator, "Demo Person".
`dashboard/src/__tests__/no_developer_words.test.ts` and
`mobile/src/__tests__/no_developer_words.test.ts` fail the build if one
appears in a visible string. Presenter controls live on `/presenter`, which is
never shown in a recording.

## Accessibility

Visible focus ring everywhere, skip link, reduced motion honoured, more
contrast honoured (muted darkens, borders strengthen), every field labelled,
headings marked as headings, live regions for what the watch says.
