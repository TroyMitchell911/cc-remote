import type { ClaudeProfileInfo, CodexProfileInfo } from "./protocol";

const SECONDARY_NAMES = [
  "nyx",
  "iris",
  "echo",
  "gaia",
  "metis",
  "themis",
  "hestia",
  "athena",
  "hermes",
  "atlas",
  "orpheus",
  "asteria",
] as const;

export interface CodexProfilePresentation {
  name: string;
  label: string;
  fullLabel: string;
  tone: number;
}

type AccountProfileInfo = ClaudeProfileInfo | CodexProfileInfo;

/** Resolve account ownership from the routing id while a fresh/forked row is
 * still waiting for the authoritative catalog. Multi-profile wire ids
 * are always ``profile@native``; falling back to the default during this gap
 * would paint the wrong account's model and capability catalogs. */
export function codexProfileIdForSession(
  sessionId: string | null | undefined,
  defaultProfileId: string | null | undefined,
): string | null {
  if (!sessionId) return defaultProfileId ?? null;
  const separator = sessionId.indexOf("@");
  return separator > 0
    ? sessionId.slice(0, separator)
    : defaultProfileId ?? null;
}

export const claudeProfileIdForSession = codexProfileIdForSession;

export function codexProfilePresentation(
  profiles: readonly AccountProfileInfo[],
  defaultProfileId: string | null | undefined,
  profileId: string | null | undefined,
): CodexProfilePresentation | null {
  if (profiles.length <= 1 || !profileId) return null;
  const profile = profiles.find((candidate) => candidate.id === profileId);
  if (!profile) return null;
  const isDefault = profile.id === defaultProfileId;
  const secondaryIndex = profiles
    .filter((candidate) => candidate.id !== defaultProfileId)
    .findIndex((candidate) => candidate.id === profile.id);
  const name = isDefault
    ? "default"
    : SECONDARY_NAMES[secondaryIndex] ?? "more";
  return {
    name,
    label: profile.label,
    fullLabel: profile.label.toLowerCase() === name
      ? name
      : `${name} · ${profile.label}`,
    tone: isDefault ? 0 : (secondaryIndex % 7) + 1,
  };
}

export const claudeProfilePresentation = codexProfilePresentation;
