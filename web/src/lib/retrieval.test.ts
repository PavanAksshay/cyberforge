import { afterEach, describe, expect, it, vi } from "vitest";
import { measureRetrieval } from "./retrieval";

afterEach(() => vi.restoreAllMocks());
describe("retrieval duration", () => {
  it("measures through response decoding and retains the returned data", async () => {
    const clock = vi
      .spyOn(performance, "now")
      .mockReturnValueOnce(100)
      .mockReturnValueOnce(142.75);
    let finish!: (data: object) => void;
    const pending = measureRetrieval(
      () =>
        new Promise<object>((resolve) => {
          finish = resolve;
        }),
    );
    expect(clock).toHaveBeenCalledTimes(1);
    finish({ buckets: [] });
    expect(await pending).toEqual({
      data: { buckets: [] },
      retrievalMs: 42.75,
    });
  });
  it("does not present a failed request as a successful retrieval", async () => {
    await expect(
      measureRetrieval(async () => {
        throw new Error("503");
      }),
    ).rejects.toThrow("503");
  });
});
