---
name: SN118 Backroom funding surface
description: Incumbent operations-console design, scoped to Gamma funding history
colors:
  ink: "#0b0d0c"
  panel: "#131714"
  line: "#303631"
  muted-strong: "#c6cec9"
  acid: "#ccf56a"
  red: "#ff8178"
typography:
  body:
    fontFamily: 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
  title:
    fontSize: "18px"
    fontWeight: 600
rounded:
  panel: "12px"
  control: "8px"
spacing:
  panel: "20px"
  panel-wide: "24px"
---

# Design System: SN118 Backroom funding surface

## Overview

**Creative North Star: "Operate"**

Preserve the incumbent dark operations console. Funding records prioritize legible quantities, explicit provenance, and source inspection. This is a scoped description of the implemented Gamma addition, not a replacement identity for the monorepo.

**Key Characteristics:**
- Dark surfaces with visible borders.
- Compact labels and tabular numeric amounts.
- Expandable provenance beside linked source records.

## Colors

### Primary
Acid green identifies keyboard focus. Red identifies refresh and loading errors.

### Neutral
Ink supplies the page background; panel supplies the funding card; line separates rows and controls; muted-strong carries explanations and reference text. These values come from `src/styles.css`.

## Typography

Use the incumbent Inter/system stack. Section titles use the title role. Funding amounts use tabular numerals; secondary details use smaller text. Preserve complete exact decimal amounts rather than shortening them for layout.

## Layout

The funding card uses panel spacing, increasing to panel-wide from the small breakpoint. Its summary is a single-column grid that becomes three columns. Keep the table at a minimum width of 760px inside its own horizontal scroller. The small-screen scroll hint remains visible below 640px. The region is named, focusable, and connected to that hint. Route sections remain vertically stacked.

## Elevation & Depth

Panels and table rows use tonal surfaces and borders. Keyboard focus uses the existing acid treatment; the table region adds an acid outline. Funding content introduces no decorative shadow.

## Shapes

The funding card uses the panel radius. Its refresh control uses the control radius and a minimum height of 40px. Preserve these shapes alongside the existing manual-transfer panel.

## Components

The refresh control exposes a busy label and disabled state. Error text uses an alert. The empty state explains what reconciliation adds. Table column headers and its hidden caption name the data. Native disclosure controls reveal operator provenance without widening every row.

## Do's and Don'ts

- Do preserve visible precision and source links.
- Do keep mobile overflow inside the named table region.
- Do expose the difference between operator correlation and provider attestation.
- Don't present historical additions as spendable balance.
- Don't imply that this history enables automatic top-ups.
