# Configuration and logging

`tw` reads its settings from the command line and from one optional TOML
file. This page describes the file, the environment variables that locate it,
and the logging and progress options.

## Config file

The config file sets defaults for the global options `--port` and
`--profile`, so you do not have to pass them on every command.

```toml
profile = "colorado"
port = "/dev/cu.usbmodem1101"
```

| Key | Type | Description |
|---|---|---|
| `profile` | string | Default for `--profile`: a profile name or a path to a `.toml` file. See [Drive profiles](profiles.md). |
| `port` | string | Default for `--port`: the Greaseweazle's serial port. |

These are the only keys. `tw` warns about any other key and ignores it:

```text
WARNING  config /Users/me/.config/tapewyrm/config.toml: unknown key 'prfile' ignored (known keys: port, profile)
```

A file that is not valid TOML stops every command with `could not read config
PATH: ...`.

### Where tw looks

| Condition | File |
|---|---|
| `--config FILE` is given | `FILE`, and only that file. If it does not exist, `tw` uses the built-in defaults. |
| Windows | `%APPDATA%\tapewyrm\config.toml` |
| `XDG_CONFIG_HOME` is set to an absolute path | `$XDG_CONFIG_HOME/tapewyrm/config.toml` |
| Otherwise (Linux, macOS) | `~/.config/tapewyrm/config.toml` |

A relative `XDG_CONFIG_HOME` is ignored, as the XDG specification requires.
The per-user file is read only if it exists.

### Precedence

For each setting, the first of these that sets it wins:

1. The command-line option (`--port`, `--profile`).
2. The config file.
3. The built-in default: `auto` for the profile; for the port, the one
   attached Greaseweazle found by its USB ID.

`tw -v COMMAND` logs which config file it looked for, whether it loaded it,
and which profile it picked.

## Environment

| Variable | Effect |
|---|---|
| `XDG_CONFIG_HOME` | Base directory for the per-user config file, on systems other than Windows. |
| `APPDATA` | Base directory for the per-user config file on Windows. If it is unset, `tw` uses `~\AppData\Roaming`. |

`tw` reads no other environment variables.

## Logging

Logs go to stderr; results go to stdout. Each log line has a time stamp and a
level. The level is set by the global `-v` and `-q` options, counted from
INFO:

| Options | Level | Shows |
|---|---|---|
| `-qq` | ERROR | Errors only. |
| `-q` | WARNING | Warnings and errors. |
| none | INFO | The narrative: each track, each wake-up try, each convert stage. |
| `-v` | DEBUG | Every decision, guard and command, with the module that logged it. |

Use `-v` when something fails and you want to know why. The level applies to
the Tapewyrm packages only (`tapewyrm`, `tapewyrm_archive`, `qiclib`);
third-party libraries stay at WARNING.

Options go before the subcommand: `tw -v drive status`.

## Progress

`--progress` shows progress bars on stderr: tracks, bytes and segments for
`tw dump`, captures and decode stages for `tw convert`. The bars and the log
share one console, so log lines print above the bars without breaking them.
Without `--progress`, nothing is redrawn and the output suits a log file.

`qicsilver` takes the same `--progress`, `-v` and `-q` options.

## Examples

Set the profile once:

```sh
mkdir -p ~/.config/tapewyrm
printf 'profile = "colorado"\n' > ~/.config/tapewyrm/config.toml
```

Use a config file for one bench setup:

```sh
tw --config bench-teac.toml drive status
```

Keep a quiet log of a long dump:

```sh
tw -q --profile colorado dump captures/mytape 2> captures/mytape-dump.log
```

## See also

- [tw](index.md)
- [Drive profiles](profiles.md)
- [Troubleshooting](../guide/troubleshooting.md)
