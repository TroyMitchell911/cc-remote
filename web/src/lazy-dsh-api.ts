import type { DshApi, DshApiClient } from "./dsh-api";
import type { RelayWs } from "./ws";
import type { ServerEvent } from "./protocol";
export function lazyDshApi(transport: () => RelayWs | null): DshApiClient & { reset(): void; accept(event: ServerEvent): boolean } {
  let current: DshApi | undefined, generation = 0;
  const get = async () => {
    const before = generation, ws = transport();
    const { DshApi } = await import("./dsh-api");
    if (before !== generation || ws !== transport()) throw new Error("连接已切换，请重试。");
    return current ??= new DshApi(transport);
  };
  return {
    watchTerminal: (...args) => {
      let disposed = false, unsubscribe: (() => void) | undefined;
      void get().then(api => { if (!disposed) unsubscribe = api.watchTerminal(...args); })
        .catch(() => { if (!disposed) args[3](); });
      return () => { disposed = true; unsubscribe?.(); };
    },
    read: async (...args) => (await get()).read(...args),
    act: async (...args) => (await get()).act(...args),
    goal: async (...args) => (await get()).goal(...args),
    control: async (...args) => (await get()).control(...args),
    download: async (...args) => (await get()).download(...args),
    accept: event => current?.accept(event) ?? false,
    reset: () => { generation++; current?.reset(); current = undefined; },
  };
}
