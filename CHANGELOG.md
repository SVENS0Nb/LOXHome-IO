# Changelog

## 0.11.1

- Add an explicit per-door five-second display fallback after a successfully
  sent open command when no definite Loxone lock status exists. Disabled by
  default; no lock command, automation, credential or Miniserver change.
- Mark both phases as assumed state with a question-mark lock icon and source
  attributes. Real feedback always takes precedence; repeated opens replace the
  timer, failed commands cannot create an estimate, and disconnect/reload clears
  it. Initial state stays unknown rather than claiming a secure door at startup.
- Add synthetic timer/race/feedback regression tests and German/English labels.

## 0.11.0

- Add separately selected lock profiles for NFC access outputs and door-release
  Pushbuttons without replacing existing buttons or enabling actions by default.
- Mirror native WindowMonitor or explicitly selected existing Loxone digital
  lock-status feedback. Missing/invalid feedback stays unknown; no optimistic
  state changes, helper automations, or interpretation of control-lock flags.
- Clear stale lock feedback across disconnect/reconnect and reject events from
  another Miniserver configuration.
- Keep existing native door identities. Validate state masks without truncating
  malformed values and clear missing entries instead of retaining stale values.
- Send secured mapped actions only through Loxone's authenticated path; refuse
  requests without the required visualization password and never persist it.
- Add German/English setup guidance and synthetic regression tests. Publication
  does not install/reload any live integration or operate a door.
