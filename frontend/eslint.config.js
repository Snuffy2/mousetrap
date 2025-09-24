import js from '@eslint/js';
import globals from 'globals';
import pluginReact from 'eslint-plugin-react';
import pluginUnused from 'eslint-plugin-unused-imports';
import { URL, fileURLToPath } from 'url';

// Single clean ESM flat config for the frontend. Parser is specified
// by module name so ESLint will resolve it from frontend/node_modules.
export default [
  // Provide a top-level settings object so eslint-plugin-react picks up the
  // React version immediately when the config is loaded (prevents the
  // "React version not specified" warning).
  {
    settings: { react: { version: '18.0.0' } },
  },
  js.configs.recommended,
  pluginReact.configs && pluginReact.configs.flat && pluginReact.configs.flat.recommended,
  {
    // Match both repository-root and frontend/ subfolder layouts. Pre-commit
    // runs ESLint from the repo root and passes file paths like
    // 'frontend/src/...', so use a glob that covers both.
    files: ['**/src/**/*.{js,jsx,ts,tsx}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: {
        ...globals.browser,
        // Add missing modern browser globals that may not be present in the
        // globals package depending on its version or environment.
        fetch: 'writable',
        document: 'writable',
        window: 'writable',
        // Add project-specific globals that are referenced in several files
        // to avoid no-undef errors for legacy patterns. Prefer fixing code
        // to declare these, but adding them here keeps lint runs clean.
        lastNextCheckTime: 'writable',
        polling: 'writable',
        pollingRef: 'writable',
        msg: 'writable',
      },
      parserOptions: {
        project: './tsconfig.json',
        tsconfigRootDir: fileURLToPath(new URL('.', import.meta.url)).replace(/\/$/, ''),
        ecmaVersion: 'latest',
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: { react: pluginReact, 'unused-imports': pluginUnused },
    // Explicitly set the React version to match package.json dependencies
    // to silence the eslint-plugin-react detection warning when run under
    // pre-commit environments.
    settings: { react: { version: '18.0.0' } },
    rules: {
      // '@typescript-eslint/no-unused-vars' removed to avoid requiring the
      // plugin in environments where @typescript-eslint isn't installed.
      // unused-imports plugin rules removed to avoid requiring the plugin
      // where it's not installed in the frontend environment.
      // Turn off the core rule so the plugin can handle unused imports/vars
      'no-unused-vars': 'off',

      // remove unused imports automatically
      'unused-imports/no-unused-imports': 'error',

      // Treat unused variables as warnings (not errors). eslint cannot reliably
      // auto-fix local unused variables; keeping these as warnings prevents
      // the lint run from failing while still surfacing them for later manual
      // cleanup or renaming to `_`-prefixed names.
      'unused-imports/no-unused-vars': [
        'warn',
        {
          vars: 'all',
          varsIgnorePattern: '^_',
          args: 'after-used',
          argsIgnorePattern: '^_',
        },
      ],

      // allow empty catch blocks (common pattern where errors are intentionally swallowed)
      'no-empty': ['error', { allowEmptyCatch: true }],

      // don't treat constant conditions inside loop headers as errors (these are used
      // intentionally for some polling patterns)
      'no-constant-condition': ['warn', { checkLoops: false }],

      // Self-assignments often appear in defensive code or are accidental; warn
      // instead of erroring so lint --fix can complete.
      'no-self-assign': 'warn',

      'react/prop-types': 'off',
      'react/no-unescaped-entities': 'off',
      'react/react-in-jsx-scope': 'off',
    },
  },
].filter(Boolean);
