/** Public per-wave retest medians, including an agent outside the current board. */
export function confirmationSampleComposites(
  boardSamples: number[] | null | undefined,
  pipelineSamples: number[] | null | undefined,
): number[] {
  return (boardSamples?.length ? boardSamples : pipelineSamples || []).filter(Number.isFinite);
}
