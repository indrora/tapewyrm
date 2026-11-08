# Install the tools

This page sets up `tw`, `qicsilver` and `cinnabar` to run from a clone of the
repository. Nothing is installed system-wide: `uv` creates a virtual
environment per package and fetches the dependencies on first use.

## Prerequisites

- **Python 3.11 or newer.** Every package sets `requires-python = ">=3.11"`.
  `uv` can download a suitable Python for you.
- **[uv](https://docs.astral.sh/uv/).** Runs each command in its package's
  environment.
- **[git](https://git-scm.com/).** To clone the repository.
- **[just](https://github.com/casey/just)** (optional). A task runner; the
  repository's `justfile` wraps the common commands.
- **[PlatformIO Core](https://platformio.org/install/cli)** (optional). Only
  if you build the firmware yourself. See
  [Get a firmware image](flash-greaseweazle.md#get-a-firmware-image).

## Install

1. Clone the repository and change into it.

    ```sh
    git clone https://github.com/indrora/tapewyrm.git
    cd tapewyrm
    ```

    Every command in these docs runs from the repository root.

2. Run `tw` once.

    ```sh
    uv run --project packages/tapewyrm-cli tw --version
    ```

    The first run creates `packages/tapewyrm-cli/.venv` and installs the
    dependencies, then prints the version. Later runs start at once.

3. Run `qicsilver` and `cinnabar` once the same way.

    ```sh
    uv run --project packages/qicsilver qicsilver --version
    uv run --project packages/cinnabar cinnabar --version
    ```

    `cinnabar` is a desktop viewer (Qt). It is optional; the recovery
    pipeline does not need it.

4. Define shell aliases so the rest of the docs read as plain commands.

    ```sh
    alias tw='uv run --project packages/tapewyrm-cli tw'
    alias qicsilver='uv run --project packages/qicsilver qicsilver'
    alias cinnabar='uv run --project packages/cinnabar cinnabar'
    ```

    The aliases only work from the repository root, because `--project` takes
    a relative path. Put them in your shell profile with absolute paths if you
    want them everywhere.

    The `tapewyrm-cli` package installs the same command under two names, `tw`
    and `tapewyrm`. The docs use `tw`.

## Use just instead

If you installed `just`, these recipes do the same thing from the repository
root:

| Recipe | Runs |
|---|---|
| `just tapewyrm ARGS` | `tw ARGS` |
| `just qicsilver ARGS` | `qicsilver ARGS` |
| `just cinnabar ARGS` | `cinnabar ARGS` |
| `just fw` | builds the firmware into `dist/` (needs PlatformIO) |
| `just dfu [BIN]` | `tw dfu BIN` |
| `just update [IMAGE]` | `tw update IMAGE` |
| `just package` | builds the host wheels and the firmware into `dist/` |

`just --list` shows every recipe. The development recipes (`just host`,
`just test`) are described in [Building and testing](../dev/building.md).

## Verify

Run the help for each command. None of these talk to hardware.

```sh
tw --help
qicsilver --help
```

You should see the list of subcommands. `tw --help` lists `convert`, `dfu`,
`drive`, `dump`, `update`, `info` and `inspect`.

## Next steps

- [Flash the Greaseweazle](flash-greaseweazle.md).
- [tw reference](../tw/index.md).
