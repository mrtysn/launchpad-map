# launchpad-map

Reads and rewrites the Launchpad layout (and the phone's and tablet's home screens) through
`bin/launchpad-map`, with `lib/` behind it, and shows the history in the Home Screens app
(`app/`, on the shared `MacAppStyle` package from project-lifecycle). Operator guide: README.md.

- Build, test and install the app through `project-lifecycle` (`test`, `build`, `install`, `status`),
  with `PROJECT_LIFECYCLE_PACKAGE` set to the project-lifecycle checkout so the shared package comes
  from disk. The bundle id is `local.launchpad-map.home-screens`; the manifest is `project-lifecycle.json`.
- The app is a window over `bin/launchpad-map`: it runs the commands of the checkout it was installed
  from and reimplements none of them.
- `project-lifecycle test` runs `HOME_SCREENS_SELFTEST=1` (`app/SelfTest.swift`): the window over a
  throwaway checkout in a temporary directory, holding a stub `bin/launchpad-map` that logs each call
  to `calls.log`. Every control is pressed and the logged commands checked. The real tool, layouts,
  Launchpad database, Dock and devices are never touched; the run refuses to start if any path the app
  derives from the checkout points outside the temporary one. The shared window checks (fits the
  screen, resizes, nothing focused at launch) run at exit and fail the run with `self-test: window: ...`;
  they print nothing when they pass. A new control gets a check in `SelfTestRun.run()`; a new dialog
  goes through `Controller.answerForm`, `answerPlan` or `answerFile` so the test can answer it.
- `app/snapshot.sh` renders the window states (ready, running, error, long error), light and dark, from
  `app/fixtures/` through `quiet-open` into `staging/` (gitignored). Look at the PNGs after any layout
  change.
- Look: `Style.Palette`, `Style.Typography`, no colour or font literals. Rules in project-lifecycle's
  `docs/STYLE.md`.
- `layouts/*.json` and `showcase.json` are inventories of one machine's apps and stay gitignored.
- Comments state what things are, never history.
