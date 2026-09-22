# machines.d — additional machine inventories

Drop extra inventories here as `*.json` files (same format as
`~/.rrun/machines.json`). They are merged with the main inventory
by machine name:

- `~/.rrun/machines.json` wins on name conflicts (higher priority).
- Among `machines.d` files, filename order decides
  (`10-*.json` beats `90-*.json`).

Symlinks are welcome — one inventory per file, e.g.:

    ln -s ~/src/bot_home/config/machines.json ~/.rrun/machines.d/bot_home.json

Only `*.json` files are read; this README is ignored.
Full format: https://github.com/waqiju/rrun#configuration-machinesjson
