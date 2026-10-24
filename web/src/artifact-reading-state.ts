import { createContext } from "react";

export interface ArtifactReadingState {
  mode: "preview" | "source";
  page: number;
  top: number;
  left: number;
  sourceTop: number;
}

/** Owned by App, discarded at the device boundary; never persist file bytes. */
export const ArtifactReadingMemory = createContext<Map<string, ArtifactReadingState> | null>(null);

export function rememberArtifactReading(
  memory: Map<string, ArtifactReadingState> | null,
  key: string, patch: Partial<ArtifactReadingState>,
) {
  if (!memory) return;
  const previous = memory.get(key) ?? { mode: "preview", page: 0, top: 0, left: 0, sourceTop: 0 };
  memory.delete(key);
  memory.set(key, { ...previous, ...patch });
  while (memory.size > 16) memory.delete(memory.keys().next().value!);
}
