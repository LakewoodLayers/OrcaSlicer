# Isolated Linux development containers

`scripts/dev-container.sh` builds and runs OrcaSlicer with rootless Podman. The
normal installation's binary, configuration, networking plugins, and desktop
integration are not mounted into the development containers.

Podman image layers, containers, build cache, and temporary files are stored
under the ignored `build-dev-state/`, keeping container data on the checkout's
workspace volume. The builder uses the Ubuntu 24.04 environment from
`build_linux.sh -g`. Source
is synchronized into `build-dev-state/workspace/`, excluding Git metadata,
agent configuration, and existing build trees. The build script modifies version
and localization files, so building this copy also keeps generated changes out
of the source checkout. Dependency and application build trees persist in the
copy. Rootless UID mapping leaves outputs owned by the invoking user, and a
memory limit bounds both compilation and the running desktop. Concurrent builds
are locked, and the launcher prevents replacing binaries while the GUI runs.

The runtime mounts the packaged application read-only at `/opt/orca` and starts
it through the package launcher, which resolves its bundled libraries. The image
provides the `en_US.UTF-8` locale required by the default GUI language. Its writable
state is limited to four directories under the ignored `build-dev-state/`:

| Host directory | Container use |
| --- | --- |
| `home/` | Home directory and XDG configuration, data, and caches |
| `orca/` | Explicit `--datadir`: presets, pairing, plugins, and application logs |
| `runtime/` | Desktop logs, authentication, and the application PID |
| `exchange/` | Models and exported files, visible at `/exchange` |
| `podman-storage/` | Development images, layers, and containers |
| `podman-tmp/` | Temporary Podman build data |

The desktop has its own Xvfb display, window manager, and D-Bus session. Mesa
software rendering avoids forwarding the host GPU or desktop sockets. VNC binds
to loopback port 5901 and noVNC to loopback port 6080; a generated VNC password
persists in `runtime/desktop-password`. The container has a private process
namespace and no elevated privileges. Host networking lets printer discovery and
LAN connections work without multicast forwarding, but does not block internet
access. Offline acceptance requires a separate WAN-blocked test network.

## Usage

Linux requires Podman configured for rootless use and the host tools `rsync` and
`flock`. Dependency installation happens inside the builder image.

```sh
bash scripts/dev-container.sh build
bash scripts/dev-container.sh test
bash scripts/dev-container.sh start
bash scripts/dev-container.sh status
bash scripts/dev-container.sh stop
```

Open `http://127.0.0.1:6080/vnc.html` and use the password from
`build-dev-state/runtime/desktop-password`. The instance reuses the persistent
profiles, printer records, and plugins in `build-dev-state/orca/`, along with
settings in `build-dev-state/home/`; it does not read them live from the main
installation. Host networking allows discovery of printers on the local
network. Import and export models through `build-dev-state/exchange/`; the
normal home directory is not available to its file picker.

Saved LAN pairings can be imported while the development GUI is stopped:

```sh
python3 scripts/dev-container/import_lan.py /path/to/main/OrcaSlicer.conf build-dev-state/orca
```

The importer reads the main configuration and writes only paired Bambu LAN
printer records, their access codes, and networking-plugin version into the
development configuration. It also copies the networking and required playback
shared libraries into the development plugin directory and marks that local
plugin installation as available. Login tokens and unrelated application
settings are not imported. The source configuration is checked for changes and
development credentials are stored with owner-only permissions.

Builds default to Release with two compile jobs. `ORCA_DEV_JOBS` changes that
limit; `ORCA_DEV_MEMORY` accepts a Podman memory limit such as `8g`, defaulting to
half the currently available host RAM. Additional `build` arguments are forwarded
to `build_linux.sh`, and `test` arguments to CTest. `image` prepares the builder
and desktop images without compiling OrcaSlicer. Stopping removes only this
checkout's development container and preserves its settings and build outputs.
