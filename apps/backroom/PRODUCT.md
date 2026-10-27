# Public SN118 Backroom

This application is the SN118 operations console and MCP server at `backroom.dittobench.ai`. It calls Platform and public subnet surfaces. Private Ditto product operations belong to a separate application and repository.

## Gamma funding history

The authenticated Gamma route lets an operator inspect signed emission policy, proposed settings, manual collector transfers, and historical GM funding. The funding history extends the existing operation surface; it does not grant spending authority.

The chosen direction is **Operate**: show linked DITTO sales → TAO payments → completed GM credit additions with inspectable references and operator provenance. Purchase time, operator, and recording time explain the reconciliation. Matching by time and recipient is operator reconciliation, not a GM transaction attestation or proof that a payment came entirely from one preceding sale. Existing TAO and fees can affect balances.

Amounts retain atomic precision through the displayed decimal representation; credited USD uses cents. Repeated preceding-sale references carry “Shared sale · counted once.” Historical credit additions remain distinct from the current spendable GM balance. Automatic top-ups are off. Collector transfers perform only the move to a holding wallet; later sales, payments, and credits require separate reconciliation.

## Reviewable states

The component renders initial records, a refresh busy state, refresh failure, an empty explanation, and a warning when only the latest 500 records are returned. Totals cover returned records. Movement references link to the Bittensor explorer. Reconciliation details expand in place.

On narrow screens the table retains its columns in a horizontally scrollable, keyboard-focusable named region, with a visible scroll instruction. Review captures are stored at repository `.impeccable/review/desktop.png` and `.impeccable/review/mobile.png`; captures are local presentation evidence, not live transaction or production verification.

## Source authority

Scope: `AGENTS.md`. Behavior: `src/components/TreasuryFundingHistory.tsx`, `src/routes/_authenticated/gamma.tsx`, `src/lib/treasury-funding.schemas.ts`. Visual precedent: `src/styles.css` and `src/components/TreasuryManualTransferPanel.tsx`.
