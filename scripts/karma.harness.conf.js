// =============================================================================
// SDLC-Harness — Karma config for Angular repos that have NO karma.conf.js
// =============================================================================
// Angular 15+ projects without their own karma.conf.js run the CLI's built-in
// Karma config, whose coverage reporters are html + text-summary only. The
// harness coverage gate needs a machine-readable per-file report, so this file
// reproduces the Angular CLI default config and adds the Istanbul
// 'json-summary' (and 'lcovonly') reporters. Nothing in the service repo changes.
//
// Use from the service repo's .harness/config.yaml test_command:
//   npx ng test --watch=false --browsers=ChromeHeadless --code-coverage \
//       --karma-config="$HARNESS_KARMA_CONFIG"
// (the engine workflow sets HARNESS_KARMA_CONFIG to this file's path).
//
// If the repo HAS its own karma.conf.js, do NOT use this file — it would drop the
// repo's settings. Add { type: 'json-summary' } to that repo's coverageReporter
// instead (a team decision), or keep min_coverage: 0.
//
// Karma runs with the service repo as the working directory. Every plugin is
// resolved from the SERVICE repo's node_modules (this file lives in the engine
// checkout, which has none).
// =============================================================================
const path = require('path');
const repo = process.cwd();
const fromRepo = (m) => require(require.resolve(m, { paths: [repo] }));

module.exports = function (config) {
  config.set({
    basePath: repo,
    frameworks: ['jasmine', '@angular-devkit/build-angular'],
    plugins: [
      fromRepo('karma-jasmine'),
      fromRepo('karma-chrome-launcher'),
      fromRepo('karma-jasmine-html-reporter'),
      fromRepo('karma-coverage'),
      fromRepo('@angular-devkit/build-angular/plugins/karma'),
    ],
    client: {
      jasmine: {},
      clearContext: false,
    },
    jasmineHtmlReporter: { suppressAll: true },
    coverageReporter: {
      dir: path.join(repo, 'coverage', 'harness'),
      subdir: '.',
      reporters: [
        { type: 'json-summary' },   // read by the harness coverage gate
        { type: 'lcovonly' },
        { type: 'text-summary' },   // printed in the run log
      ],
    },
    reporters: ['progress', 'kjhtml'],
    browsers: ['ChromeHeadless'],
    customLaunchers: {
      // GitHub-hosted runners do not need --no-sandbox; containers running as
      // root do. Select with --browsers=ChromeHeadlessNoSandbox if required.
      ChromeHeadlessNoSandbox: { base: 'ChromeHeadless', flags: ['--no-sandbox'] },
    },
    singleRun: true,
    restartOnFileChange: false,
  });
};
