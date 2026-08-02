// The ATH reviews page (monolith section 2910–2938): the public queue of
// held high-score submissions — scores preserved, emissions paused.
import type { JSX } from "solid-js";

import { AthQueue } from "../components/reviews/AthQueue";

export function ReviewsPage(): JSX.Element {
  return (
    <section class="page active" data-page="reviews">
      <AthQueue />
    </section>
  );
}
