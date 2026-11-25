import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { render, screen } from '@testing-library/react';
import { axe } from 'jest-axe';
import { renderToStaticMarkup } from 'react-dom/server';
import nextConfig from '../../next.config';
import LicencesPage from '@/app/licences/page';
import { Licences } from '@/components/Licences';
import { SiteFooter } from '@/components/SiteFooter';
import { isDemoPath } from '@/lib/demoPages';
import { NOTICES_FILE, noticesTitle, readNotices } from '@/lib/notices';

/**
 * The dashboard shows its third-party notices at /licences, from its own copy of
 * docs/legal/notices/dashboard-image.txt (written by `scripts/supply_chain.py render`), and the
 * standalone output the image runs carries that copy.
 */
const DASHBOARD = join(__dirname, '..', '..');
const source = readFileSync(join(DASHBOARD, '..', 'docs', 'legal', 'notices', 'dashboard-image.txt'), 'utf-8');
const pkg = JSON.parse(readFileSync(join(DASHBOARD, 'package.json'), 'utf-8')) as { dependencies: Record<string, string> };

/** Runtime dependencies the text does not name at the exact version package.json pins. */
function unlisted(text: string): string[] {
  return Object.entries(pkg.dependencies).filter(([name, v]) => !text.includes(`\n${name} ${v}\n`)).map(([name]) => name);
}

test('the bundled copy is the dashboard image notices file, unedited', () => {
  const text = readNotices();
  expect(text).toBe(source);
  expect(noticesTitle(text)).toBe('IRA CARE carer dashboard container image (deploy/Dockerfile.dashboard)');
  expect(unlisted(text)).toEqual([]);
  // Negative control: notices without one of the packages are caught.
  expect(unlisted(text.replace(`\nnext ${pkg.dependencies.next}\n`, '\n'))).toEqual(['next']);
});

test('a missing file or one that is not a notices file is an error, never an empty page', () => {
  const dir = mkdtempSync(join(tmpdir(), 'notices-'));
  expect(() => readNotices(join(dir, 'absent.txt'))).toThrow();
  writeFileSync(join(dir, 'other.txt'), 'hello\n');
  expect(() => readNotices(join(dir, 'other.txt'))).toThrow(/not a third-party notices file/);
});

test('/licences shows the whole text, with a heading, and adds no script', () => {
  render(<LicencesPage />);
  expect(screen.getByRole('heading', { level: 2, name: 'Open-source licences' })).toBeInTheDocument();
  const pre = screen.getByTestId('notices');
  expect(pre.textContent).toBe(source);
  expect(pre.getAttribute('aria-label')).toBe('Third-party notices');
  expect(pre.getAttribute('tabindex')).toBe('0'); // a scrolling region reachable by keyboard
  const html = renderToStaticMarkup(<LicencesPage />);
  expect(html).not.toMatch(/<script|on[a-z]+=/i);
});

test('negative control: the page shows what it is given, so the check above is real', () => {
  const other = 'THIRD-PARTY NOTICES: something else\n\nleft-pad 1.0.0\n';
  render(<Licences text={other} />);
  expect(screen.getByTestId('notices').textContent).toBe(other);
  expect(unlisted(screen.getByTestId('notices').textContent ?? '')).toContain('next');
});

test('the licences page has no accessibility violations', async () => {
  const { container } = render(<Licences text={source.slice(0, 4000)} />);
  expect(await axe(container)).toHaveNoViolations();
});

test('every page links to the licences, and the page is public and not a demonstration page', () => {
  render(<SiteFooter />);
  expect(screen.getByRole('link', { name: 'Open-source licences' }).getAttribute('href')).toBe('/licences');
  expect(isDemoPath('/licences')).toBe(false);
});

test('the standalone build carries the notices file and leaves the TypeScript compiler out', () => {
  const includes = nextConfig.outputFileTracingIncludes ?? {};
  expect(includes['/licences']).toEqual([`./${NOTICES_FILE}`]);
  expect(readFileSync(join(DASHBOARD, NOTICES_FILE), 'utf-8')).toBe(source);
  expect(nextConfig.outputFileTracingExcludes?.['*']).toContain('node_modules/typescript/**');
});
