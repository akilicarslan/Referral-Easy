import { describe, expect, it } from 'vitest';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

/**
 * The architectural rule, enforced mechanically.
 *
 * "Nothing outside a single adapter module may import `@a2ui/*`" is easy to
 * agree with in review and easy to break in a hurry. A grep is not a
 * sophisticated check, but it is one that cannot be forgotten, and the whole
 * value of the boundary is that it survives the person who set it up.
 */

/**
 * The package root.
 *
 * Taken from the working directory rather than from `import.meta.url`, which is
 * only a `file:` URL under some vitest environment/reporter combinations — and
 * `fileURLToPath` throws when it is not, failing the whole file at collection
 * time with an error that says nothing about the boundary. The first test below
 * asserts the assumption instead of trusting it.
 */
const repoRoot = process.cwd();
const sourceRoot = join(repoRoot, 'src');
const testRoot = join(repoRoot, 'test');

/** The one file allowed to reach across. */
const ADAPTER = join('src', 'a2ui', 'adapter.ts');

function filesUnder(directory: string): string[] {
  const results: string[] = [];
  for (const entry of readdirSync(directory)) {
    const full = join(directory, entry);
    if (statSync(full).isDirectory()) {
      results.push(...filesUnder(full));
    } else if (/\.(ts|tsx)$/.test(entry)) {
      results.push(full);
    }
  }
  return results;
}

/** Import specifiers mentioning `@a2ui`, however they are written. */
function a2uiSpecifiers(source: string): string[] {
  const matches = source.matchAll(/from\s+['"](@a2ui\/[^'"]+)['"]/g);
  const dynamic = source.matchAll(/import\(\s*['"](@a2ui\/[^'"]+)['"]\s*\)/g);
  return [...matches, ...dynamic].map((match) => match[1] ?? '');
}

describe('the A2UI adapter boundary', () => {
  it('runs from the web package root, so the scans below are meaningful', () => {
    expect(existsSync(join(repoRoot, 'package.json')), `no package.json in ${repoRoot}`).toBe(true);
    expect(existsSync(sourceRoot), `no src/ in ${repoRoot}`).toBe(true);
    expect(existsSync(testRoot), `no test/ in ${repoRoot}`).toBe(true);
  });

  it('is crossed by exactly one source file', () => {
    const offenders = [
      ...filesUnder(sourceRoot),
      ...(statSync(testRoot).isDirectory() ? filesUnder(testRoot) : []),
    ]
      .map((file) => ({ file: relative(repoRoot, file), specifiers: a2uiSpecifiers(readFileSync(file, 'utf8')) }))
      .filter((entry) => entry.specifiers.length > 0)
      .filter((entry) => entry.file !== ADAPTER);

    expect(
      offenders,
      `Only ${ADAPTER} may import @a2ui/*. Found:\n${offenders
        .map((entry) => `  ${entry.file} -> ${entry.specifiers.join(', ')}`)
        .join('\n')}`,
    ).toEqual([]);
  });

  it('is actually crossed, so the test cannot pass by accident', () => {
    const adapter = readFileSync(join(repoRoot, ADAPTER), 'utf8');
    expect(a2uiSpecifiers(adapter).length).toBeGreaterThan(0);
  });

  it('targets the v0_9 protocol entry point, not v0_8 or v1_0', () => {
    const adapter = readFileSync(join(repoRoot, ADAPTER), 'utf8');
    for (const specifier of a2uiSpecifiers(adapter)) {
      if (specifier === '@a2ui/web_core' || specifier === '@a2ui/lit') {
        continue;
      }
      expect(specifier).toMatch(/v0_9/);
    }
  });

  it('keeps the pinned versions in package.json and the adapter in step', () => {
    const pkg = JSON.parse(readFileSync(join(repoRoot, 'package.json'), 'utf8')) as {
      dependencies: Record<string, string>;
    };
    const adapter = readFileSync(join(repoRoot, ADAPTER), 'utf8');

    const core = pkg.dependencies['@a2ui/web_core'];
    const lit = pkg.dependencies['@a2ui/lit'];
    expect(core).toBeDefined();
    expect(lit).toBeDefined();

    // Exact pins, because the package is protocol-versioned: a minor bump may
    // move which wire protocol is `current`.
    expect(core).toMatch(/^\d+\.\d+\.\d+$/);
    expect(lit).toMatch(/^\d+\.\d+\.\d+$/);

    expect(adapter).toContain(`A2UI_CORE_VERSION = '${core}'`);
    expect(adapter).toContain(`A2UI_LIT_VERSION = '${lit}'`);
  });
});
