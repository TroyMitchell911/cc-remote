import type {
  ClaudeProfileInfo, CodexProfileInfo, Engine, Space,
} from "./protocol";
import { codexProfilePresentation } from "./codex-profile-presentation";

export function newWorkProfileForSidebarFilter(
  _engine: Engine,
  space: Space,
  profileFilter: string,
): string | undefined {
  return space === "work" && profileFilter !== "all"
    ? profileFilter
    : undefined;
}

export function resolveWorkScheduleProfile(
  profiles: Array<ClaudeProfileInfo | CodexProfileInfo>,
  selectedProfileId: string | null,
  preferredProfileId: string | null,
): { profileId: string | null; missing: boolean } {
  const profileId = selectedProfileId ?? preferredProfileId;
  return {
    profileId,
    missing: !!profileId
      && !profiles.some((profile) => profile.id === profileId),
  };
}

export function workProfileDisplayName(
  profiles: Array<ClaudeProfileInfo | CodexProfileInfo>,
  defaultProfileId: string | null | undefined,
  profileId: string | null | undefined,
): string {
  if (!profileId) return "未绑定账号";
  const profile = profiles.find((candidate) => candidate.id === profileId);
  if (!profile) return "账号已移除";
  return codexProfilePresentation(
    profiles, defaultProfileId, profileId,
  )?.fullLabel ?? profile.label;
}
