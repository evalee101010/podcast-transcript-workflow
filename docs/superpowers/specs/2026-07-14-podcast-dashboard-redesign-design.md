# Podcast Dashboard Redesign

## Goal

Restyle the podcast dashboard so it feels like the same product as the approved
peach podcast icon while preserving the current compact, work-focused layout.
The redesign changes presentation and static assets only. Subscription loading,
search, filtering, generation jobs, polling, document navigation, and API data
contracts remain unchanged.

## Approved Direction

The selected direction is the warm "Soft Editorial Desk" theme with the
following refinements approved during visual review:

- Keep the original system sans-serif typography and dense table structure.
- Keep horizontal row dividers instead of turning episodes into cards.
- Use warm gray, white, and pale peach surfaces rather than a one-color peach UI.
- Keep each podcast's real cover art in the subscription list.
- Use a coral curved marker on the left edge of the selected subscription.
- Add a thin full-width image banner with a translucent readability overlay.
- Use the peach podcast icon and a warmer product name in the banner.

The dashboard remains an operational tool. Decorative treatment is concentrated
in the banner and selected navigation state, while the episode table stays quiet
and optimized for scanning.

## Layout

### Desktop

- The app fills the viewport and uses two rows: a 108 px banner and the remaining
  workspace.
- The banner spans the full application width.
- The workspace retains a 320 px subscription sidebar and a flexible main pane.
- The main pane keeps the existing 68 px toolbar followed by a scrollable episode
  table.
- Table and sidebar dimensions remain stable when counts, labels, loading states,
  or hover styles change.

### Narrow Screens

- At 860 px and below, retain the existing stacked layout: subscriptions above
  the episode list.
- Reduce the banner to 88 px, reduce the brand mark to 36 px, and hide the total
  episode summary before allowing controls to overlap.
- Toolbar controls may wrap into a second row, but the search, status filter, and
  refresh command must remain available.
- The table may scroll horizontally when needed; titles must not be squeezed
  below a usable reading width.

## Banner

### Asset

- Store the replaceable image as `web_static/assets/podcast-banner.png`.
- The initial placeholder is the approved peach studio image supplied by the
  user.
- Recommended production size: 2400 x 400 px (6:1).
- Recommended high-resolution master: 2880 x 480 px (6:1).
- Keep important subjects inside the middle 70 percent safe area so responsive
  cropping does not remove them.
- Replacing the banner later should require overwriting this file with the same
  filename, not editing HTML or CSS.

### Display

- Use `background-size: cover` and initial positioning of `center 57%`.
- Apply two translucent overlays above the image:
  - Horizontal: dark cocoa at the left, lightest near the center, and moderately
    dark at the right to protect both the brand and settings control.
  - Vertical: nearly transparent near the top and gently darkened at the bottom.
- The initial overlay values are:
  `linear-gradient(90deg, rgba(69,42,34,.74) 0%, rgba(83,52,43,.28) 46%, rgba(69,42,34,.46) 100%)`
  and
  `linear-gradient(180deg, rgba(62,39,33,.04) 25%, rgba(62,39,33,.42) 100%)`.
- If the image cannot load, a warm coral fallback color and the gradients must
  still keep banner text readable.

### Banner Content

- Left: the approved peach podcast icon followed by "桃子播客工作台".
- Right: total episode count and the existing settings menu.
- The icon is 40 px on desktop. It uses an empty `alt` because the adjacent brand
  text provides the accessible name.
- Banner text is warm white with a restrained text shadow. Settings uses a
  translucent cocoa surface and visible focus state.

## Visual Tokens

Use CSS custom properties so the palette can be adjusted without hunting through
individual components.

| Role | Value | Usage |
| --- | --- | --- |
| Workspace background | `#f6f3f0` | Main page behind the table |
| Panel | `#ffffff` | Toolbar and episode rows |
| Sidebar | `#f3e6dd` | Subscription navigation |
| Table header | `#f0e7e2` | Sticky column headings |
| Border | `#e4ded9` | Table and row dividers |
| Hover | `#fff8f4` | Episode row hover |
| Selected surface | `#fff8f3` | Active subscription |
| Primary text | `#252724` | Titles and controls |
| Muted text | `#7e7770` | Program names and metadata |
| Coral | `#cf7562` | Main action and focus treatment |
| Coral marker | `#df846f` | Selected subscription curve |
| Pale coral | `#fde8df` | Pending generation action |
| Cocoa | `#493a35` | Completed/readable action |
| Error | `#b42318` | Failure and retry states |

The UI must not add gradients, peach fills, or rounded cards outside the approved
banner overlay and functional state treatments.

## Typography And Table

- Continue using the local system stack: `-apple-system`, `BlinkMacSystemFont`,
  `Segoe UI`, `PingFang SC`, and `Microsoft YaHei`.
- Do not load a remote font and do not use a serif display face.
- Retain the current 15 px base size, compact row padding, program subtitle, and
  fixed date/action columns.
- Keep the table as one bordered surface with white rows and horizontal dividers.
- The header remains sticky and uses the pale peach-gray table-header token.
- Row hover changes only the background color and must not shift dimensions.

## Subscription Sidebar

- Preserve `avatar_url` images without tinting, filtering, or replacing them with
  generated initials.
- Keep circular 42 px podcast covers and `object-fit: cover`.
- Only the synthetic "ALL" source uses a text avatar: warm-white text on cocoa.
- The selected row uses the selected-surface color and a 5-6 px coral marker
  attached to the left edge. The marker has rounded outer corners, producing the
  approved small curved-line appearance.
- Selection must not recolor or obscure the podcast's own cover.
- Long podcast names continue to truncate with an ellipsis; counts remain fixed
  at the right edge.

## Actions And States

- Coral is reserved for the primary refresh/add action, focus treatment, and
  current-location emphasis.
- "生成" uses pale coral with dark coral text.
- "阅读版" uses cocoa with warm-white text.
- "原文" remains a quiet neutral button.
- "重试" remains red and visually distinct from normal generation.
- Generating states keep the existing progress behavior; the progress line uses
  coral instead of green.
- Hover, active, disabled, loading, and keyboard focus states must be visible
  without changing component dimensions.
- Existing labels and action behavior do not change in this redesign.

## Static Asset Delivery

The current server serves only fixed HTML and document routes. Implementation
will add a constrained `/assets/` route backed only by
`web_static/assets/`. It must:

- Support `GET` and `HEAD`.
- Resolve paths under the asset directory and reject traversal outside it.
- Return the correct content type for PNG assets.
- Use the existing no-store response behavior so a replaced banner appears after
  refresh without cache troubleshooting.

The browser-facing brand icon should be copied from the approved Tool Pantry
asset into `web_static/assets/`; the source master under `assets/tool-pantry/`
remains the canonical icon artwork.

## Accessibility

- Maintain semantic buttons, inputs, select elements, table headings, and menu
  roles already present in the page.
- Ensure banner text and controls remain legible over the brightest part of the
  placeholder image.
- Use a visible coral focus ring on interactive controls.
- Do not rely on color alone for episode states; the existing Chinese labels
  remain visible.
- Preserve keyboard opening, closing, and Escape behavior for the settings menu.
- Maintain at least 40 px touch targets for primary controls where the existing
  layout permits; compact table actions keep their current dimensions because
  this is a desktop-first operational surface.

## Error Handling

- A missing banner image falls back to the banner background color and overlays;
  the dashboard remains usable.
- A missing podcast avatar continues to use the existing first-character
  fallback.
- API and job errors keep their current messages and red error treatment.
- No UI failure may prevent the subscription list or episode table from
  rendering.

## Verification

Implementation verification will include:

- Existing Python tests, including static-index and web handler coverage.
- New handler tests for `GET` and `HEAD` asset delivery plus traversal rejection.
- Static-page assertions for the banner, approved brand name, and asset paths.
- Browser screenshots at 1440 x 900, 1280 x 720, and 390 x 844.
- Visual checks that the banner is 108 px on desktop, podcast covers remain
  recognizable, the selected marker has the approved curved shape, and table
  content does not overlap.
- Interaction checks for subscription selection, search, status filtering,
  refresh, settings, generation, loading, failure, readable, and original-link
  states.

## Out Of Scope

- No API schema or backend workflow changes beyond local static asset delivery.
- No user-upload control or banner picker in the settings menu.
- No change to subscription discovery, transcription, readable-document
  generation, Feishu publishing, or scheduling.
- No card-based episode redesign, remote font, avatar restyling, or animated
  banner.
- No personal information is added to the distributable project.

## Acceptance Criteria

- The dashboard clearly belongs to the peach podcast icon family without reading
  like a promotional landing page.
- The thin banner gives the brand a first-viewport presence while leaving the
  subscription and episode work surfaces dominant.
- Replacing `podcast-banner.png` with another 6:1 image updates the banner after a
  page refresh and requires no code edits.
- Podcast sources display their real cover art, and the active source is indicated
  by the coral curved marker without altering the cover.
- The original dense system-font table and horizontal divider style are retained.
- Existing dashboard workflows and states continue to work as before.
