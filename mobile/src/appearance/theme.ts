/**
 * How the app looks for one person, and the rules that keep it readable.
 *
 * Appearance is the person's own: a wallpaper (a colour, a built-in image or
 * their own photo), light or dark, the text size, contrast, and the order of
 * the cards on their home screen. It is kept on the server so every device
 * they sign in on looks the same (`/me/preferences`).
 *
 * Two things never change with the wallpaper: text stays readable (a scrim is
 * laid over any image or photo, darker in dark mode, and the cards are opaque)
 * and the urgent actions (Help, "I'm on my way") keep their fixed colours.
 */

export type WallpaperKind = 'colour' | 'image' | 'photo';
export type Mode = 'system' | 'light' | 'dark';

export interface Appearance {
  wallpaper_kind: WallpaperKind;
  wallpaper_value: string;            // #rrggbb, a built-in image key, or a data URL for a photo
  mode: Mode;
  text_scale: number;                 // 0.85 to 1.8
  high_contrast: boolean;
  reduce_motion: boolean;
  card_order: string[];               // keys of the home cards, the person's order
  selected_person_id?: string | null;
}

export const DEFAULT_APPEARANCE: Appearance = {
  wallpaper_kind: 'colour', wallpaper_value: '#FAF8F4', mode: 'system', text_scale: 1.0,
  high_contrast: false, reduce_motion: false, card_order: [], selected_person_id: null,
};

/** The built-in images: soft gradients drawn by the app, so no file has to travel. */
export const BUILT_IN_IMAGES: Record<string, { name: string; from: string; to: string }> = {
  morning: { name: 'Morning', from: '#FDE8D7', to: '#C9DDEA' },
  garden: { name: 'Garden', from: '#DCEBD2', to: '#F3F0E2' },
  evening: { name: 'Evening', from: '#2F3A52', to: '#6B4E71' },
  sea: { name: 'Sea', from: '#D6ECF3', to: '#2F5D8A' },
};

export const WALLPAPER_COLOURS = ['#FAF8F4', '#F3EFE6', '#E8F0F7', '#EAF3EA', '#F7E9E4', '#1F2A24', '#14181B', '#2F3A52'];

export interface ThemeColors {
  bg: string; card: string; text: string; muted: string; border: string; accent: string;
  ok: string; notice: string; urgent: string; help: string; onHelp: string; scrim: string;
}

const LIGHT: ThemeColors = {
  bg: '#FAF8F4', card: '#FFFFFF', text: '#1F2A24', muted: '#5B6B63', border: '#E1DED8', accent: '#2F5D8A',
  ok: '#2E7D32', notice: '#2F5D8A', urgent: '#B35C00', help: '#B3261E', onHelp: '#FFFFFF', scrim: 'rgba(255,255,255,0.72)',
};
const DARK: ThemeColors = {
  bg: '#14181B', card: '#1D2327', text: '#EEF1F0', muted: '#A6B3AC', border: '#2C343A', accent: '#8AB6E0',
  ok: '#7BC67E', notice: '#8AB6E0', urgent: '#F0A868', help: '#D32F2F', onHelp: '#FFFFFF', scrim: 'rgba(0,0,0,0.62)',
};

/** Relative luminance of a #rrggbb colour, 0 (black) to 1 (white). */
export function luminance(hex: string): number {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return 1;
  const n = parseInt(m[1], 16);
  const c = [16, 8, 0].map((s) => {
    const v = ((n >> s) & 255) / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}

/** Contrast ratio between two colours (WCAG). */
export function contrast(a: string, b: string): number {
  const la = luminance(a), lb = luminance(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

export interface ResolvedTheme {
  dark: boolean;
  colors: ThemeColors;
  /** 1.0 is the system size; multiplied into every font size. */
  textScale: number;
  /** The wallpaper to draw behind everything, and the scrim that keeps text readable over it. */
  wallpaper: { kind: WallpaperKind; value: string; gradient?: [string, string] };
  scrimOpacity: number;
  reduceMotion: boolean;
}

/** The theme for an appearance, given what the system says about dark mode. */
export function resolveTheme(a: Appearance, systemScheme: 'light' | 'dark' | null | undefined): ResolvedTheme {
  const dark = a.mode === 'dark' || (a.mode === 'system' && systemScheme === 'dark') ||
    (a.mode === 'system' && a.wallpaper_kind === 'colour' && luminance(a.wallpaper_value) < 0.2);
  const base = dark ? DARK : LIGHT;
  const colors: ThemeColors = { ...base };
  if (a.high_contrast) {
    colors.text = dark ? '#FFFFFF' : '#000000';
    colors.muted = dark ? '#DDE3E0' : '#33403A';
    colors.border = dark ? '#A6B3AC' : '#1F2A24';
    colors.card = dark ? '#000000' : '#FFFFFF';
  }
  let wallpaper: ResolvedTheme['wallpaper'] = { kind: 'colour', value: dark ? colors.bg : (a.wallpaper_kind === 'colour' ? a.wallpaper_value : colors.bg) };
  if (a.wallpaper_kind === 'colour') wallpaper = { kind: 'colour', value: a.wallpaper_value || colors.bg };
  if (a.wallpaper_kind === 'image' && BUILT_IN_IMAGES[a.wallpaper_value]) {
    const g = BUILT_IN_IMAGES[a.wallpaper_value];
    wallpaper = { kind: 'image', value: a.wallpaper_value, gradient: [g.from, g.to] };
  }
  if (a.wallpaper_kind === 'photo' && a.wallpaper_value.startsWith('data:image/')) wallpaper = { kind: 'photo', value: a.wallpaper_value };
  // Text over a picture gets a scrim; over a plain colour, the colour itself must contrast with the text.
  let scrimOpacity = 0;
  if (wallpaper.kind !== 'colour') scrimOpacity = a.high_contrast ? 0.85 : dark ? 0.62 : 0.72;
  else if (contrast(wallpaper.value, colors.text) < 4.5) scrimOpacity = 0.55;
  const textScale = Math.min(1.8, Math.max(0.85, a.text_scale || 1));
  return { dark, colors, textScale, wallpaper, scrimOpacity, reduceMotion: !!a.reduce_motion };
}

/** Merge what the server holds with the defaults; unknown or broken values fall back. */
export function normaliseAppearance(raw: Partial<Appearance> | null | undefined): Appearance {
  const a = { ...DEFAULT_APPEARANCE, ...(raw || {}) };
  if (!['colour', 'image', 'photo'].includes(a.wallpaper_kind)) a.wallpaper_kind = 'colour';
  if (a.wallpaper_kind === 'colour' && !/^#[0-9a-f]{6}$/i.test(a.wallpaper_value || '')) a.wallpaper_value = DEFAULT_APPEARANCE.wallpaper_value;
  if (a.wallpaper_kind === 'image' && !BUILT_IN_IMAGES[a.wallpaper_value]) { a.wallpaper_kind = 'colour'; a.wallpaper_value = DEFAULT_APPEARANCE.wallpaper_value; }
  if (!['system', 'light', 'dark'].includes(a.mode)) a.mode = 'system';
  a.text_scale = Math.min(1.8, Math.max(0.85, Number(a.text_scale) || 1));
  a.card_order = Array.isArray(a.card_order) ? a.card_order.filter((c) => typeof c === 'string') : [];
  return a;
}

/** The home cards in the person's order: their order first, then any card they have not placed. */
export function orderCards<T extends { key: string }>(cards: T[], order: string[]): T[] {
  const byKey = new Map(cards.map((c) => [c.key, c]));
  const out: T[] = [];
  for (const k of order) { const c = byKey.get(k); if (c && !out.includes(c)) out.push(c); }
  for (const c of cards) if (!out.includes(c)) out.push(c);
  return out;
}

/** Move a card up or down one place in the order (the order is completed from `all` first). */
export function moveCard(order: string[], all: string[], key: string, direction: -1 | 1): string[] {
  const full = orderCards(all.map((k) => ({ key: k })), order).map((c) => c.key);
  const i = full.indexOf(key);
  const j = i + direction;
  if (i < 0 || j < 0 || j >= full.length) return full;
  [full[i], full[j]] = [full[j], full[i]];
  return full;
}
