import { test, expect } from '@playwright/test';
import { appSignIn, info, login } from './helpers';

/**
 * C-12.b, Talk shows IRA's answer as it arrives, in the phone app's web build signed in as the
 * wearer, against the running care server.
 *
 * The server's reply is real (POST /people/{id}/talk, one JSON line per sentence). The demo
 * server's model answers at once, so to see what the person sees while a slower model is still
 * talking, the page's fetch hands the app the server's lines one at a time, each only when the
 * test releases it. The app reads the body as a stream (mobile/src/api/stream.ts); if it waited
 * for the end of the reply, nothing would show until the last line is released.
 */
const i = info();
const wearer = login(i, 'wearer');

test('the first sentence shows before the reply ends', async ({ page }) => {
  test.setTimeout(180_000);
  await page.addInitScript(() => {
    const w = window as unknown as { __release: () => void; __talk: { status: number; lines: number; released: number } };
    const real = window.fetch.bind(window);
    let gate: (() => void)[] = [];
    w.__talk = { status: 0, lines: 0, released: 0 };
    w.__release = () => { const g = gate.shift(); if (g) g(); };
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      const response = await real(input, init);
      if (!/\/talk$/.test(url) || (init?.method ?? 'GET') !== 'POST') return response;
      const lines = (await response.text()).split('\n').filter((l) => l.trim());
      w.__talk = { status: response.status, lines: lines.length, released: 0 };
      gate = [];
      const enc = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({
        async start(controller) {
          for (const line of lines) {
            await new Promise<void>((resolve) => gate.push(resolve));
            controller.enqueue(enc.encode(line + '\n'));
            w.__talk.released += 1;
          }
          controller.close();
        },
      });
      return new Response(body, { status: response.status, headers: response.headers });
    };
  });
  await appSignIn(page, wearer);
  await page.getByTestId('home-talk').click();
  await page.getByTestId('talk-input').fill('hello');
  await page.getByTestId('talk-send').click();
  await expect(page.getByTestId('talk-you-0')).toContainText('hello');
  await expect.poll(() => page.evaluate(() => (window as unknown as { __talk: { lines: number } }).__talk.lines), { timeout: 30_000 }).toBeGreaterThan(2);
  const talk = await page.evaluate(() => (window as unknown as { __talk: { status: number; lines: number } }).__talk);
  expect(talk.status).toBe(200);

  // Nothing released: no answer yet.
  await page.waitForTimeout(1_000);
  await expect(page.getByTestId('talk-ira-1')).toHaveCount(0);

  // One line released: the first sentence shows, the rest of the reply has not come, Send waits.
  await page.evaluate(() => (window as unknown as { __release: () => void }).__release());
  await expect(page.getByTestId('talk-ira-1')).toBeVisible({ timeout: 10_000 });
  const first = await page.getByTestId('talk-ira-1').innerText();
  await page.waitForTimeout(2_000);
  await expect(page.getByTestId('talk-ira-2')).toHaveCount(0);
  await expect(page.getByTestId('talk-send')).toHaveAttribute('aria-disabled', 'true');

  // The rest released: every sentence shows, in order, and Send is ready again.
  for (let k = 1; k < talk.lines; k++) {
    await page.evaluate(() => (window as unknown as { __release: () => void }).__release());
    await page.waitForTimeout(300);
  }
  const sentences = talk.lines - 1; // the last line is {"done": true, ...}
  await expect(page.getByTestId(`talk-ira-${sentences}`)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId('talk-send')).not.toHaveAttribute('aria-disabled', 'true');
  const shown = await Promise.all(Array.from({ length: sentences }, (_, k) => page.getByTestId(`talk-ira-${k + 1}`).innerText()));
  console.log(JSON.stringify({ first, shown, lines: talk.lines }));
  expect(shown[0]).toBe(first);
});
