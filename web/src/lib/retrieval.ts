// Browser round trip through response decoding, not database execution time.
export async function measureRetrieval<T>(load: () => Promise<T>) {
  const start = performance.now();
  const data = await load();
  return { data, retrievalMs: performance.now() - start };
}
