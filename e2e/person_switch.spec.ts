import { test, expect } from '@playwright/test';
import { DASH, dashboardSignIn, info, login } from './helpers';

/**
 * C-10.b, overlapping and out-of-order refreshes show only the newest person, in the production
 * dashboard build against the running care server.
 *
 * The coordinator opens the first person, whose answers the browser holds back for six seconds,
 * then moves to the second person (a client-side move, as a link click is: the same page stays
 * mounted, so the first person's loads are still in flight when the second's begin and finish).
 * The first person's late answers then land. Every heading the page ever showed is recorded:
 * after the move, only the second person may appear, before or after the late answers.
 */
const i = info();
const manager = login(i, 'coordinator');
const [first, second] = i.people;

test('moving between two people while the first is still loading shows only the newest', async ({ page }) => {
  test.setTimeout(180_000);
  await page.addInitScript(() => {
    const w = window as unknown as { __headings: { t: number; text: string }[] };
    w.__headings = [];
    new MutationObserver(() => {
      const h = document.querySelector('h2')?.textContent ?? '';
      const last = w.__headings[w.__headings.length - 1];
      if (h && (!last || last.text !== h)) w.__headings.push({ t: Date.now(), text: h });
    }).observe(document, { subtree: true, childList: true, characterData: true });
  });
  await dashboardSignIn(page, manager);
  await page.goto(DASH);
  await expect(page.getByTestId(`person-${first.id}`)).toBeVisible({ timeout: 60_000 });

  let held = 0; let released = 0;
  await page.route(new RegExp(`/api/v1/care/(people/${first.id}|alerts\\?.*person_id=${first.id})`), async (route) => {
    held += 1;
    await new Promise((r) => setTimeout(r, 6_000));
    released += 1;
    await route.continue().catch(() => undefined);
  });

  await page.getByTestId(`person-${first.id}`).click();
  await expect.poll(() => held, { timeout: 20_000 }).toBeGreaterThan(0);
  const moved = await page.evaluate((id) => {
    const r = (window as unknown as { next?: { router?: { push: (u: string) => void } } }).next?.router;
    if (!r) return false;
    r.push(`/people/${id}`);
    return Date.now();
  }, second.id);
  expect(moved, 'the dashboard exposes its client-side router').toBeTruthy();

  await expect(page.locator('h2').first()).toContainText(second.display_name, { timeout: 30_000 });
  // The first person's held answers now arrive, late and out of order.
  await page.waitForTimeout(7_000);
  await expect.poll(() => held - released, { timeout: 60_000 }).toBe(0);
  expect(released).toBeGreaterThan(1);
  await page.waitForTimeout(3_000);
  await expect(page.locator('h2').first()).toContainText(second.display_name);

  const headings = await page.evaluate(() => (window as unknown as { __headings: { t: number; text: string }[] }).__headings);
  const after = headings.filter((h) => h.t >= (moved as number));
  console.log(JSON.stringify({ held, released, headings_after_move: after.map((h) => h.text) }));
  expect(after.length).toBeGreaterThan(0);
  expect(after.filter((h) => h.text.includes(first.display_name))).toEqual([]);
});
