# launchd job definitions

Source-of-truth copies of the `com.nightwatch.*` LaunchAgent plists (capture,
crime, transcribe) for this machine's Night Watch pipeline. They previously
lived only in `~/Library/LaunchAgents/` and were committed nowhere, so a machine
wipe would lose the job definitions. Committed here so a restore can reinstall.

Install / reinstall (real file copy, never a symlink — TCC blocks symlinked
plists at login):

```bash
for f in launchd/*.plist; do
  cp "$f" ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/"$(basename "$f")"
done
```

Paths inside the plists are absolute and assume the `alexhedtke` short username.
On a different username, `sed -i '' "s|/Users/alexhedtke/|/Users/$USER/|g"` the
copies before bootstrapping.
