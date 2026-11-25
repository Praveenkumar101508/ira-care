/**
 * @jest-environment node
 */
// The build tooling runs on patched versions that Expo SDK 52 does not ask for: npm
// `overrides` in package.json pin tar 7 and image-size 2 (audit advisories in tar 6 and
// image-size 1), and patch-package adapts the two callers to the new module shapes
// (patches/). These tests fail if either override or either patch goes missing: Metro then
// cannot size an image asset, and `expo prebuild` cannot unpack its project template.
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/* eslint-disable @typescript-eslint/no-var-requires */
const assets = require('metro/src/Assets');
const npmUtils = require('@expo/cli/build/src/utils/npm');
const tar = require('tar');
/* eslint-enable @typescript-eslint/no-var-requires */

const MODULES = path.join(__dirname, '..', '..', 'node_modules');
const ICON = path.join(__dirname, '..', '..', 'assets', 'icon.png');

function pngSize(file: string): { width: number; height: number } {
  // A PNG's IHDR chunk holds the width and height as big-endian 32-bit integers.
  const buf = fs.readFileSync(file);
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

describe('build tooling on the patched dependency versions', () => {
  it('runs the patched versions, not the ones with advisories', () => {
    const version = (name: string): string =>
      JSON.parse(fs.readFileSync(path.join(MODULES, name, 'package.json'), 'utf8')).version;
    expect(version('tar')).toBe('7.5.23');
    expect(version('image-size')).toBe('2.1.0');
  });

  it('Metro sizes an image asset from a buffer', () => {
    const content = fs.readFileSync(ICON);
    expect(assets.getAssetSize('png', content, ICON)).toEqual(pngSize(ICON));
  });

  it('Metro sizes an image asset from its file path, as the bundler does for app images', async () => {
    const data = await assets.getAssetData(ICON, 'assets/icon.png', [], 'android', '/assets');
    expect({ width: data.width, height: data.height }).toEqual(pngSize(ICON));
  });

  it('Expo unpacks an npm tarball with the template renames prebuild relies on', async () => {
    const work = fs.mkdtempSync(path.join(os.tmpdir(), 'ira-tar-'));
    try {
      const src = path.join(work, 'src');
      fs.mkdirSync(path.join(src, 'package', 'android', 'helloworld'), { recursive: true });
      fs.writeFileSync(path.join(src, 'package', 'android', 'helloworld', 'Main.kt'), 'main');
      fs.writeFileSync(path.join(src, 'package', 'gitignore'), 'node_modules\n');
      const tarball = path.join(work, 'template.tgz');
      tar.create({ gzip: true, file: tarball, cwd: src, sync: true }, ['package']);

      const out = path.join(work, 'out');
      await npmUtils.extractLocalNpmTarballAsync(tarball, { cwd: out, name: 'IraCare' });

      expect(fs.readFileSync(path.join(out, 'android', 'iracare', 'Main.kt'), 'utf8')).toBe('main');
      expect(fs.existsSync(path.join(out, '.gitignore'))).toBe(true);
      expect(fs.existsSync(path.join(out, 'gitignore'))).toBe(false);
    } finally {
      fs.rmSync(work, { recursive: true, force: true });
    }
  });
});
