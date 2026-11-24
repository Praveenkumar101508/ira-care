import { contrast, moveCard, normaliseAppearance, orderCards, resolveTheme, DEFAULT_APPEARANCE } from '../appearance/theme';

test('dark mode follows the person, then the system, then a dark wallpaper colour', () => {
  expect(resolveTheme({ ...DEFAULT_APPEARANCE, mode: 'dark' }, 'light').dark).toBe(true);
  expect(resolveTheme({ ...DEFAULT_APPEARANCE, mode: 'light' }, 'dark').dark).toBe(false);
  expect(resolveTheme({ ...DEFAULT_APPEARANCE, mode: 'system' }, 'dark').dark).toBe(true);
  expect(resolveTheme({ ...DEFAULT_APPEARANCE, wallpaper_value: '#14181B' }, 'light').dark).toBe(true);
});

test('text stays readable over any wallpaper: a scrim over pictures, over a clashing colour, none over a calm one', () => {
  const photo = resolveTheme({ ...DEFAULT_APPEARANCE, wallpaper_kind: 'photo', wallpaper_value: 'data:image/png;base64,AAAA' }, 'light');
  expect(photo.scrimOpacity).toBeGreaterThanOrEqual(0.6);
  const image = resolveTheme({ ...DEFAULT_APPEARANCE, wallpaper_kind: 'image', wallpaper_value: 'evening' }, 'light');
  expect(image.scrimOpacity).toBeGreaterThan(0) ; expect(image.wallpaper.gradient).toBeTruthy();
  const calm = resolveTheme(DEFAULT_APPEARANCE, 'light');
  expect(calm.scrimOpacity).toBe(0);
  expect(contrast(calm.wallpaper.value, calm.colors.text)).toBeGreaterThanOrEqual(4.5);
  const clash = resolveTheme({ ...DEFAULT_APPEARANCE, mode: 'light', wallpaper_value: '#556655' }, 'light');
  expect(clash.scrimOpacity).toBeGreaterThan(0);
});

test('urgent actions keep their colours and contrast in every theme', () => {
  for (const a of [DEFAULT_APPEARANCE, { ...DEFAULT_APPEARANCE, mode: 'dark' as const }, { ...DEFAULT_APPEARANCE, high_contrast: true }]) {
    const t = resolveTheme(a, 'light');
    expect(contrast(t.colors.help, t.colors.onHelp)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(t.colors.text, t.colors.card)).toBeGreaterThanOrEqual(a.high_contrast ? 7 : 4.5);
  }
});

test('text size and broken server values are brought back into range', () => {
  const a = normaliseAppearance({ text_scale: 9, wallpaper_kind: 'image', wallpaper_value: 'nope', mode: 'blue' as never, card_order: ['help', 3 as never] });
  expect(a.text_scale).toBe(1.8); expect(a.wallpaper_kind).toBe('colour'); expect(a.mode).toBe('system'); expect(a.card_order).toEqual(['help']);
  expect(resolveTheme({ ...DEFAULT_APPEARANCE, text_scale: 0.1 }, 'light').textScale).toBe(0.85);
});

test('home cards follow the person\'s order and new cards join at the end; moving is by one place', () => {
  const cards = [{ key: 'next_visit' }, { key: 'reminders' }, { key: 'help' }, { key: 'talk' }];
  expect(orderCards(cards, ['help', 'talk']).map((c) => c.key)).toEqual(['help', 'talk', 'next_visit', 'reminders']);
  expect(moveCard(['help', 'talk'], cards.map((c) => c.key), 'talk', 1)).toEqual(['help', 'next_visit', 'talk', 'reminders']);
  expect(moveCard([], cards.map((c) => c.key), 'next_visit', -1)).toEqual(['next_visit', 'reminders', 'help', 'talk']);
});
