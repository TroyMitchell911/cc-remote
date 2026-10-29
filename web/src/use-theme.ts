import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from "react";
import {
  LEGACY_THEME_KEY, THEME_STORAGE_KEY, resolveThemeMode, themePalette,
  type ThemeChoice, type ThemeEngine,
} from "./themes";
import { APPEARANCE_STORAGE_KEY, appearanceColors, appearanceVariables, readAppearancePreferences,
  saveAppearanceChange, type AppearanceChange, type AppearanceLook, type SessionLayout } from "./appearance";

function browserStorage(): Storage | undefined {
  try { return typeof window === "undefined" ? undefined : window.localStorage; }
  catch { return undefined; }
}

export function useTheme(engine: ThemeEngine) {
  const [preferences, setPreferences] = useState(() => readAppearancePreferences(browserStorage()));
  const [systemDark, setSystemDark] = useState(() => typeof window !== "undefined"
    && typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  const preference = preferences[engine]!;
  const choice = preference.mode;
  const sessionLayout = preference.sessionLayout ?? "compact";
  const mode = resolveThemeMode(choice, systemDark);
  const look = preference[mode];
  const colors = appearanceColors(engine, mode, look);
  const variables = useMemo(() => appearanceVariables(engine, mode, look), [engine, mode, look]);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => setSystemDark(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const update = (event: StorageEvent) => {
      if (event.key === APPEARANCE_STORAGE_KEY || event.key === THEME_STORAGE_KEY || event.key === LEGACY_THEME_KEY || event.key === null) {
        setPreferences(readAppearancePreferences(browserStorage()));
      }
    };
    window.addEventListener("storage", update);
    return () => window.removeEventListener("storage", update);
  }, []);

  useLayoutEffect(() => {
    const root = document.documentElement;
    root.dataset.engine = engine;
    root.dataset.theme = mode;
    root.dataset.palette = look.preset ?? "classic";
    root.style.colorScheme = mode;
    for (const [name, value] of Object.entries(variables)) root.style.setProperty(name, value);
    const background = getComputedStyle(root).getPropertyValue("--bg").trim();
    document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]').forEach(meta => {
      meta.content = background;
    });
    return () => { for (const name of Object.keys(variables)) root.style.removeProperty(name); };
  }, [engine, look.preset, mode, variables]);

  const changeAppearance = useCallback((change: AppearanceChange) => {
    setPreferences(current => saveAppearanceChange(browserStorage(), current, engine, change));
  }, [engine]);
  const selectTheme = useCallback((next: ThemeChoice) => {
    // Write only on a user selection, never on mount or a system appearance change.
    const palette = themePalette(next);
    if (palette) {
      changeAppearance({ target: palette.mode, replace: { preset: palette.id } });
      changeAppearance({ mode: palette.mode });
    } else if (next === "system" || next === "light" || next === "dark") changeAppearance({ mode: next });
  }, [changeAppearance]);
  const updateLook = useCallback((patch: AppearanceLook) => changeAppearance({ target: mode, patch }), [changeAppearance, mode]);
  const replaceLook = useCallback((next: AppearanceLook) => changeAppearance({ target: mode, replace: next }), [changeAppearance, mode]);
  const selectSessionLayout = useCallback((next: SessionLayout) => changeAppearance({ sessionLayout: next }), [changeAppearance]);
  return { choice, mode, look, colors, sessionLayout, selectTheme, updateLook, replaceLook, selectSessionLayout };
}

export type AppearanceController = ReturnType<typeof useTheme>;
