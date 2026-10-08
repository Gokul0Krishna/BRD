# bootdetective

Your Arch boot got slower. Which `pacman -Syu` did it?

bootdetective records how long each boot takes (total, per phase, per systemd unit), reads
`/var/log/pacman.log`, and uses changepoint detection to find the moment boot time
shifted. It then lists the packages upgraded right before that boot, best suspect first.

```
Boot time rose from ~14.8s to ~19.0s (+4.2s, +28%) at boot #42 (2026-09-14 21:03)
  Units that got slower:
    NetworkManager-wait-online.service           +3.9s
  Packages changed between boot #41 and #42, best suspect first:
    1. networkmanager 1.46.0-1 -> 1.48.0-1  (score 7.5)
         - ships NetworkManager-wait-online.service, which got 3.9s slower
         - part of the boot path (kernel, firmware, init, drivers, network)
         - new upstream version
```

## Install

```
makepkg -si -D packaging         # or, once published: yay -S bootdetective
sudo systemctl enable --now bootdetective-collect.service
sudo bootdetective backfill      # optional: import past boots from the journal
```

## Use

```
bootdetective history            # recent boots, with a trend line
bootdetective report             # find changes and likely culprits
bootdetective explain 42         # break down one boot (default: the latest)
bootdetective plot boots.png     # chart (needs python-matplotlib)
```

You need about 10 boots of history before `report` can say anything. Changes are detected
reliably when they are larger than roughly 3x your normal boot-to-boot noise.

## What it stores

A SQLite file at `/var/lib/bootdetective/bootdetective.db`: boot times, per-unit start times,
and package names/versions from pacman.log. No network access, no telemetry, nothing leaves
your machine.

## Uninstall

```
sudo systemctl disable --now bootdetective-collect.service
sudo pacman -Rns bootdetective
sudo rm -r /var/lib/bootdetective     # the collected data
```

## Develop

```
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```
