import { test, expect } from '@playwright/test';
import { Api, DASH, dashboardSignIn, info, login, type Login } from './helpers';

/**
 * Deployment administration (D-085, D-089) in a real browser against the staging stack.
 *
 * 1. A care account that holds no deployment administrator grant (the demo coordinator, a care
 *    manager, holds none: the grant is never a care role) opens /admin/devices and sees the
 *    refusal, with no field, button or watch anywhere on the page, and no "Deployment" link.
 * 2. With E2E_DEPLOYMENT_ADMIN=1 and an account the operator has granted
 *    (scripts/deployment_admin.py), named by E2E_DEPLOYMENT_ADMIN_EMAIL and
 *    E2E_DEPLOYMENT_ADMIN_PASSWORD (or E2E_DEPLOYMENT_ADMIN_KEY, a login key in the demo
 *    file), the administrator opens the screen, chooses a seeded person's paired watch, sends a
 *    one-minute maintenance window with a reason (confirming who they are if the server asks),
 *    sees the row appear, and revokes it. Without the grant the server itself would refuse, so
 *    the admin flow runs only when the stack has been prepared for it.
 */
const i = info();
const carer = login(i, 'coordinator');

const ADMIN_ON = process.env.E2E_DEPLOYMENT_ADMIN === '1';
function adminLogin(): Login {
  const key = process.env.E2E_DEPLOYMENT_ADMIN_KEY;
  if (key) return login(i, key);
  const email = process.env.E2E_DEPLOYMENT_ADMIN_EMAIL ?? '';
  const password = process.env.E2E_DEPLOYMENT_ADMIN_PASSWORD ?? '';
  return { key: 'deployment-admin', name: 'Deployment administrator', email, password, role: 'member' };
}

test('a carer who is not a deployment administrator sees the refusal and no controls', async ({ page }) => {
  await dashboardSignIn(page, carer);
  await expect(page.getByTestId('nav-deployment')).toHaveCount(0);
  await page.goto(`${DASH}/admin/devices`); await page.waitForLoadState('networkidle');
  await expect(page.getByTestId('not-admin')).toContainText('This area is for deployment administrators.');
  await expect(page.getByTestId('device-admin')).toHaveCount(0);
  const main = page.getByTestId('not-admin');
  await expect(main.getByRole('textbox')).toHaveCount(0);
  await expect(main.getByRole('combobox')).toHaveCount(0);
  await expect(main.getByRole('button')).toHaveCount(0);
  await expect(page.getByText(/maintenance window|factory reset/i)).toHaveCount(0);
  await expect(page.getByRole('link', { name: /Back to the people/ })).toHaveAttribute('href', '/');
});

test.describe('a deployment administrator', () => {
  test.skip(!ADMIN_ON, 'set E2E_DEPLOYMENT_ADMIN=1 with a granted account to run the administrator flow');

  test('sends a maintenance window to a paired watch, sees the row, and revokes it', async ({ page }) => {
    const admin = adminLogin();
    expect(admin.email, 'E2E_DEPLOYMENT_ADMIN_EMAIL or E2E_DEPLOYMENT_ADMIN_KEY names the granted account').toBeTruthy();
    // A paired watch of a seeded person, by the ordinary care routes.
    const api = await Api.signIn(i, carer);
    let deviceId = process.env.E2E_DEPLOYMENT_ADMIN_DEVICE ?? '';
    if (!deviceId) {
      for (const p of i.people) {
        const { devices } = await api.req<{ devices: { id: string; status: string }[] }>('GET', `/api/v1/care/people/${p.id}/devices`);
        const paired = devices.find((d) => d.status === 'paired');
        if (paired) { deviceId = paired.id; break; }
      }
    }
    expect(deviceId, 'a seeded person has a paired watch').toBeTruthy();

    await dashboardSignIn(page, admin);
    await expect(page.getByTestId('nav-deployment')).toBeVisible();
    await page.getByTestId('nav-deployment').click();
    await page.waitForURL(/\/admin\/devices/);
    await expect(page.getByTestId('device-admin')).toBeVisible();
    await expect(page.getByTestId('not-admin')).toHaveCount(0);
    // The tenant and workspace are filled from the sign-in; only the watch is chosen.
    await expect(page.getByLabel('Tenant id')).not.toHaveValue('');
    await expect(page.getByLabel('Workspace id')).toHaveValue(i.workspace_id);
    await page.getByLabel('Watch id').fill(deviceId);
    await expect(page.getByTestId('live')).toContainText(/Live: updated/, { timeout: 15_000 });

    const reason = 'End-to-end check of the maintenance window';
    await page.getByLabel('Minutes').fill('1');
    await page.getByLabel('Reason').fill(reason);
    await page.getByRole('button', { name: 'Send to the watch' }).click();
    // The server may ask the administrator to confirm who they are first.
    const stepUp = page.getByTestId('step-up');
    if (await stepUp.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await stepUp.getByLabel('Password').fill(admin.password);
      const code = process.env.E2E_DEPLOYMENT_ADMIN_TOTP;
      if (code) await stepUp.getByLabel('Authenticator code').fill(code);
      await stepUp.getByRole('button', { name: 'Confirm and continue' }).click();
    }
    await expect(page.getByTestId('notice')).toContainText(/^Signed\./, { timeout: 15_000 });
    const row = page.getByTestId('instruction-row').filter({ hasText: reason }).first();
    await expect(row).toBeVisible();
    await expect(row.getByTestId('status')).toHaveText(/Signed|Collected|Applied/);

    await row.getByRole('button', { name: 'Revoke' }).click();
    await row.getByLabel('Why revoke').fill('End-to-end check finished');
    await row.getByRole('button', { name: 'Confirm revoke' }).click();
    await expect(page.getByTestId('notice')).toContainText('Revoked', { timeout: 15_000 });
    await expect(page.getByTestId('instruction-row').filter({ hasText: reason }).first().getByTestId('status')).toHaveText('Revoked');
  });
});
