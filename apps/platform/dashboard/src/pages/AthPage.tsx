import type { JSX } from "solid-js";

import { AthQueue } from "../components/reviews/AthQueue";

export function AthPage(): JSX.Element {
  return (
    <section class="page active" data-page="ath">
      <AthQueue />
    </section>
  );
}
